"""python -m scripts.run_local: PostgreSQL, Ollama и MAX на одном Mac/Linux."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from ip_bot.max_api import APIError, MaxAPI


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data/local'


def environment():
    env = os.environ.copy()
    for line in (ROOT / '.env').read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not key.strip().replace('_', '').isalnum():
            raise ValueError('Неверная строка .env. Нужен формат KEY=value.')
        env[key.strip()] = ' '.join(shlex.split(value, comments=True))
    if not env.get('MAX_BOT_TOKEN', '').strip():
        raise ValueError('Заполните MAX_BOT_TOKEN в .env.')
    return env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stop', action='store_true', help='остановить запущенный локальный комплект')
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DATA / 'runner.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.stop:
                lock.seek(0)
                os.kill(int(lock.read().strip()), signal.SIGTERM)
                print('Отправлен сигнал остановки. База и модель останутся на диске.')
                return
            raise SystemExit('Локальный комплект уже запущен. Остановка: python -m scripts.run_local --stop')
        if args.stop:
            print('Локальный комплект не запущен.')
            return
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        run(environment())


def run(env):
    os.chdir(ROOT)
    # Клиент MAX использует системное окружение для доверенного CA, без вывода токена.
    for key in ('SSL_CERT_FILE', 'MAX_CA_CERT_FILE'):
        if env.get(key):
            os.environ[key] = env[key]
    api = MaxAPI(env['MAX_BOT_TOKEN'].strip())
    identity = api.request('GET', '/me')
    if api.request('GET', '/subscriptions').get('subscriptions'):
        raise ValueError('У бота есть webhook. Локальный polling одновременно с ним не запускается.')
    print('MAX подключён:', identity.get('username') or identity.get('name') or identity.get('user_id'), flush=True)
    db_options = conninfo_to_dict(env.get('DATABASE_URL', ''))
    if db_options.get('host') != '127.0.0.1' or db_options.get('port') != '55432':
        raise ValueError('Этот launcher использует локальную БД 127.0.0.1:55432. Для других настроек запускайте роли отдельно.')
    if env.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/') != 'http://127.0.0.1:11434':
        raise ValueError('Этот launcher использует локальную Ollama на 127.0.0.1:11434.')
    pg_ctl = shutil.which('pg_ctl')
    if not pg_ctl or not shutil.which('ollama'):
        raise ValueError('Для локального запуска нужны PostgreSQL 17 и Ollama в PATH.')
    pg_bin = Path(pg_ctl).resolve().parent
    cluster = DATA / 'postgres'
    processes, logs = [], []
    own_database = False
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    def launch(name, command, process_env):
        log = (DATA / f'{name}.log').open('a')
        logs.append(log)
        process = subprocess.Popen(command, env=process_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append((name, process))
        return process
    try:
        try:
            with psycopg.connect(env['DATABASE_URL'], connect_timeout=3):
                pass
            print('Используется работающая локальная PostgreSQL.', flush=True)
        except psycopg.OperationalError:
            if not (cluster / 'PG_VERSION').exists():
                with tempfile.NamedTemporaryFile(mode='w') as password:
                    password.write(db_options.get('password', ''))
                    password.flush()
                    result = subprocess.run([str(pg_bin / 'initdb'), '-D', str(cluster),
                        '-U', db_options.get('user', 'max_bot'), '--pwfile', password.name,
                        '--auth-host=scram-sha-256', '--auth-local=scram-sha-256',
                        '--encoding=UTF8', '--no-locale'], capture_output=True)
                    if result.returncode:
                        raise ValueError('Не удалось создать локальный кластер PostgreSQL.')
            socket_dir = DATA / 'socket'
            socket_dir.mkdir(exist_ok=True, mode=0o700)
            result = subprocess.run([pg_ctl, '-D', str(cluster), '-l', str(DATA / 'postgres.log'),
                '-o', f'-h 127.0.0.1 -p 55432 -k {socket_dir}', '-w', 'start'], capture_output=True)
            if result.returncode:
                raise ValueError('Не удалось запустить PostgreSQL. См. data/local/postgres.log.')
            own_database = True
            with psycopg.connect(make_conninfo(env['DATABASE_URL'], dbname='postgres'), autocommit=True) as admin:
                names = {db_options.get('dbname', 'max_bot')}
                for key in ('DEMO_DATABASE_URL', 'TEST_DATABASE_URL'):
                    if env.get(key):
                        names.add(conninfo_to_dict(env[key])['dbname'])
                for name in names:
                    if not admin.execute('SELECT 1 FROM pg_database WHERE datname = %s', (name,)).fetchone():
                        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            print('Локальная PostgreSQL готова; данные в data/local/postgres.', flush=True)
        try:
            with urlopen('http://127.0.0.1:11434/api/tags', timeout=2) as response:
                models = json.load(response)
        except OSError:
            # Не передаём токен MAX процессу сервера модели.
            ollama_env = {key: value for key, value in os.environ.items() if not key.startswith(('MAX_', 'ADMIN_', 'PG', 'DATABASE_', 'DEMO_DATABASE_', 'TEST_DATABASE_'))}
            process = launch('ollama', ['sh', 'scripts/serve_ai.sh'], ollama_env)
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise ValueError('Ollama не запустилась. См. data/local/ollama.log.')
                try:
                    with urlopen('http://127.0.0.1:11434/api/tags', timeout=2) as response:
                        models = json.load(response)
                    break
                except OSError:
                    time.sleep(0.5)
        expected = env.get('OLLAMA_MODEL', 'qwen3.5:4b-q4_K_M')
        if expected not in {model['name'] for model in models.get('models', [])}:
            raise ValueError('Модель не найдена. Запустите serve_ai.sh и скачайте OLLAMA_MODEL командой ollama pull.')
        print('Локальная модель доступна:', expected, flush=True)
        for name in ('worker', 'polling'):
            launch(name, [sys.executable, '-m', 'ip_bot', '--mode', name], env)
        print('Worker и polling запущены. Логи: data/local/. Ctrl+C — остановка.', flush=True)
        while True:
            for name, process in processes:
                if process.poll() is not None:
                    raise ValueError(f'Процесс {name} завершился. См. data/local/{name}.log.')
            time.sleep(1)
    finally:
        for name, process in reversed(processes):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for name, process in reversed(processes):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        if own_database:
            subprocess.run([pg_ctl, '-D', str(cluster), '-m', 'fast', '-w', 'stop'], capture_output=True)
        for log in logs:
            log.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nЛокальный комплект остановлен; данные сохранены.')
    except (APIError, ValueError) as error:
        raise SystemExit(str(error))
    except psycopg.Error:
        raise SystemExit('Ошибка подключения к локальной PostgreSQL. Проверьте настройки .env.')

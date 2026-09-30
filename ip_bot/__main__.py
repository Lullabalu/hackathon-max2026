"""Роли процесса: worker, polling, webhook, subscribe и локальное demo."""

import argparse
import logging
import signal
import time
from urllib.parse import urlsplit
import uuid

import psycopg

from .assistant import Assistant, timeout_reply
from .budget import (BudgetExpired, PREPARATION_SECONDS, DELIVERY_SECONDS,
                     FAILURE_RECORD_SECONDS, PERSISTENCE_RESERVE_SECONDS, check_budget, time_limit)
from .llm import ModelError
from .max_api import APIError, MaxAPI, normalize_update
from .settings import Settings
from .storage import Store, StoreInUseError
from .services import Services
from .privacy import has_consent

log = logging.getLogger(__name__)
_RETRYABLE_SQL = {'57014', '55P03', '40P01', '40001'}


def _record_failure(store, event, code, *, permanent=False):
    try:
        with time_limit(FAILURE_RECORD_SECONDS):
            store.fail(event, code, permanent=permanent)
        return True
    except (BudgetExpired, psycopg.Error):
        # A failed connection/row lock must not turn a deadline into a second
        # unbounded wait. The event remains durable for the next worker pass.
        log.warning('event_failure_record_deferred seq=%s code=%s', event['seq'], code)
        return False


def drain(store, send, assistant=None, *, retry_errors=False, send_files=None, max_events=None):
    processed = 0
    while max_events is None or processed < max_events:
        started = time.monotonic()
        try:
            with time_limit(2.5):
                event = store.pending()
        except BudgetExpired:
            log.warning('queue_read_budget_exhausted')
            return processed
        except psycopg.Error as error:
            if not retry_errors or error.sqlstate not in _RETRYABLE_SQL:
                raise
            log.warning('queue_read_retry code=%s', error.sqlstate)
            return processed
        if event is None:
            break
        processed += 1
        preparation_deadline = started + PREPARATION_SECONDS if PREPARATION_SECONDS is not None else None
        try:
            responder = None
            if assistant and event['reply'] is None:
                assistant.user_id = event['user_id']
                def responder(state, text):
                    # Команды удаления/сброса и анкета меняют состояние. Их
                    # нельзя заменять таймаутом из-за ожидания предыдущего ИИ-запроса.
                    research = (Assistant.needs_research(state, text)
                                and (not isinstance(assistant, Assistant) or has_consent(state)))
                    end = preparation_deadline
                    try:
                        with time_limit(end - time.monotonic() - PERSISTENCE_RESERVE_SECONDS if end is not None else None):
                            # Waiting in the queue never discards a user question.
                            history = store.history(event['user_id']) if research else ()
                            return assistant.respond(state, text, history)
                    except BudgetExpired:
                        log.warning('answer_budget_exhausted stage=queue_or_processing')
                        return timeout_reply(state)
            with time_limit(preparation_deadline - time.monotonic() if preparation_deadline is not None else None):
                reply = store.prepare_reply(event, responder)
            with time_limit(DELIVERY_SECONDS):
                files = store.prepared_files(event['seq'])
                if files:
                    if send_files is None:
                        raise ValueError('Для PDF нужен обработчик файлов.')
                    send_files(event['user_id'], reply, files)
                else:
                    send(event['user_id'], reply)
                store.delivered(event['seq'])
            log.info('reply_delivered seq=%s seconds=%.3f', event['seq'], time.monotonic() - started)
        except BudgetExpired:
            if not retry_errors:
                raise APIError('Превышен бюджет обработки или доставки ответа.', retryable=True) from None
            if not _record_failure(store, event, 'event_budget_timeout'):
                break
            log.warning('event_budget_exhausted seq=%s', event['seq'])
        except psycopg.Error as error:
            interrupted = getattr(store.db, 'budget_interrupted', False) is True
            if not retry_errors or error.sqlstate not in _RETRYABLE_SQL and not interrupted:
                raise
            if not _record_failure(store, event, 'storage_budget_timeout' if interrupted else 'storage_retry'):
                break
        except Exception as error:
            if not retry_errors or isinstance(error, APIError) and error.status == 401:
                raise
            permanent = isinstance(error, ValueError) or isinstance(error, APIError) and not error.retryable
            code = f'max_http_{error.status}' if isinstance(error, APIError) else type(error).__name__
            if not _record_failure(store, event, code, permanent=permanent):
                break
            log.warning('event_failed seq=%s code=%s attempts=%s', event['seq'], code, event['attempts'] + 1)
    return processed


def drain_notifications(store, send, *, max_items=1, budget_seconds=1):
    services = Services(store)
    attempted, item = 0, None
    try:
        with time_limit(budget_seconds):
            if store.pending() is not None:
                return 0
            for item in services.due(limit=max_items):
                if store.pending() is not None:
                    break
                attempted += 1
                try:
                    send(item['user_id'], item['text'])
                    services.mark(item['id'])
                except APIError as error:
                    check_budget()
                    if error.status == 401:
                        raise
                    services.fail(item['id'], f'max_http_{error.status}', permanent=not error.retryable)
                except ValueError:
                    services.fail(item['id'], 'invalid_notification', permanent=True)
                    log.warning('notification_failed code=invalid_notification')
    except BudgetExpired:
        log.warning('notification_budget_exhausted')
        if item is not None:
            try:
                # Only a short bookkeeping allowance after network cancellation.
                with time_limit(0.2):
                    services.fail(item['id'], 'notification_timeout')
            except (BudgetExpired, psycopg.Error):
                pass
    except psycopg.Error as error:
        if error.sqlstate not in _RETRYABLE_SQL and getattr(store.db, 'budget_interrupted', False) is not True:
            raise
        log.warning('notification_storage_retry')
    return attempted


def demo(store, assistant=None):
    print('Локальное демо. /quit — выход. Обычный текст — вопрос боту, /begin — анкета.')
    def send(user_id, text):
        print(f'\nБот: {text}\n', flush=True)
    def send_files(user_id, text, files):
        import base64
        from pathlib import Path
        folder = Path('data/demo-documents')
        folder.mkdir(parents=True, exist_ok=True)
        send(user_id, text)
        for file in files:
            path = folder / Path(file['name']).name
            path.write_bytes(base64.b64decode(file['content'], validate=True))
            path.chmod(0o600)
            print('PDF:', path)
    drain(store, send, assistant, send_files=send_files)
    text = '/start'
    while True:
        store.receive([{'event_id': str(uuid.uuid4()), 'user_id': 1, 'text': text}])
        drain(store, send, assistant, send_files=send_files)
        text = input('Вы: ')
        if text.strip().lower() == '/quit':
            return


def connected_api(settings, store):
    if not settings.token:
        raise ValueError('Задайте MAX_BOT_TOKEN в переменных окружения.')
    api = MaxAPI(settings.token)
    try:
        with time_limit(DELIVERY_SECONDS):
            identity = api.request('GET', '/me').get('user_id')
    except BudgetExpired:
        raise APIError('MAX: превышено время проверки подключения.', retryable=True) from None
    if type(identity) is not int:
        raise APIError('MAX не вернул user_id бота.')
    store.bind_bot(identity)
    return api


def polling(store, api):
    if api.request('GET', '/subscriptions').get('subscriptions'):
        raise APIError('У бота настроен webhook. Polling одновременно с ним не работает.')
    # Второй poller мог бы сдвигать marker; блокировка отдельная от worker.
    store.acquire_polling_lock()
    retry_delay = 1
    batch = None
    log.info('polling_started')
    while True:
        try:
            if store.reconnect_if_needed():
                store.acquire_polling_lock()
                log.info('polling_storage_reconnected')
            marker = store.get_meta('marker')
            if batch is not None and batch[0] != marker:
                # A previous commit may have succeeded before disconnection,
                # or another lock owner advanced the cursor. Never rewind it.
                batch = None
            if batch is None:
                result = api.updates(marker)
                if not isinstance(result.get('updates'), list):
                    raise APIError('MAX вернул некорректный список событий.', retryable=True)
                events = []
                for update in result['updates']:
                    if isinstance(update, dict):
                        event = normalize_update(update)
                        if event:
                            events.append(event)
                batch = (marker, events, result.get('marker'))
            store.receive(batch[1], batch[2])
            batch = None
            retry_delay = 1
        except (psycopg.Error, StoreInUseError) as error:
            if (isinstance(error, psycopg.Error) and error.sqlstate not in _RETRYABLE_SQL
                    and not isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))):
                raise
            # Even a healthy connection must be closed if lock reacquisition
            # failed: the next pass cannot poll with somebody else's cursor.
            store.close()
            log.warning('polling_storage_retry delay=%d', retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(30, retry_delay * 2)
        except APIError as error:
            if not error.retryable:
                raise
            log.warning('polling_retry delay=%d', retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(30, retry_delay * 2)


def subscribe(settings, api):
    import re
    url = urlsplit(settings.webhook_url)
    if url.scheme != 'https' or not url.hostname or url.port is not None or url.path != '/webhook' or url.query or url.fragment or url.username:
        raise ValueError('MAX_WEBHOOK_URL должен иметь вид https://bot.example.com/webhook без явного порта.')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', settings.webhook_secret):
        raise ValueError('Задайте MAX_WEBHOOK_SECRET: 32–256 символов A-Z, a-z, 0-9, _ или -.')
    subscriptions = api.request('GET', '/subscriptions').get('subscriptions', [])
    if any(item.get('url') != settings.webhook_url for item in subscriptions):
        raise ValueError('Есть подписка на другой адрес. Сначала проверьте и удалите её вручную в MAX.')
    result = api.request('POST', '/subscriptions', body={
        'url': settings.webhook_url, 'update_types': ['bot_started', 'message_created'],
        'secret': settings.webhook_secret,
    })
    if not result.get('success'):
        raise APIError('MAX не подтвердил регистрацию webhook.')
    print('Webhook зарегистрирован.')


def stop_process(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description='MAX: локальная Qwen, официальные документы и PostgreSQL')
    parser.add_argument('--mode', choices=['worker', 'polling', 'webhook', 'subscribe', 'demo', 'admin'], default='worker')
    parser.add_argument('--demo', action='store_true', help='то же, что --mode demo')
    parser.add_argument('--db', help='строка подключения PostgreSQL; предпочтительнее переменные окружения')
    args = parser.parse_args()
    mode = 'demo' if args.demo else args.mode
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        settings = Settings.from_env(demo=mode == 'demo', database_url=args.db)
    except ValueError:
        parser.error('Некорректные параметры окружения. См. .env.example.')
    if not settings.database_url:
        variable = 'DEMO_DATABASE_URL' if mode == 'demo' else 'DATABASE_URL (или PGHOST/PGDATABASE/PGUSER/PGPASSWORD)'
        parser.error(f'Задайте {variable}. См. .env.example.')
    store = None
    try:
        if mode == 'admin':
            import uvicorn
            from .admin import create_admin_app
            uvicorn.run(create_admin_app(settings), host='0.0.0.0', port=8081, access_log=False, log_level='warning')
            return
        if mode == 'webhook':
            import uvicorn
            from .webhook import create_app
            uvicorn.run(create_app(settings), host='0.0.0.0', port=8080, access_log=False, limit_concurrency=32, log_level='warning')
            return
        signal.signal(signal.SIGTERM, stop_process)
        store = Store(settings.database_url, worker=mode in {'worker', 'demo'})
        if mode == 'demo':
            demo(store, Assistant(store, settings))
            return
        api = connected_api(settings, store)
        if mode == 'polling':
            polling(store, api)
        elif mode == 'subscribe':
            subscribe(settings, api)
        else:
            assistant = Assistant(store, settings)
            log.info('model_warmup_started')
            try:
                assistant.model.warmup()
                log.info('model_warmup_finished')
            except ModelError as error:
                log.warning('model_warmup_failed code=%s', error)
            log.info('worker_started')
            while True:
                try:
                    if store.reconnect_if_needed():
                        log.info('worker_storage_reconnected')
                    drain(store, api.send_message, assistant, retry_errors=True, send_files=api.send_files, max_events=5)
                    if not store.db.closed:
                        drain_notifications(store, api.send_message)
                except (psycopg.OperationalError, psycopg.InterfaceError, StoreInUseError):
                    # No generation on an unowned/broken connection. Reconnect
                    # reacquires the worker lock before processing anything.
                    store.close()
                    log.warning('worker_storage_reconnect_pending')
                time.sleep(0.2)
    except (KeyboardInterrupt, EOFError):
        print('\nПроцесс остановлен. Очередь и ответы сохранены.')
    except (APIError, StoreInUseError, ValueError) as error:
        parser.exit(1, f'Ошибка: {error}\n')
    except psycopg.Error:
        parser.exit(1, 'Ошибка PostgreSQL. Проверьте доступность сервера, строку подключения и права на базу.\n')
    finally:
        if store is not None:
            store.close()


if __name__ == '__main__':
    main()

"""Синтетические сценарии ТЗ и замер времени на PostgreSQL; без сети и MAX."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ip_bot.__main__ import drain
from ip_bot.assistant import Assistant
from ip_bot.services import Services
from ip_bot.settings import Settings
from ip_bot.storage import Store


class OfflineOnly:
    def generate(self, *args, **kwargs):
        raise AssertionError('Типовому сценарию не нужна нейросеть')
    def search(self, *args, **kwargs):
        raise AssertionError('Типовому сценарию не нужна сеть')


def main():
    settings = Settings.from_env(demo=True)
    if not settings.database_url:
        raise SystemExit('Задайте DEMO_DATABASE_URL для временной тестовой схемы.')
    schema = 'spec_' + uuid.uuid4().hex
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'scope': 'PostgreSQL и сценарии бота, без приёма/доставки MAX', 'cases': [], 'success': False}
    with psycopg.connect(settings.database_url, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        store = None
        try:
            store = Store(make_conninfo(settings.database_url, options=f'-c search_path={schema}'), worker=False)
            assistant = Assistant(store, settings, model=OfflineOnly(), researcher=OfflineOnly())
            files, messages = [], []
            def send_files(uid, text, attachments):
                messages.append(text)
                files.extend(attachments)
            commands = ['/start', '/intro', '/consent', '/begin', 'Разработка сайтов', 'Москва', '1200000', '200000', '0', '1',
                        '/okved', '/tax_setup', 'да', '0', '1', '10000', '6', '15', 'да', 'да', 'да', '/taxes', '/tax usn_income',
                        '/regime npd', '/register', '1', '28', '2', '/checklist', '/pdf', '/form',
                        'ИВАНОВ', 'ИВАН', 'ИВАНОВИЧ', 'нет', 'demo@example.test', '/steps', '/next', '/next', '/next', '/next', '/next',
                        '/online', '/registered ' + (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat(), '/reminders on',
                        '/faq npd', '/subscribe', '/feedback 5', 'Возьму сотрудника', '/regime usn_income', '/reminders', '/lessons', '/lesson 2']
            for index, command in enumerate(commands):
                started = time.perf_counter()
                store.receive([{'event_id': f'case-{index}', 'user_id': 1, 'text': command}])
                drain(store, lambda uid, text: messages.append(text), assistant, send_files=send_files)
                elapsed = round(time.perf_counter() - started, 4)
                assert store.pending() is None
                assert messages[-1] and len(messages[-1]) <= 3900
                assert elapsed < 25
                report['cases'].append({'command': command, 'seconds': elapsed, 'reply': messages[-1]})
            assert len(files) == 2
            # Реальный маршрут с обычными ответами и кнопками, отдельный пользователь.
            guided = ['/consent', '3', 'Продаю торты', 'Пеку сам на заказ', 'Да, подходит',
                      'Пока не знаю, помогите подобрать', 'Москва', '1,2 млн', '200 тыс',
                      'без сотрудников', 'Физлица', 'показать расчёт', 'УСН Доходы',
                      'без гражданства', '17', 'МФЦ', 'Скачать чек-лист PDF',
                      'Пройти регистрацию по шагам', 'Кто ты?', 'Нужна ли касса?',
                      'готово', 'готово', 'готово', 'готово', 'готово', 'готово',
                      'Подал, жду результат', 'Получил регистрацию ИП',
                      (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
                      'Нет, без напоминаний', 'Нет, без новостей', 'пропустить']
            for index, command in enumerate(guided):
                started = time.perf_counter()
                store.receive([{'event_id': f'guided-{index}', 'user_id': 2, 'text': command}])
                drain(store, lambda uid, text: messages.append(text), assistant, send_files=send_files)
                elapsed = round(time.perf_counter() - started, 4)
                assert store.pending() is None and messages[-1] and len(messages[-1]) <= 3900
                assert elapsed < 24
                report['cases'].append({'route': 'guided', 'command': command, 'seconds': elapsed,
                                        'stage': store.load_profile(2).get('guide', {}).get('stage'), 'reply': messages[-1]})
            guided_state = store.load_profile(2)
            assert not guided_state['guide']['active']
            assert guided_state['selected_regime'] == 'usn_income'
            assert guided_state['registration']['citizenship'] == 'stateless'
            assert guided_state['registration']['age'] == 17
            assert guided_state['registration']['method'] == 'mfc'
            assert not guided_state.get('reminders_enabled')
            assert len(files) == 3
            service = Services(store)
            assert not service.subscriptions(2) and not service.list_reminders(2)
            report['guided_opt_outs_checked'] = True
            draft = service.create_draft('faq', [{'id': 'test', 'question': 'Проверочный вопрос', 'answer': 'Первый проверочный ответ', 'sources': ['https://npd.nalog.ru/']}])
            service.publish(draft['id'])
            assert 'Первый' in assistant.respond(store.load_profile(1), '/faq test').text
            second = service.create_draft('faq', [{'id': 'test', 'question': 'Проверочный вопрос', 'answer': 'Второй ответ', 'sources': ['https://npd.nalog.ru/']}])
            service.publish(second['id'])
            service.rollback('faq', draft['id'])
            assert service.published('faq')['id'] == draft['id']
            report['admin_publish_and_rollback'] = True
            report['metrics'] = service.metrics()
            store.receive([{'event_id': 'delete', 'user_id': 1, 'text': '/delete'}])
            drain(store, lambda *_: None, assistant)
            assert store.load_profile(1) is None and not service.subscriptions(1)
            assert not service.list_reminders(1)
            report['deletion_checked'] = True
            report['pdf_files'] = len(files)
            report['max_seconds'] = max(item['seconds'] for item in report['cases'])
            report['success'] = True
        finally:
            if store: store.close()
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
            path = Path('data/spec-check.json')
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Сценарии: {len(report['cases'])}; PDF: {report['pdf_files']}; максимум: {report['max_seconds']} с. Отчёт data/spec-check.json")


if __name__ == '__main__':
    main()

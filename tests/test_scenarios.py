"""Сценарии из ТЗ на настоящем PostgreSQL, без сообщений реальным пользователям."""
import base64
from datetime import datetime, timedelta, timezone
import os
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ip_bot.__main__ import drain, drain_notifications
from ip_bot.assistant import Assistant
from ip_bot.max_api import APIError
from ip_bot.services import Services
from ip_bot.settings import Settings
from ip_bot.storage import Store


class NoNetwork:
    def generate(self, *args, **kwargs):
        raise AssertionError('Модель не нужна для сценария')
    def search(self, *args, **kwargs):
        raise AssertionError('Сеть не нужна для сценария')


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Нужна изолированная TEST_DATABASE_URL')
class ScenariosTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'scenario_' + uuid.uuid4().hex
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.store = Store(make_conninfo(os.environ['TEST_DATABASE_URL'], options=f'-c search_path={self.schema}'))
        self.addCleanup(self.cleanup)
        self.assistant = Assistant(self.store, Settings(), model=NoNetwork(), researcher=NoNetwork())
        self.services = Services(self.store)
        self.sent = []
        self.files = []

    def cleanup(self):
        self.store.close()
        self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.admin.close()

    def receive(self, text, user_id=1):
        self.store.receive([{'event_id': uuid.uuid4().hex, 'user_id': user_id, 'text': text}])

    def send(self, text, user_id=1):
        self.receive(text, user_id)
        drain(self.store, lambda uid, answer: self.sent.append((uid, answer)), self.assistant,
              send_files=lambda uid, answer, files: (self.sent.append((uid, answer)), self.files.extend(files)))
        return self.sent[-1][1]

    def business(self):
        for text in ('/consent', '/begin', 'Разработка сайтов', 'Москва', '1200000', '200000', '0', '1'):
            self.send(text)

    def test_before_consent_raw_input_never_enters_queue_and_same_batch_consent_works(self):
        self.receive('Иван Иванов паспорт 12 34 567890')
        self.assertEqual(self.store.pending()['text'], '/consent_required')
        drain(self.store, lambda *_: None, self.assistant)
        self.assertIsNone(self.store.load_profile(1))
        self.store.receive([{'event_id': str(i), 'user_id': 1, 'text': text}
                            for i, text in enumerate(('/consent', '/begin', 'Разработка сайтов'))])
        drain(self.store, lambda *_: None, self.assistant)
        self.assertEqual(self.store.load_profile(1)['answers']['activity'], 'Разработка сайтов')
        self.receive('/decline')
        self.receive('Новый личный текст')
        last = self.store.db.execute('SELECT text FROM events ORDER BY seq DESC LIMIT 1').fetchone()
        self.assertEqual(last['text'], '/consent_required')

    def test_business_registration_pdf_and_changed_conditions(self):
        self.business()
        self.assertIn('tax_comparison', self.store.load_profile(1))
        self.send('/okved')
        self.send('/regime npd')
        for text in ('/register', '1', '28', '2'):
            answer = self.send(text)
        self.assertIn('не взимается', answer)
        self.send('/pdf')
        self.assertTrue(base64.b64decode(self.files[0]['content']).startswith(b'%PDF-'))
        self.assertEqual(self.store.db.execute("SELECT count(*) AS n FROM events WHERE reply_files <> '[]'::jsonb").fetchone()['n'], 0)
        answer = self.send('Возьму сотрудника')
        self.assertIn('исключён', answer)
        self.assertNotIn('selected_regime', self.store.load_profile(1))

    def test_pdf_retry_reuses_prepared_content(self):
        self.send('/consent')
        self.receive('/pdf')
        sent = []
        def offline(uid, answer, files):
            sent.append(files)
            raise APIError('temporary', retryable=True, status=503)
        drain(self.store, lambda *_: None, self.assistant, retry_errors=True, send_files=offline)
        event = self.store.db.execute('SELECT * FROM events WHERE NOT delivered').fetchone()
        self.assertEqual(event['attempts'], 1)
        self.assertTrue(event['reply_files'])
        self.store.db.execute('UPDATE events SET next_attempt_at = now() WHERE seq = %s', (event['seq'],))
        self.assistant.respond = lambda *args: self.fail('Нельзя повторно готовить PDF')
        drain(self.store, lambda *_: None, self.assistant,
              send_files=lambda uid, answer, files: sent.append(files))
        self.assertEqual(sent[0], sent[1])
        self.assertIsNone(self.store.pending())

    def test_manual_reminder_news_delivery_and_complete_deletion(self):
        self.business()
        self.send('/timezone Asia/Yekaterinburg')
        when = datetime.now(timezone.utc) + timedelta(days=2)
        self.send('/remind ' + when.strftime('%Y-%m-%d %H:%M') + ' Проверить документы')
        reminders = self.services.list_reminders(1)
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]['due_at'].astimezone(timezone.utc).hour, (when.hour - 5) % 24)
        self.send('/subscribe npd')
        news = self.services.create_draft('news', {'title': 'Проверка', 'text': 'Синтетический тест', 'url': 'https://npd.nalog.ru/', 'segment': 'npd'})
        self.services.publish(news['id'])
        self.services.enqueue_news(news['id'])
        self.store.db.execute("UPDATE service_outbox SET due_at=now(), next_attempt_at=now()")
        notifications = []
        self.assertEqual(drain_notifications(self.store, lambda uid, text: notifications.append((uid, text))), 1)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(drain_notifications(self.store, lambda uid, text: notifications.append((uid, text))), 1)
        self.assertEqual(len(notifications), 2)
        self.assertTrue(all(uid == 1 for uid, _ in notifications))
        drain_notifications(self.store, lambda *_: self.fail('Не повторять доставленные'))
        self.send('/feedback 5')
        self.send('/delete пожалуйста')
        self.assertIsNone(self.store.load_profile(1))
        self.assertEqual(self.store.history(1), [])
        for table in ('service_subscriptions', 'service_reminders', 'service_outbox', 'service_feedback'):
            self.assertEqual(self.store.db.execute(sql.SQL('SELECT count(*) AS n FROM {} WHERE user_id=1').format(sql.Identifier(table))).fetchone()['n'], 0)
        self.assertTrue(all(row['text'] == row['reply'] == '' and row['reply_files'] == []
                            for row in self.store.db.execute('SELECT * FROM events WHERE user_id=1')))

    def guided_business(self):
        for text in ('/consent', '3', 'Разработка сайтов', 'Да, подходит', 'НПД',
                     'Москва', '1200000', '200000', '0', 'Физлица', 'да', 'да', '0',
                     'НПД', 'РФ', '28', 'онлайн'):
            self.send(text)
        state = self.store.load_profile(1)
        self.assertEqual(state['guide']['stage'], 'after_checklist')
        self.assertEqual(state['selected_regime'], 'npd')

    def test_social_messages_before_consent_and_in_survey_never_start_research(self):
        for text in ('Привет, как дела?', 'Привет! Ты тут, на связи?', 'Давай поболтаем'):
            answer = self.send(text)
            self.assertNotIn('/consent', answer)
            self.assertIsNone(self.store.load_profile(1))
            self.assertEqual(self.store.history(1), [])
        for text in ('/consent', '3', 'Разработка сайтов', 'Да, подходит', 'НПД'):
            self.send(text)
        saved = self.store.load_profile(1)
        self.assertEqual(saved['guide']['field'], 'region')
        answer = self.send('Привет, бро! Как дела? Что нового?')
        self.assertEqual(self.store.load_profile(1), saved)
        self.assertEqual(self.store.history(1), [])
        self.assertIn('/continue', answer)
        self.assertLess(len(answer), 500)
        self.assertEqual(self.store.db.execute('SELECT count(*) AS n FROM documents').fetchone()['n'], 0)
        self.send('Москва')
        self.assertEqual(self.store.load_profile(1)['answers']['region'], 'Москва')
        self.assertEqual(self.store.load_profile(1)['guide']['field'], 'revenue')

    def test_guided_opt_in_persists_timezone_segment_and_delete_clears_progress(self):
        self.guided_business()
        self.assertEqual(self.services.subscriptions(1), [])
        self.assertEqual(self.services.list_reminders(1), [])
        self.send('Получил регистрацию ИП')
        registered = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        self.send(registered)
        self.assertFalse(self.store.load_profile(1).get('reminders_enabled'))
        self.send('Да, включить напоминания')
        self.send('Asia/Yekaterinburg')
        self.assertTrue(self.store.load_profile(1)['reminders_enabled'])
        self.assertEqual(self.store.load_profile(1)['timezone'], 'Asia/Yekaterinburg')
        self.assertEqual(self.services.subscriptions(1), [])
        self.send('Да, подписаться на новости')
        self.assertEqual(self.services.subscriptions(1), ['npd'])
        self.send('5')
        self.assertFalse(self.store.load_profile(1)['guide']['active'])
        self.assertIsNone(self.store.pending())
        self.send('/delete')
        self.assertIsNone(self.store.load_profile(1))
        self.assertEqual(self.services.subscriptions(1), [])
        self.assertEqual(self.services.list_reminders(1), [])

    def test_guided_step_survives_failed_delivery_and_new_assistant_without_reprocessing(self):
        for text in ('/consent', '3', 'Разработка сайтов', 'Да, подходит', 'НПД'):
            self.send(text)
        self.receive('Москва')
        def offline(*_):
            raise APIError('temporary', retryable=True, status=503)
        drain(self.store, offline, self.assistant, retry_errors=True)
        saved = self.store.load_profile(1)
        self.assertEqual(saved['guide']['field'], 'revenue')
        self.assertEqual(saved['answers']['region'], 'Москва')
        self.store.db.execute('UPDATE events SET next_attempt_at=now() WHERE NOT delivered')
        new_assistant = Assistant(self.store, Settings(), model=NoNetwork(), researcher=NoNetwork())
        new_assistant.respond = lambda *_: self.fail('Prepared guided reply must be reused')
        drain(self.store, lambda *_: None, new_assistant)
        self.assertEqual(self.store.load_profile(1), saved)
        self.assistant = Assistant(self.store, Settings(), model=NoNetwork(), researcher=NoNetwork())
        self.send('/continue')
        self.assertEqual(self.store.load_profile(1)['guide']['field'], 'revenue')
        self.send('800000')
        self.assertEqual(self.store.load_profile(1)['answers']['revenue'], '800000.00')


if __name__ == '__main__':
    unittest.main()

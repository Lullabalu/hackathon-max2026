"""Services and admin integration tests use an isolated PostgreSQL schema."""

from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi.testclient import TestClient
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ip_bot.admin import create_admin_app
from ip_bot.services import Services, validate_content, format_faq, format_news, NEWS_FOOTER
from ip_bot.settings import Settings
from ip_bot.storage import Store


FAQ = [{'id': 'register', 'question': 'Как зарегистрироваться?',
        'answer': 'Используйте сервис ФНС.', 'sources': ['https://www.nalog.gov.ru/']}]
NEWS = {'title': 'Новая памятка', 'text': 'Проверьте условия режима.',
        'url': 'https://www.nalog.gov.ru/new2026/', 'segment': 'tax'}


class ContentValidationTests(unittest.TestCase):
    def test_news_limit_includes_title_url_separators_and_unsubscribe(self):
        payload = {**NEWS, 'title': 'З' * 300, 'url': 'https://www.nalog.gov.ru/' + 'a' * 220, 'text': 'x'}
        overhead = len(format_news(payload)) - 1
        payload['text'] = 'Т' * (3900 - overhead)
        self.assertEqual(len(format_news(validate_content('news', payload))), 3900)
        self.assertTrue(format_news(payload).endswith(NEWS_FOOTER))
        payload['text'] += 'Т'
        with self.assertRaises(ValueError):
            validate_content('news', payload)
        with self.assertRaises(ValueError):
            validate_content('news', {**NEWS, 'title': 'З' * 300, 'text': 'Т' * 3500,
                                      'url': 'https://www.nalog.gov.ru/' + 'a' * 246})

    def test_faq_limit_includes_question_and_all_source_urls(self):
        entry = {**FAQ[0], 'question': 'Вопрос?', 'answer': 'x',
                 'sources': ['https://www.nalog.gov.ru/', 'https://npd.nalog.ru/']}
        overhead = len(format_faq(entry)) - 1
        entry['answer'] = 'О' * (3900 - overhead)
        self.assertEqual(len(format_faq(validate_content('faq', [entry])[0])), 3900)
        entry['answer'] += 'О'
        with self.assertRaises(ValueError):
            validate_content('faq', [entry])
        with self.assertRaises(ValueError):
            validate_content('faq', [{**FAQ[0], 'answer': 'О' * 4100}])

    def test_faq_requires_official_https_sources_and_exact_schema(self):
        for url in ('http://nalog.gov.ru/', 'https://nalog.gov.ru.evil.test/',
                    'https://nalog.gov.ru@evil.test/', 'https://user:pass@nalog.gov.ru/',
                    'https://nalog.gov.ru:8443/', 'https://evil.test/', 'https://nalog.gov.ru\\@evil.test/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_content('faq', [{**FAQ[0], 'sources': [url]}])
        for host in ('nalog.gov.ru', 'npd.nalog.ru', 'ausn.nalog.gov.ru'):
            self.assertEqual(validate_content('faq', [{**FAQ[0], 'sources': ['https://' + host]}])[0]['sources'], ['https://' + host])
        for value in ([], [FAQ[0], FAQ[0]], [{**FAQ[0], 'tax_rate': 0}], [{**FAQ[0], 'sources': []}]):
            with self.assertRaises(ValueError):
                validate_content('faq', value)
        with self.assertRaises(ValueError):
            validate_content('tax_config', {'rate': 0})

    def test_admin_fails_closed_without_valid_secret(self):
        for token in ('', 'short', ' ' * 40, 'x' * 31 + '\n'):
            with patch.dict(os.environ, {'ADMIN_TOKEN': token}), self.assertRaises(ValueError):
                create_admin_app(Settings())


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Нужен TEST_DATABASE_URL с отдельной тестовой базой PostgreSQL')
class ServicesTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'test_services_' + uuid.uuid4().hex
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.store = None
        self.addCleanup(self.cleanup)
        self.dsn = make_conninfo(os.environ['TEST_DATABASE_URL'], options=f'-c search_path={self.schema}')
        self.store = Store(self.dsn, worker=False)
        self.service = Services(self.store)
        self.now = datetime.now(timezone.utc)

    def cleanup(self):
        if self.store is not None:
            self.store.close()
        try:
            self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        finally:
            self.admin.close()

    def publish_news(self, payload=None):
        draft = self.service.create_draft('news', payload or NEWS)
        return self.service.publish(draft['id'])

    def test_reminder_idempotency_due_cancel_and_user_isolation(self):
        due = self.now + timedelta(hours=1)
        row = self.service.create_reminder(1, 'Подать документы', due, 'command-1')
        again = self.service.create_reminder(1, 'Подать документы', due, 'command-1')
        self.assertEqual(row['id'], again['id'])
        with self.assertRaises(ValueError):
            self.service.create_reminder(1, 'Другой текст', due, 'command-1')
        with self.assertRaises(ValueError):
            self.service.create_reminder(1, 'Текст', datetime(2026, 10, 1), 'command-2')
        self.assertEqual(self.service.due(now=self.now), [])
        self.assertEqual(len(self.service.due(now=due)), 1)
        self.assertFalse(self.service.cancel_reminder(2, row['id']))
        self.assertEqual(self.service.list_reminders(2), [])
        self.assertTrue(self.service.cancel_reminder(1, row['id']))
        self.assertFalse(self.service.cancel_reminder(1, row['id']))
        self.assertEqual(self.service.list_reminders(1), [])
        self.assertEqual(self.service.due(now=due), [])

    def test_delivery_ack_and_retry_preserve_one_record(self):
        due = self.now - timedelta(minutes=1)
        reminder = self.service.create_reminder(1, 'Напомнить', due, 'retry')
        item = self.service.due()[0]
        self.assertTrue(self.service.fail(item['id'], 'timeout'))
        self.assertEqual(self.service.due(), [])
        retry = self.service.due(now=self.now + timedelta(minutes=10))[0]
        self.assertEqual(retry['id'], item['id'])
        self.assertEqual(retry['attempts'], 1)
        self.assertTrue(self.service.mark(item['id']))
        self.assertFalse(self.service.mark(item['id']))
        self.assertEqual(self.service.due(now=self.now + timedelta(days=1)), [])
        repeated = self.service.create_reminder(1, 'Напомнить', due, 'retry')
        self.assertEqual(repeated['id'], reminder['id'])
        self.assertEqual(repeated['status'], 'sent')
        self.assertEqual(self.service.list_reminders(1), [])
        self.assertEqual(self.service.metrics()['outbox_delivered'], 1)

    def test_permanent_failure_and_error_redaction(self):
        self.service.create_reminder(1, 'Напомнить', self.now - timedelta(seconds=1), 'failed')
        item = self.service.due()[0]
        self.service.fail(item['id'], 'secret token inside exception', permanent=True)
        stored = self.store.db.execute('SELECT * FROM service_outbox WHERE id = %s', (item['id'],)).fetchone()
        self.assertEqual(stored['last_error'], 'delivery_error')
        self.assertTrue(stored['dead_letter'])
        self.assertEqual(self.service.due(now=self.now + timedelta(days=1)), [])

    def test_news_only_opt_in_matching_topics_and_no_duplicates(self):
        news = self.publish_news()
        self.assertEqual(self.service.subscriptions(1), [])
        self.assertEqual(self.service.enqueue_news(news['id']), 0)
        self.assertTrue(self.service.subscribe(1))
        self.assertFalse(self.service.subscribe(1))
        self.service.subscribe(1, 'tax')
        self.service.subscribe(2, 'tax')
        self.service.subscribe(3, 'registration')
        self.assertEqual(self.service.enqueue_news(news['id']), 2)
        self.assertEqual(self.service.enqueue_news(news['id']), 0)
        self.assertEqual({row['user_id'] for row in self.service.due()}, {1, 2})
        self.service.unsubscribe(1)
        self.assertEqual({row['user_id'] for row in self.service.due()}, {2})
        self.service.unsubscribe(2, 'tax')
        self.assertEqual(self.service.due(), [])

    def test_all_news_include_topic_opt_ins_but_not_unsubscribed_users(self):
        news = self.publish_news({**NEWS, 'segment': 'all'})
        self.service.subscribe(1, 'registration')
        self.assertEqual(self.service.enqueue_news(news['id']), 1)
        self.assertEqual([row['user_id'] for row in self.service.due()], [1])

    def test_enqueued_news_use_the_validated_format_with_unsubscribe(self):
        payload = {**NEWS, 'title': 'З' * 300, 'url': 'https://www.nalog.gov.ru/' + 'a' * 220, 'text': 'x'}
        payload['text'] = 'Т' * (3900 - (len(format_news(payload)) - 1))
        news = self.publish_news(payload)
        self.service.subscribe(1)
        self.assertEqual(self.service.enqueue_news(news['id']), 1)
        message = self.service.due()[0]['text']
        self.assertEqual(message, format_news(payload))
        self.assertEqual(len(message), 3900)
        self.assertTrue(message.endswith(NEWS_FOOTER))

    def test_publish_rollback_and_drafts_do_not_change_tax_config(self):
        self.store.set_meta('tax_config', {'usn': '6'})
        one = self.service.create_draft('faq', FAQ)
        self.assertIsNone(self.service.published('faq'))
        self.service.publish(one['id'])
        two = self.service.create_draft('faq', [{**FAQ[0], 'answer': 'Уточнённая памятка.'}])
        self.assertEqual(self.service.published('faq')['id'], one['id'])
        with self.assertRaises(ValueError):
            self.service.rollback('faq', two['id'])
        self.service.publish(two['id'])
        self.assertEqual(self.service.revision(one['id'])['status'], 'superseded')
        self.service.rollback('faq', one['id'])
        self.assertEqual(self.service.published('faq')['payload'], FAQ)
        self.assertEqual(self.store.get_meta('tax_config'), {'usn': '6'})
        self.assertEqual([row['action'] for row in reversed(self.service.audit())],
                         ['draft', 'publish', 'draft', 'publish', 'rollback'])
        with self.assertRaises(ValueError):
            self.service.enqueue_news(one['id'])

    def test_failed_publish_rollback_and_enqueue_are_atomic(self):
        one = self.service.create_draft('faq', FAQ)
        self.service.publish(one['id'])
        two = self.service.create_draft('faq', [{**FAQ[0], 'answer': 'Обновлено.'}])
        with patch.object(self.service, '_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.service.publish(two['id'])
        self.assertEqual(self.service.published('faq')['id'], one['id'])
        self.assertEqual(self.service.revision(two['id'])['status'], 'draft')
        self.service.publish(two['id'])
        with patch.object(self.service, '_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.service.rollback('faq', one['id'])
        self.assertEqual(self.service.published('faq')['id'], two['id'])
        news = self.publish_news()
        self.service.subscribe(1)
        with patch.object(self.service, '_audit', side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.service.enqueue_news(news['id'])
        self.assertEqual(self.service.due(), [])

    def test_cleanup_and_metrics_do_not_expose_user_data(self):
        for user_id in (87654321, 2):
            self.service.subscribe(user_id)
            self.service.create_reminder(user_id, 'Личный текст', self.now, 'personal')
            self.service.feedback(user_id, 4)
        for score in (True, 0, 6, '5'):
            with self.assertRaises(ValueError):
                self.service.feedback(87654321, score)
        metrics = self.service.metrics()
        encoded = json.dumps(metrics)
        self.assertNotIn('87654321', encoded)
        self.assertNotIn('Личный текст', encoded)
        self.assertEqual(metrics['subscribers'], 2)
        self.assertEqual(metrics['feedback_scores'], {'4': 2})
        self.service.cleanup_user(87654321)
        self.assertEqual(self.service.subscriptions(87654321), [])
        self.assertEqual(self.service.list_reminders(87654321), [])
        self.assertEqual(self.service.metrics()['feedback_count'], 1)
        self.assertEqual([row['user_id'] for row in self.service.due()], [2])

    def test_admin_auth_preview_publish_rollback_and_metrics(self):
        token = 'test-admin-token-' + 'x' * 32
        factory = Mock(side_effect=lambda dsn, **kwargs: Store(dsn, **kwargs))
        with patch.dict(os.environ, {'ADMIN_TOKEN': token}):
            with TestClient(create_admin_app(Settings(database_url=self.dsn), factory)) as client:
                factory.reset_mock()
                self.assertEqual(client.get('/api/metrics').status_code, 401)
                self.assertEqual(client.post('/api/drafts', json={'kind': 'faq', 'payload': FAQ}).status_code, 401)
                factory.assert_not_called()
                page = client.get('/')
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(token, page.text)
                self.assertIn('Content-Security-Policy', page.headers)
                headers = {'Authorization': 'Bearer ' + token}
                draft = client.post('/api/drafts', headers=headers, json={'kind': 'faq', 'payload': FAQ})
                self.assertEqual(draft.status_code, 201, draft.text)
                ident = draft.json()['id']
                preview = client.get(f'/api/revisions/{ident}', headers=headers)
                self.assertEqual(preview.json()['payload'], FAQ)
                self.assertEqual(client.post(f'/api/revisions/{ident}/publish', headers=headers).status_code, 200)
                self.assertEqual(client.post('/api/rollback', headers=headers,
                    json={'kind': 'faq', 'revision_id': ident}).status_code, 200)
                invalid = client.post('/api/drafts', headers=headers,
                    json={'kind': 'faq', 'payload': [{**FAQ[0], 'sources': ['http://example.org']}]})
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(client.post('/api/drafts', headers=headers, content='x' * 1_000_001).status_code, 413)
                self.assertEqual(client.get('/api/metrics', headers=headers).json()['drafts'], 0)
                self.assertEqual(client.get('/api/audit', headers=headers).status_code, 200)
                self.assertFalse(factory.call_args.kwargs['worker'])


if __name__ == '__main__':
    unittest.main()

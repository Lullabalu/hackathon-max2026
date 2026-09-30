import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
import psycopg

from ip_bot.settings import Settings
from ip_bot.webhook import create_app


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.factory = Mock(return_value=self.store)
        self.settings = Settings(database_url='test', webhook_secret='s' * 48)
        self.client = self.enterContext(TestClient(create_app(self.settings, self.factory)))
        self.headers = {'X-Max-Bot-Api-Secret': self.settings.webhook_secret}
        self.update = {'update_type': 'message_created', 'message': {
            'sender': {'user_id': 10}, 'recipient': {'chat_type': 'dialog'},
            'body': {'mid': 'event-1', 'text': 'Документы для ИП'}}}

    def test_secret_required_and_event_persisted_before_success(self):
        self.assertEqual(self.client.post('/webhook', json=self.update).status_code, 401)
        self.store.receive.assert_not_called()
        response = self.client.post('/webhook', headers=self.headers, json=self.update)
        self.assertEqual(response.status_code, 200)
        self.store.receive.assert_called_once_with([{'event_id': 'message:event-1', 'user_id': 10, 'text': 'Документы для ИП'}])
        self.assertFalse(self.factory.call_args.kwargs['worker'])

    def test_storage_failure_requests_redelivery(self):
        self.store.receive.side_effect = psycopg.OperationalError('test-only failure')
        response = self.client.post('/webhook', headers=self.headers, json=self.update)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('test-only failure', response.text)

    def test_invalid_payload_and_size_limit(self):
        for body in [b'not json', b'[]', b'\xff']:
            self.assertEqual(self.client.post('/webhook', headers=self.headers, content=body).status_code, 400)
        self.assertEqual(self.client.post('/webhook', headers=self.headers, content='x' * 1_000_001).status_code, 413)
        self.store.receive.assert_not_called()

    def test_group_and_bot_updates_ignored(self):
        self.update['message']['recipient']['chat_type'] = 'chat'
        self.assertEqual(self.client.post('/webhook', headers=self.headers, json=self.update).status_code, 200)
        self.store.receive.assert_not_called()

    def test_ready_checks_database(self):
        self.assertEqual(self.client.get('/readyz').status_code, 200)
        self.factory.side_effect = psycopg.OperationalError()
        self.assertEqual(self.client.get('/readyz').status_code, 503)

import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

import psycopg

from ip_bot.__main__ import main, subscribe
from ip_bot.settings import Settings


class CLITests(unittest.TestCase):
    def test_demo_does_not_fall_back_to_live_database(self):
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"DATABASE_URL": "postgresql://live-db/bot"}, clear=True),
            patch("sys.argv", ["ip_bot", "--demo"]),
            patch("ip_bot.__main__.Store") as store,
            contextlib.redirect_stderr(output),
            self.assertRaises(SystemExit) as stopped,
        ):
            main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("DEMO_DATABASE_URL", output.getvalue())
        store.assert_not_called()

    def test_database_error_does_not_print_connection_secrets(self):
        output = io.StringIO()
        dsn = "postgresql://user:fake-test-password@localhost/bot"
        with (
            patch.dict(os.environ, {"DATABASE_URL": dsn}, clear=True),
            patch("sys.argv", ["ip_bot"]),
            patch("ip_bot.__main__.Store", side_effect=psycopg.OperationalError(dsn)),
            contextlib.redirect_stderr(output),
            self.assertRaises(SystemExit) as stopped,
        ):
            main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertIn("Ошибка PostgreSQL", output.getvalue())
        self.assertNotIn("fake-test-password", output.getvalue())

    def test_subscription_uses_secret_and_preserves_other_webhooks(self):
        settings = Settings(webhook_url='https://bot.example.com/webhook', webhook_secret='s' * 48)
        api = Mock()
        api.request.side_effect = [{'subscriptions': []}, {'success': True}]
        with contextlib.redirect_stdout(io.StringIO()):
            subscribe(settings, api)
        self.assertEqual(api.request.call_args.kwargs['body'], {
            'url': settings.webhook_url, 'secret': settings.webhook_secret,
            'update_types': ['bot_started', 'message_created']})
        api.reset_mock()
        api.request.side_effect = [{'subscriptions': [{'url': 'https://previous.example.com/webhook'}]}]
        with self.assertRaises(ValueError):
            subscribe(settings, api)
        self.assertEqual(api.request.call_count, 1)

    def test_explicit_port_and_insecure_webhook_rejected(self):
        for url in ['http://bot.example.com/webhook', 'https://bot.example.com:443/webhook',
                    'https://bot.example.com/wrong', 'https://name@bot.example.com/webhook']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                subscribe(Settings(webhook_url=url, webhook_secret='s' * 48), Mock())


if __name__ == "__main__":
    unittest.main()

import io
import signal
import time
import unittest
from unittest.mock import patch

from ip_bot.assistant import Assistant
from ip_bot.budget import BudgetExpired, check_budget, remaining_seconds, time_limit
from ip_bot.llm import ModelError, Ollama, Plan
from ip_bot.settings import Settings


class BudgetTests(unittest.TestCase):
    def test_stalled_operation_is_interrupted_and_next_request_is_independent(self):
        previous = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with self.assertRaises(BudgetExpired):
            with time_limit(0.04):
                time.sleep(1)
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)
        self.assertIsNone(remaining_seconds())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        with time_limit(0.2):
            check_budget()

    def test_inner_budget_cannot_extend_parent_and_short_child_restores_parent(self):
        with time_limit(0.3):
            with time_limit(5):
                self.assertLessEqual(remaining_seconds(), 0.3)
            with self.assertRaises(BudgetExpired):
                with time_limit(0.02):
                    time.sleep(0.2)
            check_budget()
            self.assertGreater(remaining_seconds(), 0.1)

    def test_expired_budget_starts_no_work(self):
        for seconds in (0, -1):
            with self.subTest(seconds=seconds), self.assertRaises(BudgetExpired):
                with time_limit(seconds):
                    self.fail('Expired work must not start')

    def test_swallowed_child_expiration_does_not_disable_parent_alarm(self):
        started = time.monotonic()
        with self.assertRaises(BudgetExpired):
            with time_limit(0.03):
                try:
                    with time_limit(10):
                        time.sleep(0.5)
                except BudgetExpired:
                    # A domain adapter may catch a child timeout. Even then the
                    # already expired parent must stop unchecked blocking work.
                    time.sleep(0.5)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertIsNone(remaining_seconds())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_model_schema_preparation_is_inside_model_deadline(self):
        class SlowSchema:
            @staticmethod
            def model_json_schema():
                time.sleep(0.5)
                return {}
        model = Ollama(Settings(model_timeout=0.03))
        with patch.object(model.opener, 'open') as opened:
            with self.assertRaisesRegex(ModelError, 'model_timeout'):
                model.generate(SlowSchema, 'system', 'question')
        opened.assert_not_called()

    def test_model_hang_is_closed_and_reports_distinct_timeout(self):
        class SlowResponse(io.BytesIO):
            def read(self, *args):
                time.sleep(0.5)
                return super().read(*args)
        response = SlowResponse(b'{}')
        model = Ollama(Settings(model_timeout=0.03))
        with patch.object(model.opener, 'open', return_value=response):
            with self.assertRaisesRegex(ModelError, 'model_timeout'):
                model.generate(Plan, 'system', 'question')
        self.assertTrue(response.closed)

    def test_slow_research_ends_without_polluting_history_or_profile(self):
        class SlowResearcher:
            def search(self, plan):
                time.sleep(0.5)
                raise AssertionError('Слишком долго')
        state = {'answers': {}, 'paused': True}
        assistant = Assistant(None, Settings(), researcher=SlowResearcher())
        with patch('ip_bot.assistant.PREPARATION_SECONDS', 0.04):
            reply = assistant.respond(state, 'Какие документы нужны для регистрации ИП?')
        self.assertIn('Не успел', reply.text)
        self.assertFalse(reply.remember)
        self.assertEqual(reply.state, state)
        self.assertIn('Я бот', assistant.respond(state, 'Кто ты?').text)

    def test_explicit_model_timeout_is_not_clamped(self):
        with patch.dict('os.environ', {'AI_TIMEOUT_SECONDS': '600'}, clear=True):
            self.assertEqual(Settings.from_env().model_timeout, 600)

    def test_warmup_cannot_block_worker_start_indefinitely(self):
        class SlowResponse(io.BytesIO):
            def read(self, *args):
                time.sleep(0.5)
                return super().read(*args)
        response = SlowResponse(b'{"done":true}')
        model = Ollama(Settings(model_timeout=0.03))
        with patch.object(model.opener, 'open', return_value=response):
            with self.assertRaisesRegex(ModelError, 'model_warmup_timeout'):
                model.warmup()
        self.assertTrue(response.closed)

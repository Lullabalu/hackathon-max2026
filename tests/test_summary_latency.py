"""Short real waits verify nested deadlines and the shared model budget."""

import copy
from datetime import datetime, timezone
import signal
import time
import unittest
from unittest.mock import patch

from ip_bot.assistant import Assistant
from ip_bot.budget import BudgetExpired, PREPARATION_SECONDS, check_budget, remaining_seconds, time_limit
from ip_bot.llm import ModelError, QuestionIntent
from ip_bot.settings import Settings


PASSAGE = {
    'url': 'https://www.nalog.gov.ru/create_business/ip/creation/registration/step2/',
    'title': 'ФНС: подготовка документов',
    'text': 'Уникальный длинный фрагмент источника с условиями, который нельзя выдавать вместо короткой выжимки.',
    'fetched_at': datetime(2026, 9, 30, tzinfo=timezone.utc),
}
STATE = {'answers': {'region': 'Казань'}, 'consent': {'accepted': True}, 'paused': True}


class TimedModel:
    def __init__(self, *, planner=0, summary=1):
        self.delays = {'QuestionIntent': planner, 'Summary': summary}
        self.calls = []

    def generate(self, schema, system, content, **kwargs):
        call = {'schema': schema.__name__, 'allowance': remaining_seconds(), 'completed': False}
        self.calls.append(call)
        started = time.monotonic()
        try:
            time.sleep(self.delays[call['schema']])
            if schema is QuestionIntent:
                call['completed'] = True
                return QuestionIntent(action='search', text='Документы для регистрации ИП', topics=['registration'])
            raise AssertionError('Зависшая выжимка не была остановлена таймером')
        finally:
            call['elapsed'] = time.monotonic() - started


class TimedSources:
    def __init__(self, delay=0):
        self.delay = delay
        self.calls = []

    def search(self, plan):
        self.calls.append(plan)
        time.sleep(self.delay)
        return [copy.deepcopy(PASSAGE)], []


@unittest.skipUnless(hasattr(signal, 'setitimer'), 'Нужен POSIX-таймер основного потока')
class SummaryLatencyTests(unittest.TestCase):
    def setUp(self):
        self.handler = signal.getsignal(signal.SIGALRM)
        self.assertIsNone(remaining_seconds())
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def tearDown(self):
        self.assertIsNone(remaining_seconds())
        self.assertIs(signal.getsignal(signal.SIGALRM), self.handler)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def assistant(self, model, sources=None, *, model_seconds=0.06):
        assistant = Assistant(None, Settings(), model=model, researcher=sources or TimedSources())
        assistant.model_seconds = model_seconds
        return assistant

    def assert_fallback(self, reply, state):
        self.assertEqual(reply.state, state)
        self.assertFalse(reply.remember)
        self.assertEqual(reply.files, [])
        self.assertIn(PASSAGE['url'], reply.text)
        self.assertLessEqual(len(reply.text), 900)
        self.assertNotIn(PASSAGE['text'], reply.text)
        self.assertNotIn('«', reply.text)
        self.assertNotIn('Выдержки из официальных', reply.text)

    def test_stage_timeout_returns_model_error_while_parent_remains_usable(self):
        model = TimedModel(planner=1)
        assistant = self.assistant(model)
        with time_limit(0.6):
            started = time.monotonic()
            with self.assertRaisesRegex(ModelError, '^model_timeout$'):
                assistant.generate_stage(QuestionIntent, '', '', seconds=0.04, max_tokens=1)
            self.assertLess(time.monotonic() - started, 0.3)
            check_budget()
            self.assertGreater(remaining_seconds(), 0.3)
            # No stale child alarm may interrupt an independent next stage.
            time.sleep(0.01)
            check_budget()
        self.assertEqual(len(model.calls), 1)
        self.assertFalse(model.calls[0]['completed'])

    def test_stage_cannot_extend_outer_budget_or_disguise_parent_expiration(self):
        self.assertEqual(PREPARATION_SECONDS, 24)
        model = TimedModel(planner=1)
        assistant = self.assistant(model)
        started = time.monotonic()
        with self.assertRaises(BudgetExpired):
            with time_limit(0.05):
                assistant.generate_stage(QuestionIntent, '', '', seconds=PREPARATION_SECONDS, max_tokens=1)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(len(model.calls), 1)
        self.assertLessEqual(model.calls[0]['allowance'], 0.05)

    def test_hung_summary_returns_short_url_fallback_with_live_parent(self):
        model, sources = TimedModel(), TimedSources()
        assistant = self.assistant(model, sources)
        original = copy.deepcopy(STATE)
        with patch('ip_bot.assistant.MIN_SUMMARY_SECONDS', 0.005), time_limit(0.8):
            started = time.monotonic()
            reply = assistant.respond(STATE, 'Как заполнить Р21001?')
            self.assertLess(time.monotonic() - started, 0.3)
            check_budget()
            self.assertGreater(remaining_seconds(), 0.4)
            self.assert_fallback(reply, original)
        self.assertEqual(STATE, original)
        self.assertEqual(len(sources.calls), 1)
        self.assertEqual([item['schema'] for item in model.calls], ['Summary'])
        self.assertLessEqual(model.calls[0]['allowance'], assistant.model_seconds)

    def test_hung_planner_never_starts_search_or_summary(self):
        model, sources = TimedModel(planner=1), TimedSources()
        assistant = self.assistant(model, sources, model_seconds=0.05)
        with time_limit(0.8):
            started = time.monotonic()
            reply = assistant.respond(STATE, 'Что с нужным уведомлением?')
            self.assertLess(time.monotonic() - started, 0.3)
            self.assertGreater(remaining_seconds(), 0.4)
            check_budget()
        self.assertIn('Не успел', reply.text)
        self.assertEqual(reply.state, STATE)
        self.assertFalse(reply.remember)
        self.assertEqual(sources.calls, [])
        self.assertEqual([item['schema'] for item in model.calls], ['QuestionIntent'])

    def test_planner_and_summary_share_one_model_budget_not_two(self):
        model, sources = TimedModel(planner=0.10), TimedSources(delay=0.04)
        assistant = self.assistant(model, sources, model_seconds=0.16)
        with patch('ip_bot.assistant.MIN_SUMMARY_SECONDS', 0.005), time_limit(1):
            started = time.monotonic()
            reply = assistant.respond(STATE, 'Что с нужным уведомлением?')
            elapsed = time.monotonic() - started
            check_budget()
            self.assertGreater(remaining_seconds(), 0.4)
        self.assertEqual([item['schema'] for item in model.calls], ['QuestionIntent', 'Summary'])
        self.assertTrue(model.calls[0]['completed'])
        self.assertFalse(model.calls[1]['completed'])
        self.assertLessEqual(sum(item['elapsed'] for item in model.calls), assistant.model_seconds + 0.04)
        self.assertLessEqual(model.calls[1]['allowance'], 0.08)
        # Source time is part of the parent budget, not a second model call.
        self.assertGreater(model.calls[1]['allowance'], 0.025)
        self.assertLess(elapsed, 0.4)
        self.assertEqual(len(sources.calls), 1)
        self.assert_fallback(reply, STATE)

    def test_smalltalk_does_not_enter_any_model_stage_or_search(self):
        model, sources = TimedModel(planner=1, summary=1), TimedSources(delay=1)
        assistant = self.assistant(model, sources)
        state = {'answers': {'activity': 'Разработка сайтов'}, 'consent': {'accepted': True},
                 'paused': True, 'guide': {'active': True, 'stage': 'profile', 'field': 'region', 'history': []}}
        original = copy.deepcopy(state)
        with time_limit(0.2):
            reply = assistant.respond(state, 'Привет, как дела?')
        self.assertEqual(reply.state, original)
        self.assertEqual(state, original)
        self.assertFalse(reply.remember)
        self.assertEqual(model.calls, [])
        self.assertEqual(sources.calls, [])
        self.assertNotIn(PASSAGE['url'], reply.text)


if __name__ == '__main__':
    unittest.main()

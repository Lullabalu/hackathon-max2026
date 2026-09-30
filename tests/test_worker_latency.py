"""Hard deadlines use actual sleeps, fake transports, and isolated PostgreSQL."""

import copy
import os
import time
import unittest
from unittest.mock import Mock, patch
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from ip_bot.__main__ import drain, drain_notifications, polling
from ip_bot.assistant import Assistant
from ip_bot.budget import (BudgetExpired, DELIVERY_SECONDS, FAILURE_RECORD_SECONDS,
                           MODEL_SECONDS, PREPARATION_SECONDS, PROCESSING_SECONDS, time_limit)
from ip_bot.models import Reply
from ip_bot.settings import Settings
from ip_bot.storage import Store, StoreInUseError


class FakeStore:
    def __init__(self, *, age=0, files=()):
        self.event = dict(seq=1, user_id=1, text='/ask Какие документы нужны для ИП?',
                          reply=None, attempts=0, queue_age_seconds=age)
        self.state = {'answers': {}, 'paused': True}
        self.files, self.result = list(files), None
        self.deferred = self.done = False
        self.history = Mock(return_value=[])
        self.failures = []

    def pending(self):
        return None if self.done or self.deferred else self.event

    def prepare_reply(self, event, responder):
        if event['reply'] is None:
            self.result = responder(copy.deepcopy(self.state), event['text'])
            self.state = self.result.state
            event['reply'] = self.result.text
        return event['reply']

    def prepared_files(self, seq):
        return self.files

    def delivered(self, seq):
        self.done = True

    def fail(self, event, code, *, permanent=False):
        self.failures.append((code, permanent))
        self.deferred = True


class WorkerLatencyTests(unittest.TestCase):
    def setUp(self):
        route = patch('ip_bot.__main__.Assistant.needs_research', return_value=True)
        route.start()
        self.addCleanup(route.stop)

    def test_hung_preparation_leaves_time_to_persist_timeout_and_deliver(self):
        store, assistant, sent = FakeStore(), Mock(), []
        original = copy.deepcopy(store.state)

        def hung(*args):
            time.sleep(0.5)
            return Reply({'wrong': True}, 'Too late', remember=True)

        assistant.respond.side_effect = hung
        started = time.monotonic()
        with patch('ip_bot.__main__.PREPARATION_SECONDS', 0.1), \
                patch('ip_bot.__main__.PERSISTENCE_RESERVE_SECONDS', 0.04):
            self.assertEqual(drain(store, lambda _, text: sent.append(text), assistant), 1)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertIn('Не успел', sent[0])
        self.assertFalse(store.result.remember)
        self.assertEqual(store.state, original)
        self.assertTrue(store.done)

    def test_expired_research_skips_history_and_assistant(self):
        store, assistant, sent = FakeStore(age=60), Mock(), []
        drain(store, lambda _, text: sent.append(text), assistant)
        assistant.respond.assert_not_called()
        store.history.assert_not_called()
        self.assertIn('Не успел', sent[0])

    def test_pending_preparation_delivery_and_failure_fit_one_overall_allowance(self):
        self.assertEqual(PROCESSING_SECONDS, 30)
        self.assertEqual(PREPARATION_SECONDS + DELIVERY_SECONDS + FAILURE_RECORD_SECONDS, PROCESSING_SECONDS)
        self.assertEqual(MODEL_SECONDS, 20)
        store, assistant = FakeStore(), Mock()
        events = []

        def slow_pending():
            time.sleep(0.05)
            return store.event

        def hung(stage):
            def run(*args, **kwargs):
                events.append(stage)
                time.sleep(0.5)
            return run

        store.pending = slow_pending
        store.fail = hung('record_failure')
        assistant.respond.side_effect = hung('prepare')
        started = time.monotonic()
        with patch('ip_bot.__main__.PREPARATION_SECONDS', 0.12), \
                patch('ip_bot.__main__.PERSISTENCE_RESERVE_SECONDS', 0.02), \
                patch('ip_bot.__main__.DELIVERY_SECONDS', 0.04), \
                patch('ip_bot.__main__.FAILURE_RECORD_SECONDS', 0.03):
            drain(store, hung('send'), assistant, retry_errors=True)
        # The first 50 ms are part of preparation, not an extra allowance.
        self.assertLess(time.monotonic() - started, 0.22)
        self.assertEqual(events, ['prepare', 'send', 'record_failure'])

    def test_hung_delivery_retries_prepared_text_and_files_without_regeneration(self):
        for files in ([], [{'name': 'example.pdf', 'content': 'synthetic'}]):
            with self.subTest(files=bool(files)):
                store, assistant = FakeStore(files=files), Mock()
                assistant.respond.return_value = Reply(store.state, 'Готовый ответ', remember=True)
                hung = Mock(side_effect=lambda *args: time.sleep(0.5))
                started = time.monotonic()
                with patch('ip_bot.__main__.DELIVERY_SECONDS', 0.03):
                    drain(store, hung, assistant, send_files=hung, retry_errors=True)
                self.assertLess(time.monotonic() - started, 0.3)
                self.assertFalse(store.done)
                self.assertEqual(store.event['reply'], 'Готовый ответ')
                self.assertEqual(store.failures, [('event_budget_timeout', False)])
                store.deferred = False
                store.event['queue_age_seconds'] = 60
                sent = Mock()
                drain(store, sent, assistant, send_files=sent)
                self.assertTrue(store.done)
                assistant.respond.assert_called_once()
                sent.assert_called_once()
                self.assertEqual(sent.call_args.args[1], 'Готовый ответ')

    def test_notification_hang_is_bounded_and_not_marked_sent(self):
        store, services = Mock(), Mock()
        store.pending.return_value = None
        services.due.return_value = [{'id': 7, 'user_id': 1, 'text': 'Напоминание'}]
        started = time.monotonic()
        with patch('ip_bot.__main__.Services', return_value=services):
            self.assertEqual(drain_notifications(store, lambda *_: time.sleep(0.5), budget_seconds=0.03), 1)
        self.assertLess(time.monotonic() - started, 0.3)
        services.mark.assert_not_called()
        services.fail.assert_called_once_with(7, 'notification_timeout')
        services.due.assert_called_once_with(limit=1)

    def test_notifications_yield_to_new_user_events_between_messages(self):
        for pending, expected in (([{'seq': 1}], 0), ([None, None, {'seq': 1}], 1)):
            with self.subTest(expected=expected):
                store, services, send = Mock(), Mock(), Mock()
                store.pending.side_effect = pending
                services.due.return_value = [dict(id=i, user_id=1, text='Напоминание') for i in (1, 2, 3)]
                with patch('ip_bot.__main__.Services', return_value=services):
                    self.assertEqual(drain_notifications(store, send, max_items=3), expected)
                self.assertEqual(send.call_count, expected)
                self.assertEqual(services.mark.call_count, expected)
                if not expected:
                    services.due.assert_not_called()


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Нужен TEST_DATABASE_URL')
class PostgreSQLLatencyTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'test_latency_' + uuid.uuid4().hex
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.store = None
        self.addCleanup(self.cleanup_database)
        self.dsn = make_conninfo(os.environ['TEST_DATABASE_URL'], options=f'-c search_path={self.schema}')
        self.store = Store(self.dsn, worker=False)

    def cleanup_database(self):
        if self.store is not None:
            self.store.close()
        try:
            self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        finally:
            self.admin.close()

    def enqueue(self, text='/ask Какие документы нужны для ИП?'):
        self.store.enqueue([dict(event_id=uuid.uuid4().hex, user_id=1, text=text)])

    def reconnect(self):
        # Closing the client socket is immediate; PostgreSQL may release its
        # session lock only after the interrupted statement ends on the server.
        deadline = time.monotonic() + 1
        while True:
            try:
                return self.store.reconnect_if_needed()
            except StoreInUseError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)

    def test_interrupted_query_closes_connection_and_reconnect_reacquires_worker_lock(self):
        self.store.close()
        self.store = Store(self.dsn, initialize=False)
        started = time.monotonic()
        with self.assertRaises(BudgetExpired):
            with time_limit(0.04):
                self.store.db.execute('SELECT pg_sleep(0.5)')
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertTrue(self.store.db.closed)
        self.assertTrue(self.store.db.budget_interrupted)
        self.assertTrue(self.reconnect())
        self.assertFalse(self.store.reconnect_if_needed())
        self.assertEqual(self.store.db.execute('SELECT current_schema() AS name').fetchone()['name'], self.schema)
        with self.assertRaises(StoreInUseError):
            Store(self.dsn, initialize=False)
        self.enqueue('/help')
        self.assertIsNotNone(self.store.pending())

    def test_expired_real_assistant_route_skips_model_research_and_history(self):
        state = {'answers': {}, 'paused': True, 'consent': {'accepted': True}}
        self.store.db.execute('INSERT INTO profiles(user_id, state) VALUES (1, %s)', (Jsonb(state),))
        self.enqueue()
        self.store.db.execute("UPDATE events SET received_at = now() - interval '1 minute'")
        model, researcher, sent = Mock(), Mock(), Mock()
        assistant = Assistant(self.store, Settings(), model=model, researcher=researcher)
        with patch.object(self.store, 'history', side_effect=AssertionError('No history for expired work')):
            drain(self.store, sent, assistant)
        model.generate.assert_not_called()
        researcher.search.assert_not_called()
        self.assertIn('Не успел', sent.call_args.args[1])
        self.assertEqual(self.store.load_profile(1), state)
        self.assertEqual(self.store.history(1), [])

    def test_hung_database_during_preparation_does_not_start_model_or_kill_worker(self):
        self.enqueue()
        assistant, sent = Mock(), Mock()

        def hung_history(user_id):
            self.store.db.execute('SELECT pg_sleep(0.5)')
            return []

        started = time.monotonic()
        with patch.object(self.store, 'history', side_effect=hung_history), \
                patch('ip_bot.__main__.PREPARATION_SECONDS', 0.1), \
                patch('ip_bot.__main__.PERSISTENCE_RESERVE_SECONDS', 0.04):
            drain(self.store, sent, assistant, retry_errors=True)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertTrue(self.store.db.closed)
        assistant.respond.assert_not_called()
        sent.assert_not_called()
        self.assertTrue(self.reconnect())
        self.assertIsNone(self.store.pending()['reply'])
        self.store.db.execute("UPDATE events SET received_at = now() - interval '1 minute'")
        drain(self.store, sent, assistant, retry_errors=True)
        assistant.respond.assert_not_called()
        self.assertIn('Не успел', sent.call_args.args[1])
        self.assertIsNone(self.store.pending())

    def test_lock_timeout_defers_event_and_worker_can_deliver_on_retry(self):
        self.enqueue('/help')
        event = self.store.pending()
        self.store.prepare_reply(event, lambda state, text: Reply(state, 'Сохранённый ответ'))
        locker = psycopg.connect(self.dsn)
        try:
            locker.execute('SELECT seq FROM events WHERE seq = %s FOR UPDATE', (event['seq'],))
            self.store.db.execute("SET lock_timeout = '30ms'")
            sent, assistant = Mock(), Mock()
            started = time.monotonic()
            drain(self.store, sent, assistant, retry_errors=True)
            self.assertLess(time.monotonic() - started, 0.4)
            self.assertFalse(self.store.db.closed)
            assistant.respond.assert_not_called()
        finally:
            locker.rollback()
            locker.close()
        # Failure bookkeeping was blocked by the same row lock. The already
        # prepared reply stays durable and the next worker pass can retry it.
        self.assertEqual(self.store.pending()['reply'], 'Сохранённый ответ')
        drain(self.store, sent, assistant, retry_errors=True)
        self.assertIsNone(self.store.pending())
        assistant.respond.assert_not_called()

    def test_polling_retries_database_timeout_only_after_reacquiring_session_lock(self):
        class StopPolling(BaseException):
            pass

        self.store.set_meta('marker', 10)
        self.store.db.execute("SET lock_timeout = '30ms'")
        competitor = psycopg.connect(self.dsn)
        self.addCleanup(competitor.close)
        competitor.execute("SELECT value FROM meta WHERE key = 'marker' FOR UPDATE")
        api, pauses = Mock(), []
        api.request.return_value = {}

        def updates(marker):
            if api.updates.call_count == 1:
                self.assertEqual(marker, 10)
                return {'marker': 20, 'updates': [{'update_type': 'bot_started', 'timestamp': 1,
                                                   'user': {'user_id': 1}}]}
            self.assertEqual(marker, 20)
            self.assertFalse(competitor.execute('SELECT pg_try_advisory_lock(1296128066, 4)').fetchone()[0])
            raise StopPolling()

        def retry_pause(delay):
            pauses.append(delay)
            self.assertTrue(self.store.db.closed)
            # The fetched batch must not trigger another MAX request while its
            # DB write is uncommitted or another poller owns the session lock.
            self.assertEqual(api.updates.call_count, 1)
            competitor.rollback()
            self.assertEqual(competitor.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone()[0], 10)
            self.assertEqual(competitor.execute('SELECT count(*) FROM events').fetchone()[0], 0)
            if len(pauses) == 1:
                self.assertTrue(competitor.execute('SELECT pg_try_advisory_lock(1296128066, 4)').fetchone()[0])
            else:
                self.assertEqual(len(pauses), 2)
                competitor.execute('SELECT pg_advisory_unlock(1296128066, 4)')
            competitor.commit()

        api.updates.side_effect = updates
        with patch('ip_bot.__main__.time.sleep', side_effect=retry_pause), self.assertRaises(StopPolling):
            polling(self.store, api)
        self.assertEqual(pauses, [1, 2])
        self.assertEqual(api.updates.call_count, 2)
        self.assertEqual(self.store.get_meta('marker'), 20)
        self.assertEqual(self.store.db.execute('SELECT count(*) AS n FROM events').fetchone()['n'], 1)


if __name__ == '__main__':
    unittest.main()

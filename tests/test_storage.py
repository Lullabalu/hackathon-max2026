"""Интеграционные тесты на настоящем PostgreSQL, в отдельной временной схеме."""

import os
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ip_bot.__main__ import drain
from ip_bot.storage import Store, StoreInUseError
from ip_bot.models import Reply
from ip_bot.sources import Document
from ip_bot.max_api import APIError


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "Нужен TEST_DATABASE_URL с отдельной тестовой базой PostgreSQL")
class StorageTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True)
        self.schema = "test_bot_" + uuid.uuid4().hex
        self.store = None
        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup_database)
        self.dsn = make_conninfo(
            os.environ["TEST_DATABASE_URL"], options=f"-c search_path={self.schema}",
        )
        self.store = Store(self.dsn)

    def cleanup_database(self):
        if self.store is not None:
            self.store.close()
        try:
            self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))
        finally:
            self.admin.close()

    def send_input(self, text, user_id=1):
        self.store.enqueue([{"event_id": uuid.uuid4().hex, "user_id": user_id, "text": text}])
        drain(self.store, lambda *_: None)

    def test_received_at_is_recorded_once_and_queue_age_survives_retry(self):
        before = self.store.db.execute('SELECT clock_timestamp() AS timestamp').fetchone()['timestamp']
        event = {'event_id': 'received-once', 'user_id': 1, 'text': 'Вопрос'}
        self.store.enqueue([event])
        pending = self.store.pending()
        after = self.store.db.execute('SELECT clock_timestamp() AS timestamp').fetchone()['timestamp']
        self.assertLessEqual(before, pending['received_at'])
        self.assertLessEqual(pending['received_at'], after)
        self.assertGreaterEqual(pending['queue_age_seconds'], 0)

        self.store.db.execute("UPDATE events SET received_at = clock_timestamp() - interval '20 seconds' WHERE seq = %s",
                              (pending['seq'],))
        pending = self.store.pending()
        original_received_at = pending['received_at']
        self.assertGreaterEqual(pending['queue_age_seconds'], 20)
        self.store.fail(pending, 'temporary_failure')
        self.store.enqueue([event])
        self.store.db.execute('UPDATE events SET next_attempt_at = now() WHERE seq = %s', (pending['seq'],))
        retried = self.store.pending()
        self.assertEqual(retried['received_at'], original_received_at)
        self.assertGreaterEqual(retried['queue_age_seconds'], pending['queue_age_seconds'])
        self.assertEqual(retried['attempts'], 1)

    def test_queue_age_does_not_change_order_or_bypass_same_user_retry(self):
        self.store.enqueue([{'event_id': name, 'user_id': user, 'text': 'Вопрос'}
                            for name, user in [('a', 1), ('b', 1), ('c', 2)]])
        # Искусственно старое время c не меняет порядок seq и зависимость b от a.
        self.store.db.execute("UPDATE events SET received_at = clock_timestamp() - interval '90 seconds' WHERE event_id = 'c'")
        first = self.store.pending()
        self.assertEqual(first['event_id'], 'a')
        self.store.fail(first, 'temporary_failure')
        other = self.store.pending()
        self.assertEqual(other['event_id'], 'c')
        self.assertGreaterEqual(other['queue_age_seconds'], 90)
        self.store.db.execute('UPDATE events SET next_attempt_at = now() WHERE seq = %s', (first['seq'],))
        self.assertEqual(self.store.pending()['event_id'], 'a')
        self.store.delivered(first['seq'])
        self.assertEqual(self.store.pending()['event_id'], 'b')

    def test_received_at_migration_preserves_existing_pending_events(self):
        # Имитируем старую схему в изолированной тестовой базе.
        self.store.db.execute('ALTER TABLE events DROP COLUMN received_at')
        self.store.db.execute("DELETE FROM schema_migrations WHERE version = '004_event_received_at.sql'")
        self.store.enqueue([{'event_id': 'legacy', 'user_id': 1, 'text': 'Старый вопрос'}])
        self.store.migrate()
        pending = self.store.pending()
        self.assertEqual(pending['event_id'], 'legacy')
        self.assertEqual(pending['text'], 'Старый вопрос')
        self.assertIsNotNone(pending['received_at'])
        self.assertGreaterEqual(pending['queue_age_seconds'], 0)

    def test_restart_duplicate_and_delivery_failure(self):
        self.store.enqueue([
            {"event_id": "a", "user_id": 1, "text": "/begin"},
            {"event_id": "b", "user_id": 1, "text": "Ремонт компьютеров"},
        ], marker=777)
        delivered = []
        first = self.store.pending()
        delivered.append(self.store.prepare_reply(first))
        self.store.delivered(first["seq"])
        original_reply = self.store.prepare_reply(self.store.pending())
        self.store.close()

        self.store = Store(self.dsn)
        self.assertEqual(self.store.get_meta("marker"), 777)
        self.assertEqual(self.store.prepare_reply(self.store.pending()), original_reply)
        drain(self.store, lambda user_id, text: delivered.append(text))
        self.store.enqueue([{"event_id": "b", "user_id": 1, "text": "Ремонт компьютеров"}], marker=778)
        drain(self.store, lambda user_id, text: delivered.append(text))
        self.assertEqual(len(delivered), 2)
        self.assertEqual(self.store.get_meta("marker"), 778)
        self.assertEqual(self.store.load_profile(1)["answers"], {"activity": "Ремонт компьютеров"})

    def test_separate_users_and_delete(self):
        for user, text in ((1, "/begin"), (2, "/begin"), (1, "Разработка сайтов"), (2, "Ремонт техники"), (1, "/delete")):
            self.send_input(text, user)
        self.assertIsNone(self.store.load_profile(1))
        self.assertEqual(self.store.load_profile(2)["answers"]["activity"], "Ремонт техники")
        old_texts = self.store.db.execute("SELECT text, reply FROM events WHERE user_id = 1").fetchall()
        self.assertTrue(all(row["text"] == row["reply"] == "" for row in old_texts))

    def test_failed_send_keeps_pending_event(self):
        self.store.enqueue([{"event_id": "a", "user_id": 1, "text": "/begin"}])

        def unavailable(*_):
            raise OSError("network unavailable")

        with self.assertRaises(OSError):
            drain(self.store, unavailable)
        self.assertIsNotNone(self.store.pending())
        self.assertEqual(self.store.load_profile(1), {"answers": {}})

    def test_batch_and_marker_roll_back_together(self):
        self.store.set_meta("marker", 123)
        with self.assertRaises(psycopg.errors.NotNullViolation):
            self.store.enqueue([
                {"event_id": "valid", "user_id": 1, "text": "/begin"},
                {"event_id": "broken", "user_id": 1, "text": None},
            ], marker=456)
        self.assertIsNone(self.store.pending())
        self.assertEqual(self.store.get_meta("marker"), 123)
        self.store.enqueue([], marker=0)
        self.assertEqual(self.store.get_meta("marker"), 0)

    def test_stale_event_does_not_advance_survey_twice(self):
        self.send_input("/begin")
        self.store.enqueue([{"event_id": "activity", "user_id": 1, "text": "Ремонт техники"}])
        event = self.store.pending()
        reply = self.store.prepare_reply(event)
        self.assertEqual(self.store.prepare_reply(event), reply)
        self.assertEqual(self.store.load_profile(1)["answers"], {"activity": "Ремонт техники"})

    def test_profile_and_reply_roll_back_together(self):
        self.send_input("/begin")
        self.store.enqueue([{"event_id": "activity", "user_id": 1, "text": "Ремонт техники"}])
        self.store.db.execute("""
            CREATE FUNCTION fail_reply() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'simulated write failure'; END;
            $$;
            CREATE TRIGGER fail_reply BEFORE UPDATE ON events
                FOR EACH ROW EXECUTE FUNCTION fail_reply();
        """)
        with self.assertRaises(psycopg.errors.RaiseException):
            self.store.prepare_reply(self.store.pending())
        self.assertEqual(self.store.load_profile(1), {"answers": {}})
        self.assertIsNone(self.store.pending()["reply"])
        self.store.db.execute("DROP TRIGGER fail_reply ON events")
        drain(self.store, lambda *_: None)
        self.assertEqual(self.store.load_profile(1)["answers"], {"activity": "Ремонт техники"})

    def test_bigint_user_and_decimal_values_survive_restart(self):
        user_id = 2**40
        for answer in ("/begin", "Разработка сайтов", "Самарская область", "1 200 000,50", "300000", "2", "3"):
            self.send_input(answer, user_id)
        self.store.close()
        self.store = Store(self.dsn)
        answers = self.store.load_profile(user_id)["answers"]
        self.assertEqual(answers["revenue"], "1200000.50")
        self.assertEqual(answers["employees"], 2)
        self.assertEqual(answers["region"], "Самарская область")

    def test_new_clears_old_profile_and_delivery_content(self):
        self.send_input("/begin")
        self.send_input("Ремонт техники")
        self.send_input("/new")
        self.assertEqual(self.store.load_profile(1), {"answers": {}})
        rows = self.store.db.execute("SELECT text, reply FROM events").fetchall()
        self.assertTrue(all(row["text"] == row["reply"] == "" for row in rows))

    def test_second_worker_rejected_until_connection_closes(self):
        with self.assertRaises(StoreInUseError):
            Store(self.dsn)
        self.store.close()
        self.store = Store(self.dsn)
        self.send_input("/begin")
        self.assertEqual(self.store.load_profile(1), {"answers": {}})

    def test_generation_has_no_transaction_and_receiver_keeps_accepting(self):
        receiver = Store(self.dsn, worker=False)
        self.addCleanup(receiver.close)
        self.store.enqueue([{'event_id': 'ai', 'user_id': 1, 'text': 'Документы ИП'}])
        def generate(state, text):
            self.assertEqual(self.store.db.info.transaction_status, psycopg.pq.TransactionStatus.IDLE)
            receiver.enqueue([{'event_id': 'while-generating', 'user_id': 2, 'text': '/help'}])
            return Reply(state, 'Проверенный ответ', remember=True)
        event = self.store.pending()
        self.assertEqual(self.store.prepare_reply(event, generate), 'Проверенный ответ')
        def must_not_generate(*args):
            self.fail('Сохранённый ответ не должен генерироваться повторно')
        self.assertEqual(self.store.prepare_reply(event, must_not_generate), 'Проверенный ответ')
        self.assertEqual(self.store.history(1)[0]['question'], 'Документы ИП')
        self.store.delivered(event['seq'])
        self.assertEqual(self.store.pending()['user_id'], 2)

    def test_retries_preserve_user_order_but_allow_other_users(self):
        self.store.enqueue([{'event_id': name, 'user_id': user, 'text': '/help'}
                            for name, user in [('a', 1), ('b', 1), ('c', 2)]])
        first = self.store.pending()
        self.store.fail(first, 'network')
        other = self.store.pending()
        self.assertEqual(other['event_id'], 'c')
        self.store.delivered(other['seq'])
        self.assertIsNone(self.store.pending())
        self.store.fail(first, 'forbidden', permanent=True)
        self.assertEqual(self.store.pending()['event_id'], 'b')

    def test_history_limited_and_delete_removes_it(self):
        for i in range(23):
            self.store.enqueue([{'event_id': str(i), 'user_id': 1, 'text': f'Вопрос {i}'}])
            event = self.store.pending()
            self.store.prepare_reply(event, lambda state, text: Reply(state, 'Ответ', remember=True))
            self.store.delivered(event['seq'])
        self.assertEqual(self.store.db.execute('SELECT count(*) AS n FROM conversation_turns').fetchone()['n'], 20)
        self.assertEqual(self.store.history(1)[-1]['question'], 'Вопрос 22')
        self.send_input('/delete')
        self.assertEqual(self.store.history(1), [])

    def test_documents_russian_search_refresh_and_scope(self):
        document = Document('https://nalog.gov.ru/a', 'Форма Р21001',
                            'Для регистрации индивидуального предпринимателя нужно заявление по форме Р21001.', [], 'text/html')
        self.store.save_document(document)
        original_ids = [r['id'] for r in self.store.db.execute('SELECT id FROM passages')]
        self.store.save_document(document)
        self.assertEqual(original_ids, [r['id'] for r in self.store.db.execute('SELECT id FROM passages')])
        results = self.store.search_documents('регистрация предпринимателя', [document.url])
        self.assertEqual(results[0]['url'], document.url)
        self.assertIn('Р21001', results[0]['text'])
        self.assertEqual(self.store.search_documents('регистрация', ['https://nalog.gov.ru/other']), [])
        document.text = 'Новый документ о прекращении деятельности индивидуального предпринимателя и закрытии ИП.'
        self.store.save_document(document)
        self.assertEqual(self.store.search_documents('Р21001', [document.url]), [])

    def test_generated_reply_survives_delivery_retry_without_second_generation(self):
        self.store.enqueue([{'event_id': 'ai-retry', 'user_id': 1, 'text': 'Документы ИП'}])
        calls, sent = [], []
        class Assistant:
            def needs_research(self, state, text):
                return True
            def respond(self, state, text, history):
                calls.append(text)
                return Reply(state, 'Готовый ответ', remember=True)
        def unavailable(user_id, text):
            if text == 'Готовый ответ':
                raise APIError('test failure', retryable=True, status=429)
            sent.append(text)
        drain(self.store, unavailable, Assistant(), retry_errors=True)
        self.assertEqual(len(sent), 0)
        self.assertEqual(len(calls), 1)
        self.store.db.execute("UPDATE events SET next_attempt_at = now(), received_at = now() - interval '1 minute'")
        drain(self.store, lambda user, text: sent.append(text), Assistant(), retry_errors=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sent[-1], 'Готовый ответ')
        self.assertEqual(len(sent), 1)
        self.assertIsNone(self.store.pending())

    def test_old_question_is_answered_and_remembered(self):
        self.store.enqueue([{'event_id': 'old-ai', 'user_id': 1, 'text': 'Документы ИП'}])
        self.store.db.execute("UPDATE events SET received_at = now() - interval '1 hour'")
        class Respond:
            def respond(self, state, text, history):
                return Reply(state, 'Подтверждённый ответ', remember=True)
        sent = []
        drain(self.store, lambda user, text: sent.append(text), Respond())
        self.assertEqual(sent, ['Подтверждённый ответ'])
        self.assertEqual(len(self.store.history(1)), 1)
        self.assertIsNone(self.store.pending())

    def test_expired_delete_and_new_still_apply_to_profile(self):
        from ip_bot.assistant import Assistant
        from ip_bot.settings import Settings
        for command in ('/delete', '/new'):
            with self.subTest(command=command):
                self.send_input('/begin')
                self.send_input('Разработка сайтов')
                self.store.enqueue([{'event_id': 'expired-' + command, 'user_id': 1, 'text': command}])
                self.store.db.execute("UPDATE events SET received_at = now() - interval '1 minute' WHERE NOT delivered")
                sent = []
                drain(self.store, lambda user, text: sent.append(text), Assistant(self.store, Settings()))
                self.assertNotIn('Не успел', sent[0])
                expected = None if command == '/delete' else {'answers': {}}
                self.assertEqual(self.store.load_profile(1), expected)

    def test_search_keeps_unmatched_next_passage_with_fee_exception(self):
        foreign = Document('https://nalog.gov.ru/foreign', 'Другая страница',
                           'Чужая страница не должна попадать в соседний контекст. ' * 25, [], 'text/html')
        self.store.save_document(foreign)
        first = 'Подайте заявление по форме Р21001. При бумажной подаче оплачивается государственная пошлина. '
        padding = 'Пояснение к порядку подачи. ' * 40
        exception = 'При электронной подаче документов уплачивать государственную пошлину не требуется.'
        document = Document('https://nalog.gov.ru/registration', 'Регистрация',
                            first + padding + exception, [], 'text/html')
        self.store.save_document(document)
        matched = self.store.db.execute('''SELECT text FROM passages WHERE url = %s
            AND search @@ websearch_to_tsquery('russian', 'Р21001')''', (document.url,)).fetchall()
        self.assertEqual(len(matched), 1)
        self.assertNotIn('не требуется', matched[0]['text'])

        results = self.store.search_documents('Р21001', [document.url], limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['url'], document.url)
        self.assertIn(exception, results[0]['text'])
        self.assertEqual(results[0]['text'], document.text)
        self.assertNotIn('Чужая страница', results[0]['text'])
        self.assertEqual(set(results[0]), {'url', 'title', 'text', 'fetched_at', 'rank'})

    def test_search_expands_bounded_neighbors_and_bounds_results(self):
        # Уникальные строки делают ошибочную склейку повторяющихся фраз заметной.
        text = ''.join(f'Строка {i:03}: пояснение условий документа.\n' for i in range(140))
        text = text[:2100] + ' Форма Р21001. ' + text[2100:]
        document = Document('https://nalog.gov.ru/context', 'Контекст', text, [], 'text/html')
        self.store.save_document(document)
        foreign = Document('https://nalog.gov.ru/other', 'Другой документ',
                           'Чужой контекст и форма Р21001. ' * 50, [], 'text/html')
        self.store.save_document(foreign)
        result = self.store.search_documents('Р21001', [document.url], limit=1)[0]
        # Термин внутри третьего окна; расширение включает второе–пятое.
        self.assertEqual(result['text'], text[840:4360].strip())
        self.assertLessEqual(len(result['text']), 3520)
        self.assertNotIn('Чужой контекст', result['text'])
        self.assertEqual(self.store.search_documents('Р21001', [document.url], limit=0), [])
        for i in range(15):
            self.store.save_document(Document(f'https://nalog.gov.ru/result-{i}', 'Форма',
                'Заявление по форме Р21001 подают при регистрации индивидуального предпринимателя.', [], 'text/html'))
        urls = [f'https://nalog.gov.ru/result-{i}' for i in range(15)]
        self.assertEqual(len(self.store.search_documents('Р21001', urls, limit=100)), 12)

    def test_search_keeps_later_filing_option_and_its_condition(self):
        opening = 'Заполните заявление Р21001. '
        padding = ''.join(f'Строка {i}: синтетическое пояснение. ' for i in range(60))
        option = 'В многофункциональный центр можно обратиться лично. '
        condition = 'Информацию об оказании данной услуги в МФЦ необходимо уточнить.'
        text = opening + padding + option + condition
        self.assertGreater(text.index(option), 1840)
        self.assertLess(len(text), 2680)
        document = Document('https://nalog.gov.ru/filing-context', 'Подача', text, [], 'text/html')
        self.store.save_document(document)
        result = self.store.search_documents('Р21001', [document.url], limit=1)[0]
        self.assertIn(option, result['text'])
        self.assertIn(condition, result['text'])


if __name__ == "__main__":
    unittest.main()

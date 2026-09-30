"""A persisted questionnaire remains the source for personal replies after restart."""

import copy
from decimal import Decimal
import os
import unittest
from unittest.mock import Mock
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ip_bot.__main__ import drain
from ip_bot.assistant import Assistant
from ip_bot.settings import Settings
from ip_bot.storage import Store


PROFILE_SECTIONS = ('answers', 'tax_options', 'registration', 'selected_regime', 'okved')
PERSONAL_QUESTION = 'Какой режим мне подходит?'
ACTIVITY = 'Разработка сайтов дополнительно консультирую по IT'


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Нужен TEST_DATABASE_URL с тестовым PostgreSQL')
class ProfilePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'profile_persistence_' + uuid.uuid4().hex
        self.store = None
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.addCleanup(self.cleanup)
        self.dsn = make_conninfo(os.environ['TEST_DATABASE_URL'], options=f'-c search_path={self.schema}')
        self.model, self.researcher = Mock(), Mock()
        self.model.generate.side_effect = AssertionError('Персональный расчёт не должен вызывать модель')
        self.researcher.search.side_effect = AssertionError('Для известной анкеты поиск не требуется')
        self.open_store()

    def open_store(self):
        # The production worker's database-wide lock must not block isolated tests.
        self.store = Store(self.dsn, worker=False)
        self.assistant = Assistant(self.store, Settings(), model=self.model, researcher=self.researcher)

    def reopen(self):
        old_store, old_assistant = self.store, self.assistant
        old_store.close()
        self.open_store()
        self.assertIsNot(self.store, old_store)
        self.assertIsNot(self.assistant, old_assistant)

    def cleanup(self):
        if self.store is not None:
            self.store.close()
        try:
            self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        finally:
            self.admin.close()

    def tearDown(self):
        self.model.generate.assert_not_called()
        self.assertEqual(self.researcher.mock_calls, [])

    def send(self, text, user_id=1):
        self.store.receive([{'event_id': uuid.uuid4().hex, 'user_id': user_id, 'text': text}])
        event = self.store.pending()
        self.assertIsNotNone(event)
        self.assertEqual(event['user_id'], user_id)
        state = self.store.load_profile(user_id)
        self.assertFalse(self.assistant.needs_research(state, event['text']), text)
        delivered = []
        processed = drain(self.store, lambda uid, reply: delivered.append((uid, reply)),
                          self.assistant, max_events=1)
        self.assertEqual(processed, 1)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], user_id)
        self.assertIsNone(self.store.pending())
        self.last_seq = event['seq']
        self.assertLessEqual(len(delivered[0][1]), 3900)
        return delivered[0][1]

    def complete_profile(self, user_id=1, *, region='Казань', employees=0,
                         revenue='1200000.50', preferred='НПД', method='МФЦ'):
        for text in ('/consent', '3', ACTIVITY, 'Да, подходит', preferred, region,
                     revenue, '200000', str(employees), 'Физлица'):
            self.send(text, user_id)
        if preferred == 'НПД':
            # Explicit eligibility and deduction answers are part of the persisted inputs.
            for text in ('да', 'да', '5000'):
                self.send(text, user_id)
        else:
            self.send('показать расчёт', user_id)
        for text in (preferred, 'РФ', '31', method):
            self.send(text, user_id)
        state = self.store.load_profile(user_id)
        self.assertEqual(state['guide']['stage'], 'after_checklist')
        self.assertEqual(state['answers']['employees'], employees)
        self.assertEqual(state['answers']['region'], region)
        self.assertEqual(state['okved']['primary']['code'], '62.01')
        self.assertEqual([item['code'] for item in state['okved']['additional']], ['62.02'])
        self.assertTrue(state['consent']['accepted'])
        return state

    def npd_row(self, user_id=1):
        comparison = self.store.load_profile(user_id)['tax_comparison']
        return next(row for row in comparison['rows'] if row['id'] == 'npd')

    def test_all_profile_sections_survive_new_store_and_assistant(self):
        user_id = 2**40
        state = self.complete_profile(user_id)
        expected = {section: copy.deepcopy(state[section]) for section in PROFILE_SECTIONS}
        self.assertEqual(expected['tax_options']['npd_deduction_remaining'], '5000')
        self.assertTrue(expected['tax_options']['npd_activity_eligible'])
        self.assertEqual(expected['registration'], {'citizenship': 'ru', 'age': 31, 'method': 'mfc'})
        self.assertEqual(expected['selected_regime'], 'npd')
        self.assertEqual(expected['answers']['revenue'], '1200000.50')

        self.reopen()
        restored = self.store.load_profile(user_id)
        self.assertEqual({key: restored[key] for key in PROFILE_SECTIONS}, expected)
        self.assertEqual(restored['guide'], state['guide'])
        answer = self.send('/profile', user_id)
        for text in ('Разработка сайтов', 'Казань', 'НПД', 'МФЦ'):
            self.assertIn(text, answer)
        self.assertIn('1200000.50', answer.replace(' ', '').replace('\xa0', '').replace(',', '.'))
        self.assertEqual({key: self.store.load_profile(user_id)[key] for key in PROFILE_SECTIONS}, expected)
        self.send(PERSONAL_QUESTION, user_id)
        self.assertEqual(Decimal(self.npd_row(user_id)['total']), Decimal('43000.02'))
        self.send('/set npd_deduction_remaining 0', user_id)
        self.send(PERSONAL_QUESTION, user_id)
        self.assertEqual(Decimal(self.npd_row(user_id)['total']), Decimal('48000.02'))
        self.assertEqual(self.store.load_profile(user_id)['tax_options']['npd_deduction_remaining'], '0')

    def test_personal_calculation_uses_latest_set_values_after_restart(self):
        original = self.complete_profile()
        self.send(PERSONAL_QUESTION)
        self.assertNotEqual(self.npd_row()['status'], 'excluded')
        self.send('/set сотрудники 2')
        self.send('/set доход 5000000')
        changed = self.store.load_profile(1)
        self.assertEqual(changed['answers']['employees'], 2)
        self.assertEqual(Decimal(changed['answers']['revenue']), Decimal('5000000'))
        self.assertNotIn('selected_regime', changed)
        self.assertEqual(changed['registration'], original['registration'])
        self.assertEqual(changed['okved'], original['okved'])
        self.reopen()

        answer = self.send(PERSONAL_QUESTION)
        self.assertEqual(self.npd_row()['status'], 'excluded')
        self.assertIn('НПД', answer)
        self.assertIn('исключ', answer.casefold())
        state = self.store.load_profile(1)
        self.assertEqual(state['answers']['region'], 'Казань')
        self.assertEqual(state['answers']['employees'], 2)
        profile = self.send('/profile')
        self.assertIn('Работники: 2', profile)
        self.assertIn('5000000', profile.replace(' ', '').replace('\xa0', ''))

        # A fresh answer must use the correction, not the last comparison/history.
        self.send('/set сотрудники 0')
        self.send('/set доход 900000')
        self.send(PERSONAL_QUESTION)
        self.assertNotEqual(self.npd_row()['status'], 'excluded')
        self.assertEqual(self.store.load_profile(1)['answers']['employees'], 0)
        self.assertEqual(Decimal(self.store.load_profile(1)['answers']['revenue']), Decimal('900000'))

    def test_two_users_keep_different_profiles_and_recommendations_after_restart(self):
        first = self.complete_profile(1)
        second = self.complete_profile(2, region='Самарская область', employees=2,
                                       revenue='3000000', preferred='УСН Доходы', method='онлайн')
        self.reopen()
        second_answer = self.send(PERSONAL_QUESTION, 2)
        self.assertEqual(self.npd_row(2)['status'], 'excluded')
        self.assertIn('исключ', second_answer.casefold())
        self.assertEqual(self.store.load_profile(1), first)
        self.send(PERSONAL_QUESTION, 1)
        self.assertNotEqual(self.npd_row(1)['status'], 'excluded')
        for user_id, expected, absent in ((1, first, 'Самарская область'), (2, second, 'Казань')):
            with self.subTest(user_id=user_id):
                profile = self.send('/profile', user_id)
                self.assertIn(expected['answers']['region'], profile)
                self.assertNotIn(absent, profile)
                state = self.store.load_profile(user_id)
                self.assertEqual(state['answers'], expected['answers'])
                self.assertEqual(state['selected_regime'], expected['selected_regime'])
                self.assertEqual(state['registration'], expected['registration'])

    def test_delete_removes_profile_and_history_before_new_personal_question(self):
        self.complete_profile(1)
        unaffected = self.complete_profile(2, region='Самарская область')
        self.send('/profile', 1)
        # Seed one synthetic prior research turn: local calculations need no LLM or HTTP.
        self.store.db.execute('''INSERT INTO conversation_turns(seq, user_id, question, answer)
            VALUES (%s, 1, %s, %s)''',
            (self.last_seq, 'Старый контекст: Казань, доход 9000000, работников 7',
             'Сохранённый исторический ответ о прежних условиях бизнеса.'))
        self.assertEqual(len(self.store.history(1)), 1)
        self.send('/delete', 1)
        self.assertIsNone(self.store.load_profile(1))
        self.assertEqual(self.store.history(1), [])
        self.assertEqual(self.store.load_profile(2), unaffected)
        rows = self.store.db.execute('SELECT text, reply, reply_files FROM events WHERE user_id=1').fetchall()
        self.assertTrue(all(row['text'] == row['reply'] == '' and row['reply_files'] == [] for row in rows))
        self.reopen()

        answer = self.send(PERSONAL_QUESTION, 1)
        self.assertIn('/consent', answer)
        self.assertIsNone(self.store.load_profile(1))
        self.send('/consent', 1)
        answer = self.send(PERSONAL_QUESTION, 1)
        new_state = self.store.load_profile(1)
        self.assertEqual(new_state.get('answers'), {})
        for section in ('tax_comparison', 'selected_regime', 'tax_options', 'registration', 'okved'):
            self.assertFalse(new_state.get(section), section)
        for stale in ('Казань', '9000000', 'работников 7'):
            self.assertNotIn(stale, answer)
        self.assertEqual(self.store.history(1), [])
        self.assertEqual(self.store.load_profile(2), unaffected)


if __name__ == '__main__':
    unittest.main()

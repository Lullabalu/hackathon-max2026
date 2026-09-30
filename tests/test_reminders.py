from datetime import date, datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from ip_bot.reminders import AUTO_PREFIX, _plan, _working_day_2026, sync_reminders
from ip_bot.services import Services
from ip_bot.storage import Store


NOW = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)
STATE = {'reminders_enabled': True, 'registration': {'date': '2026-09-15'},
         'selected_regime': 'usn_income', 'timezone': 'Europe/Moscow'}


class CalendarTests(unittest.TestCase):
    def test_verified_dates_and_timezone(self):
        plan = _plan(STATE, NOW)
        dates = {row['id']: row['date'] for row in plan['deadlines']}
        self.assertEqual(dates, {'registration_notice': '2026-10-15', 'fixed_2026': '2026-12-28',
                                'additional_2026': '2027-07-01', 'usn_q3': '2026-10-28',
                                'usn_declaration_2026': '2027-04-26'})
        self.assertEqual(len(plan['desired']), 10)
        october = [item for item in plan['desired'].values() if item['due_at'].date() == date(2026, 10, 15)]
        self.assertEqual(october[0]['due_at'].hour, 6)
        other = _plan({**STATE, 'timezone': 'Asia/Yekaterinburg'}, NOW)
        october = [item for item in other['desired'].values() if item['due_at'].date() == date(2026, 10, 15)]
        self.assertEqual(october[0]['due_at'].hour, 4)

    def test_weekends_holidays_and_year_boundary(self):
        for start, expected in ((date(2026, 1, 1), date(2026, 1, 12)),
                                (date(2026, 3, 7), date(2026, 3, 10)),
                                (date(2026, 5, 9), date(2026, 5, 12)),
                                (date(2026, 6, 12), date(2026, 6, 15)),
                                (date(2026, 11, 4), date(2026, 11, 5))):
            self.assertEqual(_working_day_2026(start), expected)
        self.assertIsNone(_working_day_2026(date(2026, 12, 31)))
        plan = _plan({**STATE, 'registration': {'date': '2026-12-10'}},
                     datetime(2026, 12, 10, tzinfo=timezone.utc))
        self.assertNotIn('registration_notice', {row['id'] for row in plan['deadlines']})
        self.assertTrue(any('30-дневный' in note for note in plan['notes']))

    def test_explicit_consent_and_valid_state_required(self):
        for patch_state in ({'reminders_enabled': False}, {'reminders_enabled': 'true'},
                            {'registration': {}}, {'registration': {'date': '2026-02-30'}},
                            {'registration': {'date': '2025-01-01'}},
                            {'registration': {'date': '2026-12-01'}},
                            {'selected_regime': 'unknown'}, {'selected_regime': {}}, {'timezone': 'No/Such_Zone'}):
            with self.subTest(patch_state=patch_state):
                self.assertEqual(_plan({**STATE, **patch_state}, NOW)['desired'], {})

    def test_no_old_periods_or_unsupported_future_years(self):
        later = _plan({**STATE, 'registration': {'date': '2026-10-01'}},
                      datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.assertNotIn('usn_q3', {row['id'] for row in later['deadlines']})
        self.assertFalse(any('2025' in row['text'] for row in later['desired'].values()))
        self.assertFalse(_plan(STATE, datetime(2027, 7, 2, tzinfo=timezone.utc))['desired'])
        self.assertTrue(any('Декларации' in note for note in later['notes']))

    def test_npd_ausn_and_patent_have_different_obligations(self):
        npd = _plan({**STATE, 'selected_regime': 'npd'}, NOW)
        self.assertTrue(all(row['id'].startswith('npd_check_') for row in npd['deadlines']))
        ausn = _plan({**STATE, 'selected_regime': 'ausn_income'}, NOW)
        self.assertEqual([row['id'] for row in ausn['deadlines']],
                         ['registration_notice', 'ausn_check_2026_09', 'ausn_check_2026_10', 'ausn_check_2026_11'])
        patent = _plan({**STATE, 'selected_regime': 'psn'}, NOW)
        self.assertEqual({row['id'] for row in patent['deadlines']}, {'fixed_2026', 'additional_2026'})

    def test_monthly_npd_and_ausn_dates_roll_forward_and_are_conditional(self):
        for regime, expected in (
            ('npd', ['2026-10-28', '2026-11-30', '2026-12-28']),
            ('ausn_profit', ['2026-10-26', '2026-11-25', '2026-12-25']),
        ):
            plan = _plan({**STATE, 'selected_regime': regime}, NOW)
            monthly = [row for row in plan['deadlines'] if '_check_' in row['id']]
            self.assertEqual([row['date'] for row in monthly], expected)
            self.assertTrue(all('Если ФНС начислила' in row['label'] for row in monthly))
            self.assertTrue(all('при отсутствии начисления' in row['label'] for row in monthly))

    def test_monthly_checks_start_with_registration_and_stop_before_2027(self):
        for regime in ('npd', 'ausn_income'):
            late = _plan({**STATE, 'selected_regime': regime, 'registration': {'date': '2026-11-15'}},
                         datetime(2026, 11, 15, tzinfo=timezone.utc))
            monthly = [row for row in late['deadlines'] if '_check_' in row['id']]
            self.assertEqual(len(monthly), 1)
            self.assertTrue(monthly[0]['id'].endswith('_11'))
            self.assertTrue(monthly[0]['date'].startswith('2026-12'))
            december = _plan({**STATE, 'selected_regime': regime, 'registration': {'date': '2026-12-15'}},
                             datetime(2026, 12, 15, tzinfo=timezone.utc))
            self.assertFalse(any('_check_' in row['id'] for row in december['deadlines']))
        self.assertTrue(any('Первый налоговый период' in note for note in
                            _plan({**STATE, 'selected_regime': 'npd'}, NOW)['notes']))

    def test_usn_2026_declaration_has_verified_2027_date_and_source(self):
        for regime in ('usn_income', 'usn_profit'):
            plan = _plan({**STATE, 'selected_regime': regime}, NOW)
            declaration = next(row for row in plan['deadlines'] if row['id'] == 'usn_declaration_2026')
            self.assertEqual(declaration['date'], '2027-04-26')
            self.assertEqual(declaration['source'], 'https://www.nalog.gov.ru/rn19/news/activities_fts/16641463/')
            days = {item['due_at'].date() for key, item in plan['desired'].items() if 'usn_declaration_2026' in key}
            self.assertEqual(days, {date(2027, 4, 23), date(2027, 4, 26)})


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Нужен TEST_DATABASE_URL с отдельной тестовой базой PostgreSQL')
class ReminderSyncTests(unittest.TestCase):
    def setUp(self):
        self.admin = psycopg.connect(os.environ['TEST_DATABASE_URL'], autocommit=True)
        self.schema = 'test_reminders_' + uuid.uuid4().hex
        self.admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        self.store = None
        self.addCleanup(self.cleanup)
        self.dsn = make_conninfo(os.environ['TEST_DATABASE_URL'], options=f'-c search_path={self.schema}')
        self.store = Store(self.dsn, worker=False)
        self.service = Services(self.store)

    def cleanup(self):
        if self.store:
            self.store.close()
        try:
            self.admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        finally:
            self.admin.close()

    def test_repeat_disable_reenable_and_manual_preservation(self):
        manual = self.service.create_reminder(1, 'Моя запись', NOW + timedelta(days=7), 'manual:test')
        one = sync_reminders(self.service, 1, STATE, now=NOW)
        self.assertEqual(one['created'], 10)
        self.assertEqual(sync_reminders(self.service, 1, STATE, now=NOW)['created'], 0)
        self.assertEqual(len(self.service.list_reminders(1)), 11)
        off = sync_reminders(self.service, 1, {**STATE, 'reminders_enabled': False}, now=NOW)
        self.assertEqual(off['cancelled'], 10)
        self.assertEqual([r['id'] for r in self.service.list_reminders(1)], [manual['id']])
        self.assertEqual(sync_reminders(self.service, 1, STATE, now=NOW)['created'], 10)
        self.assertEqual(len(self.service.list_reminders(1)), 11)

    def test_state_changes_cancel_only_own_auto_reminders(self):
        sync_reminders(self.service, 1, STATE, now=NOW)
        sync_reminders(self.service, 2, STATE, now=NOW)
        manual = self.service.create_reminder(1, 'Моя запись', NOW + timedelta(days=7), 'manual:test')
        changed = sync_reminders(self.service, 1, {**STATE, 'selected_regime': None}, now=NOW)
        self.assertEqual(changed['cancelled'], 10)
        self.assertEqual([r['id'] for r in self.service.list_reminders(1)], [manual['id']])
        self.assertEqual(len(self.service.list_reminders(2)), 10)
        sync_reminders(self.service, 1, STATE, now=NOW)
        changed = sync_reminders(self.service, 1, {**STATE, 'timezone': 'Asia/Yekaterinburg'}, now=NOW)
        self.assertEqual((changed['created'], changed['cancelled']), (10, 10))

    def test_sent_and_manually_cancelled_auto_reminders_do_not_reappear(self):
        sync_reminders(self.service, 1, STATE, now=NOW)
        rows = self.service.list_reminders(1)
        self.service.cancel_reminder(1, rows[0]['id'])
        outbox = self.store.db.execute('SELECT id FROM service_outbox WHERE reminder_id = %s', (rows[1]['id'],)).fetchone()
        self.service.mark(outbox['id'])
        self.assertEqual(sync_reminders(self.service, 1, STATE, now=NOW)['created'], 0)
        self.assertEqual(len(self.service.list_reminders(1)), 8)

    def test_repeated_profile_keeps_already_due_delivery(self):
        sync_reminders(self.service, 1, STATE, now=NOW)
        pending = self.service.list_reminders(1)
        later = pending[0]['due_at'] + timedelta(minutes=1)
        result = sync_reminders(self.service, 1, STATE, now=later)
        self.assertEqual(result['cancelled'], 0)
        self.assertEqual(self.service.list_reminders(1), pending)

    def test_reconciliation_is_atomic(self):
        sync_reminders(self.service, 1, STATE, now=NOW)
        before = self.service.list_reminders(1)
        with patch.object(self.service, 'create_reminder', side_effect=RuntimeError('database unavailable')):
            with self.assertRaises(RuntimeError):
                sync_reminders(self.service, 1, {**STATE, 'timezone': 'Asia/Yekaterinburg'}, now=NOW)
        self.assertEqual(self.service.list_reminders(1), before)


if __name__ == '__main__':
    unittest.main()

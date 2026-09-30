from datetime import date
import json
from pathlib import Path
import unittest

from ip_bot.regions import ausn_region_status, resolve_region


class RegionTests(unittest.TestCase):
    def test_snapshot_has_primary_source_version_and_all_published_participants(self):
        data = json.loads((Path(__file__).resolve().parents[1] / 'config/ausn_regions.json').read_text())
        self.assertEqual(data['verified_on'], '2026-09-30')
        self.assertEqual(data['supported_years'], [2026])
        self.assertTrue(data['version'])
        self.assertEqual(data['sources'], ['https://ausn.nalog.gov.ru/'])
        self.assertEqual(len(data['regions']), 90)
        self.assertEqual(sum(row['status'] == 'participating' for row in data['regions']), 89)
        self.assertEqual([row['code'] for row in data['regions'] if row['status'] == 'not_listed'], ['52'])
        for row in data['regions']:
            self.assertEqual(resolve_region(row['name'])['code'], row['code'])
            self.assertEqual(resolve_region(row['code'])['name'], row['name'])
            for alias in row['aliases']:
                self.assertEqual(resolve_region(alias)['code'], row['code'])

    def test_codes_names_and_curated_aliases(self):
        for value, expected in (('  г. Москва ', '77'), ('МОСКВА', '77'), (77, '77'),
                                ('код региона 77', '77'), ('77 регион', '77'), ('RU-77', '77'),
                                ('МСК', '77'), ('Московская обл.', '50'), ('МО', '50'),
                                ('СПб', '78'), ('г. Санкт-Петербург', '78'),
                                ('респ. Татарстан', '16'), ('ХМАО — Югра', '86'),
                                ('ЯНАО', '89'), ('Орёл', '57'), ('Кузбасс', '42'),
                                (2, '02'), ('Сириус', '23'), ('Байконур', '99')):
            with self.subTest(value=value):
                self.assertEqual(resolve_region(value)['code'], expected)
                self.assertTrue(ausn_region_status(value)['available'])

    def test_unknown_and_ambiguous_names_do_not_silently_exclude(self):
        for value in (None, '', ' ', {}, [], True, '0', '999', 'Алтай', 'Новгород', 'Ростов',
                      'Московская', 'не знаю', 'мос', 'Самара или Москва', 'Москва; 52'):
            with self.subTest(value=value):
                self.assertIsNone(resolve_region(value))
                result = ausn_region_status(value)
                self.assertIsNone(result['available'])
                self.assertEqual(result['status'], 'unknown')
                self.assertIsNone(result['code'])

    def test_recognised_absence_is_explicit_and_snapshot_limited(self):
        for value in ('52', 'Нижегородская область', 'Нижний Новгород', 'НН', '152'):
            result = ausn_region_status(value)
            self.assertFalse(result['available'])
            self.assertEqual(result['status'], 'unavailable')
            self.assertEqual(result['code'], '52')
            self.assertIsNone(result['starts_on'])
            self.assertIn('не указана', result['reason'])
            self.assertEqual(result['sources'], ['https://ausn.nalog.gov.ru/'])
        self.assertIsNone(ausn_region_status('52', on='2026-01-01')['available'])

    def test_start_dates_are_not_retroactively_applied(self):
        for region in ('Ивановская область', 'Томская область', 'Ярославская область', 'Чукотский автономный округ'):
            self.assertFalse(ausn_region_status(region, on='2026-01-31')['available'])
            self.assertTrue(ausn_region_status(region, on='2026-02-01')['available'])
        self.assertFalse(ausn_region_status('ЯНАО', on=date(2026, 3, 31))['available'])
        self.assertTrue(ausn_region_status('ЯНАО', on=date(2026, 4, 1))['available'])
        self.assertTrue(ausn_region_status('63', on='2026-01-01')['available'])
        self.assertEqual(ausn_region_status('77')['starts_on'], '2022-07-01')

    def test_unverified_dates_are_unknown_and_not_wall_clock_guesses(self):
        self.assertEqual(ausn_region_status('77')['checked_on'], '2026-09-30')
        for on in ('2026-10-01', '2027-01-01', '2025-12-31', '2026-02-30', 'bad', 2026, {}):
            with self.subTest(on=on):
                result = ausn_region_status('77', on=on)
                self.assertIsNone(result['available'])
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['name'], 'Москва')

    def test_caller_cannot_mutate_the_registry(self):
        result = resolve_region('77')
        result['name'] = 'Изменено'
        result['aliases'].clear()
        self.assertEqual(resolve_region('МСК')['name'], 'Москва')
        status = ausn_region_status('77')
        status['sources'].clear()
        self.assertEqual(ausn_region_status('77')['sources'], ['https://ausn.nalog.gov.ru/'])


if __name__ == '__main__':
    unittest.main()

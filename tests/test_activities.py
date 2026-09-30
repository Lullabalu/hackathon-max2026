import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from ip_bot.activities import CATALOG_FILE, format_activity, suggest_activity


class ActivitiesTests(unittest.TestCase):
    def test_spec_examples_and_common_services(self):
        for description, code in [
            ('Хочу делать торты на заказ', '10.71.2'),
            ('Пеку торты дома на заказ', '10.71.2'),
            ('Открою шиномонтаж', '45.20'),
            ('Буду сдавать квартиру', '68.20.1'),
            ('Разработка сайтов', '62.01'),
            ('Пишу приложения на заказ', '62.01'),
            ('IT-консультирование', '62.02'),
            ('Ремонт компьютеров и ноутбуков', '95.11'),
            ('Уборка квартир и офисов', '81.21'),
            ('Открою парикмахерскую', '96.02'),
            ('Маникюр', '96.02'),
        ]:
            with self.subTest(description=description):
                result = suggest_activity(description)
                self.assertEqual(result['status'], 'matched')
                self.assertEqual(result['primary']['code'], code)
                self.assertEqual(result['additional'], [])
                self.assertTrue(result['sources'])

    def test_cakes_require_manufacture_or_resale_and_channel(self):
        for description in ['Торты на заказ', 'Продажа тортов', 'Хочу перепродавать готовые торты']:
            with self.subTest(description=description):
                result = suggest_activity(description)
                self.assertEqual(result['status'], 'clarify')
                self.assertIsNone(result['primary'])
                self.assertIn('сами печёте', result['question'])
        result = suggest_activity('Магазин: перепродаю готовые торты')
        self.assertEqual(result['primary']['code'], '47.24.2')
        result = suggest_activity('Перепродаю покупные торты через Интернет')
        self.assertEqual(result['primary']['code'], '47.91.2')
        result = suggest_activity('Пеку собственные торты и продаю их через интернет')
        self.assertEqual(result['primary']['code'], '10.71.2')
        self.assertEqual(result['additional'], [])

    def test_additional_codes_are_backed_by_explicit_separate_work(self):
        result = suggest_activity('Разработка сайтов, дополнительно IT-консультации')
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['primary']['code'], '62.01')
        self.assertEqual([item['code'] for item in result['additional']], ['62.02'])
        result = suggest_activity('Шиномонтаж, также продаю автомобильные шины')
        self.assertEqual(result['primary']['code'], '45.20')
        self.assertEqual([item['code'] for item in result['additional']], ['45.32'])
        result = suggest_activity('Разработка сайтов, дополнительно доставка цветов')
        self.assertEqual(result['additional'], [])
        self.assertTrue(any('не распознана' in warning for warning in result['warnings']))

    def test_main_direction_is_not_guessed_from_multiple_businesses(self):
        for description in ['Разработка сайтов и IT-консультации', 'Пеку торты и сдаю квартиру',
                            'Шиномонтаж и продажа шин']:
            with self.subTest(description=description):
                result = suggest_activity(description)
                self.assertEqual(result['status'], 'clarify')
                self.assertIsNone(result['primary'])
                self.assertIn('основным', result['question'])
        result = suggest_activity('Пеку торты и перепродаю чужие торты в магазине')
        self.assertEqual(result['status'], 'clarify')

    def test_restricted_alcohol_is_not_a_blanket_ban(self):
        for description in ['Магазин водки', 'Хочу продавать коньяк', 'Торговля крепким алкоголем']:
            with self.subTest(description=description):
                self.assertEqual(suggest_activity(description)['status'], 'restricted')
        for description in ['Продажа пива', 'Винный магазин', 'Не продаю водку, пеку торты']:
            with self.subTest(description=description):
                self.assertNotEqual(suggest_activity(description)['status'], 'restricted')

    def test_regulated_or_distinct_services_are_not_misclassified(self):
        for description in ['Инъекции ботокса в салоне красоты', 'Дезинфекция квартир',
                            'Буду сдавать квартиру посуточно', 'Риелтор по аренде квартир',
                            'IT услуги', 'Услуги', 'Пеку торты или открою магазин',
                            'Арендую квартиру', 'Аренда квартир', 'Сдаю квартиру и офис']:
            with self.subTest(description=description):
                self.assertEqual(suggest_activity(description)['status'], 'clarify')
        self.assertEqual(suggest_activity('Восстановление протекторов шин')['status'], 'unknown')

    def test_software_client_industry_is_not_own_trade_or_medical_work(self):
        for description in ['Разработка сайтов для магазина шин',
                            'Разработка программ для магазина водки',
                            'Разработка сайтов для медицинских клиник']:
            with self.subTest(description=description):
                result = suggest_activity(description)
                self.assertEqual(result['status'], 'matched')
                self.assertEqual(result['primary']['code'], '62.01')
                self.assertEqual(result['additional'], [])

    def test_unknown_and_invalid_input_does_not_invent_a_code(self):
        for description in ['Запуск космических аппаратов', 'Кто ты?', '', None, 'x' * 2001]:
            with self.subTest(description=description):
                result = suggest_activity(description)
                self.assertIsNone(result['primary'])
                self.assertEqual(result['additional'], [])
                self.assertIn(result['status'], {'unknown', 'clarify'})
                self.assertIn('https://service.nalog.ru/okved/', format_activity(result))

    def test_config_contains_verification_and_only_official_source_domains(self):
        catalog = json.loads(CATALOG_FILE.read_text())
        self.assertRegex(catalog['checked_on'], r'^\d{4}-\d{2}-\d{2}$')
        for source in catalog['sources'].values():
            url = urlsplit(source['url'])
            self.assertEqual(url.scheme, 'https')
            self.assertTrue(any(url.hostname == domain or url.hostname.endswith('.' + domain)
                                for domain in ['nalog.gov.ru', 'nalog.ru', 'rosstat.gov.ru', 'rospotrebnadzor.ru']))
        for entry in catalog['activities'].values():
            self.assertRegex(entry['code'], r'^\d{2}\.\d{2}(?:\.\d{1,2})?$')
            self.assertTrue(entry['sources'])
            self.assertTrue(all(key in catalog['sources'] for key in entry['sources']))

    def test_formatted_reply_is_bounded_and_explains_limited_coverage(self):
        for description in ['Пеку торты', 'Шиномонтаж', 'Продажа водки', 'Торты на заказ',
                            'Разработка сайтов, дополнительно IT-консультации и ремонт компьютеров']:
            with self.subTest(description=description):
                reply = format_activity(suggest_activity(description))
                self.assertLess(len(reply), 3500)
                self.assertIn('не полный классификатор', reply)
                self.assertIn('https://service.nalog.ru/okved/', reply)
                self.assertNotIn('100 сотрудников', reply)

    def test_callers_cannot_modify_other_users_results(self):
        first = suggest_activity('Шиномонтаж')
        first['primary']['code'] = 'fake'
        first['sources'].clear()
        second = suggest_activity('Шиномонтаж')
        self.assertEqual(second['primary']['code'], '45.20')
        self.assertTrue(second['sources'])


if __name__ == '__main__':
    unittest.main()

import hashlib
import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from ip_bot.forms import R21001Error, fill_r21001, r21001_support


ROOT = Path(__file__).resolve().parent.parent


def synthetic_state():
    return {'form': {'surname': 'Тестов-Ёлкин', 'name': 'Семён', 'patronymic': 'Тестович',
                     'inn': '000000000000', 'email': 'Test.User@example.org'},
            'registration': {'citizenship': 'ru', 'age': 30},
            'okved': {'primary': {'code': '10.71.2'},
                      'additional': [{'code': '47.24.2'}, {'code': '62.01'}]}}


def compact_text(page):
    return re.sub(r'\s+', '', page.extract_text())


class FormsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads((ROOT / 'config/forms/r21001-2024.json').read_text())
        cls.template = PdfReader(ROOT / 'config/forms/r21001-2024.pdf')

    def test_pdf_contains_provided_fields_on_correct_official_pages(self):
        pdf = fill_r21001(synthetic_state())
        self.assertTrue(pdf.startswith(b'%PDF-'))
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 5)
        page_one = compact_text(reader.pages[0])
        self.assertTrue(page_one.startswith('ТЕСТОВ-ЁЛКИНСЕМЁНТЕСТОВИЧ0000000000001'))
        self.assertTrue(compact_text(reader.pages[3]).startswith('10712472426201004'))
        self.assertTrue(compact_text(reader.pages[4]).startswith('Test.User@example.org005'))
        for index, page in enumerate(reader.pages):
            self.assertIn('НЕ ПОДПИСАНО И НЕ ПОДАНО В ФНС', page.extract_text())
            self.assertIn('Не готово для подачи', page.extract_text())
            self.assertEqual(list(page.mediabox), list(self.template.pages[index].mediabox))
            self.assertEqual(page.images[0].data, self.template.pages[index].images[0].data)
        self.assertEqual(reader.metadata['/R21001Revision'], '2024-01-09')
        self.assertNotIn('Test.User', str(reader.metadata))
        self.assertNotIn('ТЕСТОВ', str(reader.metadata))

    def test_does_not_fill_passport_address_registry_email_or_signatures(self):
        state = synthetic_state()
        state['form'].update({'passport': '4010999999', 'address': 'Ненастоящая улица',
                              'birth_date': '1996-01-01', 'phone': '+70000000000'})
        reader = PdfReader(io.BytesIO(fill_r21001(state)))
        for index in (1, 2):
            text = reader.pages[index].extract_text()
            self.assertNotIn('ТЕСТОВ', text)
            self.assertNotIn('1996', text)
            self.assertNotIn('4010', text)
            self.assertNotIn('Test.User', compact_text(reader.pages[index]))
        self.assertNotIn('ТЕСТОВ', compact_text(reader.pages[4]))

    def test_empty_optional_values_remain_blank(self):
        reader = PdfReader(io.BytesIO(fill_r21001({})))
        self.assertTrue(reader.pages[0].extract_text().startswith('ЧЕРНОВИК'))
        self.assertEqual(r21001_support({})['filled_fields'], [])
        state = synthetic_state()
        state['form']['patronymic'] = None
        reader = PdfReader(io.BytesIO(fill_r21001(state)))
        self.assertNotIn('ТЕСТОВИЧ', compact_text(reader.pages[0]))

    def test_foreign_and_minor_are_only_common_field_drafts(self):
        state = synthetic_state()
        state['registration'] = {'citizenship': 'foreign', 'age': 17}
        support = r21001_support(state)
        self.assertTrue(support['supported'])
        self.assertEqual(support['mode'], 'partial_draft')
        self.assertTrue(any('латинская' in warning for warning in support['warnings']))
        self.assertTrue(any('несовершеннолетнего' in warning for warning in support['warnings']))
        reader = PdfReader(io.BytesIO(fill_r21001(state)))
        self.assertTrue(compact_text(reader.pages[0]).startswith('ТЕСТОВ-ЁЛКИНСЕМЁНТЕСТОВИЧ0000000000002'))
        self.assertNotIn('2009', reader.pages[0].extract_text())

    def test_rejects_unsupported_data_without_truncation_or_guessing(self):
        cases = [('surname', 'Я' * 35), ('name', 'John'), ('inn', '1234567890'),
                 ('inn', 123456789012), ('email', 'a' * 27 + '@example.org'),
                 ('email', 'почта@example.org'), ('email', 'missing-at.example.org')]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                state = synthetic_state()
                state['form'][key] = value
                with self.assertRaises(R21001Error):
                    fill_r21001(state)
                support = r21001_support(state)
                self.assertFalse(support['supported'])
                self.assertTrue(support['reason'])
        for state in ({'registration': {'citizenship': 'unknown'}}, {'form': []}, []):
            with self.assertRaises(R21001Error):
                fill_r21001(state)

    def test_stateless_is_distinct_from_foreign_and_has_no_guessed_country(self):
        state = synthetic_state()
        state['registration']['citizenship'] = 'stateless'
        support = r21001_support(state)
        self.assertTrue(support['supported'])
        self.assertTrue(any('лица без гражданства' in warning for warning in support['warnings']))
        self.assertFalse(any('код страны' in warning for warning in support['warnings']))
        reader = PdfReader(io.BytesIO(fill_r21001(state)))
        self.assertTrue(compact_text(reader.pages[0]).startswith('ТЕСТОВ-ЁЛКИНСЕМЁНТЕСТОВИЧ0000000000003'))

    def test_accepts_exact_cell_capacity(self):
        state = synthetic_state()
        state['form']['surname'] = 'Я' * 34
        state['form']['email'] = 'a' * 26 + '@example.org'
        self.assertEqual(len(state['form']['email']), 38)
        reader = PdfReader(io.BytesIO(fill_r21001(state)))
        self.assertTrue(compact_text(reader.pages[0]).startswith('Я' * 34))
        self.assertTrue(compact_text(reader.pages[4]).startswith(state['form']['email']))

    def test_okved_requires_main_and_four_to_six_digits_without_padding(self):
        for okved in ({'primary': {'code': '62'}}, {'primary': {'code': '62.0'}},
                      {'primary': {'code': '62.01.111'}}, {'additional': ['62.01']}):
            with self.subTest(okved=okved), self.assertRaises(R21001Error):
                fill_r21001({'okved': okved})
        state = {'okved': {'primary': {'code': '62.01'},
                           'additional': ['62.01', {'code': '62.02'}, {'code': '62.02'}, '01.11.11']}}
        reader = PdfReader(io.BytesIO(fill_r21001(state)))
        self.assertTrue(compact_text(reader.pages[3]).startswith('62016202011111004'))

    def test_all_68_additional_slots_and_overflow(self):
        state = {'okved': {'primary': {'code': '62.01'},
                           'additional': [f'10.{number:02}' for number in range(68)]}}
        reader = PdfReader(io.BytesIO(fill_r21001(state)))
        codes = ''.join(code.replace('.', '') for code in state['okved']['additional'])
        self.assertTrue(compact_text(reader.pages[3]).startswith('6201' + codes + '004'))
        state['okved']['additional'].append('10.68')
        with self.assertRaisesRegex(R21001Error, '68'):
            fill_r21001(state)

    def test_official_source_and_schema_match_bundled_template(self):
        self.assertEqual(self.schema['source_pages'], [4, 5, 6, 7, 8])
        self.assertEqual(self.schema['revision'], '2024-01-09')
        self.assertTrue(self.schema['source_pdf'].startswith('https://www.nalog.gov.ru/'))
        content = (ROOT / 'config/forms/r21001-2024.pdf').read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), self.schema['template_sha256'])
        self.assertEqual(len(self.template.pages), len(self.schema['pages']))
        for page, definition in zip(self.template.pages, self.schema['pages']):
            self.assertEqual(list(page.mediabox)[2:], definition['size_points'])
            self.assertEqual(list(page.images[0].image.size), definition['image_pixels'])
        for name, field in self.schema['fields'].items():
            if name == 'additional':
                continue
            width, height = self.schema['pages'][field['page']]['image_pixels']
            for offset in field.get('digit_offsets', range(field['cells'])):
                x = field['center'][0] + offset * field['advance'][0]
                y = field['center'][1] + offset * field['advance'][1]
                self.assertGreater(x, 0)
                self.assertLess(x, width)
                self.assertGreater(y, 0)
                self.assertLess(y, height)

    def test_actual_glyph_positions_land_in_reviewed_field_regions(self):
        reader = PdfReader(io.BytesIO(fill_r21001(synthetic_state())))
        # Fixed review regions in PDF points, independent of JSON coordinate calculation.
        regions = {0: [(100, 620, 300, 655), (110, 430, 290, 467), (42, 255, 63, 275)],
                   3: [(200, 665, 315, 694), (52, 585, 165, 635)],
                   4: [(40, 570, 365, 607)]}
        for index, boxes in regions.items():
            glyphs = []
            def visitor(text, cm, tm, font, size):
                if size == 12 and text.strip():
                    glyphs.append((text.strip(), tm[4], tm[5]))
            reader.pages[index].extract_text(visitor_text=visitor)
            for left, bottom, right, top in boxes:
                self.assertTrue(any(left <= x <= right and bottom <= y <= top for _, x, y in glyphs),
                                (index, boxes, glyphs))

    def test_altered_template_is_not_filled_using_stale_coordinates(self):
        schema = json.loads(json.dumps(self.schema))
        schema['template_sha256'] = '0' * 64
        with patch('ip_bot.forms._manifest', return_value=schema):
            with self.assertRaisesRegex(R21001Error, 'Шаблон'):
                fill_r21001(synthetic_state())

    def test_does_not_mutate_or_leak_another_applicants_fields(self):
        state = synthetic_state()
        original = json.loads(json.dumps(state))
        fill_r21001(state)
        self.assertEqual(state, original)
        clean = PdfReader(io.BytesIO(fill_r21001({})))
        for page in clean.pages:
            self.assertNotIn('ТЕСТОВ', compact_text(page))
            self.assertNotIn('Test.User', compact_text(page))


if __name__ == '__main__':
    unittest.main()

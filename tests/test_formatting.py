import io
import json
import unittest
from unittest.mock import Mock

from ip_bot.formatting import format_message
from ip_bot.max_api import MaxAPI, keyboard_for


class MessageFormattingTests(unittest.TestCase):
    def test_inline_lists_use_actual_lines_and_formatting_is_idempotent(self):
        for original, expected in [
            ('Документы: 1. Заявление 2. Паспорт 3. Копия',
             'Документы:\n1. Заявление\n2. Паспорт\n3. Копия'),
            ('1) Заполните форму. 2) Проверьте данные.',
             '1) Заполните форму.\n2) Проверьте данные.'),
            ('Учтено: • Казань • Работников нет',
             'Учтено:\n• Казань\n• Работников нет'),
        ]:
            with self.subTest(original=original):
                formatted = format_message(original)
                self.assertEqual(formatted, expected)
                self.assertEqual(format_message(formatted), formatted)

    def test_amounts_dates_codes_references_and_urls_are_not_lists(self):
        text = ('Доход 1 200 000,50 ₽; ставки 1.5% и 2.0%.\n'
                'Дата 01.01.2019; ОКВЭД 62.01 и 62.02; форма Р21001.\n'
                'См. п. 1. Условия регистрации описаны в п. 2.\n\n'
                'Источники:\n[1] https://example.test/1.2?q=2&x=1\n'
                '[2] https://example.test/form/21001')
        self.assertEqual(format_message(text), text)

    def test_existing_paragraphs_and_choice_button_labels_survive(self):
        text = ('Выберите вариант:\n1 — Первый вариант\n2 — Второй вариант\n\n'
                '/continue — продолжить\n/back — назад')
        self.assertEqual(format_message(text), text)
        self.assertEqual(keyboard_for(format_message(text)), keyboard_for(text))

    def test_max_json_transports_newlines_and_preserves_attachments(self):
        api = MaxAPI('synthetic-test-token')
        api.opener.open = Mock(return_value=io.BytesIO(b'{"success":true}'))
        attachment = {'type': 'file', 'payload': {'token': 'synthetic-file'}}
        api.send_message(1, 'Шаги: 1) Заполните форму. 2) Проверьте данные.', attachments=[attachment])
        payload = json.loads(api.opener.open.call_args.args[0].data)
        self.assertEqual(payload['text'], 'Шаги:\n1) Заполните форму.\n2) Проверьте данные.')
        self.assertEqual(payload['attachments'], [attachment])
        self.assertNotIn('\\n', payload['text'])


if __name__ == '__main__':
    unittest.main()

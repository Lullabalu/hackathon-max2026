import unittest

from ip_bot.conversation import ABOUT, GREETING, quick_reply, strip_social_prefix


class QuickConversationTests(unittest.TestCase):
    def test_introductions_and_capabilities(self):
        for text in (
            'Кто ты?', 'Ты кто такой?', 'Кто вы такие?', 'Расскажи о себе',
            'Расскажи мне немного о себе!', 'Что умеешь?', 'Что ты можешь делать?',
            'Чем можешь помочь?', 'Чем ты мне можешь помочь?', 'Как ты помогаешь?',
            'Как тебя зовут?', 'Назови своё имя', 'Как называется этот бот?',
            'Для чего ты нужен?', 'Зачем этот бот?', 'В чём твоё назначение?',
            'Какие твои возможности?', 'Что это за бот?', 'Ты человек?',
            'Что ты за бот?', 'Чем ты занимаешься?', 'Расскажи кто ты такой',
            'Кто ты такой и что умеешь?',
            'Ты бот или человек?', 'Ты бот?', 'О боте', '/about',
        ):
            with self.subTest(text=text):
                self.assertEqual(quick_reply(text), ABOUT)
        self.assertIn('MAX', ABOUT)
        self.assertIn('ФНС', ABOUT)
        self.assertIn('не человек', ABOUT)
        self.assertIn('заявления не отправляю', ABOUT)

    def test_normalization_addresses_and_greetings_before_identity(self):
        for text in (
            '  БРО, ТЫ КТО ТАКОЙ?!  ', 'Привет, кто ты?',
            'Добрый день! Расскажите о себе, пожалуйста.',
            'Бот, скажи, как тебя зовут?', 'А что ты умеешь, бро?',
            'Привет, бот 👋 Расскажи про себя!', 'Ну, кто ты вообще???',
        ):
            with self.subTest(text=text):
                self.assertEqual(quick_reply(text), ABOUT)

    def test_ask_command_supports_small_talk(self):
        for text in ('/ask кто ты?', '/ASK  Расскажи о себе', '/ask\n/about'):
            with self.subTest(text=text):
                self.assertEqual(quick_reply(text), ABOUT)
        self.assertEqual(quick_reply('/ask привет'), GREETING)

    def test_greetings(self):
        for text in ('привет', 'Привет, бро!', 'Здравствуйте.', 'Доброе утро ☀️', 'Йо!', 'Хай, бот'):
            with self.subTest(text=text):
                self.assertEqual(quick_reply(text), GREETING)

    def test_thanks_goodbye_and_how_are_you(self):
        for text, expected in (
            ('Спасибо!', 'Пожалуйста'), ('Спасибо тебе большое, бро!', 'Пожалуйста'),
            ('Благодарю за помощь', 'Пожалуйста'), ('Спс', 'Пожалуйста'),
            ('Пока!', 'До связи'), ('Спасибо, пока', 'До связи'),
            ('До свидания, бот', 'До связи'), ('Как дела?', 'Готов помогать'),
            ('Привет, как твои дела?', 'Готов помогать'),
        ):
            with self.subTest(text=text):
                self.assertIn(expected, quick_reply(text))

    def test_business_and_mixed_questions_are_never_intercepted(self):
        for text in (
            'Кто может открыть ИП?', 'Кто такой индивидуальный предприниматель?',
            'Что нужно для УСН?', 'Привет, какие документы нужны?',
            'Привет! Как тебя зовут и какие документы нужны для ИП?',
            'Спасибо, а что нужно для патента?', 'Как дела с моим заявлением?',
            'Расскажи о себе и регистрации ИП', 'Кто выдает патенты?',
            'Ты можешь помочь выбрать налоговый режим?',
            'Чем можешь помочь при закрытии ИП?', 'Что за документы?',
            'Кто ты? Какая госпошлина?', '/ask привет, какие документы нужны?',
            'Пока нет документов', 'Какое имя писать в заявлении?',
            'Что нового в налогах?', 'Мне нужно спасибо написать в заявлении?',
        ):
            with self.subTest(text=text):
                self.assertIsNone(quick_reply(text))

    def test_social_prefix_removal_preserves_business_constraints_and_commands(self):
        for text, expected in (
            ('Привет, как дела? Какие документы нужны для ИП в Казани в 2026 году?',
             'Какие документы нужны для ИП в Казани в 2026 году?'),
            ('Привет! Как дела с моим заявлением?', 'Как дела с моим заявлением?'),
            ('Спасибо, а что нужно для патента?', 'что нужно для патента?'),
            ('Привет! Я НЕ хочу открывать ИП, хочу закрыть.', 'Я НЕ хочу открывать ИП, хочу закрыть.'),
            ('Привет, /delete', 'Привет, /delete'),
            ('Бро, /regime npd', 'Бро, /regime npd'),
            ('/ask Привет, какие документы нужны?', '/ask Привет, какие документы нужны?'),
            ('Привет, https://example.test', 'https://example.test'),
        ):
            with self.subTest(text=text):
                self.assertEqual(strip_social_prefix(text), expected)

    def test_commands_and_empty_or_oversized_inputs_continue_normal_routing(self):
        for text in ('', '   ', '👋', '/ask', '/begin', '/delete', '/about extra', '/help', 'привет' * 400):
            with self.subTest(text=text):
                self.assertIsNone(quick_reply(text))


if __name__ == '__main__':
    unittest.main()

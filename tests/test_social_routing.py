"""Social messages stay local; a greeting must not swallow a business question."""

import copy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock

from ip_bot.assistant import Assistant
from ip_bot.conversation import quick_reply
from ip_bot.privacy import inbound_text
from ip_bot.llm import QuestionIntent
from ip_bot.settings import Settings
from ip_bot.summarization import Summary
from tests.test_assistant import first_sentence_summary


SOCIAL_MESSAGES = (
    'Привет, как дела?',
    'Привет, бро! Как у тебя дела? Что нового?',
    'Здравствуйте! Как вы? Что нового?',
    'Добрый день, как настроение?',
    'ПРИВЕТ 👋 КАК ДЕЛА?',
    'Ты тут?',
    'Бро, ты на связи?',
    'Привет! Ты тут, на связи?',
    'Давай поболтаем.',
    'Привет, давай поболтаем!',
    'Хай! Как дела? Что нового?',
    '/ask Привет! Как дела?',
)
PROFILE = {
    'answers': {'activity': 'Разработка сайтов', 'region': 'Москва',
                'revenue': '1200000', 'expenses': '200000',
                'employees': 0, 'clients': 'Физлица'},
    'consent': {'accepted': True}, 'paused': True,
}
PASSAGE = {
    'url': 'https://www.nalog.gov.ru/create_business/ip/creation/registration/step2/',
    'title': 'ФНС: документы для регистрации ИП',
    'text': 'Для регистрации ИП используется заявление по форме Р21001. Требования к документам проверьте в официальном сервисе ФНС.',
    'fetched_at': datetime(2026, 9, 30, tzinfo=timezone.utc),
}


class RecordingPlanner:
    """A local fake retains the incoming question if the fast router cannot plan it."""

    def __init__(self):
        self.questions = []

    def generate(self, schema, system, content, **kwargs):
        if schema is Summary:
            return first_sentence_summary(json.loads(content))
        if schema is not QuestionIntent:
            raise AssertionError('Нужен только разбор вопроса, без генерации ответа')
        question = content.split('CURRENT USER QUESTION:\n', 1)[1]
        self.questions.append(question)
        topic = 'taxes' if 'налог' in question.casefold() else 'registration'
        return QuestionIntent(action='search', text=question, topics=[topic])


class RecordingSources:
    def __init__(self, passage=None):
        self.plans = []
        self.passage = passage or PASSAGE

    def search(self, plan):
        self.plans.append(plan)
        return [copy.deepcopy(self.passage)], []


class SocialRoutingTests(unittest.TestCase):
    def local_assistant(self):
        model, researcher = Mock(), Mock()
        model.generate.side_effect = AssertionError('Разговорная реплика не вызывает модель')
        researcher.search.side_effect = AssertionError('Разговорная реплика не вызывает поиск')
        return Assistant(None, Settings(), model=model, researcher=researcher), model, researcher

    @staticmethod
    def region_state():
        return {
            'answers': {'activity': 'Разработка сайтов'},
            'consent': {'accepted': True}, 'paused': True,
            'guide': {'active': True, 'stage': 'profile', 'field': 'region', 'history': []},
        }

    @staticmethod
    def registration_state():
        return {
            **copy.deepcopy(PROFILE), 'selected_regime': 'npd',
            'registration': {'citizenship': 'ru'},
            'flow': {'kind': 'registration', 'index': 1},
            'guide': {'active': True, 'stage': 'registration', 'history': []},
        }

    def assert_short_social_reply(self, reply):
        self.assertTrue(reply.text.strip())
        self.assertLess(len(reply.text), 900)
        self.assertFalse(reply.remember)
        self.assertEqual(reply.files, [])
        for unexpected in ('Предварительное сравнение', 'Р21001', 'Выдержки из официальных',
                           'Источники:', 'Шаг 1 из', 'Чек-лист документов'):
            self.assertNotIn(unexpected, reply.text)

    def test_social_combinations_are_recognized_without_hiding_a_suffix(self):
        for text in SOCIAL_MESSAGES:
            with self.subTest(text=text):
                self.assertIsNotNone(quick_reply(text))
        for text in (
            'Привет, как дела? Какие документы нужны для регистрации ИП?',
            'Привет! Что нового в налогах в 2026 году?',
            'Ты тут? Как получить заявление Р21001?',
            'Давай поболтаем о документах для регистрации ИП.',
            'Как дела с регистрацией ИП?',
            'Что нового по УСН?',
            'Спасибо! Какие документы нужны для регистрации ИП?',
            'Привет, как дела? Меня зовут Иван Иванов.',
        ):
            with self.subTest(non_social=text):
                self.assertIsNone(quick_reply(text), 'Содержательная часть сообщения потеряна')

    def test_social_messages_before_consent_pass_privacy_gate_and_stay_local(self):
        for state in (None, {'answers': {}, 'paused': True}):
            for text in SOCIAL_MESSAGES:
                with self.subTest(state=state, text=text):
                    assistant, model, researcher = self.local_assistant()
                    services = Mock()
                    assistant.journey.services = services
                    original = copy.deepcopy(state)
                    safe = inbound_text(state, text)
                    self.assertEqual(safe, text)
                    self.assertFalse(assistant.needs_research(state, safe))
                    reply = assistant.respond(state, safe)
                    self.assertEqual(state, original)
                    self.assertEqual(reply.state, original)
                    self.assert_short_social_reply(reply)
                    model.generate.assert_not_called()
                    self.assertEqual(researcher.mock_calls, [])
                    self.assertEqual(services.mock_calls, [])

    def test_social_messages_preserve_all_survey_stages_without_repeating_documents(self):
        assistant, _, _ = self.local_assistant()
        comparison = assistant.journey.guide.offer_regimes(copy.deepcopy(PROFILE)).state
        walking = {**copy.deepcopy(PROFILE), 'registration_step': 0,
                   'guide': {'active': True, 'stage': 'walk', 'history': []}}
        paused = self.registration_state()
        paused['guide'].update(active=False, paused_flow=paused.pop('flow'))
        legacy = {'answers': {'activity': 'Разработка сайтов'}, 'consent': {'accepted': True}}
        states = (legacy, self.region_state(), self.registration_state(), comparison, walking, paused)
        for state in states:
            for text in SOCIAL_MESSAGES:
                with self.subTest(stage=state.get('guide', {}).get('stage', 'legacy'), text=text):
                    assistant, model, researcher = self.local_assistant()
                    original = copy.deepcopy(state)
                    self.assertEqual(inbound_text(state, text), text)
                    self.assertFalse(assistant.needs_research(state, text))
                    reply = assistant.respond(state, text)
                    self.assertEqual(state, original)
                    self.assertEqual(reply.state, original)
                    self.assert_short_social_reply(reply)
                    model.generate.assert_not_called()
                    self.assertEqual(researcher.mock_calls, [])

    def test_region_answer_after_smalltalk_goes_to_same_pending_question(self):
        assistant, model, researcher = self.local_assistant()
        state = self.region_state()
        reply = assistant.respond(state, 'Привет, как дела? Что нового?')
        self.assertEqual(reply.state, state)
        self.assertNotIn('region', reply.state['answers'])
        reply = assistant.respond(reply.state, 'Москва')
        self.assertEqual(reply.state['answers']['region'], 'Москва')
        self.assertEqual(reply.state['guide']['stage'], 'profile')
        self.assertEqual(reply.state['guide']['field'], 'revenue')
        model.generate.assert_not_called()
        self.assertEqual(researcher.mock_calls, [])

    def test_mixed_greeting_searches_business_part_and_preserves_pending_field(self):
        questions = (
            'Привет, как дела? Какие документы нужны для регистрации ИП?',
            'Привет, бро! Как дела? Что нового? Какие документы нужны для регистрации ИП в Казани в 2026 году?',
            'Ты тут? Какие документы нужны для регистрации ИП?',
            'Давай поболтаем. Какие документы нужны для регистрации ИП?',
            'Спасибо! Какие документы нужны для регистрации ИП?',
            'Привет, как дела? Покажи документы для регистрации ИП.',
            'Привет! Нужны документы для регистрации ИП.',
        )
        for state in (self.region_state(), self.registration_state()):
            for text in questions:
                with self.subTest(stage=state['guide']['stage'], text=text):
                    model, researcher = RecordingPlanner(), RecordingSources()
                    assistant = Assistant(None, Settings(), model=model, researcher=researcher)
                    original = copy.deepcopy(state)
                    self.assertIsNone(quick_reply(text))
                    self.assertEqual(inbound_text(state, text), text)
                    self.assertTrue(assistant.needs_research(state, text))
                    reply = assistant.respond(state, text)
                    self.assertEqual(state, original)
                    self.assertEqual(reply.state, original)
                    self.assertEqual(len(researcher.plans), 1)
                    query = researcher.plans[0].query.casefold()
                    self.assertIn('документ', query)
                    self.assertIn('регистрац', query)
                    self.assertIn('ип', query)
                    if 'Казани' in text:
                        self.assertIn('казани', query)
                        self.assertIn('2026', query)
                    self.assertIn('Р21001', reply.text)
                    self.assertIn(PASSAGE['url'], reply.text)
                    self.assertIn('/continue', reply.text)
                    self.assertTrue(reply.remember)
                    self.assertLessEqual(len(model.questions), 1)
                    self.assertLessEqual(len(reply.text), 3900)

    def test_mixed_personal_or_business_text_before_consent_is_not_admitted_as_social(self):
        for text in (
            'Привет, как дела? Какие документы нужны для регистрации ИП?',
            'Привет, как дела? Меня зовут Иван Иванов.',
            'Ты тут? Москва, доход 1200000, работников нет.',
        ):
            with self.subTest(text=text):
                assistant, model, researcher = self.local_assistant()
                assistant.journey.services = Mock()
                safe = inbound_text(None, text)
                self.assertEqual(safe, '/consent_required')
                reply = assistant.respond(None, safe)
                self.assertIsNone(reply.state)
                self.assertIn('/consent', reply.text)
                self.assertEqual(reply.files, [])
                model.generate.assert_not_called()
                self.assertEqual(researcher.mock_calls, [])

    def test_social_sounding_opening_with_business_object_is_still_a_question(self):
        cases = (
            ('Привет! Что нового в налогах в 2026 году?', ('налог', '2026')),
            ('Что нового по УСН?', ('усн',)),
            ('Привет! Как дела с регистрацией ИП?', ('регистрац', 'ип')),
        )
        passage = {**PASSAGE, 'url': 'https://www.nalog.gov.ru/new2026/',
                   'title': 'ФНС: изменения правил',
                   'text': 'Сведения об изменениях правил опубликованы на сайте ФНС.'}
        for text, required in cases:
            with self.subTest(text=text):
                state = self.region_state()
                original = copy.deepcopy(state)
                model, researcher = RecordingPlanner(), RecordingSources(passage)
                assistant = Assistant(None, Settings(), model=model, researcher=researcher)
                self.assertIsNone(quick_reply(text))
                self.assertTrue(assistant.needs_research(state, text))
                reply = assistant.respond(state, text)
                self.assertEqual(state, original)
                self.assertEqual(reply.state, original)
                self.assertEqual(len(researcher.plans), 1)
                for word in required:
                    self.assertIn(word, researcher.plans[0].query.casefold())
                self.assertIn(passage['url'], reply.text)
                self.assertLessEqual(len(model.questions), 1)


if __name__ == '__main__':
    unittest.main()

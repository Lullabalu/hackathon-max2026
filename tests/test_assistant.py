from datetime import datetime, timezone
import io
import json
import unittest

from ip_bot.assistant import Assistant, render_answer
from ip_bot.llm import Answer, ModelError, Ollama, Plan, QuestionIntent
from ip_bot.settings import Settings
from ip_bot.summarization import Summary, render_summary


PASSAGE = {'url': 'https://www.nalog.gov.ru/test/', 'title': 'Регистрация ИП',
           'text': 'Для регистрации ИП подайте заявление по форме Р21001 и копию паспорта гражданина.',
           'fetched_at': datetime(2026, 9, 29, tzinfo=timezone.utc)}
PLAN = Plan(query='Документы для регистрации ИП', topics=['registration'], keywords=['регистрация'], clarification='', relevant=True)
ANSWER = Answer(items=[1], clarification='')


def first_sentence_summary(payload):
    """Ground the fake answer in the evidence IDs actually supplied to the model."""
    source = payload['sources'][0]
    sentence = source['sentences'][0]
    return Summary(points=[{'text': sentence['text'], 'source': source['id'],
                            'evidence': sentence['id']}], clarification='')


class FakeModel:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.calls = []
        self.schemas = []
        self.summary_response = None

    def generate(self, schema, system, content, **kwargs):
        self.calls.append(json.JSONDecoder().raw_decode(content)[0])
        self.schemas.append(schema)
        if schema is Summary:
            if isinstance(self.summary_response, Exception):
                raise self.summary_response
            if self.summary_response is not None:
                return self.summary_response
            return first_sentence_summary(self.calls[-1])
        answer = next(self.answers, ModelError("model_unavailable"))
        if isinstance(answer, Exception):
            raise answer
        if schema is QuestionIntent and isinstance(answer, Plan):
            action = 'offtopic' if not answer.relevant else 'clarify' if answer.clarification else 'search'
            return QuestionIntent(action=action, text=answer.clarification or answer.query,
                                  topics=answer.topics)
        return answer


class FakeResearcher:
    def __init__(self, passages=None, errors=None):
        self.passages = [PASSAGE] if passages is None else passages
        self.errors = errors or []
        self.calls = 0

    def search(self, plan):
        self.calls += 1
        return self.passages, self.errors


class AssistantTests(unittest.TestCase):
    def assistant(self, model, researcher=None):
        return Assistant(None, Settings(), model=model, researcher=researcher or FakeResearcher())

    def test_question_to_verified_sources(self):
        model = FakeModel()
        reply = self.assistant(model).respond(None, 'Какие документы нужны для регистрации ИП?')
        self.assertIn('Р21001', reply.text)
        self.assertIn(PASSAGE['url'], reply.text)
        self.assertNotIn('Выдержки из официальных', reply.text)
        self.assertTrue(reply.remember)
        self.assertLessEqual(len(reply.text), 1500)
        self.assertEqual(model.schemas, [Summary])

    def test_side_question_preserves_guided_registration_and_returns_resume_link(self):
        assistant = self.assistant(FakeModel())
        state = {'answers': {}, 'guide': {'active': True, 'stage': 'registration', 'history': []},
                 'flow': {'kind': 'registration', 'index': 1}, 'registration': {'citizenship': 'ru'}}
        question = 'Какие документы нужны для регистрации ИП?'
        self.assertTrue(assistant.needs_research(state, question))
        reply = assistant.respond(state, question)
        self.assertEqual(reply.state, state)
        self.assertIn('Р21001', reply.text)
        self.assertIn('/continue', reply.text)
        self.assertEqual(assistant.model.schemas, [Summary])

    def test_long_valid_summary_in_active_guide_keeps_length_limit_without_resume_tail(self):
        sentences = (
            'Для регистрации ИП используются заявление по форме Р21001 и документы, перечисленные в официальной инструкции; требования к заполнению формы приведены на странице регистрации.',
            'При электронной подаче документов, в том числе через МФЦ и нотариуса, уплачивать государственную пошлину не требуется; условия электронного направления документов уточняются в инструкции.',
            'Перед отправкой заявления проверьте сведения о заявителе, выбранные виды деятельности и комплект документов по инструкции для соответствующего способа подачи заявления.',
        )
        passage = {**PASSAGE, 'url': PASSAGE['url'] + 'section/' * 110,
                   'text': ' '.join(sentences)}
        model = FakeModel()
        model.summary_response = Summary(points=[
            {'text': text, 'source': 1, 'evidence': index}
            for index, text in enumerate(sentences, 1)
        ], clarification='')
        expected = render_summary(model.summary_response, [passage])
        self.assertIsNotNone(expected)
        self.assertGreater(len(expected), 1470)
        self.assertLessEqual(len(expected), 1500)
        state = {'answers': {}, 'guide': {'active': True, 'stage': 'registration', 'history': []},
                 'flow': {'kind': 'registration', 'index': 1}, 'registration': {'citizenship': 'ru'}}

        reply = self.assistant(model, FakeResearcher([passage])).respond(
            state, 'Какие документы нужны для регистрации ИП?')

        self.assertEqual(reply.text, expected)
        self.assertLessEqual(len(reply.text), 1500)
        self.assertNotIn('/continue', reply.text)
        self.assertEqual(reply.state, state)
        self.assertTrue(reply.remember)
        self.assertEqual(model.schemas, [Summary])

    def test_faq_without_yo_does_not_become_region_answer(self):
        assistant = self.assistant(FakeModel())
        state = {'answers': {'activity': 'Разработка сайтов'}, 'guide': {'active': True, 'stage': 'profile', 'field': 'region', 'history': []}}
        reply = assistant.respond(state, 'Нужен ли расчетный счет?')
        self.assertEqual(reply.state, state)
        self.assertNotIn('region', reply.state['answers'])
        self.assertIn('бан', reply.text.lower())
        self.assertIn('регион', reply.text.lower())
        self.assertEqual(assistant.researcher.calls, 0)

    def test_followup_includes_previous_question_and_profile(self):
        model = FakeModel(PLAN)
        state = {'answers': {'region': 'Казань'}, 'paused': True}
        self.assistant(model).respond(state, 'А куда его подавать?', [{'question': 'Какое заявление нужно для регистрации ИП?', 'answer': 'Р21001'}])
        self.assertEqual(model.calls[0]['profile']['region'], 'Казань')
        self.assertEqual(model.calls[0]['history'][0]['answer'], 'Р21001')
        self.assertEqual(model.schemas, [QuestionIntent, Summary])

    def test_explicit_form_followup_skips_planner_but_summarizes(self):
        model, researcher = FakeModel(), FakeResearcher()
        reply = self.assistant(model, researcher).respond(None, 'А куда его подавать?',
            [{'question': 'Где взять Р21001?', 'answer': 'Форма Р21001'}])
        self.assertIn('Р21001', reply.text)
        self.assertEqual(model.schemas, [Summary])
        self.assertEqual(researcher.calls, 1)

    def test_number_guard_recognizes_form_beyond_model_history_excerpt(self):
        model = FakeModel(QuestionIntent(action='search', text='Куда подать Р21001?', topics=['registration']))
        researcher = FakeResearcher()
        reply = self.assistant(model, researcher).respond(None, 'Куда его подавать?',
            [{'question': 'Какие документы нужны для регистрации ИП?',
              'answer': 'Порядок регистрации. ' * 25 + 'Заявление Р21001.'}])
        self.assertEqual(researcher.calls, 1)
        self.assertIn(PASSAGE['url'], reply.text)
        self.assertNotIn('Р21001', model.calls[0]['history'][0]['answer'])

    def test_survey_does_not_invoke_model_and_ask_preserves_survey(self):
        model = FakeModel(PLAN)
        assistant = self.assistant(model)
        state = assistant.respond(None, '/begin').state
        state = assistant.respond(state, 'Разработка сайтов').state
        self.assertEqual(model.calls, [])
        reply = assistant.respond(state, '/ask Какие документы нужны?')
        self.assertEqual(reply.state, state)
        self.assertEqual(model.schemas, [QuestionIntent, Summary])
        self.assertTrue(assistant.respond(state, '/chat').state['paused'])

    def test_clarification_and_unrelated_question_do_not_fetch(self):
        for relevant, clarification in [(True, 'Какой документ?'), (False, '')]:
            with self.subTest(relevant=relevant):
                researcher = FakeResearcher()
                plan = PLAN.model_copy(update={'relevant': relevant, 'clarification': clarification})
                reply = self.assistant(FakeModel(plan), researcher).respond(None, 'Вопрос')
                self.assertTrue(reply.text)
                self.assertEqual(researcher.calls, 0)

    def test_model_outage_and_source_outage_are_user_visible(self):
        reply = self.assistant(FakeModel(ModelError('model_unavailable'))).respond(None, 'Документы ИП')
        self.assertIn('модель', reply.text)
        reply = self.assistant(FakeModel(PLAN), FakeResearcher([], ['source_network_failed'])).respond(None, 'Документы ИП')
        self.assertIn('не удалось прочитать', reply.text)

    def test_invalid_selection_is_not_rendered(self):
        bad = Answer(items=[4], clarification='')
        self.assertIsNone(render_answer(bad, [PASSAGE]))

    def test_smalltalk_needs_neither_model_nor_sources_and_preserves_survey(self):
        for state in [None, {'answers': {'activity': 'Разработка сайтов'}}]:
            for question in ['Кто ты такой?', 'Привет, бро!', '/ask Что ты умеешь?', '/about', 'Спасибо!']:
                with self.subTest(question=question, state=state):
                    model, researcher = FakeModel(), FakeResearcher()
                    assistant = self.assistant(model, researcher)
                    self.assertFalse(assistant.needs_research(state, question))
                    reply = assistant.respond(state, question)
                    self.assertEqual(reply.state, state)
                    self.assertTrue(reply.text)
                    self.assertFalse(reply.remember)
                    self.assertEqual(model.calls, [])
                    self.assertEqual(researcher.calls, 0)

    def test_mixed_greeting_still_researches_business_question(self):
        model, researcher = FakeModel(), FakeResearcher()
        assistant = self.assistant(model, researcher)
        question = 'Привет, какие документы нужны для регистрации ИП?'
        self.assertTrue(assistant.needs_research(None, question))
        self.assertIn('Р21001', assistant.respond(None, question).text)
        self.assertEqual(researcher.calls, 1)
        self.assertEqual(model.schemas, [Summary])

    def test_lower_ranked_continuation_keeps_exception_in_summary_input(self):
        article = ('Документы можно подать на бумаге. При электронной подаче документов, '
                   'в том числе через МФЦ и нотариуса, уплачивать государственную пошлину не требуется!')
        passages = [{**PASSAGE, 'text': article[:125]},
                    {**PASSAGE, 'url': 'https://www.nalog.gov.ru/other/', 'text': 'Другой материал.'},
                    {**PASSAGE, 'url': 'https://www.nalog.gov.ru/third/', 'text': 'Ещё один материал.'},
                    {**PASSAGE, 'text': article[65:]}]
        model = FakeModel()
        reply = self.assistant(model, FakeResearcher(passages)).respond(None, 'Как открыть ИП?')
        sentences = model.calls[-1]['sources'][0]['sentences']
        self.assertIn(article, ' '.join(sentence['text'] for sentence in sentences))
        self.assertLess(len(reply.text), 1500)

    def test_nonoverlapping_fourth_fragment_keeps_exception_with_its_source(self):
        exception = ('При электронной подаче документов, в том числе через МФЦ и нотариуса, '
                     'государственная пошлина не уплачивается.')
        passages = [PASSAGE,
                    {**PASSAGE, 'url': 'https://www.nalog.gov.ru/other/', 'text': 'Другой материал о регистрации опубликован на сайте.'},
                    {**PASSAGE, 'url': 'https://www.nalog.gov.ru/third/', 'text': 'Ещё один материал о подаче документов опубликован на сайте.'},
                    {**PASSAGE, 'text': exception}]
        model = FakeModel()
        model.summary_response = Summary(points=[
            {'text': exception, 'source': 1, 'evidence': 2},
        ], clarification='')

        reply = self.assistant(model, FakeResearcher(passages)).respond(None, 'Как открыть ИП?')

        sources = model.calls[-1]['sources']
        self.assertEqual(len(sources), 3)
        sentences = sources[0]['sentences']
        self.assertEqual([sentence['text'] for sentence in sentences], [PASSAGE['text'], exception])
        self.assertNotEqual(sentences[0]['segment'], sentences[1]['segment'])
        self.assertTrue(sources[0]['truncated'])
        self.assertIn(exception, reply.text)
        self.assertIn(PASSAGE['url'], reply.text)
        self.assertTrue(reply.remember)
        self.assertEqual(model.schemas, [Summary])

    def test_summary_outage_returns_short_sources_without_a_wall_of_quotes(self):
        model = FakeModel()
        model.summary_response = ModelError('model_timeout')
        article = {**PASSAGE, 'text': PASSAGE['text'] * 25}
        reply = self.assistant(model, FakeResearcher([article])).respond(None, 'Как открыть ИП?')
        self.assertIn('не удалось', reply.text)
        self.assertIn(PASSAGE['url'], reply.text)
        self.assertNotIn(PASSAGE['text'], reply.text)
        self.assertLess(len(reply.text), 900)
        self.assertFalse(reply.remember)

    def test_answer_cannot_invent_requirement_with_a_real_source(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            Answer.model_validate({'items': [{'source': 1, 'quote': PASSAGE['text'],
                'text': 'Уведомление об отказе от УСН прилагается по желанию'}], 'clarification': ''})
        rendered = render_answer(ANSWER, [PASSAGE])
        self.assertIn(PASSAGE['text'], rendered)
        self.assertNotIn('прилагается по желанию', rendered)

    def test_selected_excerpt_preserves_conditions_and_exceptions(self):
        passage = {**PASSAGE, 'text': 'При подаче на бумаге нужна квитанция. При электронной подаче пошлина не уплачивается.'}
        rendered = render_answer(ANSWER, [passage])
        self.assertIn(passage['text'], rendered)

    def test_overlapping_fragments_restore_complete_exception(self):
        article = 'Документы можно подать на бумаге. При электронной подаче документов, в том числе через МФЦ и нотариуса, уплачивать государственную пошлину не требуется!'
        passages = [{**PASSAGE, 'text': article[:125]}, {**PASSAGE, 'text': article[65:]}]
        rendered = render_answer(Answer(items=[1, 2], clarification=''), passages)
        self.assertIn(article, rendered)
        self.assertEqual(rendered.count('При электронной'), 1)
        self.assertNotIn('часть сведений', rendered)

    def test_incomplete_trailing_sentence_is_not_presented_as_full_quote(self):
        complete = 'Для регистрации подайте заявление по форме Р21001. При электронной подаче действуют другие условия.'
        rendered = render_answer(ANSWER, [{**PASSAGE, 'text': complete + ' Начало оборванного предлож'}])
        self.assertIn(complete, rendered)
        self.assertNotIn('оборванного', rendered)

    def test_long_question_is_not_sent_to_model(self):
        model = FakeModel()
        reply = self.assistant(model).respond(None, 'x' * 2001)
        self.assertIn('2000', reply.text)
        self.assertEqual(model.calls, [])

    def test_dropped_claim_is_marked_as_partial_answer(self):
        answer = Answer(items=[1, 4], clarification='')
        rendered = render_answer(answer, [PASSAGE])
        self.assertIn('часть сведений', rendered)
        self.assertIn('Р21001', rendered)


class OllamaTests(unittest.TestCase):
    def test_structured_request_and_invalid_response(self):
        requests = []
        payload = {'done': True, 'message': {'content': PLAN.model_dump_json()}}
        class Opener:
            def open(self, request, timeout):
                requests.append(json.loads(request.data))
                return io.BytesIO(json.dumps(payload).encode())
        model = Ollama(Settings())
        model.opener = Opener()
        self.assertEqual(model.generate(Plan, 'system', 'question'), PLAN)
        self.assertFalse(requests[0]['stream'])
        self.assertFalse(requests[0]['think'])
        self.assertEqual(requests[0]['options']['num_gpu'], 0)
        self.assertEqual(requests[0]['format']['type'], 'object')
        payload['done_reason'] = 'length'
        with self.assertRaises(ModelError):
            model.generate(Plan, '', '')
        del payload['done_reason']
        payload['message']['content'] = '{"extra":true}'
        with self.assertRaises(ModelError):
            model.generate(Plan, '', '')
        payload = []
        with self.assertRaises(ModelError):
            model.generate(Plan, '', '')

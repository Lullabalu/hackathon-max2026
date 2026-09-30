import copy
import io
import json
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from ip_bot.assistant import Assistant
from ip_bot.budget import remaining_seconds, time_limit
from ip_bot.llm import ModelError, Ollama, Plan
from ip_bot.okved_search import ActivityResolver, ActivityQuery, ActivitySelection, reference
from ip_bot.settings import Settings
from ip_bot.summarization import Summary, summary_input, render_summary
from test_assistant import FakeModel, FakeResearcher, PASSAGE, first_sentence_summary


class QualityPipelineTests(unittest.TestCase):
    def test_default_generation_has_no_deadline_or_socket_time_limit(self):
        model = Ollama(Settings())
        result = Plan(query='Документы ИП', topics=['registration'], keywords=[], clarification='', relevant=True)
        def opened(request, timeout):
            self.assertIsNone(timeout)
            self.assertIsNone(remaining_seconds())
            return io.BytesIO(json.dumps({'done': True, 'message': {'content': result.model_dump_json()}}).encode())
        with patch.object(model.opener, 'open', side_effect=opened):
            self.assertEqual(model.generate(Plan, '', ''), result)

    def test_all_preparation_stages_can_finish_without_a_shared_deadline(self):
        class SlowModel(FakeModel):
            def generate(self, *args, **kwargs):
                self_deadlines.append(remaining_seconds())
                time.sleep(0.02)
                return super().generate(*args, **kwargs)
        class SlowResearcher(FakeResearcher):
            def search(self, plan):
                self_deadlines.append(remaining_seconds())
                time.sleep(0.02)
                return super().search(plan)
        self_deadlines = []
        assistant = Assistant(None, Settings(), model=SlowModel(), researcher=SlowResearcher())
        reply = assistant.respond(None, 'Как заполнить Р21001?')
        self.assertIn('Р21001', reply.text)
        self.assertTrue(reply.remember)
        self.assertEqual(self_deadlines, [None, None])

    def test_invalid_summary_is_retried_without_weakening_evidence_checks(self):
        class RepairModel(FakeModel):
            def generate(self, schema, system, content, **kwargs):
                if not self.schemas:
                    self.schemas.append(schema)
                    return Summary(points=[{'text': 'Нужно заплатить 99999 рублей.', 'source': 1, 'evidence': 1}], clarification='')
                return super().generate(schema, system, content, **kwargs)
        model = RepairModel()
        reply = Assistant(None, Settings(), model=model, researcher=FakeResearcher()).respond(None, 'Как заполнить Р21001?')
        self.assertEqual(model.schemas, [Summary, Summary])
        self.assertTrue(model.calls[-1]['validation_errors'])
        self.assertNotIn('99999', reply.text)
        self.assertIn('Р21001', reply.text)

    def test_no_evidence_triggers_reformulation_and_another_search(self):
        plan = Plan(query='Заполнение заявления для регистрации ИП', topics=['registration'], keywords=[], clarification='', relevant=True)
        model, researcher = FakeModel(plan), Mock()
        researcher.search.side_effect = [([], []), ([PASSAGE], [])]
        reply = Assistant(None, Settings(), model=model, researcher=researcher).respond(None, 'Как заполнить Р21001?')
        self.assertEqual(researcher.search.call_count, 2)
        self.assertIn('Р21001', reply.text)
        self.assertTrue(reply.remember)

    def test_unlimited_child_does_not_disable_explicit_parent_limit(self):
        with time_limit(1):
            with time_limit(None):
                self.assertLessEqual(remaining_seconds(), 1)

    def test_compound_question_keeps_evidence_for_all_requested_actions(self):
        source = {**PASSAGE, 'text': 'Заявление можно заполнить на бумаге. ' + 'Описание отдельного условия. ' * 8 +
                  'Документы можно подать лично в МФЦ.'}
        payload = json.loads(summary_input('Как заполнить Р21001 и куда его подать?', {}, [source]))
        text = ' '.join(unit['text'] for item in payload['sources'] for unit in item['sentences'])
        self.assertIn('заполнить на бумаге', text)
        self.assertIn('подать лично в МФЦ', text)
        self.assertNotIn('focus', payload)

    def test_absence_is_a_negative_claim_not_a_positive_paraphrase(self):
        result = Summary(points=[{'text': 'Госпошлина отсутствует при электронной подаче документов.',
                                  'source': 1, 'evidence': 1}], clarification='')
        negative = {**PASSAGE, 'text': 'Госпошлина не требуется при электронной подаче документов.'}
        positive = {**PASSAGE, 'text': 'Госпошлина требуется при электронной подаче документов.'}
        self.assertIsNotNone(render_summary(result, [negative]))
        self.assertIsNone(render_summary(result, [positive]))


class OfficialOkvedTests(unittest.TestCase):
    def resolver(self, code='74.20'):
        model = Mock()
        model.generate.side_effect = [ActivityQuery(queries=['фотография'], clarification=''),
                                     ActivitySelection(primary=code, additional=[], clarification='')]
        opener = Mock()
        opener.open.side_effect = URLError('offline')
        return ActivityResolver(model, opener=opener)

    def test_classifier_contains_verified_codes_beyond_small_curated_catalog(self):
        data, rows, _, _ = reference()
        self.assertGreater(len(rows), 2000)
        self.assertEqual(data['source'], 'https://service.nalog.ru/okved/api/v1/reference/okveds')
        for code in ('74.20', '95.23', '49.41'):
            self.assertIn(code, rows)

    def test_offline_snapshot_still_supplies_official_names_for_model_selection(self):
        result = self.resolver()('Фотографирую людей в студии')
        self.assertEqual(result['status'], 'matched')
        self.assertEqual(result['primary'], {'code': '74.20', 'title': reference()[1]['74.20']['name']})
        self.assertEqual(result['additional'], [])
        self.assertNotIn('небольшом справочнике', result.get('coverage', ''))

    def test_model_cannot_invent_a_code_or_select_an_unretrieved_code(self):
        for code in ('00.00', '01.11'):
            result = self.resolver(code)('Фотографирую людей в студии')
            self.assertNotEqual(result['status'], 'matched')
            self.assertIsNone(result['primary'])

    def test_free_question_does_not_silently_change_confirmed_profile(self):
        assistant = Assistant(None, Settings(), model=FakeModel(), researcher=FakeResearcher())
        state = {'answers': {'activity': 'Разработка сайтов'}, 'consent': {'accepted': True}, 'paused': True}
        original = copy.deepcopy(state)
        assistant.journey.activity_resolver = self.resolver()
        reply = assistant.respond(state, '/ask Какой ОКВЭД для фотографии?')
        self.assertIn('74.20', reply.text)
        self.assertEqual(reply.state, original)
        self.assertEqual(state, original)

    def test_guided_activity_uses_expanded_search_and_waits_for_confirmation(self):
        assistant = Assistant(None, Settings(), model=FakeModel(), researcher=FakeResearcher())
        suggestion = {'status': 'matched', 'primary': {'code': '74.20', 'title': 'Деятельность в области фотографии'},
                      'additional': [], 'warnings': [], 'sources': []}
        assistant.journey.activity_resolver = Mock(return_value=suggestion)
        state = {'answers': {}, 'consent': {'accepted': True},
                 'guide': {'active': True, 'stage': 'activity', 'history': []}}
        reply = assistant.respond(state, 'Фотографирую людей в студии')
        self.assertEqual(reply.state['guide']['stage'], 'okved_confirm')
        self.assertNotIn('okved', reply.state)
        self.assertIn('74.20', reply.text)
        confirmed = assistant.respond(reply.state, 'Да, подходит')
        self.assertEqual(confirmed.state['okved']['primary']['code'], '74.20')


if __name__ == '__main__':
    unittest.main()

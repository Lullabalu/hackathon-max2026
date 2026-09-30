import copy
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock

from ip_bot.assistant import Assistant
from ip_bot.llm import QuestionIntent
from ip_bot.settings import Settings
from ip_bot.summarization import Summary
from tests.test_assistant import first_sentence_summary


PROFILE = {
    'answers': {'activity': 'Разработка сайтов', 'region': 'Казань', 'revenue': '1200000.50',
                'expenses': '200000.00', 'employees': 0, 'clients': 'Физлица'},
    'tax_options': {'new_ip': True, 'previous_revenue': '0', 'npd_activity_eligible': True,
                    'npd_deduction_remaining': '5000'},
    'registration': {'citizenship': 'foreign', 'age': 17, 'method': 'mfc'},
    'selected_regime': 'npd', 'consent': {'accepted': True}, 'paused': True,
    'form': {'surname': 'СЕКРЕТНОЕФИО', 'inn': '123456789012', 'email': 'private@example.test'},
}
PASSAGE = {'url': 'https://www.nalog.gov.ru/example/', 'title': 'Официальная инструкция',
           'text': 'Порядок заполнения заявления указан в официальной инструкции ФНС.',
           'fetched_at': datetime(2026, 9, 30, tzinfo=timezone.utc)}


class PersonalAnswersTests(unittest.TestCase):
    def assistant(self, *, intent=None):
        model, sources = Mock(), Mock()
        def generate(schema, system, content, **kwargs):
            if schema is Summary:
                return first_sentence_summary(json.loads(content))
            if schema is QuestionIntent and intent is not None:
                return intent
            raise AssertionError('Для понятного вопроса планировщик не нужен')
        model.generate.side_effect = generate
        sources.search.return_value = ([PASSAGE], [])
        return Assistant(None, Settings(), model=model, researcher=sources)

    def test_personal_calculation_uses_saved_deduction_and_annual_amount(self):
        assistant = self.assistant()
        original = copy.deepcopy(PROFILE)
        reply = assistant.respond(PROFILE, 'Сколько мне платить налогов?')
        self.assertEqual(PROFILE, original)
        row = next(row for row in reply.state['tax_comparison']['rows'] if row['id'] == 'npd')
        self.assertEqual(row['total'], '43000.02')
        self.assertIn('Казань', reply.text)
        self.assertIn('43 000.02 ₽', reply.text)
        self.assertIn('условный', reply.text)
        self.assertIn('полный год', reply.text)
        self.assertIn('/tax npd', reply.text)
        self.assertLess(len(reply.text), 600)
        self.assertNotIn('СЕКРЕТНОЕФИО', reply.text)
        self.assertNotIn('private@example.test', reply.text)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()

    def test_short_tax_answer_keeps_range_and_unknown_deduction(self):
        assistant = self.assistant()
        state = copy.deepcopy(PROFILE)
        state['tax_options'].pop('npd_deduction_remaining')
        reply = assistant.respond(state, 'Сколько мне платить налогов?')
        self.assertIn('условный диапазон', reply.text)
        self.assertIn('38 000.02 ₽', reply.text)
        self.assertIn('48 000.02 ₽', reply.text)
        self.assertIn('Нужно уточнить:\n• остаток вычета НПД', reply.text)
        self.assertIn('/tax npd', reply.text)
        self.assertNotIn('условный итог', reply.text)
        self.assertLess(len(reply.text), 700)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()

    def test_short_tax_answer_does_not_present_missing_vat_and_worker_contributions_as_zero(self):
        assistant = self.assistant()
        state = copy.deepcopy(PROFILE)
        state['selected_regime'] = 'usn_income'
        state['answers']['employees'] = 2
        state['tax_options'] = {'usn_income_rate': '6'}
        reply = assistant.respond(state, 'Сколько мне платить налогов?')
        self.assertIn('неполная сумма', reply.text)
        self.assertIn('неизвестные НДС/взносы работников не включены', reply.text)
        self.assertIn('взносы за работников с травматизмом', reply.text)
        self.assertIn('годовой НДС', reply.text)
        self.assertIn('доход прошлого года', reply.text)
        self.assertIn('Ставка 6.00%', reply.text)
        self.assertIn('/tax usn_income', reply.text)
        self.assertLess(len(reply.text), 1000)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()

    def test_short_tax_answer_explains_excluded_saved_regime_and_keeps_choice_route(self):
        assistant = self.assistant()
        state = copy.deepcopy(PROFILE)
        state['answers']['employees'] = 1
        state['guide'] = {'active': True, 'stage': 'registration', 'history': []}
        state['flow'] = {'kind': 'registration', 'index': 1}
        reply = assistant.respond(state, 'Сколько мне платить налогов?')
        self.assertIn('НПД', reply.text)
        self.assertIn('режим исключён', reply.text)
        self.assertIn('работников', reply.text)
        self.assertNotIn('43 000', reply.text)
        self.assertNotIn('selected_regime', reply.state)
        self.assertNotIn('flow', reply.state)
        self.assertEqual(reply.state['guide']['stage'], 'regime_choice')
        self.assertIn('/tax npd', reply.text)
        self.assertLess(len(reply.text), 500)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()

    def test_tax_details_remain_available_and_no_selection_does_not_invent_one(self):
        assistant = self.assistant()
        details = assistant.respond(PROFILE, '/tax npd')
        self.assertIn('Состав итога:', details.text)
        self.assertIn('43 000.02 ₽', details.text)
        self.assertIn('Источники ФНС', details.text)
        state = copy.deepcopy(PROFILE)
        state.pop('selected_regime')
        reply = assistant.respond(state, 'Сколько мне платить налогов?')
        self.assertIn('Режим ещё не выбран', reply.text)
        self.assertIn('/regime', reply.text)
        self.assertNotIn('selected_regime', reply.state)
        self.assertEqual(reply.state['answers'], state['answers'])
        self.assertLess(len(reply.text), 300)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()

    def test_registration_answer_uses_age_citizenship_method_and_regime(self):
        assistant = self.assistant()
        foreign = assistant.respond(PROFILE, 'Какие документы мне нужны для регистрации ИП?')
        for required in ('Казань', 'Разработка сайтов', 'Несовершеннолетнему', 'проживания', 'не взимается', 'НПД'):
            self.assertIn(required, foreign.text)
        self.assertNotIn('800', foreign.text)
        adult = copy.deepcopy(PROFILE)
        adult['registration'] = {'citizenship': 'ru', 'age': 35, 'method': 'paper'}
        adult['selected_regime'] = 'usn_income'
        reply = assistant.respond(adult, 'Какие документы нужны для регистрации ИП?')
        self.assertIn('800', reply.text)
        self.assertIn('УСН', reply.text)
        self.assertNotIn('Несовершеннолетнему', reply.text)
        self.assertNotIn('Для иностранца', reply.text)
        assistant.researcher.search.assert_not_called()

    def test_missing_registration_parameter_is_asked_without_repeating_saved_answers(self):
        assistant = self.assistant()
        state = copy.deepcopy(PROFILE)
        state['registration'] = {'citizenship': 'foreign', 'age': 17}
        reply = assistant.respond(state, 'Какие документы мне нужны для регистрации ИП?')
        self.assertEqual(reply.state['flow'], {'kind': 'registration', 'index': 2})
        self.assertIn('Как планируете подать', reply.text)
        self.assertEqual(reply.state['answers'], state['answers'])
        assistant.researcher.search.assert_not_called()

    def test_registered_user_is_asked_which_procedure_the_documents_are_for(self):
        state = copy.deepcopy(PROFILE)
        state['registration']['date'] = '2026-09-01'
        assistant = self.assistant()
        reply = assistant.respond(state, 'Какие документы мне нужны?')
        self.assertEqual(reply.state, state)
        self.assertIn('2026-09-01', reply.text)
        self.assertIn('Уточните', reply.text)
        assistant.researcher.search.assert_not_called()

    def test_generic_documents_question_does_not_assume_registration_from_profile(self):
        assistant = self.assistant()
        reply = assistant.respond(PROFILE, 'Какие документы мне нужны?',
            [{'question': 'Как закрыть ИП?', 'answer': 'Для закрытия используется форма Р26001.'}])
        self.assertEqual(reply.state, PROFILE)
        self.assertIn('для какого действия', reply.text)
        self.assertNotIn('Р21001', reply.text)
        assistant.researcher.search.assert_not_called()
        assistant.model.generate.assert_not_called()

    def test_missing_business_parameter_is_requested_for_personal_comparison(self):
        state = {'answers': {'activity': 'Разработка сайтов', 'region': 'Казань'},
                 'consent': {'accepted': True}}
        assistant = self.assistant()
        reply = assistant.respond(state, 'Какой режим мне подходит?')
        self.assertEqual(reply.state['guide']['field'], 'revenue')
        self.assertEqual(reply.state['answers'], state['answers'])
        self.assertNotIn('tax_comparison', reply.state)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()

    def test_personal_comparison_keeps_visible_next_step_in_active_guide(self):
        state = copy.deepcopy(PROFILE)
        state['guide'] = {'active': True, 'stage': 'after_checklist'}
        assistant = self.assistant()
        reply = assistant.respond(state, 'Какой режим мне подходит?')
        self.assertIn('Казань', reply.text)
        self.assertIn('Какой режим хотите выбрать?', reply.text)
        self.assertEqual(reply.state['guide']['stage'], 'regime_choice')
        self.assertIn('npd', reply.state['guide']['offered_regimes'])
        self.assertLessEqual(len(reply.text), 3900)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()

    def test_local_research_always_receives_relevant_profile_conditions(self):
        assistant = self.assistant()
        original = copy.deepcopy(PROFILE)
        reply = assistant.respond(PROFILE, 'Как заполнить Р21001?')
        self.assertEqual(PROFILE, original)
        self.assertEqual(reply.state, original)
        plan = assistant.researcher.search.call_args.args[0]
        for condition in ('иностранное гражданство', '17', 'МФЦ', 'Казань'):
            self.assertIn(condition, plan.query)
        self.assertIn(PASSAGE['url'], reply.text)
        summary_context = json.loads(assistant.model.generate.call_args.args[2])
        self.assertEqual(summary_context['profile']['registration'], PROFILE['registration'])
        self.assertEqual(summary_context['profile']['region'], 'Казань')
        self.assertLess(len(reply.text), 1500)
        self.assertNotIn('1200000.50', reply.text)
        self.assertEqual(assistant.model.generate.call_count, 1)

    def test_model_gets_full_current_business_context_not_identity_or_old_profile(self):
        assistant = self.assistant(intent=QuestionIntent(action='search', text='Уведомление при доходе 1200000.5', topics=['taxes']))
        reply = assistant.respond(PROFILE, 'Что с нужным уведомлением в моей ситуации?',
            [{'question': 'Раньше у меня было 7 работников в Самаре', 'answer': 'Старый ответ'}])
        context = json.JSONDecoder().raw_decode(assistant.model.generate.call_args_list[0].args[2])[0]
        current = context['profile']
        self.assertEqual(current['region'], 'Казань')
        self.assertEqual(current['employees'], 0)
        self.assertEqual(current['selected_regime'], 'npd')
        self.assertEqual(current['registration'], PROFILE['registration'])
        self.assertEqual(current['tax_options'], PROFILE['tax_options'])
        for secret in ('СЕКРЕТНОЕФИО', '123456789012', 'private@example.test'):
            self.assertNotIn(secret, str(context))
        self.assertIn(PASSAGE['url'], reply.text)
        summary_context = json.loads(assistant.model.generate.call_args_list[1].args[2])
        self.assertEqual(summary_context['profile']['region'], 'Казань')

    def test_hypothetical_question_does_not_overwrite_saved_region(self):
        question = 'А если в Москве, как заполнить Р21001?'
        assistant = self.assistant(intent=QuestionIntent(action='search', text='Как заполнить Р21001 в Москве?', topics=['registration']))
        original = copy.deepcopy(PROFILE)
        reply = assistant.respond(PROFILE, question)
        plan = assistant.researcher.search.call_args.args[0]
        self.assertIn('Москве', plan.query)
        self.assertNotIn('Казань', plan.query)
        self.assertEqual(reply.state, original)
        self.assertEqual(PROFILE, original)

    def test_memory_question_uses_stored_field_without_search(self):
        assistant = self.assistant()
        reply = assistant.respond(PROFILE, 'Какой регион я указал?')
        self.assertIn('Казань', reply.text)
        reply = assistant.respond(PROFILE, 'Сколько у меня сотрудников?')
        self.assertIn('Работники: 0', reply.text)
        assistant.model.generate.assert_not_called()
        assistant.researcher.search.assert_not_called()


if __name__ == '__main__':
    unittest.main()

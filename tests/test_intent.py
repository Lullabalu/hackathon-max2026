import unittest

from pydantic import ValidationError

from ip_bot.llm import QuestionIntent


class QuestionIntentTests(unittest.TestCase):
    def test_search_preserves_conditions_without_inventing_keywords(self):
        text = 'Заполнение Р21001 гражданином Казахстана в Казани в 2027 году'
        plan = QuestionIntent(action='search', text=text, topics=['registration']).to_plan()
        self.assertEqual(plan.query, text)
        self.assertEqual(plan.keywords, [])
        self.assertTrue(plan.relevant)
        self.assertFalse(plan.clarification)

    def test_clarification_and_offtopic_never_become_search_requests(self):
        plan = QuestionIntent(action='clarify', text='Какой документ вы имеете в виду?', topics=[]).to_plan()
        self.assertTrue(plan.relevant)
        self.assertTrue(plan.clarification)
        self.assertEqual(plan.query, '')
        generic = QuestionIntent(action='clarify', text='', topics=[]).to_plan()
        self.assertIn('какой документ', generic.clarification)
        plan = QuestionIntent(action='offtopic', text='', topics=[]).to_plan()
        self.assertFalse(plan.relevant)
        self.assertEqual(plan.query, '')

    def test_empty_search_and_unknown_topics_are_rejected(self):
        for fields in [dict(action='search', text='Документы', topics=[]),
                       dict(action='search', text='', topics=['registration']),
                       dict(action='search', text='Документы', topics=['unknown'])]:
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                QuestionIntent(**fields)

    def test_invented_form_number_is_not_used_for_research(self):
        intent = QuestionIntent(action='search', text='Добавить ОКВЭД форма Р21001 Самара', topics=['changes'])
        plan = intent.to_plan(context={'question': 'Как добавить ОКВЭД в Самаре?', 'history': [], 'profile': {}}, current_year=2026)
        self.assertTrue(plan.clarification)
        self.assertEqual(plan.query, '')

    def test_form_from_history_and_explicit_age_are_allowed(self):
        intent = QuestionIntent(action='search', text='Р21001 регистрация ИП в 17 лет', topics=['registration'])
        plan = intent.to_plan(context={'question': 'А если мне 17 лет?', 'history': [{'question': 'Где взять Р21001?'}], 'profile': {}}, current_year=2026)
        self.assertFalse(plan.clarification)

    def test_relative_year_is_allowed_only_with_matching_question(self):
        intent = QuestionIntent(action='search', text='УСН в Татарстане 2027', topics=['taxes'])
        plan = intent.to_plan(context={'question': 'А со следующего года в Татарстане?'}, current_year=2026)
        self.assertFalse(plan.clarification)
        plan = intent.to_plan(context={'question': 'А в Татарстане?'}, current_year=2026)
        self.assertTrue(plan.clarification)

    def test_saved_decimal_amount_and_nested_registration_values_are_allowed(self):
        intent = QuestionIntent(action='search', text='УСН доход 1200000 расходы 200000.5 ИП в 17 лет', topics=['taxes'])
        context = {'question': 'Как считать налог по моей анкете?', 'profile': {
            'revenue': '1200000.00', 'expenses': '200000.50', 'registration': {'age': 17}}}
        self.assertFalse(intent.to_plan(context=context).clarification)
        wrong = intent.model_copy(update={'text': 'УСН доход 1200001 ИП в 18 лет'})
        self.assertTrue(wrong.to_plan(context=context).clarification)

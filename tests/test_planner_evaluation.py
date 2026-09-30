import copy
import json
from pathlib import Path
import unittest

from ip_bot.llm import Plan
from scripts.evaluate_planner import evaluate


FIXTURE = Path(__file__).parent / 'fixtures/planner_cases.json'


class PlannerEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = json.loads(FIXTURE.read_text())
        cls.cases = {case['id']: case for case in fixture['cases']}
        cls.known_topics = {topic['id'] for topic in fixture['topics']}

    def checks(self, case_id, query, *, expected=None):
        case = self.cases[case_id]
        expected = case['expected'] if expected is None else expected
        plan = Plan(query=query, topics=expected.get('topics_include', []),
                    keywords=[], clarification='', relevant=expected['relevant'])
        return {check['name']: check for check in evaluate(plan, expected, case['input'], self.known_topics)}

    def test_paraphrases_accept_either_term_but_require_every_group(self):
        case_id = 'historical_period_and_negation'
        for query in ('Совмещение ПСН и НПД в Пермском крае в 2024 году',
                      'СОВМЕСТИМОСТЬ ПАТЕНТА И НПД В ПЕРМСКОМ КРАЕ В 2024 ГОДУ'):
            with self.subTest(query=query):
                self.assertTrue(all(check['passed'] for check in self.checks(case_id, query).values()))
        # Явно проверяем ё/е на дополнительном синтетическом условии helper.
        expected = copy.deepcopy(self.cases[case_id]['expected'])
        expected['query_term_groups'].append(['учет'])
        checks = self.checks(case_id, 'УЧЁТ: совмещение ПСН и НПД в Пермском крае в 2024 году', expected=expected)
        self.assertTrue(checks['query_term_groups']['passed'])

    def test_lost_region_or_subject_fails_even_with_correct_topics_and_numbers(self):
        examples = [
            ('form_referent_foreign_citizen', 'Как заполнить Р21001 гражданину Казахстана?', ['казан']),
            ('closing_followup_usn', 'Закрытие ИП на УСН осенью 2026 года в Санкт-Петербурге', ['документ']),
        ]
        for case_id, query, missing in examples:
            with self.subTest(case_id=case_id):
                checks = self.checks(case_id, query)
                self.assertTrue(checks['topics_include']['passed'])
                self.assertTrue(checks['required_query_numbers']['passed'])
                self.assertFalse(checks['query_term_groups']['passed'])
                self.assertIn(missing, checks['query_term_groups']['missing_groups'])

    def test_preserved_words_do_not_mask_changed_year(self):
        checks = self.checks('historical_period_and_negation', 'Совмещение ПСН и НПД в Пермском крае в 2026 году')
        self.assertTrue(checks['query_term_groups']['passed'])
        self.assertFalse(checks['required_query_numbers']['passed'])
        self.assertFalse(checks['no_unprovided_numbers']['passed'])


if __name__ == '__main__':
    unittest.main()

import copy
import json
import unittest

from ip_bot.llm import Plan, QuestionIntent
from ip_bot.profile_context import build_profile_context, enrich_plan, profile_summary, summary


def state():
    return {
        'answers': {'activity': 'Разработка сайтов', 'region': 'Казань', 'revenue': '1200000.00',
                    'expenses': '300000.50', 'employees': 0, 'clients': 'Физлица'},
        'tax_options': {'year': 2026, 'new_ip': True, 'usn_income_rate': '6', 'client_person_share': '0.5',
                        'npd_activity_eligible': True, 'payroll': '100000', 'vat_amount': '1000'},
        'registration': {'citizenship': 'foreign', 'age': 17, 'method': 'online', 'date': '2026-09-15'},
        'selected_regime': 'npd', 'timezone': 'Europe/Moscow',
        'okved': {'status': 'matched', 'primary': {'code': '62.01', 'title': 'Разработка'},
                  'additional': [{'code': '62.02', 'title': 'Консультации'}], 'warnings': ['Не передавать']},
        'form': {'surname': 'СЕКРЕТНАЯФАМИЛИЯ', 'name': 'СЕКРЕТНОЕИМЯ', 'inn': '123456789012',
                 'email': 'secret@example.test'},
        'consent': {'accepted': True}, 'guide': {'answer': 'СЕКРЕТНЫЙТЕКСТ'}, 'user_id': 999999,
        'history': [{'question': 'СЕКРЕТНЫЙВОПРОС'}],
    }


def plan(question, topics=('registration',), keywords=()):
    return Plan(query=question, topics=list(topics), keywords=list(keywords), clarification='', relevant=True)


class ProfileContextTests(unittest.TestCase):
    def test_whitelist_preserves_structured_numbers_and_codes_without_identity_fields(self):
        original = state()
        original['answers']['email'] = 'private@example.test'
        original['tax_options']['surname'] = 'СЕКРЕТ'
        original['registration']['inn'] = '123456789012'
        result = build_profile_context(original, 'Какие документы мне нужны?')
        self.assertEqual(result['region'], 'Казань')
        self.assertEqual(result['revenue'], '1200000.00')
        self.assertEqual(result['employees'], 0)
        self.assertEqual(result['tax_options']['npd_activity_eligible'], True)
        self.assertEqual(result['registration'],
                         {'citizenship': 'foreign', 'age': 17, 'method': 'online', 'date': '2026-09-15'})
        self.assertEqual(result['okved'], {'primary': '62.01', 'additional': ['62.02']})
        self.assertEqual(result['timezone'], 'Europe/Moscow')
        encoded = json.dumps(result, ensure_ascii=False)
        for value in ('СЕКРЕТ', '123456789012', '@', 'consent', 'user_id', 'history', 'guide', 'warnings'):
            self.assertNotIn(value, encoded)

    def test_no_profile_or_unrelated_state_returns_empty_dict(self):
        for value in (None, {}, [], {'form': state()['form']}, {'answers': {}}, {'answers': []}):
            with self.subTest(value=value):
                self.assertEqual(build_profile_context(value, 'А если в Москве?'), {})
                self.assertEqual(summary(value), '')

    def test_detached_context_and_plan_do_not_change_state_or_other_users(self):
        first, other = state(), state()
        before = copy.deepcopy(first)
        context = build_profile_context(first, '')
        context['tax_options']['year'] = 2027
        context['okved']['additional'].append('99.99')
        context['registration']['age'] = 18
        self.assertEqual(first, before)
        self.assertEqual(other, before)
        source = plan('Какие документы нужны для регистрации ИП?', keywords=['ИП', 'документы'])
        previous = source.model_dump()
        result = enrich_plan(source, build_profile_context(first), source.query)
        result.keywords.append('Изменение')
        self.assertEqual(source.model_dump(), previous)
        self.assertEqual(first, before)

    def test_explicit_and_ambiguous_region_alternatives_drop_old_region_and_dependent_rates(self):
        for question in ('А если в Москве?', 'а если в москве?', 'А если в другом регионе?', 'Хочу переехать в Нытву.'):
            with self.subTest(question=question):
                original = state()
                profile = build_profile_context(original, question)
                self.assertNotIn('region', profile)
                self.assertNotIn('usn_income_rate', profile.get('tax_options', {}))
                self.assertIn('region', profile['omitted_fields'])
                self.assertEqual(original['answers']['region'], 'Казань')
                enriched = enrich_plan(plan(question, ('taxes',)), profile, question)
                self.assertNotIn('Казань', enriched.query)
                self.assertNotIn('Казань', enriched.keywords)
                self.assertNotIn('Казань', profile_summary(profile))

    def test_institution_and_own_region_do_not_override_saved_region(self):
        for question in ('Как подать документы в ФНС?', 'Как подать документы в налоговую?',
                         'Какие правила в моём регионе?', 'Как отправить в электронном виде?',
                         'В Р21001 как указать адрес?', 'Какие сведения по ОКВЭД мне указать?',
                         'Какой регион я указал?', 'КАКОЙ РЕГИОН Я УКАЗАЛ?', 'Мой регион'):
            with self.subTest(question=question):
                self.assertEqual(build_profile_context(state(), question)['region'], 'Казань')

    def test_explicit_region_and_city_labels_override_saved_location_and_rates(self):
        for question in ('Какие документы нужны для регистрации ИП, регион Москва?',
                         'Какие документы нужны для регистрации ИП, город: Москва?',
                         'Какие налоги платить, регион: москва?'):
            with self.subTest(question=question):
                original = state()
                previous = copy.deepcopy(original)
                context = build_profile_context(original, question)
                self.assertNotIn('region', context)
                self.assertNotIn('usn_income_rate', context.get('tax_options', {}))
                self.assertIn('region', context['omitted_fields'])
                self.assertIn('tax_options.usn_income_rate', context['omitted_fields'])
                enriched = enrich_plan(plan(question, ('registration', 'taxes')), context, question)
                self.assertTrue(enriched.query.startswith(question))
                self.assertNotIn('Казань', enriched.query)
                self.assertNotIn('Казань', profile_summary(context))
                self.assertEqual(original, previous)

    def test_mentioning_a_field_does_not_override_its_saved_value(self):
        self.assertEqual(build_profile_context(state(), 'Как отразить гражданство в форме?')['registration']['citizenship'], 'foreign')
        self.assertEqual(build_profile_context(state(), 'Как вести учёт продаж?')['activity'], 'Разработка сайтов')
        self.assertEqual(build_profile_context(state(), 'Хочу открыть ИП, какие документы нужны?')['activity'], 'Разработка сайтов')
        self.assertEqual(build_profile_context(state(), 'Нужно ли указывать мой возраст?')['registration']['age'], 17)
        self.assertEqual(build_profile_context(state(), 'Как учитывать доходы в 2026 году?')['revenue'], '1200000.00')

    def test_question_overrides_amounts_workers_and_related_options(self):
        question = 'А если доход 3000000, расходы 500000 и найму сотрудника?'
        profile = build_profile_context(state(), question)
        for field in ('revenue', 'expenses', 'employees'):
            self.assertNotIn(field, profile)
        for field in ('payroll', 'vat_amount'):
            self.assertNotIn(field, profile['tax_options'])
        enriched = enrich_plan(plan(question, ('taxes',)), profile, question)
        self.assertTrue(enriched.query.startswith(question))
        self.assertNotIn('1200000', enriched.query)
        self.assertNotIn('работники 0', enriched.query)

    def test_explicit_regime_citizenship_age_method_and_period_take_priority(self):
        question = 'Я гражданин РФ, мне 30 лет. Что будет на УСН в следующем году, если подам через МФЦ?'
        profile = build_profile_context(state(), question)
        self.assertNotIn('selected_regime', profile)
        self.assertNotIn('year', profile['tax_options'])
        for field in ('citizenship', 'age', 'method'):
            self.assertNotIn(field, profile['registration'])
        enriched = enrich_plan(plan(question, ('registration', 'taxes')), profile, question)
        self.assertNotIn('иностранное', enriched.query)
        self.assertNotIn('НПД', enriched.query)
        self.assertNotIn('возраст 17', enriched.query)
        self.assertNotIn('онлайн', enriched.query)

    def test_new_activity_drops_saved_codes_and_activity_specific_options(self):
        profile = build_profile_context(state(), 'Какие налоги для магазина?')
        self.assertNotIn('activity', profile)
        self.assertNotIn('okved', profile)
        self.assertNotIn('npd_activity_eligible', profile['tax_options'])
        self.assertNotIn('usn_income_rate', profile['tax_options'])

    def test_registration_enrichment_has_only_relevant_conditions(self):
        question = 'Какие документы нужны для регистрации ИП?'
        original = plan(question, keywords=['ИП', 'документы'])
        enriched = enrich_plan(original, build_profile_context(state(), question), question)
        self.assertTrue(enriched.query.startswith(question))
        for condition in ('иностранное гражданство', 'возраст 17', 'онлайн', 'Казань'):
            self.assertIn(condition, enriched.query)
        self.assertNotIn('1200000', enriched.query)
        self.assertNotIn('300000', enriched.query)
        self.assertNotIn('Разработка', enriched.query)
        self.assertEqual(enriched.topics, original.topics)
        self.assertLessEqual(len(enriched.keywords), len(original.keywords) + 2)

    def test_model_number_guard_receives_saved_date_age_amount_and_okved(self):
        context = build_profile_context(state(), '')
        intent = QuestionIntent(action='search', text='Регистрация 2026-09-15, возраст 17, доход 1200000.00, ОКВЭД 62.01',
                                topics=['registration'])
        result = intent.to_plan(context={'question': 'Мои условия', 'profile': context}, current_year=2026)
        self.assertFalse(result.clarification)
        self.assertEqual(result.query, intent.text)

    def test_limits_keep_original_query_and_keywords_whole(self):
        source = plan('Какие документы для ИП? ' + 'длинный вопрос ' * 30, keywords=[f'ключ{i}' for i in range(8)])
        self.assertLessEqual(len(source.query), 500)
        enriched = enrich_plan(source, build_profile_context(state()), source.query)
        self.assertTrue(enriched.query.startswith(source.query))
        self.assertLessEqual(len(enriched.query), 500)
        self.assertEqual(enriched.keywords, source.keywords)
        summary_state = state()
        summary_state['answers']['activity'] = 'Длинная деятельность ' * 14
        rendered = summary(summary_state)
        self.assertLessEqual(len(rendered), 450)
        self.assertTrue(rendered.startswith('Учитываю вашу анкету: '))

    def test_profile_terms_do_not_displace_long_original_query_from_fts(self):
        source = plan(' '.join(f'термин{i}' for i in range(20)))
        self.assertEqual(enrich_plan(source, build_profile_context(state()), source.query), source)

    def test_clarification_and_offtopic_plans_are_not_rewritten(self):
        for source in (Plan(query='', topics=[], keywords=[], clarification='Какой документ?', relevant=True),
                       Plan(query='Погода', topics=[], keywords=[], clarification='', relevant=False)):
            self.assertEqual(enrich_plan(source, build_profile_context(state()), source.query), source)

    def test_invalid_types_and_embedded_contact_fields_are_not_forwarded(self):
        value = {'answers': {'activity': 'Написать secret@example.test', 'region': {'name': 'Казань'},
                             'employees': True, 'revenue': '-5', 'expenses': float('nan'), 'clients': ['Физлица']},
                 'registration': {'age': True, 'date': '2026-02-30', 'citizenship': ['ru']},
                 'selected_regime': ['npd'], 'tax_options': {'new_ip': 'yes', 'year': '2026', 'payroll': 'NaN'},
                 'okved': {'primary': {'code': '62.01 email'}, 'additional': '62.02'},
                 'timezone': 'No/Such_Zone'}
        self.assertEqual(build_profile_context(value), {})


if __name__ == '__main__':
    unittest.main()

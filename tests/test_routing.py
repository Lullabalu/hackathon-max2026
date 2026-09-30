import unittest

from ip_bot.routing import context_plan, local_plan


class LocalPlanTests(unittest.TestCase):
    def test_explicit_registration_questions(self):
        for question in ['Какие документы нужны для регистрации ИП?',
                         'Хочу открыть ИП, как подать заявление?',
                         'Как зарегистрироваться в качестве индивидуального предпринимателя?',
                         'Как стать индивидуальным предпринимателем?',
                         'Где взять форму Р21001?', 'P21001', 'Открыть ИП']:
            with self.subTest(question=question):
                plan = local_plan(question)
                self.assertEqual(plan.topics, ['registration'])
                self.assertEqual(plan.query, question)
                self.assertTrue(plan.relevant)
                self.assertEqual(plan.clarification, '')

    def test_closing_and_changes(self):
        for question, topic in [
            ('Как закрыть ИП?', 'closing'),
            ('Закрытие ИП', 'closing'),
            ('Какие документы нужны для прекращения деятельности ИП?', 'closing'),
            ('Что писать в Р26001?', 'closing'),
            ('Как добавить ОКВЭД?', 'changes'),
            ('Добавить ОКВЭД', 'changes'),
            ('Как изменить сведения в ЕГРИП?', 'changes'),
            ('Как поменять адрес ИП?', 'changes'),
            ('Где найти Р24001?', 'changes'),
        ]:
            with self.subTest(question=question):
                self.assertEqual(local_plan(question).topics, [topic])

    def test_explicit_tax_regimes(self):
        for question, topic in [
            ('Как перейти на УСН?', 'taxes'),
            ('Как перейти на упрощённую систему налогообложения?', 'taxes'),
            ('Как ИП получить патент?', 'taxes'),
            ('Как перейти на ПСН?', 'taxes'),
            ('Как выбрать налоговый режим ИП?', 'taxes'),
            ('Какие документы нужны самозанятому?', 'self_employed'),
            ('Кто может применять НПД?', 'self_employed'),
            ('Как закрыть самозанятость?', 'self_employed'),
        ]:
            with self.subTest(question=question):
                self.assertEqual(local_plan(question).topics, [topic])

    def test_mixed_question_keeps_both_topics(self):
        plan = local_plan('Как открыть ИП и перейти на УСН?')
        self.assertEqual(plan.topics, ['registration', 'taxes'])
        self.assertIn('УСН', plan.keywords)
        self.assertIsNone(local_plan('Как открыть ИП, перейти на УСН и потом закрыть ИП?'))

    def test_context_dependent_questions_are_not_guessed_from_profile(self):
        for question in ['А куда его подавать?', 'Какие документы мне нужны?',
                         'Какие документы нужны для ИП?', 'А по УСН?', 'А если на НПД?',
                         'Куда его подавать при регистрации ИП?', 'Продолжай',
                         'Что дальше?', 'А такой патент нужен ИП?',
                         'А для ИП на УСН?', 'А если ИП на УСН?',
                         'Можно было совмещать это с НПД в Пермском крае в 2024 году?']:
            with self.subTest(question=question):
                self.assertIsNone(local_plan(question, {'region': 'Москва', 'business': 'ИП'}))

    def test_off_topic_and_ambiguous_patent_are_not_tax_searches(self):
        for question in ['Кто ты?', 'Привет, расскажи о себе', 'Напиши сказку',
                         'Сколько стоит патент на изобретение?',
                         'Как закрыть вклад?', 'Как открыть дверь?',
                         'Расскажи не про регистрацию ИП, а про футбол',
                         'Как выбрать ОКВЭД?', 'Что такое ИП?']:
            with self.subTest(question=question):
                self.assertIsNone(local_plan(question))

    def test_region_and_period_are_preserved_without_inferred_facts(self):
        question = 'Какие документы нужны для регистрации ИП в Казани в октябре 2026 года?'
        plan = local_plan(question, {'region': 'Москва'})
        self.assertEqual(plan.query, question)
        self.assertNotIn('Москва', plan.query)
        self.assertNotIn('Р21001', plan.keywords)

    def test_explicit_form_identifiers_are_preserved_in_keywords(self):
        for question in ['Форма P24001 для ИП', 'Нужна форма р 24001']:
            with self.subTest(question=question):
                self.assertIn('Р24001', local_plan(question).keywords)

    def test_incomplete_or_overlong_questions_are_not_truncated(self):
        for question in ['', ' ', 'УСН', 'А патент?', 'Регистрация ИП ' + 'в регионе ' * 60]:
            with self.subTest(question=question):
                self.assertIsNone(local_plan(question))


class ContextPlanTests(unittest.TestCase):
    def history(self, question='Где взять Р21001?', answer='Форма Р21001 приведена на странице ФНС.'):
        return [{'question': question, 'answer': answer}]

    def test_clear_singular_form_followups_need_no_model(self):
        for question, expected in [
            ('А куда его подавать?', 'Куда подавать форму Р21001?'),
            ('Куда подавать её?', 'Куда подавать форму Р21001?'),
            ('Как его отправить?', 'Как подать форму Р21001?'),
            ('А тогда когда его подать?', 'В какой срок подавать форму Р21001?'),
            ('В какие сроки подавать ее?', 'В какой срок подавать форму Р21001?'),
            ('Как долго его рассматривают?', 'Сколько времени рассматривают форму Р21001?'),
            ('Сколько стоит его подать?', 'Сколько стоит подача формы Р21001?'),
            ('Какая госпошлина за ее подачу?', 'Какая госпошлина за подачу формы Р21001?'),
            ('Какие документы к нему приложить?', 'Какие документы приложить к форме Р21001?'),
            ('Какие документы нужны для ее подачи?', 'Какие документы нужны для подачи формы Р21001?'),
            ('А где ее скачать?', 'Где взять форму Р21001?'),
        ]:
            with self.subTest(question=question):
                plan = context_plan(question, self.history())
                self.assertEqual(plan.topics, ['registration'])
                self.assertTrue(plan.query.startswith(expected))
                self.assertIn('Р21001', plan.keywords)
                self.assertTrue(plan.relevant)
                self.assertEqual(plan.clarification, '')

    def test_topic_comes_from_actual_form(self):
        for number, topic in [('24001', 'changes'), ('26001', 'closing')]:
            with self.subTest(number=number):
                history = self.history(f'Где найти P{number}?', f'Форма Р{number} на сайте.')
                plan = context_plan('Куда его подавать?', history)
                self.assertEqual(plan.topics, [topic])
                self.assertIn('Р' + number, plan.query)

    def test_general_document_list_does_not_establish_singular_subject(self):
        history = self.history('Какие документы нужны для регистрации ИП?',
                               'Подготовьте Р21001, паспорт и уведомление по форме 26.2-1.')
        self.assertIsNone(context_plan('А куда его подавать?', history))
        history = self.history('Какие документы нужны для регистрации ИП?', 'Форма Р21001.')
        self.assertIsNone(context_plan('А куда его подавать?', history))

    def test_form_number_does_not_hide_another_document_subject(self):
        for previous in ['Нужен ли паспорт при подаче Р21001?',
                         'Где взять Р21001 и квитанцию?',
                         'Как подать Р21001 вместе с договором?',
                         'Где скачать уведомление и форму Р21001?',
                         'Как заполнить Р21001 и декларацию?',
                         'Где взять копию паспорта для Р21001?',
                         'Где найти инструкцию, чтобы заполнить Р21001?',
                         'Нужна справка для Р21001?', 'Нужна ли Р21001?']:
            with self.subTest(previous=previous):
                self.assertIsNone(context_plan('А куда его подавать?', self.history(previous)))

    def test_direct_form_action_keeps_fast_context_route(self):
        for previous in ['Где взять Р21001?', 'Где найти форму Р21001?',
                         'Как скачать бланк Р21001?', 'Как заполнить форму Р21001?',
                         'Как подать заявление по форме Р21001?']:
            with self.subTest(previous=previous):
                self.assertEqual(context_plan('А куда его подавать?', self.history(previous)).topics,
                                 ['registration'])

    def test_two_forms_and_mixed_topics_are_ambiguous(self):
        histories = [
            self.history('Где взять Р21001 и Р26001?', 'Список форм.'),
            self.history(answer='Здесь Р21001 и Р24001.'),
            self.history(answer='Форма Р21001 и форма 26.2-1.'),
            self.history(answer='Р21001 и документ по КНД 1150001.'),
            self.history('Где взять Р21001 и как перейти на УСН?', 'Р21001.'),
            self.history('Где взять Р24001?', 'Р24001.') + self.history(),
            self.history('Как перейти на УСН?', 'Порядок применения УСН.') + self.history(),
        ]
        for history in histories:
            with self.subTest(history=history):
                self.assertIsNone(context_plan('А куда его подавать?', history))

    def test_new_region_period_conditions_and_regime_are_not_guessed(self):
        for question in ['А куда его подавать в Казани?', 'А куда его подавать в 2027 году?',
                         'А если я на УСН?', 'А для ИП на патенте?',
                         'А куда его подавать, если мне нет 18 лет?',
                         'А сколько это стоит?', 'Какие документы мне нужны?',
                         'А куда их подавать?', 'А куда подавать?', 'Кто ты?',
                         'А куда его подавать? И какой налог платить?',
                         'Не подскажешь, куда его подавать?']:
            with self.subTest(question=question):
                self.assertIsNone(context_plan(question, self.history()))

    def test_region_and_period_in_history_are_preserved(self):
        previous = 'Где взять Р21001 для регистрации ИП в Казани в 2027 году?'
        plan = context_plan('А куда его подавать?', self.history(previous))
        self.assertIn(previous, plan.query)
        self.assertNotIn('Москва', plan.query)
        # A current profile may have changed: reconcile rich old conditions
        # through the planner rather than copy them into a new personal query.
        self.assertIsNone(context_plan('А куда его подавать?', self.history(previous), {'region': 'Москва'}))

    def test_bare_form_followup_can_use_current_profile_without_old_conditions(self):
        plan = context_plan('А куда его подавать?', self.history('Как заполнить Р21001?'),
                            {'region': 'Москва', 'revenue': '2000000'})
        self.assertIsNotNone(plan)
        self.assertIn('Р21001', plan.query)
        self.assertEqual(plan.topics, ['registration'])
        self.assertNotIn('заполнить', plan.query)

    def test_two_users_do_not_share_context_and_inputs_are_unchanged(self):
        first = self.history()
        second = self.history('Где взять Р26001?', 'Форма Р26001 на сайте ФНС.')
        original = [dict(item) for item in first]
        self.assertEqual(context_plan('А куда его подавать?', first).topics, ['registration'])
        self.assertEqual(context_plan('А куда его подавать?', second).topics, ['closing'])
        self.assertIsNone(context_plan('А куда его подавать?', []))
        self.assertEqual(context_plan('А куда его подавать?', first).topics, ['registration'])
        self.assertEqual(first, original)

    def test_unsupported_or_invalid_context_falls_back(self):
        for history in [None, [], [{}], [{'question': None}], [{'question': 'Р21001', 'answer': None}],
                        self.history('Где взять Р99999?', 'Форма Р99999.'),
                        self.history('Где взять Р21001? ' + 'Уточнение условий. ' * 40)]:
            with self.subTest(history=history):
                self.assertIsNone(context_plan('Куда его подавать?', history))

    def test_repeated_questions_with_same_form_keep_single_subject(self):
        history = self.history() + self.history('Как заполнить форму Р21001?', 'Форма Р21001 доступна на сайте.')
        self.assertEqual(context_plan('Куда его подавать?', history).topics, ['registration'])


if __name__ == '__main__':
    unittest.main()

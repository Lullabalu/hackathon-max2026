"""Acceptance conversations: the assistant asks the next question locally."""

import base64
import copy
from datetime import date, timedelta
from decimal import Decimal
import io
import json
import unittest
from unittest.mock import Mock, patch

from pypdf import PdfReader

from ip_bot.assistant import Assistant
from ip_bot.privacy import inbound_text
from ip_bot.settings import Settings


class GuidedConversationTests(unittest.TestCase):
    def setUp(self):
        self.model = Mock()
        self.model.generate.side_effect = AssertionError('Направляемый диалог не вызывает модель')
        self.researcher = Mock()
        self.researcher.search.side_effect = AssertionError('Направляемый диалог не обращается к сети')
        self.assistant = self.make_assistant()
        self.state = None

    def make_assistant(self):
        return Assistant(None, Settings(), model=self.model, researcher=self.researcher)

    def tearDown(self):
        self.model.generate.assert_not_called()
        self.assertEqual(self.researcher.mock_calls, [])

    def say(self, text, *, stage=None, field=None):
        previous = copy.deepcopy(self.state)
        self.assertFalse(self.assistant.needs_research(self.state, text), text)
        reply = self.assistant.respond(self.state, text)
        self.assertEqual(self.state, previous, 'Входное состояние меняется до фиксации ответа: ' + text)
        self.assertTrue(reply.text, text)
        self.assertLessEqual(len(reply.text), 3900, text)
        # Each turn crosses the same JSON boundary as persisted dialog state.
        self.state = json.loads(json.dumps(reply.state, ensure_ascii=False))
        if stage is not None:
            self.assertEqual(self.state['guide']['stage'], stage, reply.text)
            self.assertTrue(self.state['guide']['active'])
        if field is not None:
            self.assertEqual(self.state['guide']['field'], field, reply.text)
        return reply

    def start_activity(self, activity='Разработка сайтов', preference='НПД'):
        self.say('/consent', stage='goal')
        self.assertTrue(self.state['consent']['accepted'])
        self.say('3', stage='activity')
        self.say(activity, stage='okved_confirm')
        self.say('Да, подходит', stage='preference')
        self.say(preference, stage='profile', field='region')

    def fill_profile(self, clients='Физлица'):
        for value, field in [('Москва', 'revenue'), ('1,2 млн', 'expenses'),
                             ('200 тыс', 'employees'), ('нет сотрудников', 'clients')]:
            self.say(value, stage='profile', field=field)
        self.say(clients, stage='refine', field='new_ip')
        answers = self.state['answers']
        self.assertEqual(Decimal(answers['revenue']), Decimal('1200000'))
        self.assertEqual(Decimal(answers['expenses']), Decimal('200000'))
        self.assertEqual(answers['employees'], 0)

    def npd_choice(self):
        self.start_activity()
        self.fill_profile()
        self.say('да', stage='refine', field='npd_activity_eligible')
        self.say('да', stage='refine', field='npd_deduction_remaining')
        self.say('0', stage='regime_choice')
        self.assertNotIn('selected_regime', self.state)

    def registration(self):
        self.npd_choice()
        self.say('НПД', stage='registration')
        self.assertEqual(self.state['selected_regime'], 'npd')
        self.assertEqual(self.state['flow'], {'kind': 'registration', 'index': 0})

    def test_cakes_to_pdf_steps_egrip_and_two_separate_opt_outs(self):
        with patch.object(self.assistant.journey, 'service_command', wraps=self.assistant.journey.service_command) as service:
            self.say('/consent', stage='goal')
            self.say('Подготовиться к регистрации', stage='activity')
            reply = self.say('Продаю торты', stage='activity_clarify')
            self.assertIn('перепрода', reply.text)
            self.assertNotIn('okved', self.state)
            self.assertNotIn('region', self.state['answers'])
            self.say('Пеку сам на заказ', stage='okved_confirm')
            self.assertEqual(self.state['guide']['suggestion']['primary']['code'], '10.71.2')
            self.assertNotIn('okved', self.state, 'Предложение кода ещё не подтверждено пользователем')
            self.say('1', stage='preference')
            self.assertEqual(self.state['okved']['primary']['code'], '10.71.2')
            self.say('Пока не знаю, помогите подобрать', stage='profile', field='region')
            self.fill_profile()
            self.say('/skip', stage='refine', field='npd_activity_eligible')
            self.assertNotIn('new_ip', self.state.get('tax_options', {}))
            reply = self.say('показать расчёт', stage='regime_choice')
            self.assertIn('Предварительное сравнение', reply.text)
            self.assertNotIn('selected_regime', self.state)
            self.say('УСН Доходы', stage='registration')
            self.assertEqual(self.state['selected_regime'], 'usn_income')
            self.say('1', stage='registration')
            self.say('25', stage='registration')
            reply = self.say('2', stage='after_checklist')
            self.assertEqual(self.state['registration'], {'citizenship': 'ru', 'age': 25, 'method': 'mfc'})
            self.assertNotIn('flow', self.state)
            self.assertIn('МФЦ', reply.text)
            self.assertNotIn('800', reply.text)
            service.assert_not_called()

            reply = self.say('Скачать чек-лист PDF', stage='after_pdf')
            self.assertEqual(len(reply.files), 1)
            content = base64.b64decode(reply.files[0]['content'], validate=True)
            pdf = PdfReader(io.BytesIO(content))
            pdf_text = '\n'.join(page.extract_text() for page in pdf.pages)
            self.assertIn('Р21001', pdf_text)
            self.assertIn('МФЦ', pdf_text)
            self.assertIn('не отправлены', reply.text)
            self.say('Пройти регистрацию по шагам', stage='walk')
            for index in range(1, 6):
                reply = self.say('Готово' if index % 2 else '/next', stage='walk')
                self.assertEqual(self.state['registration_step'], index)
                self.assertIn('не отправляет', reply.text)
            self.say('Готово', stage='filing')
            self.say('Подал, жду результат', stage='waiting')
            self.assertNotIn('date', self.state['registration'])
            self.say('Получил регистрацию ИП', stage='registered_date')
            registered = date.today() - timedelta(days=1)
            reply = self.say(registered.strftime('%d.%m.%Y'), stage='reminder_offer')
            self.assertEqual(self.state['registration']['date'], registered.isoformat())
            self.assertIn('с ваших слов', reply.text)
            self.assertFalse(self.state.get('reminders_enabled'))
            service.assert_not_called()
            self.say('Нет, без напоминаний', stage='news_offer')
            self.assertFalse(self.state.get('reminders_enabled'))
            self.say('Нет, без новостей', stage='feedback')
            self.say('пропустить')
            self.assertFalse(self.state['guide']['active'])
            service.assert_not_called()

    def test_preferred_npd_is_not_selected_before_confirmation_or_details(self):
        self.npd_choice()
        answers = copy.deepcopy(self.state['answers'])
        reply = self.say('подробнее НПД', stage='regime_choice')
        self.assertIn('НПД', reply.text)
        self.assertEqual(self.state['answers'], answers)
        self.assertNotIn('selected_regime', self.state)
        self.say('/continue', stage='regime_choice')
        # The displayed number must select the same regime as its full label.
        choice = str(self.state['guide']['offered_regimes'].index('npd') + 1)
        self.say(choice, stage='registration')
        self.assertEqual(self.state['selected_regime'], 'npd')
        self.assertEqual(self.state['okved']['primary']['code'], '62.01')
        self.assertEqual(self.state['tax_options']['previous_revenue'], '0')

    def test_back_corrects_previous_profile_answer_and_preserves_activity(self):
        self.start_activity()
        self.say('Москва', stage='profile', field='revenue')
        self.say('1,2 млн', stage='profile', field='expenses')
        self.say('/back', stage='profile', field='revenue')
        self.assertNotIn('revenue', self.state['answers'])
        self.assertEqual(self.state['answers']['region'], 'Москва')
        self.assertEqual(self.state['okved']['primary']['code'], '62.01')
        self.say('900 тыс', stage='profile', field='expenses')
        self.assertEqual(Decimal(self.state['answers']['revenue']), Decimal('900000'))
        self.say('0', stage='profile', field='employees')
        self.say('без сотрудников', stage='profile', field='clients')
        self.say('ИП и организации', stage='refine', field='new_ip')
        self.assertEqual(self.state['answers']['clients'], 'ИП и организации')

    def test_registration_interruptions_pause_and_restart_resume_same_question(self):
        self.registration()
        self.say('РФ', stage='registration')
        expected_flow = copy.deepcopy(self.state['flow'])
        self.assertEqual(expected_flow['index'], 1)
        for question, expected in [('Нужна ли касса?', 'касса'), ('/faq account', 'счёт'), ('Кто ты?', 'помощник')]:
            with self.subTest(question=question):
                reply = self.say(question, stage='registration')
                self.assertIn(expected, reply.text.casefold())
                self.assertEqual(self.state['flow'], expected_flow)
                self.assertEqual(self.state['registration'], {'citizenship': 'ru'})
        self.say('/cancel')
        self.assertFalse(self.state['guide']['active'])
        # A newly constructed Assistant must resume from persisted state alone.
        self.assistant = self.make_assistant()
        reply = self.say('/continue', stage='registration')
        self.assertEqual(self.state['flow'], expected_flow)
        self.assertIn('лет', reply.text)
        self.say('25', stage='registration')
        self.say('/back', stage='registration')
        self.assertEqual(self.state['flow']['index'], 1)
        self.say('26', stage='registration')
        self.say('МФЦ', stage='after_checklist')
        self.assertEqual(self.state['registration']['age'], 26)
        self.assertEqual(self.state['registration']['method'], 'mfc')

    def test_profile_change_rechecks_npd_and_keeps_unrelated_answers(self):
        self.registration()
        old_answers = copy.deepcopy(self.state['answers'])
        reply = self.say('Возьму сотрудника')
        self.assertEqual(self.state['answers'], {**old_answers, 'employees': 1})
        self.assertNotIn('selected_regime', self.state)
        self.assertNotIn('flow', self.state)
        self.assertTrue(self.state['guide']['active'])
        self.assertIn(self.state['guide']['stage'], {'refine', 'regime_choice'})
        if self.state['guide']['stage'] == 'refine':
            self.assertEqual(self.state['guide']['field'], 'employer_contributions')
            reply = self.say('показать расчёт', stage='regime_choice')
        row = next(row for row in self.state['tax_comparison']['rows'] if row['id'] == 'npd')
        self.assertEqual(row['status'], 'excluded')
        self.assertNotIn('npd', self.state['guide']['offered_regimes'])
        self.assertIn('режим', reply.text.casefold())

    def test_registration_date_needs_user_confirmation_and_rejects_future(self):
        self.registration()
        self.say('получил регистрацию ИП', stage='registered_date')
        self.assertNotIn('flow', self.state)
        future = (date.today() + timedelta(days=1)).isoformat()
        self.say(future, stage='registered_date')
        self.assertNotIn('date', self.state.get('registration', {}))
        self.say('ещё не зарегистрировался', stage='filing')
        self.assertNotIn('date', self.state.get('registration', {}))
        self.assertFalse(self.state.get('reminders_enabled'))

    def test_selecting_regime_leaves_old_tax_flow_and_reuses_registration_fields(self):
        self.registration()
        for answer in ('РФ', '25', 'онлайн'):
            self.say(answer)
        self.say('/tax_setup', stage='tax')
        self.assertEqual(self.state['flow']['kind'], 'tax')
        self.say('/regime usn_income', stage='after_checklist')
        self.assertEqual(self.state['selected_regime'], 'usn_income')
        self.assertNotIn('flow', self.state)
        self.assertEqual(self.state['registration']['method'], 'online')

    def test_separate_users_and_resumed_profile_do_not_share_progress(self):
        self.start_activity()
        self.say('Москва', stage='profile', field='revenue')
        first_user = copy.deepcopy(self.state)
        self.state = None
        self.say('/consent', stage='goal')
        self.say('3', stage='activity')
        self.say('Шиномонтаж', stage='okved_confirm')
        self.say('Да, подходит', stage='preference')
        self.assertEqual(self.state['okved']['primary']['code'], '45.20')
        self.assertNotIn('region', self.state['answers'])
        self.state = first_user
        self.assistant = self.make_assistant()
        self.say('/continue', stage='profile', field='revenue')
        self.assertEqual(self.state['okved']['primary']['code'], '62.01')
        self.assertEqual(self.state['answers']['region'], 'Москва')
        self.say('500000', stage='profile', field='expenses')
        self.assertEqual(Decimal(self.state['answers']['revenue']), Decimal('500000'))

    def test_control_commands_are_not_saved_as_required_business_answers(self):
        self.say('/consent', stage='goal')
        self.say('3', stage='activity')
        for text in ('/next', '/prev', '/skip'):
            with self.subTest(stage='activity', text=text):
                self.say(text, stage='activity')
                self.assertEqual(self.state['answers'], {})
        self.say('Разработка сайтов', stage='okved_confirm')
        self.say('1', stage='preference')
        self.say('НПД', stage='profile', field='region')
        for text in ('/skip', '/next', '/prev', 'пропустить'):
            with self.subTest(stage='profile', text=text):
                self.say(text, stage='profile', field='region')
                self.assertNotIn('region', self.state['answers'])
        self.say('Москва', stage='profile', field='revenue')
        for text in ('много', '-100', 'завтра'):
            self.say(text, stage='profile', field='revenue')
            self.assertNotIn('revenue', self.state['answers'])

    def test_user_who_knows_basics_can_start_business_without_menu_loop(self):
        for text in ('Я в курсе, давай к делу', 'Хочу открыть ИП'):
            with self.subTest(text=text):
                self.state = None
                self.say('/consent', stage='goal')
                reply = self.say(text, stage='activity')
                self.assertIn('Чем', reply.text)
                self.assertEqual(self.state['answers'], {})
                self.say('Шиномонтаж', stage='okved_confirm')

    def test_explicit_regime_command_advances_to_registration(self):
        self.npd_choice()
        self.say('/regime npd', stage='registration')
        self.assertEqual(self.state['selected_regime'], 'npd')
        self.assertEqual(self.state['flow'], {'kind': 'registration', 'index': 0})
        self.say('/continue', stage='registration')
        self.say('1', stage='registration')
        self.assertEqual(self.state['registration']['citizenship'], 'ru')

    def test_one_employee_in_words_is_not_treated_as_working_alone(self):
        self.start_activity()
        self.say('Москва', stage='profile', field='revenue')
        self.say('1200000', stage='profile', field='expenses')
        self.say('200000', stage='profile', field='employees')
        self.say('один', stage='profile', field='clients')
        self.assertEqual(self.state['answers']['employees'], 1)
        self.say('Физлица', stage='refine', field='new_ip')
        self.say('показать расчёт', stage='regime_choice')
        row = next(row for row in self.state['tax_comparison']['rows'] if row['id'] == 'npd')
        self.assertEqual(row['status'], 'excluded')
        self.assertNotIn('npd', self.state['guide']['offered_regimes'])

    def test_tax_setup_interruption_then_set_and_okved_keep_route_consistent(self):
        self.registration()
        self.say('/tax_setup', stage='tax')
        self.say('да', stage='tax')
        expected_flow = {'kind': 'tax', 'index': 1}
        self.assertEqual(self.state['flow'], expected_flow)
        self.say('/faq npd', stage='tax')
        self.assertEqual(self.state['flow'], expected_flow)
        self.say('/cancel')
        self.say('/continue', stage='tax')
        self.assertEqual(self.state['flow'], expected_flow)
        self.say('/set доход 1000000', stage='regime_choice')
        self.assertNotIn('flow', self.state)
        self.assertEqual(Decimal(self.state['answers']['revenue']), Decimal('1000000'))
        self.say('/okved Продаю торты', stage='activity_clarify')
        self.assertNotIn('selected_regime', self.state)
        self.assertNotIn('okved', self.state)
        self.assertNotIn('npd_activity_eligible', self.state['tax_options'])
        self.say('Пеку сам', stage='okved_confirm')
        self.say('Да, подходит', stage='preference')
        self.say('НПД', stage='refine', field='npd_activity_eligible')
        self.assertEqual(self.state['okved']['primary']['code'], '10.71.2')
        self.assertEqual(self.state['answers']['region'], 'Москва')
        self.say('/set деятельность Разработка сайтов', stage='okved_confirm')
        self.say('1', stage='refine', field='npd_activity_eligible')
        self.assertEqual(self.state['okved']['primary']['code'], '62.01')
        self.assertNotIn('selected_regime', self.state)

    def test_personal_answers_wait_for_explicit_consent_in_persistent_mode(self):
        services = Mock()
        services.published.return_value = None
        self.assistant.journey.services = services
        reply = self.say('/start')
        self.assertIn('/consent', reply.text)
        self.assertIsNone(self.state)
        for text in ('3', 'Продаю торты', 'Иван Иванов', '/register', '/continue'):
            safe_text = inbound_text(self.state, text)
            self.assertEqual(safe_text, '/consent_required')
            reply = self.say(safe_text)
            self.assertIn('/consent', reply.text)
            self.assertIsNone(self.state)
        self.assertEqual(services.mock_calls, [])
        self.say(inbound_text(self.state, '/consent'), stage='goal')
        self.assertTrue(self.state['consent']['accepted'])
        self.say('3', stage='activity')
        self.say('Разработка сайтов', stage='okved_confirm')

    def test_back_and_continue_from_comparison_keep_a_valid_next_question(self):
        self.start_activity(preference='Пока не знаю, помогите подобрать')
        self.fill_profile()
        self.say('показать расчёт', stage='regime_choice')
        self.say('/back')
        guide = copy.deepcopy(self.state['guide'])
        self.say('/continue', stage=guide['stage'])
        self.assertEqual(self.state['guide'].get('field'), guide.get('field'))
        # Returning from the abbreviated comparison must still accept the next answer.
        if self.state['guide']['stage'] == 'profile':
            self.assertEqual(self.state['guide']['field'], 'clients')
            self.say('Физлица', stage='refine', field='new_ip')
        elif self.state['guide']['stage'] == 'refine':
            self.say('/skip')
        else:
            self.fail('Возврат из сравнения должен вести к предыдущему вопросу анкеты')


if __name__ == '__main__':
    unittest.main()

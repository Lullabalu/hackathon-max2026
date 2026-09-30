import base64
import copy
import io
import json
import unittest
from unittest.mock import Mock

from pypdf import PdfReader

from ip_bot.assistant import Assistant
from ip_bot.documents import make_pdf, registration_checklist
from ip_bot.journey import Journey
from ip_bot.privacy import inbound_text
from ip_bot.settings import Settings


PROFILE = {'answers': {'activity': 'Разработка сайтов', 'region': 'Москва', 'revenue': '1200000',
                      'expenses': '200000', 'employees': 0, 'clients': 'Физлица'},
           'consent': {'accepted': True}, 'paused': True}


class LocalJourneyTests(unittest.TestCase):
    def setUp(self):
        self.model = Mock()
        self.model.generate.side_effect = AssertionError('Сценарий не должен вызывать модель')
        self.researcher = Mock()
        self.researcher.search.side_effect = AssertionError('Сценарий не должен обращаться к сети')
        self.assistant = Assistant(None, Settings(), model=self.model, researcher=self.researcher)

    def reply(self, state, text):
        old = copy.deepcopy(state)
        reply = self.assistant.respond(state, text)
        self.assertEqual(state, old, 'Нельзя менять состояние до фиксации ответа')
        self.assertLessEqual(len(reply.text), 3900)
        json.dumps(reply.state, ensure_ascii=False)
        return reply

    def test_public_introduction_and_consent(self):
        self.assistant.journey.services = Mock()
        self.assertIn('/consent', self.reply(None, '/start').text)
        self.assertIn('НПД', self.reply(None, '/intro').text)
        self.assertIn('/consent', self.reply(None, '/begin').text)
        state = self.reply(None, '/consent').state
        self.assertTrue(state['consent']['accepted'])
        self.assertIn('Вопрос 1', self.reply(state, '/begin').text)

    def test_business_survey_finishes_with_comparison_and_no_model(self):
        state = self.reply(None, '/begin').state
        for answer in ('Разработка сайтов', 'Москва', '1200000', '200000', '0', '1'):
            result = self.reply(state, answer)
            state = result.state
        self.assertIn('tax_comparison', state)
        self.assertIn('НПД', result.text)
        self.assertEqual(len(state['tax_comparison']['rows']), 7)
        self.assertTrue(state['paused'])

    def test_partial_profile_with_out_of_order_fields_still_asks_missing_field(self):
        state = {'answers': {'region': 'Москва'}}
        result = self.reply(state, '/begin')
        self.assertIn('Чем планируете', result.text)
        result = self.reply(result.state, 'Разработка сайтов')
        self.assertEqual(result.state['answers']['activity'], 'Разработка сайтов')
        self.assertIn('доход', result.text)

    def test_back_from_completed_survey_accepts_correction_without_model(self):
        state = self.reply(PROFILE, '/taxes').state
        state = self.reply(state, '/back').state
        self.assertNotIn('clients', state['answers'])
        result = self.reply(state, '2')
        self.assertEqual(result.state['answers']['clients'], 'ИП и организации')
        self.assertIn('tax_comparison', result.state)
        self.model.generate.assert_not_called()

    def test_activity_change_invalidates_selected_regime(self):
        state = self.reply(PROFILE, '/regime npd').state
        result = self.reply(state, '/okved Перепродаю готовые торты в магазине')
        self.assertNotIn('selected_regime', result.state)
        self.assertNotIn('tax_comparison', result.state)
        self.assertIn('проверить заново', result.text)

    def test_patent_limits_apply_to_saved_selection(self):
        state = copy.deepcopy(PROFILE)
        state['answers']['employees'] = 16
        result = self.reply(state, '/regime psn')
        self.assertNotIn('selected_regime', result.state)
        self.assertIn('исключён', result.text)

    def test_resale_is_not_recommended_as_npd_after_profile_change(self):
        state = copy.deepcopy(PROFILE)
        state['tax_options'] = {'npd_activity_eligible': True, 'npd_deduction_remaining': '0', 'usn_income_rate': '6', 'new_ip': True}
        state = self.reply(state, '/okved Перепродаю готовые торты в магазине').state
        result = self.reply(state, '/taxes')
        row = next(row for row in result.state['tax_comparison']['rows'] if row['id'] == 'npd')
        self.assertEqual(row['status'], 'excluded')
        self.assertNotIn('npd_activity_eligible', state['tax_options'])

    def test_clients_and_region_changes_invalidate_dependent_options(self):
        state = copy.deepcopy(PROFILE)
        state['tax_options'] = {'client_person_share': '1', 'npd_activity_eligible': True, 'usn_income_rate': '1', 'patent_cost': '20000'}
        result = self.reply(state, '/set clients 2')
        self.assertNotIn('client_person_share', result.state['tax_options'])
        self.assertNotIn('npd_activity_eligible', result.state['tax_options'])
        result = self.reply(result.state, '/set регион Самарская область')
        self.assertNotIn('usn_income_rate', result.state['tax_options'])
        self.assertNotIn('patent_cost', result.state['tax_options'])

    def test_unsupported_tax_year_cannot_save_a_regime(self):
        state = copy.deepcopy(PROFILE)
        state['tax_options'] = {'year': 2027}
        result = self.reply(state, '/regime npd')
        self.assertNotIn('selected_regime', result.state)
        self.assertIn('Выбор не сохранён', result.text)

    def test_okved_clarification_and_registration_progress(self):
        state = self.reply(PROFILE, '/okved Разработка сайтов').state
        self.assertEqual(state['okved']['primary']['code'], '62.01')
        state = self.reply(state, '/register').state
        for answer in ('1', '25', '2'):
            result = self.reply(state, answer)
            state = result.state
        self.assertEqual(state['registration']['method'], 'mfc')
        self.assertIn('не взимается', result.text)
        self.assertNotIn('800', result.text)
        self.assertNotIn('flow', state)
        state = self.reply(state, '/steps').state
        self.assertEqual(self.reply(state, '/next').state['registration_step'], 1)
        self.assertIn('МФЦ', self.reply({**state, 'registration_step': 3}, '/steps').text)

    def test_foreign_minor_checklist_is_specific_and_has_sources(self):
        state = {**PROFILE, 'registration': {'citizenship': 'foreign', 'age': 17, 'method': 'paper'}}
        result = self.reply(state, '/checklist')
        self.assertIn('проживания', result.text)
        self.assertIn('Несовершеннолетнему', result.text)
        self.assertIn('800', result.text)
        self.assertIn('nalog.gov.ru', result.text)

    def test_hiring_invalidates_npd_and_retains_other_answers(self):
        state = self.reply(PROFILE, '/regime npd').state
        self.assertEqual(state['selected_regime'], 'npd')
        result = self.reply(state, 'Возьму сотрудника')
        self.assertNotIn('selected_regime', result.state)
        self.assertEqual(result.state['answers']['employees'], 1)
        self.assertEqual(result.state['answers']['region'], 'Москва')
        self.assertIn('исключён', result.text)

    def test_revenue_update_recalculates_and_reset_preserves_consent(self):
        result = self.reply(PROFILE, 'Доход вырастет до 5 млн')
        self.assertEqual(result.state['answers']['revenue'], '5000000')
        self.assertEqual(next(x for x in result.state['tax_comparison']['rows'] if x['id'] == 'npd')['status'], 'excluded')
        state = self.reply(result.state, '/new').state
        self.assertEqual(state['answers'], {})
        self.assertTrue(state['consent']['accepted'])

    def test_flow_back_cancel_and_skip_clear_old_tax_option(self):
        state = self.reply(PROFILE, '/register').state
        state = self.reply(state, '2').state
        state = self.reply(state, '/back').state
        state = self.reply(state, '1').state
        self.assertEqual(state['registration']['citizenship'], 'ru')
        state = self.reply(state, '/cancel').state
        self.assertNotIn('flow', state)
        state.update(flow={'kind': 'tax', 'index': 3}, tax_options={'npd_deduction_remaining': '9000'})
        self.assertNotIn('npd_deduction_remaining', self.reply(state, 'пропустить').state['tax_options'])

    def test_checklist_pdf_embeds_cyrillic_and_does_not_call_model(self):
        result = self.reply(PROFILE, '/pdf')
        data = base64.b64decode(result.files[0]['content'], validate=True)
        pdf = PdfReader(io.BytesIO(data))
        text = '\n'.join(page.extract_text() for page in pdf.pages)
        self.assertIn('Москва', text)
        self.assertIn('Разработка сайтов', text)
        self.assertIn('Р21001', text)
        self.assertGreater(len(data), 10000)

    def test_pdf_pagination_and_hostile_characters_remain_plain_text(self):
        data = make_pdf('Проверка', '<script>не исполняется</script>\n' + 'Строка документа\n' * 180)
        pdf = PdfReader(io.BytesIO(data))
        self.assertGreaterEqual(len(pdf.pages), 3)
        self.assertIn('<script>', pdf.pages[0].extract_text())

    def test_faq_uses_published_revision(self):
        services = Mock()
        services.published.return_value = {'payload': [{'id': 'new', 'question': 'Новый вопрос?', 'answer': 'Проверенный ответ', 'sources': ['https://npd.nalog.ru/']}]}
        journey = Journey(services)
        self.assertIn('Проверенный ответ', journey.handle(PROFILE, '/faq new', user_id=1).text)
        self.assertNotIn('/faq account', journey.handle(PROFILE, '/faq', user_id=1).text)

    def test_input_redaction_and_public_short_answers(self):
        for text in ('Иван Иванов', 'Мой паспорт 12 34 567890', '/set доход 2000000'):
            self.assertEqual(inbound_text(None, text), '/consent_required')
        for text in ('/consent', '/delete', 'Кто ты?', '/intro', '/lesson 1'):
            self.assertEqual(inbound_text(None, text), text)
        self.assertEqual(inbound_text(PROFILE, 'Москва'), 'Москва')

    def test_long_personalized_checklist_stays_inside_max_limit(self):
        state = copy.deepcopy(PROFILE)
        state['answers'].update(activity='А' * 300, region='Б' * 100)
        state['registration'] = {'citizenship': 'foreign', 'age': 17, 'method': 'representative'}
        self.assertLessEqual(len(registration_checklist(state)), 3900)


if __name__ == '__main__':
    unittest.main()

import copy
import unittest

from ip_bot.documents import STEPS, step_text


class RegistrationStepsTests(unittest.TestCase):
    def test_uc06_order_starts_after_business_survey(self):
        self.assertEqual(len(STEPS), 6)
        titles = [step_text(index).splitlines()[0] for index in range(6)]
        for title, expected in zip(titles, ('Р21001', 'подпись', 'Подайте', 'ЕГРИП', 'налоговый режим', 'счётом')):
            self.assertIn(expected, title)
        self.assertNotIn('ОКВЭД', titles[0])

    def test_first_step_offers_draft_or_fns_and_keeps_blank_fields_clear(self):
        text = step_text(0)
        for expected in ('/form', '/form pdf', 'частичный', 'не готов к подаче', 'Паспорт', 'адрес', 'service.nalog.ru'):
            self.assertIn(expected, text)
        usn = step_text(0, {'selected_regime': 'usn_income'})
        self.assertIn('вместе с документами', usn)
        self.assertIn('не откладывая до шага 5', usn)

    def test_signature_and_submission_follow_all_five_methods(self):
        expected = {'online': ('Госключ', 'Самостоятельно отправьте'),
                    'mfc': ('МФЦ', 'Передайте документы в выбранный МФЦ'),
                    'paper': ('личную подачу на бумаге', 'лично в регистрирующий орган'),
                    'representative': ('нотариальную доверенность', 'своему представителю'),
                    'notary': ('нотариуса', 'нотариусу электронную передачу')}
        for method, (signature, submission) in expected.items():
            with self.subTest(method=method):
                state = {'registration': {'method': method}}
                self.assertIn(signature, step_text(1, state))
                self.assertIn(submission, step_text(2, state))
                self.assertIn('расписк', step_text(2, state).lower())
        mfc = step_text(1, {'registration': {'method': 'mfc'}}) + step_text(2, {'registration': {'method': 'mfc'}})
        self.assertIn('госпошлина за регистрацию не требуется', mfc)
        self.assertNotIn('800', mfc)

    def test_missing_method_does_not_guess_online(self):
        for method in (None, 'unknown'):
            for index in (1, 2):
                with self.subTest(method=method, index=index):
                    text = step_text(index, {'registration': {'method': method}})
                    self.assertIn('/register', text)
                    self.assertIn('ещё не указан', text)
                    self.assertNotIn('Вы выбрали онлайн', text)

    def test_egrip_result_is_checked_by_user_and_can_be_refused(self):
        for method in ('online', 'mfc', 'paper', 'representative', 'notary'):
            with self.subTest(method=method):
                text = step_text(3, {'registration': {'method': method}})
                self.assertIn('ЕГРИП', text)
                self.assertIn('Если пришёл отказ', text)
                self.assertIn('Бот статус в ФНС не проверяет', text)
                self.assertIn('/registered ГГГГ-ММ-ДД', text)
        self.assertIn('МФЦ', step_text(3, {'registration': {'method': 'mfc'}}))

    def test_fifth_step_uses_saved_regime(self):
        cases = {'npd': ('НПД', 'Мой налог'),
                 'usn_income': ('УСН «Доходы»', '30 календарных дней'),
                 'usn_profit': ('УСН «Доходы минус расходы»', 'объект налогообложения'),
                 'psn': ('ПСН', 'отдельное заявление'),
                 'ausn_income': ('АУСН «Доходы»', 'уполномоченный банк'),
                 'ausn_profit': ('АУСН «Доходы минус расходы»', 'уполномоченный банк'),
                 'osno': ('ОСНО', 'не подаёт уведомлений')}
        for regime, expected in cases.items():
            with self.subTest(regime=regime):
                text = step_text(4, {'selected_regime': regime})
                for phrase in expected:
                    self.assertIn(phrase, text)
                self.assertNotIn('ещё не сохранён', text)
        self.assertIn('ещё не сохранён', step_text(4))
        self.assertIn('/regime', step_text(4))

    def test_bank_is_optional_and_reminders_need_a_choice(self):
        text = step_text(5)
        for expected in ('необязательное', 'можно пропустить', 'Бот счёт не открывает',
                         'банку не передаёт', '/reminders', 'по вашему решению'):
            self.assertIn(expected, text)

    def test_every_step_has_next_action_fits_max_and_does_not_change_profile(self):
        state = {'registration': {'method': 'representative', 'age': 17, 'citizenship': 'stateless'},
                 'selected_regime': 'usn_profit', 'registration_step': 0,
                 'form': {'surname': 'СИНТЕТИЧЕСКИЙ'}}
        before = copy.deepcopy(state)
        for index in range(6):
            text = step_text(index, state)
            self.assertIn('?', text)
            self.assertIn('«Готово»', text)
            self.assertIn('/next', text)
            self.assertIn('/prev', text)
            self.assertIn('не подписывает и не отправляет документы', text)
            self.assertNotIn('СИНТЕТИЧЕСКИЙ', text)
            self.assertLessEqual(len(text), 3900)
        self.assertEqual(state, before)
        self.assertEqual(step_text(-1), step_text(0))
        self.assertEqual(step_text(99), step_text(5))


if __name__ == '__main__':
    unittest.main()

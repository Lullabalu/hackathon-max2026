"""The manual performance check also rejects fast but unhelpful replies."""

import copy
import unittest

from scripts.benchmark import answer_metrics, check_fee_answer, check_registration_answer, check_summary_case


POINT = 'При электронном направлении документов через МФЦ госпошлину платить не нужно.'
REPLY = POINT + ' [1]\n\nИсточники:\n[1] https://www.nalog.gov.ru/example/'


def summary_case(reply=REPLY):
    return {'checks': [], 'reply': reply, 'model_calls': 1,
            'model_stages': [{'schema': 'Summary', 'success': True, 'profile_seen': True,
                              'result': {'points': [{'text': POINT, 'source': 1, 'evidence': 1}],
                                         'clarification': ''}}],
            **answer_metrics(reply)}


class BenchmarkQualityTests(unittest.TestCase):
    def test_short_grounded_summary_passes_including_fee_condition(self):
        case = summary_case()
        check_summary_case(case, expected_model_calls=1)
        check_fee_answer(case)
        self.assertTrue(all(item['passed'] for item in case['checks']))

    def test_timeout_is_not_a_successful_fast_reply(self):
        case = summary_case('Не успел подготовить подтверждённый ответ за отведённое время.')
        case['model_stages'][0]['success'] = False
        with self.assertRaises(AssertionError):
            check_summary_case(case)

    def test_successful_generation_rejected_by_renderer_is_not_counted_as_summary(self):
        case = summary_case('Найденные источники:\n[1] https://www.nalog.gov.ru/example/')
        with self.assertRaisesRegex(AssertionError, 'Пользователь получил пункт'):
            check_summary_case(case)

    def test_missing_evidence_can_be_an_honest_clarification(self):
        case = summary_case('В найденных материалах недостаточно данных для ответа.')
        case['model_stages'][0]['result'] = {'points': [], 'clarification': 'Нужны данные формы.'}
        check_summary_case(case, allow_clarification=True)
        with self.assertRaises(AssertionError):
            check_summary_case(copy.deepcopy(case))

    def test_large_source_copy_fails_even_without_quote_marks(self):
        copied = 'Сведения из официального источника. ' * 14
        case = summary_case(REPLY + '\n' + copied)
        case.update(answer_metrics(case['reply'], [{'text': copied}]))
        self.assertGreater(case['longest_source_copy_characters'], 360)
        with self.assertRaisesRegex(AssertionError, 'больших цитат'):
            check_summary_case(case)

    def test_quote_and_size_measurements_exclude_no_visible_text(self):
        reply = 'Ответ «короткая цитата».\n> другая цитата'
        metrics = answer_metrics(reply)
        self.assertEqual(metrics['reply_characters'], len(reply))
        self.assertEqual(metrics['longest_quote_characters'], len('короткая цитата'))
        self.assertEqual(metrics['longest_source_copy_characters'], 0)
        case = summary_case(REPLY + ' ' + 'а' * 3600)
        with self.assertRaisesRegex(AssertionError, '3600'):
            check_summary_case(case)

    def test_fee_answer_must_retain_electronic_condition_and_direct_conclusion(self):
        for reply in ('Госпошлину через МФЦ платить не нужно. [1]',
                      'Документы направляются электронно. Сведения о госпошлине в источнике. [1]'):
            with self.subTest(reply=reply), self.assertRaises(AssertionError):
                check_fee_answer(summary_case(reply))

    def test_summary_must_receive_profile_and_model_calls_are_bounded(self):
        for change in ('profile', 'model_calls'):
            case = summary_case()
            if change == 'profile':
                case['model_stages'][0]['profile_seen'] = False
            else:
                case['model_calls'] = 7
            with self.subTest(change=change), self.assertRaises(AssertionError):
                check_summary_case(case)

    def test_mfc_profile_rejects_mandatory_digital_signature_in_varied_wording(self):
        replies = (
            'Для заполнения Р21001 получите УКЭП и подпишите заявление.',
            'Заявление Р21001 подписывается квалифицированной электронной подписью.',
            'Подача через МФЦ: электронная подпись обязательна.',
            'Для подачи заявления через МФЦ вам нужна КЭП.',
            'Не забудьте оформить электронную подпись перед подачей Р21001.',
            'Для электронной подачи требуется УКЭП.',
            'При подаче через МФЦ пошлину не нужно платить, но УКЭП обязательна.',
            'МФЦ принимает документы без квитанции, электронная подпись обязательна.',
        )
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaisesRegex(AssertionError, 'подпись'):
                check_registration_answer(summary_case(reply))

    def test_mfc_profile_rejects_required_switch_of_filing_method(self):
        replies = (
            'Заявление Р21001 необходимо подать онлайн.',
            'Подайте заявление в электронной форме через сайт ФНС.',
            'МФЦ выбран в анкете. Документы отправьте через интернет.',
            'Для регистрации направьте заявление лично в налоговую инспекцию.',
            'Вместо МФЦ можно подать заявление через интернет.',
            'МФЦ не принимает заявление Р21001. Подавайте его в инспекцию.',
            'Подайте заявление через сервис ФНС.',
            'Заявление нужно подавать онлайн.',
            'Для регистрации необходима подача в электронной форме.',
            'Подача заявления допускается только в электронной форме.',
        )
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaisesRegex(AssertionError, 'заменить'):
                check_registration_answer(summary_case(reply))

    def test_mfc_profile_allows_preparation_tools_negation_and_optional_alternatives(self):
        replies = (
            'Р21001 можно заполнить онлайн в сервисе ФНС. Готовое заявление подайте через МФЦ.',
            'Укажите данные в заявлении Р21001. Электронная подпись для МФЦ не нужна.',
            'Подайте документы в МФЦ без УКЭП.',
            'Документы можно подать через МФЦ. Для электронной подачи напрямую в ФНС потребуется УКЭП.',
            'Вы можете подать документы через МФЦ или отправить их онлайн.',
            'МФЦ сам направляет документы в электронной форме. Вам не нужно получать УКЭП.',
            'Укажите ФИО и адрес в заявлении Р21001 по инструкции ФНС.',
        )
        for reply in replies:
            with self.subTest(reply=reply):
                check_registration_answer(summary_case(reply))

    def test_followup_must_explain_filing_destination_not_merely_mention_mfc(self):
        replies = (
            'При подаче через МФЦ госпошлина не уплачивается.',
            'Заполните заявление Р21001. МФЦ работает по расписанию.',
            'Заявление подпишите в присутствии нотариуса.',
            'Подайте заявление в налоговую инспекцию по месту жительства.',
            'Нельзя направлять документы через МФЦ.',
            'Заявление не следует передавать в МФЦ.',
            'В найденных материалах недостаточно данных для ответа.',
            'МФЦ помогает заявителям. [1]\n\nИсточники:\n[1] Подайте заявление через МФЦ\nhttps://www.nalog.gov.ru/',
        )
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaisesRegex(AssertionError, 'куда подать'):
                check_registration_answer(summary_case(reply), followup=True)

    def test_followup_accepts_different_direct_mfc_instructions(self):
        replies = (
            'Заявление Р21001 подайте через МФЦ по месту жительства.',
            'Обратитесь в ближайший многофункциональный центр с заявлением.',
            'Документы принимают в МФЦ.',
            'Заявление подается через отделение МФЦ.',
            'МФЦ принимает заявление Р21001 и передает его в налоговую.',
            'Место подачи — МФЦ.',
            'Форму Р21001 нужно подавать в многофункциональный центр лично.',
            'Заявление следует передавать через МФЦ.',
            'Документы нужно направлять в МФЦ.',
        )
        for reply in replies:
            with self.subTest(reply=reply):
                check_registration_answer(summary_case(reply), followup=True)

    def test_filling_question_rejects_tautological_answer(self):
        for reply in ('Заполните заявление по форме Р21001.',
                      'Заполните заявление (форма Р21001) для регистрации ИП в Казани.',
                      'Для регистрации нужно правильно заполнить Р21001 и подать его в МФЦ.'):
            with self.subTest(reply=reply), self.assertRaisesRegex(AssertionError, 'способ заполнения'):
                check_registration_answer(summary_case(reply), require_filling=True)

    def test_filling_question_accepts_concrete_method_or_fields(self):
        for reply in ('Распечатайте бланк Р21001 и заполните его от руки.',
                      'Р21001 можно сформировать в сервисе ФНС, затем подать через МФЦ.',
                      'Введите ФИО и адрес в соответствующие поля заявления.',
                      'Заявление Р21001 заполняется печатными заглавными буквами.',
                      'Форму Р21001 можно заполнить электронно.'):
            with self.subTest(reply=reply):
                case = summary_case(reply)
                check_registration_answer(case, require_filling=True)
                self.assertEqual(case['filling_answer'], 'instruction')

    def test_honest_filling_clarification_is_distinguished_from_instruction(self):
        case = summary_case('В найденных материалах недостаточно данных для ответа.')
        case['model_stages'][0]['result'] = {'points': [], 'clarification': 'Уточните, какое поле требуется заполнить.'}
        check_registration_answer(case, require_filling=True)
        self.assertEqual(case['filling_answer'], 'clarification')

    def test_fee_exception_is_not_a_directive_to_change_mfc_filing(self):
        reply = (
            'Заявление Р21001 можно заполнить на бумаге или в электронном виде через сервисы подачи документов. [1]\n\n'
            '• При подаче через МФЦ госпошлину не требуется, так как с 01.01.2019 она не уплачивается '
            'при направлении документов в электронной форме. [1]'
        )
        check_registration_answer(summary_case(reply), require_filling=True)
        for instruction in ('Документы отправьте онлайн.',
                            'Вам необходимо подавать заявление в электронной форме.',
                            'Требуется подача через сайт ФНС.'):
            with self.subTest(instruction=instruction), self.assertRaisesRegex(AssertionError, 'заменить'):
                check_registration_answer(summary_case(reply + '\n\n' + instruction), require_filling=True)


if __name__ == '__main__':
    unittest.main()

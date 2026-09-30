"""Направляемый диалог UC01–UC11: один следующий вопрос, без сети и LLM."""
import copy
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from .activities import suggest_activity, format_activity
from .dialog import QUESTIONS, CLIENTS, parse_answer, question, respond
from .models import Reply
from .conversation import strip_social_prefix

CHOICE_MARKER = 'Выберите вариант:'
SKIP = {'пропустить', 'не знаю', 'пока не знаю', '/skip'}
CONTINUE = {'/continue', 'продолжить', 'продолжим', 'дальше', 'что дальше', 'готово'}
GOAL = {'1': 'Расскажите об ИП', '2': 'Подобрать налоговый режим', '3': 'Подготовиться к регистрации'}
REGIMES = [('npd', 'НПД'), ('usn_income', 'УСН Доходы'), ('usn_profit', 'УСН Доходы минус расходы'),
           ('psn', 'Патент'), ('ausn_income', 'АУСН Доходы'), ('ausn_profit', 'АУСН Доходы минус расходы'), ('osno', 'ОСНО')]
REGIME_ALIASES = {'нпд': 'npd', 'самозанятость': 'npd', 'усн': 'usn', 'упрощенка': 'usn',
                  'усн 6': 'usn_income', 'усн 6%': 'usn_income', 'усн доходы': 'usn_income',
                  'усн 15': 'usn_profit', 'усн 15%': 'usn_profit', 'усн доходы минус расходы': 'usn_profit',
                  'псн': 'psn', 'патент': 'psn', 'аусн': 'ausn', 'осно': 'osno'}
REFINEMENTS = {
    'new_ip': 'Это новый бизнес без дохода прошлого года? Ответьте «да» или «нет».',
    'previous_revenue': 'Какой доход бизнеса был в прошлом календарном году, в рублях?',
    'client_person_share': 'Какая доля выручки будет от обычных физлиц? Например, 70% или 0,7.',
    'npd_activity_eligible': 'Для расчёта НПД нужно уточнение: весь доход подходит под условия режима? '
        'В частности, нет перепродажи чужих товаров, запрещённых подакцизных/маркированных товаров, посредничества '
        'и дохода от нынешнего работодателя либо бывшего менее двух лет назад. Работников по трудовым договорам быть не должно. '
        'Проверьте условия: https://npd.nalog.ru/faq/\nОтветьте «да», «нет» или «не знаю».',
    'npd_deduction_remaining': 'Какой остаток вычета НПД доступен в «Мой налог»? От 0 до 10000 ₽. Если ещё не проверяли, напишите «не знаю».',
    'usn_income_rate': 'Какая ставка УСН «Доходы» подтверждена для вашей деятельности и региона? '
        'Укажите процент, например 6. Если не проверяли — «не знаю»: покажу условный расчёт.',
    'usn_profit_rate': 'Какая ставка УСН «Доходы минус расходы» подтверждена для вашей деятельности и региона? '
        'Укажите процент, например 15, или «не знаю».',
    'expenses_confirmed': 'Все указанные расходы подтверждены документами и допустимы для налогообложения? да / нет / не знаю.',
    'expenses_include_contributions': 'Указанная сумма расходов уже включает страховые взносы? да / нет / не знаю.',
    'employer_contributions': 'Какую общую сумму взносов за работников, включая травматизм, ожидаете за год? '
        'Укажите сумму в рублях или «не знаю». Без неё итог с работниками будет неполным.',
    'patent_cost': 'Какова стоимость патента до уменьшения на взносы? '
        'Проверьте региональный расчёт https://patent.nalog.ru/info/ и укажите сумму либо «не знаю».',
    'patent_potential_income': 'Какой потенциально возможный доход установлен для выбранного патента и срока? '
        'Укажите сумму из расчёта ФНС либо «не знаю». Это не ваша фактическая выручка.',
}


def normal(text):
    return ' '.join(text.casefold().replace('ё', 'е').strip().rstrip('.!?').split())


def choice(text, options):
    value = normal(text)
    for key, label in options.items():
        if value in {str(key), normal(label), normal(f'{key} — {label}'), normal(f'{key}. {label}')}:
            return str(key)
    return None


def options_text(options):
    return CHOICE_MARKER + '\n' + '\n'.join(f'{key} — {label}' for key, label in options.items()) + '\n\nМожно ответить номером или словами.'


def business_answer(field, text):
    value = normal(text)
    if field in {'revenue', 'expenses'}:
        matched = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(млн|миллион(?:а|ов)?|тыс\.?|тысяч(?:а|и)?)\s*(?:рублей|руб\.?|₽)?', value)
        if matched:
            amount = Decimal(matched[1].replace(',', '.')) * (1000000 if matched[2].startswith(('млн', 'миллион')) else 1000)
            return parse_answer(field, str(amount))
        return parse_answer(field, re.sub(r'\s*(?:рублей|руб\.?|₽)$', '', value))
    if field == 'employees':
        if value in {'нет', 'нет сотрудников', 'без сотрудников', 'сам', 'работаю один', 'без работников'}:
            return 0
        value = {'один': '1', 'одного': '1', 'два': '2', 'три': '3'}.get(value, value)
        value = re.sub(r'\s+(?:сотрудник\w*|работник\w*|человек\w*)$', '', value)
    if field == 'clients':
        labels = {'физлица': '1', 'физические лица': '1', 'люди': '1', 'обычные люди': '1',
                  'компании': '2', 'юрлица': '2', 'ип и организации': '2', 'оба': '3', 'и те и другие': '3', 'все': '3'}
        value = labels.get(value, choice(text, {'1': 'Физлица', '2': 'ИП и организации', '3': 'И те и другие'}) or value)
    return parse_answer(field, value if field in {'employees', 'clients'} else text)


class Guide:
    def __init__(self, journey):
        self.journey = journey

    @staticmethod
    def active(state):
        return bool((state or {}).get('guide', {}).get('active'))

    @staticmethod
    def is_local(state, text):
        return Guide.active(state) and not text.strip().casefold().startswith('/ask ')

    @staticmethod
    def side_question(state, text):
        if not Guide.active(state) or text.lstrip().startswith('/'):
            return False
        value = normal(strip_social_prefix(text))
        common = {'нужна ли касса', 'нужен ли расчетный счет', 'чем нпд отличается от ип',
                  'можно ли зарегистрироваться по домашнему адресу'}
        if value in common:
            return False
        if re.match(r'^(?:покажи(?:те)?|найди(?:те)?|расскажи(?:те)?|объясни(?:те)?|подскажи(?:те)?|помоги(?:те)?)\s+', value):
            return True
        if re.match(r'^(?:мне )?(?:нужн[аоы]|нужен)\s+(?:документ|заявлен|форм[ауы]|инструкц|справк)', value):
            return True
        if re.match(r'^давай(?:те)?\s+(?:обсудим|поговорим|поболтаем)\s+(?:о|об|про)\s+', value):
            return True
        return bool(re.match(r'^(?:а )?(?:как |какие |какой |какая |когда |почему |зачем |где |куда |(?:нужн[аоы]|нужен) ли |можно ли |что (?:нужно|такое|делать|будет|нового|изменилось|с |по ))', value))

    def new(self, state, stage='goal', **data):
        state.pop('flow', None)
        state['guide'] = {'active': True, 'stage': stage, 'history': [], **data}
        state['paused'] = True
        return state

    def start(self, state, *, directly=False):
        state = copy.deepcopy(state) if state else {'answers': {}}
        if self.active(state):
            return self.start_business(state) if directly else self.prompt(state)
        self.new(state)
        return self.start_business(state) if directly else self.prompt(state)

    def push(self, state):
        guide = state['guide']
        snapshot = {key: copy.deepcopy(state.get(key)) for key in ('answers', 'tax_options', 'okved', 'selected_regime')}
        guide.setdefault('history', []).append({'guide': {key: copy.deepcopy(value) for key, value in guide.items() if key != 'history'}, 'snapshot': snapshot})
        guide['history'] = guide['history'][-16:]

    def move(self, state, stage, *, prefix='', **data):
        state['guide'].update(stage=stage, **data)
        return self.prompt(state, prefix=prefix)

    def prompt(self, state, *, prefix=''):
        guide = state['guide']
        stage = guide['stage']
        choices = None
        if stage == 'goal':
            text = 'С чего начнём? Я могу объяснить основы, помочь выбрать налоговый режим или подготовить регистрацию. '
            choices = GOAL
        elif stage == 'activity':
            text = 'Чем хотите заниматься? Опишите товар или услугу своими словами. Например: «хочу печь торты на заказ» или «открою шиномонтаж».'
        elif stage == 'activity_clarify':
            text = guide['suggestion'].get('question') or 'Вы производите товар сами или перепродаёте готовый?'
        elif stage == 'okved_confirm':
            text = format_activity(guide['suggestion']) + '\n\nЭто соответствует вашему бизнесу?'
            choices = {'1': 'Да, подходит', '2': 'Уточнить деятельность'}
        elif stage == 'okved_unknown':
            text = format_activity(guide['suggestion']) + '\n\nКак продолжим?'
            choices = {'1': 'Продолжить, код проверю в ФНС', '2': 'Уточнить деятельность'}
        elif stage == 'activity_restricted':
            text = format_activity(guide['suggestion']) + '\n\nДля этой деятельности путь регистрации ИП продолжать нельзя. Что выбираете?'
            choices = {'1': 'Другая деятельность', '2': 'Узнать об ООО'}
        elif stage == 'preference':
            text = 'Вы уже выбрали систему налогообложения или помочь сравнить варианты? Проверим выбор по вашей анкете.'
            choices = {'1': 'Пока не знаю, помогите подобрать', **{str(i + 2): label for i, (_, label) in enumerate(REGIMES)}}
        elif stage in {'usn_kind', 'ausn_kind'}:
            text = 'Какой вариант режима рассматриваете?'
            choices = {'1': 'Доходы', '2': 'Доходы минус расходы', '3': 'Сравнить оба'}
        elif stage == 'profile':
            text = question(state.get('answers', {}))
            if guide.get('field') == 'clients':
                text = 'Кто будет платить за товары или услуги?'
                choices = {'1': 'Физлица', '2': 'ИП и организации', '3': 'И те и другие'}
        elif stage == 'refine':
            text = REFINEMENTS[guide['field']] + '\n\nМожно написать «пропустить» или /skip. Чтобы сразу увидеть условный расчёт — «показать расчёт».'
        elif stage == 'regime_choice':
            text = self.comparison_text(state) + '\n\nКакой режим хотите выбрать? Если данных не хватает, можно сначала уточнить расчёт.'
            choices = self.regime_choices(state)
        elif stage == 'after_checklist':
            text = 'Чек-лист готов. Что сделаем дальше?'
            choices = {'1': 'Скачать чек-лист PDF', '2': 'Подготовить черновик Р21001', '3': 'Пройти регистрацию по шагам'}
        elif stage == 'after_pdf':
            text = 'Как продолжим подготовку регистрации?'
            choices = {'1': 'Подготовить черновик Р21001', '2': 'Пройти регистрацию по шагам'}
        elif stage == 'after_form':
            text = 'Черновик нужно дополнить и проверить перед подачей. Продолжим регистрацию?'
            choices = {'1': 'Пройти регистрацию по шагам', '2': 'Открыть сервис ФНС'}
        elif stage == 'filing':
            from .documents import REGISTRATION
            text = 'Откройте официальный сервис и завершите подачу самостоятельно:\n' + REGISTRATION + '\n\nДанные из бота автоматически не передаются. На каком вы этапе?'
            choices = {'1': 'Документы ещё не поданы', '2': 'Подал, жду результат', '3': 'Получил регистрацию ИП'}
        elif stage == 'waiting':
            text = 'Сохраню, что вы сообщили о подаче. Статус в ФНС автоматически не проверяю. '
            text += 'Когда получите лист записи ЕГРИП, сообщите об этом. Что нужно сейчас?'
            choices = {'1': 'Получил регистрацию ИП', '2': 'Проверить документы и шаги', '3': 'Пока остановиться'}
        elif stage == 'registered_date':
            text = 'Какую дату регистрации указали в листе записи ЕГРИП? Напишите ГГГГ-ММ-ДД или ДД.ММ.ГГГГ. '
            text += 'Если результата пока нет, ответьте «ещё не зарегистрировался».'
        elif stage == 'reminder_offer':
            text = 'Включить напоминания по выбранному режиму — за три дня и в день поддерживаемого срока? Это отдельная подписка; её можно отключить.'
            choices = {'1': 'Да, включить напоминания', '2': 'Нет, без напоминаний'}
        elif stage == 'timezone':
            text = 'Какой часовой пояс использовать для напоминаний? Например, Europe/Moscow, Asia/Yekaterinburg или Asia/Novosibirsk.'
            choices = {'1': 'Московское время', '2': 'Укажу другой часовой пояс'}
        elif stage == 'news_offer':
            text = 'Подписать вас на новости и изменения по выбранному налоговому режиму? Рассылку готовит редактор; её можно отключить в любой момент.'
            choices = {'1': 'Да, подписаться на новости', '2': 'Нет, без новостей'}
        elif stage == 'feedback':
            text = 'Насколько полезной была консультация? Оцените от 1 до 5, где 5 — очень полезно, или напишите «пропустить».'
        elif stage == 'walk':
            from .documents import step_text
            text = step_text(state.get('registration_step', 0), state) + '\n\nКогда проверите этот шаг, ответьте «готово». Если уже получили регистрацию, напишите «получил регистрацию ИП».'
        elif stage in {'registration', 'form', 'tax'}:
            if state.get('flow'):
                text = self.journey.flow_question(state['flow'])
            else:
                return self.move(state, 'after_checklist' if stage == 'registration' else 'after_form', prefix=prefix)
        else:
            return self.move(state, 'goal', prefix=prefix)
        if choices:
            text += '\n\n' + options_text(choices)
        text += '\n\n/continue — текущий шаг\n/back — назад\n/cancel — пауза\n/ask вопрос — спросить отдельно.'
        full = (prefix + '\n\n' if prefix else '') + text
        if len(full) > 3900:
            # Содержимое нормативной части не режем: подсказки уже есть в меню.
            full = (prefix + '\n\n' if prefix else '') + text.split('\n\n/continue')[0]
        if len(full) > 3900:
            # Сохраняем справку полностью; следующий вопрос доступен отдельным ходом.
            full = prefix + '\n\n/continue — следующий вопрос.' if len(prefix) <= 3850 else prefix
            full = full[:3900]
        return Reply(state, full)

    def start_business(self, state):
        state.pop('flow', None)
        if state.get('answers', {}).get('activity'):
            if state.get('okved') or state['guide'].get('okved_skipped'):
                return self.after_activity(state)
            return self.activity(state, state['answers']['activity'])
        return self.move(state, 'activity')

    def activity(self, state, text):
        try:
            description = parse_answer('activity', text)
        except ValueError as error:
            return self.prompt(state, prefix=str(error))
        if state.get('answers', {}).get('activity') != description:
            self.journey.invalidate_options(state, 'activity')
            state.pop('okved', None)
            state.pop('selected_regime', None)
            self.journey.refresh_reminders(state)
        state.setdefault('answers', {})['activity'] = description
        result = self.journey.activity_resolver(description, catalog=self.journey.published('activities'))
        target = {'matched': 'okved_confirm', 'clarify': 'activity_clarify', 'unknown': 'okved_unknown', 'restricted': 'activity_restricted'}[result['status']]
        return self.move(state, target, suggestion=result, description=description)

    def after_activity(self, state):
        if not state['guide'].get('preference_asked'):
            return self.move(state, 'preference')
        return self.advance_profile(state)

    def advance_profile(self, state):
        field = next((key for key, _ in QUESTIONS if key not in state.get('answers', {})), None)
        if field:
            return self.move(state, 'profile', field=field)
        return self.advance_refinements(state)

    def refinement_fields(self, state):
        answers, options = state.get('answers', {}), state.get('tax_options', {})
        fields = ['new_ip']
        if options.get('new_ip') is False:
            fields.append('previous_revenue')
        if answers.get('clients') == CLIENTS['3']:
            fields.append('client_person_share')
        prefer = state['guide'].get('preferred_regime')
        can_npd = not answers.get('employees') and Decimal(answers.get('revenue', '0')) <= 2400000
        if can_npd and prefer in {None, 'npd'} and self.journey.tax_inputs(state).get('npd_activity_eligible') is not False:
            fields.append('npd_activity_eligible')
            if options.get('npd_activity_eligible') is True: fields.append('npd_deduction_remaining')
        if prefer in {None, 'usn_income', 'usn_profit'}:
            fields += ['usn_income_rate'] if prefer != 'usn_profit' else []
            if prefer == 'usn_profit' or Decimal(answers.get('expenses', '0')) > 0:
                fields += ['usn_profit_rate', 'expenses_confirmed']
                if options.get('expenses_confirmed') is True: fields.append('expenses_include_contributions')
        if answers.get('employees', 0) > 0:
            fields.append('employer_contributions')
        if prefer == 'psn': fields += ['patent_cost', 'patent_potential_income']
        return fields

    def advance_refinements(self, state):
        skipped = state['guide'].get('skipped', [])
        options = state.get('tax_options', {})
        field = next((field for field in self.refinement_fields(state) if field not in options and field not in skipped), None)
        if field:
            return self.move(state, 'refine', field=field)
        return self.offer_regimes(state)

    def offer_regimes(self, state, *, prefix=''):
        result = self.journey.comparison(state)
        state = result.state
        if 'tax_comparison' not in state:
            return self.advance_profile(state)
        if not self.active(state): self.new(state, 'regime_choice', origin='legacy')
        state['guide']['stage'] = 'regime_choice'
        return self.prompt(state, prefix=prefix)

    def regime_choices(self, state):
        rows = state.get('tax_comparison', {}).get('rows', [])
        ids = [key for key, _ in REGIMES if any(row['id'] == ('patent' if key == 'psn' else key) and row['status'] != 'excluded' and 'year' not in row.get('missing', []) for row in rows)]
        state['guide']['offered_regimes'] = ids
        return {**{str(i + 1): dict(REGIMES)[key] for i, key in enumerate(ids)},
                '8': 'Уточнить данные расчёта', '9': 'Показать подробности режима'}

    def comparison_text(self, state):
        result = state['tax_comparison']
        lines = [f"Предварительное сравнение за {result['year']} год. Это оценка при указанных условиях, а не подтверждение права на режим."]
        for row in result['rows']:
            if row['status'] == 'excluded':
                value = 'исключён: ' + row.get('reason', '')[:170]
            elif row.get('total') is not None:
                value = row['total'] + ' ₽' + ('; нужны уточнения' if row.get('missing') else ' при указанных условиях')
            elif row.get('range'):
                interval = row['range']
                value = str(interval.get('min', interval.get('low', '?'))) + '–' + str(interval.get('max', interval.get('high', '?'))) + ' ₽; условный диапазон'
            elif row.get('subtotal') is not None:
                value = str(row['subtotal']) + ' ₽ — часть суммы, не полный итог'
            else:
                value = 'нужны дополнительные данные'
            lines.append(row['title'] + ': ' + value)
        recommendation = result.get('recommendation')
        lines.append(recommendation['text'] if recommendation else 'Для надёжного рейтинга данных пока недостаточно. Можно уточнить расчёт или сохранить свой предварительный выбор.')
        lines.append('НПД/АУСН без фиксированных взносов за себя. Региональные условия, состав расходов, НДС и взносы работников могут менять результат. /taxes — все условия; /tax режим — подробности.')
        lines.append('Правила проверены: ' + result['verified_on'] + '. https://www.nalog.gov.ru/new2026/')
        return '\n\n'.join(lines)

    def choose_regime(self, state, regime):
        if regime in {'usn', 'ausn'}:
            return self.move(state, regime + '_kind', selecting=True)
        reply = self.journey.select_regime(state, regime)
        if reply.state.get('selected_regime') != regime:
            return self.offer_regimes(reply.state, prefix=reply.text)
        state = reply.state
        self.new(state, 'registration')
        return self.begin_registration(state, prefix='Вы выбрали ' + dict(REGIMES)[regime] + '. Теперь подготовим документы.')

    def begin_registration(self, state, *, prefix=''):
        from .journey import REG_FIELDS
        fields = state.get('registration', {})
        missing = next((i for i, (key, _) in enumerate(REG_FIELDS) if key not in fields), None)
        if missing is None:
            return self.after_flow(Reply(state, ''), registration=True)
        state['guide'].update(stage='registration')
        state['flow'] = {'kind': 'registration', 'index': missing}
        return self.prompt(state, prefix=prefix)

    def after_flow(self, reply, *, registration=False):
        state = reply.state
        if not self.active(state):
            self.new(state, 'after_checklist' if registration else 'after_form')
        if state.get('flow'):
            return reply
        stage = state['guide']['stage']
        if registration or stage == 'registration':
            from .documents import registration_checklist
            return self.move(state, 'after_checklist', prefix=registration_checklist(state).replace('\nДокументы никуда не отправлены. /pdf — скачать чек-лист; /steps — пошаговое прохождение.', ''))
        if stage == 'form':
            follow = self.move(state, 'after_form', prefix=reply.text)
            follow.files = reply.files
            return follow
        return reply

    def start_walk(self, state):
        state.pop('flow', None)
        state['registration_step'] = state.get('registration_step', 0)
        return self.move(state, 'walk')

    def handle(self, state, text):
        low = normal(text)
        if low in {'/continue', 'продолжить', 'продолжим'} and state.get('guide'):
            state['guide']['active'] = True
            paused_flow = state['guide'].pop('paused_flow', None)
            if paused_flow: state['flow'] = paused_flow
            return self.prompt(state)
        if not self.active(state): return None
        stage = state['guide']['stage']
        if low in {'получил регистрацию', 'получил регистрацию ип', 'получил егрип', 'ип зарегистрирован', '/registered'}:
            state.pop('flow', None)
            return self.move(state, 'registered_date')
        if low == '/back' and state.get('flow'):
            return None
        if low in {'/back', 'назад'}:
            history = state['guide'].get('history', [])
            if history:
                frame = history.pop()
                for key, value in frame['snapshot'].items():
                    if value is None: state.pop(key, None)
                    else: state[key] = value
                state.pop('tax_comparison', None)
                state['guide'] = {**frame['guide'], 'history': history}
                self.journey.refresh_reminders(state)
                return self.prompt(state)
            if state['guide'].get('origin') == 'legacy':
                state['guide']['active'] = False
                field = next((key for key, _ in reversed(QUESTIONS) if key in state.get('answers', {})), None)
                self.journey.invalidate_options(state, field)
                updated, answer = respond(state, '/back')
                updated['paused'] = False
                return Reply(updated, answer)
            return self.prompt(state, prefix='Это начало текущего этапа. Можно изменить параметры через /profile или /set.')
        if low.startswith('/') and low not in {'/skip', '/next', '/prev'}:
            return None
        if low == '/skip' and stage != 'refine' or low in {'/next', '/prev'} and stage != 'walk':
            return self.prompt(state, prefix='Сейчас нужен ответ на текущий вопрос. /cancel — приостановить.')
        if low in SKIP and stage in {'profile', 'activity', 'activity_clarify', 'registration', 'form'}:
            return self.prompt(state, prefix='Это обязательный параметр текущей анкеты. Ответьте на вопрос или выберите /cancel, чтобы вернуться позже.')
        if low in {'меню', 'я в курсе', 'я в курсе, давай к делу', 'давай к делу', 'хочу открыть ип'}:
            return self.start_business(state)
        if stage in {'registration', 'form', 'tax'}:
            return None  # Валидация существующего опроса и /back остаются в Journey.
        if low.startswith(('подробнее ', 'покажи подробнее ', 'расчёт ', 'расчет ')):
            name = re.sub(r'^(?:покажи )?(?:подробнее|расчет)\s+', '', low)
            regime = REGIME_ALIASES.get(name, name)
            if regime in dict(REGIMES):
                reply = self.journey.handle(state, '/tax ' + regime, user_id=self.journey.user_id)
                reply.text += '\n\n/continue — вернуться к выбору.' if len(reply.text) < 3840 else ''
                return reply
        if stage == 'goal':
            selected = choice(text, GOAL)
            if selected == '1':
                from .journey import INTRO
                return self.prompt(state, prefix=INTRO)
            if selected in {'2', '3'}:
                state['guide']['goal'] = 'register' if selected == '3' else 'compare'
                return self.start_business(state)
            if re.match(r'^(?:я )?(?:хочу|буду|планирую|открою|делаю|разрабатываю|продаю|пеку|сдаю|ремонтирую)\b', low):
                return self.activity(state, text)
            return self.prompt(state, prefix='Выберите, с чего начать, или опишите свой бизнес.')
        if stage in {'activity', 'activity_clarify'}:
            if not 2 <= len(text.strip()) <= 300:
                return self.prompt(state, prefix='Опишите деятельность в пределах 2–300 символов.')
            self.push(state)
            description = text if stage == 'activity' else state['guide'].get('description', '') + ' ' + text
            if len(description) > 300:
                return self.move(state, 'activity', prefix='Объединённое описание слишком длинное. Напишите заново товар или услугу и уточнение, до 300 символов.')
            return self.activity(state, description)
        if stage in {'okved_confirm', 'okved_unknown', 'activity_restricted'}:
            options = ({'1': 'Да, подходит', '2': 'Уточнить деятельность'} if stage == 'okved_confirm' else
                       {'1': 'Продолжить, код проверю в ФНС', '2': 'Уточнить деятельность'} if stage == 'okved_unknown' else
                       {'1': 'Другая деятельность', '2': 'Узнать об ООО'})
            selected = choice(text, options)
            if low in {'да', 'подходит', 'продолжить'}: selected = '1'
            if low in {'нет', 'не подходит', 'уточнить'}: selected = '2'
            if not selected: return self.prompt(state, prefix='Подтвердите вариант или уточните деятельность.')
            self.push(state)
            if stage == 'activity_restricted':
                prefix = 'ООО — отдельное юридическое лицо. Этот бот ведёт регистрацию ИП; информацию об ООО проверьте здесь: https://www.nalog.gov.ru/create_business/ul/' if selected == '2' else ''
                return self.move(state, 'activity', prefix=prefix)
            if selected == '2': return self.move(state, 'activity')
            if stage == 'okved_confirm': state['okved'] = state['guide']['suggestion']
            else: state['guide']['okved_skipped'] = True
            return self.after_activity(state)
        if stage == 'preference':
            options = {'1': 'Пока не знаю, помогите подобрать', **{str(i + 2): label for i, (_, label) in enumerate(REGIMES)}}
            selected = choice(text, options)
            regime = REGIME_ALIASES.get(low)
            if selected == '1' or low in SKIP or low in {'подберите', 'помоги подобрать', 'помогите подобрать'}:
                regime = None
            elif selected:
                regime = REGIMES[int(selected) - 2][0]
            elif regime is None:
                return self.prompt(state, prefix='Можно назвать режим или ответить «пока не знаю».')
            self.push(state)
            state['guide'].update(preferred_regime=regime, preference_asked=True)
            if regime in {'usn', 'ausn'}: return self.move(state, regime + '_kind', selecting=False)
            return self.advance_profile(state)
        if stage in {'usn_kind', 'ausn_kind'}:
            selected = choice(text, {'1': 'Доходы', '2': 'Доходы минус расходы', '3': 'Сравнить оба'})
            if not selected: return self.prompt(state)
            regime = None if selected == '3' else stage.split('_')[0] + ('_income' if selected == '1' else '_profit')
            if state['guide'].get('selecting'):
                return self.offer_regimes(state) if regime is None else self.choose_regime(state, regime)
            state['guide']['preferred_regime'] = regime
            return self.advance_profile(state)
        if stage == 'profile':
            field = state['guide']['field']
            try: value = business_answer(field, text)
            except ValueError as error: return self.prompt(state, prefix=str(error))
            self.push(state)
            self.journey.invalidate_options(state, field)
            state.setdefault('answers', {})[field] = value
            return self.advance_profile(state)
        if stage == 'refine':
            if low in {'показать расчет', 'сравнить сейчас', 'без уточнений'}:
                return self.offer_regimes(state)
            field = state['guide']['field']
            if low in SKIP:
                self.push(state)
                state['guide'].setdefault('skipped', []).append(field)
            else:
                from .journey import parse_extra
                value = text.strip().rstrip('%').strip()
                if field == 'client_person_share' and text.strip().endswith('%'):
                    try: value = str(Decimal(value.replace(',', '.')) / 100)
                    except InvalidOperation: return self.prompt(state, prefix='Укажите процент или долю от 0 до 1.')
                try: value = parse_extra(field, value)
                except ValueError as error: return self.prompt(state, prefix=str(error))
                self.push(state)
                state.setdefault('tax_options', {})[field] = value
                if field == 'new_ip' and value is True: state['tax_options']['previous_revenue'] = '0'
            return self.advance_refinements(state)
        if stage == 'regime_choice':
            options = self.regime_choices(state)
            selected = choice(text, options)
            if selected == '8':
                state['guide']['skipped'] = []
                return self.advance_refinements(state)
            if selected == '9':
                return self.prompt(state, prefix='Напишите, например, «подробнее НПД» или «подробнее УСН доходы».')
            if selected and int(selected) <= len(state['guide']['offered_regimes']):
                return self.choose_regime(state, state['guide']['offered_regimes'][int(selected) - 1])
            regime = REGIME_ALIASES.get(re.sub(r'^(?:выбираю|хочу|давайте)\s+', '', low))
            if regime:
                return self.choose_regime(state, regime)
            return self.prompt(state, prefix='Назовите режим, выберите номер или попросите подробнее о конкретном варианте.')
        if stage in {'after_checklist', 'after_pdf', 'after_form'}:
            options = ({'1': 'Скачать чек-лист PDF', '2': 'Подготовить черновик Р21001', '3': 'Пройти регистрацию по шагам'} if stage == 'after_checklist' else
                       {'1': 'Подготовить черновик Р21001', '2': 'Пройти регистрацию по шагам'} if stage == 'after_pdf' else
                       {'1': 'Пройти регистрацию по шагам', '2': 'Открыть сервис ФНС'})
            selected = choice(text, options)
            if not selected: return self.prompt(state)
            label = options[selected]
            if label == 'Скачать чек-лист PDF':
                reply = self.journey.pdf(state)
                follow = self.move(state, 'after_pdf', prefix=reply.text)
                follow.files = reply.files
                return follow
            if label == 'Подготовить черновик Р21001':
                state['guide']['stage'] = 'form'
                return self.journey.handle(state, '/form', user_id=self.journey.user_id)
            if label == 'Открыть сервис ФНС': return self.move(state, 'filing')
            return self.start_walk(state)
        if stage == 'walk':
            if low in {'/prev'}:
                state['registration_step'] = max(0, state.get('registration_step', 0) - 1)
                return self.prompt(state)
            if low in CONTINUE | {'/next'}:
                if state.get('registration_step', 0) < 5:
                    state['registration_step'] = state.get('registration_step', 0) + 1
                    return self.prompt(state)
                return self.move(state, 'filing')
            return self.prompt(state, prefix='Отметьте «готово», когда проверите текущий шаг. Фактическую подачу и регистрацию я сам не подтверждаю.')
        if stage in {'filing', 'waiting'}:
            options = {'1': 'Документы ещё не поданы', '2': 'Подал, жду результат', '3': 'Получил регистрацию ИП'} if stage == 'filing' else {'1': 'Получил регистрацию ИП', '2': 'Проверить документы и шаги', '3': 'Пока остановиться'}
            selected = choice(text, options)
            if not selected: return self.prompt(state)
            if options[selected] == 'Получил регистрацию ИП': return self.move(state, 'registered_date')
            if options[selected] == 'Подал, жду результат': return self.move(state, 'waiting')
            if options[selected] == 'Пока остановиться':
                state['guide']['active'] = False
                return Reply(state, 'Остановились здесь. /continue — продолжить, когда будет удобно.')
            return self.start_walk(state)
        if stage == 'registered_date':
            if low in {'еще не зарегистрировался', 'пока нет', 'нет'}: return self.move(state, 'filing')
            try:
                from datetime import datetime
                registered = datetime.strptime(text.strip(), '%d.%m.%Y').date() if '.' in text else date.fromisoformat(text.strip())
                if registered > date.today() or registered.year < 2000: raise ValueError
            except ValueError:
                return self.prompt(state, prefix='Нужна прошедшая или сегодняшняя дата, например 30.09.2026.')
            state.setdefault('registration', {})['date'] = registered.isoformat()
            self.journey.refresh_reminders(state)
            if not state.get('selected_regime'):
                return self.offer_regimes(state, prefix='Дату сохранил с ваших слов. Для напоминаний сначала выберите налоговый режим.')
            return self.move(state, 'reminder_offer', prefix='Дату сохранил с ваших слов. Статус в ФНС автоматически не проверялся.')
        if stage == 'reminder_offer':
            selected = choice(text, {'1': 'Да, включить напоминания', '2': 'Нет, без напоминаний'})
            if low in {'да', 'включить'}: selected = '1'
            if low in {'нет', 'не надо'}: selected = '2'
            if selected == '1': return self.move(state, 'timezone')
            if selected == '2': return self.move(state, 'news_offer', prefix='Новые напоминания не включаю.')
            return self.prompt(state)
        if stage == 'timezone':
            selected = choice(text, {'1': 'Московское время', '2': 'Укажу другой часовой пояс'})
            if selected == '2': return self.prompt(state, prefix='Напишите IANA-название часового пояса, например Asia/Yekaterinburg.')
            value = 'Europe/Moscow' if selected == '1' else text.strip()
            reply = self.journey.service_command(state, '/timezone', value)
            if reply.state.get('timezone') != value: return self.prompt(state, prefix=reply.text)
            enabled = self.journey.service_command(reply.state, '/reminders', 'on')
            return self.move(enabled.state, 'news_offer', prefix=enabled.text)
        if stage == 'news_offer':
            selected = choice(text, {'1': 'Да, подписаться на новости', '2': 'Нет, без новостей'})
            if low in {'да', 'подписаться'}: selected = '1'
            if low in {'нет', 'не надо'}: selected = '2'
            if selected == '1':
                reply = self.journey.service_command(state, '/subscribe', '')
                return self.move(reply.state, 'feedback', prefix=reply.text)
            if selected == '2': return self.move(state, 'feedback', prefix='Новую подписку на новости не включаю.')
            return self.prompt(state)
        if stage == 'feedback':
            if low in SKIP:
                reply = Reply(state, 'Продолжим, когда понадобится помощь.')
            elif low in {'1', '2', '3', '4', '5'}:
                reply = self.journey.service_command(state, '/feedback', low)
            else: return self.prompt(state)
            reply.state['guide']['active'] = False
            reply.text += '\n\n/profile — ваши параметры; /ask вопрос — помощь; /start — новый маршрут. Если условия бизнеса изменятся, напишите об этом.'
            return reply
        return self.prompt(state)

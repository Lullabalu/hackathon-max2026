"""Сценарии регистрации: действия пользователя, расчёты и документы без LLM."""

import base64
import copy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .activities import suggest_activity, format_activity
from .dialog import QUESTIONS, LABELS, parse_answer, profile, question
from .documents import registration_checklist, step_text, make_pdf, form_draft, REGISTRATION
from .models import Reply
from .privacy import CONSENT_VERSION, PRIVACY, has_consent
from .tax import compare_taxes, render_comparison, render_tax_details
from .guided import Guide, choice, CHOICE_MARKER
from .personal import local_personal_intent

MENU = (
    'Помогу пройти путь от идеи бизнеса до регистрации ИП.\n\n'
    '/intro — что такое ИП\n/okved — подобрать ОКВЭД\n'
    '/begin — анкета бизнеса\n/taxes — сравнить налоги\n'
    '/register — подготовиться к регистрации\n/checklist — документы под вашу ситуацию\n'
    '/steps — пройти регистрацию по шагам\n/faq — частые вопросы\n'
    '/reminders — напоминания\n/help — все команды'
)
INTRO = (
    'ИП — физическое лицо, зарегистрированное как предприниматель. '
    'Это статус, а НПД («самозанятость») — налоговый режим: его могут применять и подходящие ИП. '
    'ООО — отдельное юридическое лицо.\n\n'
    'ИП ведёт учёт, платит предусмотренные выбранным режимом налоги и соблюдает требования к деятельности. '
    'Нанимать работников можно с учётом режима; общего ограничения «100 работников для любого ИП» нет. '
    'Для части деятельности нужны лицензии или уведомления, часть доступна только организациям. '
    'Проверим именно вашу деятельность через /okved.\n\n'
    'Для иностранного гражданина требуется право временного или постоянного проживания в РФ; '
    'документы зависят от ситуации. /register — уточнить.\n\n'
    'Уже знаете основы? /begin — подбор режима, /register — регистрация.\n'
    'https://www.nalog.gov.ru/create_business/ip/\nhttps://npd.nalog.ru/'
)
REGIME_NAMES = {'npd': 'НПД', 'usn_income': 'УСН Доходы', 'usn_profit': 'УСН Доходы минус расходы',
                'psn': 'ПСН', 'ausn_income': 'АУСН Доходы', 'ausn_profit': 'АУСН Доходы минус расходы', 'osno': 'ОСНО'}
HELP = MENU + (
    '\n\n/profile — профиль\n/set поле значение — исправить параметры (например /set доход 2000000)'
    '\n/tax_setup — уточнения для расчёта\n/regime — выбрать режим\n/pdf — PDF-чек-лист'
    '\n/tax идентификатор — подробности расчёта режима'
    '\n/form — черновик данных Р21001\n/online — официальный сервис регистрации'
    '\n/registered ГГГГ-ММ-ДД — записать дату регистрации\n/remind ГГГГ-ММ-ДД ЧЧ:ММ текст — своё напоминание'
    '\n/timezone Europe/Moscow — часовой пояс\n/subscribe — новости выбранного режима\n/unsubscribe — отключить новости'
    '\n/feedback 1..5 — оценить консультацию\n/consultant — контакт консультанта'
    '\n/lessons — короткие уроки об ИП'
    '\n/back — предыдущий ответ\n/cancel — выйти из текущего сценария\n/ask вопрос — поиск в источниках'
    '\n/privacy — сохранение данных\n/delete — удалить данные и подписки'
)
REG_FIELDS = [('citizenship', 'Ваше гражданство?\n1 — РФ\n2 — иностранное гражданство\n3 — без гражданства'),
              ('age', 'Сколько вам полных лет? Дату рождения и паспорт присылать не нужно.'),
              ('method', 'Как планируете подать документы?\n1 — онлайн\n2 — МФЦ\n3 — лично на бумаге\n4 — представитель\n5 — нотариус')]
FORM_FIELDS = [('surname', 'Фамилия для черновика Р21001:'), ('name', 'Имя:'),
               ('patronymic', 'Отчество, либо «нет»:'), ('inn', 'ИНН из 12 цифр, либо «нет», если не знаете:'),
               ('email', 'Email для получения результата регистрации:')]
TAX_FIELDS = [('new_ip', 'Это новый бизнес без доходов прошлого года? Ответьте да или нет.'),
              ('previous_revenue', 'Какой доход был в прошлом календарном году? Для нового бизнеса укажите 0.'),
              ('client_person_share', 'Какая доля выручки приходится на физлиц? Число от 0 до 1, например 0,7.'),
              ('npd_deduction_remaining', 'Какой остаток вычета НПД доступен в «Мой налог»? От 0 до 10000. «Пропустить», если не знаете.'),
              ('usn_income_rate', 'Какая ставка УСН Доходы подтверждена для вашей деятельности и региона? В процентах. «Пропустить», если не проверяли.'),
              ('usn_profit_rate', 'Какая ставка УСН Доходы минус расходы подтверждена? В процентах. «Пропустить», если не проверяли.'),
              ('expenses_confirmed', 'Все указанные расходы подтверждены документами и допустимы для выбранного режима? да / нет / пропустить'),
              ('expenses_include_contributions', 'Введённые расходы уже включают страховые взносы? да / нет / пропустить')]
TAX_FIELDS.append(('npd_activity_eligible', 'Весь указанный доход подходит для НПД? Подтвердите по https://npd.nalog.ru/: '
                   'в частности, нет перепродажи чужих товаров, запрещённых подакцизных/маркированных товаров, '
                   'посредничества, дохода от текущего работодателя или бывшего менее двух лет назад. '
                   'Работников по трудовым договорам быть не должно. да / нет / пропустить'))
ALIASES = {'доход': 'revenue', 'расходы': 'expenses', 'работники': 'employees', 'сотрудники': 'employees',
           'регион': 'region', 'деятельность': 'activity', 'фот': 'payroll', 'патент': 'patent_cost'}
BOOL_FIELDS = {'new_ip', 'expenses_confirmed', 'expenses_include_contributions', 'tax_resident', 'usn_tax_holiday', 'npd_activity_eligible'}
MONEY_FIELDS = {'payroll', 'patent_cost', 'previous_revenue', 'npd_deduction_remaining', 'patent_potential_income',
                'employer_contributions', 'vat_amount', 'other_income'}
RATE_FIELDS = {'client_person_share', 'usn_income_rate', 'usn_profit_rate'}


def parse_extra(key, value):
    if key in {field for field, _ in QUESTIONS}:
        return parse_answer(key, value)
    if key in BOOL_FIELDS:
        if value.casefold() not in {'да', 'нет', 'true', 'false'}:
            raise ValueError('Ответьте да или нет.')
        return value.casefold() in {'да', 'true'}
    if key in MONEY_FIELDS | RATE_FIELDS:
        text = value.replace(' ', '').replace(',', '.')
        if not re.fullmatch(r'\d{1,12}(\.\d{1,4})?', text):
            raise ValueError('Введите неотрицательное число без единиц измерения.')
        number = Decimal(text)
        cap = 1 if key == 'client_person_share' else 100 if key in RATE_FIELDS else 10000 if key == 'npd_deduction_remaining' else Decimal('999999999999')
        if number > cap:
            raise ValueError(f'Максимальное значение: {cap}.')
        return str(number)
    if key == 'year' and re.fullmatch(r'20\d{2}', value):
        return int(value)
    raise ValueError('Неизвестное поле. /set — список полей.')


class Journey:
    def __init__(self, services=None, settings=None):
        self.services = services
        self.settings = settings
        self.guide = Guide(self)

    @staticmethod
    def invalidate_options(state, field):
        dependencies = {
            'activity': {'usn_income_rate', 'usn_profit_rate', 'usn_tax_holiday', 'patent_cost', 'patent_potential_income', 'npd_activity_eligible', 'expenses_confirmed'},
            'region': {'usn_income_rate', 'usn_profit_rate', 'usn_tax_holiday', 'patent_cost', 'patent_potential_income'},
            'clients': {'client_person_share', 'npd_activity_eligible'},
            'employees': {'payroll', 'employer_contributions'},
            'revenue': {'vat_amount'},
            'expenses': {'expenses_confirmed', 'expenses_include_contributions', 'vat_amount'},
        }
        options = state.get('tax_options', {})
        for key in dependencies.get(field, set()):
            options.pop(key, None)
        state.pop('tax_comparison', None)

    def tax_inputs(self, state):
        inputs = {**state.get('answers', {}), **state.get('tax_options', {})}
        activity = inputs.get('activity', '').strip().casefold()
        # Сам код розничной торговли не доказывает перепродажу чужих товаров.
        # Отсекаем только явное описание перепродажи в текущей анкете.
        if re.match(r'^(?:я )?(?:перепродаю|перепродажа|(?:буду|планирую) перепродавать)\b', activity):
            inputs['npd_activity_eligible'] = False
        return inputs

    @staticmethod
    def is_local(state, text):
        low = text.strip().casefold()
        if local_personal_intent(state, text):
            return True
        if Guide.side_question(state, text):
            return False
        if Guide.is_local(state, text):
            return True
        if (state or {}).get('flow') and not low.startswith('/ask '):
            return True
        if low in {'меню', 'я в курсе, давай к делу', 'хочу открыть ип', 'что такое ип', 'что такое ип?',
                   'подобрать режим', 'подобрать режим налогообложения', 'рассчитать налоги',
                   'сравнить налоги', 'зарегистрировать ип'}:
            return True
        if low.rstrip('?.!') in {'нужен ли расчётный счёт', 'чем нпд отличается от ип',
                                'можно ли зарегистрироваться по домашнему адресу', 'нужна ли касса'}:
            return True
        if len((state or {}).get('answers', {})) == len(QUESTIONS):
            return bool(re.fullmatch(r'(?:возьму|найму) (?:одного )?сотрудника[.!]?', low) or
                        re.fullmatch(r'(?:доход (?:вырастет|увеличится) до|будет доход) (\d+(?:[.,]\d+)?)\s*(млн|миллионов|тыс|тысяч)?(?: рублей| руб)?[.!]?', low))
        return False

    def personal_answer(self, state, text):
        kind = local_personal_intent(state, text)
        if kind is None:
            return None
        if kind == 'profile':
            return self.handle(state, '/profile', user_id=self.user_id)
        if kind.startswith('field_'):
            field = kind[6:]
            if field == 'okved':
                selected = state.get('okved', {}).get('primary')
                answer = ('В анкете подтверждён основной ОКВЭД ' + selected['code'] + ' — ' + selected['title'] + '.') if selected else 'ОКВЭД в анкете ещё не подтверждён. /okved — подобрать.'
                return Reply(state, answer)
            value = state.get('answers', {}).get(field)
            if value is None:
                return Reply(state, LABELS[field] + ' пока не указаны в анкете. /continue — продолжить анкету; /set — добавить данные.')
            shown = profile({field: value}).split('\n', 1)[1]
            return Reply(state, 'По вашей сохранённой анкете:\n' + shown + '\n\n/set ' + field + ' значение — исправить.')
        if kind == 'regime':
            regime = REGIME_NAMES.get(state.get('selected_regime'))
            if regime:
                return Reply(state, 'В вашей анкете выбран ' + regime + '. Это ваш сохранённый выбор; '
                             'подтверждение применения режима проверяется в ФНС. /taxes — расчёт по текущей анкете.')
            return Reply(state, 'Налоговый режим в анкете ещё не выбран. Напишите «Какой режим мне подходит?» — сравню по вашим данным.')
        if kind in {'compare', 'tax'}:
            answers = state.get('answers', {})
            if any(key not in answers for key, _ in QUESTIONS):
                if not self.guide.active(state):
                    self.guide.new(state, 'profile')
                state.pop('flow', None)
                return self.guide.advance_profile(state)
            selected = state.get('selected_regime')
            result = self.comparison(state)
            if kind == 'tax':
                # comparison may clear a now-excluded selection. Explain that
                # same regime rather than silently showing another calculation.
                if not result.state.get('selected_regime') and self.guide.active(result.state):
                    result.state.pop('flow', None)
                    self.guide.move(result.state, 'regime_choice')
                return Reply(result.state, self.short_personal_tax(result.state, selected))
            # Calculation uses current options and clears a now-excluded selection.
            if self.guide.active(result.state):
                result.state.pop('flow', None)
                guided = self.guide.move(result.state, 'regime_choice')
                return Reply(guided.state, self.with_profile_basis(guided.state, guided.text))
            return Reply(result.state, self.with_profile_basis(result.state, result.text))
        if kind == 'documents':
            registration = state.get('registration', {})
            if not re.search(r'регистр|открыти|чек[\s-]*лист', text.casefold()):
                basis = ('В анкете уже сохранена дата регистрации ИП: ' + registration['date'] + '. '
                         if registration.get('date') else 'Учту сохранённую анкету. ')
                return Reply(state, basis + 'Уточните, для какого действия нужны документы: '
                             'открытия или закрытия ИП, изменения сведений, налоговой отчётности?')
            if any(key not in registration for key, _ in REG_FIELDS):
                if not self.guide.active(state):
                    self.guide.new(state, 'registration')
                state.pop('flow', None)
                return self.guide.begin_registration(state, prefix='Учту сохранённую анкету. Для вашего комплекта документов нужно уточнение.')
            return Reply(state, registration_checklist(state))
        return None

    @staticmethod
    def short_personal_tax(state, selected):
        """Summarise calculator output without turning partial amounts into totals."""
        from .tax import _LABELS
        result = state['tax_comparison']
        regime_id = 'patent' if selected == 'psn' else selected
        row = next((item for item in result['rows'] if item['id'] == regime_id), None)
        if row is None:
            return 'Режим ещё не выбран. /taxes — сравнить по вашей анкете; /regime — выбрать режим для короткого расчёта.'

        def rub(value):
            return f'{Decimal(value):,.2f}'.replace(',', ' ') + ' ₽'

        title = row['title'] + ', ' + str(result['year'])
        if row['status'] == 'excluded':
            return (title + ': режим исключён по вашей анкете.\n\n' + row.get('reason', '') +
                    '\n\n/tax ' + selected + ' — причина; /taxes — другие варианты.')
        if row.get('total') is not None:
            heading = title + ': условный итог налога и взносов — ' + rub(row['total']) + '.'
        elif row.get('range'):
            bounds = row['range']
            heading = title + ': условный диапазон — ' + rub(bounds['min']) + ' – ' + rub(bounds['max']) + '.\n' + bounds['scope'] + '.'
        elif row.get('subtotal') is not None:
            heading = title + ': неполная сумма — ' + rub(row['subtotal']) + '.\n' + row['subtotal_scope'] + '.'
        else:
            heading = title + ': общий итог пока не рассчитан.'
        lines = [heading]
        if row.get('reason'):
            lines.append(row['reason'])

        answers, options = state.get('answers', {}), state.get('tax_options', {})
        inputs = [str(answers['region']), 'доход ' + rub(answers['revenue']),
                  'работники: ' + str(answers['employees'])]
        if regime_id in {'usn_profit', 'ausn_profit', 'osno'}:
            inputs.append('расходы ' + rub(answers['expenses']))
        if regime_id == 'npd':
            inputs.append('клиенты: ' + answers['clients'])
            if options.get('npd_deduction_remaining') is not None:
                inputs.append('остаток вычета ' + rub(options['npd_deduction_remaining']))
        lines.append('Учтено:\n' + '\n'.join('• ' + item for item in inputs) + '.')
        if regime_id in {'usn_income', 'usn_profit'}:
            rate = next((detail for detail in row.get('details', []) if detail.startswith('Ставка ')), None)
            if rate:
                lines.append(rate)
        if row.get('missing'):
            lines.append('Нужно уточнить:\n' + '\n'.join('• ' + _LABELS.get(key, key) for key in row['missing']) + '.')
        lines.append('План за полный год; право на режим требует проверки. Подробности и границы расчёта: /tax ' + selected + '.')
        urls = [source['url'] for source in row.get('sources', []) if len(source.get('url', '')) <= 180]
        if urls:
            lines.append(min(urls, key=len))
        return '\n\n'.join(lines)

    @staticmethod
    def with_profile_basis(state, answer):
        from .profile_context import build_profile_context, profile_summary
        basis = profile_summary(build_profile_context(state))
        if basis and len(basis) + len(answer) + 2 <= 3900:
            return basis + '\n\n' + answer
        if basis and len(answer) <= 3810:
            return 'Ответ по вашей сохранённой анкете. /profile — исходные данные.\n\n' + answer
        # /profile remains the full view; don't cut conditions or source URLs.
        return answer

    def published(self, kind):
        publication = self.services.published(kind) if self.services else None
        return publication['payload'] if publication else None

    def comparison(self, state):
        answers = state.get('answers', {})
        if any(key not in answers for key, _ in QUESTIONS):
            state['paused'] = False
            return Reply(state, 'Для сравнения заполните анкету.\n' + question(answers))
        inputs = self.tax_inputs(state)
        result = compare_taxes(inputs, rules=self.published('tax_rules'))
        state['tax_comparison'] = result
        state['paused'] = True
        selected = state.get('selected_regime')
        old = next((r for r in result['rows'] if r['id'] == ('patent' if selected == 'psn' else selected)), None)
        prefix = ''
        if old and old['status'] == 'excluded':
            prefix = f'Изменились условия: выбранный {REGIME_NAMES.get(selected, selected)} исключён. Проверьте порядок перехода в ФНС.\n\n'
            state.pop('selected_regime', None)
            self.refresh_reminders(state)
        return Reply(state, (prefix + render_comparison(result))[:3900])

    def select_regime(self, state, argument):
        if argument == 'patent': argument = 'psn'
        if not argument:
            return Reply(state, 'Выберите режим после /taxes:\n' + '\n'.join(f'/regime {key} — {value}' for key, value in REGIME_NAMES.items()))
        if argument not in REGIME_NAMES:
            return Reply(state, 'Неизвестный режим. /regime — варианты.')
        if any(key not in state['answers'] for key, _ in QUESTIONS):
            return self.comparison(state)
        comparison = compare_taxes(self.tax_inputs(state), rules=self.published('tax_rules'))
        row = next((row for row in comparison['rows'] if row['id'] == ('patent' if argument == 'psn' else argument)), None)
        if row is None or 'year' in row.get('missing', []) or comparison.get('notes', [''])[0].startswith('Исправьте параметр'):
            return Reply(state, 'Выбор не сохранён: сначала исправьте параметры и проверьте поддерживаемый год через /taxes.')
        if row and row['status'] == 'excluded':
            return Reply(state, f'По указанным условиям режим исключён: {row.get("reason", "проверьте /taxes")}.')
        state['selected_regime'] = argument
        self.refresh_reminders(state)
        return Reply(state, f'Сохранён ваш выбор: {REGIME_NAMES[argument]}. Условия допуска проверьте в /taxes.\n/register — способ подачи и документы.')

    def refresh_reminders(self, state):
        if self.services is None or not getattr(self, 'user_id', None):
            return
        from .reminders import sync_reminders
        result = sync_reminders(self.services, self.user_id, state)
        state['reminder_notes'] = result['notes']

    def pdf(self, state, *, form=False):
        text = form_draft(state) if form else registration_checklist(state)
        title = 'Черновик данных Р21001' if form else 'Чек-лист регистрации ИП'
        notice = ''
        if form:
            from .forms import fill_r21001, R21001Error
            try:
                data = fill_r21001(state)
                title = 'Р21001: частично заполненный черновик'
                notice = ' Паспорт, адрес и остальные обязательные поля заполните в сервисе ФНС; этот черновик не готов к подаче.'
            except R21001Error:
                data = make_pdf(title, text)
                notice = ' Для ваших данных автоматическое заполнение клеток недоступно: приложен список данных для переноса в сервис ФНС.'
        else:
            data = make_pdf(title, text)
        return Reply(state, title + '. Документы в ФНС не отправлены.' + notice, files=[
            {'name': 'r21001-draft.pdf' if form else 'ip-checklist.pdf', 'content': base64.b64encode(data).decode()}])

    def handle(self, state, text, *, user_id=None):
        self.user_id = user_id
        raw = text.strip()
        parts = raw.split(maxsplit=1)
        command, argument = parts[0] if parts else '', parts[1] if len(parts) > 1 else ''
        command = command.casefold()
        low = raw.casefold()
        original = state
        state = copy.deepcopy(state) if state else {'answers': {}, 'paused': True}
        if command in {'/privacy', '/consent_required'}:
            return Reply(original, PRIVACY)
        if command == '/consent':
            state['consent'] = {'accepted': True, 'version': CONSENT_VERSION, 'at': datetime.now(timezone.utc).isoformat()}
            state['paused'] = True
            return self.guide.start(state)
        if command in {'/decline', '/delete'}:
            return Reply(None, 'Анкета, история, подготовленные файлы и подписки удалены из базы бота. Переписка в MAX остаётся в самом мессенджере. /intro — общая информация.')
        if command in {'/start', '/menu'} or low in {'меню', 'я в курсе, давай к делу', 'хочу открыть ип'}:
            if not has_consent(state) and self.services is not None:
                return Reply(original, MENU + '\n\n' + PRIVACY)
            return self.guide.start(state, directly=low in {'я в курсе, давай к делу', 'хочу открыть ип'})
        if command == '/intro' or low in {'что такое ип', 'что такое ип?'}:
            return Reply(original, INTRO)
        if command == '/help':
            return Reply(original, HELP)
        if command == '/lessons':
            return Reply(original, 'Короткие уроки:\n/lesson 1 — ИП, ООО и самозанятость\n/lesson 2 — выбор режима\n/lesson 3 — регистрация по шагам')
        if command == '/lesson':
            lessons = {'1': INTRO,
                       '2': 'Сначала проверьте допуск по деятельности, региону, доходу и работникам. Затем сравните налог и взносы. '
                       'Минимальный расчёт не означает, что режим вам доступен: региональные условия и подтверждение расходов важны. '
                       'НПД — режим для подходящей самостоятельной деятельности; УСН и АУСН имеют разные объекты налога; '
                       'ПСН рассчитывается по региональному потенциальному доходу. /begin — ваша анкета.\nhttps://www.nalog.gov.ru/rn77/service/mp/',
                       '3': '1. Проверьте ОКВЭД и режим.\n\n'
                       '2. Соберите документы по гражданству и способу подачи, заполните Р21001.\n\n'
                       '3. Подпишите и отправьте пакет в ФНС.\n\n'
                       '4. Получите лист записи ЕГРИП.\n\n'
                       '5. Отдельно проверьте уведомление о спецрежиме, кассу и разрешения.\n\n'
                       '/steps — пройти этот путь с сохранением шага.\n' + REGISTRATION}
            return Reply(original, lessons.get(argument, 'Выберите урок: /lesson 1, /lesson 2 или /lesson 3.'))
        if command == '/new':
            reset = {'answers': {}}
            if state.get('consent'): reset['consent'] = state['consent']
            return Reply(reset, question({}) if self.services is None or has_consent(state) else PRIVACY)
        # При отсутствии хранилища можно использовать чистый диалог в памяти.
        if self.services is not None and not has_consent(state):
            return Reply(original, PRIVACY)
        if command == '/cancel':
            saved_flow = state.pop('flow', None)
            if state.get('guide'):
                state['guide']['active'] = False
                if saved_flow: state['guide']['paused_flow'] = saved_flow
            state['paused'] = True
            return Reply(state, 'Сценарий приостановлен. /continue — вернуться к текущему шагу.\n' + MENU)
        if command == '/profile':
            extra = '\nРежим: ' + REGIME_NAMES.get(state.get('selected_regime'), 'не выбран')
            registration = state.get('registration', {})
            if registration:
                citizenship = {'ru': 'РФ', 'foreign': 'иностранное', 'stateless': 'без гражданства'}
                if registration.get('citizenship'):
                    extra += '\nГражданство: ' + citizenship.get(registration['citizenship'], 'не указано')
                if registration.get('age') is not None: extra += '\nВозраст: ' + str(registration['age'])
                methods = {'online': 'онлайн', 'mfc': 'МФЦ', 'paper': 'на бумаге', 'representative': 'через представителя', 'notary': 'через нотариуса'}
                extra += '\nПодача: ' + methods.get(registration.get('method'), 'ещё не выбрана')
                if registration.get('date'): extra += '\nДата регистрации: ' + registration['date']
            okved = state.get('okved', {})
            if okved.get('primary'):
                extra += '\nОсновной ОКВЭД: ' + okved['primary']['code']
                additional = [item['code'] for item in okved.get('additional', [])]
                if additional: extra += '\nДополнительные ОКВЭД: ' + ', '.join(additional)
            if state.get('tax_options'):
                from .tax import _LABELS
                names = {**_LABELS, 'year': 'Расчётный год', 'new_ip': 'Новый бизнес',
                         'expenses_confirmed': 'Расходы подтверждены',
                         'expenses_include_contributions': 'Взносы уже включены в расходы',
                         'npd_activity_eligible': 'Условия дохода для НПД подтверждены'}
                extra += '\n\nУточнения расчёта:'
                for key, value in state['tax_options'].items():
                    label = names.get(key, key)
                    shown = 'да' if value is True else 'нет' if value is False else str(value)
                    extra += '\n' + label + ': ' + shown
            return Reply(original, (profile(state['answers']) + extra + '\n/set — исправить параметры; /taxes — пересчитать')[:3900])
        if command == '/begin':
            state.pop('flow', None)
            state.pop('guide', None)
            state['paused'] = False
            return Reply(state, question(state['answers']))
        personal = self.personal_answer(state, raw)
        if personal is not None:
            return personal
        # Справочный вопрос не должен попасть в поле анкеты (например, гражданство).
        common = {'нужен ли расчётный счёт': 'account', 'чем нпд отличается от ип': 'npd',
                  'можно ли зарегистрироваться по домашнему адресу': 'address', 'нужна ли касса': 'cash'}
        faq_id = {key.replace('ё', 'е'): value for key, value in common.items()}.get(low.rstrip('?.!').replace('ё', 'е'))
        if command == '/faq' or faq_id:
            answer = self.faq(argument if command == '/faq' else faq_id)
            if faq_id == 'cash' or argument == 'cash':
                answers = state.get('answers', {})
                if answers.get('activity') and state.get('selected_regime'):
                    answer = answer.replace('[деятельность]', answers['activity']).replace('[режим]', REGIME_NAMES[state['selected_regime']])
            answer = self.with_profile_basis(state, answer)
            if self.guide.active(state):
                return self.guide.prompt(state, prefix=answer)
            if state.get('flow'):
                answer += '\n\nПродолжим анкету: ' + self.flow_question(state['flow'])
            return Reply(state, answer[:3900])
        # Изменение уже известного профиля пересчитывает выбор и сохраняет остальные поля.
        if len(state.get('answers', {})) == len(QUESTIONS):
            hired = re.fullmatch(r'(?:возьму|найму) (?:одного )?сотрудника[.!]?', low)
            income = re.fullmatch(r'(?:доход (?:вырастет|увеличится) до|будет доход) (\d+(?:[.,]\d+)?)\s*(млн|миллионов|тыс|тысяч)?(?: рублей| руб)?[.!]?', low)
            if hired or income:
                key = 'employees' if hired else 'revenue'
                value = int(state['answers'].get('employees', 0)) + 1 if hired else str(Decimal(income[1].replace(',', '.')) * (1000000 if income[2] in {'млн', 'миллионов'} else 1000 if income[2] else 1))
                self.invalidate_options(state, key)
                state['answers'][key] = value
                state.pop('flow', None)
                computed = self.comparison(state)
                if self.guide.active(state):
                    return self.guide.offer_regimes(computed.state, prefix='Профиль обновлён. Проверьте выбор режима заново. ' + ('Предыдущий режим исключён.' if original.get('selected_regime') and not computed.state.get('selected_regime') else ''))
                return computed
        if Guide.side_question(state, raw):
            return None
        guided = self.guide.handle(state, raw)
        if guided is not None:
            return guided
        if state.get('flow') and not raw.startswith('/'):
            kind = state['flow']['kind']
            result = self.flow(state, raw)
            if self.guide.active(state) and kind in {'registration', 'form'}:
                return self.guide.after_flow(result, registration=kind == 'registration')
            if self.guide.active(state) and kind == 'tax' and not result.state.get('flow'):
                return self.guide.offer_regimes(result.state)
            return result
        if command == '/back' and state.get('flow'):
            flow = state['flow']
            flow['index'] = max(0, flow.get('index', 0) - 1)
            return Reply(state, self.flow_question(flow))
        if command == '/okved':
            activity = argument or state['answers'].get('activity', '')
            if self.guide.active(state):
                state.pop('flow', None)
                return self.guide.activity(state, activity) if activity else self.guide.move(state, 'activity')
            if not activity:
                state['flow'] = {'kind': 'okved', 'index': 0}
                return Reply(state, 'Опишите, чем будете заниматься: что производить, продавать или какие услуги оказывать.')
            return self.activity(state, activity)
        if command in {'/taxes', '/compare'} or low in {'подобрать режим', 'подобрать режим налогообложения', 'рассчитать налоги', 'сравнить налоги'}:
            if self.guide.active(state) and any(key not in state['answers'] for key, _ in QUESTIONS):
                state.pop('flow', None)
                return self.guide.advance_profile(state)
            return self.comparison(state)
        if command == '/tax_setup':
            state['flow'] = {'kind': 'tax', 'index': 0}
            if self.guide.active(state): state['guide']['stage'] = 'tax'
            return Reply(state, self.flow_question(state['flow']))
        if command == '/tax':
            if argument not in REGIME_NAMES and argument != 'patent':
                return Reply(original, 'Формат: /tax usn_income. Доступно: ' + ', '.join(REGIME_NAMES))
            computed = self.comparison(state)
            if 'tax_comparison' not in computed.state:
                return computed
            return Reply(computed.state, render_tax_details(computed.state['tax_comparison'], argument))
        if command == '/set':
            key, _, value = argument.partition(' ')
            key = ALIASES.get(key.casefold(), key.casefold())
            if not value:
                return Reply(original, 'Формат: /set поле значение.\nОсновные: деятельность, регион, доход, расходы, сотрудники, clients (1/2/3).\n'
                             'Уточнения: ' + ', '.join(sorted(BOOL_FIELDS | MONEY_FIELDS | RATE_FIELDS | {'year'})) + '\n/tax_setup — вопросы по очереди.')
            try:
                parsed = parse_extra(key, value)
            except ValueError as error:
                return Reply(original, str(error))
            target = state['answers'] if key in {name for name, _ in QUESTIONS} else state.setdefault('tax_options', {})
            if target.get(key) != parsed:
                self.invalidate_options(state, key)
            target[key] = parsed
            if key == 'activity':
                state.pop('okved', None)
                state.pop('selected_regime', None)
                state.pop('tax_comparison', None)
                self.refresh_reminders(state)
                if self.guide.active(state):
                    state.pop('flow', None)
                    return self.guide.activity(state, parsed)
            if self.guide.active(state):
                state.pop('flow', None)
                return self.guide.offer_regimes(state)
            return self.comparison(state)
        if command == '/regime':
            if self.guide.active(state) and argument:
                return self.guide.choose_regime(state, 'psn' if argument == 'patent' else argument)
            return self.select_regime(state, argument)
        if command == '/register' or low == 'зарегистрировать ип':
            if self.guide.active(state): state['guide']['stage'] = 'registration'
            state['flow'] = {'kind': 'registration', 'index': 0}
            state['paused'] = True
            return Reply(state, self.flow_question(state['flow']))
        if command == '/checklist':
            return Reply(original, registration_checklist(state)[:3900])
        if command == '/pdf':
            return self.pdf(state)
        if command == '/form':
            if argument == 'pdf':
                return self.pdf(state, form=True)
            state['flow'] = {'kind': 'form', 'index': 0}
            if self.guide.active(state): state['guide']['stage'] = 'form'
            return Reply(state, 'Заполню известные поля в черновике Р21001: ФИО, ИНН, email и выбранные ОКВЭД. '
                         'Данные сохранятся в профиле; это не готовое заявление. Паспорт не присылайте. '
                         '/cancel — выйти из сценария; /delete — удалить сохранённые данные.\n\n' + self.flow_question(state['flow']))
        if command in {'/steps', '/next', '/prev'}:
            index = state.get('registration_step', 0)
            if command == '/next': index = min(5, index + 1)
            if command == '/prev': index = max(0, index - 1)
            state['registration_step'] = index
            if self.guide.active(state):
                state.pop('flow', None)
                return self.guide.move(state, 'walk')
            return Reply(state, step_text(index, state))
        if command == '/online':
            return Reply(original, 'Официальная регистрация ИП:\n' + REGISTRATION + '\n\nОткройте сервис и заполните данные самостоятельно. '
                         'Анкета из бота автоматически не передаётся. /form pdf — черновик для переноса. '
                         'Подачу и подпись вы подтверждаете на стороне ФНС.')
        if command == '/registered':
            if self.guide.active(state):
                state.pop('flow', None)
                self.guide.move(state, 'registered_date')
                return self.guide.handle(state, argument) if argument else self.guide.prompt(state)
            try:
                registered = date.fromisoformat(argument)
                if registered > date.today() or registered.year < 2000:
                    raise ValueError
            except ValueError:
                return Reply(original, 'Укажите фактическую дату регистрации: /registered ГГГГ-ММ-ДД')
            state.setdefault('registration', {})['date'] = registered.isoformat()
            self.refresh_reminders(state)
            return Reply(state, 'Дата сохранена с ваших слов; статус в ФНС автоматически не проверялся. /reminders on — включить напоминания.')
        if command in {'/reminders', '/remind', '/timezone', '/subscribe', '/unsubscribe', '/feedback'}:
            return self.service_command(state, command, argument)
        if command == '/consultant':
            contact = getattr(self.settings, 'consultant_url', '') if self.settings else ''
            return Reply(original, ('Контакт консультанта: ' + contact + '\nИстория ему автоматически не отправляется.') if contact else
                         'Живой консультант пока не подключён. Можно обратиться в ФНС: https://www.nalog.gov.ru/rn77/service/obr_fts/')
        return None

    def activity(self, state, text):
        result = suggest_activity(text, catalog=self.published('activities'))
        prefix = ''
        if result['status'] == 'clarify':
            state['flow'] = {'kind': 'okved', 'index': 0, 'description': text[:500]}
        else:
            state.pop('flow', None)
            if state['answers'].get('activity') != text[:300]:
                self.invalidate_options(state, 'activity')
                state.pop('okved', None)
                state.pop('tax_comparison', None)
                if state.pop('selected_regime', None):
                    prefix = 'Деятельность изменилась. Выбор режима нужно проверить заново: /taxes. Это не уведомление ФНС о переходе.\n\n'
                self.refresh_reminders(state)
            state['answers']['activity'] = text[:300]
        if result['status'] == 'matched':
            state['okved'] = result
        return Reply(state, (prefix + format_activity(result) + '\n\n/begin — анкета для подбора режима; /taxes — сравнить; /register — регистрация.')[:3900])

    @staticmethod
    def flow_question(flow):
        fields = {'registration': REG_FIELDS, 'form': FORM_FIELDS, 'tax': TAX_FIELDS}
        if flow['kind'] == 'okved':
            return 'Уточните, что именно будете делать. Если продаёте товар: производите сами или перепродаёте?'
        text = fields[flow['kind']][flow['index']][1]
        if '\n1 —' in text:
            text = text.replace('\n1 —', '\n\n' + CHOICE_MARKER + '\n1 —', 1)
        return text + '\n\n/cancel — остановить; /back — назад.'

    def flow(self, state, text):
        flow = state['flow']
        kind = flow['kind']
        if kind == 'okved':
            return self.activity(state, (flow.get('description', '') + ' ' + text).strip())
        fields = {'registration': REG_FIELDS, 'form': FORM_FIELDS, 'tax': TAX_FIELDS}[kind]
        key = fields[flow['index']][0]
        target = state.setdefault({'registration': 'registration', 'form': 'form', 'tax': 'tax_options'}[kind], {})
        try:
            if kind == 'registration':
                if key == 'citizenship':
                    text = choice(text, {'1': 'РФ', '2': 'иностранное гражданство', '3': 'без гражданства'}) or text
                    value = {'1': 'ru', 'рф': 'ru', 'россия': 'ru', '2': 'foreign', 'иностранное': 'foreign', '3': 'stateless', 'без гражданства': 'stateless'}.get(text.casefold())
                    if value is None: raise ValueError('Выберите 1, 2 или 3.')
                elif key == 'age':
                    if not re.fullmatch(r'\d{1,3}', text) or not 14 <= int(text) <= 120:
                        raise ValueError('Этот сценарий рассчитан на возраст от 14 лет. Введите полное число лет или /cancel.')
                    value = int(text)
                else:
                    text = choice(text, {'1': 'онлайн', '2': 'МФЦ', '3': 'лично на бумаге', '4': 'представитель', '5': 'нотариус'}) or text
                    value = {'1': 'online', '2': 'mfc', '3': 'paper', '4': 'representative', '5': 'notary'}.get(text)
                    if value is None: raise ValueError('Выберите число от 1 до 5.')
            elif kind == 'tax':
                value = None if text.casefold() == 'пропустить' else parse_extra(key, text)
            else:
                if key in {'surname', 'name', 'patronymic'}:
                    if key == 'patronymic' and text.casefold() == 'нет': value = ''
                    elif re.fullmatch(r'[А-Яа-яЁё -]{1,60}', text): value = text.upper()
                    else: raise ValueError('Введите имя русскими буквами, без других данных.')
                elif key == 'inn':
                    if text.casefold() == 'нет': value = ''
                    elif re.fullmatch(r'\d{12}', text): value = text
                    else: raise ValueError('ИНН физлица — 12 цифр, либо «нет».')
                else:
                    if not re.fullmatch(r'[^\s@]{1,80}@[^\s@]{1,100}\.[A-Za-zА-Яа-я]{2,20}', text):
                        raise ValueError('Введите корректный email без пробелов.')
                    value = text
            if value is not None:
                target[key] = value
            else:
                target.pop(key, None)
        except (ValueError, InvalidOperation) as error:
            return Reply(state, str(error) + '\n' + self.flow_question(flow))
        flow['index'] += 1
        if flow['index'] < len(fields):
            return Reply(state, self.flow_question(flow))
        state.pop('flow', None)
        state['paused'] = True
        if kind == 'tax': return self.comparison(state)
        if kind == 'form': return self.pdf(state, form=True)
        return Reply(state, registration_checklist(state)[:3900])

    def service_command(self, state, command, argument):
        if self.services is None or self.user_id is None:
            return Reply(state, 'Для сохранения напоминаний и подписок запустите бота с PostgreSQL.')
        if command == '/timezone':
            try: ZoneInfo(argument)
            except (ZoneInfoNotFoundError, ValueError): return Reply(state, 'Пример: /timezone Europe/Moscow или /timezone Asia/Yekaterinburg')
            state['timezone'] = argument
            self.refresh_reminders(state)
            return Reply(state, 'Часовой пояс сохранён: ' + argument)
        if command == '/feedback':
            if argument not in {'1', '2', '3', '4', '5'}:
                return Reply(state, 'Оцените консультацию: /feedback 1 ... /feedback 5')
            self.services.feedback(self.user_id, int(argument))
            return Reply(state, 'Спасибо, оценка сохранена.')
        if command == '/unsubscribe':
            self.services.unsubscribe(self.user_id)
            return Reply(state, 'Новости отключены. Напоминания отключаются отдельно: /reminders off')
        if command == '/subscribe':
            topic = argument or state.get('selected_regime', 'all')
            if topic not in set(REGIME_NAMES) | {'all'}:
                return Reply(state, 'Укажите /subscribe all или идентификатор режима из /regime.')
            self.services.subscribe(self.user_id, topic)
            return Reply(state, 'Подписка на обновления включена: ' + REGIME_NAMES.get(topic, 'все режимы') + '. /unsubscribe — отключить.')
        if command == '/remind':
            parts = argument.split(' ', 2)
            try:
                when = datetime.fromisoformat(parts[0] + 'T' + parts[1]).replace(tzinfo=ZoneInfo(state.get('timezone', 'Europe/Moscow')))
                if when <= datetime.now(timezone.utc) or len(parts) != 3 or not 1 <= len(parts[2]) <= 500:
                    raise ValueError
            except (ValueError, IndexError):
                return Reply(state, 'Формат: /remind ГГГГ-ММ-ДД ЧЧ:ММ что напомнить. Время должно быть в будущем. Часовой пояс: ' + state.get('timezone', 'Europe/Moscow'))
            import hashlib
            key = 'manual:' + hashlib.sha256(argument.encode()).hexdigest()
            self.services.create_reminder(self.user_id, parts[2], when.astimezone(timezone.utc), key)
            return Reply(state, 'Напоминание сохранено на ' + when.strftime('%d.%m.%Y %H:%M') + ' ' + str(when.tzinfo) + '. /reminders — список.')
        if command == '/reminders':
            if argument in {'on', 'вкл'}:
                if not state.get('registration', {}).get('date') or not state.get('selected_regime'):
                    return Reply(state, 'Сначала /registered ГГГГ-ММ-ДД и /regime — дата регистрации и режим.')
                state['reminders_enabled'] = True
                self.refresh_reminders(state)
            elif argument in {'off', 'выкл'}:
                state['reminders_enabled'] = False
                for item in self.services.list_reminders(self.user_id):
                    self.services.cancel_reminder(self.user_id, item['id'])
                return Reply(state, 'Все ваши напоминания отключены.')
            elif argument.startswith('cancel '):
                try: self.services.cancel_reminder(self.user_id, int(argument.split()[1]))
                except (ValueError, IndexError): return Reply(state, 'Формат: /reminders cancel ID')
            items = self.services.list_reminders(self.user_id)
            lines = ['Напоминания (' + state.get('timezone', 'Europe/Moscow') + '):']
            page = max(1, min(int(argument), 10000)) if argument.isdigit() else 1
            for item in items[(page - 1) * 12:page * 12]:
                when = item['due_at'].astimezone(ZoneInfo(state.get('timezone', 'Europe/Moscow')))
                lines.append(f"{item['id']}: {when:%d.%m.%Y %H:%M} — {item['text'][:140]}")
            if not items: lines.append('Активных напоминаний нет.')
            if len(items) > page * 12: lines.append(f'/reminders {page + 1} — следующие напоминания (всего {len(items)}).')
            lines.extend(state.get('reminder_notes', []))
            lines.append('/reminders on — включить сроки по регистрации; /remind — своё; /reminders off — отключить.')
            return Reply(state, '\n'.join(lines)[:3900])
        return None

    def faq(self, argument):
        entries = [
            {'id': 'account', 'question': 'Нужен ли расчётный счёт?', 'answer': 'Открытие счёта — отдельный шаг после регистрации. '
             'Способ расчётов и ограничения зависят от клиентов и операций. Выберите банк самостоятельно; бот счёт не открывает.', 'sources': [REGISTRATION]},
            {'id': 'npd', 'question': 'Чем НПД отличается от ИП?', 'answer': 'ИП — статус предпринимателя, НПД — налоговый режим. '
             'При соблюдении условий НПД можно применять и без ИП. Чеки формируются в «Мой налог». /taxes — сравнить вашу ситуацию.', 'sources': ['https://npd.nalog.ru/']},
            {'id': 'address', 'question': 'Можно ли зарегистрироваться по домашнему адресу?', 'answer': 'Регистрация ИП привязана к месту жительства. '
             'Порядок подтверждения адреса и использования жилого помещения для деятельности — разные вопросы. Проверьте условия для своей деятельности.', 'sources': [REGISTRATION]},
            {'id': 'cash', 'question': 'Нужна ли касса?', 'answer': 'Ответ зависит от режима, деятельности и способа расчётов. '
             'На НПД чеки формируются через «Мой налог». Для остальных случаев задайте: /ask Нужна ли касса для [деятельность], режим [режим], расчёты [способ]?', 'sources': ['https://npd.nalog.ru/', 'https://www.nalog.gov.ru/rn77/taxation/reference_work/newkkt/']},
        ]
        if self.services:
            publication = self.services.published('faq')
            if publication:
                payload = publication.get('payload', {})
                entries = payload.get('entries', entries) if isinstance(payload, dict) else payload if isinstance(payload, list) else entries
        if argument:
            matches = [item for item in entries if argument.casefold() == item['id'] or argument.casefold() in item['question'].casefold()]
            if matches:
                item = matches[0]
                return item['question'] + '\n\n' + item['answer'] + '\n\n' + '\n'.join(item['sources'])
            return 'В локальном FAQ ответа нет. /ask ваш вопрос — поиск в официальных источниках; /consultant — контакт консультанта.'
        lines = ['Частые вопросы:']
        for item in entries[:15]:
            line = f"/faq {item['id']} — {item['question'][:180]}"
            if sum(map(len, lines)) + len(line) + len(lines) > 3800:
                break
            lines.append(line)
        return '\n'.join(lines)

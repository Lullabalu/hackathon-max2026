"""Detached, allowlisted business context; no identity/form fields or legal facts.

Saved values are defaults for a question, not new assertions about the user.
Explicit alternatives suppress affected defaults instead of modifying state.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .llm import Plan
from .regions import resolve_region


_MONEY = {'revenue', 'expenses', 'payroll', 'patent_cost', 'previous_revenue',
          'npd_deduction_remaining', 'patent_potential_income', 'employer_contributions',
          'vat_amount', 'other_income'}
_BOOL = {'new_ip', 'expenses_confirmed', 'expenses_include_contributions', 'tax_resident',
         'usn_tax_holiday', 'npd_activity_eligible'}
_RATES = {'client_person_share', 'usn_income_rate', 'usn_profit_rate'}
_REGIMES = {'npd': 'НПД', 'usn_income': 'УСН Доходы', 'usn_profit': 'УСН Доходы минус расходы',
            'psn': 'ПСН', 'patent': 'ПСН', 'ausn_income': 'АУСН Доходы',
            'ausn_profit': 'АУСН Доходы минус расходы', 'osno': 'ОСНО'}
_CITIZENSHIP = {'ru': 'гражданство РФ', 'foreign': 'иностранное гражданство', 'stateless': 'без гражданства'}
_METHODS = {'online': 'онлайн', 'mfc': 'МФЦ', 'paper': 'лично на бумаге',
            'representative': 'через представителя', 'notary': 'через нотариуса'}
_CLIENTS = {'Физлица', 'ИП и организации', 'Физлица, ИП и организации'}
_DEPENDENCIES = {
    'region': {'usn_income_rate', 'usn_profit_rate', 'usn_tax_holiday', 'patent_cost', 'patent_potential_income'},
    'activity': {'usn_income_rate', 'usn_profit_rate', 'usn_tax_holiday', 'patent_cost',
                 'patent_potential_income', 'npd_activity_eligible', 'expenses_confirmed'},
    'revenue': {'vat_amount'}, 'expenses': {'expenses_confirmed', 'expenses_include_contributions', 'vat_amount'},
    'employees': {'payroll', 'employer_contributions'},
    'clients': {'client_person_share', 'npd_activity_eligible'},
}
_REGIME_WORDS = r'\b(?:усн|нпд|псн|аусн|осно|патент\w*|самозанят\w*)\b'
_IDENTITY = re.compile(r'[^\s@]+@[^\s@]+|\b(?:инн|фио|паспорт|телефон)\b|(?<!\d)\d{10,12}(?!\d)', re.I)


def _normal(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е').split())


def _text(value, maximum):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum or _IDENTITY.search(value):
        return None
    if any(ord(char) < 32 and char not in '\n\t' for char in value):
        return None
    return ' '.join(value.split())


def _number(value, maximum, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    raw = str(value)
    if not re.fullmatch(r'\d{1,12}(?:\.\d{1,4})?', raw):
        return None
    try:
        parsed = Decimal(raw)
        if parsed > maximum or integer and parsed != parsed.to_integral_value():
            return None
    except InvalidOperation:
        return None
    return int(parsed) if integer else raw


def _location_mentioned(question):
    text = _normal(question)
    if re.search(r'\b(?:друг\w*|нов\w*)\s+(?:регион\w*|город\w*|област\w*)|\bпереед\w*|\bпереез\w*', text):
        return True
    # A short location after a preposition may be unknown to the registry.
    # Capitalised names are conservatively treated as an explicit location;
    # known business/institution phrases are not places.
    ignored = {'моем', 'моей', 'нашем', 'нашей', 'этом', 'этой', 'своем', 'своей', 'следующем',
               'прошлом', 'текущем', 'этом', 'налоговой', 'фнс', 'мфц', 'ип', 'рф', 'усн',
               'нпд', 'псн', 'аусн', 'осно', 'интернете', 'электронном', 'бумажном',
               'заявлении', 'форме', 'декларации', 'банке', 'личном', 'случае', 'течение',
               'срок', 'документах', 'каком', 'какой', 'какие', 'размере', 'рублях',
               'я', 'мы', 'вы', 'ты', 'мой', 'моя', 'наш', 'наша', 'меня', 'указал', 'указала'}
    ignored.update({'оквэд', 'октмо', 'окато', 'кнд', 'пфр', 'сфр', 'ндс', 'егрип', 'егрюл'})
    for match in re.finditer(r'\b(?:(?:в|во|из|по)\s+|(?:регион|город)(?:\s*[:=]\s*|\s+))'
                             r'([А-ЯЁа-яёA-Za-z][А-ЯЁа-яёA-Za-z0-9-]*)', question, re.I):
        word = match.group(1)
        norm = _normal(word)
        if norm in ignored or re.match(_REGIME_WORDS, norm) or any(char.isdigit() for char in word):
            continue
        if word[0].isupper():
            return True
        variants = {norm}
        for ending, replacements in [('ской', ('ская',)), ('ом', ('',)), ('е', ('а', 'ь', '')), ('и', ('ь', 'я'))]:
            if norm.endswith(ending):
                variants.update(norm[:-len(ending)] + suffix for suffix in replacements)
        if any(resolve_region(variant) for variant in variants):
            return True
        if re.search(r'\b(?:если|вместо)\s+(?:в|во|из|по)\s+' + re.escape(norm) + r'\b', text):
            return True
    return bool(re.search(r'\b(?:области|крае|республике|автономном округе)\b', text)
                and not re.search(r'\b(?:моей|нашем|нашей|своей|какой)\s+', text))


def _overrides(question):
    text = _normal(question) if isinstance(question, str) else ''
    result = set()
    if _location_mentioned(question if isinstance(question, str) else ''):
        result.add('region')
    if re.search(_REGIME_WORDS, text):
        result.add('selected_regime')
    patterns = {
        'revenue': r'(?:доход\w*|выручк\w*)\s*(?:(?:будет|составит|вырастет|увеличится|снизится)\s+)?(?:до\s+|:\s*|=\s*)?\d|'
                   r'\d+(?:\s*(?:руб\w*|тыс\w*|млн|миллион\w*))?\s+(?:доход\w*|выручк\w*)|без доход\w*',
        'expenses': r'(?:расход\w*|затрат\w*)\s*(?:(?:будут|составят|вырастут|увеличатся|снизятся)\s+)?(?:до\s+|:\s*|=\s*)?\d|'
                    r'\d+(?:\s*(?:руб\w*|тыс\w*|млн|миллион\w*))?\s+(?:расход\w*|затрат\w*)|без расход\w*',
        'employees': r'\b(?:найму|нанять|нанима\w*|наем\w*)\b|(?:без|нет|\d+|один|одного|два|двух|три|трех|с)\s+(?:наемных\s+)?(?:работник\w*|сотрудник\w*)',
        'clients': r'\b(?:физлиц\w*|юрлиц\w*|юридическ\w*\s+лиц\w*|физическ\w*\s+лиц\w*)\b',
        'activity': r'\b(?:занимаюсь|продаю|перепродаю|пеку)\b|'
                    r'\b(?:буду|планирую|хочу|начну|если)\s+(?:заниматься|продавать|перепродавать|ремонтировать|торговать)\b|'
                    r'\b(?:открыть|открою)\s+(?:магазин\w*|салон\w*|шиномонтаж\w*)\b|'
                    r'\bдля\s+(?:магазин\w*|клининг\w*|такси|маникюр\w*|парикмахер\w*|программист\w*|ремонт\w*)\b',
        'registration.citizenship': r'\b(?:иностран\w*|россиян\w*|без гражданства)\b|'
                                    r'\bграждан(?:ин|ка|ином|кой)\s+[а-я-]+\b|'
                                    r'\bгражданство\s+(?:рф|россии|иностран\w*)\b',
        'registration.age': r'\b\d{1,3}\s*(?:лет|год\w*)\b|\b(?:несовершеннолет\w*|совершеннолет\w*)\b',
        'registration.method': r'\b(?:онлайн|мфц|нотариус\w*|представител\w*)\b|\b(?:на бумаге|лично|электронн\w*\s+подач\w*)\b',
        'registration.date': r'\b(?:зарегистрир\w*|откры\w*)[^?;.]{0,30}\d{1,4}[-./]\d|\b(?:дата регистрации|другую дату)\b',
        'tax_options.year': r'\b20\d{2}\b|\b(?:следующ\w*|прошл\w*|предыдущ\w*)\s+год\w*',
    }
    for field, pattern in patterns.items():
        if re.search(pattern, text):
            result.add(field)
    return result


def build_profile_context(state, question='') -> dict:
    """Return flat questionnaire fields plus allowlisted nested sections.

    ``omitted_fields`` names saved defaults suppressed by this question. It
    contains no values and is absent when there is nothing to suppress.
    """
    if not isinstance(state, dict):
        return {}
    answers = state.get('answers', state)
    if not isinstance(answers, dict):
        answers = {}
    result = {}
    for key, limit in [('activity', 300), ('region', 100)]:
        value = _text(answers.get(key), limit)
        if value is not None:
            result[key] = value
    for key in ('revenue', 'expenses', 'employees'):
        value = _number(answers.get(key), 999999 if key == 'employees' else 999999999999,
                        integer=key == 'employees')
        if value is not None:
            result[key] = value
    if isinstance(answers.get('clients'), str) and answers['clients'] in _CLIENTS:
        result['clients'] = answers['clients']
    options = state.get('tax_options', {})
    if isinstance(options, dict):
        clean = {}
        for key in sorted(_MONEY - {'revenue', 'expenses'} | _RATES | _BOOL | {'year'}):
            value = options.get(key)
            if key in _BOOL:
                value = value if type(value) is bool else None
            elif key == 'year':
                value = value if type(value) is int and 2000 <= value <= 2099 else None
            else:
                maximum = 1 if key == 'client_person_share' else 100 if key in _RATES else 999999999999
                value = _number(value, maximum)
            if value is not None:
                clean[key] = value
        if clean:
            result['tax_options'] = clean
    selected = state.get('selected_regime')
    if isinstance(selected, str) and selected in _REGIMES:
        result['selected_regime'] = selected
    registration = state.get('registration', {})
    if isinstance(registration, dict):
        clean = {}
        for key, values in [('citizenship', _CITIZENSHIP), ('method', _METHODS)]:
            value = registration.get(key)
            if isinstance(value, str) and value in values:
                clean[key] = value
        age = registration.get('age')
        if type(age) is int and 0 <= age <= 120:
            clean['age'] = age
        registered = registration.get('date')
        if isinstance(registered, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', registered):
            try:
                clean['date'] = date.fromisoformat(registered).isoformat()
            except ValueError:
                pass
        if clean:
            result['registration'] = clean
    okved = state.get('okved', {})
    if isinstance(okved, dict):
        def code(item):
            value = item.get('code') if isinstance(item, dict) else item
            return value if isinstance(value, str) and re.fullmatch(r'\d{2}\.\d{2}(?:\.\d{1,2})?', value) else None
        primary = code(okved.get('primary'))
        additional = okved.get('additional', [])
        additional = list(dict.fromkeys(value for item in additional[:20] if (value := code(item)))) if isinstance(additional, list) else []
        if primary or additional:
            result['okved'] = {'primary': primary, 'additional': additional}
    zone = state.get('timezone')
    if isinstance(zone, str) and len(zone) <= 80:
        try:
            ZoneInfo(zone)
            result['timezone'] = zone
        except (ZoneInfoNotFoundError, ValueError):
            pass
    omitted = []
    for field in sorted(_overrides(question)):
        section, _, key = field.partition('.')
        container = result.get(section, {}) if key else result
        if (key or section) in container:
            container.pop(key or section)
            omitted.append(field)
        if not key:
            for dependent in _DEPENDENCIES.get(field, ()):
                if dependent in result.get('tax_options', {}):
                    result['tax_options'].pop(dependent)
                    omitted.append('tax_options.' + dependent)
        if field == 'activity' and result.pop('okved', None) is not None:
            omitted.append('okved')
    for section in ('registration', 'tax_options'):
        if section in result and not result[section]:
            result.pop(section)
    if omitted:
        result['omitted_fields'] = sorted(omitted)
    return result


def _clauses(profile, topics, question):
    text = _normal(question)
    registration = profile.get('registration', {})
    taxes = bool(set(topics) & {'taxes', 'self_employed', 'closing'})
    clauses = []
    if 'registration' in topics:
        person = [_CITIZENSHIP[registration['citizenship']]] if 'citizenship' in registration else []
        if 'age' in registration:
            person.append(f"возраст {registration['age']} лет")
        if person:
            clauses.append((', '.join(person), None))
        if 'method' in registration:
            clauses.append(('подача ' + _METHODS[registration['method']], _METHODS[registration['method']]))
    if 'region' in profile:
        clauses.append(('регион ' + profile['region'], profile['region']))
    if taxes and 'selected_regime' in profile:
        name = _REGIMES[profile['selected_regime']]
        clauses.append(('выбранный режим ' + name, name))
    if taxes and 'employees' in profile:
        clauses.append((f"работники {profile['employees']}", None))
    for key, label, relevant in [
        ('revenue', 'доход за год', r'доход|выручк|ндс|лимит|огранич|взнос'),
        ('expenses', 'расходы за год', r'расход|затрат'),
        ('clients', 'покупатели', r'клиент|покупател|нпд|самозанят'),
    ]:
        if taxes and key in profile and re.search(relevant, text):
            clauses.insert(0, (label + ' ' + str(profile[key]), None))
    if 'changes' in topics and profile.get('okved', {}).get('primary'):
        code = profile['okved']['primary']
        clauses.insert(0, ('ОКВЭД ' + code, code))
    if registration.get('date') and re.search(r'срок|когда|дат|напомин', text):
        clauses.insert(0, ('дата регистрации ' + registration['date'], None))
    if taxes and 'activity' in profile:
        clauses.append(('деятельность ' + profile['activity'], None))
    return clauses


def enrich_plan(plan: Plan, profile: dict, question: str) -> Plan:
    """Append at most three relevant conditions; preserve the original query.

    No form number, tax rate, requirement or missing value is inferred. The full
    profile belongs in the planner input; the search receives a small subset.
    """
    data = plan.model_dump()
    if not plan.relevant or plan.clarification or not plan.query.strip():
        return Plan.model_validate(data)
    profile = build_profile_context(profile, question)
    query, keywords = plan.query, list(plan.keywords)
    terms = set(re.findall(r'[а-яa-z0-9]+', _normal(' '.join([query, *keywords]))))
    added, added_words, count = 0, 0, 0
    for clause, keyword in _clauses(profile, plan.topics, question):
        words = re.findall(r'[а-яa-z0-9]+', _normal(clause))
        if _normal(clause) in _normal(query) or len(clause) > 100:
            continue
        if count == 3 or added_words + len(words) > 12 or added + len(clause) > 120:
            continue
        # Storage selects at most 20 distinct query terms. Appended defaults
        # must not push the original user's terms out of that selection.
        if len(terms | set(words)) > 20:
            continue
        if len(query) + 2 + len(clause) > 500:
            continue
        query += '; ' + clause
        added += len(clause)
        added_words += len(words)
        terms.update(words)
        count += 1
        if keyword and len(keywords) < min(8, len(plan.keywords) + 2) and _normal(keyword) not in {_normal(value) for value in keywords}:
            keywords.append(keyword)
    return Plan.model_validate({**data, 'query': query, 'keywords': keywords})


def profile_summary(profile: dict) -> str:
    """Describe supplied business defaults, never identity/form data, <=450 chars."""
    profile = build_profile_context(profile)
    pieces = []
    for key, label in [('activity', 'деятельность'), ('region', 'регион'), ('revenue', 'доход за год'),
                       ('expenses', 'расходы за год'), ('employees', 'работники'), ('clients', 'покупатели')]:
        if key in profile:
            pieces.append(f'{label}: {profile[key]}')
    if profile.get('selected_regime'):
        pieces.append('выбранный режим: ' + _REGIMES[profile['selected_regime']])
    registration = profile.get('registration', {})
    if registration.get('citizenship'):
        pieces.append(_CITIZENSHIP[registration['citizenship']])
    if 'age' in registration:
        pieces.append(f"возраст: {registration['age']} лет")
    if registration.get('method'):
        pieces.append('подача: ' + _METHODS[registration['method']])
    if registration.get('date'):
        pieces.append('дата регистрации: ' + registration['date'])
    if not pieces:
        return ''
    prefix, selected = 'Учитываю вашу анкету: ', []
    for piece in pieces:
        if len(prefix) + len('; '.join([*selected, piece])) + 1 <= 450:
            selected.append(piece)
    return prefix + '; '.join(selected) + '.' if selected else ''


def summary(state_or_profile, question='') -> str:
    return profile_summary(build_profile_context(state_or_profile, question))

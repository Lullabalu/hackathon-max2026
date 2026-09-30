"""Explicit personal questions answered from the saved business profile."""

import re


_MEMORY_FIELDS = {
    'region': {'какой у меня регион', 'какой регион я указал', 'какой регион я указала', 'в каком регионе мой бизнес'},
    'activity': {'чем я планирую заниматься', 'какая у меня деятельность', 'какой у меня бизнес'},
    'revenue': {'какой у меня доход', 'какой доход я указал', 'какой доход я указала'},
    'expenses': {'какие у меня расходы', 'какие расходы я указал', 'какие расходы я указала'},
    'employees': {'сколько у меня сотрудников', 'сколько у меня работников', 'сколько работников я указал'},
    'clients': {'кто мои клиенты', 'каких клиентов я указал', 'каких клиентов я указала'},
    'okved': {'какой у меня оквэд', 'какой оквэд я выбрал', 'какой оквэд я выбрала'},
}


def personal_intent(text):
    value = text.strip().casefold().replace('ё', 'е')
    if value.startswith('/ask '):
        value = value[5:]
    elif value.startswith('/'):
        return None
    value = ' '.join(re.findall(r'[а-яa-z0-9]+', value))
    for field, phrases in _MEMORY_FIELDS.items():
        if value in phrases:
            return 'field_' + field
    if value in {'моя анкета', 'мой профиль', 'покажи мою анкету', 'покажи мой профиль',
                 'что я указал в анкете', 'что я указала в анкете', 'какие данные я указал',
                 'ты помнишь мою анкету', 'какие данные ты обо мне помнишь'}:
        return 'profile'
    if re.fullmatch(r'(?:какой|какую) (?:налоговый режим|режим|систему налогообложения) '
                    r'(?:(?:мне|для меня) )?(?:выбрать|подходит|подойдет|лучше выбрать)(?: мне)?', value):
        return 'compare'
    if value in {'что мне выгоднее', 'какой режим для меня выгоднее', 'подбери мне налоговый режим',
                 'подбери режим по моей анкете', 'рассчитай налоги по моей анкете',
                 'сравни режимы для меня', 'сравни налоги для меня', 'что выгоднее для моего бизнеса'}:
        return 'compare'
    if value in {'сколько мне платить налогов', 'сколько налогов я буду платить',
                 'сколько налогов мне платить', 'какие налоги мне платить', 'какие налоги я буду платить',
                 'рассчитай мои налоги', 'рассчитай мой налог', 'посчитай мои налоги'}:
        return 'tax'
    if value in {'какие документы мне нужны', 'какие мне нужны документы',
                 'какие документы нужны мне', 'что мне нужно для регистрации ип',
                 'какие документы мне нужны для регистрации ип', 'какие документы нужны мне для регистрации ип',
                 'какие документы нужны для моего ип', 'подготовь мой чек лист',
                 'какие документы нужны для регистрации ип', 'какие документы нужны для открытия ип'}:
        return 'documents'
    if value in {'какой режим я выбрал', 'какой режим я выбрала', 'какой у меня налоговый режим',
                 'какую систему я выбрал', 'какую систему я выбрала'}:
        return 'regime'
    return None


def local_personal_intent(state, text):
    kind = personal_intent(text)
    if kind == 'documents':
        # A generic document question can mean any procedure. Use the registration
        # checklist automatically only when the registration profile establishes it.
        registration = (state or {}).get('registration', {})
        value = text.casefold().replace('ё', 'е')
        personal = any(word in value.split() for word in ('мне', 'мне?', 'моего', 'мой'))
        if not personal and not all(key in registration for key in ('citizenship', 'age', 'method')):
            return None
    return kind

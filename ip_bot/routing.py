"""Cheap, conservative routing of self-contained business questions.

This module identifies search topics only. It does not supply requirements,
deadlines, fees, or other facts that should be checked in official sources.
Ambiguous questions stay with the model, which also has conversation history.
"""

import re
import unicodedata

from .llm import Plan


_FORM_REFERENCE = re.compile(r'(?<![а-яa-z0-9])[рp]\s*([0-9]{5})(?![а-яa-z0-9])')
_NUMBERED_FORM = re.compile(r'\bформ[аыуе]\s*(?:№\s*)?(?:[рp]\s*)?([0-9]+(?:[.-][0-9]+)+|[0-9]{5,8})\b')
_FORM_TOPICS = {'21001': 'registration', '24001': 'changes', '26001': 'closing'}


def context_plan(question: str, history, profile: dict | None = None) -> Plan | None:
    """Resolve narrow follow-ups only when the user just named one known form.

    History is supplied by the caller for this user; nothing is retained here.
    Only the two latest turns are considered, matching the model's context.
    An answer mentioning a form does not establish the user's intended object:
    the latest question itself must identify it. No legal facts are supplied.
    """
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        return None
    text = unicodedata.normalize('NFKC', question).casefold().replace('ё', 'е')
    text = ' '.join(re.findall(r'[а-яa-z0-9]+', text))
    text = re.sub(r'^(?:(?:а|и|тогда)\s+){1,2}', '', text)
    pronoun = r'(?:его|ее)'
    submit = r'(?:подать|подавать|отправить|отправлять)'
    patterns = [
        (rf'куда (?:{pronoun} {submit}|{submit} {pronoun})',
         'Куда подавать форму {form}?', 'подача'),
        (rf'как (?:{pronoun} {submit}|{submit} {pronoun})',
         'Как подать форму {form}?', 'подача'),
        (rf'(?:когда|в какой срок|в какие сроки|до какого срока) (?:{pronoun} {submit}|{submit} {pronoun})',
         'В какой срок подавать форму {form}?', 'срок подачи'),
        (rf'(?:сколько времени|как долго) (?:{pronoun} рассматривают|рассматривают {pronoun})',
         'Сколько времени рассматривают форму {form}?', 'срок рассмотрения'),
        (rf'сколько стоит (?:{pronoun} {submit}|{submit} {pronoun})',
         'Сколько стоит подача формы {form}?', 'стоимость подачи'),
        (rf'какая (?:пошлина|госпошлина) за {pronoun} подачу',
         'Какая госпошлина за подачу формы {form}?', 'госпошлина'),
        (r'какие документы (?:нужно |надо )?(?:к нему|к ней) (?:приложить|прикладывать)',
         'Какие документы приложить к форме {form}?', 'документы'),
        (r'какие документы (?:нужны |нужны мне )?для (?:его|ее) подачи',
         'Какие документы нужны для подачи формы {form}?', 'документы'),
        (rf'где (?:{pronoun} взять|взять {pronoun}|{pronoun} скачать|скачать {pronoun})',
         'Где взять форму {form}?', 'форма'),
    ]
    matched = next(((template, keyword) for pattern, template, keyword in patterns
                    if re.fullmatch(pattern, text)), None)
    if matched is None:
        # Full matches reject added regions, dates, conditions, or tax changes.
        return None
    if history is None:
        return None
    try:
        recent = list(history)[-2:]
    except TypeError:
        return None
    if not recent or any(not isinstance(turn, dict) or
                         not isinstance(turn.get('question'), str) or
                         not isinstance(turn.get('answer', ''), str) for turn in recent):
        return None

    previous = recent[-1]['question'].strip()
    normalized = unicodedata.normalize('NFKC', previous).casefold().replace('ё', 'е')
    bare_form_question = re.fullmatch(
            r'(?:где|как|куда)\s+(?:(?:можно|мне)\s+)?(?:взять|найти|скачать|заполнить|подать|подавать)\s+'
            r'(?:(?:заявление|форму|бланк)\s+)?[рp]\s*\d{5}[?!.]*', normalized)
    if profile and not bare_form_question:
        # A bare form question contains no old region, amount or age that could
        # conflict with the current questionnaire. Richer history still needs
        # the planner to reconcile its conditions with the current profile.
        return None
    # A single form number is not necessarily the subject: in "паспорт для
    # Р21001" the pronoun may refer to the passport. Require a direct action
    # on the form and decline questions naming another possible document.
    other_documents = ('паспорт', 'квитанц', 'договор', 'уведомлен', 'справк',
                       'свидетельств', 'выписк', 'доверенност', 'разрешен',
                       'согласи', 'декларац', 'документ', 'копи', 'инструкц')
    if any(word.startswith(other_documents) for word in re.findall(r'[а-яa-z]+', normalized)):
        return None
    if not re.search(r'\b(?:взять|найти|скачать|заполнить|подать|подавать)\s+'
                     r'(?:(?:заявление|форму|бланк)\s+)?(?:по\s+форме\s+)?[рp]\s*[0-9]{5}\b', normalized):
        return None
    named_forms = set(_FORM_REFERENCE.findall(normalized))
    if len(named_forms) != 1:
        return None
    number = next(iter(named_forms))
    topic = _FORM_TOPICS.get(number)
    if topic is None:
        return None

    for turn in recent:
        previous_plan = local_plan(turn['question'])
        if previous_plan is None or previous_plan.topics != [topic]:
            return None
        context = unicodedata.normalize('NFKC', turn['question'] + '\n' + turn.get('answer', '')).casefold().replace('ё', 'е')
        references = set(_FORM_REFERENCE.findall(context)) | set(_NUMBERED_FORM.findall(context))
        # KND codes and other form numbers make a singular pronoun ambiguous.
        if references != {number} or re.search(r'\bкнд\s*[0-9]{7}\b', context):
            return None

    form = 'Р' + number
    template, keyword = matched
    query = template.format(form=form)
    if not bare_form_question:
        query += ' Предыдущий вопрос: ' + previous
    if len(query) > 500:
        # Never truncate the original region, date, or condition from context.
        return None
    return Plan(query=query, topics=[topic], keywords=[form, keyword],
                clarification='', relevant=True)


def local_plan(question: str, profile: dict | None = None) -> Plan | None:
    """Return a search plan only when the question names its subject explicitly.

    ``profile`` is accepted for callers' convenience. It never supplies missing
    intent: an earlier questionnaire about opening an IP does not establish what
    document a subsequent "where do I submit it?" refers to.
    """
    question = question.strip()
    if not question or len(question) > 500:
        return None
    text = unicodedata.normalize('NFKC', question).casefold().replace('ё', 'е')
    words = re.findall(r'[а-яa-z0-9]+', text)
    if not words:
        return None

    # Numbers are document identifiers supplied by the user, not inferred rules.
    forms = re.findall(r'(?<![а-яa-z0-9])[рp]\s*(21001|24001|26001)(?![а-яa-z0-9])', text)
    # These fragments depend on history even if a tax regime is mentioned.
    if re.search(r'\b(его|ее|их|него|нее|этому|этого|эту|это|этим|этом|туда|также|такой|такие)\b', text):
        return None
    if re.search(r'\bне\s+(про|о|об|для)\b', text):
        return None
    if re.match(r'^а\s+(если|для|по|насчет)\b', text):
        return None
    if words[0] in {'а', 'и', 'но', 'тогда'} and len(words) <= 4 and not forms:
        return None

    def has(*prefixes):
        return any(word.startswith(prefixes) for word in words)

    ip = 'ип' in words or (has('индивидуальн') and has('предпринимател'))
    usn = 'усн' in words or has('упрощенк') or (has('упрощенн') and has('систем', 'режим', 'налог'))
    psn = 'псн' in words or (has('патент') and (ip or usn or has('налог', 'режим')))
    npd = 'нпд' in words or has('самозанят') or (has('профессиональн') and has('доход') and has('налог'))
    registration = ip and (has('регистр', 'зарегистр', 'откры', 'откро') or
                           (has('стать') and has('предпринимател')))
    closing = ip and (has('закры', 'закро', 'ликвидир', 'ликвидац') or
                      (has('прекрат', 'прекращ') and has('деятельност')))
    changes = (has('измен', 'помен', 'смен', 'добав', 'удал', 'исключ', 'обнов') and
               (has('оквэд', 'егрип') or (ip and has('сведен', 'адрес', 'фамил', 'паспорт', 'код'))))
    taxes = usn or psn or (ip and has('налог', 'налогооблож') and has('режим', 'систем', 'выбор', 'выбрать'))
    if len(words) < 3 and not (forms or registration or closing or changes):
        return None

    topics = []
    keywords = []
    candidates = [
        ('registration', registration or '21001' in forms, 'регистрация'),
        ('taxes', taxes, 'налогообложение'),
        ('self_employed', npd, 'самозанятость'),
        ('changes', changes or '24001' in forms, 'изменения'),
        ('closing', closing or '26001' in forms, 'прекращение деятельности'),
    ]
    for topic, matched, keyword in candidates:
        if matched:
            topics.append(topic)
            keywords.append(keyword)
    if not topics or len(topics) > 2:
        return None

    # Preserve explicit search terms; never invent the form number for an action.
    for term, matched in [('ИП', ip), ('УСН', usn), ('ПСН', psn), ('НПД', npd),
                          ('ОКВЭД', has('оквэд')), ('документы', has('документ'))]:
        if matched:
            keywords.append(term)
    keywords.extend('Р' + form for form in dict.fromkeys(forms))
    return Plan(query=question, topics=topics, keywords=list(dict.fromkeys(keywords))[:8],
                clarification='', relevant=True)

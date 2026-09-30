"""Короткие разговорные ответы, для которых не нужны модель и источники."""

import re
import unicodedata


ABOUT = (
    'Я бот-помощник по ИП и документам в MAX. Помогаю разобраться с регистрацией '
    'ИП, налоговыми режимами и нужными документами; для таких вопросов ищу '
    'официальные материалы ФНС и даю ссылки. Я программа, не человек, '
    'и сам заявления не отправляю. Напишите, с чем нужна помощь.'
)
GREETING = (
    'Привет! Я помощник по ИП и документам в MAX. Задайте вопрос, например: '
    '«Какие документы нужны для регистрации ИП?» '
    '/begin — пройти анкету, /about — узнать обо мне.'
)

_GREETINGS = {
    'привет', 'приветик', 'приветствую', 'здравствуйте', 'здравствуй',
    'здрасьте', 'здарова', 'здорово', 'хай', 'йо', 'hello', 'hi',
    'добрый день', 'доброе утро', 'добрый вечер', 'доброго времени суток',
}
_THANKS = {
    'спасибо', 'спасибо большое', 'большое спасибо', 'огромное спасибо',
    'спасибо огромное', 'спасибо тебе', 'спасибо вам', 'спасибо тебе большое',
    'спасибо за помощь', 'спасибо за ответ', 'спасибо за информацию',
    'благодарю', 'благодарю тебя', 'благодарю вас', 'благодарю за помощь',
    'спс', 'пасиб', 'пасиба', 'thanks',
}
_GOODBYES = {
    'пока', 'пока пока', 'до свидания', 'до встречи', 'до связи',
    'увидимся', 'всего доброго', 'всего хорошего', 'спокойной ночи',
    'хорошего дня', 'хорошего вечера', 'спасибо пока', 'спасибо до свидания',
}
_HOW_ARE_YOU = {
    'как дела', 'как твои дела', 'как у тебя дела', 'как у вас дела',
    'как ты', 'как вы', 'как жизнь', 'как настроение', 'как твое настроение',
    'как поживаешь', 'как поживаете', 'что нового', 'как делишки',
    'как у тебя делишки', 'как оно', 'как сам', 'как сама',
}
_PRESENCE = {'ты тут', 'ты здесь', 'ты на связи', 'вы на связи', 'на связи', 'есть кто',
             'ты работаешь', 'ты работаешь сейчас', 'ты меня слышишь', 'алло', 'ау'}
_CHAT = {'давай поболтаем', 'давайте поболтаем', 'хочу поболтать',
         'просто поболтать', 'давай поговорим', 'давайте поговорим', 'поговорим'}
_SOCIAL_ANSWERS = {'у меня все хорошо', 'у меня все отлично', 'у меня нормально',
                   'у меня тоже все хорошо', 'тоже все хорошо', 'все хорошо'}

# Совпадает всё сообщение, а не отдельные ключевые слова. Поэтому приветствие
# перед вопросом о документах не мешает передать сам вопрос в поиск.
_ABOUT_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r'кто (?:ты|вы)(?: такой| такая| такие| вообще)?',
    r'(?:ты|вы) кто(?: такой| такая| такие| вообще)?',
    r'(?:расскажи|расскажите|рассказывай)(?: мне| нам)?(?: немного| подробнее)? о себе',
    r'(?:расскажи|расскажите)(?: мне)? (?:про себя|кто ты|что ты умеешь)',
    r'(?:расскажи|расскажите)(?: мне)? кто (?:ты|вы)(?: такой| такие)?',
    r'(?:что|че) (?:ты |вы )?(?:умеешь|умеете|можешь|можете)(?: делать)?',
    r'(?:что|че) (?:ты|вы) (?:можешь|можете) (?:сделать|предложить)',
    r'(?:чем|как) (?:ты |вы )?(?:можешь|можете)(?: мне| нам)? помочь',
    r'(?:чем|как) (?:ты |вы )?(?:мне |нам )?(?:можешь|можете) помочь',
    r'(?:чем|как) (?:ты |вы )?(?:помогаешь|помогаете)',
    r'чем (?:ты |вы )?(?:занимаешься|занимаетесь)',
    r'(?:чем|как) (?:ты |вы )?(?:полезен|полезна|полезны)',
    r'как (?:тебя|вас) (?:зовут|называют)',
    r'какое (?:у тебя |у вас )?имя',
    r'(?:твое|ваше) имя',
    r'назови (?:свое )?имя',
    r'как называется (?:этот )?бот',
    r'(?:зачем|для чего) (?:ты|вы|этот бот)(?: нужен|нужна|нужны)?',
    r'(?:какое |в чем )?(?:твое |ваше )?назначение',
    r'(?:какие |каковы )?(?:твои |ваши )?(?:возможности|функции)',
    r'(?:что это за|что за|кто такой этот) бот',
    r'(?:что|че) ты за (?:бот|нейросеть)',
    r'кто ты(?: такой)? и (?:что (?:ты )?умеешь|чем (?:ты )?занимаешься)',
    r'(?:ты|вы) (?:бот|робот|человек|нейросеть|нейронка|искусственный интеллект|живой|живая|реальный человек)',
    r'ты (?:бот или человек|человек или бот)',
    r'о (?:тебе|вас|боте)',
))
_ADDRESSES = {'бро', 'друг', 'дружище', 'пожалуйста'}
_PREFIXES = _ADDRESSES | {'бот', 'ну', 'а', 'эй', 'слушай', 'скажи', 'скажите'}
_PHRASES = sorted(
    [(phrase, kind) for kind, phrases in (
        ('greeting', _GREETINGS), ('thanks', _THANKS), ('goodbye', _GOODBYES),
        ('how', _HOW_ARE_YOU), ('presence', _PRESENCE), ('chat', _CHAT),
        ('social_answer', _SOCIAL_ANSWERS)) for phrase in phrases],
    key=lambda item: len(item[0]), reverse=True,
)


def _unwrap(text: str) -> str:
    words = text.split()
    while words and words[0] in _PREFIXES:
        words.pop(0)
    while words and words[-1] in _ADDRESSES:
        words.pop()
    return ' '.join(words)


def _social_prefix(text):
    """Longest complete social phrase at the start, never part of a word."""
    matches = [(len(phrase), kind) for phrase, kind in _PHRASES
               if text == phrase or text.startswith(phrase + ' ')]
    for pattern in _ABOUT_PATTERNS:
        match = pattern.match(text)
        if match and (match.end() == len(text) or text[match.end()] == ' '):
            matches.append((match.end(), 'about'))
    return max(matches, default=(0, None))


def _social_kinds(text):
    """All meaningful words must belong to social phrases, including combinations."""
    kinds = set()
    text = _unwrap(text)
    while text:
        length, kind = _social_prefix(text)
        if not length:
            return None
        kinds.add(kind)
        text = _unwrap(text[length:].strip())
        if text.startswith('и '):
            text = _unwrap(text[2:])
            if not text:
                return None
    return kinds or None


def strip_social_prefix(text: str) -> str:
    """Remove greeting clauses before the useful question, preserving its text.

    A phrase such as «как дела с заявлением» stays intact: a social phrase
    followed by other words is only removed at a sentence boundary. Commands
    are never promoted out of an ordinary message.
    """
    raw = unicodedata.normalize('NFKC', text).strip()
    if not raw or raw.startswith('/'):
        return text
    tokens = list(re.finditer(r'[а-яёa-z0-9]+', raw, flags=re.IGNORECASE))
    index = 0
    while index < len(tokens):
        words = [item.group().casefold().replace('ё', 'е') for item in tokens[index:]]
        candidate = ' '.join(words)
        if words[0] in _PREFIXES:
            count, kind = 1, 'address'
        else:
            length, kind = _social_prefix(candidate)
            if not length:
                break
            count = len(candidate[:length].split())
        next_index = index + count
        if next_index == len(tokens):
            # Pure conversation is handled by quick_reply; keep it intact here.
            return text
        gap = raw[tokens[next_index - 1].end():tokens[next_index].start()]
        if kind not in {'address', 'greeting', 'thanks'} and not re.search(r'[.!?;\n]', gap):
            break
        # Don't remove punctuation that may introduce a command, URL or amount.
        if '/' in gap or re.search(r'[^\s,;.!?…:—–\-👋🙂😊🙌🤝]', gap):
            break
        index = next_index
    return raw[tokens[index].start():] if index else text


def quick_reply(text: str) -> str | None:
    """Вернуть готовую реплику или None, если нужен обычный разбор вопроса.

    Не обращается к сети, модели или хранилищу и не меняет состояние анкеты.
    Команда /ask допускает те же разговорные реплики, что и обычный текст.
    """
    if not isinstance(text, str) or len(text) > 2000:
        return None
    text = unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е').strip()
    if re.match(r'^/ask\s', text):
        text = text[4:].strip()
    if text == '/about':
        return ABOUT
    if text.startswith('/'):
        return None
    text = ' '.join(re.findall(r'[а-яa-z0-9]+', text))
    kinds = _social_kinds(text)
    if kinds is None and text.endswith(' бот'):
        kinds = _social_kinds(text[:-4])
    if not kinds:
        return None
    if 'about' in kinds:
        return ABOUT
    if 'how' in kinds:
        return ('Привет! ' if 'greeting' in kinds else '') + 'Готов помогать 🙂 Я на связи!'
    if 'presence' in kinds:
        return 'Да, я здесь и на связи 🙂 Можешь писать.'
    if 'chat' in kinds:
        return 'Давай поболтаем 🙂 Я на связи.'
    if 'goodbye' in kinds:
        return 'До связи! Возвращайтесь, если понадобится помощь с ИП или документами.'
    if 'thanks' in kinds:
        return 'Пожалуйста! Если появятся вопросы об ИП или документах — пишите.'
    if 'social_answer' in kinds:
        return 'Хорошо 🙂 Я здесь, если захочешь что-нибудь обсудить.'
    if 'greeting' in kinds:
        return GREETING
    return None

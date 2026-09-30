"""Явное согласие на анкету и историю; до него текст с данными не ставится в очередь."""

CONSENT_VERSION = '2026-09-30'
PRIVACY = (
    'Для персонального подбора бот сохраняет анкету бизнеса, настройки, выбранный режим и последние вопросы '
    'в PostgreSQL. Обработку выполняет локальная модель; паспорт и сканы присылать не нужно. '
    'MAX хранит переписку по своим правилам. Технический ID чата нужен для доставки ответа.\n\n'
    'Анкета и до 20 последних вопросов хранятся до /delete. PDF отправляется только по вашему запросу. '
    'Для поиска ОКВЭД в сервис ФНС передаются поисковые слова о деятельности. Анкета целиком и регистрационные документы туда автоматически не отправляются. '
    'Новости и напоминания включаются отдельно и отключаются через /unsubscribe и /reminders off.\n\n'
    '/consent — согласен на сохранение анкеты и истории, продолжить\n'
    '/decline — отказать и удалить сохранённые данные\n'
    '/intro — общая информация без анкеты\n'
    '/delete — удалить анкету, историю, файлы и подписки из базы бота'
)
PUBLIC_COMMANDS = {'/start', '/menu', '/help', '/privacy', '/consent', '/decline', '/delete', '/new', '/about', '/intro', '/sources', '/consent_required', '/lessons', '/lesson 1', '/lesson 2', '/lesson 3'}


def has_consent(state):
    return bool(state and state.get('consent', {}).get('accepted') is True)


def inbound_text(state, text):
    from .conversation import quick_reply
    command = text.strip().split(maxsplit=1)[0].lower() if text.strip() else ''
    if command in PUBLIC_COMMANDS:
        # Не сохраняем лишние персональные данные после публичной команды.
        return command
    if has_consent(state) or text.strip().lower() in PUBLIC_COMMANDS:
        return text
    if quick_reply(text) is not None:
        return text
    # Не сохраняем произвольный текст/имя/телефон, присланные до согласия.
    return '/consent_required'

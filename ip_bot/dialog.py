"""Логика диалога, одинаковая для MAX и терминала."""

import copy
import re
from decimal import Decimal

from .content import HELP, SOURCES, WELCOME, checklist

QUESTIONS = [
    ("activity", "Чем планируете заниматься? Например: разрабатывать сайты на заказ."),
    ("region", "В каком регионе будет бизнес? Например: Самарская область."),
    ("revenue", "Какой ожидаете доход за год в рублях? Например: 1200000."),
    ("expenses", "Какие ожидаете расходы за год в рублях? Например: 300000 или 0."),
    ("employees", "Сколько планируете работников по трудовым договорам? Например: 0 или 2."),
    ("clients", "Кто будет платить?\n1 — физлица\n2 — ИП и организации\n3 — и те и другие"),
]
LABELS = {
    "activity": "Деятельность", "region": "Регион", "revenue": "Годовой доход",
    "expenses": "Годовые расходы", "employees": "Работники", "clients": "Покупатели",
}
CLIENTS = {"1": "Физлица", "2": "ИП и организации", "3": "Физлица, ИП и организации"}


def question(answers: dict) -> str:
    if len(answers) == len(QUESTIONS):
        return "Анкета заполнена. /profile — ответы, /checklist — следующие шаги."
    index = next(i for i, (field, _) in enumerate(QUESTIONS) if field not in answers)
    return f"Вопрос {index + 1} из {len(QUESTIONS)}. {QUESTIONS[index][1]}"


def parse_answer(field: str, text: str):
    if field in {"revenue", "expenses"}:
        value = re.sub(r"[\s\u00a0]", "", text).replace(",", ".")
        if not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,2})?", value):
            raise ValueError("Введите неотрицательную сумму числом, например 1200000 или 1200000,50.")
        # Деньги храним десятичной строкой, без погрешности float.
        return str(Decimal(value).quantize(Decimal("0.01")))
    if field == "employees":
        if not re.fullmatch(r"[0-9]{1,6}", text):
            raise ValueError("Введите целое неотрицательное число работников, например 0 или 2.")
        return int(text)
    if field == "clients":
        if text not in CLIENTS:
            raise ValueError("Выберите 1, 2 или 3.")
        return CLIENTS[text]
    limit = 300 if field == "activity" else 100
    if not 2 <= len(text) <= limit:
        raise ValueError(f"Введите от 2 до {limit} символов.")
    return text


def profile(answers: dict) -> str:
    if not answers:
        return "Ответов пока нет."
    lines = ["Ваши ответы:"]
    for field, _ in QUESTIONS:
        if field in answers:
            value = answers[field]
            if field in {"revenue", "expenses"}:
                value = f"{Decimal(value):,.2f}".replace(",", " ") + " ₽"
            lines.append(f"{LABELS[field]}: {value}")
    return "\n".join(lines)


def respond(state: dict | None, text: str) -> tuple[dict | None, str]:
    """Получить новое состояние и ответ. Исходный state не изменяем."""
    state = copy.deepcopy(state)
    text = text.strip()
    command = text.lower()
    answers = state["answers"] if state is not None else {}

    if command == "/start":
        return state, WELCOME if state is None else profile(answers) + "\n\n" + question(answers)
    if command in {"/help", "помощь"}:
        return state, HELP
    if command == "/sources":
        return state, SOURCES
    if command == "/delete":
        return None, "Анкета и история вопросов удалены из базы бота. Сообщения в самом MAX остаются в истории чата. Можно задать новый вопрос или начать анкету через /begin."
    if command in {"/begin", "начать", "продолжить"}:
        state = state if state is not None else {"answers": {}}
        return state, question(state["answers"])
    if command == "/new":
        return {"answers": {}}, question({})
    if command == "/profile":
        return state, profile(answers) + "\n\n" + (question(answers) if state is not None else "/begin — начать.")
    if command in {"/back", "назад"}:
        if not answers:
            return state, "Предыдущего ответа пока нет. " + (question(answers) if state is not None else "/begin — начать.")
        field = next(field for field, _ in reversed(QUESTIONS) if field in answers)
        answers.pop(field)
        return state, question(answers)
    if command in {"/checklist", "что дальше"}:
        if len(answers) < len(QUESTIONS):
            return state, "Сначала заполните анкету. " + (question(answers) if state is not None else "/begin — начать.")
        return state, checklist(answers)
    if text.startswith("/"):
        return state, "Не знаю эту команду. /help — список команд."
    if state is None:
        return None, WELCOME
    if len(answers) == len(QUESTIONS):
        return state, "Анкета уже заполнена. /checklist — следующие шаги, /back — исправить, /new — заполнить заново."

    field = next(field for field, _ in QUESTIONS if field not in answers)
    try:
        answers[field] = parse_answer(field, text)
    except ValueError as error:
        return state, f"{error}\n\n{question(answers)}"
    if len(answers) == len(QUESTIONS):
        return state, profile(answers) + "\n\n" + checklist(answers)
    return state, question(answers)

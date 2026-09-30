"""Deterministic, offline annual IP tax scenarios; never a regional eligibility oracle.

Money is Decimal internally and a two-decimal string in the public result. ``total``
means the complete *stated scenario*, not legally verified eligibility. Partial sums
are ``subtotal``; bounded estimates are ``range`` with an explicit ``scope``.

Optional rates are percentages (6, 15); client_person_share is a fraction [0, 1].
expenses_confirmed + expenses_include_contributions=True mean that ``expenses``
is the final deductible ledger total, including wages and contributions recognized
in this year. Without this clarification we do not subtract insurance twice.
vat_amount and employer_contributions are explicitly supplied annual liabilities,
not rates. This module never obtains them from the model or assumes missing = 0.
"""

from contextvars import ContextVar
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .regions import ausn_region_status


_DATA = json.loads((Path(__file__).resolve().parent.parent / "config/tax_rules.json").read_text())
_RULES = _DATA["rules"]
_ACTIVE_DATA = ContextVar("ip_tax_rules", default=_DATA)
_ZERO = Decimal("0")
_CENT = Decimal("0.01")
_TITLES = {
    "npd": "НПД", "usn_income": "УСН «Доходы»", "usn_profit": "УСН «Доходы − расходы»",
    "patent": "ПСН", "ausn_income": "АУСН «Доходы»",
    "ausn_profit": "АУСН «Доходы − расходы»", "osno": "ОСНО",
}
_LABELS = {
    "revenue": "годовой доход", "expenses": "годовые расходы", "employees": "число работников",
    "activity": "вид деятельности", "region": "регион", "clients": "категории клиентов",
    "client_person_share": "доля дохода от физлиц (0–1)",
    "npd_deduction_remaining": "остаток вычета НПД",
    "npd_activity_eligible": "подтверждение ограничений НПД по деятельности и клиентам",
    "previous_revenue": "доход прошлого года или new_ip=true",
    "payroll": "годовой ФОТ", "employer_contributions": "взносы за работников с травматизмом",
    "patent_cost": "стоимость патента до уменьшения", "patent_potential_income": "потенциальный доход за срок патента",
    "usn_income_rate": "региональная ставка УСН доходы", "usn_profit_rate": "региональная ставка УСН Д−Р",
    "expenses_confirmed": "налоговая допустимость расходов",
    "expenses_include_contributions": "включены ли признанные взносы в расходы",
    "monthly_income_expenses": "помесячные доходы и расходы АУСН",
    "vat_amount": "годовой НДС с учётом вычетов и льгот",
    "tax_resident": "налоговое резидентство", "other_income": "другие доходы основной базы НДФЛ",
    "usn_tax_holiday": "право на налоговые каникулы 0%",
}


def default_rules() -> dict:
    """A detached template for a reviewed admin revision."""
    return deepcopy(_DATA)


def validate_rules(payload: dict) -> dict:
    """Validate data, never code. New tax years/formulas require a code release."""
    if not isinstance(payload, dict) or set(payload) != set(_DATA):
        raise ValueError("Налоговые правила: неверный набор полей.")
    if not isinstance(payload['version'], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', payload['version']):
        raise ValueError("Нужна версия правил: 1–64 символа A–Z, 0–9, точка, дефис или подчёркивание.")
    try:
        checked = date.fromisoformat(payload['verified_on'])
        if checked.isoformat() != payload['verified_on'] or checked > date.today():
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("verified_on: нужна прошедшая или текущая дата ГГГГ-ММ-ДД.") from None
    if payload['supported_years'] != [2026] or type(payload['supported_years'][0]) is not int:
        raise ValueError("Алгоритм поддерживает только 2026 год; новый год требует отдельной проверки кода.")
    if not isinstance(payload['scope'], str) or not 1 <= len(payload['scope'].strip()) <= 1000:
        raise ValueError("scope: нужно описание области расчёта до 1000 символов.")
    rules = payload['rules']
    if not isinstance(rules, dict) or set(rules) != set(_RULES):
        raise ValueError("Налоговые правила: требуется полный набор разделов.")

    def decimal(value, *, rate=False, positive=False):
        if not isinstance(value, str) or not re.fullmatch(r'\d{1,15}(?:\.\d{1,8})?', value):
            raise ValueError("Численные правила задаются неотрицательными десятичными строками без экспоненты.")
        number = Decimal(value)
        if number > (1 if rate else Decimal('100000000000000')) or positive and number <= 0:
            raise ValueError("Численное правило вне допустимых границ.")
        return number

    for name, rule in rules.items():
        if not isinstance(rule, dict) or set(rule) != set(_RULES[name]):
            raise ValueError(f"Раздел {name}: неверный набор полей.")
        for key, value in rule.items():
            if key == 'sources':
                if not isinstance(value, list) or not 1 <= len(value) <= 20:
                    raise ValueError("Нужны 1–20 официальных источников для каждого раздела.")
                for url in value:
                    try:
                        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 or c == '\\' for c in url):
                            raise ValueError
                        parsed = urlsplit(url)
                        host = parsed.hostname or ''
                        if parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or not any(host == domain or host.endswith('.' + domain) for domain in ('nalog.gov.ru', 'nalog.ru')):
                            raise ValueError
                    except ValueError:
                        raise ValueError("Источники налоговых правил: только HTTPS официальных сайтов ФНС.") from None
            elif key in ('note', 'profit_regimes_base'):
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1000:
                    raise ValueError(f"{key}: требуется текст до 1000 символов.")
            elif key == 'employees_limit':
                if type(value) is not int or not 0 <= value <= 100000:
                    raise ValueError("employees_limit: целое число 0–100000.")
            elif key in ('tax_period', 'check_previous_year', 'own_contributions'):
                if type(value) is not type(_RULES[name][key]) or value != _RULES[name][key]:
                    raise ValueError(f"{name}.{key}: изменение требует нового алгоритма, не только данных.")
            elif key == 'resident_ndfl_brackets':
                if not isinstance(value, list) or not 1 <= len(value) <= 10:
                    raise ValueError("НДФЛ: нужно 1–10 ступеней.")
                last_ceiling, last_rate = Decimal(0), Decimal(0)
                for index, bracket in enumerate(value):
                    if not isinstance(bracket, dict) or set(bracket) != {'up_to', 'rate'}:
                        raise ValueError("Ступень НДФЛ: up_to и rate.")
                    rate = decimal(bracket['rate'], rate=True)
                    if rate < last_rate:
                        raise ValueError("Ставки НДФЛ должны быть неубывающими.")
                    last_rate = rate
                    if index == len(value) - 1:
                        if bracket['up_to'] is not None:
                            raise ValueError("Последняя ступень НДФЛ должна иметь up_to=null.")
                    else:
                        ceiling = decimal(bracket['up_to'], positive=True)
                        if ceiling <= last_ceiling:
                            raise ValueError("Пороги НДФЛ должны строго возрастать.")
                        last_ceiling = ceiling
            elif isinstance(_RULES[name][key], list):
                if not isinstance(value, list) or not 1 <= len(value) <= 5:
                    raise ValueError("Нужен непустой список ставок до 5 элементов.")
                for item in value:
                    decimal(item, rate=True)
            else:
                decimal(value, rate=(key.endswith('_rate') or key == 'employee_deduction_limit'), positive=key == 'revenue_limit')
    npd = rules['npd']
    if any(Decimal(npd[f'{kind}_deduction_rate']) > Decimal(npd[f'{kind}_rate']) for kind in ('person', 'business')):
        raise ValueError("Уменьшение ставки НПД не может превышать саму ставку.")
    if Decimal(npd['person_rate']) > Decimal(npd['business_rate']) or Decimal(npd['person_rate']) - Decimal(npd['person_deduction_rate']) > Decimal(npd['business_rate']) - Decimal(npd['business_deduction_rate']):
        raise ValueError("Текущий алгоритм НПД требует неубывающих ставок от физлиц к организациям.")
    if not Decimal(rules['vat']['usn_exemption_limit']) <= Decimal(rules['vat']['usn_reduced_rate_threshold']) <= Decimal(rules['usn']['revenue_limit']):
        raise ValueError("Пороги НДС/УСН должны быть согласованы.")
    if Decimal(rules['ausn']['minimum_rate']) > Decimal(rules['ausn']['profit_rate']):
        raise ValueError("Минимальная ставка АУСН не должна превышать ставку прибыли.")
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 200_000:
        raise ValueError("Налоговые правила превышают 200 КБ.")
    return deepcopy(payload)


def _d(rule: str, key: str) -> Decimal:
    return Decimal(_ACTIVE_DATA.get()['rules'][rule][key])


def _money(value: Decimal) -> str:
    return str(value.quantize(_CENT, rounding=ROUND_HALF_UP))


def _percent(rule: str, key: str) -> str:
    value = format(_d(rule, key) * 100, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def _rub(value: str) -> str:
    return f"{Decimal(value):,.2f}".replace(",", " ") + " ₽"


def _number(answers: dict, key: str, maximum: Decimal = Decimal("1000000000000000")):
    value = answers.get(key)
    if value is None or value == "":
        return None
    try:
        if isinstance(value, bool):
            raise ValueError(key)
        number = Decimal(str(value).replace(" ", "").replace(",", "."))
        if not number.is_finite() or number < 0 or number > maximum:
            raise ValueError(key)
        return number
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(key) from None


def _boolean(answers: dict, key: str):
    value = answers.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.casefold() in {"true", "да", "1", "false", "нет", "0"}:
        return value.casefold() in {"true", "да", "1"}
    raise ValueError(key)


def _row(key: str) -> dict:
    return {"id": key, "title": _TITLES[key], "status": "conditional", "details": [], "missing": [], "comparable": False}


def _missing(row: dict, *keys: str):
    row["missing"] = list(dict.fromkeys([*row["missing"], *keys]))
    row["status"] = "needs_input"
    row["comparable"] = False


def _exclude(row: dict, reason: str):
    row.update(status="excluded", reason=reason, comparable=False)


def _limits(row: dict, regime: str, revenue, employees) -> bool:
    rules = _ACTIVE_DATA.get()['rules'][regime]
    if revenue is not None and revenue > Decimal(rules["revenue_limit"]):
        _exclude(row, f"Доход выше лимита {_rub(rules['revenue_limit'])} за 2026 год.")
    elif employees is not None and employees > rules["employees_limit"]:
        _exclude(row, f"Число работников больше {rules['employees_limit']}.")
    else:
        if revenue is None:
            _missing(row, "revenue")
        if employees is None:
            _missing(row, "employees")
    return row["status"] != "excluded" and revenue is not None and employees is not None


def _own_contributions(base: Decimal) -> Decimal:
    extra = max(base - _d("own_contributions", "threshold"), _ZERO) * _d("own_contributions", "additional_rate")
    return Decimal(_money(_d("own_contributions", "fixed") + min(extra, _d("own_contributions", "additional_cap"))))


def _range(row: dict, lower: Decimal, upper: Decimal, scope: str):
    row["range"] = {"min": _money(lower), "max": _money(upper), "scope": scope}


def _complete(row: dict, tax: Decimal, own: Decimal, employee: Decimal, vat: Decimal):
    row["components"] = {"tax": _money(tax), "own_contributions": _money(own),
                         "employer_contributions": _money(employee), "vat": _money(vat)}
    # Sum the actual displayed money components, so the report cannot disagree
    # with its own breakdown by a kopeck after independent rounding.
    row["total"] = _money(sum((Decimal(value) for value in row["components"].values()), _ZERO))
    row["comparable"] = not row["missing"]


def _employee_amount(row: dict, values: dict, employees: int):
    if employees == 0:
        return _ZERO
    amount = values.get("employer_contributions")
    if amount is None:
        if values.get("payroll") is None:
            _missing(row, "payroll")
        _missing(row, "employer_contributions")
        row["details"].append("Один ФОТ не определяет взносы: важны выплаты по сотрудникам, тариф, льготы и травматизм.")
    return amount


def _vat(row: dict, values: dict, flags: dict, revenue: Decimal):
    if values.get("vat_amount") is not None:
        row["details"].append("НДС принят в указанной вами годовой сумме; ставка и вычеты отдельно не проверены.")
        return values["vat_amount"]
    threshold = _d("vat", "usn_exemption_limit")
    previous = values.get("previous_revenue")
    if revenue <= threshold and (flags.get("new_ip") is True or previous is not None and previous <= threshold):
        row["details"].append("Для обычной реализации учтено освобождение от НДС по указанному доходу; ввоз и обязанности налогового агента вне расчёта.")
        return _ZERO
    _missing(row, "vat_amount")
    if previous is None and flags.get("new_ip") is not True:
        _missing(row, "previous_revenue")
    row["details"].append(f"НДС неизвестен: порог УСН {_rub(_money(threshold))}, важны прошлый год, месяц превышения, ставка и вычеты.")
    return None


def _npd(row: dict, values: dict, clients: str, revenue: Decimal):
    share = values.get("client_person_share")
    if share is None:
        share = {"физлица": Decimal(1), "1": Decimal(1), "ип и организации": _ZERO, "2": _ZERO}.get(clients.casefold().strip())
    shares = [share] if share is not None else [_ZERO, Decimal(1)]
    if share is None:
        _missing(row, "client_person_share")
    deduction = values.get("npd_deduction_remaining")
    deductions = [deduction] if deduction is not None else [_ZERO, _d("npd", "deduction_limit")]
    if deduction is None:
        _missing(row, "npd_deduction_remaining")
    scenarios = []
    for part in shares:
        gross = revenue * (_d("npd", "person_rate") * part + _d("npd", "business_rate") * (1 - part))
        possible = revenue * (_d("npd", "person_deduction_rate") * part + _d("npd", "business_deduction_rate") * (1 - part))
        scenarios.extend(gross - min(balance, possible) for balance in deductions)
    if min(scenarios) == max(scenarios):
        _complete(row, scenarios[0], _ZERO, _ZERO, _ZERO)
    else:
        _range(row, min(scenarios), max(scenarios), "налог НПД; обязательные взносы за себя отсутствуют")
    row["details"].append(f"{_percent('npd', 'person_rate')}% с физлиц, {_percent('npd', 'business_rate')}% с ИП/организаций; вычет до {_rub(_money(_d('npd', 'deduction_limit')))} зависит от остатка. Фиксированных взносов за себя нет.")
    row["details"].append("Проверить ограничения: перепродажа, маркируемые/подакцизные товары, посредничество, гражданство, доходы от нынешнего/недавнего работодателя. Свободный текст деятельности не доказывает право на НПД.")


def _usn(row: dict, values: dict, flags: dict, revenue: Decimal, employees: int):
    profit = row["id"] == "usn_profit"
    rate_key = "usn_profit_rate" if profit else "usn_income_rate"
    supplied_rate = values.get(rate_key)
    rate = supplied_rate / 100 if supplied_rate is not None else _d("usn", "profit_rate" if profit else "income_rate")
    row["details"].append(f"Ставка {_money(rate * 100)}%: " + ("указана вами; право на неё требует проверки." if supplied_rate is not None else "стандартный сценарий; региональная ставка и льготы неизвестны."))
    if supplied_rate is None:
        _missing(row, rate_key)
    if rate == 0 and flags.get("usn_tax_holiday") is not True:
        _missing(row, "usn_tax_holiday")
    employee = _employee_amount(row, values, employees)
    vat = _vat(row, values, flags, revenue)
    if not profit:
        own = _own_contributions(revenue)
        before = revenue * rate
        reduction_limit = before * (_d("usn", "employee_deduction_limit") if employees else 1)
        tax = before - min(reduction_limit, own + (employee if employee is not None else _ZERO))
        if employee is not None and vat is not None:
            _complete(row, tax, own, employee, vat)
        else:
            row["subtotal"] = _money(tax + own + (employee if employee is not None else _ZERO))
            row["subtotal_scope"] = "налог после учтённых взносов + известные взносы; неизвестные НДС/взносы работников не включены"
            row["components"] = {"tax": _money(tax), "own_contributions": _money(own)}
        row["details"].append(f"Взносы уменьшают налог: без работников до 100%, с работниками до {_percent('usn', 'employee_deduction_limit')}%; дополнительный взнос отнесён на этот год без повторного учёта.")
        return
    expenses = values.get("expenses")
    if expenses is None:
        _missing(row, "expenses")
        return
    if flags.get("expenses_confirmed") is not True:
        _missing(row, "expenses_confirmed")
    minimum = _ZERO if rate == 0 and flags.get("usn_tax_holiday") is True else revenue * _d("usn", "minimum_rate")
    if flags.get("expenses_include_contributions") is True:
        base = max(revenue - expenses, _ZERO)
        own = _own_contributions(base)
        tax = max(base * rate, minimum)
        row["details"].append("Расходы приняты как итоговая налоговая сумма с уже включёнными признанными взносами; повторно их не вычитаем.")
        if employee is not None and vat is not None:
            _complete(row, tax, own, employee, vat)
        else:
            row["subtotal"] = _money(tax + own + (employee if employee is not None else _ZERO))
            row["subtotal_scope"] = "налог + известные взносы, без неизвестных НДС/взносов работников"
        return
    _missing(row, "expenses_include_contributions")
    # Bounds cover whether insurance is inside the entered expense total and
    # which payable contributions are recognized this year; no circular deduction.
    fixed = _d("own_contributions", "fixed")
    own_upper = _own_contributions(max(revenue - expenses, _ZERO))
    known_employee = employee if employee is not None else _ZERO
    lower = max(max(revenue - expenses - fixed - known_employee, _ZERO) * rate, minimum) + fixed + known_employee
    upper = max(max(revenue - expenses, _ZERO) * rate, minimum) + own_upper + known_employee
    if vat is not None:
        lower += vat
        upper += vat
    scope = "сценарий: все указанные расходы допустимы; уточнить состав взносов"
    if vat is None or employee is None:
        scope += "; неизвестные НДС/взносы работников не включены"
    _range(row, lower, upper, scope)
    row["details"].append(f"Минимальный налог {_percent('usn', 'minimum_rate')}% дохода (кроме подтверждённых каникул); дополнительный взнос {_percent('own_contributions', 'additional_rate')}% считается с разницы доходов и учитываемых расходов.")


def _patent(row: dict, values: dict, flags: dict, employees: int):
    previous = values.get("previous_revenue")
    if previous is not None and previous > _d("patent", "revenue_limit"):
        _exclude(row, f"Доход за 2025 год выше {_rub(_money(_d('patent', 'revenue_limit')))}: ПСН в 2026 году недоступна.")
        return
    if previous is None and flags.get("new_ip") is not True:
        _missing(row, "previous_revenue")
    cost, potential = values.get("patent_cost"), values.get("patent_potential_income")
    employee = _employee_amount(row, values, employees)
    row["details"].append("Цена зависит от регионального закона, деятельности, срока и физических показателей. Дополнительные взносы — с потенциального дохода за срок патента, не с выручки; при совмещении доходы для лимита суммируются.")
    if cost is None:
        _missing(row, "patent_cost")
    if potential is None:
        _missing(row, "patent_potential_income")
    if cost is None:
        return
    limit = cost * (_d("patent", "employee_deduction_limit") if employees else 1)
    known_employee = employee if employee is not None else _ZERO
    if potential is not None:
        own = _own_contributions(potential)
        tax = cost - min(limit, own + known_employee)
        if employee is not None:
            _complete(row, tax, own, employee, _ZERO)
        else:
            row["subtotal"] = _money(tax + own)
            row["subtotal_scope"] = "патент после уменьшения + взносы за себя, без неизвестных взносов работников"
    else:
        fixed = _d("own_contributions", "fixed")
        maximum = fixed + _d("own_contributions", "additional_cap")
        _range(row, cost - min(limit, fixed + known_employee) + fixed + known_employee,
               cost - min(limit, maximum + known_employee) + maximum + known_employee,
               "патент + взносы при неизвестной потенциальной базе; неизвестные взносы работников не включены")


def _ausn(row: dict, values: dict, flags: dict, revenue: Decimal, employees: int):
    accident = _d("ausn", "employee_accident_annual") if employees else _ZERO
    row["details"].append("Проверка региона относится к месту регистрации ИП. Также проверить деятельность, банк и безналичную зарплату. Совмещения с другими спецрежимами нет.")
    row["details"].append(f"Взносы за себя и обычные взносы работников — 0; при работниках травматизм {_rub(_money(_d('ausn', 'employee_accident_annual')))} за полный год.")
    if row["id"] == "ausn_income":
        _complete(row, revenue * _d("ausn", "income_rate"), _ZERO, accident, _ZERO)
        return
    expenses = values.get("expenses")
    if expenses is None:
        _missing(row, "expenses")
        return
    if flags.get("expenses_confirmed") is not True:
        _missing(row, "expenses_confirmed")
    lower = max(max(revenue - expenses, _ZERO) * _d("ausn", "profit_rate"), revenue * _d("ausn", "minimum_rate"))
    upper = revenue * _d("ausn", "profit_rate")
    row["details"].append(f"{_percent('ausn', 'profit_rate')}% прибыли, минимум {_percent('ausn', 'minimum_rate')}% дохода считаются каждый месяц; годовые суммы не дают точный годовой налог. Диапазон без убытков прошлых лет.")
    if lower == upper:
        _complete(row, lower, _ZERO, accident, _ZERO)
    else:
        _missing(row, "monthly_income_expenses")
        _range(row, lower + accident, upper + accident, "годовой налог АУСН + травматизм при указанной сумме допустимых расходов")


def _ndfl(base: Decimal) -> Decimal:
    previous, tax = _ZERO, _ZERO
    for bracket in _ACTIVE_DATA.get()['rules']["osno"]["resident_ndfl_brackets"]:
        ceiling = Decimal(bracket["up_to"]) if bracket["up_to"] is not None else base
        tax += max(min(base, ceiling) - previous, _ZERO) * Decimal(bracket["rate"])
        previous = ceiling
        if base <= ceiling:
            break
    return tax


def _osno(row: dict, values: dict, flags: dict, revenue: Decimal, employees: int):
    expenses = values.get("expenses")
    if expenses is None:
        _missing(row, "expenses")
        return
    employee = _employee_amount(row, values, employees)
    for flag in ("expenses_confirmed", "expenses_include_contributions", "tax_resident"):
        if flags.get(flag) is not True:
            _missing(row, flag)
    for key in ("other_income", "vat_amount"):
        if values.get(key) is None:
            _missing(row, key)
    base = max(revenue - expenses, _ZERO)
    other = values.get("other_income")
    tax = _ndfl(base + other) - _ndfl(other) if other is not None else _ndfl(base)
    own = _own_contributions(base)
    row["components"] = {"scenario_ndfl": _money(tax), "scenario_own_contributions": _money(own)}
    row["details"].append("Сценарий НДФЛ резидента по прогрессивной шкале с документально подтверждённой прибыли; расходы включают признанные взносы. Другие доходы основной базы меняют ступень ставки.")
    row["details"].append(f"Основная ставка НДС {_percent('vat', 'standard_rate')}%; возможны другие ставки, освобождения и вычеты: без данных об операциях нельзя умножить всю выручку на основную ставку.")
    if not row["missing"] and employee is not None:
        _complete(row, tax, own, employee, values["vat_amount"])


def compare_taxes(answers: dict, *, today=None, rules=None) -> dict:
    """Use one isolated, validated rules snapshot for this entire calculation."""
    token = _ACTIVE_DATA.set(validate_rules(rules) if rules is not None else _DATA)
    try:
        return _compare_taxes(answers, today=today)
    finally:
        _ACTIVE_DATA.reset(token)


def _compare_taxes(answers: dict, *, today=None) -> dict:
    """Return serializable planning scenarios without any network/model calls.

    Only 2026 is implemented. A recommendation compares at least two numerically
    complete scenarios with all relevant inputs supplied; legal conditions remain.
    Invalid explicit values return needs_input rows, not zero-valued estimates.
    """
    today = today or date.today()
    data = _ACTIVE_DATA.get()
    result = {"year": answers.get("year", today.year), "rules_version": data["version"],
              "verified_on": data["verified_on"], "rows": [_row(key) for key in _TITLES],
              "recommendation": None, "notes": [], "sources": []}
    source_urls = dict.fromkeys(url for rule in data['rules'].values() for url in rule["sources"])
    result["sources"] = [{"title": "ФНС России", "url": url, "verified_on": data["verified_on"]} for url in source_urls]
    for row in result['rows']:
        regime = row['id'].split('_')[0]
        sections = [regime]
        if regime in {'usn', 'patent', 'osno'}:
            sections += ['own_contributions', 'employer_contributions']
        if regime in {'usn', 'osno'}:
            sections.append('vat')
        urls = dict.fromkeys(url for section in sections for url in data['rules'][section]['sources'])
        row['sources'] = [{'title': 'ФНС России', 'url': url} for url in urls]
    try:
        raw_year = str(result["year"])
        if not raw_year.isdigit() or len(raw_year) != 4:
            raise ValueError("year")
        result["year"] = int(raw_year)
        if result["year"] not in data["supported_years"]:
            result["notes"] = [f"Правила за {result['year']} год не проверены. Расчёт не выполнен; правила 2026 года не перенесены на другой год."]
            for row in result["rows"]:
                _missing(row, "year")
                row["reason"] = result["notes"][0]
            return result
        values = {key: _number(answers, key) for key in (
            "revenue", "expenses", "employees", "payroll", "patent_cost", "previous_revenue",
            "patent_potential_income", "employer_contributions", "vat_amount", "other_income")}
        for key, maximum in (("client_person_share", "1"), ("npd_deduction_remaining", _d('npd', 'deduction_limit')),
                             ("usn_income_rate", _d('usn', 'income_rate') * 100), ("usn_profit_rate", _d('usn', 'profit_rate') * 100)):
            values[key] = _number(answers, key, Decimal(maximum))
        for key, minimum in (("usn_income_rate", Decimal(1)), ("usn_profit_rate", Decimal(5))):
            if values[key] is not None and values[key] != 0 and values[key] < minimum:
                raise ValueError(key)
        flags = {key: _boolean(answers, key) for key in (
            "new_ip", "expenses_confirmed", "expenses_include_contributions", "tax_resident", "usn_tax_holiday", "npd_activity_eligible")}
        employee_value = values["employees"]
        if employee_value is not None and employee_value != employee_value.to_integral_value():
            raise ValueError("employees")
        employees = int(employee_value) if employee_value is not None else None
        if flags["new_ip"] is True and values["previous_revenue"] not in (None, _ZERO):
            raise ValueError("previous_revenue")
        if employees == 0 and any(values[key] not in (None, _ZERO) for key in ("payroll", "employer_contributions")):
            raise ValueError("employees")
    except ValueError as error:
        key = str(error)
        result["notes"] = [f"Исправьте параметр {key}: требуется корректное неотрицательное значение в допустимых пределах."]
        for row in result["rows"]:
            _missing(row, key)
            row["reason"] = result["notes"][0]
        return result

    revenue = values["revenue"]
    for row in result["rows"]:
        regime = row["id"].split("_")[0]
        if regime == 'ausn':
            region = ausn_region_status(answers.get('region'), on=today)
            row['region_check'] = region
            row['details'].append(region['reason'])
            if region['available'] is False:
                _exclude(row, region['reason'])
                continue
            if region['available'] is None:
                _missing(row, 'region')
            elif region['starts_on'] > f"{result['year']}-01-01":
                row['details'].append('АУСН введена в регионе в течение 2026 года. Годовая сумма ниже — условный сценарий; для фактического периода применения нужен отдельный расчёт.')
        if regime != "osno" and not _limits(row, regime, revenue, employees):
            continue
        if regime == "osno" and (revenue is None or employees is None):
            _missing(row, *(key for key in ("revenue", "employees") if values[key] is None))
            continue
        if regime == "npd":
            if flags['npd_activity_eligible'] is False:
                _exclude(row, "По указанным сведениям деятельность или доходы не соответствуют ограничениям НПД.")
                continue
            if flags['npd_activity_eligible'] is not True:
                # A money calculation alone must not promote e.g. resale to a
                # recommended NPD scenario. Keep the amount conditional until
                # the user explicitly confirms the activity/client restrictions.
                row['missing'].append('npd_activity_eligible')
                row['details'].append('До подтверждения ограничений по деятельности и клиентам НПД не участвует в рекомендации.')
            _npd(row, values, str(answers.get("clients", "")), revenue)
        elif regime == "usn":
            _usn(row, values, flags, revenue, employees)
        elif regime == "patent":
            _patent(row, values, flags, employees)
        elif regime == "ausn":
            _ausn(row, values, flags, revenue, employees)
        else:
            _osno(row, values, flags, revenue, employees)
        if row["status"] != "excluded":
            for key in ("activity", "region"):
                if key == 'region' and regime == 'ausn' and row['region_check']['available'] is True:
                    continue
                if not isinstance(answers.get(key), str) or not answers[key].strip():
                    _missing(row, key)
            row["details"].append("Право на режим и региональные условия по этой анкете не подтверждены.")
    complete = [row for row in result["rows"] if row["comparable"] and "total" in row and row["status"] != "excluded"]
    if len(complete) >= 2:
        minimum = min(Decimal(row["total"]) for row in complete)
        best = [row for row in complete if Decimal(row["total"]) == minimum]
        result["recommendation"] = {
            "ids": [row["id"] for row in best], "total": _money(minimum),
            "compared_ids": [row["id"] for row in complete],
            "text": "Среди полностью рассчитанных сценариев при указанных условиях меньше: "
                    + ", ".join(row["title"] for row in best) + f" ({_rub(_money(minimum))}). "
                    "Право на режим надо подтвердить; неполные варианты в рейтинг не входят.",
        }
    result["notes"] = [
        "План за полный 2026 год, суммы округлены до копеек. Неполный год, освобождения от взносов, совмещение режимов и прошлые убытки требуют отдельного расчёта.",
        "Сравниваем налог режима + обязательные взносы ИП/работников + известный НДС. Зарплата и удержанный из неё НДФЛ не добавляются как отдельный налог ИП; имущественные налоги, импорт и обязанности налогового агента вне расчёта.",
        "Расходы должны быть налогово допустимыми; взносы, уже включённые в расходы, повторно не вычитаются. Для режимов с НДС суммы доходов/расходов вводятся с учётом их налогового порядка, а НДС — отдельно.",
    ]
    if result["recommendation"] is None:
        result["notes"].append("Рекомендации пока нет: недостаточно двух полностью рассчитанных сопоставимых сценариев. Неизвестные суммы не равны нулю.")
    return result


def render_comparison(result: dict) -> str:
    """Render every regime and material uncertainty in a single MAX message."""
    lines = [f"Налоги ИП, {result['year']}. Условные годовые сценарии."]
    if result["year"] not in _DATA["supported_years"] or all(row.get("reason") for row in result["rows"]):
        return "\n".join([lines[0], *result.get("notes", [])])[:3700]
    statuses = {"available": "доступен", "conditional": "при указанных условиях", "excluded": "исключён", "needs_input": "уточнить"}
    for row in result["rows"]:
        line = f"{row['title']}: {statuses[row['status']]}"
        if "total" in row:
            line += "; " + _rub(row["total"])
        elif "range" in row:
            bounds = row["range"]
            line += f"; {_rub(bounds['min'])} – {_rub(bounds['max'])} ({bounds['scope']})"
        elif "subtotal" in row:
            line += "; часть суммы " + _rub(row["subtotal"]) + " (" + row["subtotal_scope"] + ")"
        elif row["id"] == "osno" and "components" in row:
            line += "; только сценарий НДФЛ " + _rub(row["components"]["scenario_ndfl"]) + ", общий итог не рассчитан"
        if row.get("reason"):
            line += ". " + row["reason"]
        if row["missing"]:
            line += ". Нужны: " + ", ".join(_LABELS.get(key, key) for key in row["missing"])
        lines.append(line + ".")
    recommendation = result.get("recommendation")
    lines.append(recommendation["text"] if recommendation else "Пока недостаточно полных данных для рекомендации; неполные суммы не участвуют в рейтинге.")
    lines.extend([
        "Право на режим/региональные льготы не подтверждено. НПД: без работников, ограничения на деятельность; НПД/АУСН без фиксированных взносов за себя. АУСН Д−Р считается помесячно.",
        "Полный год; расходы налогово допустимые. Имущественные налоги, импорт и обязанности налогового агента вне расчёта. Уточнения: /tax_setup или /set.",
        "Правила проверены " + result["verified_on"] + ". ФНС: https://www.nalog.gov.ru/new2026/ ; https://npd.nalog.ru/ ; https://ausn.nalog.gov.ru/",
    ])
    # All seven rows remain visible. In unusually verbose input-error cases the
    # structured result still retains the full explanation for export.
    text = "\n\n".join(lines)
    return text if len(text) <= 3700 else text[:3699] + "…"


def render_tax_details(result: dict, regime_id: str) -> str:
    """One complete scenario explanation, with whole conditions and source links.

    Conditions and money have priority over the number of repeated citations.
    Oversized arbitrary legacy data is explicitly refused, never cut mid-rule.
    """
    regime_id = 'patent' if regime_id == 'psn' else regime_id
    row = next((entry for entry in result.get('rows', []) if entry.get('id') == regime_id), None)
    if row is None:
        return 'Для этого режима нет расчёта. /taxes — получить актуальное сравнение.'
    statuses = {'available': 'По указанным условиям', 'conditional': 'Условный сценарий',
                'needs_input': 'Нужны уточнения', 'excluded': 'Режим исключён'}
    heading = f"{row['title']}, {result['year']}. {statuses[row['status']]}."
    lines = [heading]
    if row.get('reason'):
        lines.append(row['reason'])
    if 'total' in row:
        lines.append('Итог указанного сценария: ' + _rub(row['total']) + '.')
    if 'range' in row:
        bounds = row['range']
        lines.append(f"Диапазон: {_rub(bounds['min'])} – {_rub(bounds['max'])}.\nУсловия диапазона: {bounds['scope']}.")
    if 'subtotal' in row:
        lines.append('Частичная сумма: ' + _rub(row['subtotal']) + '.\n' + row['subtotal_scope'] + '.')
    component_labels = {
        'tax': 'Налог режима', 'own_contributions': 'Взносы ИП за себя',
        'employer_contributions': 'Взносы за работников', 'vat': 'НДС',
        'scenario_ndfl': 'НДФЛ по условному сценарию',
        'scenario_own_contributions': 'Взносы ИП по условному сценарию',
    }
    components = row.get('components', {})
    if components:
        label = 'Состав итога:' if 'total' in row else 'Известные/условные компоненты; общего итога нет:'
        lines.append(label + '\n' + '\n'.join(f"{component_labels.get(key, key)}: {_rub(value)}"
                                               for key, value in components.items()))
    if row.get('missing'):
        lines.append('Нужно уточнить: ' + '; '.join(_LABELS.get(key, key) for key in row['missing']) + '.')
    lines.extend(dict.fromkeys(row.get('details', [])))
    # Only calculation-wide assumptions belong in this single-regime view;
    # the ranking's "not enough comparable scenarios" message does not.
    notes = [note for note in result.get('notes', []) if not note.startswith('Рекомендации пока нет:')]
    if row['status'] == 'excluded':
        notes = []
    metadata = f"Правила: версия {result.get('rules_version', 'не указана')}; проверены {result.get('verified_on', 'дата не указана')}."
    footer = '/taxes — общее сравнение; /tax_setup или /set — уточнить данные.'
    if row['status'] != 'excluded':
        footer = 'Право на режим и региональные условия требуют проверки.\n' + footer
    sources = row.get('sources', [])
    # Old persisted results may have only a common source list. Use only links
    # known to belong to this regime rather than borrowing an unrelated source.
    if not sources:
        section = regime_id.split('_')[0]
        expected = set(_RULES.get(section, {}).get('sources', []))
        sources = [source for source in result.get('sources', []) if source.get('url') in expected]
    urls = list(dict.fromkeys(source['url'] for source in sources if isinstance(source.get('url'), str)))

    def body(selected_notes):
        return '\n\n'.join([*lines, *selected_notes, metadata, footer])

    text = body(notes)
    # Preserve all material assumptions in a shorter formulation when the first
    # official link is unusually long (some FNS PDF URLs exceed 2000 characters).
    shortest = min((len(url) for url in urls), default=0)
    if len(text) + shortest + 16 > 3900 and any(key in row for key in ('total', 'subtotal', 'range', 'components')):
        notes = [
            'План за полный год, до копеек. Неполный год, освобождения от взносов, совмещение режимов и прошлые убытки требуют отдельного расчёта. '
            'Зарплата и удержанный из неё НДФЛ не прибавляются к налогам ИП. Имущественные налоги, импорт и обязанности налогового агента вне расчёта. '
            'Доходы/расходы вводятся по налоговому порядку; расходы допустимые, признанные взносы повторно не вычитаются, НДС учитывается отдельно.'
        ]
        text = body(notes)
    if len(text) > 3900:
        # All current validated calculator rows fit. This branch makes an old or
        # externally supplied oversized result safe without hiding a condition.
        return (_TITLES.get(regime_id, 'Выбранный режим') + '\n\nПолная детализация не помещается в одно сообщение; '
                'обрезанный расчёт не показан. Пересчитайте актуальные данные через /taxes.')
    links = []
    for url in urls:
        candidate = '\n\nИсточники ФНС:\n' + '\n'.join([*links, url])
        if len(text) + len(candidate) <= 3900:
            links.append(url)
    if links:
        text += '\n\nИсточники ФНС:\n' + '\n'.join(links)
    else:
        notice = '\n\nСсылки не помещаются в сообщение.' if urls else '\n\nСсылки в этом расчёте недоступны; обновите его: /taxes.'
        if len(text) + len(notice) <= 3900:
            text += notice
    return text

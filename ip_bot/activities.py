"""Ограниченный проверяемый подбор ОКВЭД без модели и сетевых запросов."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit


CATALOG_FILE = Path(__file__).resolve().parent.parent / 'config/activities.json'
_DEFAULT_CATALOG = json.loads(CATALOG_FILE.read_text(encoding='utf-8'))


def default_catalog() -> dict:
    return deepcopy(_DEFAULT_CATALOG)


def validate_catalog(payload: dict) -> dict:
    """Reviewed labels/codes/sources only; matching rules remain trusted code."""
    if not isinstance(payload, dict) or set(payload) != set(_DEFAULT_CATALOG):
        raise ValueError('Каталог ОКВЭД: неверный набор полей.')
    if type(payload['schema_version']) is not int or payload['schema_version'] != 1:
        raise ValueError('Поддерживается schema_version=1.')
    try:
        checked = date.fromisoformat(payload['checked_on'])
        if checked.isoformat() != payload['checked_on'] or checked > date.today():
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('checked_on: нужна прошедшая или текущая дата ГГГГ-ММ-ДД.') from None

    def text(value, label, maximum):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum or any(ord(c) < 32 and c not in '\n\t' for c in value):
            raise ValueError(f'{label}: нужна строка до {maximum} символов без управляющих знаков.')

    def source_url(value):
        try:
            if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or c == '\\' for c in value):
                raise ValueError
            parsed = urlsplit(value)
            host = parsed.hostname or ''
            domains = ('nalog.gov.ru', 'nalog.ru', 'rosstat.gov.ru', 'rospotrebnadzor.ru')
            if parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or not any(host == domain or host.endswith('.' + domain) for domain in domains):
                raise ValueError
        except ValueError:
            raise ValueError('Источники ОКВЭД: HTTPS сайтов ФНС, Росстата или Роспотребнадзора.') from None

    text(payload['coverage'], 'coverage', 1000)
    text(payload['explanation'], 'explanation', 1500)
    if payload['official_search'] != 'https://service.nalog.ru/okved/':
        raise ValueError('official_search должен вести в официальный сервис Мой ОКВЭД.')
    sources = payload['sources']
    if not isinstance(sources, dict) or not 1 <= len(sources) <= 100:
        raise ValueError('Нужно 1–100 источников каталога.')
    for key, source in sources.items():
        if not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key):
            raise ValueError('Некорректный идентификатор источника.')
        if not isinstance(source, dict) or set(source) not in ({'title', 'url'}, {'title', 'url', 'scope'}):
            raise ValueError('Источник: title, url и необязательное scope.')
        text(source['title'], 'title', 300)
        source_url(source['url'])
        if 'scope' in source:
            text(source['scope'], 'scope', 1000)
    if not {'fns_search', 'fns_types', 'alcohol', 'medical_beauty'} <= set(sources) or sources['fns_search']['url'] != payload['official_search']:
        raise ValueError('Отсутствуют обязательные официальные источники распознавания.')
    for group, keys in (('activities', {'code', 'title', 'sources', 'warnings'}), ('restrictions', {'sources', 'warnings'})):
        entries = payload[group]
        if not isinstance(entries, dict) or set(entries) != set(_DEFAULT_CATALOG[group]):
            raise ValueError(f'{group}: нужны все поддерживаемые ID; новые правила распознавания требуют обновления кода.')
        codes = set()
        for entry in entries.values():
            if not isinstance(entry, dict) or set(entry) != keys:
                raise ValueError(f'{group}: неверный набор полей записи.')
            if group == 'activities':
                if not isinstance(entry['code'], str) or not re.fullmatch(r'\d{2}\.\d{2}(?:\.\d{1,2})?', entry['code']) or entry['code'] in codes:
                    raise ValueError('Код ОКВЭД: уникальные 4–6 цифр с точками, например 62.01.')
                codes.add(entry['code'])
                text(entry['title'], 'Название деятельности', 500)
            links = entry['sources']
            if not isinstance(links, list) or not 1 <= len(links) <= 20 or any(not isinstance(key, str) or key not in sources for key in links):
                raise ValueError('Запись должна ссылаться на 1–20 существующих источников.')
            warnings = entry['warnings']
            if not isinstance(warnings, list) or not 1 <= len(warnings) <= 10:
                raise ValueError('Нужны 1–10 предупреждений/условий; ограничения нельзя удалить пустым списком.')
            for warning in warnings:
                text(warning, 'Предупреждение', 1000)
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 200_000:
        raise ValueError('Каталог ОКВЭД превышает 200 КБ.')
    return deepcopy(payload)


def _normal(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е').split())


def _match(text):
    """IDs are evidence from explicit activities, not speculative extra codes."""
    found = []
    cake = bool(re.search(r'\b(торт\w*|пирожн\w*|кондитер\w*)\b', text))
    production = bool(re.search(r'\b(печь|пеку|печем|выпек\w*|изгот\w*|производ\w*|дела\w*)\b', text))
    resale = bool(re.search(r'\b(перепрода\w*|закуп\w*|покупн\w*|чуж\w*|готов\w*)\b', text))
    sales = bool(re.search(r'\b(прода\w*|продаж\w*|торг\w*|перепрода\w*|магазин\w*)\b', text))
    online = bool(re.search(r'\b(интернет\w*|онлайн\w*|маркетплейс\w*|ozon|wildberries)\b', text))
    shop = bool(re.search(r'\b(магазин\w*|розниц\w*)\b', text))
    if cake and production:
        found.append('cake_production')
    if cake and resale and sales and not production:
        if online:
            found.append('online_retail')
        elif shop:
            found.append('cake_retail')
    if re.search(r'\bшиномонтаж\w*|\b(?:монтаж|балансировк\w*)\s+(?:автомобильных\s+)?(?:шин|колес)\b', text):
        found.append('tyre_service')
    if re.search(r'\b(?:прода\w*|продаж\w*|торг\w*|магазин\w*)\s+(?:автомобильн\w*\s+)?(?:шин[аыу]?|покрыш\w*)\b', text):
        found.append('tyre_retail')
    if re.search(r'\bсда\w*\b', text) and re.search(r'\b(?:квартир\w*|жил[ьо]\w*|комнат\w*)\b', text):
        found.append('housing_rental')
    if (re.search(r'\b(?:разраб\w*|созда\w*|пишу|писать|программир\w*)\b', text)
            and re.search(r'\b(?:сайт\w*|приложен\w*|программ\w*|софт\w*|бот\w*)\b', text)) or re.search(r'\bпрограммист\w*\b', text):
        found.append('software')
    if re.search(r'\bконсульт\w*|\bконсалтинг\w*', text) and re.search(r'\b(?:it|ит|компьютер\w*|информационн\w*)\b', text):
        found.append('it_consulting')
    if re.search(r'\b(?:ремонт\w*|чиню|чинить)\b', text) and re.search(r'\b(?:компьютер\w*|ноутбук\w*|принтер\w*)\b', text):
        found.append('computer_repair')
    if re.search(r'\bклининг\w*\b|\bуборк\w*\s+(?:квартир\w*|дом\w*|офис\w*|помещен\w*)\b', text):
        found.append('cleaning')
    if re.search(r'\b(?:парикмахер\w*|барбершоп\w*|маникюр\w*|педикюр\w*|стриж\w*)\b|\bсалон\s+красоты\b', text):
        found.append('beauty')
    return list(dict.fromkeys(found))


def suggest_activity(text: str, *, catalog=None) -> dict:
    """Return a suggestion, a focused clarification, or an explicit limitation.

    ``primary`` is None unless status is matched. Additional codes are supplied
    only for an explicitly separate activity after «также»/«дополнительно».
    Nothing in this function chooses a tax regime or submits registration data.
    """
    catalog = validate_catalog(catalog) if catalog is not None else _DEFAULT_CATALOG
    result = {'status': 'unknown', 'primary': None, 'additional': [],
              'warnings': [], 'sources': [], 'checked_on': catalog['checked_on']}

    def finish(status, question=None, ids=(), source_ids=(), warnings=()):
        result['status'] = status
        if question:
            result['question'] = question
        selected_sources = list(source_ids)
        result['warnings'].extend(warnings)
        for key in ids:
            entry = catalog['activities'][key]
            result['warnings'].extend(entry['warnings'])
            selected_sources.extend(entry['sources'])
        result['warnings'] = list(dict.fromkeys(result['warnings']))
        selected_sources.extend(['fns_types', 'fns_search'])
        result['sources'] = [deepcopy(catalog['sources'][key]) for key in dict.fromkeys(selected_sources)]
        result['coverage'] = catalog['coverage']
        result['explanation'] = catalog['explanation']
        return result

    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        return finish('clarify', 'Опишите одним коротким сообщением, что будете производить, продавать или какие услуги оказывать.')
    text = _normal(text)
    # Do not treat negated activities or a comparison as an actual business.
    if re.search(r'\b(?:не|либо|или)\b', text):
        return finish('clarify', 'Укажите выбранную деятельность без альтернатив и исключённых вариантов: что именно будете делать?')
    # A client's industry does not become the developer's own activity.
    client_prefix = re.split(r'\bдля\b', text, maxsplit=1)[0].strip()
    if client_prefix != text and _match(client_prefix) == ['software'] and not re.search(r'\b(?:также|дополнительно)\b', text):
        text = client_prefix
    sales = re.search(r'\b(?:прода\w*|продаж\w*|торг\w*|магазин\w*)\b', text)
    if sales and re.search(r'\b(?:водк\w*|коньяк\w*|виски|крепк\w*\s+алкогол\w*)\b', text):
        restricted = catalog['restrictions']['spirits_retail']
        return finish('restricted', source_ids=restricted['sources'], warnings=restricted['warnings'])
    if re.search(r'\b(?:алкогол\w*|пив\w*|вин[оа]|сидр\w*)\b', text):
        return finish('clarify', 'Уточните напитки и формат: производство, магазин или общепит. Ограничения для ИП зависят от этого.',
                      source_ids=['alcohol'])
    if re.search(r'\b(?:инъекц\w*|ботокс\w*|медицин\w*|косметолог\w*|дезинфекц\w*)\b', text):
        return finish('clarify', 'Уточните конкретные процедуры и услуги: для регулируемой деятельности требуется отдельная проверка кода и лицензирования.',
                      source_ids=['medical_beauty'] if re.search(r'инъекц|ботокс|косметолог|медицин', text) else ['fns_search'])
    if re.search(r'\b(?:посуточ\w*|гостиниц\w*|хостел\w*|риелтор\w*|риэлтор\w*|посреднич\w*)\b', text):
        return finish('clarify', 'Вы сдаёте своё жильё внаём, оказываете гостиничные услуги или выступаете посредником? Для этих вариантов разные коды.')
    if re.search(r'восстановлен\w*\s+(?:шин|покрыш|протектор)', text):
        return finish('unknown', 'Промышленное восстановление покрышек отличается от шиномонтажа; уточните технологию для подбора в сервисе ФНС.')
    if re.search(r'\b(?:квартир\w*|жил[ьо]\w*|комнат\w*)\b', text):
        if re.search(r'\b(?:офис\w*|нежил\w*|склад\w*)\b', text) and re.search(r'\bсда\w*\b', text):
            return finish('clarify', 'Вы сдаёте жилые или нежилые помещения? Разделите эти направления и укажите основное.')
        if re.search(r'\bаренд\w*\b', text) and not re.search(r'\bсда\w*\b', text):
            return finish('clarify', 'Вы будете сдавать жильё другим людям или арендуете помещение для собственного бизнеса?')

    sections = re.split(r'\b(?:также|дополнительно)\b', text, maxsplit=1)
    main = _match(sections[0])
    extras = _match(sections[1]) if len(sections) == 2 else []
    if 'cake_production' in main and re.search(r'\b(?:перепрода\w*|чуж\w*|покупн\w*)\b', sections[0]):
        return finish('clarify', 'Вы описали изготовление своих и перепродажу чужих изделий. Что будет основным? Для торговли уточните формат магазина.')
    if len(main) > 1:
        return finish('clarify', 'В описании несколько направлений. Какое будет основным по планируемым показателям бизнеса? Остальные перечислите после слова «дополнительно».', ids=main)
    if main:
        key = main[0]
        entry = catalog['activities'][key]
        result['primary'] = {field: entry[field] for field in ('code', 'title')}
        for extra in extras:
            if extra != key:
                card = catalog['activities'][extra]
                result['additional'].append({field: card[field] for field in ('code', 'title')})
        if len(sections) == 2 and not extras:
            result['warnings'].append('Дополнительная деятельность пока не распознана; код для неё не добавлен.')
        return finish('matched', ids=[key, *extras])
    if re.search(r'\b(?:торт\w*|пирожн\w*|кондитер\w*)\b', text):
        return finish('clarify', 'Вы сами печёте торты или перепродаёте готовые чужие изделия? Для перепродажи уточните: специализированный магазин или Интернет.')
    if re.search(r'\b(?:it|ит|айти|компьютер\w*|услуг\w*|консульт\w*)\b', text):
        return finish('clarify', 'Какие именно услуги: разработка программ, IT-консультации, ремонт компьютеров, уборка или другое? Опишите работу для клиента.')
    return finish('unknown', 'В небольшом справочнике нет уверенного совпадения. Уточните продукт и действия; полный подбор доступен в сервисе ФНС «Мой ОКВЭД».')


def format_activity(result: dict) -> str:
    """Render under the MAX message limit without dropping code conditions."""
    status = result.get('status', 'unknown')
    lines = []
    primary = result.get('primary')
    if status == 'matched' and primary:
        lines.append(f"Кандидат в основной ОКВЭД: {primary['code']} — {primary['title']}.")
        if result.get('additional'):
            lines.append('Дополнительные коды для описанных вами отдельных работ:')
            lines.extend(f"• {item['code']} — {item['title']}" for item in result['additional'])
        else:
            lines.append('Дополнительные коды без описанных вами других работ не добавляю.')
        lines.append(result.get('explanation', ''))
    elif status == 'restricted':
        lines.append('Для описанной деятельности есть ограничение по форме ИП.')
    if result.get('question'):
        lines.append(result['question'])
    lines.extend(result.get('warnings', []))
    lines.append(result.get('coverage', 'Это ограниченный справочник, не полный классификатор.'))
    sources = result.get('sources', [])
    # Always retain the official interactive search even when source lists grow.
    official = next((s for s in sources if s.get('url') == 'https://service.nalog.ru/okved/'),
                    {'title': 'ФНС: Мой ОКВЭД', 'url': 'https://service.nalog.ru/okved/'})
    footer = f"\n\nПолный подбор: {official['url']}"
    if result.get('checked_on'):
        footer += f"\nСправочник проверен: {result['checked_on']}"
    body = '\n\n'.join(line for line in lines if line)
    if len(body) + len(footer) >= 3500:
        return ('Описание включает много направлений. Разделите их на основной вид и отдельные дополнительные работы; '
                'сверьте ограничения в официальном сервисе.' + footer)
    for source in sources:
        if source['url'] == official['url']:
            continue
        block = f"\n\n{source['title']}:\n{source['url']}"
        if len(body) + len(block) + len(footer) < 3500:
            body += block
    return body + footer

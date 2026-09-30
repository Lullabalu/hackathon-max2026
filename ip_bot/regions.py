"""Offline name/code lookup against a dated, separately reviewed AUSN registry.

No fuzzy matching, geocoding, LLM calls or changes to tax rates. A recognised
region and a known absence from the FNS list are different from unknown input.
The snapshot makes no promise about dates later than its verification date.
"""

from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import re
import unicodedata


_DATA = json.loads((Path(__file__).resolve().parents[1] / 'config/ausn_regions.json').read_text())
_VERIFIED = date.fromisoformat(_DATA['verified_on'])


def _normalise(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        return None
    value = unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е')
    value = re.sub(r'[.()«»,:;–—−_-]+', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip()
    value = re.sub(r'\bобл\b', 'область', value)
    value = re.sub(r'\bресп\b', 'республика', value)
    value = re.sub(r'^(?:г|город)\s+', '', value)
    code = re.fullmatch(r'(?:(?:код\s+)?регион(?:а)?\s+|код\s+|ru\s+)?(\d{1,3})(?:\s+регион)?', value)
    if code:
        digits = code.group(1)
        return digits.zfill(2) if len(digits) <= 2 else digits
    return value


_BY_NAME = {}
_BY_CODE = {}
for _entry in _DATA['regions']:
    if _entry['code'] in _BY_CODE or not re.fullmatch(r'\d{2}', _entry['code']):
        raise ValueError('Справочник АУСН содержит повторный или неверный код региона.')
    if _entry['status'] not in ('participating', 'not_listed'):
        raise ValueError('Неизвестный статус региона в справочнике АУСН.')
    if _entry['status'] == 'participating':
        if not _entry['starts_on'] or date.fromisoformat(_entry['starts_on']) > _VERIFIED:
            raise ValueError('Дата введения АУСН не подтверждена снимком справочника.')
    elif _entry['starts_on'] is not None:
        raise ValueError('У отсутствующего участника не должно быть даты введения.')
    _BY_CODE[_entry['code']] = _entry
    for _name in [_entry['code'], _entry['name'], *_entry['aliases']]:
        _key = _normalise(_name)
        if not _key or _key in _BY_NAME and _BY_NAME[_key]['code'] != _entry['code']:
            raise ValueError('Справочник АУСН содержит неоднозначный псевдоним региона.')
        _BY_NAME[_key] = _entry


def resolve_region(value):
    """Return a detached region record or None; accept names, codes and aliases."""
    key = _normalise(value)
    found = _BY_NAME.get(key) if key is not None else None
    return deepcopy(found) if found is not None else None


def ausn_region_status(value, *, on=None):
    """Return JSON-safe metadata and ``available`` True/False/None.

    ``on`` is a date or ISO date string. The default is the verified snapshot
    date, never the wall clock. Invalid/unsupported dates and unknown regions
    return unknown. Absence from the current registry establishes nonparticipation
    only at the checked snapshot date; it is not a reconstructed historical law.
    """
    entry = resolve_region(value)
    result = {
        'available': None, 'status': 'unknown', 'code': entry['code'] if entry else None,
        'name': entry['name'] if entry else None, 'starts_on': entry['starts_on'] if entry else None,
        'checked_on': None, 'verified_on': _DATA['verified_on'], 'version': _DATA['version'],
        'sources': list(_DATA['sources']), 'reason': '',
    }
    try:
        checked = _VERIFIED if on is None else on
        if isinstance(checked, str):
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', checked):
                raise ValueError
            checked = date.fromisoformat(checked)
        if not isinstance(checked, date) or isinstance(checked, datetime):
            raise ValueError
        result['checked_on'] = checked.isoformat()
    except ValueError:
        result['reason'] = 'Нужна корректная дата проверки региона в формате ГГГГ-ММ-ДД.'
        return result
    if checked.year not in _DATA['supported_years'] or checked > _VERIFIED:
        result['reason'] = f'Доступность АУСН на {checked:%d.%m.%Y} не подтверждена справочником, проверенным {_VERIFIED:%d.%m.%Y}.'
        return result
    if entry is None:
        result['reason'] = 'Регион не распознан. Укажите полное название региона регистрации ИП или его код ФНС; неизвестный регион не считается запретом на АУСН.'
        return result
    if entry['status'] == 'not_listed':
        if checked != _VERIFIED:
            result['reason'] = f"{entry['name']} отсутствует в перечне ФНС на {_VERIFIED:%d.%m.%Y}; историческая доступность на выбранную дату не подтверждена."
            return result
        result.update(available=False, status='unavailable',
                      reason=f"{entry['name']} не указана в полном перечне участников АУСН ФНС на {_VERIFIED:%d.%m.%Y}.")
        return result
    start = date.fromisoformat(entry['starts_on'])
    if checked < start:
        result.update(available=False, status='unavailable',
                      reason=f"{entry['name']}: АУСН введена с {start:%d.%m.%Y}; на {checked:%d.%m.%Y} режим ещё не действовал.")
    else:
        result.update(available=True, status='available',
                      reason=f"{entry['name']}: регион участвует в АУСН с {start:%d.%m.%Y} по перечню ФНС, проверенному {_VERIFIED:%d.%m.%Y}.")
    return result

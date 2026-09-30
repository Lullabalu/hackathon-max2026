"""Обновить локальный классификатор из открытого API ФНС: python -m scripts.update_okved."""

from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import build_opener, ProxyHandler

from ip_bot.llm import NoRedirect
from ip_bot.okved_search import API, REFERENCE


def main():
    url = API + 'reference/okveds'
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=60) as response:
        raw = response.read(16_000_001)
    if len(raw) > 16_000_000:
        raise ValueError('Ответ ФНС превышает ожидаемый размер; прежний справочник сохранён.')
    rows = json.loads(raw).get('okveds')
    if not isinstance(rows, list) or len(rows) < 2000:
        raise ValueError('Получен неполный справочник; прежний файл сохранён.')
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not re.fullmatch(r'\d{2}(?:\.\d{1,2}){0,2}', row.get('code', ''))
                or not isinstance(row.get('name'), str) or not row['name'].strip()
                or row['code'] in seen or type(row.get('selectable', False)) is not bool):
            raise ValueError('Неверная запись классификатора; прежний файл сохранён.')
        seen.add(row['code'])
    payload = {'source': url, 'checked_on': date.today().isoformat(), 'okveds': rows}
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=REFERENCE.parent, delete=False) as stream:
            name = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
        os.chmod(name, 0o644)
        name.replace(REFERENCE)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)
    print(f'Справочник обновлён: {len(rows)} записей. Перезапустите worker для загрузки новой версии.')


if __name__ == '__main__':
    main()

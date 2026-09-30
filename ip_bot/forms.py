"""Частичное заполнение официального скана Р21001, без отправки данных.

Это черновик для проверки/переноса данных, а не готовое заявление для подачи.
Схема полей, происхождение PDF и ограничения: config/forms/r21001-2024.json.
"""

from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import re

from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas


_ROOT = Path(__file__).resolve().parent.parent
_MANIFEST = _ROOT / 'config/forms/r21001-2024.json'
_FONT = 'R21001NotoSans'
_DRAFT = 'ЧЕРНОВИК — НЕ ПОДПИСАНО И НЕ ПОДАНО В ФНС'


class R21001Error(ValueError):
    """Данные не помещаются в поддерживаемые поля или имеют неверный формат."""


def _manifest():
    return json.loads(_MANIFEST.read_text(encoding='utf-8'))


def _mapping(value, label):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise R21001Error(f'{label}: ожидается объект с полями.')
    return value


def _text(value, label):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise R21001Error(f'{label}: ожидается строка.')
    return value.strip()


def _code(value):
    if isinstance(value, dict):
        value = value.get('code')
    value = _text(value, 'ОКВЭД')
    if not re.fullmatch(r'[0-9]{2}\.[0-9]{2}(?:\.[0-9]{1,2})?', value):
        raise R21001Error('ОКВЭД: нужен код вида 62.01, 10.71.2 или 01.11.11.')
    return value


def _values(state, schema):
    state = _mapping(state, 'Анкета')
    form = _mapping(state.get('form'), 'Данные формы')
    registration = _mapping(state.get('registration'), 'Регистрация')
    okved = _mapping(state.get('okved'), 'ОКВЭД')
    values = {}
    for key, label in (('surname', 'Фамилия'), ('name', 'Имя'), ('patronymic', 'Отчество')):
        value = _text(form.get(key), label).upper()
        if value and not re.fullmatch(r"[А-ЯЁ]+(?:[ '\-][А-ЯЁ]+)*", value):
            raise R21001Error(f'{label}: используйте русскую запись из документа; латинская запись не поддерживается.')
        if len(value) > schema['fields'][key]['cells']:
            raise R21001Error(f'{label}: больше 34 клеток. Заполните заявление в сервисе ФНС.')
        values[key] = value
    values['inn'] = _text(form.get('inn'), 'ИНН')
    if values['inn'] and not re.fullmatch(r'[0-9]{12}', values['inn']):
        raise R21001Error('ИНН физического лица должен содержать 12 цифр; при отсутствии оставьте поле пустым.')
    values['email'] = _text(form.get('email'), 'Email')
    if values['email']:
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}", values['email']):
            raise R21001Error('Email: поддерживается адрес латиницей без пробелов.')
        if len(values['email']) > schema['fields']['email']['cells']:
            raise R21001Error('Email длиннее 38 клеток бланка. Заполните заявление в сервисе ФНС.')
    citizenship = registration.get('citizenship')
    if citizenship not in (None, '', 'ru', 'foreign', 'stateless'):
        raise R21001Error('Для этого гражданства автоматическое предзаполнение не поддерживается.')
    values['citizenship'] = {'ru': '1', 'foreign': '2', 'stateless': '3'}.get(citizenship, '')
    values['primary'] = _code(okved['primary']) if okved.get('primary') else ''
    additional = okved.get('additional') or []
    if not isinstance(additional, list):
        raise R21001Error('Дополнительные ОКВЭД: ожидается список кодов.')
    values['additional'] = []
    for item in additional:
        code = _code(item)
        if code != values['primary'] and code not in values['additional']:
            values['additional'].append(code)
    if values['additional'] and not values['primary']:
        raise R21001Error('Сначала выберите основной ОКВЭД; бот не назначает его автоматически.')
    if len(values['additional']) > schema['fields']['additional']['capacity']:
        raise R21001Error('Больше 68 дополнительных кодов: требуется ещё один лист А в сервисе ФНС.')
    return values


def r21001_support(state):
    """Описание поддерживаемого черновика; позволяет UI показать причину отказа."""
    schema = _manifest()
    result = {'supported': False, 'mode': 'partial_draft',
              'unsupported': deepcopy(schema['unsupported']), 'warnings': [
                  'Паспорт, адрес, дата и место рождения, телефон и подпись не заполнены.',
                  'Проверьте данные и завершите заявление в официальном сервисе ФНС.']}
    try:
        values = _values(state, schema)
    except R21001Error as exc:
        result['reason'] = str(exc)
        return result
    result['supported'] = True
    result['filled_fields'] = [key for key, value in values.items() if value]
    registration = (state or {}).get('registration') or {}
    if registration.get('citizenship') == 'foreign':
        result['warnings'].append('Для иностранного гражданина не заполнены латинская запись ФИО, код страны и сведения о праве проживания.')
    elif registration.get('citizenship') == 'stateless':
        result['warnings'].append('Для лица без гражданства не заполнены латинская запись ФИО и сведения о праве проживания.')
    age = registration.get('age')
    if isinstance(age, int) and not isinstance(age, bool) and age < 18:
        result['warnings'].append('Для несовершеннолетнего отдельно проверьте право регистрации и дополнительные документы; возраст не заменяет дату рождения.')
    return result


def _draw_cells(canvas, value, field, page_size, image_size):
    """В каждой клетке — один символ; точки ОКВЭД уже напечатаны в оригинале."""
    x_scale, y_scale = page_size[0] / image_size[0], page_size[1] / image_size[1]
    x, y = field['center']
    dx, dy = field['advance']
    offsets = field.get('digit_offsets', range(len(value)))
    font_size = 12
    ascent = pdfmetrics.getAscent(_FONT, font_size)
    descent = pdfmetrics.getDescent(_FONT, font_size)
    canvas.setFont(_FONT, font_size)
    for char, offset in zip(value, offsets):
        center_x = (x + offset * dx) * x_scale
        center_y = page_size[1] - (y + offset * dy) * y_scale
        canvas.drawCentredString(center_x, center_y - (ascent + descent) / 2, char)


def fill_r21001(state) -> bytes:
    """Вернуть пять страниц официального бланка с частичными данными и отметками черновика.

    Пропущенные данные остаются пустыми. Неподдерживаемые значения вызывают
    R21001Error и никогда не обрезаются. Функция не использует сеть и дисковые
    временные файлы с данными пользователя.
    """
    schema = _manifest()
    values = _values(state, schema)
    template = (_MANIFEST.parent / schema['template']).read_bytes()
    if hashlib.sha256(template).hexdigest() != schema['template_sha256']:
        raise R21001Error('Шаблон Р21001 изменён: нужно повторно проверить расположение полей.')
    reader = PdfReader(io.BytesIO(template))
    if len(reader.pages) != len(schema['pages']):
        raise R21001Error('Число страниц шаблона не соответствует проверенной схеме.')
    if _FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT, str(_ROOT / 'config/fonts/NotoSans-Regular.ttf')))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        size = (float(page.mediabox.width), float(page.mediabox.height))
        if list(size) != schema['pages'][index]['size_points']:
            raise R21001Error('Размер страницы шаблона не соответствует проверенной схеме.')
        image_size = schema['pages'][index]['image_pixels']
        overlay = io.BytesIO()
        canvas = Canvas(overlay, pagesize=size, pageCompression=1)
        for name, value in values.items():
            field = schema['fields'][name]
            if not value or field['page'] != index:
                continue
            if name == 'additional':
                for number, code in enumerate(value):
                    row, column = divmod(number, 4)
                    cell = {**field, 'center': [field['column_centers'][column] + row * field['row_x_shift'],
                                              field['row_centers'][row] + field['column_y_shift'][column]]}
                    _draw_cells(canvas, code.replace('.', ''), cell, size, image_size)
            else:
                _draw_cells(canvas, value.replace('.', '') if name == 'primary' else value, field, size, image_size)
        if index in (3, 4):
            field = schema['fields']['page_a' if index == 3 else 'page_b']
            _draw_cells(canvas, f'{index + 1:03}', field, size, image_size)
        canvas.setFont(_FONT, 8)
        canvas.drawCentredString(size[0] / 2, size[1] - 20, _DRAFT)
        canvas.setFont(_FONT, 7)
        canvas.drawCentredString(size[0] / 2, size[1] - 31, 'Заполнены только отдельные поля. Дополните и проверьте сведения в сервисе ФНС.')
        canvas.drawCentredString(size[0] / 2, 17, 'Паспорт, адрес, дата рождения, телефон и подпись не заполнены. Не готово для подачи.')
        canvas.save()
        filled_page = writer.add_page(page)
        filled_page.merge_page(PdfReader(overlay).pages[0])
    writer.add_metadata({'/Title': 'Р21001 — частично заполненный черновик',
                         '/Subject': _DRAFT + '. Бланк: ' + schema['order'],
                         '/Author': 'MAX IP bot', '/R21001Revision': schema['revision'],
                         '/R21001Source': schema['source_pdf']})
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()

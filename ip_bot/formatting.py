"""Plain-text layout for MAX. Numbers, links and list labels stay unchanged."""

import re


_NUMBERED = re.compile(r'(?<!\S)(?P<number>[1-9]\d?)(?P<marker>[.)]|[ \t]+[—–-])(?=[ \t]+\S)')
_BULLET = re.compile(r'(?<!\S)[•●▪](?=[ \t]+\S)')


def _separate(text, positions):
    for position in reversed(positions):
        before = text[:position].rstrip(' \t')
        text = before + ('\n' if before and not before.endswith('\n') else '') + text[position:]
    return text


def _paragraph(text):
    items = list(_NUMBERED.finditer(text))
    if len(items) >= 2:
        prefix = text[:items[0].start()]
        starts_list = not prefix.rsplit('\n', 1)[-1].strip() or prefix.rstrip().endswith(':')
        numbers = [int(item['number']) for item in items]
        markers = {item['marker'].strip() for item in items}
        # A sequence beginning at 1 and a list boundary avoid interpreting
        # dates, decimal amounts, OKVED codes and legal references as steps.
        if starts_list and len(markers) == 1 and numbers == list(range(1, len(items) + 1)):
            text = _separate(text, [item.start() for item in items])
    bullets = list(_BULLET.finditer(text))
    if len(bullets) >= 2:
        text = _separate(text, [item.start() for item in bullets])
    return text


def format_message(text: str) -> str:
    """Keep paragraphs and split clearly marked inline lists onto real lines.

    No Markdown/HTML interpretation, sentence rewriting or model call. Running
    this again for a saved reply or a delivery retry produces the same text.
    """
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = '\n'.join(line.rstrip() for line in text.split('\n')).strip()
    return '\n\n'.join(_paragraph(part) for part in re.split(r'\n{2,}', text))

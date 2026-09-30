"""Создаёт локальный .env без перезаписи существующего и вывода секретов."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent.parent
text = (root / '.env.example').read_text()
text = text.replace('REPLACE_WITH_RANDOM_PASSWORD', secrets.token_urlsafe(32))
text = text.replace('REPLACE_WITH_RANDOM_SECRET_AT_LEAST_32_CHARACTERS', secrets.token_urlsafe(48))
text = text.replace('REPLACE_WITH_RANDOM_ADMIN_TOKEN', secrets.token_urlsafe(48))
try:
    fd = os.open(root / '.env', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    raise SystemExit('.env уже существует: файл сохранён без изменений.')
with os.fdopen(fd, 'w') as file:
    file.write(text)
print('Создан .env. Укажите MAX_BOT_TOKEN; для webhook также BOT_DOMAIN и MAX_WEBHOOK_URL.')

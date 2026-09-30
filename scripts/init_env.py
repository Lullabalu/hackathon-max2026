"""Создаёт локальный .env без перезаписи существующего и вывода секретов."""
import os
from pathlib import Path
import secrets

root = Path(__file__).resolve().parent.parent
template = (root / '.env.example').read_text()
text = template
text = text.replace('REPLACE_WITH_RANDOM_PASSWORD', secrets.token_urlsafe(32))
text = text.replace('REPLACE_WITH_RANDOM_SECRET_AT_LEAST_32_CHARACTERS', secrets.token_urlsafe(48))
text = text.replace('REPLACE_WITH_RANDOM_ADMIN_TOKEN', secrets.token_urlsafe(48))
try:
    fd = os.open(root / '.env', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    if (root / '.env').read_text() != template:
        print('.env уже существует: файл сохранён без изменений.')
        raise SystemExit(0)
    # Репозиторий включает нетронутый шаблон .env.
    fd = os.open(root / '.env', os.O_WRONLY | os.O_TRUNC, 0o600)
os.chmod(root / '.env', 0o600)
with os.fdopen(fd, 'w') as file:
    file.write(text)
print('Создан .env. Укажите MAX_BOT_TOKEN; для webhook также BOT_DOMAIN и MAX_WEBHOOK_URL.')

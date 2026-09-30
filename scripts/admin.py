"""Run the local admin UI: python -m scripts.admin --port 8081."""

import argparse
import os
from pathlib import Path
import shlex

import uvicorn

from ip_bot.admin import create_admin_app
from ip_bot.settings import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('Порт должен быть от 1 до 65535.')
    env_file = Path(__file__).resolve().parent.parent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            key, separator, value = line.strip().partition('=')
            if separator and key.replace('_', '').isalnum():
                os.environ.setdefault(key, ' '.join(shlex.split(value, comments=True)))
    settings = Settings.from_env()
    if not settings.database_url:
        parser.error('Нужен DATABASE_URL или настройки PGHOST/PGDATABASE/PGUSER.')
    app = create_admin_app(settings)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()

# Разработка и проверки

Нужны Python 3.13 и зависимости из `requirements-dev.txt`. Для интеграционных проверок — отдельные PostgreSQL-базы; для проверки поиска — Ollama со скачанной моделью и доступ к источникам.

```bash
python3.13 -m venv .venv
# Активируйте окружение способом, принятым в вашей оболочке
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests
```

Переменные окружения экспортируйте из `.env` средствами своей оболочки. Команды приложения сами не читают `.env`; Docker Compose и `scripts.run_local` загружают его автоматически. Не используйте рабочую БД для тестов.

| Команда | Что проверяет |
| --- | --- |
| `python -m unittest discover -s tests` | Логика, маршрутизация, поиск, документы, настройки и хранение |
| `python -m scripts.check_spec` | Пользовательские сценарии и PDF в отдельной схеме `DEMO_DATABASE_URL` |
| `python -m scripts.benchmark` | Реальные источники, модель, кэш, анкета и качество ответов |
| `python -m scripts.benchmark --skip-model` | Только разговор и ответы по анкете |
| `python -m scripts.evaluate_planner --help` | Параметры независимой проверки разбора вопросов |

Тесты хранилища используют `TEST_DATABASE_URL`; без него пропускаются. Сценарии и benchmark создают временную схему в демонстрационной базе и не отправляют сообщения в MAX. Отчёты сохраняются в `data/` и не входят в публичный комплект.

## Запуск без контейнеров

Установите PostgreSQL 17 и Ollama, создайте базы и задайте URL в окружении. Запустите `ollama serve`, затем скачайте модель `ollama pull qwen3.5:9b`.

В отдельных терминалах с одинаковыми переменными окружения:

```bash
python -m ip_bot --mode polling
python -m ip_bot --mode worker
```

`python -m ip_bot --demo` запускает консольный диалог на `DEMO_DATABASE_URL` без MAX. Вспомогательный `python -m scripts.run_local` запускает комплект для разработки на POSIX-системах с PostgreSQL и Ollama в PATH; он использует `.env`, БД на `127.0.0.1:55432` и Ollama на `127.0.0.1:11434`.

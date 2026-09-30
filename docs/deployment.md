# Развёртывание

## Настройки

Docker Compose читает `.env` рядом с `compose.yaml`. Для нового экземпляра выполните `python3 scripts/init_env.py` и укажите токен MAX. Если команда передала заполненный `.env` отдельно, замените им шаблон репозитория. Если Python недоступен, скопируйте `.env.example` в `.env` и самостоятельно замените значения `REPLACE_WITH_...` случайными секретами.

| Переменная | Назначение / значение по умолчанию |
| --- | --- |
| `MAX_BOT_TOKEN` | Токен вашего бота MAX, обязателен |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL, обязателен |
| `OLLAMA_MODEL` | `qwen3.5:9b` |
| `AI_DEVICE` | `cpu`; `auto` — автоматический выбор устройства |
| `AI_THREADS` | `4`; подберите по числу доступных CPU |
| `AI_CONTEXT_TOKENS` | `16384` |
| `AI_TIMEOUT_SECONDS` | `0` — без лимита генерации |
| `AI_THINK` | `false`; внутреннее рассуждение включается отдельно |
| `AI_KEEP_ALIVE` | `30m` — хранение модели в памяти |
| `SEARCH_MAX_PAGES` | `12` |
| `SOURCE_TTL_HOURS` | `24` — срок свежести кэша |
| `ADMIN_TOKEN` | Отдельный секрет админки |
| `CONSULTANT_URL` | Необязательная HTTPS-ссылка на консультанта |
| `BOT_DOMAIN`, `MAX_WEBHOOK_URL`, `MAX_WEBHOOK_SECRET` | Настройки webhook |

Адреса PostgreSQL и Ollama внутри контейнеров задаёт Compose. `DATABASE_URL`, `DEMO_DATABASE_URL`, `TEST_DATABASE_URL` и `OLLAMA_BASE_URL` в шаблоне нужны для запуска Python вне контейнера. Для нового тома БД пароль берётся из `.env`; изменение переменной не меняет пароль уже существующей базы.

## Polling

Не требует домена и входящих портов. У бота не должно быть зарегистрированного webhook.

```bash
docker compose --profile polling config --quiet
docker compose --profile polling up -d --build
docker compose --profile polling ps -a
docker compose logs -f worker polling
```

При первом старте скачивается модель. Дождитесь успешного завершения `model-init`. PostgreSQL и Ollama не публикуют порты наружу.

## Webhook

Для сервера нужны домен, направленный на его IP, и открытые входящие TCP 80/443. В `.env` задайте `BOT_DOMAIN=bot.example.com`, `MAX_WEBHOOK_URL=https://bot.example.com/webhook` и случайный `MAX_WEBHOOK_SECRET`. Caddy получает HTTPS-сертификат автоматически.

```bash
# Остановить polling, если он был запущен
docker compose --profile polling stop polling
# Запустить сервер
docker compose --profile production up -d --build
docker compose --profile production ps -a
# После готовности HTTPS зарегистрировать адрес в MAX
docker compose --profile production run --rm --no-deps webhook python -m ip_bot --mode subscribe
```

Не запускайте polling и webhook одновременно для одного токена. Команда регистрации не удаляет подписку на другой адрес: перенос существующего webhook выполняйте отдельно в MAX.

## Админка

```bash
docker compose --profile admin up -d admin
```

Интерфейс доступен на `http://127.0.0.1:8081`, защищён `ADMIN_TOKEN`. Для удалённого сервера используйте SSH-туннель. Можно создавать версии FAQ, новостей, налоговых правил и типовых ОКВЭД, проверять, публиковать и откатывать их.

## Обновление и резервная копия

Перед заменой кода сохраните базу:

```bash
mkdir -p backups
docker compose exec -T db pg_dump -U max_bot -d max_bot -Fc > backups/max_bot.dump
# После обновления исходников
docker compose --profile polling up -d --build
```

Для webhook используйте профиль `production`. Храните копии вне сервера и проверяйте восстановление в отдельной базе. `docker compose --profile polling down` сохраняет тома; добавление `--volumes` удаляет их.

## Если запуск не удался

- `model-init` завершился с ошибкой: проверьте интернет, свободное место и `OLLAMA_MODEL`, затем повторите запуск.
- Бот молчит: проверьте токен, статус worker/polling, отсутствие другого экземпляра и зарегистрированного webhook.
- Модель отвечает долго: проверьте доступную память и CPU, дождитесь окончания первой загрузки. Общего ограничения времени нет.
- PostgreSQL отклоняет пароль: проверьте настройки существующего тома; создание нового `.env` не меняет пароль в базе.

Логи: `docker compose logs --tail=100 worker polling model-init`. Рабочие секреты не публикуйте вместе с диагностикой.

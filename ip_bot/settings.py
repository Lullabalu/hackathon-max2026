"""Настройки без секретов в коде; .env загружает оболочка или Compose."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from psycopg.conninfo import make_conninfo

from .budget import MODEL_SECONDS


ROOT = Path(__file__).resolve().parent.parent


def integer(name, default, minimum, maximum):
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name}: допустимо {minimum}–{maximum}.")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    token: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:4b-q4_K_M"
    model_timeout: int = MODEL_SECONDS
    context_tokens: int = 16384
    model_threads: int = 4
    model_device: str = "cpu"
    model_keep_alive: str = "30m"
    sources_file: Path = ROOT / "config/sources.json"
    max_pages: int = 12
    source_ttl_hours: int = 24
    webhook_secret: str = ""
    webhook_url: str = ""
    consultant_url: str = ""

    def __post_init__(self):
        if self.consultant_url:
            url = urlsplit(self.consultant_url)
            if url.scheme != 'https' or not url.hostname or url.username or len(self.consultant_url) > 1000:
                raise ValueError('CONSULTANT_URL: нужна HTTPS-ссылка на контакт.')
        if self.model_device not in ("cpu", "auto"):
            raise ValueError("AI_DEVICE: допустимо cpu или auto.")
        if not isinstance(self.model_keep_alive, str) or not re.fullmatch(r"[1-9][0-9]{0,2}[smh]", self.model_keep_alive):
            raise ValueError("AI_KEEP_ALIVE: положительная длительность, например 30m или 1h.")

    @classmethod
    def from_env(cls, *, demo=False, database_url=None):
        dsn = database_url or os.getenv("DEMO_DATABASE_URL" if demo else "DATABASE_URL", "").strip()
        # Compose передаёт пароль отдельным полем: спецсимволы не ломают URL.
        if not demo and not dsn and os.getenv("PGHOST"):
            dsn = make_conninfo(
                host=os.environ["PGHOST"], port=os.getenv("PGPORT", "5432"),
                dbname=os.getenv("PGDATABASE", "max_bot"), user=os.getenv("PGUSER", "max_bot"),
                password=os.getenv("PGPASSWORD", ""),
            )
        return cls(
            database_url=dsn, token=os.getenv("MAX_BOT_TOKEN", "").strip(),
            ollama_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
            model=os.getenv("OLLAMA_MODEL", "qwen3.5:4b-q4_K_M"),
            model_timeout=integer("AI_TIMEOUT_SECONDS", MODEL_SECONDS, 0, 86400),
            context_tokens=integer("AI_CONTEXT_TOKENS", 16384, 4096, 32768),
            model_threads=integer("AI_THREADS", 4, 1, 64),
            model_device=os.getenv("AI_DEVICE", "cpu").strip().lower(),
            model_keep_alive=os.getenv("AI_KEEP_ALIVE", "30m").strip(),
            sources_file=Path(os.getenv("SOURCES_FILE", str(ROOT / "config/sources.json"))),
            max_pages=integer("SEARCH_MAX_PAGES", 12, 1, 12),
            source_ttl_hours=integer("SOURCE_TTL_HOURS", 24, 1, 168),
            webhook_secret=os.getenv("MAX_WEBHOOK_SECRET", ""),
            webhook_url=os.getenv("MAX_WEBHOOK_URL", ""),
            consultant_url=os.getenv("CONSULTANT_URL", "").strip(),
        )

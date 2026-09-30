"""Локальный Ollama: структурированный результат и проверка схемы."""

import json
import re
from typing import Annotated, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .budget import BudgetExpired, check_budget, request_timeout, time_limit


class ModelError(RuntimeError):
    pass


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(max_length=500)
    topics: list[str] = Field(max_length=2)
    keywords: list[str] = Field(max_length=8)
    clarification: str = Field(max_length=400)
    relevant: bool


class QuestionIntent(BaseModel):
    """Короткий ответ модели: поля поиска и флаги код добавляет сам."""
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal['search', 'clarify', 'offtopic']
    text: str = Field(max_length=400)
    topics: list[Literal['registration', 'taxes', 'self_employed', 'changes', 'closing']] = Field(max_length=2)

    @model_validator(mode='after')
    def meaningful_result(self):
        if self.action == 'search' and not self.text.strip():
            raise ValueError('Нужен поисковый вопрос.')
        if self.action == 'search' and not self.topics:
            raise ValueError('Для поиска нужна тема.')
        return self

    def to_plan(self, *, context=None, current_year=None):
        # Поисковый запрос не должен получать придуманный номер формы,
        # возраст или сумму. При сомнении просим уточнение без обращения к сайтам.
        if self.action == 'search' and context is not None:
            supplied = json.dumps({key: context.get(key, {}) for key in ('question', 'profile', 'history')}, ensure_ascii=False)
            number_pattern = r'\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?'
            def canonical(number):
                # Decimal strings from PostgreSQL often end with .00. Keep
                # leading zeroes (document/date identifiers) significant.
                number = re.sub(r'[ \u00a0\u202f]', '', number)
                whole, separator, fraction = number.replace(',', '.').partition('.')
                fraction = fraction.rstrip('0')
                return whole + ('.' + fraction if separator and fraction else '')
            allowed = {canonical(number) for number in re.findall(number_pattern, supplied)}
            if current_year is not None:
                allowed.add(str(current_year))
                question = context.get('question', '').casefold()
                for prefix, offset in [('следующ', 1), ('прошл', -1), ('предыдущ', -1)]:
                    if re.search(rf'{prefix}\w*\s+год', question):
                        allowed.add(str(current_year + offset))
            if {canonical(number) for number in re.findall(number_pattern, self.text)} - allowed:
                return Plan(query='', topics=[], keywords=[], relevant=True,
                            clarification='Уточните документ, действие и период, о которых идёт речь.')
        return Plan(query=self.text if self.action == 'search' else '',
                    topics=self.topics, keywords=[],
                    clarification=(self.text.strip() or 'Уточните, какой документ или действие вы имеете в виду.')
                    if self.action == 'clarify' else '',
                    relevant=self.action != 'offtopic')


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    # Внутренний набор ID для рендера: текст берётся напрямую из источника.
    items: list[Annotated[int, Field(ge=1, le=4)]] = Field(max_length=3)
    clarification: str = Field(max_length=400)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Ollama:
    def __init__(self, settings):
        self.settings = settings
        parsed = urlsplit(settings.ollama_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Некорректный OLLAMA_BASE_URL.")
        if parsed.hostname == "ollama.com" or settings.model.endswith("-cloud"):
            raise ValueError("Для этого проекта нужна локальная модель Ollama.")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _options(self):
        options = {"num_ctx": self.settings.context_tokens,
                   "num_thread": self.settings.model_threads,
                   "temperature": 1.0 if self.settings.model_think else 0.7,
                   "top_p": 0.95 if self.settings.model_think else 0.8,
                   "top_k": 20, "min_p": 0.0,
                   "presence_penalty": 1.5, "repeat_penalty": 1.0}
        # CPU mode is explicit; auto lets Ollama select the available device.
        if self.settings.model_device == "cpu":
            options["num_gpu"] = 0
        return options

    def warmup(self):
        """Загрузить модель пустым запросом без генерации пользовательского ответа."""
        body = {
            "model": self.settings.model, "messages": [], "stream": False, "think": False,
            "keep_alive": self.settings.model_keep_alive, "options": self._options(),
        }
        req = Request(self.settings.ollama_url + "/api/chat", data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with time_limit(self.settings.model_timeout or None):
                with self.opener.open(req, timeout=request_timeout(self.settings.model_timeout or None)) as response:
                    raw = response.read(16_001)
                if len(raw) > 16_000:
                    raise ModelError("model_warmup_response_too_large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ModelError("model_warmup_invalid_response")
                if result.get("error") or result.get("done") is not True:
                    raise ModelError("model_warmup_incomplete_response")
        except BudgetExpired:
            check_budget()
            raise ModelError("model_warmup_timeout") from None
        except TimeoutError:
            raise ModelError("model_warmup_timeout") from None
        except (HTTPError, URLError, TimeoutError, OSError):
            raise ModelError("model_warmup_unavailable") from None
        except (ValueError, TypeError):
            raise ModelError("model_warmup_invalid_response") from None

    def generate(self, schema, system, content, *, max_tokens=650):
        try:
            with time_limit(self.settings.model_timeout or None):
                messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
                body = {
                    "model": self.settings.model, "stream": False, "think": self.settings.model_think,
                    "format": schema.model_json_schema(), "keep_alive": self.settings.model_keep_alive,
                    "messages": messages,
                    "options": dict(self._options(), num_predict=max(max_tokens, 8192) if self.settings.model_think else max_tokens),
                }
                req = Request(self.settings.ollama_url + "/api/chat", data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"}, method="POST")
                with self.opener.open(req, timeout=request_timeout(self.settings.model_timeout or None)) as response:
                    raw = response.read(128_001)
                if len(raw) > 128_000:
                    raise ModelError("model_response_too_large")
                result = json.loads(raw)
                if not isinstance(result, dict) or not isinstance(result.get("message"), dict):
                    raise ModelError("model_invalid_response")
                if result.get("error") or not result.get("done") or result.get("done_reason") == "length":
                    raise ModelError("model_incomplete_response")
                return schema.model_validate_json(result["message"]["content"])
        except BudgetExpired:
            check_budget()
            raise ModelError("model_timeout") from None
        except TimeoutError:
            raise ModelError("model_timeout") from None
        except (HTTPError, URLError, TimeoutError, OSError):
            raise ModelError("model_unavailable") from None
        except (ValueError, KeyError, TypeError, ValidationError):
            raise ModelError("model_invalid_response") from None

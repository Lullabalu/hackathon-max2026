"""Минимальный клиент API MAX на стандартной библиотеке Python."""

import hashlib
import base64
import json
import os
from pathlib import Path
import ssl
import time
import re
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from .budget import BudgetExpired, DELIVERY_SECONDS, check_budget, request_timeout, time_limit
from .formatting import format_message

BASE_URL = "https://platform-api2.max.ru"
# Публичный корневой сертификат с https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt.
# Дополнительное доверие действует только в клиенте MAX, не в системе/загрузчике сайтов.
DEFAULT_CA_CERT_FILE = Path(__file__).resolve().parent.parent / "config/certs/russian_trusted_root_ca.crt"


def keyboard_for(text):
    # Подписи отправляются как обычные сообщения; доступны также номер и текст.
    if 'Выберите вариант:\n' in text:
        block = text.rsplit('Выберите вариант:\n', 1)[1].split('\n\n', 1)[0]
        labels = [line for line in block.splitlines() if re.fullmatch(r'\d — .{1,100}', line)]
        if labels:
            buttons = [{'type': 'message', 'text': label} for label in labels[:10]]
            return [{'type': 'inline_keyboard', 'payload': {'buttons': [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}}]
    commands = ['/consent', '/decline', '/intro', '/begin', '/taxes', '/okved', '/register',
                '/pdf', '/checklist', '/steps', '/next', '/prev', '/form', '/menu', '/help', '/cancel']
    selected = [command for command in commands if command + ' —' in text][:6]
    if not selected:
        return []
    buttons = [{'type': 'message', 'text': command} for command in selected]
    return [{'type': 'inline_keyboard', 'payload': {'buttons': [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}}]


class APIError(Exception):
    def __init__(self, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Не отправляем токен авторизации на другой адрес при перенаправлении.
        return None


def max_ssl_context():
    try:
        context = ssl.create_default_context()
        context.load_verify_locations(cafile=os.getenv("MAX_CA_CERT_FILE") or str(DEFAULT_CA_CERT_FILE))
        return context
    except (OSError, ssl.SSLError):
        raise APIError("Не удалось загрузить TLS-сертификаты MAX. Проверьте MAX_CA_CERT_FILE.") from None


class MaxAPI:
    def __init__(self, token: str):
        self.token = token
        self.opener = build_opener(
            NoRedirect(),
            HTTPSHandler(context=max_ssl_context()),
        )
        self.last_request = 0.0
        self.last_message: dict[int, float] = {}
        self.uploaded_files = {}

    def request(self, method: str, path: str, params=None, body=None):
        try:
            # Long polling keeps its transport allowance, but a trickling body
            # cannot restart the socket timeout forever. Delivery nests inside
            # its stricter five-second deadline.
            with time_limit(45):
                return self._request(method, path, params=params, body=body)
        except BudgetExpired:
            raise APIError('MAX: превышено время запроса.', retryable=True) from None

    def _request(self, method: str, path: str, params=None, body=None):
        # Не более 20 запросов в секунду от этого процесса.
        check_budget()
        time.sleep(max(0, 0.05 - (time.monotonic() - self.last_request)))
        check_budget()
        url = BASE_URL + path
        if params:
            url += "?" + urlencode(params)
        request = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
            headers={"Authorization": self.token, "Content-Type": "application/json"},
            method=method,
        )
        self.last_request = time.monotonic()
        try:
            with self.opener.open(request, timeout=request_timeout(45)) as response:
                result = json.load(response)
            check_budget()
            if not isinstance(result, dict):
                raise APIError("MAX вернул неожиданный формат ответа.")
            return result
        except HTTPError as error:
            # Не печатаем тело ответа, заголовки и токен в лог.
            not_ready = False
            if error.code == 400:
                try:
                    payload = json.loads(error.read(16384))
                    not_ready = isinstance(payload, dict) and payload.get('code') == 'attachment.not.ready'
                except (ValueError, OSError):
                    pass
            raise APIError(
                f"MAX: HTTP {error.code} при {method} {path}.",
                retryable=not_ready or error.code in {408, 429} or error.code >= 500,
                status=error.code,
            ) from None
        except URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise APIError("Не удалось проверить TLS-сертификат MAX. Проверьте MAX_CA_CERT_FILE и дату на компьютере.") from None
            raise APIError("Не удалось связаться с MAX. Проверьте сеть и доверенные TLS-сертификаты.", retryable=True) from None
        except ssl.SSLCertVerificationError:
            raise APIError("Не удалось проверить TLS-сертификат MAX. Проверьте MAX_CA_CERT_FILE и дату на компьютере.") from None
        except (TimeoutError, OSError):
            raise APIError("Не удалось связаться с MAX. Проверьте сеть и доверенные TLS-сертификаты.", retryable=True) from None
        except (ValueError, UnicodeError):
            raise APIError("Не удалось прочитать JSON от MAX.", retryable=True) from None

    def updates(self, marker):
        params = {"timeout": 30, "limit": 100, "types": "bot_started,message_created"}
        if marker is not None:
            params["marker"] = marker
        return self.request("GET", "/updates", params=params)

    def send_message(self, user_id: int, text: str, *, attachments=None):
        try:
            # Общий бюджет включает ожидание rate limit, соединение и чтение.
            with time_limit(DELIVERY_SECONDS):
                text = format_message(text)
                if len(text) > 4000:
                    raise ValueError("Ответ превышает лимит MAX в 4000 символов.")
                # MAX допускает не более 2 сообщений в секунду в один диалог.
                time.sleep(max(0, 0.55 - (time.monotonic() - self.last_message.get(user_id, 0))))
                check_budget()
                self.last_message[user_id] = time.monotonic()
                attachments = list(attachments or []) + keyboard_for(text)
                return self.request(
                    "POST", "/messages",
                    params={"user_id": user_id, "disable_link_preview": "true"},
                    body={"text": text, **({'attachments': attachments} if attachments else {})},
                )
        except BudgetExpired:
            raise APIError("MAX: превышено время доставки сообщения.", retryable=True) from None

    def upload_file(self, file):
        from .sources import URLPolicy, PinnedHTTPS, SourceError
        if not isinstance(file, dict) or not re.fullmatch(r'[a-z0-9_-]{1,60}\.pdf', file.get('name', '')):
            raise ValueError('Некорректное имя PDF.')
        encoded = file.get('content', '')
        if not isinstance(encoded, str) or len(encoded) > 8_000_000:
            raise ValueError('PDF слишком большой.')
        data = base64.b64decode(encoded, validate=True)
        if not data.startswith(b'%PDF-') or len(data) > 6_000_000:
            raise ValueError('Некорректный PDF.')
        upload = self.request('POST', '/uploads', params={'type': 'file'})
        policy = URLPolicy(['oneme.ru', 'okcdn.ru', 'max.ru'])
        connection = None
        try:
            url = urlsplit(policy.normalize(upload.get('url')))
            addresses = policy.addresses(url.hostname)
            boundary = 'MaxBot' + secrets.token_hex(16)
            body = (f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="{file["name"]}"\r\n'
                    'Content-Type: application/pdf\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
            connection = PinnedHTTPS(url.hostname, addresses, max_ssl_context())
            # Подписанный URL уже авторизует загрузку; токен бота сюда не передаём.
            connection.request('POST', url.path + ('?' + url.query if url.query else ''), body=body,
                               headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
            response = connection.getresponse()
            if response.status != 200:
                raise APIError('MAX: не удалось загрузить PDF.', status=response.status,
                               retryable=response.status in {408, 429} or response.status >= 500)
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError
            result = json.loads(raw)
            token = result.get('token') if isinstance(result, dict) else None
            if not isinstance(token, str) or not 1 <= len(token) <= 12000:
                raise ValueError
            check_budget()
            return {'type': 'file', 'payload': {'token': token}}
        except SourceError:
            raise APIError('MAX вернул недопустимый адрес загрузки файла.') from None
        except (OSError, TimeoutError):
            raise APIError('MAX: загрузка PDF недоступна.', retryable=True) from None
        except (ValueError, UnicodeError):
            raise APIError('MAX: некорректный ответ загрузки PDF.', retryable=True) from None
        finally:
            if connection is not None:
                connection.close()

    def send_files(self, user_id, text, files):
        try:
            with time_limit(DELIVERY_SECONDS):
                if not isinstance(files, list) or len(files) != 1:
                    raise ValueError('Некорректный список PDF.')
                file = files[0]
                key = (user_id, hashlib.sha256(file['content'].encode()).hexdigest())
                if key not in self.uploaded_files:
                    if len(self.uploaded_files) >= 16:
                        self.uploaded_files.pop(next(iter(self.uploaded_files)))
                    self.uploaded_files[key] = self.upload_file(file)
                # MAX может обрабатывать файл несколько секунд. При повторе
                # используем тот же токен, чтобы не начинать обработку заново.
                result = self.send_message(user_id, text, attachments=[self.uploaded_files[key]])
                self.uploaded_files.pop(key, None)
                return result
        except BudgetExpired:
            raise APIError('MAX: превышено время доставки PDF.', retryable=True) from None


def normalize_update(update: dict) -> dict | None:
    if not isinstance(update, dict):
        return None
    kind = update.get("update_type")
    if kind == "bot_started":
        user = update.get("user")
        if not isinstance(user, dict):
            return None
        text = "/start"
        identity = json.dumps(update, sort_keys=True, ensure_ascii=False)
        event_id = "started:" + hashlib.sha256(identity.encode()).hexdigest()
    elif kind == "message_created":
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        recipient = message.get("recipient")
        if not isinstance(recipient, dict):
            return None
        if recipient.get("chat_type") != "dialog":
            return None
        user = message.get("sender")
        body = message.get("body")
        if not isinstance(user, dict) or not isinstance(body, dict):
            return None
        if not isinstance(body.get("mid"), str) or not body["mid"] or len(body["mid"]) > 500:
            return None
        text = body.get("text") or ""
        if not isinstance(text, str):
            return None
        text = text[:2001]
        event_id = "message:" + str(body["mid"])
    else:
        return None
    user_id = user.get("user_id")
    if type(user_id) is not int or not 0 < user_id < 2**63 or user.get("is_bot"):
        return None
    return {"event_id": event_id, "user_id": user_id, "text": text}

"""Webhook только сохраняет событие; модель работает в отдельном worker."""

from contextlib import asynccontextmanager
import hmac
import json
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
import psycopg

from .max_api import normalize_update
from .storage import Store


MAX_BODY = 1_000_000


def create_app(settings, store_factory=Store):
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', settings.webhook_secret):
        raise ValueError('MAX_WEBHOOK_SECRET должен содержать 32–256 символов A-Z, a-z, 0-9, _ или -.')

    @asynccontextmanager
    async def lifespan(app):
        store = store_factory(settings.database_url, worker=False)
        store.close()
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.get('/healthz')
    def health():
        return {'status': 'ok'}

    @app.get('/readyz')
    def ready():
        try:
            store = store_factory(settings.database_url, worker=False, initialize=False)
            try:
                store.db.execute('SELECT 1')
            finally:
                store.close()
        except psycopg.Error:
            return JSONResponse({'status': 'unavailable'}, status_code=503)
        return {'status': 'ready'}

    @app.post('/webhook')
    async def receive(request: Request):
        supplied = request.headers.get('X-Max-Bot-Api-Secret', '').encode()
        if not hmac.compare_digest(supplied, settings.webhook_secret.encode()):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return JSONResponse({'error': 'body_too_large'}, status_code=413)
        try:
            update = json.loads(body)
            if not isinstance(update, dict):
                raise ValueError
            event = normalize_update(update)
        except (ValueError, TypeError, UnicodeError):
            return JSONResponse({'error': 'invalid_update'}, status_code=400)
        if event is not None:
            def persist():
                store = store_factory(settings.database_url, worker=False, initialize=False)
                try:
                    store.receive([event])
                finally:
                    store.close()
            try:
                await run_in_threadpool(persist)
            except psycopg.Error:
                # MAX повторит доставку. До COMMIT успех не подтверждаем.
                return JSONResponse({'error': 'storage_unavailable'}, status_code=503)
        return {'success': True}

    return app

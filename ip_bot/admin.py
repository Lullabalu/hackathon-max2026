"""Local admin UI and authenticated API for reviewed content/rules revisions."""

from contextlib import asynccontextmanager
import hmac
import os
import secrets

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt
import psycopg

from .services import Services, content_template
from .storage import Store


class Draft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: str
    payload: list | dict


class Rollback(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: str
    revision_id: StrictInt


PAGE = '''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Администрирование помощника ИП</title>
<style>body{font:16px system-ui;max-width:960px;margin:2rem auto;padding:0 1rem;color:#162231}
input,select,textarea,button{font:inherit;padding:.5rem;margin:.3rem 0}textarea{width:100%;box-sizing:border-box}
button{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:1rem}
label{display:block}#status{min-height:2rem}small{display:block}</style>
<h1>Материалы помощника ИП</h1>
<label>ADMIN_TOKEN <input id="token" type="password" autocomplete="off" size="44"></label>
<small>Токен хранится только в открытой странице.</small>
<button id="load">Загрузить версии</button><button id="metrics">Статистика</button><button id="audit">Журнал</button>
<p id="status" role="status"></p>
<h2>Новый черновик</h2>
<label>Тип <select id="kind"><option value="faq">FAQ</option><option value="news">Новость</option>
<option value="tax_rules">Налоговые правила</option><option value="activities">Каталог ОКВЭД</option></select></label>
<small>FAQ: список {id, question, answer, sources: [HTTPS-ссылка ФНС]}. Новость: {title, text, url, segment}.
Сегмент all — все подписавшиеся; другой сегмент — подписка на него или all.</small>
<small>Для налоговых правил и ОКВЭД загрузите активную версию или исходный шаблон, измените данные и проверьте примеры.
Числа налогового справочника — десятичные строки; ставки — доли, например 0.06. Источники и дата проверки обязательны.
Новый налоговый год и правила распознавания деятельности требуют обновления кода.</small>
<button id="template">Загрузить активную версию / шаблон</button>
<textarea id="payload" rows="12" aria-label="Материал в JSON">[]</textarea>
<button id="validate">Проверить изменения и примеры</button><button id="draft">Сохранить черновик</button>
<h2>Версии</h2><pre id="versions">Загрузите список после ввода токена.</pre>
<label>Номер версии <input id="revision" type="number" min="1"></label>
<button id="preview">Предпросмотр</button><button id="publish">Опубликовать</button>
<button id="rollback">Откатить к версии</button><button id="enqueue">Добавить новость в очередь подписчиков</button>
<small>Публикация и откат меняют активную версию. Очередь создаётся отдельно; страница не отправляет сообщения.</small>
<h2>Результат / предпросмотр</h2><pre id="output"></pre>
<script nonce="NONCE">
const byId = id => document.getElementById(id);
async function api(path, method='GET', body) {
  const response = await fetch('/api/' + path, {method,
    headers: {'Authorization': 'Bearer ' + byId('token').value, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body), credentials: 'omit'});
  const value = await response.json();
  if (!response.ok) throw Error(value.detail || value.error || 'Запрос не выполнен');
  return value;
}
async function action(work) {
  byId('status').textContent = 'Выполняется…';
  try { const result = await work(); byId('output').textContent = JSON.stringify(result, null, 2);
    byId('status').textContent = 'Готово'; }
  catch(error) { byId('status').textContent = error.message; }
}
async function revisions() { const value = await api('revisions');
  byId('versions').textContent = JSON.stringify(value, null, 2); return value; }
function revisionId() { const id = Number(byId('revision').value);
  if (!Number.isSafeInteger(id) || id < 1) throw Error('Укажите номер версии'); return id; }
byId('load').onclick = () => action(revisions);
byId('metrics').onclick = () => action(() => api('metrics'));
byId('audit').onclick = () => action(() => api('audit'));
byId('template').onclick = () => action(async () => {
  const kind = byId('kind').value;
  const active = await api('published/' + kind);
  const value = active ? active.payload : await api('templates/' + kind);
  byId('payload').value = JSON.stringify(value, null, 2); return value;
});
byId('validate').onclick = () => action(() => api('preview', 'POST',
  {kind: byId('kind').value, payload: JSON.parse(byId('payload').value)}));
byId('draft').onclick = () => action(async () => {
  const value = await api('drafts', 'POST', {kind: byId('kind').value, payload: JSON.parse(byId('payload').value)});
  byId('revision').value = value.id; await revisions(); return value;
});
byId('preview').onclick = () => action(() => api('revisions/' + revisionId() + '/preview'));
byId('publish').onclick = () => action(async () => {
  const value = await api('revisions/' + revisionId() + '/publish', 'POST'); await revisions(); return value;
});
byId('rollback').onclick = () => action(async () => {
  const row = await api('revisions/' + revisionId());
  const value = await api('rollback', 'POST', {kind: row.kind, revision_id: row.id}); await revisions(); return value;
});
byId('enqueue').onclick = () => action(() => api('revisions/' + revisionId() + '/enqueue', 'POST'));
</script></html>'''


def create_admin_app(settings, store_factory=Store):
    token = os.getenv('ADMIN_TOKEN', '')
    if not 32 <= len(token) <= 256 or not token.isascii() or any(c.isspace() for c in token):
        raise ValueError('ADMIN_TOKEN должен содержать 32–256 ASCII-символов без пробелов.')

    @asynccontextmanager
    async def lifespan(app):
        store = store_factory(settings.database_url, worker=False)
        store.close()
        yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware('http')
    async def headers(request, call_next):
        if request.url.path.startswith('/api/'):
            supplied = request.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                return JSONResponse({'error': 'unauthorized'}, status_code=401,
                                    headers={'Cache-Control': 'no-store', 'WWW-Authenticate': 'Bearer'})
            # Limit actual bytes, including requests without Content-Length.
            if request.method in ('POST', 'PUT', 'PATCH'):
                body = bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > 1_000_000:
                        return JSONResponse({'error': 'body_too_large'}, status_code=413)
                request._body = bytes(body)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({'error': str(error)}, status_code=400)

    @app.exception_handler(psycopg.Error)
    async def unavailable(request, error):
        return JSONResponse({'error': 'storage_unavailable'}, status_code=503)

    def services():
        store = store_factory(settings.database_url, worker=False, initialize=False)
        try:
            yield Services(store)
        finally:
            store.close()

    @app.get('/', response_class=HTMLResponse)
    def index():
        nonce = secrets.token_urlsafe(24)
        return HTMLResponse(PAGE.replace('NONCE', nonce), headers={
            'Content-Security-Policy': "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
                f"script-src 'nonce-{nonce}'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"})

    @app.get('/api/metrics')
    def metrics(service=Depends(services)):
        return service.metrics()

    @app.get('/api/audit')
    def audit(service=Depends(services)):
        return service.audit()

    @app.get('/api/revisions')
    def revisions(kind: str | None = None, service=Depends(services)):
        return service.list_revisions(kind)

    @app.get('/api/revisions/{revision_id}')
    def revision(revision_id: int, service=Depends(services)):
        return service.revision(revision_id)

    @app.get('/api/published/{kind}')
    def published(kind: str, service=Depends(services)):
        return service.published(kind)

    @app.get('/api/templates/{kind}')
    def template(kind: str):
        return content_template(kind)

    @app.post('/api/preview')
    def preview(data: Draft, service=Depends(services)):
        return service.preview(data.kind, data.payload)

    @app.get('/api/revisions/{revision_id}/preview')
    def preview_revision(revision_id: int, service=Depends(services)):
        return service.preview_revision(revision_id)

    @app.post('/api/drafts', status_code=201)
    def draft(data: Draft, service=Depends(services)):
        return service.create_draft(data.kind, data.payload)

    @app.post('/api/revisions/{revision_id}/publish')
    def publish(revision_id: int, service=Depends(services)):
        return service.publish(revision_id)

    @app.post('/api/rollback')
    def rollback(data: Rollback, service=Depends(services)):
        return service.rollback(data.kind, data.revision_id)

    @app.post('/api/revisions/{revision_id}/enqueue')
    def enqueue(revision_id: int, service=Depends(services)):
        return {'enqueued': service.enqueue_news(revision_id)}

    return app

"""Opt-in services and reviewed, versioned content. No network calls or file writes.

The caller owns Store and its worker lock. Outbox delivery is at-least-once:
one worker reads ``due()``, sends, and calls ``mark()``; an external send and a
PostgreSQL commit cannot form one atomic transaction.
"""

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from .activities import default_catalog, format_activity, suggest_activity, validate_catalog
from .tax import compare_taxes, default_rules, render_comparison, validate_rules


CONTENT_KINDS = ('faq', 'news', 'tax_rules', 'activities')
MAX_CONTENT_LENGTH = 3900
NEWS_FOOTER = '\n\n/unsubscribe — отключить новости.'


def format_news(payload):
    return payload['title'] + '\n\n' + payload['text'] + '\n' + payload['url'] + NEWS_FOOTER


def format_faq(entry):
    return entry['question'] + '\n\n' + entry['answer'] + '\n\n' + '\n'.join(entry['sources'])


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{name}: нужна непустая строка до {maximum} символов.')
    return value.strip()


def _topic(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise ValueError('Тема: 1–64 символа a-z, 0-9, _ или -.')
    return value


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Нужны дата и время с часовым поясом.')
    return value.astimezone(timezone.utc)


def _url(value, *, official=False):
    value = _text(value, 'URL', 2048)
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'https' and parsed.hostname and parsed.username is None
                 and parsed.password is None and parsed.port in (None, 443)
                 and not any(ord(c) < 33 or c == '\\' for c in value))
        if official:
            valid = valid and (parsed.hostname in ('nalog.gov.ru', 'npd.nalog.ru', 'ausn.nalog.ru')
                               or parsed.hostname.endswith('.nalog.gov.ru'))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Нужен HTTPS URL официального сайта ФНС.' if official else 'Нужен корректный HTTPS URL.')
    return value


def validate_content(kind, payload):
    """Return a detached JSON value under the exact schema for its kind."""
    if kind == 'faq':
        if not isinstance(payload, list) or not 1 <= len(payload) <= 500:
            raise ValueError('FAQ должен содержать от 1 до 500 записей.')
        result, ids = [], set()
        for entry in payload:
            if not isinstance(entry, dict) or set(entry) != {'id', 'question', 'answer', 'sources'}:
                raise ValueError('Запись FAQ: id, question, answer, sources.')
            ident = _text(entry['id'], 'id', 100)
            if ident in ids:
                raise ValueError('Идентификаторы FAQ должны быть уникальны.')
            ids.add(ident)
            sources = entry['sources']
            if not isinstance(sources, list) or not 1 <= len(sources) <= 20:
                raise ValueError('FAQ должен содержать от 1 до 20 ссылок на источники.')
            card = {'id': ident, 'question': _text(entry['question'], 'Вопрос', 2000),
                    'answer': _text(entry['answer'], 'Ответ', 12000),
                    'sources': [_url(url, official=True) for url in sources]}
            if len(format_faq(card)) > MAX_CONTENT_LENGTH:
                raise ValueError('FAQ: вопрос, ответ и все ссылки вместе должны помещаться в 3900 символов.')
            result.append(card)
    elif kind == 'news':
        if not isinstance(payload, dict) or set(payload) != {'title', 'text', 'url', 'segment'}:
            raise ValueError('Новость: title, text, url, segment.')
        result = {'title': _text(payload['title'], 'Заголовок', 300),
                  'text': _text(payload['text'], 'Текст', 3500),
                  'url': _url(payload['url']), 'segment': _topic(payload['segment'])}
        if len(format_news(result)) > MAX_CONTENT_LENGTH:
            raise ValueError('Новость с заголовком, ссылкой и командой отписки должна помещаться в 3900 символов.')
    elif kind == 'tax_rules':
        result = validate_rules(payload)
    elif kind == 'activities':
        result = validate_catalog(payload)
    else:
        raise ValueError('Тип материала: faq, news, tax_rules или activities.')
    if len(json.dumps(result, ensure_ascii=False).encode()) > 1_000_000:
        raise ValueError('Материал превышает 1 МБ.')
    return result


def content_template(kind):
    """Safe starting payload; never returns live user data."""
    if kind == 'tax_rules':
        return default_rules()
    if kind == 'activities':
        return default_catalog()
    if kind == 'faq':
        return [{'id': 'example', 'question': 'Как зарегистрировать ИП?',
                 'answer': 'Используйте официальный сервис ФНС.', 'sources': ['https://www.nalog.gov.ru/create_business/ip/']}]
    if kind == 'news':
        return {'title': 'Заголовок', 'text': 'Проверенный текст новости.', 'url': 'https://www.nalog.gov.ru/new2026/', 'segment': 'all'}
    raise ValueError('Неизвестный тип материала.')


def _changed_paths(before, after, path=''):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after)):
            location = f'{path}.{key}' if path else key
            if key not in before or key not in after:
                result.append(location)
            else:
                result.extend(_changed_paths(before[key], after[key], location))
        return result
    return [path or 'payload']


def preview_content(kind, payload, *, baseline=None):
    """Validate and demonstrate on synthetic inputs, without publishing/sending."""
    payload = validate_content(kind, payload)
    baseline = validate_content(kind, baseline) if baseline is not None else content_template(kind)
    output = {'kind': kind, 'valid': True, 'changed_fields': _changed_paths(baseline, payload),
              'notes': ['Проверена схема и работа на примерах; достоверность правовых изменений подтверждает редактор по указанным источникам.'],
              'examples': []}
    if kind == 'tax_rules':
        scenarios = [
            {'activity': 'Разработка сайтов', 'region': 'Самарская область', 'revenue': '1200000',
             'expenses': '300000', 'employees': 0, 'clients': 'Физлица', 'new_ip': True,
             'npd_deduction_remaining': 0, 'year': 2026},
            {'activity': 'Ремонт компьютеров', 'region': 'Самарская область', 'revenue': '3000000',
             'expenses': '1600000', 'employees': 2, 'clients': 'ИП и организации', 'previous_revenue': 0,
             'payroll': '1000000', 'employer_contributions': '300000', 'year': 2026},
        ]
        for answers in scenarios:
            before = compare_taxes(answers, rules=baseline)
            after = compare_taxes(answers, rules=payload)
            output['examples'].append({'input': answers, 'before': before, 'after': after,
                                       'text': render_comparison(after)})
        output['notes'].append('Поддерживается алгоритм 2026 года; новый налоговый год или новая формула требуют изменения кода.')
    elif kind == 'activities':
        for text in ('Разрабатываю сайты', 'Пеку торты', 'Продаю водку'):
            after = suggest_activity(text, catalog=payload)
            output['examples'].append({'input': text, 'before': suggest_activity(text, catalog=baseline),
                                       'after': after, 'text': format_activity(after)})
        output['notes'].append('Редактируются известные записи каталога; новые правила распознавания требуют изменения кода.')
    else:
        output['payload'] = payload
    return output


class Services:
    def __init__(self, store):
        self.store = store
        self.db = store.db

    def subscribe(self, user_id, topic='all'):
        topic = _topic(topic)
        return self.db.execute('''INSERT INTO service_subscriptions(user_id, topic)
            VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING topic''', (user_id, topic)).fetchone() is not None

    def subscriptions(self, user_id):
        return [row['topic'] for row in self.db.execute(
            'SELECT topic FROM service_subscriptions WHERE user_id = %s ORDER BY topic', (user_id,))]

    def unsubscribe(self, user_id, topic=None):
        if topic is not None:
            topic = _topic(topic)
        with self.db.transaction():
            self.db.execute('SELECT pg_advisory_xact_lock(1296128066, 5)')
            count = self.db.execute('''DELETE FROM service_subscriptions WHERE user_id = %s
                AND (%s::text IS NULL OR topic = %s)''', (user_id, topic, topic)).rowcount
            # Recheck consent when dispatching too: a queued news item grants no consent.
            self.db.execute('''UPDATE service_outbox o SET cancelled = TRUE
                WHERE o.user_id = %s AND o.kind = 'news' AND NOT o.delivered
                AND NOT EXISTS (SELECT 1 FROM service_subscriptions s
                    JOIN knowledge_revisions r ON r.id = o.revision_id
                    WHERE s.user_id = o.user_id AND (s.topic = 'all'
                        OR r.payload->>'segment' = 'all' OR s.topic = r.payload->>'segment'))''', (user_id,))
        return count

    def create_reminder(self, user_id, text, due_at, idempotency_key):
        text = _text(text, 'Напоминание', 4000)
        due_at = _utc(due_at)
        key = _text(idempotency_key, 'Ключ идемпотентности', 160)
        digest = hashlib.sha256(json.dumps([text, due_at.isoformat()], ensure_ascii=False).encode()).hexdigest()
        with self.db.transaction():
            row = self.db.execute('''INSERT INTO service_reminders(user_id, text, due_at, idempotency_key, content_hash)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, idempotency_key) DO NOTHING
                RETURNING *''', (user_id, text, due_at, key, digest)).fetchone()
            if row is None:
                row = self.db.execute('''SELECT * FROM service_reminders
                    WHERE user_id = %s AND idempotency_key = %s''', (user_id, key)).fetchone()
                if row['content_hash'] != digest:
                    raise ValueError('Ключ идемпотентности уже использован для другого напоминания.')
                return row
            self.db.execute('''INSERT INTO service_outbox
                (user_id, kind, text, idempotency_key, reminder_id, due_at, next_attempt_at)
                VALUES (%s, 'reminder', %s, %s, %s, %s, %s)''',
                (user_id, text, 'reminder:' + key, row['id'], due_at, due_at))
            return row

    def list_reminders(self, user_id):
        return self.db.execute('''SELECT id, text, due_at, status FROM service_reminders
            WHERE user_id = %s AND status = 'pending' ORDER BY due_at, id''', (user_id,)).fetchall()

    def cancel_reminder(self, user_id, reminder_id):
        with self.db.transaction():
            changed = self.db.execute('''UPDATE service_reminders SET status = 'cancelled'
                WHERE id = %s AND user_id = %s AND status = 'pending' RETURNING id''',
                (reminder_id, user_id)).fetchone()
            if changed:
                self.db.execute('''UPDATE service_outbox SET cancelled = TRUE
                    WHERE reminder_id = %s AND NOT delivered''', (reminder_id,))
            return changed is not None

    def due(self, limit=20, now=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('Размер порции: 1–100.')
        moment = _utc(now) if now is not None else datetime.now(timezone.utc)
        return self.db.execute('''SELECT o.* FROM service_outbox o
            WHERE NOT o.delivered AND NOT o.cancelled AND NOT o.dead_letter
                AND o.due_at <= %s AND o.next_attempt_at <= %s
                AND (o.kind = 'reminder' OR EXISTS (
                    SELECT 1 FROM service_subscriptions s
                    JOIN knowledge_revisions r ON r.id = o.revision_id
                    WHERE s.user_id = o.user_id AND (s.topic = 'all'
                        OR r.payload->>'segment' = 'all' OR s.topic = r.payload->>'segment')))
            ORDER BY o.due_at, o.id LIMIT %s''', (moment, moment, limit)).fetchall()

    def mark(self, outbox_id):
        with self.db.transaction():
            row = self.db.execute('''UPDATE service_outbox SET delivered = TRUE, text = '', last_error = NULL
                WHERE id = %s AND NOT delivered AND NOT cancelled RETURNING reminder_id''', (outbox_id,)).fetchone()
            if row and row['reminder_id'] is not None:
                self.db.execute("UPDATE service_reminders SET status = 'sent', text = '' WHERE id = %s", (row['reminder_id'],))
            return row is not None

    def fail(self, outbox_id, error_code, *, permanent=False):
        # Only machine codes enter storage; exception bodies can contain tokens/user text.
        if not isinstance(error_code, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', error_code):
            error_code = 'delivery_error'
        return self.db.execute('''UPDATE service_outbox SET attempts = attempts + 1,
            last_error = %s, dead_letter = (%s OR attempts + 1 >= 5),
            next_attempt_at = now() + LEAST(300, power(2, attempts + 1)) * interval '1 second'
            WHERE id = %s AND NOT delivered AND NOT cancelled AND NOT dead_letter''',
            (error_code, bool(permanent), outbox_id)).rowcount == 1

    def cleanup_user(self, user_id):
        """Delete service data; Store remains responsible for profile/dialog cleanup."""
        with self.db.transaction():
            self.db.execute('SELECT pg_advisory_xact_lock(1296128066, 5)')
            self.db.execute('DELETE FROM service_outbox WHERE user_id = %s', (user_id,))
            self.db.execute('DELETE FROM service_reminders WHERE user_id = %s', (user_id,))
            self.db.execute('DELETE FROM service_subscriptions WHERE user_id = %s', (user_id,))
            self.db.execute('DELETE FROM service_feedback WHERE user_id = %s', (user_id,))

    def feedback(self, user_id, score):
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
            raise ValueError('Оценка должна быть целым числом от 1 до 5.')
        self.db.execute('''INSERT INTO service_feedback(user_id, score) VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET score = EXCLUDED.score, created_at = now()''', (user_id, score))

    def metrics(self):
        """Only aggregate counts, without user IDs, raw messages or profiles."""
        totals = self.db.execute('''SELECT
            (SELECT count(*) FROM profiles) AS profiles,
            (SELECT count(*) FROM events) AS events,
            (SELECT count(DISTINCT user_id) FROM service_subscriptions) AS subscribers,
            (SELECT count(*) FROM service_reminders WHERE status = 'pending') AS reminders_pending,
            (SELECT count(*) FROM service_outbox WHERE NOT delivered AND NOT cancelled AND NOT dead_letter) AS outbox_pending,
            (SELECT count(*) FROM service_outbox WHERE delivered) AS outbox_delivered,
            (SELECT count(*) FROM service_outbox WHERE dead_letter) AS outbox_failed,
            (SELECT count(*) FROM knowledge_revisions WHERE status = 'draft') AS drafts,
            (SELECT count(*) FROM service_feedback) AS feedback_count''').fetchone()
        totals['feedback_scores'] = {str(row['score']): row['count'] for row in self.db.execute(
            'SELECT score, count(*) AS count FROM service_feedback GROUP BY score ORDER BY score')}
        totals['actions_30_days'] = {row['category']: int(row['count']) for row in self.db.execute('''
            SELECT category, sum(count) AS count FROM analytics_counters
            WHERE day >= CURRENT_DATE - 29 GROUP BY category ORDER BY category''')}
        totals['survey_progress'] = {str(row['answered']): row['count'] for row in self.db.execute('''
            SELECT (SELECT count(*) FROM jsonb_object_keys(coalesce(state->'answers', '{}'::jsonb))) AS answered,
                   count(*) AS count FROM profiles GROUP BY answered ORDER BY answered''')}
        return totals

    def create_draft(self, kind, payload, actor='admin'):
        payload = validate_content(kind, payload)
        actor = _text(actor, 'Автор', 100)
        with self.db.transaction():
            row = self.db.execute('''INSERT INTO knowledge_revisions(kind, payload)
                VALUES (%s, %s) RETURNING *''', (kind, Jsonb(payload))).fetchone()
            self._audit('draft', row['id'], actor)
            return row

    def list_revisions(self, kind=None):
        if kind is not None and kind not in CONTENT_KINDS:
            raise ValueError('Тип материала: faq, news, tax_rules или activities.')
        return self.db.execute('''SELECT id, kind, status, created_at, published_at
            FROM knowledge_revisions WHERE (%s::text IS NULL OR kind = %s)
            ORDER BY id DESC LIMIT 200''', (kind, kind)).fetchall()

    def revision(self, revision_id):
        row = self.db.execute('SELECT * FROM knowledge_revisions WHERE id = %s', (revision_id,)).fetchone()
        if row is None:
            raise ValueError('Версия не найдена.')
        return row

    def published(self, kind):
        if kind not in CONTENT_KINDS:
            raise ValueError('Тип материала: faq, news, tax_rules или activities.')
        return self.db.execute('''SELECT r.* FROM knowledge_revisions r
            JOIN knowledge_publications p ON p.revision_id = r.id WHERE p.kind = %s''', (kind,)).fetchone()

    def preview(self, kind, payload):
        validated = validate_content(kind, payload)
        current = self.published(kind)
        baseline, baseline_invalid = None, False
        if current:
            try:
                baseline = validate_content(kind, current['payload'])
            except ValueError:
                baseline_invalid = True
        result = preview_content(kind, validated, baseline=baseline)
        if baseline_invalid:
            result['notes'].append('Активная версия не проходит текущую схему; сравнение выполнено с исходным шаблоном. Можно опубликовать проверенное исправление или откатиться.')
        result['baseline_revision_id'] = current['id'] if current else None
        return result

    def preview_revision(self, revision_id):
        row = self.revision(revision_id)
        return {'revision': row, 'preview': self.preview(row['kind'], row['payload'])}

    def _audit(self, action, revision_id, actor):
        self.db.execute('INSERT INTO knowledge_audit(action, revision_id, actor) VALUES (%s, %s, %s)',
                        (action, revision_id, actor))

    def audit(self, limit=100):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError('Размер журнала: 1–200.')
        return self.db.execute('SELECT * FROM knowledge_audit ORDER BY id DESC LIMIT %s', (limit,)).fetchall()

    def _activate(self, row, action, actor):
        # Publication and rollback revalidate stored data, even if a draft was
        # created by an older application version. Preview performs no writes.
        self.preview(row['kind'], row['payload'])
        self.db.execute("UPDATE knowledge_revisions SET status = 'superseded' WHERE kind = %s AND status = 'published'", (row['kind'],))
        self.db.execute("UPDATE knowledge_revisions SET status = 'published', published_at = now() WHERE id = %s", (row['id'],))
        self.db.execute('''INSERT INTO knowledge_publications(kind, revision_id) VALUES (%s, %s)
            ON CONFLICT (kind) DO UPDATE SET revision_id = EXCLUDED.revision_id''', (row['kind'], row['id']))
        self._audit(action, row['id'], actor)
        return self.revision(row['id'])

    def publish(self, revision_id, actor='admin'):
        actor = _text(actor, 'Автор', 100)
        with self.db.transaction():
            self.db.execute('SELECT pg_advisory_xact_lock(1296128066, 5)')
            row = self.revision(revision_id)
            if row['status'] == 'published':
                return row
            if row['status'] != 'draft':
                raise ValueError('Опубликовать можно черновик; для старой версии используйте откат.')
            return self._activate(row, 'publish', actor)

    def rollback(self, kind, revision_id, actor='admin'):
        if kind not in CONTENT_KINDS:
            raise ValueError('Неизвестный тип материала.')
        actor = _text(actor, 'Автор', 100)
        with self.db.transaction():
            self.db.execute('SELECT pg_advisory_xact_lock(1296128066, 5)')
            row = self.revision(revision_id)
            if row['kind'] != kind or row['published_at'] is None:
                raise ValueError('Откат возможен только к опубликованной версии того же типа.')
            if row['status'] == 'published':
                return row
            return self._activate(row, 'rollback', actor)

    def enqueue_news(self, revision_id, actor='admin'):
        actor = _text(actor, 'Автор', 100)
        with self.db.transaction():
            self.db.execute('SELECT pg_advisory_xact_lock(1296128066, 5)')
            row = self.revision(revision_id)
            if row['kind'] != 'news' or row['status'] != 'published':
                raise ValueError('В очередь можно добавить только опубликованную новость.')
            payload = validate_content('news', row['payload'])
            count = self.db.execute('''INSERT INTO service_outbox
                (user_id, kind, text, idempotency_key, revision_id, due_at, next_attempt_at)
                SELECT DISTINCT s.user_id, 'news', %s, %s, %s, now(), now()
                FROM service_subscriptions s WHERE s.topic = 'all' OR %s = 'all' OR s.topic = %s
                ON CONFLICT (user_id, idempotency_key) DO NOTHING''',
                (format_news(payload),
                 f'news:{revision_id}', revision_id, payload['segment'], payload['segment'])).rowcount
            self._audit('enqueue', revision_id, actor)
            return count

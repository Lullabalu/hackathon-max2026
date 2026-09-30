"""PostgreSQL: очередь, анкеты, история диалога и прочитанные источники."""

import hashlib
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .dialog import respond
from .models import Reply
from .budget import BudgetExpired


class DeadlineConnection(psycopg.Connection):
    """Don't leave libpq ACTIVE after our BaseException interrupts a query.

    Psycopg automatically cancels KeyboardInterrupt/SystemExit, but not our
    deadline exception. Closing this socket also prevents an unbounded cancel
    handshake during an outage. The worker reacquires its lock on reconnect.
    """
    budget_interrupted = False

    def wait(self, gen, interval=0.1, **kwargs):
        try:
            return super().wait(gen, interval=interval, **kwargs)
        except BudgetExpired:
            self.budget_interrupted = True
            self.pgconn.finish()
            raise


class StoreInUseError(RuntimeError):
    pass


class Store:
    def __init__(self, dsn: str, *, worker=True, initialize=True):
        # Миграции может применить соседняя роль (например, админка).
        # SELECT * после добавления колонки не должен сломать старый prepared plan.
        self._dsn, self._worker = dsn, worker
        self.db = DeadlineConnection.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=2, prepare_threshold=None)
        try:
            # Keep DSN options (notably isolated-test search_path) intact. These
            # limits also cover operations outside a request's signal budget.
            self.db.execute("SET statement_timeout = '2000ms'")
            self.db.execute("SET lock_timeout = '1000ms'")
            if worker and not self.db.execute('SELECT pg_try_advisory_lock(1296128066, 1) AS acquired').fetchone()['acquired']:
                raise StoreInUseError('Эту базу уже использует другой обработчик очереди.')
            if initialize:
                self.migrate()
        except BaseException:
            self.db.close()
            raise

    def migrate(self):
        with self.db.transaction():
            self.db.execute('SELECT pg_advisory_xact_lock(1296128066, 2)')
            self.db.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())')
            versions = {row['version'] for row in self.db.execute('SELECT version FROM schema_migrations')}
            for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
                if path.name not in versions:
                    self.db.execute(path.read_text())
                    self.db.execute('INSERT INTO schema_migrations(version) VALUES (%s)', (path.name,))

    def close(self):
        self.db.close()

    def reconnect_if_needed(self):
        if not self.db.closed:
            return False
        replacement = type(self)(self._dsn, worker=self._worker, initialize=False)
        self.db = replacement.db
        return True

    def acquire_polling_lock(self):
        # Session locks disappear on reconnect; polling must call this again
        # before reading the durable cursor or asking MAX for another batch.
        if not self.db.execute('SELECT pg_try_advisory_lock(1296128066, 4) AS acquired').fetchone()['acquired']:
            raise StoreInUseError('Получатель polling для этой базы уже запущен.')

    def get_meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key = %s', (key,)).fetchone()
        return row['value'] if row else None

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (key, Jsonb(value)))

    def bind_bot(self, bot_id):
        with self.db.transaction():
            self.db.execute('SELECT pg_advisory_xact_lock(1296128066, 3)')
            previous = self.get_meta('bot_id')
            if previous is not None and previous != bot_id:
                raise ValueError('Эта база принадлежит другому боту.')
            self.set_meta('bot_id', bot_id)

    def enqueue(self, events, marker=None):
        with self.db.transaction():
            for event in events:
                self.db.execute('INSERT INTO events(event_id, user_id, text) VALUES (%s, %s, %s) ON CONFLICT (event_id) DO NOTHING',
                                (event['event_id'], event['user_id'], event['text']))
            if marker is not None:
                self.set_meta('marker', marker)

    def receive(self, events, marker=None):
        """Вход из MAX: до явного согласия личный текст в БД не попадает."""
        from .privacy import inbound_text
        with self.db.transaction():
            for event in events:
                state = self.load_profile(event['user_id'])
                # /consent мог прийти в том же пакете до обработки worker.
                latest = self.db.execute('''SELECT text FROM events WHERE user_id = %s
                    AND NOT delivered AND NOT dead_letter
                    AND lower(trim(text)) IN ('/consent', '/decline', '/delete')
                    ORDER BY seq DESC LIMIT 1''', (event['user_id'],)).fetchone()
                if latest:
                    state = {'consent': {'accepted': latest['text'].strip().lower() == '/consent'}}
                self.enqueue([{**event, 'text': inbound_text(state, event['text'])}])
            if marker is not None:
                self.set_meta('marker', marker)

    def pending(self):
        # Сбой одного пользователя не блокирует других. Порядок внутри диалога сохраняем.
        return self.db.execute('''
            SELECT e.*, EXTRACT(EPOCH FROM clock_timestamp() - e.received_at) AS queue_age_seconds
            FROM events e
            WHERE NOT e.delivered AND NOT e.dead_letter AND e.next_attempt_at <= now()
              AND NOT EXISTS (SELECT 1 FROM events prior WHERE prior.user_id = e.user_id
                  AND prior.seq < e.seq AND NOT prior.delivered AND NOT prior.dead_letter)
            ORDER BY e.seq LIMIT 1
        ''').fetchone()

    def load_profile(self, user_id):
        row = self.db.execute('SELECT state FROM profiles WHERE user_id = %s', (user_id,)).fetchone()
        return row['state'] if row else None

    def history(self, user_id):
        rows = self.db.execute('SELECT question, answer FROM conversation_turns WHERE user_id = %s ORDER BY seq DESC LIMIT 3', (user_id,)).fetchall()
        return list(reversed(rows))

    def prepare_reply(self, event, responder=None):
        event = self.db.execute('SELECT * FROM events WHERE seq = %s', (event['seq'],)).fetchone()
        if event is None:
            raise ValueError('Событие отсутствует в очереди.')
        if event['reply'] is not None:
            return event['reply']
        state = self.load_profile(event['user_id'])
        # Сеть и генерация выполняются БЕЗ открытой транзакции PostgreSQL.
        result = responder(state, event['text']) if responder else Reply(*respond(state, event['text']))
        with self.db.transaction():
            current = self.db.execute('SELECT reply FROM events WHERE seq = %s FOR UPDATE', (event['seq'],)).fetchone()
            if current['reply'] is not None:
                return current['reply']
            if result.state is None:
                self.db.execute('DELETE FROM profiles WHERE user_id = %s', (event['user_id'],))
            else:
                self.db.execute('INSERT INTO profiles(user_id, state) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET state = EXCLUDED.state',
                                (event['user_id'], Jsonb(result.state)))
            command = event['text'].strip().split(maxsplit=1)[0].lower() if event['text'].strip() else ''
            if command in {'/delete', '/decline', '/new'}:
                from .services import Services
                Services(self).cleanup_user(event['user_id'])
                self.db.execute("UPDATE events SET text = '', reply = '', reply_files = '[]'::jsonb WHERE user_id = %s AND seq < %s", (event['user_id'], event['seq']))
                self.db.execute('DELETE FROM conversation_turns WHERE user_id = %s', (event['user_id'],))
            if result.remember:
                self.db.execute('INSERT INTO conversation_turns(seq, user_id, question, answer) VALUES (%s, %s, %s, %s) ON CONFLICT (seq) DO NOTHING',
                                (event['seq'], event['user_id'], event['text'][:2000], result.text))
                self.db.execute('DELETE FROM conversation_turns WHERE user_id = %s AND seq NOT IN (SELECT seq FROM conversation_turns WHERE user_id = %s ORDER BY seq DESC LIMIT 20)',
                                (event['user_id'], event['user_id']))
            known = {'/start', '/consent', '/decline', '/begin', '/okved', '/taxes', '/tax', '/register', '/checklist', '/pdf',
                     '/form', '/steps', '/registered', '/faq', '/subscribe', '/reminders', '/feedback', '/delete', '/new'}
            category = command[1:] if command in known else 'research' if result.remember else 'conversation'
            self.db.execute('''INSERT INTO analytics_counters(day, category, count) VALUES (CURRENT_DATE, %s, 1)
                ON CONFLICT (day, category) DO UPDATE SET count = analytics_counters.count + 1''', (category,))
            self.db.execute('UPDATE events SET reply = %s, reply_files = %s WHERE seq = %s', (result.text, Jsonb(result.files), event['seq']))
        return result.text

    def prepared_files(self, seq):
        row = self.db.execute('SELECT reply_files FROM events WHERE seq = %s', (seq,)).fetchone()
        return row['reply_files'] if row else []

    def acknowledge(self, seq):
        self.db.execute('UPDATE events SET acknowledged = TRUE WHERE seq = %s', (seq,))

    def delivered(self, seq):
        self.db.execute("UPDATE events SET delivered = TRUE, text = '', reply = '', reply_files = '[]'::jsonb, last_error = NULL WHERE seq = %s", (seq,))

    def fail(self, event, error_code, *, permanent=False):
        attempts = event['attempts'] + 1
        self.db.execute('''UPDATE events SET attempts = %s, last_error = %s, dead_letter = %s,
            next_attempt_at = now() + %s * interval '1 second' WHERE seq = %s''',
            (attempts, error_code[:100], permanent or attempts >= 5, min(300, 2 ** attempts), event['seq']))

    def document(self, url):
        return self.db.execute('SELECT * FROM documents WHERE url = %s', (url,)).fetchone()

    def save_document(self, document):
        from .sources import EXTRACTOR_VERSION, chunks
        digest = hashlib.sha256(document.text.encode()).hexdigest()
        with self.db.transaction():
            previous = self.document(document.url)
            self.db.execute('''INSERT INTO documents(url, title, content_hash, content_type, links, extractor_version)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO UPDATE SET title = EXCLUDED.title,
                content_hash = EXCLUDED.content_hash, content_type = EXCLUDED.content_type,
                links = EXCLUDED.links, extractor_version = EXCLUDED.extractor_version, fetched_at = now()''',
                (document.url, document.title, digest, document.content_type, Jsonb(document.links), EXTRACTOR_VERSION))
            if previous is None or previous['content_hash'] != digest:
                self.db.execute('DELETE FROM passages WHERE url = %s', (document.url,))
                with self.db.cursor() as cursor:
                    cursor.executemany('INSERT INTO passages(url, text) VALUES (%s, %s)', [(document.url, text) for text in chunks(document.text)])

    def search_documents(self, query, urls, limit=4):
        from .sources import words
        tokens = sorted(words(query))[:20]
        if not urls or not tokens or limit <= 0:
            return []
        # Русский словарь PostgreSQL может сохранить регистр в номере формы
        # (Р21001), тогда как words() нормализует запрос к нижнему регистру.
        tokens += [token.upper() for token in tokens if any(c.isdigit() for c in token)
                   and token.upper() != token]
        query = ' OR '.join(tokens)
        hits = self.db.execute('''
            WITH q AS (SELECT websearch_to_tsquery('russian', %s) AS query),
            hits AS MATERIALIZED (
                SELECT p.id, d.url, d.title, d.fetched_at, p.text,
                       ts_rank_cd(p.search, q.query) AS rank
                FROM passages p JOIN documents d ON d.url = p.url CROSS JOIN q
                WHERE p.url = ANY(%s) AND p.search @@ q.query
                ORDER BY rank DESC, p.id LIMIT %s
            )
            SELECT hits.*, previous.text AS previous_text, following.text AS following_text,
                   continuation.text AS continuation_text
            FROM hits
            LEFT JOIN LATERAL (
                SELECT text FROM passages
                WHERE url = hits.url AND id < hits.id ORDER BY id DESC LIMIT 1
            ) previous ON true
            LEFT JOIN LATERAL (
                SELECT id, text FROM passages
                WHERE url = hits.url AND id > hits.id ORDER BY id LIMIT 1
            ) following ON true
            LEFT JOIN LATERAL (
                SELECT text FROM passages
                WHERE url = hits.url AND id > following.id ORDER BY id LIMIT 1
            ) continuation ON true
            ORDER BY hits.rank DESC, hits.id
        ''', (query, urls, min(limit, 12))).fetchall()

        def join_adjacent(left, right):
            if not left:
                return right
            if not right or right in left:
                return left
            if left in right:
                return right
            # passages записаны по порядку, окна имеют перекрытие 160 символов.
            # strip() в chunks может убрать пробелы с края: ищем точную общую
            # часть, сохраняя формулировки и исключения без пересказа.
            for overlap in range(min(160, len(left), len(right)), 39, -1):
                if left.endswith(right[:overlap]):
                    return left + right[overlap:]
            return left + '\n' + right

        for hit in hits:
            hit.pop('id')
            previous = hit.pop('previous_text')
            following = hit.pop('following_text')
            continuation = hit.pop('continuation_text')
            # FTS может не выбрать соседний абзац с исключением из правила.
            # Ещё одно следующее окно сохраняет продолжение списка способов
            # подачи. В модель затем попадает короткое окно по текущему вопросу.
            hit['text'] = join_adjacent(join_adjacent(join_adjacent(previous, hit['text']), following), continuation)
        return hits

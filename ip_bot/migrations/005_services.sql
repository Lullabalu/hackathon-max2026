CREATE TABLE service_subscriptions (
    user_id BIGINT NOT NULL, topic TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, topic)
);
CREATE TABLE service_reminders (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL, text TEXT NOT NULL, due_at TIMESTAMPTZ NOT NULL,
    idempotency_key TEXT NOT NULL, content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'cancelled', 'sent')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key)
);
CREATE INDEX service_reminders_user_idx ON service_reminders(user_id, due_at);
CREATE TABLE knowledge_revisions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('faq', 'news')),
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'superseded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_at TIMESTAMPTZ
);
CREATE TABLE knowledge_publications (
    kind TEXT PRIMARY KEY CHECK (kind IN ('faq', 'news')),
    revision_id BIGINT NOT NULL REFERENCES knowledge_revisions(id)
);
CREATE TABLE knowledge_audit (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    action TEXT NOT NULL CHECK (action IN ('draft', 'publish', 'rollback', 'enqueue')),
    revision_id BIGINT NOT NULL REFERENCES knowledge_revisions(id),
    actor TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE service_outbox (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id BIGINT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('reminder', 'news')),
    text TEXT NOT NULL, idempotency_key TEXT NOT NULL,
    reminder_id BIGINT REFERENCES service_reminders(id) ON DELETE CASCADE,
    revision_id BIGINT REFERENCES knowledge_revisions(id),
    due_at TIMESTAMPTZ NOT NULL, next_attempt_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered BOOLEAN NOT NULL DEFAULT FALSE, cancelled BOOLEAN NOT NULL DEFAULT FALSE,
    dead_letter BOOLEAN NOT NULL DEFAULT FALSE, last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, idempotency_key)
);
CREATE INDEX service_outbox_due_idx ON service_outbox(next_attempt_at, id)
    WHERE NOT delivered AND NOT cancelled AND NOT dead_letter;
CREATE TABLE service_feedback (
    user_id BIGINT PRIMARY KEY, score SMALLINT NOT NULL CHECK (score BETWEEN 1 AND 5),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

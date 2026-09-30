-- Время попадания в нашу очередь не меняется при повторах доставки.
-- Для старых строк исходное время неизвестно: default задаёт время миграции.
ALTER TABLE events ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp();

-- Старые фрагменты могли включать меню/боковые блоки HTML-страницы.
-- Версия 0 заставит загрузчик обновить их при следующем обращении.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS extractor_version INTEGER NOT NULL DEFAULT 0;

-- Состояние коллектора: какие материалы уже видели.
-- Выполняется при каждом запуске, поэтому всё через IF NOT EXISTS.
-- Текст статей здесь не хранится: только то, что нужно для дедупа и отладки.

CREATE TABLE IF NOT EXISTS seen (
    hash          TEXT PRIMARY KEY,  -- sha1 от нормализованного URL (collect.url_key)
    url           TEXT NOT NULL,     -- ссылка без трекинговых меток (collect.clean_url)
    source        TEXT NOT NULL,     -- id источника из collect.SOURCES
    title         TEXT,              -- пусто у старых URL из sitemap: их страницы не скачивались
    published_at  TEXT,              -- ISO 8601 UTC
    first_seen_at TEXT NOT NULL,     -- когда коллектор увидел впервые, ISO 8601 UTC
    status        TEXT NOT NULL CHECK (status IN ('new', 'duplicate', 'stale')),
                                     -- new — отдан дальше; duplicate — повтор другого;
                                     -- stale — старше окна свежести, дальше не отдавался
    primary_hash  TEXT REFERENCES seen(hash)  -- у duplicate: hash основного материала
);

CREATE INDEX IF NOT EXISTS seen_first_seen ON seen(first_seen_at);

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

-- ---------- Пайплайн (этап 4): pipeline.py ----------
-- Берёт из seen только status = 'new'. Таблицы ниже пишет только пайплайн,
-- кроме article — её заполняет коллектор.

-- Текст материала для triage и генерации. Коллектор кладёт сюда body каждого
-- нового основного материала (первые BODY_MAX_WORDS слов). Когда карточка
-- готова или материал отсеян, пайплайн текст стирает (body = ''): в состоянии
-- остаётся только нужное для дедупа, как и было задумано на этапе 3.
CREATE TABLE IF NOT EXISTS article (
    hash        TEXT PRIMARY KEY REFERENCES seen(hash),
    body        TEXT NOT NULL,
    from_page   INTEGER NOT NULL DEFAULT 0  -- 1: текст докачан со страницы, в фиде было мало
);

-- Отправленные батчи Anthropic Batch API. Переживают перезапуск процесса:
-- следующий запуск забирает то, что отправил предыдущий.
CREATE TABLE IF NOT EXISTS batch (
    id            TEXT PRIMARY KEY,   -- id батча у Anthropic, msgbatch_…
    stage         TEXT NOT NULL CHECK (stage IN ('triage', 'generate', 'fix')),
    requests      INTEGER NOT NULL,
    submitted_at  TEXT NOT NULL,
    collected_at  TEXT,             -- NULL — результат ещё не забран
    usage         TEXT              -- JSON: токены по видам и оценка в $, пишется при заборе
);

-- Ход материала по пайплайну. Одна строка на материал, stage — где он сейчас:
--   new → triage_sent → triaged → generate_sent → linted → fix_sent → draft
--   (неудачный батч возвращает материал на шаг назад, attempts + 1)
--   rejected — отсеян triage (важность 1 или не про ИИ); too_old — пайплайн
--   увидел его позже окна свежести; failed — попытки кончились, причина в error
CREATE TABLE IF NOT EXISTS item (
    hash        TEXT PRIMARY KEY REFERENCES seen(hash),
    stage       TEXT NOT NULL CHECK (stage IN ('new', 'triage_sent', 'triaged', 'rejected', 'too_old',
                    'generate_sent', 'linted', 'fix_sent', 'draft', 'failed')),
    batch_id    TEXT REFERENCES batch(id),   -- последний батч, куда уходил материал
    category    TEXT,
    vendor      TEXT,
    importance  INTEGER,                     -- 1–3 от triage
    reason      TEXT,                        -- почему такая важность, одна фраза от triage
    payload     TEXT,                        -- JSON карточки в работе (до draft)
    attempts    INTEGER NOT NULL DEFAULT 0,  -- сколько раз этап не удался
    error       TEXT,
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS item_stage ON item(stage);

-- ---------- Публикация (этап 5) ----------

-- Какие карточки уже ушли в PR сайта (publish.py). Одна строка на карточку:
-- предложенная не предлагается второй раз — ни принятая, ни отклонённая
-- (отклонить = удалить файл в PR). skipped — не вошла в PR по проверке,
-- причина в reason и в описании того PR, где об этом сказано.
CREATE TABLE IF NOT EXISTS publication (
    hash         TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('proposed', 'skipped')),
    pr           INTEGER,            -- номер PR в mashninski-site
    branch       TEXT,
    reason       TEXT,
    at           TEXT NOT NULL
);

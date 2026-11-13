-- Target Audience Reach Navigator: existing five-table layout, extended in place.
-- sources     -> where we collect from
-- materials   -> what the collector brought (one row per link)
-- findings    -> what the worker extracted with the model
-- runs        -> every run of every block, with status
-- subscribers -> who pressed Start in the Telegram bot

CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,            -- hn | arxiv | rss
    url         TEXT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS runs (
    id           SERIAL PRIMARY KEY,
    service      TEXT NOT NULL,           -- collector | worker | bot
    status       TEXT NOT NULL,           -- started | running | finished | failed
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ,
    items        INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS materials (
    id            SERIAL PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    run_id        INTEGER REFERENCES runs(id),
    url           TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    text          TEXT,
    content_hash  TEXT,
    published_at  TIMESTAMPTZ,
    collected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT NOT NULL DEFAULT 'new',  -- new | done | skipped | failed
    processed_prompt_version TEXT,
    processing_error TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id              SERIAL PRIMARY KEY,
    material_id     INTEGER NOT NULL REFERENCES materials(id),
    finding_index   INTEGER NOT NULL DEFAULT 1,
    entity          TEXT,                 -- legacy field, kept for old rows
    topic           TEXT,
    indicator_type  TEXT,
    ir_applicability TEXT,
    geography       TEXT,
    audience        TEXT,
    indicator       TEXT,
    value           TEXT,
    percent_base    TEXT,
    study_period    TEXT,
    useful_for      TEXT,
    evidence        TEXT,
    summary         TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Additive migration for databases created by the original workshop project.
ALTER TABLE materials ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS processed_prompt_version TEXT;
ALTER TABLE materials ADD COLUMN IF NOT EXISTS processing_error TEXT;

-- The old schema allowed one finding per material. A research publication may
-- contain several useful statistics, so uniqueness now includes prompt version
-- and the position of the finding in the model response.
ALTER TABLE findings DROP CONSTRAINT IF EXISTS findings_material_id_key;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS finding_index INTEGER NOT NULL DEFAULT 1;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS topic TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS indicator_type TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS ir_applicability TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS geography TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS audience TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS indicator TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS value TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS percent_base TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS study_period TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS useful_for TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS evidence TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS block_index INTEGER NOT NULL DEFAULT 1;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS block_position INTEGER NOT NULL DEFAULT 1;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS block_title TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS parent_finding_index INTEGER;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS source_value TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS source_percent_base TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS calculated_percent NUMERIC(12, 6);
ALTER TABLE findings ADD COLUMN IF NOT EXISTS calculation_formula TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS calculation_chain JSONB;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS base_kind TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS base_reason TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS methodology_evidence TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS behavior_period TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS is_derived BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS targeting_criteria JSONB;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS candidate_criteria JSONB;
-- Nullable fields distinguish unmarked legacy findings from explicit exclusions.
ALTER TABLE findings ADD COLUMN IF NOT EXISTS population_scope TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS is_primary BOOLEAN;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS primary_rank INTEGER;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS primary_reason TEXT;
ALTER TABLE findings ADD COLUMN IF NOT EXISTS is_demographic_breakdown BOOLEAN;

CREATE UNIQUE INDEX IF NOT EXISTS materials_content_hash_unique
    ON materials (content_hash) WHERE content_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS findings_material_prompt_position_unique
    ON findings (material_id, prompt_version, finding_index);
CREATE INDEX IF NOT EXISTS findings_material_prompt_block_position_idx
    ON findings (material_id, prompt_version, block_index, block_position);
CREATE INDEX IF NOT EXISTS findings_primary_publication_idx
    ON findings (material_id, prompt_version, primary_rank, finding_index, id)
    WHERE coalesce(is_primary, TRUE) AND NOT coalesce(is_demographic_breakdown, FALSE);

-- the bot marks a finding when it has been sent to subscribers
ALTER TABLE findings ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     BIGINT PRIMARY KEY,
    name        TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Preserve workshop data, but stop collecting the old AI-news sources.
UPDATE sources SET active = FALSE
WHERE kind IN ('hn', 'arxiv', 'rss')
  AND name IN ('Hacker News', 'Hacker News: Anthropic', 'Hacker News: OpenAI', 'arXiv cs.AI', 'Хабр: ИИ');

INSERT INTO sources (name, kind, url) VALUES
    ('ФОМ: исследования', 'fom_rss', 'https://fom.ru/rss/all'),
    ('НАФИ: результаты опросов', 'nafi_html', 'https://nafi.ru/polls/'),
    ('Data Insight: исследования', 'datainsight_rss', 'https://datainsight.ru/rss.xml'),
    ('Банк России: новое на сайте', 'cbr_rss', 'https://www.cbr.ru/rss/RssNews')
ON CONFLICT (name) DO UPDATE
SET kind = EXCLUDED.kind, url = EXCLUDED.url, active = TRUE;

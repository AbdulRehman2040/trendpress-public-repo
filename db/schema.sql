-- trendpress state schema for Neon Postgres.
--
-- core/db.py creates these automatically on first connect (CREATE TABLE IF NOT
-- EXISTS), so you do NOT have to run this by hand. It is provided so you can set
-- the tables up front in the Neon SQL editor if you prefer, and as documentation
-- of the shapes the dashboard reads. Safe to run repeatedly (idempotent).

CREATE TABLE IF NOT EXISTS used_topics (
    trend_id TEXT PRIMARY KEY,
    title    TEXT,
    used_on  DATE DEFAULT current_date
);

CREATE TABLE IF NOT EXISTS runs (
    id               BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT DEFAULT 'running',     -- running | success | partial | failed
    trigger          TEXT DEFAULT 'cron',        -- cron | manual
    trends_found     INT  DEFAULT 0,
    articles_written INT  DEFAULT 0,
    posts_published  INT  DEFAULT 0,
    posts_failed     INT  DEFAULT 0,
    error_summary    TEXT,
    log              TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id         BIGSERIAL PRIMARY KEY,
    run_id     BIGINT,
    site_id    TEXT,
    trend_id   TEXT,
    wp_post_id BIGINT,
    title      TEXT,
    url        TEXT,
    image_url  TEXT,
    status     TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS used_images (
    trend_id TEXT,
    site_id  TEXT,
    photo_id TEXT
);

CREATE TABLE IF NOT EXISTS site_health (
    id         BIGSERIAL PRIMARY KEY,
    site_id    TEXT,
    checked_on DATE DEFAULT current_date,
    note       TEXT,
    paused     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS angle_rotation (
    site_id TEXT PRIMARY KEY,
    idx     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sites (
    id                TEXT PRIMARY KEY,
    name              TEXT,
    url               TEXT,
    wp_username       TEXT,
    app_password_env  TEXT,
    niche             TEXT DEFAULT '',
    audience          TEXT DEFAULT '',
    tone              TEXT DEFAULT '',
    angle_pool        JSONB DEFAULT '[]'::jsonb,
    word_min          INT DEFAULT 650,
    word_max          INT DEFAULT 950,
    posts_per_day_cap INT DEFAULT 2,
    status            TEXT DEFAULT 'active',     -- active | canary | paused
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_posts_site_id     ON posts (site_id);
CREATE INDEX IF NOT EXISTS idx_posts_created_at  ON posts (created_at);
CREATE INDEX IF NOT EXISTS idx_posts_run_id      ON posts (run_id);
CREATE INDEX IF NOT EXISTS idx_used_images_trend ON used_images (trend_id);

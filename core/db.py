"""Neon Postgres-backed state for trendpress.

A thin functional wrapper around ``psycopg`` (psycopg 3) — no ORM. Replaces the
old local SQLite file so an external dashboard can read durable, structured
state (GitHub Actions runners wipe local disk between runs).

Connection
----------
Reads the Neon **pooled** connection string from the ``DATABASE_URL`` env var.
One module-level connection is opened lazily (autocommit, dict rows) and
re-opened automatically if it drops. Prepared statements are disabled
(``prepare_threshold=None``) for compatibility with Neon's PgBouncer pooler.

Every public function name, argument order and return type matches the previous
SQLite module exactly, so trends.py / matcher.py / writer.py / images.py /
publisher.py / main.py keep working unchanged. ``add_post`` gains two optional
keyword args (run_id, image_url) and a handful of NEW functions are added for
run-logging and DB-managed sites.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS used_topics (
    trend_id TEXT PRIMARY KEY,
    title    TEXT,
    used_on  DATE DEFAULT current_date
);

CREATE TABLE IF NOT EXISTS runs (
    id               BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMPTZ DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT DEFAULT 'running',     -- running|success|partial|failed
    trigger          TEXT DEFAULT 'cron',        -- cron|manual
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
    photo_id TEXT      -- Pexels ids and Openverse UUIDs both stored as text
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
    status            TEXT DEFAULT 'active',
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_posts_site_id    ON posts (site_id);
CREATE INDEX IF NOT EXISTS idx_posts_created_at ON posts (created_at);
CREATE INDEX IF NOT EXISTS idx_posts_run_id     ON posts (run_id);
CREATE INDEX IF NOT EXISTS idx_used_images_trend ON used_images (trend_id);
"""

# --------------------------------------------------------------------------- #
# Connection management (module-level, lazy, self-healing)
# --------------------------------------------------------------------------- #
_conn: psycopg.Connection | None = None
_schema_ready = False


def _open() -> psycopg.Connection:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is not set. trendpress now stores state in Neon Postgres; "
            "put the Neon POOLED connection string in your .env (local) or in the "
            "DATABASE_URL GitHub Actions secret (CI)."
        )
    # prepare_threshold=None disables server-side prepared statements, which the
    # Neon (PgBouncer transaction-mode) pooler does not support reliably.
    return psycopg.connect(
        dsn, autocommit=True, row_factory=dict_row, prepare_threshold=None
    )


def _get_conn() -> psycopg.Connection:
    """Return a live connection, (re)connecting and ensuring schema as needed."""
    global _conn, _schema_ready
    if _conn is None or _conn.closed:
        _conn = _open()
        _schema_ready = False
    if not _schema_ready:
        with _conn.cursor() as cur:
            cur.execute(_SCHEMA)
        _schema_ready = True
    return _conn


def _execute(sql: str, params: tuple | list = (), *, fetch: str | None = None):
    """Run a statement, retrying once if the pooled connection has dropped.

    fetch: None -> return None; "one" -> fetchone(); "all" -> fetchall().
    """
    global _conn
    last_exc: Exception | None = None
    for attempt in (1, 2):
        try:
            conn = _get_conn()
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            last_exc = exc
            try:
                if _conn is not None:
                    _conn.close()
            except Exception:
                pass
            _conn = None  # force a fresh connect on the retry
    raise last_exc  # type: ignore[misc]


def init_db() -> None:
    """Create all tables if they don't exist yet (idempotent)."""
    _get_conn()


# --------------------------------------------------------------------------- #
# used_topics
# --------------------------------------------------------------------------- #
def add_used_topic(trend_id: str, title: str, used_on: date | None = None) -> None:
    """Record a trend as used (idempotent on trend_id)."""
    used_on = used_on or date.today()
    _execute(
        "INSERT INTO used_topics (trend_id, title, used_on) VALUES (%s, %s, %s) "
        "ON CONFLICT (trend_id) DO UPDATE SET title = EXCLUDED.title, used_on = EXCLUDED.used_on",
        (trend_id, title, used_on),
    )


def is_topic_used(trend_id: str, within_days: int | None = None) -> bool:
    """True if trend_id was used (optionally only within the last N days)."""
    sql = "SELECT 1 FROM used_topics WHERE trend_id = %s"
    params: list[Any] = [trend_id]
    if within_days is not None:
        sql += " AND used_on >= current_date - %s::int"
        params.append(int(within_days))
    return _execute(sql, params, fetch="one") is not None


def list_used_topics(within_days: int | None = None) -> list[dict]:
    """List used topics, newest first, optionally within the last N days."""
    sql = "SELECT trend_id, title, used_on FROM used_topics"
    params: list[Any] = []
    if within_days is not None:
        sql += " WHERE used_on >= current_date - %s::int"
        params.append(int(within_days))
    return _execute(sql + " ORDER BY used_on DESC", params, fetch="all") or []


# --------------------------------------------------------------------------- #
# posts
# --------------------------------------------------------------------------- #
def add_post(
    site_id: str,
    trend_id: str,
    wp_post_id: int | None,
    title: str,
    url: str | None,
    status: str,
    run_id: int | None = None,
    image_url: str | None = None,
) -> int:
    """Insert a post record and return its id.

    ``run_id`` and ``image_url`` are new optional columns: existing positional
    callers keep working; the publisher passes them so the dashboard can show
    which run produced a post and its featured image.
    """
    row = _execute(
        "INSERT INTO posts (run_id, site_id, trend_id, wp_post_id, title, url, image_url, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (run_id, site_id, trend_id, wp_post_id, title, url, image_url, status),
        fetch="one",
    )
    return int(row["id"]) if row else 0


def list_posts(site_id: str | None = None) -> list[dict]:
    """List posts (optionally for one site), newest first."""
    sql = "SELECT * FROM posts"
    params: list[Any] = []
    if site_id:
        sql += " WHERE site_id = %s"
        params.append(site_id)
    return _execute(sql + " ORDER BY created_at DESC", params, fetch="all") or []


def count_posts_today(site_id: str) -> int:
    """Number of non-errored posts created today for a site (daily-cap check)."""
    row = _execute(
        "SELECT COUNT(*) AS n FROM posts "
        "WHERE site_id = %s AND created_at >= date_trunc('day', now()) "
        "AND status <> 'error'",
        (site_id,),
        fetch="one",
    )
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------- #
# used_images
# --------------------------------------------------------------------------- #
def add_used_image(trend_id: str, site_id: str, photo_id: str) -> None:
    """Record that a stock photo was used for a trend/site pair."""
    _execute(
        "INSERT INTO used_images (trend_id, site_id, photo_id) VALUES (%s, %s, %s)",
        (trend_id, site_id, str(photo_id)),
    )


def is_image_used(photo_id: str, site_id: str | None = None) -> bool:
    """True if a stock photo was already used (optionally on a given site)."""
    sql = "SELECT 1 FROM used_images WHERE photo_id = %s"
    params: list[Any] = [str(photo_id)]
    if site_id:
        sql += " AND site_id = %s"
        params.append(site_id)
    return _execute(sql, params, fetch="one") is not None


def images_used_for_trend(trend_id: str) -> set:
    """Return the set of photo ids (strings) already used for a trend (any site)."""
    rows = _execute(
        "SELECT DISTINCT photo_id FROM used_images WHERE trend_id = %s",
        (trend_id,),
        fetch="all",
    ) or []
    return {row["photo_id"] for row in rows}


# --------------------------------------------------------------------------- #
# site_health (also holds the kill-switch 'paused' flag)
# --------------------------------------------------------------------------- #
def add_site_health(
    site_id: str, note: str, paused: bool = False, checked_on: date | None = None
) -> None:
    """Record a health-check note for a site, with an optional 'paused' flag.

    The most-recent row per site is authoritative for is_site_paused(), so a
    later healthy check (paused=False) automatically clears an earlier pause.
    """
    checked_on = checked_on or date.today()
    _execute(
        "INSERT INTO site_health (site_id, checked_on, note, paused) VALUES (%s, %s, %s, %s)",
        (site_id, checked_on, note, 1 if paused else 0),
    )


def is_site_paused(site_id: str) -> bool:
    """True if the site is paused — either its sites.status is 'paused' OR the
    most recent site_health row has the paused flag set."""
    row = _execute(
        "SELECT ("
        "  EXISTS (SELECT 1 FROM sites WHERE id = %s AND status = 'paused')"
        "  OR COALESCE("
        "       (SELECT paused FROM site_health WHERE site_id = %s ORDER BY id DESC LIMIT 1), 0"
        "     ) = 1"
        ") AS paused",
        (site_id, site_id),
        fetch="one",
    )
    return bool(row["paused"]) if row else False


def recent_post_stats(site_id: str, days: int = 7) -> tuple[int, int]:
    """Return (successes, errors) recorded for a site in the last `days` days."""
    rows = _execute(
        "SELECT status, COUNT(*) AS n FROM posts "
        "WHERE site_id = %s AND created_at >= now() - make_interval(days => %s) "
        "GROUP BY status",
        (site_id, int(days)),
        fetch="all",
    ) or []
    errors = sum(r["n"] for r in rows if r["status"] == "error")
    successes = sum(r["n"] for r in rows if r["status"] != "error")
    return successes, errors


def list_site_health(site_id: str | None = None) -> list[dict]:
    """List health-check notes (optionally for one site), newest first."""
    sql = "SELECT site_id, checked_on, note, paused FROM site_health"
    params: list[Any] = []
    if site_id:
        sql += " WHERE site_id = %s"
        params.append(site_id)
    return _execute(sql + " ORDER BY id DESC", params, fetch="all") or []


# --------------------------------------------------------------------------- #
# angle_rotation
# --------------------------------------------------------------------------- #
def next_angle_index(site_id: str) -> int:
    """Return a site's current angle-rotation index, then advance it.

    Returns 0 on first call (storing 1), 1 on the next (storing 2), etc. — the
    same contract as the old SQLite version.
    """
    row = _execute(
        "INSERT INTO angle_rotation (site_id, idx) VALUES (%s, 1) "
        "ON CONFLICT (site_id) DO UPDATE SET idx = angle_rotation.idx + 1 "
        "RETURNING idx - 1 AS prev",
        (site_id,),
        fetch="one",
    )
    return int(row["prev"]) if row else 0


# --------------------------------------------------------------------------- #
# runs (NEW — run history for the dashboard)
# --------------------------------------------------------------------------- #
def start_run(trigger: str = "cron") -> int:
    """Insert a 'running' run row and return its id."""
    row = _execute(
        "INSERT INTO runs (trigger, status) VALUES (%s, 'running') RETURNING id",
        (trigger,),
        fetch="one",
    )
    return int(row["id"]) if row else 0


def finish_run(
    run_id: int,
    status: str,
    trends_found: int = 0,
    articles_written: int = 0,
    posts_published: int = 0,
    posts_failed: int = 0,
    error_summary: str | None = None,
    log: str | None = None,
) -> None:
    """Mark a run finished with its final counts, error summary and captured log."""
    if not run_id:
        return
    _execute(
        "UPDATE runs SET finished_at = now(), status = %s, trends_found = %s, "
        "articles_written = %s, posts_published = %s, posts_failed = %s, "
        "error_summary = %s, log = %s WHERE id = %s",
        (status, int(trends_found), int(articles_written), int(posts_published),
         int(posts_failed), error_summary, log, int(run_id)),
    )


# --------------------------------------------------------------------------- #
# sites (NEW — sites move out of YAML so the dashboard can manage them)
# --------------------------------------------------------------------------- #
def _site_row_to_dict(row: dict) -> dict:
    """Convert a sites row into the shape core.load_sites() returns from YAML."""
    angle_pool = row.get("angle_pool") or []
    if isinstance(angle_pool, str):  # defensive: should already be a list from JSONB
        angle_pool = [angle_pool] if angle_pool else []
    return {
        "id": row["id"],
        "name": row.get("name"),
        "url": row.get("url"),
        "wp_username": row.get("wp_username"),
        "app_password_env": row.get("app_password_env"),
        "niche": row.get("niche") or "",
        "audience": row.get("audience") or "",
        "tone": row.get("tone") or "",
        "angle_pool": list(angle_pool),
        "word_range": [int(row.get("word_min") or 650), int(row.get("word_max") or 950)],
        "posts_per_day_cap": int(row.get("posts_per_day_cap") or 2),
        "status": row.get("status") or "active",
    }


def load_sites_from_db() -> list[dict]:
    """Return ALL sites (active+canary+paused) as dicts in the same shape that
    the old core.load_sites() returned, so the pipeline consumes them unchanged."""
    rows = _execute(
        "SELECT * FROM sites ORDER BY created_at ASC, id ASC", fetch="all"
    ) or []
    return [_site_row_to_dict(r) for r in rows]


def upsert_site(site: dict) -> None:
    """Insert or update one site from a YAML-shaped dict (used by the importer
    and the dashboard). word_range [low, high] maps to word_min/word_max; the WP
    application password is NEVER stored — only the name of its env var."""
    word_range = site.get("word_range") or [650, 950]
    try:
        word_min = int(word_range[0])
        word_max = int(word_range[1])
    except (TypeError, ValueError, IndexError):
        word_min, word_max = 650, 950
    _execute(
        "INSERT INTO sites (id, name, url, wp_username, app_password_env, niche, "
        "audience, tone, angle_pool, word_min, word_max, posts_per_day_cap, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (id) DO UPDATE SET "
        "  name = EXCLUDED.name, url = EXCLUDED.url, wp_username = EXCLUDED.wp_username, "
        "  app_password_env = EXCLUDED.app_password_env, niche = EXCLUDED.niche, "
        "  audience = EXCLUDED.audience, tone = EXCLUDED.tone, angle_pool = EXCLUDED.angle_pool, "
        "  word_min = EXCLUDED.word_min, word_max = EXCLUDED.word_max, "
        "  posts_per_day_cap = EXCLUDED.posts_per_day_cap, status = EXCLUDED.status",
        (
            site["id"], site.get("name"), site.get("url"), site.get("wp_username"),
            site.get("app_password_env"), site.get("niche") or "", site.get("audience") or "",
            site.get("tone") or "", Json(list(site.get("angle_pool") or [])),
            word_min, word_max, int(site.get("posts_per_day_cap") or 2),
            site.get("status") or "active",
        ),
    )


def set_site_status(site_id: str, status: str) -> None:
    """Set a site's status (active|canary|paused)."""
    _execute("UPDATE sites SET status = %s WHERE id = %s", (status, site_id))


def site_exists(site_id: str) -> bool:
    """True if a site with this id exists."""
    return _execute("SELECT 1 FROM sites WHERE id = %s", (site_id,), fetch="one") is not None

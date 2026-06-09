"""SQLite-backed state for trendpress.

A thin functional wrapper around the stdlib ``sqlite3`` module — no ORM. The
schema is created on first connection; the database lives at data/state.db.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Iterator

from core import DATA_DIR

DB_PATH = DATA_DIR / "state.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS used_topics (
    trend_id TEXT PRIMARY KEY,
    title    TEXT,
    used_on  DATE
);
CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id    TEXT,
    trend_id   TEXT,
    wp_post_id INTEGER,
    title      TEXT,
    url        TEXT,
    status     TEXT,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS used_images (
    trend_id TEXT,
    site_id  TEXT,
    photo_id TEXT      -- Pexels ids and Openverse UUIDs both stored as text
);
CREATE TABLE IF NOT EXISTS site_health (
    site_id    TEXT,
    checked_on DATE,
    note       TEXT,
    paused     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS angle_rotation (
    site_id TEXT PRIMARY KEY,
    idx     INTEGER NOT NULL DEFAULT 0
);
"""


def _connect() -> sqlite3.Connection:
    """Open a connection, ensuring the data dir and schema both exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """Connection context manager that commits on success and always closes."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the database and tables if they don't exist yet."""
    with _db():
        pass


# --- used_topics ------------------------------------------------------------
def add_used_topic(trend_id: str, title: str, used_on: date | None = None) -> None:
    """Record a trend as used (idempotent on trend_id)."""
    used_on = used_on or date.today()
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO used_topics (trend_id, title, used_on) VALUES (?, ?, ?)",
            (trend_id, title, used_on.isoformat()),
        )


def is_topic_used(trend_id: str, within_days: int | None = None) -> bool:
    """True if trend_id was used (optionally only within the last N days)."""
    sql = "SELECT 1 FROM used_topics WHERE trend_id = ?"
    params: list = [trend_id]
    if within_days is not None:
        sql += " AND used_on >= date('now', ?)"
        params.append(f"-{within_days} days")
    with _db() as conn:
        return conn.execute(sql, params).fetchone() is not None


def list_used_topics(within_days: int | None = None) -> list[sqlite3.Row]:
    """List used topics, newest first, optionally within the last N days."""
    sql = "SELECT trend_id, title, used_on FROM used_topics"
    params: list = []
    if within_days is not None:
        sql += " WHERE used_on >= date('now', ?)"
        params.append(f"-{within_days} days")
    with _db() as conn:
        return conn.execute(sql + " ORDER BY used_on DESC", params).fetchall()


# --- posts ------------------------------------------------------------------
def add_post(
    site_id: str,
    trend_id: str,
    wp_post_id: int | None,
    title: str,
    url: str | None,
    status: str,
) -> int:
    """Insert a post record and return its rowid."""
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO posts (site_id, trend_id, wp_post_id, title, url, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (site_id, trend_id, wp_post_id, title, url, status,
             datetime.now().isoformat(timespec="seconds")),
        )
        return int(cur.lastrowid or 0)


def list_posts(site_id: str | None = None) -> list[sqlite3.Row]:
    """List posts (optionally for one site), newest first."""
    sql = "SELECT * FROM posts"
    params: list = []
    if site_id:
        sql += " WHERE site_id = ?"
        params.append(site_id)
    with _db() as conn:
        return conn.execute(sql + " ORDER BY created_at DESC", params).fetchall()


def count_posts_today(site_id: str) -> int:
    """Number of non-errored posts created today for a site (daily-cap check).

    created_at is stored in local time, so compare against the local date
    (not SQLite's UTC default for date('now')).
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM posts "
            "WHERE site_id = ? AND date(created_at) = date('now', 'localtime') "
            "AND status != 'error'",
            (site_id,),
        ).fetchone()
        return int(row["n"])


# --- used_images ------------------------------------------------------------
def add_used_image(trend_id: str, site_id: str, photo_id: str) -> None:
    """Record that a stock photo was used for a trend/site pair.

    photo_id is the provider's id as a string (Pexels int or Openverse UUID).
    """
    with _db() as conn:
        conn.execute(
            "INSERT INTO used_images (trend_id, site_id, photo_id) VALUES (?, ?, ?)",
            (trend_id, site_id, str(photo_id)),
        )


def is_image_used(photo_id: str, site_id: str | None = None) -> bool:
    """True if a stock photo was already used (optionally on a given site)."""
    sql = "SELECT 1 FROM used_images WHERE photo_id = ?"
    params: list = [str(photo_id)]
    if site_id:
        sql += " AND site_id = ?"
        params.append(site_id)
    with _db() as conn:
        return conn.execute(sql, params).fetchone() is not None


def images_used_for_trend(trend_id: str) -> set:
    """Return the set of photo ids (strings) already used for a trend (any site).

    Used by the images stage to give each site a different photo for the same
    trend.
    """
    with _db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT photo_id FROM used_images WHERE trend_id = ?",
            (trend_id,),
        ).fetchall()
    return {row["photo_id"] for row in rows}


# --- site_health (also holds the kill-switch 'paused' flag) -----------------
def add_site_health(
    site_id: str, note: str, paused: bool = False, checked_on: date | None = None
) -> None:
    """Record a health-check note for a site, with an optional 'paused' flag.

    The most-recent row per site is authoritative for is_site_paused(), so a
    later healthy check (paused=False) automatically clears an earlier pause.
    """
    checked_on = checked_on or date.today()
    with _db() as conn:
        conn.execute(
            "INSERT INTO site_health (site_id, checked_on, note, paused) VALUES (?, ?, ?, ?)",
            (site_id, checked_on.isoformat(), note, 1 if paused else 0),
        )


def is_site_paused(site_id: str) -> bool:
    """True if the most recent health record for the site has the paused flag set."""
    with _db() as conn:
        row = conn.execute(
            "SELECT paused FROM site_health WHERE site_id = ? ORDER BY rowid DESC LIMIT 1",
            (site_id,),
        ).fetchone()
    return bool(row["paused"]) if row else False


def recent_post_stats(site_id: str, days: int = 7) -> tuple[int, int]:
    """Return (successes, errors) recorded for a site in the last `days` days."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM posts "
            "WHERE site_id = ? AND created_at >= datetime('now', 'localtime', ?) "
            "GROUP BY status",
            (site_id, f"-{days} days"),
        ).fetchall()
    errors = sum(r["n"] for r in rows if r["status"] == "error")
    successes = sum(r["n"] for r in rows if r["status"] != "error")
    return successes, errors


def list_site_health(site_id: str | None = None) -> list[sqlite3.Row]:
    """List health-check notes (optionally for one site), newest first."""
    sql = "SELECT site_id, checked_on, note, paused FROM site_health"
    params: list = []
    if site_id:
        sql += " WHERE site_id = ?"
        params.append(site_id)
    with _db() as conn:
        return conn.execute(sql + " ORDER BY rowid DESC", params).fetchall()


# --- angle_rotation ---------------------------------------------------------
def next_angle_index(site_id: str) -> int:
    """Return a site's current angle-rotation index, then advance it.

    Persisted so that fallback editorial angles vary across consecutive runs.
    """
    with _db() as conn:
        row = conn.execute(
            "SELECT idx FROM angle_rotation WHERE site_id = ?", (site_id,)
        ).fetchone()
        current = int(row["idx"]) if row else 0
        conn.execute(
            "INSERT INTO angle_rotation (site_id, idx) VALUES (?, ?) "
            "ON CONFLICT(site_id) DO UPDATE SET idx = excluded.idx",
            (site_id, current + 1),
        )
        return current

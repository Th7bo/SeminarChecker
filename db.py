"""
PostgreSQL storage for seminar reminder state and future data.
Uses DATABASE_URL environment variable (e.g. postgresql://user:pass@host:5432/dbname).
"""

import logging
import os
from contextlib import contextmanager
from typing import Iterator

import psycopg2

log = logging.getLogger("seminar_reminder.db")

# Table for seminars we have already notified. Extensible for future fields.
SCHEMA = """
CREATE TABLE IF NOT EXISTS notified_seminars (
    seminar_id   TEXT PRIMARY KEY,
    seminar_url  TEXT,
    title        TEXT,
    notified_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_notified_seminars_notified_at
    ON notified_seminars (notified_at);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_connection():
    """Return a new connection using DATABASE_URL. Raises if DATABASE_URL is unset."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return psycopg2.connect(url)


@contextmanager
def connection() -> Iterator:
    """Context manager for a single DB connection (commit on success, rollback on error)."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they do not exist. Safe to call on every run."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
    log.debug("Database schema initialized")


def get_notified_seminar_ids() -> set[str]:
    """Return the set of seminar_id values that have already been notified."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT seminar_id FROM notified_seminars")
            rows = cur.fetchall()
    return {row[0] for row in rows}


def is_notified(seminar_id: str) -> bool:
    """Return True if this seminar_id has already been notified."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM notified_seminars WHERE seminar_id = %s",
                (seminar_id,),
            )
            return cur.fetchone() is not None


def mark_notified(
    seminar_id: str,
    *,
    seminar_url: str | None = None,
    title: str | None = None,
) -> None:
    """Record that we have notified for this seminar. Idempotent (INSERT or no-op)."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO notified_seminars (seminar_id, seminar_url, title)
                VALUES (%s, %s, %s)
                ON CONFLICT (seminar_id) DO NOTHING
                """,
                (seminar_id, seminar_url, title),
            )
    log.debug("Marked notified: %s", seminar_id)


def get_notified_count() -> int:
    """Return total number of notified seminars (for logging)."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM notified_seminars")
            return cur.fetchone()[0]


def get_status_message_id() -> str | None:
    """Return the Discord message ID of the status embed, or None if not yet created."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM bot_state WHERE key = %s",
                ("status_message_id",),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None


def set_status_message_id(message_id: str) -> None:
    """Store the Discord message ID of the status embed."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_state (key, value) VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                ("status_message_id", str(message_id)),
            )
    log.debug("Stored status_message_id: %s", message_id)

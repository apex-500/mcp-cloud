"""
PostgreSQL database backend for MCP Cloud.

When DATABASE_URL is set, provides persistent storage for API keys and usage logs.
Otherwise, the application falls back to JSON file storage.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger("mcp_cloud.db")

_DATABASE_URL: str | None = None

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore[assignment]


def get_database_url() -> str | None:
    """Return DATABASE_URL if set and psycopg2 is available."""
    global _DATABASE_URL
    if _DATABASE_URL is not None:
        return _DATABASE_URL
    url = os.getenv("DATABASE_URL")
    if url and psycopg2 is None:
        logger.warning("DATABASE_URL is set but psycopg2 is not installed; falling back to JSON files")
        return None
    _DATABASE_URL = url
    return _DATABASE_URL


def is_pg_enabled() -> bool:
    """Return True if PostgreSQL backend is active."""
    return get_database_url() is not None


@contextmanager
def get_conn() -> Generator[Any, None, None]:
    """Yield a psycopg2 connection (auto-committed on clean exit, rolled back on error)."""
    url = get_database_url()
    if url is None:
        raise RuntimeError("PostgreSQL is not configured")
    conn = psycopg2.connect(url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Schema initialisation
# --------------------------------------------------------------------------- #

_CREATE_API_KEYS = """
CREATE TABLE IF NOT EXISTS api_keys (
    api_key      TEXT PRIMARY KEY,
    email        TEXT NOT NULL,
    tier         TEXT NOT NULL DEFAULT 'free',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used    TIMESTAMPTZ,
    calls_today  INTEGER NOT NULL DEFAULT 0,
    calls_today_date DATE NOT NULL DEFAULT CURRENT_DATE,
    calls_this_month INTEGER NOT NULL DEFAULT 0,
    calls_this_month_period TEXT NOT NULL DEFAULT TO_CHAR(NOW(), 'YYYY-MM'),
    active       BOOLEAN NOT NULL DEFAULT TRUE
);
"""

_CREATE_USAGE_LOG = """
CREATE TABLE IF NOT EXISTS usage_log (
    id          SERIAL PRIMARY KEY,
    api_key     TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latency_ms  REAL,
    success     BOOLEAN NOT NULL DEFAULT TRUE,
    error       TEXT
);
"""

_CREATE_USAGE_LOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_usage_log_api_key ON usage_log (api_key);
"""

_CREATE_USAGE_LOG_TS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_usage_log_timestamp ON usage_log (timestamp);
"""


def init_db() -> None:
    """Create tables if they don't already exist. No-op when PG is disabled."""
    if not is_pg_enabled():
        logger.info("PostgreSQL not configured — using JSON file storage")
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_CREATE_API_KEYS)
            cur.execute(_CREATE_USAGE_LOG)
            cur.execute(_CREATE_USAGE_LOG_INDEX)
            cur.execute(_CREATE_USAGE_LOG_TS_INDEX)
    logger.info("PostgreSQL tables initialised")

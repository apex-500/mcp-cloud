"""
API key management for MCP Cloud.

Uses PostgreSQL when DATABASE_URL is set, otherwise falls back to
local JSON file storage for development.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import is_pg_enabled, get_conn
from .pricing import TIERS, get_tier

logger = logging.getLogger("mcp_cloud.auth")

DATA_DIR = Path(os.getenv("MCP_CLOUD_DATA_DIR", "."))
KEYS_FILE = DATA_DIR / "keys.json"

_KEY_ALPHABET = string.ascii_letters + string.digits
_KEY_PREFIX = "mcp_live_"
_ADMIN_KEY_PREFIX = "mcp_admin_"


def _generate_key(prefix: str = _KEY_PREFIX) -> str:
    random_part = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(32))
    return f"{prefix}{random_part}"


# --------------------------------------------------------------------------- #
# JSON file helpers (fallback)
# --------------------------------------------------------------------------- #

def _load_keys() -> dict[str, dict[str, Any]]:
    if not KEYS_FILE.exists():
        return {}
    with open(KEYS_FILE, "r") as f:
        return json.load(f)


def _save_keys(keys: dict[str, dict[str, Any]]) -> None:
    KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(KEYS_FILE, "w") as f:
        json.dump(keys, f, indent=2, default=str)


# --------------------------------------------------------------------------- #
# Admin key (unchanged – always file/env based)
# --------------------------------------------------------------------------- #

def get_admin_key() -> str:
    """Return the admin key from env, or generate and persist one on first run."""
    env_key = os.getenv("ADMIN_KEY")
    if env_key:
        return env_key

    admin_file = DATA_DIR / ".admin_key"
    if admin_file.exists():
        return admin_file.read_text().strip()

    key = _generate_key(prefix=_ADMIN_KEY_PREFIX)
    admin_file.parent.mkdir(parents=True, exist_ok=True)
    admin_file.write_text(key)
    logger.warning("Generated new admin key - stored in %s", admin_file)
    logger.warning("Admin key: %s", key)
    return key


def is_admin_key(key: str) -> bool:
    return secrets.compare_digest(key, get_admin_key())


# --------------------------------------------------------------------------- #
# PostgreSQL helpers
# --------------------------------------------------------------------------- #

def _pg_row_to_record(row: tuple, cur) -> dict[str, Any]:
    """Convert a DB row to the same dict shape the JSON backend uses."""
    cols = [desc[0] for desc in cur.description]
    d = dict(zip(cols, row))
    # Normalise types to match JSON backend expectations
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    if d.get("last_used"):
        d["last_used"] = d["last_used"].isoformat()
    if d.get("calls_today_date"):
        d["calls_today_date"] = str(d["calls_today_date"])
    return d


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def create_api_key(email: str, tier: str = "free") -> dict[str, Any]:
    """Create a new API key for a user."""
    if tier not in TIERS:
        raise ValueError(f"Invalid tier: {tier}. Must be one of: {list(TIERS.keys())}")

    api_key = _generate_key()
    now = datetime.now(timezone.utc)

    if is_pg_enabled():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO api_keys
                        (api_key, email, tier, created_at, calls_today_date, calls_this_month_period)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (api_key, email, tier, now, now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")),
                )
                row = cur.fetchone()
                record = _pg_row_to_record(row, cur)
        logger.info("Created API key for %s (tier=%s) [pg]", email, tier)
        return {"api_key": api_key, **{k: v for k, v in record.items() if k != "api_key"}}

    # JSON fallback
    keys = _load_keys()
    record = {
        "email": email,
        "tier": tier,
        "created_at": now.isoformat(),
        "last_used": None,
        "calls_today": 0,
        "calls_today_date": now.strftime("%Y-%m-%d"),
        "calls_this_month": 0,
        "calls_this_month_period": now.strftime("%Y-%m"),
        "active": True,
    }
    keys[api_key] = record
    _save_keys(keys)
    logger.info("Created API key for %s (tier=%s)", email, tier)
    return {"api_key": api_key, **record}


def validate_api_key(api_key: str) -> dict[str, Any] | None:
    """Validate an API key and return its record, or None if invalid."""
    if is_pg_enabled():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM api_keys WHERE api_key = %s AND active = TRUE", (api_key,))
                row = cur.fetchone()
                if row is None:
                    return None
                return _pg_row_to_record(row, cur)

    keys = _load_keys()
    record = keys.get(api_key)
    if record is None or not record.get("active", True):
        return None
    return record


def get_key_info(api_key: str) -> dict[str, Any] | None:
    """Get full info for an API key, resetting counters if date rolled over."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")

    if is_pg_enabled():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM api_keys WHERE api_key = %s", (api_key,))
                row = cur.fetchone()
                if row is None:
                    return None
                record = _pg_row_to_record(row, cur)

                changed = False
                updates: dict[str, Any] = {}

                if str(record.get("calls_today_date")) != today:
                    updates["calls_today"] = 0
                    updates["calls_today_date"] = today
                    changed = True

                if record.get("calls_this_month_period") != this_month:
                    updates["calls_this_month"] = 0
                    updates["calls_this_month_period"] = this_month
                    changed = True

                if changed:
                    set_clauses = ", ".join(f"{k} = %s" for k in updates)
                    cur.execute(
                        f"UPDATE api_keys SET {set_clauses} WHERE api_key = %s",
                        (*updates.values(), api_key),
                    )
                    record.update(updates)

                return record

    # JSON fallback
    keys = _load_keys()
    record = keys.get(api_key)
    if record is None:
        return None

    changed = False

    if record.get("calls_today_date") != today:
        record["calls_today"] = 0
        record["calls_today_date"] = today
        changed = True

    if record.get("calls_this_month_period") != this_month:
        record["calls_this_month"] = 0
        record["calls_this_month_period"] = this_month
        changed = True

    if changed:
        keys[api_key] = record
        _save_keys(keys)

    return record


def check_rate_limit(api_key: str) -> tuple[bool, dict[str, Any]]:
    """Check if a key is within its rate limit. Returns (allowed, info)."""
    record = get_key_info(api_key)
    if record is None:
        return False, {"error": "Invalid API key"}

    tier = get_tier(record["tier"])
    calls_today = record["calls_today"]
    allowed = calls_today < tier.daily_limit

    info = {
        "calls_today": calls_today,
        "daily_limit": tier.daily_limit,
        "remaining": max(0, tier.daily_limit - calls_today),
        "tier": record["tier"],
    }

    return allowed, info


def increment_usage(api_key: str) -> None:
    """Increment the usage counters for an API key."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")

    if is_pg_enabled():
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Reset day/month counters if rolled over, then increment
                cur.execute(
                    """
                    UPDATE api_keys
                    SET calls_today = CASE WHEN calls_today_date = %s THEN calls_today + 1 ELSE 1 END,
                        calls_today_date = %s,
                        calls_this_month = CASE WHEN calls_this_month_period = %s THEN calls_this_month + 1 ELSE 1 END,
                        calls_this_month_period = %s,
                        last_used = %s
                    WHERE api_key = %s
                    """,
                    (today, today, this_month, this_month, now, api_key),
                )
        return

    # JSON fallback
    keys = _load_keys()
    record = keys.get(api_key)
    if record is None:
        return

    if record.get("calls_today_date") != today:
        record["calls_today"] = 0
        record["calls_today_date"] = today

    if record.get("calls_this_month_period") != this_month:
        record["calls_this_month"] = 0
        record["calls_this_month_period"] = this_month

    record["calls_today"] += 1
    record["calls_this_month"] += 1
    record["last_used"] = now.isoformat()

    keys[api_key] = record
    _save_keys(keys)

"""
Usage tracking and analytics for MCP Cloud.

Uses PostgreSQL when DATABASE_URL is set, otherwise falls back to
an append-only JSONL log file.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import is_pg_enabled, get_conn

logger = logging.getLogger("mcp_cloud.usage")

DATA_DIR = Path(os.getenv("MCP_CLOUD_DATA_DIR", "."))
USAGE_FILE = DATA_DIR / "usage.jsonl"


async def log_call(
    api_key: str,
    tool_name: str,
    latency_ms: float,
    success: bool,
    error: str | None = None,
) -> None:
    """Append a usage record to the log."""
    now = datetime.now(timezone.utc)

    if is_pg_enabled():
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO usage_log (api_key, tool_name, timestamp, latency_ms, success, error)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (api_key, tool_name, now, round(latency_ms, 2), success, error),
                    )
        except Exception:
            logger.exception("Failed to write usage log to PostgreSQL")
        return

    # JSON fallback
    entry = {
        "timestamp": now.isoformat(),
        "api_key": api_key,
        "tool_name": tool_name,
        "latency_ms": round(latency_ms, 2),
        "success": success,
    }
    if error:
        entry["error"] = error

    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(USAGE_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.exception("Failed to write usage log")


def get_usage_stats(api_key: str) -> dict[str, Any]:
    """Aggregate usage stats for a given API key."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")

    if is_pg_enabled():
        with get_conn() as conn:
            with conn.cursor() as cur:
                # Total calls & average latency
                cur.execute(
                    "SELECT COUNT(*), COALESCE(AVG(latency_ms), 0) FROM usage_log WHERE api_key = %s",
                    (api_key,),
                )
                total_calls, avg_latency = cur.fetchone()

                # Calls today & errors today
                cur.execute(
                    """
                    SELECT COUNT(*),
                           COUNT(*) FILTER (WHERE NOT success)
                    FROM usage_log
                    WHERE api_key = %s AND timestamp::date = %s
                    """,
                    (api_key, today),
                )
                calls_today, errors_today = cur.fetchone()

                # Calls this month
                cur.execute(
                    """
                    SELECT COUNT(*) FROM usage_log
                    WHERE api_key = %s AND TO_CHAR(timestamp, 'YYYY-MM') = %s
                    """,
                    (api_key, this_month),
                )
                calls_this_month = cur.fetchone()[0]

                # Top tools
                cur.execute(
                    """
                    SELECT tool_name, COUNT(*) AS cnt
                    FROM usage_log
                    WHERE api_key = %s
                    GROUP BY tool_name
                    ORDER BY cnt DESC
                    LIMIT 10
                    """,
                    (api_key,),
                )
                top_tools = {row[0]: row[1] for row in cur.fetchall()}

        return {
            "calls_today": calls_today,
            "calls_this_month": calls_this_month,
            "total_calls": total_calls,
            "errors_today": errors_today,
            "avg_latency_ms": round(float(avg_latency), 2),
            "top_tools": top_tools,
        }

    # JSON fallback
    calls_today = 0
    calls_this_month = 0
    total_calls = 0
    tool_counts: dict[str, int] = defaultdict(int)
    errors_today = 0
    total_latency_ms = 0.0

    if USAGE_FILE.exists():
        with open(USAGE_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("api_key") != api_key:
                    continue

                total_calls += 1
                total_latency_ms += entry.get("latency_ms", 0)
                tool_counts[entry.get("tool_name", "unknown")] += 1

                ts = entry.get("timestamp", "")
                if ts[:10] == today:
                    calls_today += 1
                    if not entry.get("success"):
                        errors_today += 1
                if ts[:7] == this_month:
                    calls_this_month += 1

    avg_latency = round(total_latency_ms / total_calls, 2) if total_calls else 0

    return {
        "calls_today": calls_today,
        "calls_this_month": calls_this_month,
        "total_calls": total_calls,
        "errors_today": errors_today,
        "avg_latency_ms": avg_latency,
        "top_tools": dict(
            sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
    }


def get_global_stats() -> dict[str, Any]:
    """Aggregate global usage stats (admin only)."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    this_month = now.strftime("%Y-%m")

    if is_pg_enabled():
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM usage_log")
                total_calls = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM usage_log WHERE timestamp::date = %s", (today,))
                calls_today = cur.fetchone()[0]

                cur.execute(
                    "SELECT COUNT(*) FROM usage_log WHERE TO_CHAR(timestamp, 'YYYY-MM') = %s",
                    (this_month,),
                )
                calls_this_month = cur.fetchone()[0]

                cur.execute("SELECT COUNT(DISTINCT api_key) FROM usage_log")
                unique_keys = cur.fetchone()[0]

                cur.execute(
                    """
                    SELECT tool_name, COUNT(*) AS cnt
                    FROM usage_log
                    GROUP BY tool_name
                    ORDER BY cnt DESC
                    LIMIT 10
                    """
                )
                top_tools = {row[0]: row[1] for row in cur.fetchall()}

        return {
            "calls_today": calls_today,
            "calls_this_month": calls_this_month,
            "total_calls": total_calls,
            "unique_api_keys": unique_keys,
            "top_tools": top_tools,
        }

    # JSON fallback
    calls_today = 0
    calls_this_month = 0
    total_calls = 0
    unique_keys_set: set[str] = set()
    tool_counts: dict[str, int] = defaultdict(int)

    if USAGE_FILE.exists():
        with open(USAGE_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                total_calls += 1
                unique_keys_set.add(entry.get("api_key", ""))
                tool_counts[entry.get("tool_name", "unknown")] += 1

                ts = entry.get("timestamp", "")
                if ts[:10] == today:
                    calls_today += 1
                if ts[:7] == this_month:
                    calls_this_month += 1

    return {
        "calls_today": calls_today,
        "calls_this_month": calls_this_month,
        "total_calls": total_calls,
        "unique_api_keys": len(unique_keys_set),
        "top_tools": dict(
            sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
    }

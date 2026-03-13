"""
Usage tracking and analytics for MCP Cloud.

Append-only JSONL log for all API calls, with aggregation helpers.
"""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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

    calls_today = 0
    calls_this_month = 0
    total_calls = 0
    unique_keys: set[str] = set()
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
                unique_keys.add(entry.get("api_key", ""))
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
        "unique_api_keys": len(unique_keys),
        "top_tools": dict(
            sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
    }

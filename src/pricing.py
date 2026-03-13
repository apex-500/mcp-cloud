"""
Pricing tiers and billing calculations for MCP Cloud.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Tier:
    name: str
    daily_limit: int
    monthly_price_cents: int  # in cents
    all_tools: bool
    priority: bool


TIERS: dict[str, Tier] = {
    "free": Tier(
        name="free",
        daily_limit=100,
        monthly_price_cents=0,
        all_tools=False,
        priority=False,
    ),
    "pro": Tier(
        name="pro",
        daily_limit=10_000,
        monthly_price_cents=2900,  # $29
        all_tools=True,
        priority=False,
    ),
    "business": Tier(
        name="business",
        daily_limit=100_000,
        monthly_price_cents=9900,  # $99
        all_tools=True,
        priority=True,
    ),
}

# Tools available on the free tier (subset)
FREE_TIER_TOOLS = {
    "crypto_price",
    "csv_to_json",
    "json_to_csv",
    "api_health_check",
    "current_time",
    "uuid_generate",
    "hash_text",
    "text_summarize",
    "math_calculate",
    "url_encode_decode",
    "regex_test",
    "json_validate",
    "dns_lookup",
    "compound_interest",
    "weather",
    "qr_generate",
    "translate_text",
}


def get_tier(tier_name: str) -> Tier:
    tier = TIERS.get(tier_name)
    if tier is None:
        raise ValueError(f"Unknown tier: {tier_name}")
    return tier


def is_tool_allowed(tier_name: str, tool_name: str) -> bool:
    tier = get_tier(tier_name)
    if tier.all_tools:
        return True
    return tool_name in FREE_TIER_TOOLS


def calculate_monthly_bill(
    tier_name: str,
    usage_log_path: Path,
    api_key: str,
    year: int | None = None,
    month: int | None = None,
) -> dict:
    """Calculate monthly bill for a given API key from usage logs."""
    now = datetime.now(timezone.utc)
    year = year or now.year
    month = month or now.month
    tier = get_tier(tier_name)

    total_calls = 0
    successful_calls = 0
    failed_calls = 0

    if usage_log_path.exists():
        with open(usage_log_path, "r") as f:
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
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts.year == year and ts.month == month:
                    total_calls += 1
                    if entry.get("success"):
                        successful_calls += 1
                    else:
                        failed_calls += 1

    return {
        "tier": tier_name,
        "period": f"{year}-{month:02d}",
        "total_calls": total_calls,
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "base_price_cents": tier.monthly_price_cents,
        "base_price_display": f"${tier.monthly_price_cents / 100:.2f}",
    }

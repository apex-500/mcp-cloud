"""
Tool registry for MCP Cloud.

Registers tool handlers with metadata (name, description, input schema).
Designed for easy extension -- add new tools by calling register_tool().
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import httpx

logger = logging.getLogger("mcp_cloud.tools")


@dataclass
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    category: str = "general"
    tags: list[str] = field(default_factory=list)


_registry: dict[str, ToolDef] = {}


def register_tool(
    name: str,
    description: str,
    input_schema: dict[str, Any],
    handler: Callable[..., Awaitable[Any]],
    category: str = "general",
    tags: list[str] | None = None,
) -> None:
    _registry[name] = ToolDef(
        name=name,
        description=description,
        input_schema=input_schema,
        handler=handler,
        category=category,
        tags=tags or [],
    )


def get_tool(name: str) -> ToolDef | None:
    return _registry.get(name)


def list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "inputSchema": t.input_schema,
            "category": t.category,
            "tags": t.tags,
        }
        for t in _registry.values()
    ]


# ---------------------------------------------------------------------------
# Built-in tool handlers
# ---------------------------------------------------------------------------

_http_client: httpx.AsyncClient | None = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30, follow_redirects=True)
    return _http_client


# -- Crypto tools -----------------------------------------------------------

async def crypto_price(symbol: str, currency: str = "usd") -> dict:
    """Get current price for a cryptocurrency."""
    client = await _get_http_client()
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": symbol.lower(), "vs_currencies": currency.lower()}
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    if symbol.lower() not in data:
        return {"error": f"Unknown symbol: {symbol}"}
    return {
        "symbol": symbol,
        "currency": currency,
        "price": data[symbol.lower()][currency.lower()],
    }


async def crypto_prices_batch(symbols: list[str], currency: str = "usd") -> dict:
    """Get prices for multiple cryptocurrencies at once."""
    client = await _get_http_client()
    ids = ",".join(s.lower() for s in symbols)
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": ids, "vs_currencies": currency.lower()}
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    results = {}
    for s in symbols:
        key = s.lower()
        if key in data:
            results[s] = {"currency": currency, "price": data[key][currency.lower()]}
        else:
            results[s] = {"error": f"Unknown symbol: {s}"}
    return {"prices": results}


async def trending_tokens() -> dict:
    """Get trending cryptocurrency tokens from CoinGecko."""
    client = await _get_http_client()
    url = "https://api.coingecko.com/api/v3/search/trending"
    resp = await client.get(url)
    resp.raise_for_status()
    data = resp.json()
    coins = []
    for item in data.get("coins", [])[:10]:
        coin = item.get("item", {})
        coins.append({
            "name": coin.get("name"),
            "symbol": coin.get("symbol"),
            "market_cap_rank": coin.get("market_cap_rank"),
            "price_btc": coin.get("price_btc"),
        })
    return {"trending": coins}


# -- API health tools -------------------------------------------------------

async def api_health_check(url: str, expected_status: int = 200) -> dict:
    """Check if an API endpoint is healthy."""
    client = await _get_http_client()
    try:
        resp = await client.get(url)
        return {
            "url": url,
            "status_code": resp.status_code,
            "healthy": resp.status_code == expected_status,
            "response_time_ms": resp.elapsed.total_seconds() * 1000,
        }
    except httpx.HTTPError as exc:
        return {"url": url, "healthy": False, "error": str(exc)}


async def http_request(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    body: str | None = None,
) -> dict:
    """Make an HTTP request and return the response."""
    client = await _get_http_client()
    try:
        resp = await client.request(
            method.upper(),
            url,
            headers=headers,
            content=body,
        )
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            response_body = resp.json()
        else:
            response_body = resp.text[:10_000]  # cap text responses
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": response_body,
            "response_time_ms": resp.elapsed.total_seconds() * 1000,
        }
    except httpx.HTTPError as exc:
        return {"error": str(exc)}


# -- File conversion tools --------------------------------------------------

async def csv_to_json(csv_text: str) -> dict:
    """Convert CSV text to JSON."""
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    return {"rows": rows, "count": len(rows)}


async def json_to_csv(json_data: list[dict]) -> dict:
    """Convert a list of JSON objects to CSV text."""
    if not json_data:
        return {"csv": "", "count": 0}
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=json_data[0].keys())
    writer.writeheader()
    writer.writerows(json_data)
    return {"csv": output.getvalue(), "count": len(json_data)}


async def markdown_to_html(markdown_text: str) -> dict:
    """Convert Markdown to basic HTML (lightweight, no external deps)."""
    import re

    html = markdown_text
    # Headers
    for level in range(6, 0, -1):
        pattern = r"^" + r"#" * level + r"\s+(.+)$"
        tag = f"h{level}"
        html = re.sub(pattern, rf"<{tag}>\1</{tag}>", html, flags=re.MULTILINE)
    # Bold / italic
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*", r"<em>\1</em>", html)
    # Code blocks
    html = re.sub(r"```(\w*)\n(.*?)```", r"<pre><code>\2</code></pre>", html, flags=re.DOTALL)
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    # Line breaks -> paragraphs
    paragraphs = re.split(r"\n{2,}", html)
    html = "".join(
        f"<p>{p.strip()}</p>" if not p.strip().startswith("<h") and not p.strip().startswith("<pre") else p.strip()
        for p in paragraphs
        if p.strip()
    )
    return {"html": html}


# ---------------------------------------------------------------------------
# Register all built-in tools
# ---------------------------------------------------------------------------

def register_builtins() -> None:
    """Register all built-in tools. Called once at startup."""

    register_tool(
        name="crypto_price",
        description="Get the current price of a cryptocurrency",
        input_schema={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Coin ID (e.g. bitcoin, ethereum)"},
                "currency": {"type": "string", "default": "usd", "description": "Fiat currency code"},
            },
            "required": ["symbol"],
        },
        handler=crypto_price,
        category="crypto",
        tags=["price", "market"],
    )

    register_tool(
        name="crypto_prices_batch",
        description="Get prices for multiple cryptocurrencies at once",
        input_schema={
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of coin IDs",
                },
                "currency": {"type": "string", "default": "usd"},
            },
            "required": ["symbols"],
        },
        handler=crypto_prices_batch,
        category="crypto",
        tags=["price", "market", "batch"],
    )

    register_tool(
        name="trending_tokens",
        description="Get trending cryptocurrency tokens",
        input_schema={"type": "object", "properties": {}},
        handler=trending_tokens,
        category="crypto",
        tags=["trending", "market"],
    )

    register_tool(
        name="api_health_check",
        description="Check if an API endpoint is healthy and responding",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to check"},
                "expected_status": {"type": "integer", "default": 200},
            },
            "required": ["url"],
        },
        handler=api_health_check,
        category="monitoring",
        tags=["health", "api"],
    )

    register_tool(
        name="http_request",
        description="Make an HTTP request and return the response",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "method": {"type": "string", "default": "GET"},
                "headers": {"type": "object", "default": {}},
                "body": {"type": "string", "default": None},
            },
            "required": ["url"],
        },
        handler=http_request,
        category="monitoring",
        tags=["http", "api"],
    )

    register_tool(
        name="csv_to_json",
        description="Convert CSV text to a JSON array of objects",
        input_schema={
            "type": "object",
            "properties": {
                "csv_text": {"type": "string", "description": "CSV content"},
            },
            "required": ["csv_text"],
        },
        handler=csv_to_json,
        category="conversion",
        tags=["csv", "json", "convert"],
    )

    register_tool(
        name="json_to_csv",
        description="Convert a JSON array of objects to CSV text",
        input_schema={
            "type": "object",
            "properties": {
                "json_data": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Array of JSON objects",
                },
            },
            "required": ["json_data"],
        },
        handler=json_to_csv,
        category="conversion",
        tags=["csv", "json", "convert"],
    )

    register_tool(
        name="markdown_to_html",
        description="Convert Markdown text to HTML",
        input_schema={
            "type": "object",
            "properties": {
                "markdown_text": {"type": "string", "description": "Markdown content"},
            },
            "required": ["markdown_text"],
        },
        handler=markdown_to_html,
        category="conversion",
        tags=["markdown", "html", "convert"],
    )

    logger.info("Registered %d built-in tools", len(_registry))

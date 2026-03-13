"""
Tool registry for MCP Cloud.

Registers tool handlers with metadata (name, description, input schema).
Designed for easy extension -- add new tools by calling register_tool().
"""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import logging
import math
import operator
import re
import socket
import time
import urllib.parse
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Awaitable
from zoneinfo import ZoneInfo

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


# -- Simple in-memory cache for CoinGecko rate-limit avoidance --------------

_cache: dict[str, tuple[float, Any]] = {}  # key -> (expiry_ts, value)
_CACHE_TTL = 60  # seconds


def _cache_get(key: str) -> Any | None:
    """Return cached value if present and not expired, else None."""
    entry = _cache.get(key)
    if entry is None:
        return None
    expiry, value = entry
    if time.monotonic() > expiry:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic() + _CACHE_TTL, value)


# -- Crypto tools -----------------------------------------------------------

_COINPAPRIKA_MAP = {
    "bitcoin": "btc-bitcoin", "btc": "btc-bitcoin",
    "ethereum": "eth-ethereum", "eth": "eth-ethereum",
    "solana": "sol-solana", "sol": "sol-solana",
    "dogecoin": "doge-dogecoin", "doge": "doge-dogecoin",
    "cardano": "ada-cardano", "ada": "ada-cardano",
    "ripple": "xrp-xrp", "xrp": "xrp-xrp",
    "polkadot": "dot-polkadot", "dot": "dot-polkadot",
    "avalanche": "avax-avalanche", "avax": "avax-avalanche",
    "chainlink": "link-chainlink", "link": "link-chainlink",
    "polygon": "matic-polygon", "matic": "matic-polygon",
    "uniswap": "uni-uniswap", "uni": "uni-uniswap",
    "litecoin": "ltc-litecoin", "ltc": "ltc-litecoin",
    "tron": "trx-tron", "trx": "trx-tron",
    "binancecoin": "bnb-binance-coin", "bnb": "bnb-binance-coin",
    "shiba-inu": "shib-shiba-inu", "shib": "shib-shiba-inu",
}


async def _fetch_price_coinpaprika(symbol: str, currency: str) -> dict | None:
    """Fallback price source using CoinPaprika API (generous limits, no key)."""
    client = await _get_http_client()
    coin_id = _COINPAPRIKA_MAP.get(symbol.lower())
    if not coin_id:
        return None
    try:
        resp = await client.get(f"https://api.coinpaprika.com/v1/tickers/{coin_id}")
        if resp.status_code != 200:
            return None
        data = resp.json()
        quotes = data.get("quotes", {}).get("USD", {})
        price = quotes.get("price")
        if price is None:
            return None
        return {
            "symbol": symbol,
            "currency": currency,
            "price": round(price, 2),
            "source": "coinpaprika",
            "name": data.get("name"),
            "rank": data.get("rank"),
            "change_24h": quotes.get("percent_change_24h"),
            "market_cap": quotes.get("market_cap"),
            "volume_24h": quotes.get("volume_24h"),
        }
    except Exception:
        return None


async def crypto_price(symbol: str, currency: str = "usd") -> dict:
    """Get current price for a cryptocurrency."""
    cache_key = f"crypto_price:{symbol.lower()}:{currency.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    client = await _get_http_client()
    # Try CoinGecko first
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {"ids": symbol.lower(), "vs_currencies": currency.lower()}
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if symbol.lower() in data:
            result = {
                "symbol": symbol,
                "currency": currency,
                "price": data[symbol.lower()][currency.lower()],
                "source": "coingecko",
            }
            _cache_set(cache_key, result)
            return result
    except Exception:
        pass
    # Fallback to CoinPaprika
    result = await _fetch_price_coinpaprika(symbol, currency)
    if result:
        _cache_set(cache_key, result)
        return result
    return {"error": f"Unknown symbol: {symbol}"}


async def crypto_prices_batch(symbols: list[str], currency: str = "usd") -> dict:
    """Get prices for multiple cryptocurrencies at once."""
    sorted_ids = sorted(s.lower() for s in symbols)
    cache_key = f"crypto_batch:{','.join(sorted_ids)}:{currency.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
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
    result = {"prices": results}
    _cache_set(cache_key, result)
    return result


async def trending_tokens() -> dict:
    """Get trending cryptocurrency tokens from CoinGecko."""
    cache_key = "trending_tokens"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
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
    result = {"trending": coins}
    _cache_set(cache_key, result)
    return result


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


# -- Web/Search tools -------------------------------------------------------

async def web_search(query: str, max_results: int = 10) -> dict:
    """Search the web using DuckDuckGo HTML search."""
    client = await _get_http_client()
    try:
        resp = await client.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; MCPCloud/1.0)"},
        )
        resp.raise_for_status()
        html = resp.text

        # Parse results using regex (no BS4 needed)
        results = []
        # DuckDuckGo HTML wraps each result in <div class="result ...">
        result_blocks = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]*)"[^>]*>(.*?)</a>.*?'
            r'<a class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )
        for href, title, snippet in result_blocks[:max_results]:
            # Decode DuckDuckGo redirect URLs
            parsed = urllib.parse.urlparse(href)
            params = urllib.parse.parse_qs(parsed.query)
            actual_url = params.get("uddg", [href])[0]
            # Strip HTML tags from title and snippet
            clean_title = re.sub(r"<[^>]+>", "", title).strip()
            clean_snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            results.append({
                "title": clean_title,
                "url": actual_url,
                "snippet": clean_snippet,
            })

        return {"query": query, "results": results, "count": len(results)}
    except Exception as exc:
        return {"query": query, "error": str(exc), "results": []}


async def url_fetch(url: str, max_length: int = 20000) -> dict:
    """Fetch a URL and return clean text content (HTML tags stripped)."""
    client = await _get_http_client()
    try:
        resp = await client.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MCPCloud/1.0)"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        text = resp.text

        if "html" in content_type:
            # Remove script and style blocks
            text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
            # Remove HTML tags
            text = re.sub(r"<[^>]+>", " ", text)
            # Collapse whitespace
            text = re.sub(r"\s+", " ", text).strip()
            # Decode HTML entities
            text = text.replace("&amp;", "&").replace("&lt;", "<").replace(
                "&gt;", ">"
            ).replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")

        truncated = len(text) > max_length
        text = text[:max_length]
        return {
            "url": url,
            "content_type": content_type,
            "text": text,
            "length": len(text),
            "truncated": truncated,
        }
    except Exception as exc:
        return {"url": url, "error": str(exc)}


# -- Time/Date tools --------------------------------------------------------

_COMMON_ZONES = {
    "UTC": "UTC",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "Europe/London": "Europe/London",
    "Europe/Berlin": "Europe/Berlin",
    "Europe/Paris": "Europe/Paris",
    "Asia/Tokyo": "Asia/Tokyo",
    "Asia/Shanghai": "Asia/Shanghai",
    "Asia/Kolkata": "Asia/Kolkata",
    "Asia/Dubai": "Asia/Dubai",
    "Australia/Sydney": "Australia/Sydney",
}


async def current_time() -> dict:
    """Return current UTC time and times in common timezones."""
    now = datetime.now(timezone.utc)
    times = {}
    for label, tz_name in _COMMON_ZONES.items():
        zone = ZoneInfo(tz_name)
        local = now.astimezone(zone)
        times[label] = local.isoformat()
    return {
        "utc": now.isoformat(),
        "unix_timestamp": int(now.timestamp()),
        "timezones": times,
    }


async def timezone_convert(
    time_str: str, from_tz: str, to_tz: str
) -> dict:
    """Convert a time between timezones."""
    try:
        from_zone = ZoneInfo(from_tz)
        to_zone = ZoneInfo(to_tz)
    except KeyError as exc:
        return {"error": f"Unknown timezone: {exc}"}

    try:
        # Try parsing common formats
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
            try:
                dt = datetime.strptime(time_str, fmt)
                break
            except ValueError:
                continue
        else:
            return {"error": f"Could not parse time: {time_str}. Use format YYYY-MM-DD HH:MM:SS or HH:MM"}
        # If only time provided, use today's date
        if dt.year == 1900:
            today = datetime.now(timezone.utc).date()
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        dt = dt.replace(tzinfo=from_zone)
        converted = dt.astimezone(to_zone)
        return {
            "original": dt.isoformat(),
            "converted": converted.isoformat(),
            "from_timezone": from_tz,
            "to_timezone": to_tz,
        }
    except Exception as exc:
        return {"error": str(exc)}


# -- Text/Data processing tools ---------------------------------------------

async def text_summarize(text: str) -> dict:
    """Analyze text: word count, character count, sentence count, reading time, top words."""
    words = text.split()
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    # Frequency analysis - filter out short/common words
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
                  "have", "has", "had", "do", "does", "did", "will", "would", "could",
                  "should", "may", "might", "shall", "can", "to", "of", "in", "for",
                  "on", "with", "at", "by", "from", "as", "into", "through", "during",
                  "before", "after", "and", "but", "or", "nor", "not", "so", "yet",
                  "it", "its", "this", "that", "these", "those", "i", "you", "he", "she",
                  "we", "they", "me", "him", "her", "us", "them", "my", "your", "his"}
    filtered = [w.lower().strip(".,!?;:\"'()[]{}") for w in words]
    filtered = [w for w in filtered if len(w) > 2 and w not in stop_words]
    top_words = Counter(filtered).most_common(10)
    reading_time_min = max(1, round(len(words) / 238))  # avg adult reading speed

    return {
        "character_count": len(text),
        "word_count": len(words),
        "sentence_count": len(sentences),
        "paragraph_count": len([p for p in text.split("\n\n") if p.strip()]),
        "reading_time_minutes": reading_time_min,
        "top_words": [{"word": w, "count": c} for w, c in top_words],
    }


async def json_validate(json_string: str, schema: dict | None = None) -> dict:
    """Validate a JSON string, optionally against a schema."""
    try:
        parsed = json.loads(json_string)
    except json.JSONDecodeError as exc:
        return {"valid": False, "error": str(exc), "line": exc.lineno, "column": exc.colno}

    result: dict[str, Any] = {"valid": True, "type": type(parsed).__name__}

    if schema:
        # Basic schema validation (type checking, required fields)
        errors = []
        if "type" in schema:
            expected = schema["type"]
            type_map = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int, "boolean": bool}
            expected_type = type_map.get(expected)
            if expected_type and not isinstance(parsed, expected_type):
                errors.append(f"Expected type '{expected}', got '{type(parsed).__name__}'")
        if isinstance(parsed, dict) and "required" in schema:
            missing = [f for f in schema["required"] if f not in parsed]
            if missing:
                errors.append(f"Missing required fields: {missing}")
        if isinstance(parsed, dict) and "properties" in schema:
            for prop, prop_schema in schema["properties"].items():
                if prop in parsed and "type" in prop_schema:
                    exp = prop_schema["type"]
                    type_map = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list, "object": dict}
                    exp_type = type_map.get(exp)
                    if exp_type and not isinstance(parsed[prop], exp_type):
                        errors.append(f"Field '{prop}': expected '{exp}', got '{type(parsed[prop]).__name__}'")
        if errors:
            result["valid"] = False
            result["schema_errors"] = errors
    return result


async def regex_test(pattern: str, text: str, flags: str = "") -> dict:
    """Test a regex pattern against text, return all matches."""
    try:
        re_flags = 0
        for f in flags.upper():
            if f == "I":
                re_flags |= re.IGNORECASE
            elif f == "M":
                re_flags |= re.MULTILINE
            elif f == "S":
                re_flags |= re.DOTALL
        compiled = re.compile(pattern, re_flags)
        matches = []
        for m in compiled.finditer(text):
            match_info: dict[str, Any] = {
                "match": m.group(),
                "start": m.start(),
                "end": m.end(),
            }
            if m.groups():
                match_info["groups"] = list(m.groups())
            if m.groupdict():
                match_info["named_groups"] = m.groupdict()
            matches.append(match_info)
        return {"pattern": pattern, "matches": matches, "count": len(matches)}
    except re.error as exc:
        return {"pattern": pattern, "error": str(exc)}


async def hash_text(text: str, algorithms: list[str] | None = None) -> dict:
    """Generate cryptographic hashes of text."""
    if algorithms is None:
        algorithms = ["md5", "sha1", "sha256"]
    data = text.encode("utf-8")
    hashes = {}
    for algo in algorithms:
        algo_lower = algo.lower().replace("-", "")
        if algo_lower == "md5":
            hashes["md5"] = hashlib.md5(data).hexdigest()
        elif algo_lower == "sha1":
            hashes["sha1"] = hashlib.sha1(data).hexdigest()
        elif algo_lower == "sha256":
            hashes["sha256"] = hashlib.sha256(data).hexdigest()
        elif algo_lower == "sha512":
            hashes["sha512"] = hashlib.sha512(data).hexdigest()
        else:
            hashes[algo] = f"unsupported algorithm: {algo}"
    return {"hashes": hashes, "input_length": len(data)}


async def url_encode_decode(text: str, action: str = "encode") -> dict:
    """URL encode or decode text."""
    if action == "encode":
        result = urllib.parse.quote(text, safe="")
    elif action == "decode":
        result = urllib.parse.unquote(text)
    else:
        return {"error": f"Unknown action: {action}. Use 'encode' or 'decode'."}
    return {"input": text, "action": action, "result": result}


async def uuid_generate(count: int = 1, version: int = 4) -> dict:
    """Generate one or more UUID v4 values."""
    if count < 1:
        count = 1
    if count > 100:
        count = 100
    uuids = [str(uuid.uuid4()) for _ in range(count)]
    if count == 1:
        return {"uuid": uuids[0]}
    return {"uuids": uuids, "count": count}


# -- Math/Finance tools -----------------------------------------------------

# Safe math evaluator using AST
_SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

_SAFE_FUNCTIONS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "log2": math.log2,
    "ceil": math.ceil, "floor": math.floor, "pi": math.pi, "e": math.e,
}


def _safe_eval_node(node: ast.AST) -> float:
    """Recursively evaluate an AST node safely."""
    if isinstance(node, ast.Expression):
        return _safe_eval_node(node.body)
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"Unsupported constant: {node.value}")
    elif isinstance(node, ast.BinOp):
        op_func = _SAFE_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        # Prevent huge exponents
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("Exponent too large (max 1000)")
        return op_func(left, right)
    elif isinstance(node, ast.UnaryOp):
        op_func = _SAFE_OPERATORS.get(type(node.op))
        if op_func is None:
            raise ValueError(f"Unsupported unary operator: {type(node.op).__name__}")
        return op_func(_safe_eval_node(node.operand))
    elif isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in _SAFE_FUNCTIONS:
                val = _SAFE_FUNCTIONS[func_name]
                if callable(val):
                    args = [_safe_eval_node(a) for a in node.args]
                    return val(*args)
                return val  # constant like pi
            raise ValueError(f"Unknown function: {func_name}")
        raise ValueError("Unsupported function call")
    elif isinstance(node, ast.Name):
        if node.id in _SAFE_FUNCTIONS:
            val = _SAFE_FUNCTIONS[node.id]
            if not callable(val):
                return val  # constants like pi, e
        raise ValueError(f"Unknown variable: {node.id}")
    else:
        raise ValueError(f"Unsupported expression: {type(node).__name__}")


async def math_calculate(expression: str) -> dict:
    """Safely evaluate a mathematical expression."""
    try:
        tree = ast.parse(expression, mode="eval")
        result = _safe_eval_node(tree)
        return {"expression": expression, "result": result}
    except (ValueError, TypeError, ZeroDivisionError, OverflowError, SyntaxError) as exc:
        return {"expression": expression, "error": str(exc)}


async def currency_convert(
    amount: float, from_currency: str, to_currency: str
) -> dict:
    """Convert between fiat currencies using frankfurter.app (free, no key)."""
    cache_key = f"currency:{from_currency.upper()}:{to_currency.upper()}"
    cached = _cache_get(cache_key)
    rate = None
    if cached is not None:
        rate = cached

    if rate is None:
        client = await _get_http_client()
        try:
            resp = await client.get(
                "https://api.frankfurter.app/latest",
                params={"from": from_currency.upper(), "to": to_currency.upper()},
            )
            resp.raise_for_status()
            data = resp.json()
            rate = data.get("rates", {}).get(to_currency.upper())
            if rate is None:
                return {"error": f"Could not get rate for {from_currency} -> {to_currency}"}
            _cache_set(cache_key, rate)
        except Exception as exc:
            return {"error": str(exc)}

    converted = round(amount * rate, 2)
    return {
        "amount": amount,
        "from": from_currency.upper(),
        "to": to_currency.upper(),
        "rate": rate,
        "converted": converted,
    }


async def compound_interest(
    principal: float,
    annual_rate: float,
    years: float,
    compounds_per_year: int = 12,
    monthly_contribution: float = 0,
) -> dict:
    """Calculate compound interest with optional monthly contributions."""
    r = annual_rate / 100
    n = compounds_per_year
    t = years

    # Base compound interest: A = P(1 + r/n)^(nt)
    amount = principal * (1 + r / n) ** (n * t)

    # Future value of series (monthly contributions)
    if monthly_contribution > 0 and n > 0:
        # Convert to per-period contribution
        periods = n * t
        rate_per_period = r / n
        if rate_per_period > 0:
            fv_series = monthly_contribution * (((1 + rate_per_period) ** periods - 1) / rate_per_period)
        else:
            fv_series = monthly_contribution * periods
        amount += fv_series

    total_contributions = principal + (monthly_contribution * 12 * t)
    interest_earned = amount - total_contributions

    return {
        "principal": principal,
        "annual_rate_percent": annual_rate,
        "years": years,
        "compounds_per_year": n,
        "monthly_contribution": monthly_contribution,
        "final_amount": round(amount, 2),
        "total_contributions": round(total_contributions, 2),
        "interest_earned": round(interest_earned, 2),
    }


# -- Network/DNS tools -----------------------------------------------------

async def dns_lookup(hostname: str, record_type: str = "A") -> dict:
    """Perform DNS lookup for a hostname."""
    try:
        results = []
        if record_type.upper() in ("A", "AAAA"):
            family = socket.AF_INET if record_type.upper() == "A" else socket.AF_INET6
            try:
                infos = socket.getaddrinfo(hostname, None, family, socket.SOCK_STREAM)
                seen = set()
                for info in infos:
                    addr = info[4][0]
                    if addr not in seen:
                        seen.add(addr)
                        results.append({"type": record_type.upper(), "address": addr})
            except socket.gaierror:
                # Try both families
                pass
        if not results:
            # Fallback: resolve any address
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            seen = set()
            for info in infos:
                addr = info[4][0]
                family_name = "A" if info[0] == socket.AF_INET else "AAAA"
                if addr not in seen:
                    seen.add(addr)
                    results.append({"type": family_name, "address": addr})
        return {"hostname": hostname, "records": results, "count": len(results)}
    except socket.gaierror as exc:
        return {"hostname": hostname, "error": str(exc)}


async def ip_geolocation(ip: str) -> dict:
    """Get geolocation info for an IP address using ip-api.com (free, no key)."""
    cache_key = f"geoip:{ip}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    client = await _get_http_client()
    try:
        resp = await client.get(f"http://ip-api.com/json/{ip}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "fail":
            return {"ip": ip, "error": data.get("message", "Lookup failed")}
        result = {
            "ip": ip,
            "country": data.get("country"),
            "country_code": data.get("countryCode"),
            "region": data.get("regionName"),
            "city": data.get("city"),
            "zip": data.get("zip"),
            "lat": data.get("lat"),
            "lon": data.get("lon"),
            "timezone": data.get("timezone"),
            "isp": data.get("isp"),
            "org": data.get("org"),
        }
        _cache_set(cache_key, result)
        return result
    except Exception as exc:
        return {"ip": ip, "error": str(exc)}


# -- Crypto (expanded) tools -----------------------------------------------

async def defi_yields(chain: str | None = None, min_tvl: float = 1_000_000, limit: int = 20) -> dict:
    """Get top DeFi yields from DeFiLlama API."""
    cache_key = f"defi_yields:{chain}:{min_tvl}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    client = await _get_http_client()
    try:
        resp = await client.get("https://yields.llama.fi/pools")
        resp.raise_for_status()
        data = resp.json()
        pools = data.get("data", [])

        # Filter
        filtered = []
        for p in pools:
            tvl = p.get("tvlUsd", 0) or 0
            if tvl < min_tvl:
                continue
            if chain and p.get("chain", "").lower() != chain.lower():
                continue
            apy = p.get("apy", 0) or 0
            if apy <= 0 or apy > 10000:  # filter unreasonable APYs
                continue
            filtered.append({
                "pool": p.get("pool"),
                "project": p.get("project"),
                "chain": p.get("chain"),
                "symbol": p.get("symbol"),
                "tvl_usd": round(tvl, 2),
                "apy": round(apy, 2),
                "apy_base": round(p.get("apyBase", 0) or 0, 2),
                "apy_reward": round(p.get("apyReward", 0) or 0, 2),
                "stablecoin": p.get("stablecoin", False),
            })

        # Sort by APY descending
        filtered.sort(key=lambda x: x["apy"], reverse=True)
        filtered = filtered[:limit]

        result = {"pools": filtered, "count": len(filtered), "filter_chain": chain, "min_tvl": min_tvl}
        _cache_set(cache_key, result)
        return result
    except Exception as exc:
        return {"error": str(exc)}


async def gas_prices(chain: str = "ethereum") -> dict:
    """Get current gas prices for EVM chains using public RPC endpoints."""
    rpc_urls = {
        "ethereum": "https://ethereum-rpc.publicnode.com",
        "polygon": "https://polygon-rpc.com",
        "arbitrum": "https://arb1.arbitrum.io/rpc",
        "optimism": "https://mainnet.optimism.io",
        "base": "https://mainnet.base.org",
        "bsc": "https://bsc-dataseed.binance.org",
        "avalanche": "https://api.avax.network/ext/bc/C/rpc",
    }
    chain_lower = chain.lower()
    rpc_url = rpc_urls.get(chain_lower)
    if not rpc_url:
        return {"error": f"Unsupported chain: {chain}. Supported: {', '.join(rpc_urls.keys())}"}

    cache_key = f"gas:{chain_lower}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = await _get_http_client()
    try:
        resp = await client.post(
            rpc_url,
            json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1},
        )
        resp.raise_for_status()
        data = resp.json()
        gas_hex = data.get("result", "0x0")
        gas_wei = int(gas_hex, 16)
        gas_gwei = gas_wei / 1e9
        result = {
            "chain": chain,
            "gas_price_wei": gas_wei,
            "gas_price_gwei": round(gas_gwei, 4),
            "estimated_transfer_cost_usd": None,  # would need ETH price
        }
        _cache_set(cache_key, result)
        return result
    except Exception as exc:
        return {"chain": chain, "error": str(exc)}


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

    # -- Web/Search tools ---

    register_tool(
        name="web_search",
        description="Search the web using DuckDuckGo and return titles, URLs, and snippets",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {"type": "integer", "default": 10, "description": "Max results to return (1-20)"},
            },
            "required": ["query"],
        },
        handler=web_search,
        category="web",
        tags=["search", "web"],
    )

    register_tool(
        name="url_fetch",
        description="Fetch a URL and return clean text content (HTML tags stripped)",
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "max_length": {"type": "integer", "default": 20000, "description": "Max characters to return"},
            },
            "required": ["url"],
        },
        handler=url_fetch,
        category="web",
        tags=["fetch", "web", "scrape"],
    )

    # -- Time/Date tools ---

    register_tool(
        name="current_time",
        description="Get the current time in UTC and common timezones (US, EU, Asia)",
        input_schema={"type": "object", "properties": {}},
        handler=current_time,
        category="time",
        tags=["time", "date", "timezone"],
    )

    register_tool(
        name="timezone_convert",
        description="Convert a time from one timezone to another",
        input_schema={
            "type": "object",
            "properties": {
                "time_str": {"type": "string", "description": "Time string (e.g. '2024-01-15 14:30:00' or '14:30')"},
                "from_tz": {"type": "string", "description": "Source timezone (e.g. 'America/New_York', 'UTC')"},
                "to_tz": {"type": "string", "description": "Target timezone (e.g. 'Asia/Tokyo')"},
            },
            "required": ["time_str", "from_tz", "to_tz"],
        },
        handler=timezone_convert,
        category="time",
        tags=["time", "timezone", "convert"],
    )

    # -- Text/Data processing tools ---

    register_tool(
        name="text_summarize",
        description="Analyze text: word count, character count, sentence count, reading time, and top word frequency",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to analyze"},
            },
            "required": ["text"],
        },
        handler=text_summarize,
        category="text",
        tags=["text", "analysis", "statistics"],
    )

    register_tool(
        name="json_validate",
        description="Validate a JSON string, optionally against a JSON schema (checks types and required fields)",
        input_schema={
            "type": "object",
            "properties": {
                "json_string": {"type": "string", "description": "JSON string to validate"},
                "schema": {"type": "object", "description": "Optional JSON schema to validate against"},
            },
            "required": ["json_string"],
        },
        handler=json_validate,
        category="text",
        tags=["json", "validate", "schema"],
    )

    register_tool(
        name="regex_test",
        description="Test a regular expression pattern against text, returning all matches with positions and groups",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "text": {"type": "string", "description": "Text to search"},
                "flags": {"type": "string", "default": "", "description": "Regex flags: I=ignorecase, M=multiline, S=dotall"},
            },
            "required": ["pattern", "text"],
        },
        handler=regex_test,
        category="text",
        tags=["regex", "pattern", "search"],
    )

    register_tool(
        name="hash_text",
        description="Generate MD5, SHA-1, SHA-256 (and optionally SHA-512) hashes of text",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to hash"},
                "algorithms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["md5", "sha1", "sha256"],
                    "description": "Hash algorithms to use",
                },
            },
            "required": ["text"],
        },
        handler=hash_text,
        category="text",
        tags=["hash", "crypto", "checksum"],
    )

    register_tool(
        name="url_encode_decode",
        description="URL encode or decode a text string",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to encode or decode"},
                "action": {"type": "string", "enum": ["encode", "decode"], "default": "encode"},
            },
            "required": ["text"],
        },
        handler=url_encode_decode,
        category="text",
        tags=["url", "encode", "decode"],
    )

    register_tool(
        name="uuid_generate",
        description="Generate one or more UUID v4 values",
        input_schema={
            "type": "object",
            "properties": {
                "count": {"type": "integer", "default": 1, "description": "Number of UUIDs to generate (max 100)"},
            },
        },
        handler=uuid_generate,
        category="text",
        tags=["uuid", "generate", "id"],
    )

    # -- Math/Finance tools ---

    register_tool(
        name="math_calculate",
        description="Safely evaluate a mathematical expression (supports +, -, *, /, **, sqrt, sin, cos, log, pi, etc.)",
        input_schema={
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "Math expression (e.g. '2**10', 'sqrt(144)', 'sin(pi/2)')"},
            },
            "required": ["expression"],
        },
        handler=math_calculate,
        category="math",
        tags=["math", "calculate", "expression"],
    )

    register_tool(
        name="currency_convert",
        description="Convert between fiat currencies using live exchange rates (frankfurter.app)",
        input_schema={
            "type": "object",
            "properties": {
                "amount": {"type": "number", "description": "Amount to convert"},
                "from_currency": {"type": "string", "description": "Source currency code (e.g. USD, EUR, GBP)"},
                "to_currency": {"type": "string", "description": "Target currency code (e.g. EUR, JPY)"},
            },
            "required": ["amount", "from_currency", "to_currency"],
        },
        handler=currency_convert,
        category="finance",
        tags=["currency", "exchange", "convert"],
    )

    register_tool(
        name="compound_interest",
        description="Calculate compound interest with optional monthly contributions",
        input_schema={
            "type": "object",
            "properties": {
                "principal": {"type": "number", "description": "Initial investment amount"},
                "annual_rate": {"type": "number", "description": "Annual interest rate as percentage (e.g. 7 for 7%)"},
                "years": {"type": "number", "description": "Investment period in years"},
                "compounds_per_year": {"type": "integer", "default": 12, "description": "Compounding frequency per year"},
                "monthly_contribution": {"type": "number", "default": 0, "description": "Monthly contribution amount"},
            },
            "required": ["principal", "annual_rate", "years"],
        },
        handler=compound_interest,
        category="finance",
        tags=["interest", "investment", "finance"],
    )

    # -- Network/DNS tools ---

    register_tool(
        name="dns_lookup",
        description="Perform DNS lookup for a hostname, returning IP addresses",
        input_schema={
            "type": "object",
            "properties": {
                "hostname": {"type": "string", "description": "Hostname to look up (e.g. example.com)"},
                "record_type": {"type": "string", "default": "A", "description": "Record type: A or AAAA"},
            },
            "required": ["hostname"],
        },
        handler=dns_lookup,
        category="network",
        tags=["dns", "network", "lookup"],
    )

    register_tool(
        name="ip_geolocation",
        description="Get geolocation info for an IP address (country, city, ISP, coordinates)",
        input_schema={
            "type": "object",
            "properties": {
                "ip": {"type": "string", "description": "IP address to geolocate"},
            },
            "required": ["ip"],
        },
        handler=ip_geolocation,
        category="network",
        tags=["ip", "geolocation", "network"],
    )

    # -- Crypto (expanded) tools ---

    register_tool(
        name="defi_yields",
        description="Get top DeFi yield farming opportunities from DeFiLlama (filterable by chain and TVL)",
        input_schema={
            "type": "object",
            "properties": {
                "chain": {"type": "string", "description": "Filter by chain (e.g. Ethereum, Arbitrum). Omit for all chains."},
                "min_tvl": {"type": "number", "default": 1000000, "description": "Minimum TVL in USD"},
                "limit": {"type": "integer", "default": 20, "description": "Max number of pools to return"},
            },
        },
        handler=defi_yields,
        category="crypto",
        tags=["defi", "yield", "farming"],
    )

    register_tool(
        name="gas_prices",
        description="Get current gas prices for EVM chains (Ethereum, Polygon, Arbitrum, Base, BSC, etc.)",
        input_schema={
            "type": "object",
            "properties": {
                "chain": {
                    "type": "string",
                    "default": "ethereum",
                    "description": "Chain name: ethereum, polygon, arbitrum, optimism, base, bsc, avalanche",
                },
            },
        },
        handler=gas_prices,
        category="crypto",
        tags=["gas", "evm", "transaction"],
    )

    logger.info("Registered %d built-in tools", len(_registry))

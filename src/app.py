"""
MCP Cloud - Hosted MCP server platform.

Serves MCP tools via REST API and SSE (for MCP protocol compatibility).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from .auth import (
    create_api_key,
    check_rate_limit,
    get_key_info,
    increment_usage,
    is_admin_key,
    validate_api_key,
    get_admin_key,
)
from .db import init_db
from .pricing import TIERS, get_tier, is_tool_allowed
from .tools_registry import get_tool, list_tools, register_builtins
from .usage import get_usage_stats, get_global_stats, log_call

logger = logging.getLogger("mcp_cloud")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    register_builtins()
    admin_key = get_admin_key()
    logger.info("MCP Cloud started - %d tools registered", len(list_tools()))
    logger.info("Admin key: %s", admin_key)
    yield
    logger.info("MCP Cloud shutting down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="MCP Cloud",
    description="Hosted MCP server platform with API key auth, usage tracking, and billing",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_api_key(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization must use Bearer scheme")
    return authorization[7:]


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "tools": len(list_tools())}


# ---------------------------------------------------------------------------
# Tool discovery (no auth)
# ---------------------------------------------------------------------------

@app.get("/v1/tools")
async def list_available_tools():
    """List all available tools with their schemas."""
    return {"tools": list_tools()}


# ---------------------------------------------------------------------------
# Execute a tool
# ---------------------------------------------------------------------------

@app.post("/v1/tools/{tool_name}")
async def execute_tool(
    tool_name: str,
    request: Request,
    authorization: str | None = Header(None),
):
    api_key = _extract_api_key(authorization)

    # Validate key
    record = validate_api_key(api_key)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    # Rate limit
    allowed, limit_info = check_rate_limit(api_key)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                **limit_info,
            },
        )

    # Check tool exists
    tool = get_tool(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Tool not found: {tool_name}")

    # Tier check
    if not is_tool_allowed(record["tier"], tool_name):
        raise HTTPException(
            status_code=403,
            detail=f"Tool '{tool_name}' is not available on the {record['tier']} tier. Upgrade to pro or business.",
        )

    # Parse body
    try:
        body = await request.json()
    except Exception:
        body = {}

    # Execute
    start = time.monotonic()
    try:
        result = await tool.handler(**body)
        latency_ms = (time.monotonic() - start) * 1000
        increment_usage(api_key)
        await log_call(api_key, tool_name, latency_ms, success=True)
        return {"result": result, "latency_ms": round(latency_ms, 2)}
    except TypeError as exc:
        latency_ms = (time.monotonic() - start) * 1000
        await log_call(api_key, tool_name, latency_ms, success=False, error=str(exc))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid arguments for tool '{tool_name}': {exc}",
        )
    except Exception as exc:
        latency_ms = (time.monotonic() - start) * 1000
        await log_call(api_key, tool_name, latency_ms, success=False, error=str(exc))
        logger.exception("Tool execution failed: %s", tool_name)
        raise HTTPException(status_code=500, detail=f"Tool execution failed: {exc}")


# ---------------------------------------------------------------------------
# Usage stats
# ---------------------------------------------------------------------------

@app.get("/v1/usage")
async def usage_stats(authorization: str | None = Header(None)):
    api_key = _extract_api_key(authorization)
    record = validate_api_key(api_key)
    if record is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    stats = get_usage_stats(api_key)
    key_info = get_key_info(api_key)
    tier = get_tier(key_info["tier"])

    return {
        **stats,
        "tier": key_info["tier"],
        "daily_limit": tier.daily_limit,
        "remaining_today": max(0, tier.daily_limit - stats["calls_today"]),
    }


# ---------------------------------------------------------------------------
# Admin: create API key
# ---------------------------------------------------------------------------

@app.post("/v1/keys/create")
async def create_key(
    request: Request,
    authorization: str | None = Header(None),
):
    admin_key = _extract_api_key(authorization)
    if not is_admin_key(admin_key):
        raise HTTPException(status_code=403, detail="Admin access required")

    body = await request.json()
    email = body.get("email")
    tier = body.get("tier", "free")

    if not email:
        raise HTTPException(status_code=400, detail="email is required")
    if tier not in TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Must be one of: {list(TIERS.keys())}")

    result = create_api_key(email=email, tier=tier)
    return result


# ---------------------------------------------------------------------------
# Admin: global stats
# ---------------------------------------------------------------------------

@app.get("/v1/admin/stats")
async def admin_stats(authorization: str | None = Header(None)):
    admin_key = _extract_api_key(authorization)
    if not is_admin_key(admin_key):
        raise HTTPException(status_code=403, detail="Admin access required")
    return get_global_stats()


# ---------------------------------------------------------------------------
# SSE endpoint for MCP protocol compatibility
# ---------------------------------------------------------------------------

# In-memory session store for SSE connections
_sse_sessions: dict[str, asyncio.Queue] = {}


@app.get("/sse")
async def sse_endpoint(request: Request):
    """
    SSE endpoint for MCP protocol compatibility.
    Clients connect here to receive server-sent events and get a
    session-specific endpoint for sending JSON-RPC messages.
    """
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sse_sessions[session_id] = queue

    messages_url = f"/mcp/messages?session_id={session_id}"

    async def event_stream():
        # First event tells the client where to POST messages
        yield f"event: endpoint\ndata: {messages_url}\n\n"

        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: message\ndata: {json.dumps(message)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive
                    yield ": keepalive\n\n"
        finally:
            _sse_sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/mcp/messages")
async def mcp_messages(request: Request):
    """
    Handle JSON-RPC messages from MCP clients.
    This processes MCP protocol messages and queues responses
    back through the SSE connection.
    """
    session_id = request.query_params.get("session_id")
    if not session_id or session_id not in _sse_sessions:
        raise HTTPException(status_code=400, detail="Invalid or expired session")

    queue = _sse_sessions[session_id]
    body = await request.json()

    method = body.get("method")
    msg_id = body.get("id")
    params = body.get("params", {})

    response: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}

    if method == "initialize":
        response["result"] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "mcp-cloud",
                "version": "0.1.0",
            },
        }

    elif method == "notifications/initialized":
        # No response needed for notifications
        return JSONResponse({"ok": True})

    elif method == "tools/list":
        tools = list_tools()
        mcp_tools = [
            {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": t["inputSchema"],
            }
            for t in tools
        ]
        response["result"] = {"tools": mcp_tools}

    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        tool = get_tool(tool_name)
        if tool is None:
            response["error"] = {
                "code": -32602,
                "message": f"Unknown tool: {tool_name}",
            }
        else:
            try:
                result = await tool.handler(**arguments)
                response["result"] = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2),
                        }
                    ],
                    "isError": False,
                }
            except Exception as exc:
                response["result"] = {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Error: {exc}",
                        }
                    ],
                    "isError": True,
                }

    elif method == "ping":
        response["result"] = {}

    else:
        response["error"] = {
            "code": -32601,
            "message": f"Method not found: {method}",
        }

    await queue.put(response)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Pricing info (public)
# ---------------------------------------------------------------------------

@app.get("/v1/pricing")
async def pricing():
    return {
        tier_name: {
            "daily_limit": t.daily_limit,
            "monthly_price": f"${t.monthly_price_cents / 100:.2f}",
            "all_tools": t.all_tools,
            "priority": t.priority,
        }
        for tier_name, t in TIERS.items()
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def start():
    uvicorn.run("src.app:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    start()

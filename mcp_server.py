"""A small, read-only Streamable HTTP MCP endpoint for project diagnostics."""

import asyncio
import os
import secrets
from typing import Any

import requests
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse


app = FastAPI(title="MoneyPrinterTurbo diagnostics MCP")

_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
_API_URL = os.environ.get("MCP_API_URL", "http://api:8080").rstrip("/")
_KB_URL = os.environ.get("KB_API_BASE", "http://host.docker.internal:3001").rstrip("/")


def _require_token(authorization: str | None) -> None:
    expected = f"Bearer {_TOKEN}"
    if not _TOKEN or not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="A valid bearer token is required")


def _request_status(url: str) -> dict[str, Any]:
    try:
        result = requests.get(url, timeout=5)
        return {"reachable": True, "status_code": result.status_code}
    except requests.RequestException as exc:
        return {"reachable": False, "error": str(exc)}


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "project_health",
            "description": "Read-only check of the video project's API and knowledge-base service availability.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True},
        }
    ]


async def _invoke_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name != "project_health":
        return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
    if arguments:
        return {"content": [{"type": "text", "text": "project_health accepts no arguments"}], "isError": True}

    api, knowledge_base = await asyncio.gather(
        asyncio.to_thread(_request_status, f"{_API_URL}/docs"),
        asyncio.to_thread(_request_status, f"{_KB_URL}/api/health"),
    )
    result = {"api": api, "knowledge_base": knowledge_base, "read_only": True}
    return {"content": [{"type": "text", "text": __import__("json").dumps(result, ensure_ascii=False)}]}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/mcp")
async def mcp(request: Request, authorization: str | None = Header(default=None)) -> Response:
    _require_token(authorization)
    body = await request.json()
    request_id = body.get("id")
    method = body.get("method")

    if body.get("jsonrpc") != "2.0" or not method:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid Request"}}, status_code=400)

    if method.startswith("notifications/"):
        return Response(status_code=202)

    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "moneyprinterturbo-diagnostics", "version": "1.0.0"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tool_definitions()}
    elif method == "tools/call":
        params = body.get("params") or {}
        result = await _invoke_tool(params.get("name", ""), params.get("arguments") or {})
    else:
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})

    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})

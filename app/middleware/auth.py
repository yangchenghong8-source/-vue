"""
API authentication middleware.

When MPT_API_AUTH_KEY is configured, all /api/* requests must include
an ``Authorization: Bearer <key>`` header.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from loguru import logger


_AUTH_HEADER_PREFIX = "Bearer "


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Simple API-key middleware that only guards /api/* routes."""

    async def dispatch(self, request: Request, call_next):
        # Only guard API routes — leave static files, docs, etc. open
        if not request.url.path.startswith("/api/"):
            return await call_next(request)

        # Allow OpenAPI docs even under /api (though they live at /docs usually)
        if request.url.path in ("/api/v1/openapi.json",):
            return await call_next(request)

        from app.config.config import app as _cfg
        expected_key = (_cfg.get("api_auth_key") or "").strip()

        # No key configured → auth is disabled
        if not expected_key:
            return await call_next(request)

        # Check Authorization header
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith(_AUTH_HEADER_PREFIX):
            token = auth_header[len(_AUTH_HEADER_PREFIX):]
            if token == expected_key:
                return await call_next(request)

        logger.warning(
            f"Rejected unauthenticated API request from {request.client.host if request.client else 'unknown'}"
        )
        return JSONResponse(
            status_code=401,
            content={
                "status": 401,
                "message": "Missing or invalid API key. Use Authorization: Bearer <key>",
            },
        )

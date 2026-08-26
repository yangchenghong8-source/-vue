"""
Structured logging configuration.

Provides:
  - Per-request trace_id injected via contextvars
  - JSON log files with daily rotation in storage/logs/
  - Console output unchanged (human-readable)
"""

import os
import sys
import uuid
from contextvars import ContextVar

from loguru import logger

from app.utils import utils

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_request_id_var: ContextVar[str] = ContextVar("request_id", default="")
_json_sink_id: int | None = None


def get_trace_id() -> str:
    return _trace_id_var.get() or ""


def get_request_id() -> str:
    return _request_id_var.get() or ""


def set_trace_id(trace_id: str | None = None) -> str:
    """Set the trace_id for the current async context.  Returns the id."""
    tid = trace_id or uuid.uuid4().hex[:12]
    _trace_id_var.set(tid)
    return tid


def set_request_id(request_id: str) -> None:
    _request_id_var.set(request_id)


def _json_patcher(record: dict) -> None:
    """Inject trace_id into every log record."""
    record["extra"]["trace_id"] = _trace_id_var.get() or "-"
    rid = _request_id_var.get()
    if rid:
        record["extra"]["request_id"] = rid


def setup_structured_logging():
    """Add a JSON file sink with rotation. Call once at application startup."""
    global _json_sink_id

    logs_dir = os.path.join(utils.storage_dir(), "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # Remove previously added JSON sink to avoid duplicates on reload
    if _json_sink_id is not None:
        try:
            logger.remove(_json_sink_id)
        except (TypeError, ValueError):
            pass

    logger.configure(patcher=_json_patcher)

    _json_sink_id = logger.add(
        os.path.join(logs_dir, "app_{time:YYYY-MM-DD}.jsonl"),
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[trace_id]} | {name}:{function}:{line} | {message}",
        rotation="00:00",          # rotate at midnight
        retention="7 days",        # keep 7 days of logs
        compression="gz",          # compress rotated logs
        level="DEBUG",
        enqueue=True,              # thread-safe writing
        serialize=True,            # JSON output
    )

    logger.info(f"Structured logging initialized (JSON sink: {logs_dir})")


# Initialize immediately so task-worker threads also get structured output
setup_structured_logging()

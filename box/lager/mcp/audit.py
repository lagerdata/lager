# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Audit logging for MCP tool calls.

Every tool invocation is appended as a JSON line to /etc/lager/mcp_audit.log
so downstream consumers (Stout, security review, post-mortem) have a durable
record of what an agent did against real hardware.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

AUDIT_LOG_PATH = Path(os.environ.get("LAGER_MCP_AUDIT_LOG", "/etc/lager/mcp_audit.log"))


def _redact(value: Any) -> Any:
    """Best-effort redaction of obvious oversize args."""
    if isinstance(value, str) and len(value) > 256:
        return value[:256] + f"...<truncated {len(value) - 256} chars>"
    return value


def log_tool_call(
    tool_name: str,
    args: dict[str, Any] | None = None,
    result_summary: str | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
) -> None:
    """Append a single audit record. Failures here must never crash a tool."""
    record = {
        "ts": time.time(),
        "tool": tool_name,
        "args": {k: _redact(v) for k, v in (args or {}).items()},
        "duration_ms": duration_ms,
        "result_summary": result_summary,
        "error": error,
    }
    try:
        AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG_PATH.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning("audit log write failed: %s", e)


def audited(tool_name: str | None = None) -> Callable:
    """Decorator that wraps an MCP tool function and audits every call.

    Place directly under @mcp.tool() so audited is the inner wrapper that
    sees the real arguments. Uses inspect.signature so functools.wraps
    preserves the FastMCP-visible schema.
    """
    def deco(fn: Callable) -> Callable:
        name = tool_name or fn.__name__
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.monotonic()
            error = None
            result = None
            try:
                bound = sig.bind_partial(*args, **kwargs)
                call_args = dict(bound.arguments)
            except TypeError:
                call_args = {"_args": list(args), "_kwargs": kwargs}
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                raise
            finally:
                duration_ms = (time.monotonic() - start) * 1000.0
                summary = result[:200] if isinstance(result, str) else None
                log_tool_call(
                    tool_name=name,
                    args=call_args,
                    result_summary=summary,
                    duration_ms=duration_ms,
                    error=error,
                )

        return wrapper
    return deco

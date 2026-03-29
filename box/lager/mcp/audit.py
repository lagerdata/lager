# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Audit logging stub.

Deferred until the core HIL loop is proven. For now, logs to stderr
via the standard logging module.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_tool_call(tool_name: str, **kwargs) -> None:
    """Log an MCP tool invocation (no-op beyond stderr for now)."""
    logger.debug("MCP tool call: %s %s", tool_name, kwargs)

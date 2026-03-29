# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Minimal session state for the MCP server.

v0 simplification: no lock management, no reservation tokens, no DUT
selection.  The box is assumed to be exclusively available to this
server process.  A future version will integrate with the existing
POST /lock endpoint (box/lager/http_handlers/lock_handler.py) to
acquire an exclusive reservation on session init and release it on
shutdown.

For now we only track a random session ID for correlation.
"""

from __future__ import annotations

import uuid


_session_id: str = str(uuid.uuid4())


def get_session_id() -> str:
    return _session_id

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The control and exec tiers answer only on a box with an MCP bearer token.

``LAGER_MCP_ALLOW_CONTROL`` and ``LAGER_MCP_ALLOW_EXEC`` expose hardware
control and arbitrary command execution. Before this guard, a box with a tier
on and no token file served those tools to anything that could reach the
port, and the server only logged a warning about it.

The check runs on every call, not at registration: the token file is the
switch for the bearer check in ``lager.mcp.auth``, and ``enable`` and
``disable`` take effect with no restart. Deciding once at startup would
leave the tiers open after a ``disable``, or refused after an ``enable``.

A call that gets this far on a box with a token has already passed the bearer
check, so the guard only has to ask whether a token exists. The read-only
tools are not wrapped and keep answering without one.
"""

from __future__ import annotations

import functools
import logging

from ...box_config import mcp_token
from ._payload import reply as _reply

logger = logging.getLogger(__name__)

REFUSAL = (
    "This box has no MCP bearer token, so the {tier} tools are refused. "
    "On the operator machine, run 'lager box-config mcp-token enable --box <BOX>' "
    "and give the token to your MCP client."
)


def guarded(fn, tier: str, token_path: str | None = None):
    """``fn``, refusing with an error reply unless a token is enabled."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from ...constants import MCP_TOKEN_PATH

        path = MCP_TOKEN_PATH if token_path is None else token_path
        if mcp_token.state(path) != mcp_token.ENABLED:
            logger.warning("Refused MCP %s tool %s: no bearer token on this box", tier, fn.__name__)
            return _reply({"error": REFUSAL.format(tier=tier)})
        return fn(*args, **kwargs)

    return wrapper


__all__ = ["REFUSAL", "guarded"]

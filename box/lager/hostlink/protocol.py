# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Hostlink wire protocol: version negotiation and the error envelope.

The payloads themselves are the Box's existing ones (``POST /invoke`` and
friends). This module only defines what the link adds around them.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Bumped only for a change one side cannot ignore. A side supports every
# version from MIN_PROTOCOL_VERSION to PROTOCOL_VERSION inclusive.
PROTOCOL_VERSION = 1
MIN_PROTOCOL_VERSION = 1

HANDSHAKE_PATH = "/v1/handshake"

# Sent by the relay on every forwarded request, so an agent that was upgraded
# underneath a running container refuses instead of misreading a payload.
PROTOCOL_HEADER = "X-Lager-Hostlink-Protocol"

# Paths under this prefix are answered by the relay itself and never
# forwarded, so they cannot collide with a hardware service route.
RELAY_PREFIX = "/_hostlink/"

# Error codes. Stable strings: the CLI and the installer match on them.
ERR_UNAUTHORIZED = "unauthorized"
ERR_PROTOCOL_MISMATCH = "protocol-mismatch"
ERR_AGENT_UNREACHABLE = "host-agent-unreachable"
ERR_NOT_CONFIGURED = "host-agent-not-configured"
ERR_BAD_REQUEST = "bad-request"


class ProtocolMismatch(Exception):
    """The two sides share no protocol version."""

    def __init__(self, own: tuple, peer: tuple):
        self.own = own
        self.peer = peer
        super().__init__(
            f"no shared hostlink protocol version: this side supports "
            f"{own[0]}-{own[1]}, the other side supports {peer[0]}-{peer[1]}"
        )


def negotiate(peer_min: Any, peer_max: Any, *,
              own_min: int = MIN_PROTOCOL_VERSION,
              own_max: int = PROTOCOL_VERSION) -> int:
    """Return the highest protocol version both sides support.

    Raises ``ProtocolMismatch`` when the ranges do not overlap, and
    ``ValueError`` when the peer's range is not a pair of positive integers
    in order -- a malformed handshake is a bad request, not a mismatch.
    """
    for value in (peer_min, peer_max):
        # bool is an int subclass; True must not pass as version 1.
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"protocol version must be a positive integer, got {value!r}")
    if peer_min > peer_max:
        raise ValueError(f"protocol range is reversed: {peer_min} > {peer_max}")
    shared = min(own_max, peer_max)
    if shared < max(own_min, peer_min):
        raise ProtocolMismatch((own_min, own_max), (peer_min, peer_max))
    return shared


def handshake_request(box_version: str = "") -> Dict[str, Any]:
    """Body the relay sends to ``HANDSHAKE_PATH``."""
    return {
        "protocol_min": MIN_PROTOCOL_VERSION,
        "protocol_max": PROTOCOL_VERSION,
        "box_version": box_version,
    }


def handshake_response(protocol: int, agent_version: str, platform: str) -> Dict[str, Any]:
    """Body the agent answers a successful handshake with."""
    return {
        "protocol": protocol,
        "agent_version": agent_version,
        "platform": platform,
    }


def error_body(code: str, message: str, *, retryable: bool = False,
               details: Optional[str] = None) -> Dict[str, Any]:
    """Error envelope for failures of the link itself.

    ``error`` carries the human-readable line because that is the field the
    Box's ``DeviceError`` already stringifies to, so a link failure reads as
    one clear sentence wherever a hardware service error would have.
    Failures raised by a driver are not wrapped: they pass through unchanged
    so their text matches a Linux Box exactly.
    """
    body: Dict[str, Any] = {"error": message, "code": code, "retryable": retryable}
    if details:
        body["details"] = details
    return body

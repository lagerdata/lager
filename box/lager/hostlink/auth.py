# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Bearer-token authentication for the host agent.

The agent listens on loopback, which keeps the LAN out but not other users
or processes on the same machine. Every request therefore carries the shared
token from the endpoint record, and the agent refuses any that does not.
"""
from __future__ import annotations

import hmac
from typing import Mapping, Optional

AUTH_HEADER = "Authorization"
_SCHEME = "Bearer"


def bearer_header(token: str) -> str:
    """Value of the ``Authorization`` header for ``token``."""
    return f"{_SCHEME} {token}"


def presented_token(headers: Mapping[str, str]) -> Optional[str]:
    """The bearer token in ``headers``, or None when there is none."""
    value = headers.get(AUTH_HEADER)
    if not value:
        return None
    scheme, _, token = value.strip().partition(" ")
    if scheme.lower() != _SCHEME.lower():
        return None
    token = token.strip()
    return token or None


def is_authorized(headers: Mapping[str, str], expected_token: str) -> bool:
    """True when ``headers`` carries exactly ``expected_token``.

    Compared in constant time. An empty expected token never authorizes
    anything: a misconfigured agent must fail closed, not open.
    """
    if not expected_token:
        return False
    token = presented_token(headers)
    if token is None:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8"))

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""One place to stamp a tool reply with the box it came from.

A client that talks to many boxes through one session (a fleet host, a
control plane) needs every reply to say which box answered; the transport
does not tell it. The discovery tools carry ``bench.box_id``; the control
and exec tiers have no bench at hand, so they stamp the id from the box's
own file here.
"""

from __future__ import annotations

import json
from typing import Any


def box_id() -> str:
    """The box id, or ``"unknown"``; never raises, so a reply is never lost
    to an unreadable id file."""
    try:
        from ..config import get_box_id

        return get_box_id()
    except Exception:  # pragma: no cover - permission or I/O failure
        return "unknown"


def reply(payload: dict[str, Any]) -> str:
    """Serialise ``payload`` with ``box_id`` first."""
    return json.dumps({"box_id": box_id(), **payload})


__all__ = ["box_id", "reply"]

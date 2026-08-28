#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`_auto_connect_if_needed` must gate on the target, not on a live process.

The gate read one boolean from `/debug/status` that meant "the gdbserver PID is
alive", and returned True on it without touching the target. On a box where the
server outlives the part, `flash`, `reset`, `memrd` and `erase` therefore all
proceeded against absent hardware believing they were connected.

The half most likely to regress is the tri-state, so it is pinned hardest here.
`target_attached` is None whenever the box could not establish an answer -- an
older box that does not send the field, a gdbserver refusing a second GDB
client because a user is already attached, a probe that timed out. None is not
False: treating it as "absent" reconnects a session that was working, and
treating it as "attached" reinstates the original bug.

What enforces that is the explicit `attached is None` branch in the gate, not
the `is True` spelling next to it -- with the branch present the two spellings
behave identically, and removing the branch fails
`test_an_inconclusive_probe_falls_back_to_server_liveness` and
`test_an_older_box_that_omits_the_field_still_works` (both verified).

`_is_connected` has never had a direct unit test; test/COVERAGE.md recorded the
whole `lager debug` group as having none.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

debug_mod = importlib.import_module("cli.commands.development.debug.commands")

NET = {"name": "debug1", "role": "debug", "channel": "NRF5340_XXAA_APP"}


class FakeClient:
    """Answers /debug/status from a canned shape and records connects."""

    def __init__(self, status):
        self._status = status
        self.connects = []

    def get_debug_status(self, net=None, probe=False):
        self.connects.append(("status", probe))
        return dict(self._status)

    def connect(self, net, **kwargs):
        self.connects.append(("connect", kwargs))
        return {"status": "connected"}

    @property
    def connect_calls(self):
        return [c for c in self.connects if c[0] == "connect"]


def _run(status):
    client = FakeClient(status)
    ok = debug_mod._auto_connect_if_needed(client, NET, ctx=None, quiet=True)
    return ok, client


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def test_an_answering_target_skips_the_connect():
    ok, client = _run({"gdbserver_running": True, "target_attached": True})
    assert ok is True
    assert client.connect_calls == []


def test_a_live_server_with_an_absent_target_forces_a_reconnect():
    """The case the old gate could not see. It used to return True here."""
    ok, client = _run({"gdbserver_running": True, "target_attached": False})
    assert ok is True
    assert len(client.connect_calls) == 1
    # force=True: reusing the session whose target is gone is the bug.
    assert client.connect_calls[0][1]["force"] is True


def test_nothing_running_connects_normally():
    ok, client = _run({"gdbserver_running": False, "target_attached": False})
    assert ok is True
    assert len(client.connect_calls) == 1
    assert client.connect_calls[0][1]["force"] is False


# --------------------------------------------------------------------------
# None is not False
# --------------------------------------------------------------------------

def test_an_inconclusive_probe_falls_back_to_server_liveness():
    """None with a live server behaves exactly as the old gate did."""
    ok, client = _run({"gdbserver_running": True, "target_attached": None})
    assert ok is True
    assert client.connect_calls == [], (
        "None is not evidence the target is absent; reconnecting here would "
        "tear down a working session"
    )


def test_an_inconclusive_probe_with_no_server_still_connects():
    ok, client = _run({"gdbserver_running": False, "target_attached": None})
    assert ok is True
    assert len(client.connect_calls) == 1


def test_an_older_box_that_omits_the_field_still_works():
    """A box predating this change sends only `connected`."""
    ok, client = _run({"connected": True})
    assert ok is True
    assert client.connect_calls == []


def test_an_older_box_with_no_session_connects():
    ok, client = _run({"connected": False})
    assert ok is True
    assert len(client.connect_calls) == 1


# --------------------------------------------------------------------------
# The helpers themselves
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ({"gdbserver_running": True, "connected": True}, True),
    ({"gdbserver_running": False, "connected": False}, False),
    # The explicit field wins over the alias if they ever disagree.
    ({"gdbserver_running": False, "connected": True}, False),
    # Older box: only the alias exists.
    ({"connected": True}, True),
    ({}, False),
])
def test_is_connected_reads_the_server_state(status, expected):
    assert debug_mod._is_connected(FakeClient(status), NET) is expected


@pytest.mark.parametrize("status,expected", [
    ({"target_attached": True}, True),
    ({"target_attached": False}, False),
    ({"target_attached": None}, None),
    ({}, None),
])
def test_target_attached_preserves_the_tri_state(status, expected):
    assert debug_mod._target_attached(FakeClient(status), NET) is expected


def test_target_attached_asks_the_box_to_probe():
    """The wire read is opt-in, so this caller has to request it."""
    client = FakeClient({"target_attached": True})
    debug_mod._target_attached(client, NET)
    assert ("status", True) in client.connects


def test_is_connected_does_not_pay_for_a_probe():
    """The cheap question must stay cheap; it is asked on every subcommand."""
    client = FakeClient({"gdbserver_running": True})
    debug_mod._is_connected(client, NET)
    assert ("status", False) in client.connects


def test_a_box_that_errors_is_not_reported_as_attached():
    class Broken(FakeClient):
        def get_debug_status(self, net=None, probe=False):
            raise RuntimeError("box unreachable")

    assert debug_mod._target_attached(Broken({}), NET) is None
    assert debug_mod._is_connected(Broken({}), NET) is False

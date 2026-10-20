#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager debug <net> memrd --json` writes only JSON to stdout.

The auto-connect progress lines ("Auto-connecting to debugger...",
"Auto-connected!") used to go to stdout, so whenever memrd had to connect
first, the JSON a script read was preceded by them and did not parse. They go
to stderr, with every other progress message in the command.

The box is mocked at the `DebugServiceClient` boundary, so no hardware or
network is touched.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import patch

from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

debug_mod = importlib.import_module("cli.commands.development.debug.commands")

NET = {"name": "debug1", "role": "debug", "channel": "NRF5340_XXAA_APP"}


class _Obj:
    """Settable stand-in for the LagerContext (the group stashes `net_name`)."""


class FakeClient:
    """A target with no debug session yet, so memrd must connect first."""

    def __init__(self):
        self.connected = False

    def get_debug_status(self, net, probe=False):
        return {"connected": self.connected, "target_attached": self.connected}

    def connect(self, net, speed=None, force=False, halt=False, gdb=False,
                gdb_port=None, jlink_script=None, openocd_config=None):
        self.connected = True
        return {"status": "connected"}

    def read_memory(self, net, start_addr, length, no_reset=False):
        return bytes(range(length))

    def close(self):
        pass


def run_memrd(client, args):
    obj = _Obj()
    obj.net_name = NET["name"]
    with patch.object(debug_mod, "_resolve_box_with_username",
                      lambda ctx, box: ("1.2.3.4", "lagerdata")), \
         patch.object(debug_mod, "_get_debug_net", lambda ctx, box, net_name=None: NET), \
         patch.object(debug_mod, "_resolve_debug_scripts", lambda ctx, name, net: (None, None)), \
         patch.object(debug_mod, "_get_service_client", lambda box: client):
        return CliRunner().invoke(debug_mod.memrd, args, obj=obj, catch_exceptions=False)


def test_json_stdout_parses_after_an_auto_connect():
    client = FakeClient()
    result = run_memrd(client, ["0x0", "16", "--json", "--box", "mybox"])
    assert result.exit_code == 0, result.output
    assert client.connected, "the fake should have been connected by memrd"
    payload = json.loads(result.stdout)
    assert payload["length"] == 16
    assert payload["data"][0].startswith("0x0:\t0x00\t0x01")


def test_the_progress_lines_still_reach_the_terminal():
    result = run_memrd(FakeClient(), ["0x0", "16", "--json", "--box", "mybox"])
    assert "Auto-connected!" in result.stderr
    assert "Auto-connected!" not in result.stdout

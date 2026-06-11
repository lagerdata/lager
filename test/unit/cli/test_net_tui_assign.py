#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the custom-device assignment helpers in
``cli/commands/box/net_tui.py``.

The TUI's assign flow (AssignDeviceScreen / DevicePickDialog /
ConfirmUnassignDialog) is driven by three module-level helpers that carry
all the decision logic, so they're tested here without a running Textual
app — same approach as test_net_tui_uart_guard.py:

  * ``_assign_payload`` — picks the durable cable identity (serial when the
    cable has one, else port path) and forwards the optional baud override.
  * ``_cable_ident`` — the human label used by both the tree rows and the
    confirmation dialogs.
  * ``_run_custom_devices`` — wraps the box backend; "no parseable stdout"
    is the failure signal (the backend reports errors on stderr with a
    non-zero exit, which ``_run_script`` swallows).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

tui = importlib.import_module('cli.commands.box.net_tui')


# --------------------------------------------------------------------------- #
# _assign_payload                                                             #
# --------------------------------------------------------------------------- #

class TestAssignPayload:
    def test_prefers_serial_identity(self):
        cable = {"serial": "00000006", "port_path": "1-1.2"}
        payload = tui._assign_payload(cable, "Rigol_DP711", None)
        assert payload == {"instrument": "Rigol_DP711", "serial": "00000006"}

    def test_falls_back_to_port_path(self):
        cable = {"serial": None, "port_path": "1-1.2"}
        payload = tui._assign_payload(cable, "Rigol_DP711", None)
        assert payload == {"instrument": "Rigol_DP711", "port_path": "1-1.2"}

    def test_baud_included_only_when_set(self):
        cable = {"serial": "S"}
        assert "baud" not in tui._assign_payload(cable, "Rigol_DP711", None)
        assert tui._assign_payload(cable, "Rigol_DP711", 19200)["baud"] == 19200


# --------------------------------------------------------------------------- #
# _cable_ident                                                                #
# --------------------------------------------------------------------------- #

class TestCableIdent:
    def test_serial_form(self):
        assert tui._cable_ident({"serial": "00000006"}) == "serial 00000006"

    def test_port_form_when_no_serial(self):
        assert tui._cable_ident({"serial": None, "port_path": "1-1.2"}) == "port 1-1.2"


# --------------------------------------------------------------------------- #
# _run_custom_devices                                                         #
# --------------------------------------------------------------------------- #

class TestRunCustomDevices:
    def _with_output(self, raw: str):
        with patch.object(tui, "_run_script", return_value=raw):
            return tui._run_custom_devices(None, "box", "list")

    def test_parses_dict_output(self):
        data = {"catalog": [], "assignments": [], "cables": []}
        assert self._with_output(json.dumps(data)) == data

    def test_empty_output_is_failure(self):
        # Old box image (script missing) or backend error: stdout is empty.
        assert self._with_output("") is None
        assert self._with_output("   \n") is None

    def test_garbage_output_is_failure(self):
        assert self._with_output("Traceback (most recent call last): ...") is None

    def test_duplicated_json_objects_are_tolerated(self):
        # The box backend can double-execute and emit the payload twice;
        # _parse_backend_json_tui's dedup handles it.
        data = {"removed": True}
        raw = json.dumps(data) + json.dumps(data)
        assert self._with_output(raw) == data

    def test_forwards_args_to_backend(self):
        with patch.object(tui, "_run_script", return_value="{}") as run:
            tui._run_custom_devices(None, "mybox", "assign", '{"x":1}')
        run.assert_called_once_with(None, "custom_devices.py", "mybox",
                                    "assign", '{"x":1}')


# --------------------------------------------------------------------------- #
# table regression                                                            #
# --------------------------------------------------------------------------- #

class TestSingleChannelTable:
    def test_dp711_is_single_channel_with_power_supply_role(self):
        # Catalog says single_channel=True; the role tuple uses the
        # scanner-vocabulary "power-supply" (what saved nets carry).
        assert tui._SINGLE_CHANNEL_INST["Rigol_DP711"] == ("power-supply",)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

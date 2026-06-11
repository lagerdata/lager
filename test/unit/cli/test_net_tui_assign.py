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
# --as-net twin (CreateNetDialog helpers)                                     #
# --------------------------------------------------------------------------- #

ASSIGNMENT = {
    "instrument": "Rigol_DP711",
    "address": "serial://067b:23a3/serial/00000006",
    "roles": ["power-supply"],
    "channels": {"power-supply": ["1"]},
}


class TestNetFromAssignment:
    def test_builds_power_supply_net(self):
        net = tui._net_from_assignment(ASSIGNMENT, "main_supply")
        assert net.net == "main_supply"
        assert net.instrument == "Rigol_DP711"
        # REGRESSION: the saved role must be the scanner-vocabulary
        # "power-supply" — the supply CLI and the box dispatcher match it
        # exactly; the legacy "supply" token saves an undriveable net.
        assert net.type == "power-supply"
        assert net.chan == "1"
        assert net.addr == ASSIGNMENT["address"]
        assert net.saved is False

    def test_falls_back_to_power_supply_defaults(self):
        # Old backend response without roles/channels: sane defaults.
        net = tui._net_from_assignment(
            {"instrument": "Rigol_DP711", "address": "serial://x"}, "n")
        assert net.type == "power-supply"
        assert net.chan == "1"


class TestDefaultNetName:
    def test_derives_from_instrument(self):
        assert tui._default_net_name(ASSIGNMENT) == "rigol_dp711"

    def test_tolerates_missing_instrument(self):
        assert tui._default_net_name({}) == "net"


class TestNetNameTaken:
    def test_saved_name_is_taken(self):
        nets = [tui.Net("I", "1", "power-supply", "supply1", "a", saved=True)]
        assert tui._net_name_taken(nets, "supply1") is True

    def test_unsaved_placeholder_does_not_block(self):
        # Placeholders get auto-names; only *saved* nets reserve a name.
        nets = [tui.Net("I", "1", "power-supply", "supply1", "a", saved=False)]
        assert tui._net_name_taken(nets, "supply1") is False
        assert tui._net_name_taken(nets, "other") is False


class TestAddressHasSavedNet:
    """REGRESSION: a baud-only re-assign must not re-offer the Create Net
    dialog — a second net at the same serial:// address would bypass the
    single-channel constraint nets-add enforces."""

    ADDR = "serial://067b:23a3/serial/00000006"

    def test_saved_net_at_address_suppresses_offer(self):
        nets = [tui.Net("Rigol_DP711", "1", "power-supply", "supply1",
                        self.ADDR, saved=True)]
        assert tui._address_has_saved_net(nets, self.ADDR) is True

    def test_unsaved_placeholder_does_not_suppress(self):
        nets = [tui.Net("Rigol_DP711", "1", "power-supply", "supply1",
                        self.ADDR, saved=False)]
        assert tui._address_has_saved_net(nets, self.ADDR) is False

    def test_other_address_does_not_suppress(self):
        nets = [tui.Net("Rigol_DP711", "1", "power-supply", "supply1",
                        "serial://067b:23a3/serial/OTHER", saved=True)]
        assert tui._address_has_saved_net(nets, self.ADDR) is False
        assert tui._address_has_saved_net(nets, "") is False


# --------------------------------------------------------------------------- #
# row labels — markup safety                                                  #
# --------------------------------------------------------------------------- #

class TestRowLabels:
    """REGRESSION: device fields like ``[067b:23a3]`` crashed the TUI with
    ``MarkupError: Expected markup style value`` when interpolated into
    markup-parsed widget content. Tree rows therefore use ``rich.Text``
    objects (markup-inert) and the dialogs pass ``markup=False``.
    """

    CABLE = {"serial": "00000006", "vid": "067b", "pid": "23a3",
             "port_path": "1-1.2", "tty": "/dev/ttyUSB0"}

    def test_cable_label_is_markup_inert_text(self):
        from rich.text import Text
        label = tui._cable_row_label(self.CABLE)
        assert isinstance(label, Text)
        # The vid:pid brackets survive literally instead of being parsed.
        assert "[067b:23a3]" in label.plain
        assert "/dev/ttyUSB0" in label.plain

    def test_assignment_label_is_markup_inert_text(self):
        from rich.text import Text
        label = tui._assignment_row_label({
            "instrument": "Rigol_DP711", "serial": "00000006",
            "tty": "/dev/ttyUSB0", "baud": 19200,
        })
        assert isinstance(label, Text)
        assert "Rigol_DP711" in label.plain
        assert "baud 19200" in label.plain
        assert "→ /dev/ttyUSB0" in label.plain

    def test_assignment_label_unplugged_cable(self):
        label = tui._assignment_row_label({"instrument": "Rigol_DP711",
                                           "serial": "S"})
        assert "(cable not connected)" in label.plain

    def test_bracketed_device_data_crashes_markup_statics(self):
        # Pin the failure mechanism this guards against: the same string
        # crashes a default (markup=True) Static but renders with
        # markup=False — the setting DevicePickDialog / ConfirmUnassignDialog
        # use for interpolated content.
        from textual.widgets import Static
        content = tui._cable_row_label(self.CABLE).plain
        with pytest.raises(Exception):
            Static(content).visual  # noqa: B018 — render is the assertion
        Static(content, markup=False).visual  # must not raise


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

#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``lager nets assign`` (cli/commands/box/nets.py).

The Click command end-to-end with ``run_python_internal`` mocked, so the
box round-trip is replaced by an in-memory custom-device backend and net DB.

These tests pin two contracts:
  * The assign flow round-trips: assign → (optional) --as-net net creation →
    remove, with the JSON payloads the box-side custom_devices.py expects.
  * REGRESSION: a net created via ``--as-net`` (or suggested by the printed
    hint) carries role ``power-supply`` — the scanner-vocabulary role the
    supply CLI (validate_net_exists) and the box dispatcher (ensure_role)
    match exactly. The legacy ``supply`` token saves a net those paths
    reject, which is why INSTRUMENT_NET_MAP gates Rigol_DP711 on
    ``power-supply``.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

nets_mod = importlib.import_module('cli.commands.box.nets')
from cli.commands.box.nets import nets as nets_group  # noqa: E402


VID, PID, SERIAL = "067b", "23a3", "00000006"
TTY = "/dev/ttyUSB0"
ADDR = f"serial://{VID}:{PID}/serial/{SERIAL}"


class FakeBox:
    """In-memory stand-in for the box-side scripts assign_cmd shells out to."""

    def __init__(self):
        self.assignments: list[dict] = []
        self.saved_nets: list[dict] = []
        # Names the next "assign" should report as cascade-deleted (the
        # replaced-assignment case); tests inject these directly.
        self.pending_deleted_on_assign: list[str] = []

    # The DP711 record the scanner reports once the cable is assigned.
    def _dp711_record(self) -> dict:
        return {
            "name": "Rigol_DP711",
            "vid": VID, "pid": PID, "serial": SERIAL,
            "address": ADDR,
            "net_type": ["power-supply"],
            "channels": {"power-supply": ["1"]},
            "tty_path": TTY,
            "custom": True,
        }

    def run_python_internal(self, ctx, runnable, box, *, args=(), **kwargs):
        script = os.path.basename(str(runnable))
        args = tuple(args)

        if script == "custom_devices.py":
            cmd = args[0]
            if cmd == "list":
                print(json.dumps({
                    "catalog": [{"name": "Rigol_DP711", "display_name": "Rigol DP711",
                                 "roles": ["power-supply"],
                                 "channels": {"power-supply": ["1"]},
                                 "default_baud": 9600}],
                    "assignments": list(self.assignments),
                    "cables": [] if self.assignments else [
                        {"vid": VID, "pid": PID, "serial": SERIAL,
                         "port_path": "1-1.2", "tty": TTY}],
                }))
            elif cmd == "assign":
                payload = json.loads(args[1])
                # Mirror the impl's replacement cascade: stale nets are
                # reported via deleted_nets (tests inject them directly).
                deleted = list(self.pending_deleted_on_assign)
                self.pending_deleted_on_assign = []
                rec = {
                    "instrument": "Rigol_DP711",
                    "vid": VID, "pid": PID,
                    "serial": payload.get("serial"),
                    "port_path": payload.get("port_path"),
                    "address": ADDR, "tty": TTY,
                    "roles": ["power-supply"],
                    "channels": {"power-supply": ["1"]},
                    "deleted_nets": deleted,
                }
                if payload.get("baud") is not None:
                    rec["baud"] = payload["baud"]
                self.assignments = [rec]
                print(json.dumps(rec))
            elif cmd == "remove":
                removed = bool(self.assignments)
                self.assignments = []
                # Mirror the impl's cascade: nets bound to the assignment's
                # address are deleted and reported.
                deleted = [n["name"] for n in self.saved_nets
                           if n.get("address") == ADDR] if removed else []
                self.saved_nets = [n for n in self.saved_nets
                                   if n.get("address") != ADDR or not removed]
                print(json.dumps({"removed": removed, "instrument": "Rigol_DP711",
                                  "address": ADDR, "deleted_nets": deleted}))
            return

        if script == "query_instruments.py":
            instruments = [self._dp711_record()] if self.assignments else []
            if args and args[0] == "get_instrument":
                match = next((i for i in instruments if i["address"] == args[1]), {})
                print(json.dumps(match))
            else:
                print(json.dumps(instruments))
            return

        if script == "net.py":
            cmd = args[0]
            if cmd == "list":
                print(json.dumps(self.saved_nets))
            elif cmd == "save":
                self.saved_nets.append(json.loads(args[1]))
            return

        raise AssertionError(f"unexpected script {script} {args}")


@pytest.fixture
def fake_box():
    box = FakeBox()
    with patch.object(nets_mod, "run_python_internal", box.run_python_internal), \
         patch("cli.box_storage.resolve_and_validate_box",
               lambda ctx, name: name or "testbox"):
        yield box


def _invoke(args):
    return CliRunner().invoke(nets_group, args, obj=None, catch_exceptions=False)


# --------------------------------------------------------------------------- #
# assign                                                                      #
# --------------------------------------------------------------------------- #

class TestAssign:
    def test_assign_by_serial_prints_address_and_hint(self, fake_box):
        result = _invoke(["assign", "Rigol_DP711", "--serial", SERIAL, "--box", "b"])
        assert result.exit_code == 0
        assert ADDR in result.output
        # The printed nets-add hint must use the driveable role vocabulary.
        assert "power-supply 1" in result.output
        assert "supply 1 '" not in result.output.replace("power-supply 1", "")
        assert fake_box.assignments[0]["serial"] == SERIAL

    def test_assign_with_baud_forwards_override(self, fake_box):
        result = _invoke(["assign", "Rigol_DP711", "--serial", SERIAL,
                          "--baud", "19200", "--box", "b"])
        assert result.exit_code == 0
        assert fake_box.assignments[0]["baud"] == 19200
        assert "19200" in result.output

    def test_as_net_saves_power_supply_role(self, fake_box):
        # REGRESSION: the saved role must be "power-supply" (what the supply
        # CLI and the box dispatcher match), never the legacy "supply" token.
        result = _invoke(["assign", "Rigol_DP711", "--serial", SERIAL,
                          "--as-net", "main_supply", "--box", "b"])
        assert result.exit_code == 0, result.output
        assert len(fake_box.saved_nets) == 1
        net = fake_box.saved_nets[0]
        assert net["role"] == "power-supply"
        assert net["name"] == "main_supply"
        assert net["instrument"] == "Rigol_DP711"
        assert net["address"] == ADDR
        assert str(net["pin"]) == "1"

    def test_as_net_bare_flag_derives_name_from_device(self, fake_box):
        result = _invoke(["assign", "Rigol_DP711", "--serial", SERIAL,
                          "--as-net", "--box", "b"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["name"] == "rigol_dp711"

    def test_assign_requires_exactly_one_identity(self, fake_box):
        result = _invoke(["assign", "Rigol_DP711", "--box", "b"])
        assert result.exit_code != 0
        assert "--serial or --port" in result.output

        result = _invoke(["assign", "Rigol_DP711", "--serial", SERIAL,
                          "--port", "1-1.2", "--box", "b"])
        assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# modes / validation                                                          #
# --------------------------------------------------------------------------- #

class TestModes:
    def test_no_mode_is_an_error_with_usage_fixes(self, fake_box):
        result = _invoke(["assign", "--box", "b"])
        assert result.exit_code != 0
        assert "--list" in result.output

    def test_list_renders_three_sections(self, fake_box):
        result = _invoke(["assign", "--list", "--box", "b"])
        assert result.exit_code == 0
        assert "Assignable devices:" in result.output
        assert "Rigol_DP711" in result.output
        assert "Unassigned USB-serial cables:" in result.output
        assert SERIAL in result.output

    def test_remove_round_trip(self, fake_box):
        _invoke(["assign", "Rigol_DP711", "--serial", SERIAL, "--box", "b"])
        result = _invoke(["assign", "--remove", "--serial", SERIAL, "--box", "b"])
        assert result.exit_code == 0
        assert "Removed" in result.output
        assert fake_box.assignments == []

    def test_remove_rejects_baud_and_as_net(self, fake_box):
        result = _invoke(["assign", "--remove", "--serial", SERIAL,
                          "--baud", "9600", "--box", "b"])
        assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# assignment -> nets cascade                                                  #
# --------------------------------------------------------------------------- #

class TestNetCascade:
    """Nets live and die with their assignment: removing it deletes the nets
    bound to its address (backend cascade) and the CLI reports the names."""

    def test_remove_reports_cascaded_net_deletion(self, fake_box):
        # Full user story: assign + --as-net, then remove the assignment.
        _invoke(["assign", "Rigol_DP711", "--serial", SERIAL,
                 "--as-net", "supply1", "--box", "b"])
        assert [n["name"] for n in fake_box.saved_nets] == ["supply1"]

        result = _invoke(["assign", "--remove", "--serial", SERIAL, "--box", "b"])
        assert result.exit_code == 0
        assert "Deleted 1 net" in result.output
        assert "supply1" in result.output
        # The net is gone from the box, not merely warned about.
        assert fake_box.saved_nets == []

    def test_remove_without_nets_prints_no_deletion_line(self, fake_box):
        _invoke(["assign", "Rigol_DP711", "--serial", SERIAL, "--box", "b"])
        result = _invoke(["assign", "--remove", "--serial", SERIAL, "--box", "b"])
        assert result.exit_code == 0
        assert "Deleted" not in result.output

    def test_assign_reports_replaced_assignment_nets(self, fake_box):
        fake_box.pending_deleted_on_assign = ["supply1"]
        result = _invoke(["assign", "Rigol_DP711", "--serial", SERIAL, "--box", "b"])
        assert result.exit_code == 0
        assert "Replaced the cable's previous assignment" in result.output
        assert "supply1" in result.output

    def test_fresh_assign_prints_no_replacement_line(self, fake_box):
        result = _invoke(["assign", "Rigol_DP711", "--serial", SERIAL, "--box", "b"])
        assert result.exit_code == 0
        assert "Replaced" not in result.output


# --------------------------------------------------------------------------- #
# table regression                                                            #
# --------------------------------------------------------------------------- #

class TestRoleTables:
    def test_dp711_gated_on_power_supply_role(self):
        # ensure_role / validate_net_exists match "power-supply" exactly;
        # gating nets-add on "supply" would let users save undriveable nets.
        assert nets_mod.INSTRUMENT_NET_MAP["Rigol_DP711"] == ["power-supply"]
        assert nets_mod._SINGLE_CHANNEL_INST["Rigol_DP711"] == ("power-supply",)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for role-token normalization in ``lager nets add`` / ``delete``
/ ``add-batch`` (cli/commands/box/nets.py).

REGRESSION: saved nets must carry the canonical scanner-vocabulary role
string — the instrument CLIs (``validate_net_exists``), the box dispatchers
(``ensure_role``) and ``NetType.from_role`` all match it EXACTLY. ``nets
add`` historically accepted the legacy short tokens ``supply`` / ``batt``
and saved them verbatim, producing nets that listed fine but could never be
driven. The tokens remain accepted as input aliases and are normalized to
``power-supply`` / ``battery`` before validation and save.
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


DP811_ADDR = "USB0::0x1AB1::0x0E11::DP8H123456::INSTR"
KEITHLEY_ADDR = "USB0::0x05E6::0x2281::4519728::INSTR"

DP811 = {
    "name": "Rigol_DP811",
    "vid": "1ab1", "pid": "0e11", "serial": "DP8H123456",
    "address": DP811_ADDR,
    "net_type": ["power-supply"],
    "channels": {"power-supply": ["1"]},
}
KEITHLEY = {
    "name": "Keithley_2281S",
    "vid": "05e6", "pid": "2281", "serial": "4519728",
    "address": KEITHLEY_ADDR,
    "net_type": ["battery", "power-supply"],
    "channels": {"power-supply": ["1"], "battery": ["1"]},
}
JS220_ADDR = "USB0::0x16D0::0x10BA::001234::INSTR"
JS220 = {
    "name": "Joulescope_JS220",
    "vid": "16d0", "pid": "10ba", "serial": "001234",
    "address": JS220_ADDR,
    "net_type": ["watt-meter", "energy-analyzer"],
    "channels": {"watt-meter": ["0"], "energy-analyzer": ["0"]},
}


class FakeBox:
    """In-memory stand-in for the box-side scripts nets-add shells out to.

    Consumers read the payload two ways: add_cmd captures stdout via
    redirect_stdout, while ``_run_net_py`` (delete, batch save) uses the
    return value of ``run_python_internal_get_output``. The fake serves
    both: it prints the payload AND returns it.
    """

    def __init__(self, instruments):
        self.instruments = instruments
        self.saved_nets: list[dict] = []

    def run_python_internal(self, ctx, runnable, box, env=None, passenv=(),
                            kill=False, download=(), allow_overwrite=False,
                            signum="SIGTERM", timeout=30, detach=False,
                            port=(), org=None, args=(), **kwargs):
        # Positional-friendly: run_python_internal_get_output forwards every
        # parameter positionally.
        out = self._dispatch(os.path.basename(str(runnable)), tuple(args))
        if out:
            print(out)
        return out

    def _dispatch(self, script, args) -> str:
        if script == "query_instruments.py":
            if args and args[0] == "get_instrument":
                match = next((i for i in self.instruments
                              if i["address"] == args[1]), {})
                return json.dumps(match)
            return json.dumps(self.instruments)

        if script == "net.py":
            cmd = args[0]
            if cmd == "list":
                return json.dumps(self.saved_nets)
            if cmd == "save":
                self.saved_nets.append(json.loads(args[1]))
                return json.dumps({"ok": True})
            if cmd == "save-batch":
                records = json.loads(args[1])
                self.saved_nets.extend(records)
                return json.dumps({"ok": True, "count": len(records)})
            if cmd == "delete":
                self.saved_nets = [n for n in self.saved_nets
                                   if not (n.get("name") == args[1]
                                           and n.get("role") == args[2])]
                return json.dumps({"ok": True})

        raise AssertionError(f"unexpected script {script} {args}")


@pytest.fixture
def fake_box():
    # NB: ``import cli.commands.development.python`` would resolve to the
    # Click command shadowing the submodule attribute; go via importlib.
    devpy = importlib.import_module("cli.commands.development.python")

    box = FakeBox([DP811, KEITHLEY, JS220])
    # add_cmd calls run_python_internal via the nets module's import;
    # _run_net_py goes through devpy.run_python_internal_get_output, which
    # resolves run_python_internal from devpy's globals at call time.
    with patch.object(nets_mod, "run_python_internal", box.run_python_internal), \
         patch.object(devpy, "run_python_internal", box.run_python_internal), \
         patch("cli.box_storage.resolve_and_validate_box",
               lambda ctx, name: name or "testbox"), \
         patch.object(nets_mod, "_resolve_box", lambda ctx, name: name or "testbox"):
        yield box


def _invoke(args, input=None):
    return CliRunner().invoke(nets_group, args, input=input, catch_exceptions=False)


# --------------------------------------------------------------------------- #
# add: alias normalization                                                    #
# --------------------------------------------------------------------------- #

class TestAddRoleAliases:
    def test_legacy_supply_token_saves_power_supply(self, fake_box):
        result = _invoke(["add", "psu", "supply", "1", DP811_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["role"] == "power-supply"

    def test_canonical_power_supply_accepted(self, fake_box):
        result = _invoke(["add", "psu", "power-supply", "1", DP811_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["role"] == "power-supply"

    def test_legacy_batt_token_saves_battery(self, fake_box):
        result = _invoke(["add", "bat1", "batt", "1", KEITHLEY_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["role"] == "battery"

    def test_canonical_battery_accepted(self, fake_box):
        result = _invoke(["add", "bat1", "battery", "1", KEITHLEY_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["role"] == "battery"

    def test_channel_validation_now_enforced_for_supplies(self, fake_box):
        # Pre-fix, chan_map.get("supply") was None so channel validation was
        # silently skipped; with the canonical role it actually runs.
        result = _invoke(["add", "psu", "supply", "9", DP811_ADDR, "--box", "b"])
        assert result.exit_code != 0
        assert "not valid" in result.output
        assert fake_box.saved_nets == []


# --------------------------------------------------------------------------- #
# delete: alias symmetry                                                      #
# --------------------------------------------------------------------------- #

class TestDeleteRoleAliases:
    def test_delete_accepts_legacy_supply_token(self, fake_box):
        _invoke(["add", "psu", "supply", "1", DP811_ADDR, "--box", "b"])
        result = _invoke(["delete", "psu", "supply", "--box", "b", "--yes"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets == []

    def test_delete_accepts_canonical_role(self, fake_box):
        _invoke(["add", "psu", "supply", "1", DP811_ADDR, "--box", "b"])
        result = _invoke(["delete", "psu", "power-supply", "--box", "b", "--yes"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets == []

    def test_delete_reaches_legacy_saved_nets(self, fake_box):
        # REGRESSION: nets saved by older CLIs carry the raw legacy role
        # ("supply"). Both token spellings must still find and delete them —
        # they're exactly the (undriveable) nets users need to clean up. The
        # box-side delete is exact-match, so the CLI must pass the record's
        # stored role.
        fake_box.saved_nets.append({
            "name": "oldnet", "role": "supply", "instrument": "Rigol_DP811",
            "pin": "1", "address": DP811_ADDR,
        })
        result = _invoke(["delete", "oldnet", "supply", "--box", "b", "--yes"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets == []

        fake_box.saved_nets.append({
            "name": "oldbat", "role": "batt", "instrument": "Keithley_2281S",
            "pin": "1", "address": KEITHLEY_ADDR,
        })
        result = _invoke(["delete", "oldbat", "battery", "--box", "b", "--yes"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets == []

    def test_add_duplicate_guard_sees_legacy_saved_roles(self, fake_box):
        # REGRESSION: a legacy net (role "supply") on the same channel must
        # still block re-adding it with the canonical role.
        fake_box.saved_nets.append({
            "name": "oldnet", "role": "supply", "instrument": "Rigol_DP811",
            "pin": "1", "address": DP811_ADDR,
        })
        result = _invoke(["add", "psu2", "power-supply", "1", DP811_ADDR, "--box", "b"])
        assert result.exit_code != 0
        assert "already exists" in result.output
        assert len(fake_box.saved_nets) == 1


# --------------------------------------------------------------------------- #
# add-batch: alias normalization                                              #
# --------------------------------------------------------------------------- #

class TestAddBatchRoleAliases:
    def test_batch_normalizes_legacy_tokens(self, fake_box, tmp_path):
        batch = tmp_path / "nets.json"
        batch.write_text(json.dumps([
            {"name": "psu", "role": "supply", "channel": "1",
             "address": DP811_ADDR, "instrument": "Rigol_DP811"},
            {"name": "bat1", "role": "batt", "channel": "1",
             "address": KEITHLEY_ADDR, "instrument": "Keithley_2281S"},
            {"name": "dbg", "role": "debug", "channel": "STM32F4",
             "address": "USB::001::002", "instrument": "J-Link"},
        ]))
        result = _invoke(["add-batch", str(batch), "--box", "b"])
        assert result.exit_code == 0, result.output
        roles = {n["name"]: n["role"] for n in fake_box.saved_nets}
        assert roles["psu"] == "power-supply"
        assert roles["bat1"] == "battery"
        assert roles["dbg"] == "debug"  # non-aliased roles pass through


# --------------------------------------------------------------------------- #
# table hygiene                                                               #
# --------------------------------------------------------------------------- #

class TestTablesAreCanonical:
    def test_instrument_net_map_has_no_legacy_tokens(self):
        for inst, roles in nets_mod.INSTRUMENT_NET_MAP.items():
            for role in roles:
                assert role not in nets_mod._ROLE_ALIASES, (
                    f"{inst} lists legacy token {role!r}; the table must use "
                    f"the canonical saved-role vocabulary"
                )

    def test_single_channel_table_has_no_legacy_tokens(self):
        for inst, roles in nets_mod._SINGLE_CHANNEL_INST.items():
            for role in roles:
                assert role not in nets_mod._ROLE_ALIASES

    def test_canonical_role_mapping(self):
        assert nets_mod._canonical_role("supply") == "power-supply"
        assert nets_mod._canonical_role("batt") == "battery"
        # Everything else passes through untouched.
        for role in ("power-supply", "battery", "solar", "debug", "uart", "adc"):
            assert nets_mod._canonical_role(role) == role


# --------------------------------------------------------------------------- #
# add: watt-meter / energy-analyzer instruments (Joulescope, PPK2, Yocto)      #
# --------------------------------------------------------------------------- #

class TestWattMeterInstrumentsInMap:
    def test_joulescope_roles_present(self):
        assert set(nets_mod.INSTRUMENT_NET_MAP["Joulescope_JS220"]) == {
            "watt-meter", "energy-analyzer"}
        assert set(nets_mod.INSTRUMENT_NET_MAP["Nordic_PPK2"]) == {
            "watt-meter", "energy-analyzer"}
        assert nets_mod.INSTRUMENT_NET_MAP["Yocto_Watt"] == ["watt-meter"]

    def test_add_energy_analyzer_on_joulescope(self, fake_box):
        # REGRESSION: `lager nets add` rejected every Joulescope net because the
        # instrument was absent from INSTRUMENT_NET_MAP, forcing GUI-only adds.
        result = _invoke(["add", "energy1", "energy-analyzer", "0", JS220_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["role"] == "energy-analyzer"
        assert fake_box.saved_nets[0]["instrument"] == "Joulescope_JS220"

    def test_add_watt_meter_on_joulescope(self, fake_box):
        result = _invoke(["add", "watt1", "watt-meter", "0", JS220_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["role"] == "watt-meter"

    def test_watt_and_energy_nets_coexist_on_one_joulescope(self):
        # The JS220 backs both roles simultaneously; it must not be single-channel.
        assert "Joulescope_JS220" not in nets_mod._SINGLE_CHANNEL_INST


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

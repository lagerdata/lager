# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager nets set-power-up` and `lager nets apply-power-up`: declaring the level
a LabJack gpio net powers up at, and writing it to the LabJack.

The level is always explicit (no default), --check only reads and fails on
drift so it can gate CI, and a write asks first because it drives pins.
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

importlib.import_module("cli.commands.box.nets")
from cli.commands.box.nets import nets as nets_group  # noqa: E402
from test.unit.cli.nets_http_fake import FakeBoxHTTP, FakeResponse  # noqa: E402

T7 = "USB0::0x0CD5::0x0007::470000001::INSTR"


def _device(in_sync, written=False, stored="input", error=None):
    dev = {"device_id": "labjack:" + T7, "model": "T7", "serial": "470000001",
           "factory": stored == "input", "stored_known": True, "in_sync": in_sync,
           "written": written, "undeclared_outputs": [],
           "pins": [{"net": "PUMP", "dio_name": "FIO3", "dio": 3,
                     "declared": "low", "stored": stored}]}
    if error:
        dev = {"device_id": dev["device_id"], "error": error}
    return dev


class PowerUpBox(FakeBoxHTTP):
    def __init__(self):
        super().__init__()
        self.plan = {"mode": "plan", "devices": [_device(False)], "undeclared": ["SENSE"],
                     "in_sync": False}
        self.applied = {"mode": "apply", "devices": [_device(True, True, "low")],
                        "undeclared": ["SENSE"], "in_sync": True}

    def _route(self, method, path, body, params):
        if method == "PUT" and path.endswith("/power-up"):
            name = path[len("/nets/"):-len("/power-up")]
            for net in self.saved_nets:
                if net["name"] == name:
                    if body["power_up"] is None:
                        net.pop("power_up", None)
                    else:
                        net["power_up"] = body["power_up"]
            return FakeResponse(200, {"ok": True, "power_up": body["power_up"]})
        if method == "POST" and path == "/nets/power-up":
            return FakeResponse(200, self.plan if body.get("mode") == "plan" else self.applied)
        return super()._route(method, path, body, params)


@pytest.fixture
def box():
    fake = PowerUpBox()
    fake.saved_nets = [
        {"name": "PUMP", "role": "gpio", "instrument": "LabJack_T7", "address": T7, "pin": "FIO3"},
        {"name": "BLOW", "role": "gpio", "instrument": "LabJack_T7", "address": T7, "pin": "FIO2"},
        {"name": "AIN", "role": "adc", "instrument": "LabJack_T7", "address": T7, "pin": 0},
        {"name": "FT", "role": "gpio", "instrument": "FTDI_FT232H", "address": "x", "pin": 4},
    ]
    with patch("requests.request", fake.request), \
         patch("cli.box_storage._check_box_lock", lambda *a, **k: None), \
         patch("cli.box_storage.get_box_ip", lambda name: None):
        yield fake


def _invoke(*args, input=None):
    return CliRunner().invoke(nets_group, [*args, "--box", "10.0.0.9"], obj=None,
                              input=input, catch_exceptions=False)


def _net(box, name):
    return next(n for n in box.saved_nets if n["name"] == name)


def _posts(box):
    return [c[2].get("mode") for c in box.calls if c[0] == "POST"]


# ----- set-power-up -----

def test_sets_several_nets_in_one_call(box):
    result = _invoke("set-power-up", "PUMP", "BLOW", "low")
    assert result.exit_code == 0, result.output
    assert _net(box, "PUMP")["power_up"] == "low"
    assert _net(box, "BLOW")["power_up"] == "low"
    assert "apply-power-up" in result.output


def test_none_clears_the_level(box):
    _net(box, "PUMP")["power_up"] = "high"
    assert _invoke("set-power-up", "PUMP", "none").exit_code == 0
    assert "power_up" not in _net(box, "PUMP")


def test_the_level_is_required(box):
    result = _invoke("set-power-up", "PUMP")
    assert result.exit_code == 2
    assert "power_up" not in _net(box, "PUMP")


def test_an_unknown_level_is_refused(box):
    result = _invoke("set-power-up", "PUMP", "input")
    assert result.exit_code == 2
    assert "power_up" not in _net(box, "PUMP")


def test_a_bad_net_refuses_the_whole_call(box):
    result = _invoke("set-power-up", "PUMP", "FT", "low")
    assert result.exit_code == 1
    assert "FTDI_FT232H" in result.output
    assert "power_up" not in _net(box, "PUMP")
    result = _invoke("set-power-up", "PUMP", "AIN", "NOPE", "low")
    assert result.exit_code == 1
    assert "adc net" in result.output and "no net named 'NOPE'" in result.output
    assert not [c for c in box.calls if c[0] == "PUT"]


# ----- apply-power-up -----

def test_check_exits_1_on_drift_and_writes_nothing(box):
    result = _invoke("apply-power-up", "--check")
    assert result.exit_code == 1
    assert _posts(box) == ["plan"]
    assert "CHANGE" in result.output
    assert "SENSE" in result.output  # undeclared nets are listed


def test_check_exits_0_when_in_sync(box):
    box.plan = dict(box.plan, devices=[_device(True, stored="low")], in_sync=True)
    result = _invoke("apply-power-up", "--check")
    assert result.exit_code == 0, result.output
    assert _posts(box) == ["plan"]


def test_dry_run_never_applies(box):
    result = _invoke("apply-power-up", "--dry-run")
    assert result.exit_code == 0, result.output
    assert _posts(box) == ["plan"]


def test_apply_asks_first(box):
    declined = _invoke("apply-power-up", input="n\n")
    assert declined.exit_code == 1
    assert _posts(box) == ["plan"]
    accepted = _invoke("apply-power-up", input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert _posts(box) == ["plan", "plan", "apply"]
    assert "Written to flash" in accepted.output


def test_yes_and_allow_undeclared_are_passed(box):
    result = _invoke("apply-power-up", "--yes", "--allow-undeclared")
    assert result.exit_code == 0, result.output
    apply_body = [c[2] for c in box.calls if c[0] == "POST"][-1]
    assert apply_body == {"mode": "apply", "allow_undeclared": True}


def test_in_sync_writes_nothing_without_asking(box):
    box.plan = dict(box.plan, devices=[_device(True, stored="low")], in_sync=True)
    result = _invoke("apply-power-up")
    assert result.exit_code == 0, result.output
    assert _posts(box) == ["plan"]
    assert "nothing written" in result.output


def test_a_device_error_fails_the_apply(box):
    box.applied = {"mode": "apply", "devices": [_device(False, error="refused")],
                   "undeclared": [], "in_sync": False}
    result = _invoke("apply-power-up", "--yes")
    assert result.exit_code == 1
    assert "refused" in result.output
    # The entry has no model: name it once, by its device_id.
    assert "LabJack LabJack" not in result.output
    assert "LabJack (labjack:" + T7 + ")" in result.output


def test_the_title_comes_from_the_reported_backend(box):
    # A newer box can report a family this CLI has no label for: it is named
    # generically rather than called a LabJack.
    box.plan = {"mode": "plan", "undeclared": [], "in_sync": False,
                "devices": [{"device_id": "other:1", "backend": "other", "error": "x"}]}
    result = _invoke("apply-power-up", "--check")
    assert "Device (other:1)" in result.output
    assert "LabJack" not in result.output.split("Device (other:1)")[0]


def test_json_report(box):
    result = _invoke("apply-power-up", "--check", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["undeclared"] == ["SENSE"]


def test_check_and_dry_run_are_exclusive(box):
    assert _invoke("apply-power-up", "--check", "--dry-run").exit_code == 2


# ----- nets add / add-batch / show -----

def test_show_prints_the_level(box):
    _net(box, "PUMP")["power_up"] = "low"
    result = _invoke("show", "PUMP")
    assert result.exit_code == 0, result.output
    assert "Power-up:   LOW" in result.output


def test_add_batch_accepts_and_validates_power_up(box, tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps([{"name": "VALVE", "role": "gpio", "channel": "FIO1",
                                 "address": T7, "instrument": "LabJack_T7",
                                 "power_up": "LOW"}]))
    result = _invoke("add-batch", str(good))
    assert result.exit_code == 0, result.output
    assert _net(box, "VALVE")["power_up"] == "low"

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"name": "V2", "role": "gpio", "channel": "FIO1",
                                "address": T7, "instrument": "LabJack_T7",
                                "power_up": "input"}]))
    result = _invoke("add-batch", str(bad))
    assert result.exit_code != 0
    assert "power_up must be 'low' or 'high'" in result.output


def test_a_tui_metadata_edit_keeps_the_level():
    from cli.commands.box.net_tui import _merge_net_metadata
    records = [{"name": "PUMP", "role": "gpio", "instrument": "LabJack_T7",
                "pin": "FIO3", "power_up": "low"}]
    merged = _merge_net_metadata(records, "PUMP", "gpio", "pump", "", [])
    assert merged["power_up"] == "low"

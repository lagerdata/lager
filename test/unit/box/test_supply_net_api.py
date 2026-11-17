# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The supply, battery and e-load surface a ``Net.get`` script reaches.

Three defects, each invisible until a script ran on a box:

* A channel-less ``disable_ocp()`` / ``disable_ovp()`` / ``get_monitor_state()``
  sent channel-less SCPI, which a multi-output supply applies to whichever
  output is selected on its front panel (#846).
* ``EAMapper`` called driver methods that do not exist, so every EA call
  through ``Net.get`` died with "Function not found"; the EA driver itself
  called helpers it never defined (#843).
* Getters printed in the hardware-service process and returned ``None`` to the
  script (#845).
"""

import ast
import pathlib
from unittest.mock import patch

import pytest

from lager.power.supply.rigol_dp800 import RigolDP800
from lager.power.supply.ea import EA
from lager.power.battery.keithley import KeithleyBattery
from lager.power.eload.rigol_dl3021 import RigolDL3021

BOX = pathlib.Path(__file__).resolve().parents[3] / "box" / "lager"
EA_MAPPER_PY = BOX / "nets" / "mappers" / "ea.py"


def _bare(cls):
    """A driver instance with no connection, for method-level tests."""
    return object.__new__(cls)


class FakeInstr:
    def __init__(self, answers=None):
        self.writes = []
        self.answers = answers or {}

    def write(self, cmd, *a, **k):
        self.writes.append(cmd)

    def query(self, cmd, *a, **k):
        return self.answers.get(cmd, "0")


# --------------------------------------------------------------------------
# #846: a channel-less call acts on the net's own channel
# --------------------------------------------------------------------------

def _dp800_on_ch2():
    drv = _bare(RigolDP800)
    drv.instr = FakeInstr()
    drv.channel = 2
    return drv


def test_disable_ocp_without_channel_targets_the_nets_channel():
    drv = _dp800_on_ch2()
    drv.disable_ocp()
    assert drv.instr.writes == [":OUTP:OCP CH2,OFF"]


def test_disable_ovp_without_channel_targets_the_nets_channel():
    drv = _dp800_on_ch2()
    drv.disable_ovp()
    assert drv.instr.writes == [":OUTP:OVP CH2,OFF"]


def test_explicit_channel_still_wins():
    drv = _dp800_on_ch2()
    drv.disable_ocp(1)
    assert drv.instr.writes == [":OUTP:OCP CH1,OFF"]


def test_get_monitor_state_without_channel_reads_the_nets_channel():
    drv = _dp800_on_ch2()
    seen = []

    def record(name):
        def f(*args, **kwargs):
            seen.append((name, args, kwargs))
            return 1.0
        return f

    for name in ("measure_voltage", "measure_current", "measure_power",
                 "get_overcurrent_protection_value",
                 "get_overvoltage_protection_value",
                 "overcurrent_protection_is_tripped",
                 "overvoltage_protection_is_tripped", "get_output_mode",
                 "get_channel_voltage", "get_channel_current",
                 "get_channel_limits"):
        setattr(drv, name, record(name))
    drv.output_state = lambda channel=None: seen.append(("output_state", (channel,), {}))
    drv.get_monitor_state()
    assert seen, "no reads recorded"
    for name, args, kwargs in seen:
        channel = args[0] if args else kwargs.get("source", kwargs.get("channel"))
        assert channel == 2, f"{name} read channel {channel!r}, not the net's channel 2"


# --------------------------------------------------------------------------
# #843: every EAMapper call target exists on the EA driver
# --------------------------------------------------------------------------

def _ea_mapper_device_calls():
    """Names the mapper calls on its device, via self.device.X or self.X."""
    tree = ast.parse(EA_MAPPER_PY.read_text())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "EAMapper")
    defined = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
    defined |= {t.id for n in cls.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    called = set()
    for node in ast.walk(cls):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func.value
        if (isinstance(target, ast.Attribute) and target.attr == "device"
                and isinstance(target.value, ast.Name) and target.value.id == "self"):
            called.add(node.func.attr)
        elif isinstance(target, ast.Name) and target.id == "self":
            if node.func.attr not in defined:
                called.add(node.func.attr)
    return called


def test_the_ea_walker_found_something():
    assert len(_ea_mapper_device_calls()) >= 10, _ea_mapper_device_calls()


@pytest.mark.parametrize("name", sorted(_ea_mapper_device_calls()))
def test_ea_mapper_targets_exist_on_the_driver(name):
    assert callable(getattr(EA, name, None)), (
        f"EAMapper calls {name}() on the EA driver, which does not define it; "
        f"over Net.get it fails as 'Function not found: {name}'."
    )


@pytest.mark.parametrize("name", ["enable_output", "disable_output"])
def test_ea_driver_has_the_net_output_calls(name):
    # Net.enable()/Net.disable() call these on every non-Keithley supply.
    assert callable(getattr(EA, name, None))


def _ea_with_replies(replies):
    drv = _bare(EA)
    drv.channel = 1
    drv._safe_query = lambda cmd, default="n/a": replies.get(cmd, default)
    drv._write = lambda cmd: None
    return drv


def test_ea_reads_do_not_raise_and_unknown_is_none():
    drv = _ea_with_replies({"OUTPut?": "OFF", "MEASure:SCALar:VOLTage:DC?": "12.00 V"})
    assert drv.measure_voltage() == 12.0
    # A failed query ("n/a") is unknown, not zero.
    assert drv.measure_current() is None
    assert drv.overcurrent_protection_is_tripped() is False
    assert drv.overvoltage_protection_is_tripped(1) is False


def test_ea_state_returns_a_dict():
    drv = _ea_with_replies({"OUTPut?": "OFF"})
    with patch("time.sleep"):
        report = drv.state()
        full = drv.get_full_state()
    for r in (report, full):
        assert isinstance(r, dict)
        assert r["channel"] == 1
        assert r["enabled"] is False


# --------------------------------------------------------------------------
# #845: getters return their value
# --------------------------------------------------------------------------

def test_dp800_state_returns_the_monitor_dict():
    drv = _dp800_on_ch2()
    drv.instr = FakeInstr({":OUTP? CH2": "ON"})
    report = drv.state()
    assert isinstance(report, dict)
    assert report["channel"] == 2
    for key in ("voltage", "current", "enabled", "ocp_limit", "ovp_tripped"):
        assert key in report


def _battery_with_replies(replies):
    drv = _bare(KeithleyBattery)
    drv._safe_query = lambda cmd, default="n/a": replies.get(cmd, default)
    return drv


@pytest.mark.parametrize("getter,cmd,reply,expected", [
    ("soc", ":BATT:SIM:SOC?", "80", 80.0),
    ("voc", ":BATT:SIM:VOC?", "3.7", 3.7),
    ("voltage_full", ":BATT:SIM:VOC:FULL?", "4.2", 4.2),
    ("voltage_empty", ":BATT:SIM:VOC:EMPT?", "3.0", 3.0),
    ("capacity", ":BATT:SIM:CAP:LIM?", "2.5", 2.5),
    ("current_limit", ":BATT:SIM:CURR:LIM?", "1.0", 1.0),
    ("ovp", ":BATT:SIM:TVOL:PROT?", "4.5", 4.5),
    ("ocp", ":BATT:SIM:CURR:PROT?", "2.0", 2.0),
])
def test_battery_getters_return_the_reading(getter, cmd, reply, expected):
    drv = _battery_with_replies({cmd: reply})
    assert getattr(drv, getter)() == expected


def test_battery_failed_read_is_none_not_zero():
    drv = _battery_with_replies({})
    assert drv.soc() is None


def test_battery_mode_and_model_return_strings():
    drv = _battery_with_replies({":BATT:SIM:METH?": "STAT"})
    assert drv.mode() == "static"
    drv.current_model = lambda: "DEFAULT"
    assert drv.model() == "DEFAULT"


def test_battery_print_state_returns_the_monitor_dict():
    drv = _battery_with_replies({})
    drv._is_batt_output_on = lambda: False
    drv._mode_string = lambda: "static"
    drv.current_model = lambda: "DEFAULT"
    drv.get_monitor_state = lambda channel=None: {"soc": 50.0}
    assert drv.print_state() == {"soc": 50.0}


def test_eload_print_state_returns_the_state_dict():
    drv = _bare(RigolDL3021)
    drv.mode = lambda: "CC"
    drv.get_input_state = lambda: True
    drv.input_state = lambda: True
    drv.measured_voltage = lambda: 5.0
    drv.measured_current = lambda: 0.5
    drv.measured_power = lambda: 2.5
    drv.current = lambda: 0.5
    report = drv.print_state()
    assert report == drv.get_state_dict()
    assert report["mode"] == "CC"

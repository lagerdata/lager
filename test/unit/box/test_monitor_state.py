# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the single-call TUI monitor-state helpers.

The supply/battery WebSocket monitors used to issue ~12-17 separate
hardware_service ``/invoke`` calls per poll tick, each taking the shared
per-device lock — saturating slow instruments and starving interactive
TUI commands. ``SupplyNet.get_monitor_state`` (with a non-intrusive
Keithley 2281S override) and ``KeithleyBattery.get_monitor_state``
collapse a tick into one call. ``describe_error`` fixes the empty
"Hardware service unreachable: " detail (ConnectionFailed is raised bare).

Modules are spec-loaded with stubbed ``lager.*`` parents so the tests run
in the CLI test environment without box-only dependencies (pyvisa etc.),
following the pattern in test_lock_state.py.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)


def _load_module(name, relpath, stubs=None):
    """Spec-load a box module with stubbed dependency modules."""
    for stub_name, stub in (stubs or {}).items():
        sys.modules.setdefault(stub_name, stub)
    path = os.path.join(_REPO_ROOT, *relpath.split("/"))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _lager_exceptions_stub():
    mod = types.ModuleType("lager.exceptions")
    for cls in ("SupplyBackendError", "LibraryMissingError", "DeviceNotFoundError",
                "BatteryBackendError"):
        setattr(mod, cls, type(cls, (Exception,), {}))
    lager_pkg = types.ModuleType("lager")
    lager_pkg.exceptions = mod
    return {"lager": lager_pkg, "lager.exceptions": mod}


@pytest.fixture(scope="module")
def supply_net():
    return _load_module(
        "supply_net_under_test",
        "box/lager/power/supply/supply_net.py",
        stubs=_lager_exceptions_stub(),
    )


# ---------------------------------------------------------------------------
# SupplyNet.get_monitor_state (default composition)
# ---------------------------------------------------------------------------


def _fake_supply(supply_net, **overrides):
    """Concrete SupplyNet with per-field methods; overridable per test."""

    class FakeSupply(supply_net.SupplyNet):
        calls = []

        # abstract interface — irrelevant for these tests
        def voltage(self, value=None, ocp=None, ovp=None): pass
        def current(self, value=None, ocp=None, ovp=None): pass
        def enable(self): pass
        def disable(self): pass
        def set_mode(self): pass
        def state(self): pass
        def clear_ocp(self): pass
        def clear_ovp(self): pass
        def ocp(self, value=None): pass
        def ovp(self, value=None): pass

        # monitor surface
        def measure_voltage(self, channel=None): return 3.3
        def measure_current(self, channel=None): return 0.5
        def measure_power(self, channel=None): return 1.65
        def output_is_enabled(self, channel=None): return True
        def get_output_mode(self, channel=None): return "CC"
        def get_channel_voltage(self, source=None): return 3.4
        def get_channel_current(self, source=None): return 0.6
        def get_channel_limits(self, channel=None):
            return {"voltage_max": 30.0, "current_max": 5.0}
        def get_overcurrent_protection_value(self, channel=None): return 4.0
        def overcurrent_protection_is_tripped(self, channel=None): return False
        def get_overvoltage_protection_value(self, channel=None): return 31.0
        def overvoltage_protection_is_tripped(self, channel=None): return True

    for name, fn in overrides.items():
        setattr(FakeSupply, name, fn)
    return FakeSupply()


class TestSupplyNetGetMonitorState:
    def test_full_wire_shape(self, supply_net):
        state = _fake_supply(supply_net).get_monitor_state(channel=1)
        assert state == {
            "voltage": 3.3, "current": 0.5, "power": 1.65,
            "enabled": True, "mode": "CC",
            "voltage_set": 3.4, "current_set": 0.6,
            "voltage_max": 30.0, "current_max": 5.0,
            "ocp_limit": 4.0, "ocp_tripped": False,
            "ovp_limit": 31.0, "ovp_tripped": True,
        }

    def test_missing_limits_fall_back_to_zero(self, supply_net):
        def boom(self, channel=None):
            raise NotImplementedError
        state = _fake_supply(supply_net, get_channel_limits=boom).get_monitor_state(1)
        assert state["voltage_max"] == 0
        assert state["current_max"] == 0

    def test_missing_protection_falls_back_to_none(self, supply_net):
        def boom(self, channel=None):
            raise AttributeError
        state = _fake_supply(
            supply_net,
            get_overcurrent_protection_value=boom,
            get_overvoltage_protection_value=boom,
        ).get_monitor_state(1)
        assert state["ocp_limit"] is None
        assert state["ocp_tripped"] is None
        assert state["ovp_limit"] is None
        assert state["ovp_tripped"] is None

    def test_no_mode_method_defaults_to_cv(self, supply_net):
        fake = _fake_supply(supply_net)
        # Simulate a driver without get_output_mode.
        delattr(type(fake), "get_output_mode")
        assert fake.get_monitor_state(1)["mode"] == "CV"


# ---------------------------------------------------------------------------
# Keithley 2281S override: non-intrusive (no-mode queries only)
# ---------------------------------------------------------------------------


class TestKeithleyMonitorStateIsNonIntrusive:
    """The override must never issue mode-enforcing queries: in battery
    mode those fail at VISA-timeout speed and blow the Device proxy's
    HTTP budget (the original supply-TUI failure)."""

    @pytest.fixture()
    def keithley_cls(self, supply_net):
        stubs = _lager_exceptions_stub()
        wrap = types.ModuleType("lager.instrument_wrappers.instrument_wrap")
        wrap.InstrumentWrapKeithley = object
        pkg = types.ModuleType("lager.instrument_wrappers")
        stubs.update({
            "lager.instrument_wrappers": pkg,
            "lager.instrument_wrappers.instrument_wrap": wrap,
        })
        # keithley.py does `from .supply_net import ...`; loading it as a
        # standalone module needs the relative import resolvable. Register
        # a tiny package whose supply_net is the already-loaded module.
        pkg_name = "ks_pkg"
        package = types.ModuleType(pkg_name)
        package.__path__ = [os.path.join(_REPO_ROOT, "box/lager/power/supply")]
        sys.modules[pkg_name] = package
        sys.modules[pkg_name + ".supply_net"] = supply_net
        for stub_name, stub in stubs.items():
            sys.modules.setdefault(stub_name, stub)
        path = os.path.join(_REPO_ROOT, "box/lager/power/supply/keithley.py")
        spec = importlib.util.spec_from_file_location(pkg_name + ".keithley", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name + ".keithley"] = mod
        spec.loader.exec_module(mod)
        return mod.Keithley2281S

    def test_uses_only_no_mode_queries(self, keithley_cls):
        drv = object.__new__(keithley_cls)
        queries = []

        def no_mode(cmd, default="n/a"):
            queries.append(cmd)
            return {
                ":OUTP?": "1",
                ":SOUR1:VOLT?": "3.3",
                ":SOUR1:CURR?": "0.5",
                ":MEAS:VOLT?": "3.29",
                ":MEAS:CURR?": "0.49",
                ":SOUR1:CURR:PROT?": "4.0",
                ":SOUR1:VOLT:PROT?": "20.0",
                ":OUTP:PROT:TRIP?": "OVP",
            }.get(cmd, default)

        drv._safe_query_no_mode = no_mode
        drv._determine_operating_mode_no_mode = lambda: "CV"

        def forbidden(*a, **k):
            raise AssertionError("mode-enforcing query used by monitor path")
        drv._safe_query = forbidden
        drv._ensure_ps_mode = forbidden

        state = drv.get_monitor_state()
        assert state["enabled"] is True
        assert state["voltage"] == pytest.approx(3.29)
        assert state["voltage_set"] == pytest.approx(3.3)
        assert state["ovp_tripped"] is True
        assert state["ocp_tripped"] is False
        assert state["voltage_max"] == 20.0
        assert state["current_max"] == 6.0
        # Every query went through the no-mode path.
        assert all(q.startswith(":") for q in queries)

    def test_disabled_output_uses_setpoints(self, keithley_cls):
        drv = object.__new__(keithley_cls)
        drv._safe_query_no_mode = lambda cmd, default="n/a": {
            ":OUTP?": "0",
            ":SOUR1:VOLT?": "5.0",
            ":SOUR1:CURR?": "1.0",
            ":SOUR1:CURR:PROT?": "2.0",
            ":SOUR1:VOLT:PROT?": "21.0",
            ":OUTP:PROT:TRIP?": "",
        }.get(cmd, default)
        drv._determine_operating_mode_no_mode = lambda: (_ for _ in ()).throw(
            AssertionError("mode determination must be skipped when output is off"))

        state = drv.get_monitor_state()
        assert state["enabled"] is False
        assert state["voltage"] == 5.0
        assert state["mode"] == "CV"
        assert state["power"] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# KeithleyBattery.get_monitor_state: defensive numeric parsing
# ---------------------------------------------------------------------------


class TestBatteryMonitorStateParsing:
    @pytest.fixture()
    def battery_cls(self):
        stubs = _lager_exceptions_stub()
        wrap = types.ModuleType("lager.instrument_wrappers.instrument_wrap")
        wrap.InstrumentWrapKeithley = object
        defines = types.ModuleType("lager.instrument_wrappers.keithley_defines")
        defines.Mode = types.SimpleNamespace(BatterySimulator=types.SimpleNamespace(to_cmd=lambda: "BATT"))
        defines.SimMethod = object
        util = types.ModuleType("lager.instrument_wrappers.util")
        util.InvalidEnumError = type("InvalidEnumError", (Exception,), {})
        pkg = types.ModuleType("lager.instrument_wrappers")
        stubs.update({
            "lager.instrument_wrappers": pkg,
            "lager.instrument_wrappers.instrument_wrap": wrap,
            "lager.instrument_wrappers.keithley_defines": defines,
            "lager.instrument_wrappers.util": util,
        })
        for stub_name, stub in stubs.items():
            sys.modules.setdefault(stub_name, stub)

        pkg_name = "kb_pkg"
        package = types.ModuleType(pkg_name)
        package.__path__ = [os.path.join(_REPO_ROOT, "box/lager/power/battery")]
        sys.modules[pkg_name] = package
        bn = _load_module(pkg_name + ".battery_net", "box/lager/power/battery/battery_net.py")
        sys.modules[pkg_name + ".battery_net"] = bn

        path = os.path.join(_REPO_ROOT, "box/lager/power/battery/keithley.py")
        spec = importlib.util.spec_from_file_location(pkg_name + ".keithley", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg_name + ".keithley"] = mod
        spec.loader.exec_module(mod)
        return mod.KeithleyBattery

    def test_odd_reply_formats_parse_instead_of_failing_the_tick(self, battery_cls):
        drv = object.__new__(battery_cls)
        drv._is_batt_output_on = lambda: True
        drv._mode_string = lambda: "Dynamic"
        replies = {
            ":BATT:SIM:TVOL?": "3.3 V",                       # trailing unit
            ":BATT:SIM:CURR?": "+1.500000E+00A,+3.2E+00V",    # multi-value
            ":BATT:SIM:RES?": "garbage",                       # unparseable
            ":BATT:SIM:SOC?": "+3.000000E+01",                 # sci-notation
        }
        drv._safe_query = lambda cmd, default="": replies.get(cmd, default)

        state = drv.get_monitor_state()
        assert state["terminal_voltage"] == pytest.approx(3.3)
        assert state["current"] == pytest.approx(1.5)
        assert state["esr"] == pytest.approx(0.067)   # falls back to default
        assert state["soc"] == pytest.approx(30.0)
        assert state["enabled"] is True
        assert state["mode"] == "Dynamic"

    def test_clean_replies_unchanged(self, battery_cls):
        drv = object.__new__(battery_cls)
        drv._is_batt_output_on = lambda: False
        drv._mode_string = lambda: "OFF"
        drv._safe_query = lambda cmd, default="": {
            ":BATT:SIM:TVOL?": "0.0",
            ":OUTP:PROT:TRIP?": "OCP",
        }.get(cmd, default)

        state = drv.get_monitor_state()
        assert state["terminal_voltage"] == 0.0
        assert state["ocp_tripped"] is True
        assert state["ovp_tripped"] is False


# ---------------------------------------------------------------------------
# describe_error: no more empty 'Hardware service unreachable: '
# ---------------------------------------------------------------------------


class TestDescribeError:
    @pytest.fixture(scope="class")
    def device_mod(self):
        stubs = _lager_exceptions_stub()
        wrappers = types.ModuleType("lager.instrument_wrappers")
        visa_enum = types.ModuleType("lager.instrument_wrappers.visa_enum")
        visa_enum.EnumEncoder = object
        for defines in ("rigol_mso5000_defines", "rigol_dm3000_defines"):
            setattr(wrappers, defines, types.ModuleType(defines))
            stubs[f"lager.instrument_wrappers.{defines}"] = getattr(wrappers, defines)
        stubs.update({
            "lager.instrument_wrappers": wrappers,
            "lager.instrument_wrappers.visa_enum": visa_enum,
        })
        constants = types.ModuleType("dv_pkg.constants")
        constants.HARDWARE_PORT = 8080
        pkg = types.ModuleType("dv_pkg")
        pkg.__path__ = [os.path.join(_REPO_ROOT, "box/lager/nets")]
        sys.modules["dv_pkg"] = pkg
        sys.modules["dv_pkg.constants"] = constants
        for stub_name, stub in stubs.items():
            sys.modules.setdefault(stub_name, stub)
        path = os.path.join(_REPO_ROOT, "box/lager/nets/device.py")
        spec = importlib.util.spec_from_file_location("dv_pkg.device", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["dv_pkg.device"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_bare_connectionfailed_names_its_cause(self, device_mod):
        # The Device proxy does `raise ConnectionFailed from exc` — str() is
        # empty, which used to surface as 'Hardware service unreachable: '.
        try:
            try:
                raise TimeoutError("read timed out after 10s")
            except TimeoutError as exc:
                raise device_mod.ConnectionFailed from exc
        except device_mod.ConnectionFailed as cf:
            text = device_mod.describe_error(cf)
        assert text == "ConnectionFailed: read timed out after 10s"

    def test_bare_exception_with_messageless_cause(self, device_mod):
        try:
            try:
                raise TimeoutError
            except TimeoutError as exc:
                raise device_mod.ConnectionFailed from exc
        except device_mod.ConnectionFailed as cf:
            text = device_mod.describe_error(cf)
        assert text == "ConnectionFailed: TimeoutError"

    def test_message_is_preserved(self, device_mod):
        err = device_mod.DeviceError("device returned -700")
        assert device_mod.describe_error(err) == "DeviceError: device returned -700"

    def test_completely_bare_exception(self, device_mod):
        assert device_mod.describe_error(device_mod.ConnectionFailed()) == "ConnectionFailed"

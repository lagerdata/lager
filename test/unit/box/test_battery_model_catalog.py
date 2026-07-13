# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the read-only battery model catalog.

The 2281S has no :BATT:MODel:CATalog? query (reference manual 077114601,
March 2019 — the word "catalog" appears nowhere in it), so
``KeithleyBattery.model_catalog`` assembles the catalog from the documented
query-only :BATT:MOD<n>:VOC:STEPs? probes plus the five firmware built-in
models. These tests cover the probe parsing (occupied/empty slots), the
unsupported-firmware rejection path, and the ``list_models`` dispatcher
action that returns the structured payload to /battery/command callers.

Modules are spec-loaded with stubbed ``lager.*`` parents so the tests run
in the CLI test environment without box-only dependencies (pyvisa etc.),
following the pattern in test_monitor_state.py.
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


def _instrument_wrapper_stubs():
    wrap = types.ModuleType("lager.instrument_wrappers.instrument_wrap")
    wrap.InstrumentWrapKeithley = object
    defines = types.ModuleType("lager.instrument_wrappers.keithley_defines")
    defines.Mode = types.SimpleNamespace(
        BatterySimulator=types.SimpleNamespace(to_cmd=lambda: "BATT"))
    defines.SimMethod = object
    util = types.ModuleType("lager.instrument_wrappers.util")
    util.InvalidEnumError = type("InvalidEnumError", (Exception,), {})
    pkg = types.ModuleType("lager.instrument_wrappers")
    return {
        "lager.instrument_wrappers": pkg,
        "lager.instrument_wrappers.instrument_wrap": wrap,
        "lager.instrument_wrappers.keithley_defines": defines,
        "lager.instrument_wrappers.util": util,
    }


@pytest.fixture(scope="module")
def keithley_mod():
    stubs = _lager_exceptions_stub()
    stubs.update(_instrument_wrapper_stubs())
    for stub_name, stub in stubs.items():
        sys.modules.setdefault(stub_name, stub)

    pkg_name = "kbc_pkg"
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
    return mod


@pytest.fixture(scope="module")
def dispatcher_mod(keithley_mod):
    stubs = _lager_exceptions_stub()
    base = types.ModuleType("lager.dispatchers.base")

    class BaseDispatcher:
        pass

    base.BaseDispatcher = BaseDispatcher
    dispatchers = types.ModuleType("lager.dispatchers")
    stubs.update({
        "lager.dispatchers": dispatchers,
        "lager.dispatchers.base": base,
    })
    for stub_name, stub in stubs.items():
        sys.modules.setdefault(stub_name, stub)

    # dispatcher.py does `from .battery_net import ...` / `from .keithley
    # import ...`; reuse the modules already loaded by the keithley fixture.
    pkg_name = "kbc_pkg"
    path = os.path.join(_REPO_ROOT, "box/lager/power/battery/dispatcher.py")
    spec = importlib.util.spec_from_file_location(pkg_name + ".dispatcher", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name + ".dispatcher"] = mod
    spec.loader.exec_module(mod)
    return mod


def _driver_with_replies(keithley_mod, replies=None, error=None):
    """KeithleyBattery with a fake checked-query transport.

    ``replies`` maps SCPI command -> response string; unmapped probes answer
    "0" (empty slot). ``error`` maps command -> exception to raise instead.
    Writes are forbidden: the catalog must be read-only.
    """
    drv = object.__new__(keithley_mod.KeithleyBattery)
    drv.commands = []

    def query(cmd):
        drv.commands.append(cmd)
        if error and cmd in error:
            raise error[cmd]
        return (replies or {}).get(cmd, "0")

    drv._query = query

    def forbidden(*args, **kwargs):
        raise AssertionError("model_catalog must not write to the instrument")

    drv._write = forbidden
    drv._tolerant_write = forbidden
    drv._set_with_output_management = forbidden
    return drv


class TestKeithleyModelCatalog:
    def test_normal_catalog_lists_occupied_slots_and_builtins(self, keithley_mod):
        drv = _driver_with_replies(keithley_mod, replies={
            ":BATT:MOD1:VOC:STEP?": "101",
            ":BATT:MOD3:VOC:STEP?": "101",
        })
        catalog = drv.model_catalog()
        assert catalog[0] == {"slot": 0, "name": "DISCHARGE"}
        assert {"slot": 1, "name": None} in catalog
        assert {"slot": 3, "name": None} in catalog
        # Unoccupied slots are omitted.
        assert not any(entry["slot"] == 2 for entry in catalog)
        # The five firmware built-ins are always listed, slot-less.
        builtins = [entry["name"] for entry in catalog if entry["slot"] is None]
        assert builtins == list(keithley_mod.BUILTIN_MODELS)
        # All nine slots were probed, read-only.
        assert drv.commands == [f":BATT:MOD{i}:VOC:STEP?" for i in range(1, 10)]

    def test_empty_slots_leave_discharge_and_builtins_only(self, keithley_mod):
        drv = _driver_with_replies(keithley_mod)
        catalog = drv.model_catalog()
        assert catalog[0] == {"slot": 0, "name": "DISCHARGE"}
        assert [e for e in catalog if e["slot"] not in (0, None)] == []
        assert len(catalog) == 1 + len(keithley_mod.BUILTIN_MODELS)

    def test_odd_step_replies_treated_as_empty(self, keithley_mod):
        drv = _driver_with_replies(keithley_mod, replies={
            ":BATT:MOD1:VOC:STEP?": "garbage",
            ":BATT:MOD2:VOC:STEP?": "",
            ":BATT:MOD3:VOC:STEP?": "+1.010000E+02",   # sci-notation counts
        })
        catalog = drv.model_catalog()
        slots = [e["slot"] for e in catalog if e["slot"] not in (0, None)]
        assert slots == [3]

    def test_per_slot_instrument_error_means_slot_is_skipped(self, keithley_mod):
        # e.g. an execution error against one slot must not abort the sweep.
        drv = _driver_with_replies(
            keithley_mod,
            replies={":BATT:MOD2:VOC:STEP?": "101"},
            error={":BATT:MOD1:VOC:STEP?": Exception(704, "Not permitted", "")},
        )
        catalog = drv.model_catalog()
        slots = [e["slot"] for e in catalog if e["slot"] not in (0, None)]
        assert slots == [2]

    def test_undefined_header_raises_unsupported_firmware(self, keithley_mod):
        # InstrumentError style: (code, message, response) in args.
        err = Exception(-113, "Undefined header", "")
        drv = _driver_with_replies(
            keithley_mod, error={":BATT:MOD1:VOC:STEP?": err})
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.model_catalog()
        assert "does not support" in str(excinfo.value)
        # Bailed on the first probe instead of sweeping the rest.
        assert drv.commands == [":BATT:MOD1:VOC:STEP?"]

    def test_string_coded_error_also_detected(self, keithley_mod):
        # Some wrappers stringify SCPI errors as '<code>,"<message>"'.
        err = keithley_mod.BatteryBackendError('-113,"Undefined header"')
        drv = _driver_with_replies(
            keithley_mod, error={":BATT:MOD1:VOC:STEP?": err})
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.model_catalog()
        assert "does not support" in str(excinfo.value)

    def test_no_response_fails_fast(self, keithley_mod):
        # A codeless transport failure (e.g. VISA timeout on old firmware)
        # must raise on the first probe, not time out nine times.
        drv = _driver_with_replies(
            keithley_mod, error={":BATT:MOD1:VOC:STEP?": Exception("timeout")})
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.model_catalog()
        assert "catalog query failed" in str(excinfo.value)
        assert drv.commands == [":BATT:MOD1:VOC:STEP?"]


class TestListModelsAction:
    def _fake_resolver(self, dispatcher_mod, catalog):
        class FakeDriver:
            def model_catalog(self):
                return catalog

        dispatcher_mod._dispatcher._resolve_net_and_driver = (
            lambda netname: (FakeDriver(), 1))

    def test_returns_structured_payload(self, dispatcher_mod, capsys):
        catalog = [
            {"slot": 0, "name": "DISCHARGE"},
            {"slot": 1, "name": None},
            {"slot": None, "name": "LI-ION4_2"},
        ]
        self._fake_resolver(dispatcher_mod, catalog)
        result = dispatcher_mod.list_models("batt1")
        # HTTP callers of /battery/command receive the catalog in the
        # response payload, like the 'state' action's structured dict.
        assert result == {"models": catalog}
        out = capsys.readouterr().out
        assert "DISCHARGE" in out
        assert "(custom model)" in out
        assert "LI-ION4_2 (built-in)" in out
        assert "valid inputs to the 'model' command" in out

    def test_backend_error_propagates(self, dispatcher_mod):
        class FailingDriver:
            def model_catalog(self):
                raise dispatcher_mod.BatteryBackendError("no catalog support")

        dispatcher_mod._dispatcher._resolve_net_and_driver = (
            lambda netname: (FailingDriver(), 1))
        with pytest.raises(dispatcher_mod.BatteryBackendError):
            dispatcher_mod.list_models("batt1")


class TestFormatModelCatalog:
    def test_table_shape(self, dispatcher_mod):
        text = dispatcher_mod.format_model_catalog([
            {"slot": 0, "name": "DISCHARGE"},
            {"slot": 4, "name": None},
            {"slot": None, "name": "NIMH12"},
        ])
        lines = text.splitlines()
        assert lines[0] == "Slot  Model"
        assert lines[1].startswith("0") and "DISCHARGE" in lines[1]
        assert lines[2].startswith("4") and "(custom model)" in lines[2]
        assert lines[3].startswith("-") and "NIMH12 (built-in)" in lines[3]
        assert lines[-1] == (
            "Slots and names above are valid inputs to the 'model' command.")

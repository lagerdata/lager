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
        assert catalog[0] == {"slot": 1, "name": None}
        assert {"slot": 3, "name": None} in catalog
        # Unoccupied slots are omitted, and there is no slot-0 DISCHARGE
        # entry: firmware rejects every SCPI recall form for discharge
        # (hardware-verified 2026-07-14), so listing it would advertise an
        # unloadable input to the 'model' command.
        assert not any(entry["slot"] in (0, 2) for entry in catalog)
        # The five firmware built-ins are always listed, slot-less.
        builtins = [entry["name"] for entry in catalog if entry["slot"] is None]
        assert builtins == list(keithley_mod.BUILTIN_MODELS)
        # All nine slots were probed, read-only.
        assert drv.commands == [f":BATT:MOD{i}:VOC:STEP?" for i in range(1, 10)]

    def test_empty_slots_leave_builtins_only(self, keithley_mod):
        drv = _driver_with_replies(keithley_mod)
        catalog = drv.model_catalog()
        assert [e for e in catalog if e["slot"] is not None] == []
        assert len(catalog) == len(keithley_mod.BUILTIN_MODELS)

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


class TestCurrentModel:
    """current_model() reads :BATT:MOD:RCL? — hardware-verified: it answers
    with the slot number while a numbered slot is active and never replies
    while a firmware built-in is active. (:BATT:STAT?, which older code used,
    reports charge/discharge status, not the model.)"""

    def _drv(self, keithley_mod, rcl_reply):
        drv = object.__new__(keithley_mod.KeithleyBattery)

        class FakeInstr:
            timeout = 5000

        drv.instr = FakeInstr()
        drv._safe_query = (
            lambda cmd, default="": rcl_reply if cmd == ":BATT:MOD:RCL?" else default)
        return drv

    def test_numbered_slot(self, keithley_mod):
        drv = self._drv(keithley_mod, "5")
        assert drv.current_model() == "slot 5"
        # The shortened query timeout must be restored afterwards.
        assert drv.instr.timeout == 5000

    def test_slot_zero_reads_as_discharge(self, keithley_mod):
        assert self._drv(keithley_mod, "0").current_model() == "DISCHARGE"

    def test_name_reply_passes_through(self, keithley_mod):
        # Future firmware that answers with a name should just work.
        assert self._drv(keithley_mod, '"LI-ION4_2"').current_model() == "LI-ION4_2"

    def test_silence_falls_back_to_cached_name(self, keithley_mod):
        drv = self._drv(keithley_mod, "")
        drv._active_model_name = "LI-ION4_2"
        assert drv.current_model() == "LI-ION4_2"

    def test_silence_without_cache_reads_custom(self, keithley_mod):
        assert self._drv(keithley_mod, "").current_model() == "Custom"


class TestSetModelVerification:
    """set_model verifies through :BATT:MOD:RCL? because recalling an empty
    slot fails silently (hardware-verified: no error queued, previous model
    stays active). For built-ins, RCL? silence is the success signature."""

    def _drv(self, keithley_mod, rcl_reply):
        drv = object.__new__(keithley_mod.KeithleyBattery)
        drv.writes = []
        drv._set_with_output_management = (
            lambda cmd, ignore_codes=(): drv.writes.append(cmd))
        drv._active_model_raw = lambda: rcl_reply
        return drv

    def test_numeric_slot_success(self, keithley_mod):
        drv = self._drv(keithley_mod, "5")
        drv.set_model(5)
        assert drv.writes == [":BATT:MOD:RCL 5"]
        assert drv._active_model_name == "slot 5"

    def test_alias_maps_to_slot(self, keithley_mod):
        drv = self._drv(keithley_mod, "1")
        drv.set_model("liion")
        assert drv.writes == [":BATT:MOD:RCL 1"]
        assert drv._active_model_name == "slot 1"

    def test_discharge_spellings_all_satisfied_without_writes(self, keithley_mod):
        # Discharge / slot 0 is the instrument's always-available idle
        # default, not a stored model. Firmware 01.08b rejects every SCPI
        # recall form for it (hardware-verified 2026-07-14: ':BATT:MOD:RCL 0'
        # is -222 "Data out of range" since only 1-9 are valid numeric
        # arguments, ':BATT:MOD:RCL DISCHARGE' is -102 Syntax error, and the
        # quoted/abbreviated forms fail too), and :BATT:MOD:RCL? never echoes
        # it. set_model must treat the request as satisfied — no doomed
        # recall sent, no readback demanded — so existing
        # set_model('discharge') callers (HIL cold-boot power cycles) keep
        # working. Raising here was the 0.32.0 regression. The RCL? readback
        # is deliberately simulated as NOT '0' (a built-in name / a numbered
        # slot): success must not depend on it.
        for rcl_reply in ("LEAD_ACID12", "3", ""):
            for spelling in ("discharge", "DISCHARGE", 0, "0"):
                drv = self._drv(keithley_mod, rcl_reply)
                drv.set_model(spelling)  # must not raise
                assert drv.writes == [], (rcl_reply, spelling)
                assert drv._active_model_name == "DISCHARGE", (rcl_reply, spelling)

    def test_empty_slot_detected_by_unchanged_readback(self, keithley_mod):
        # Recall of empty slot 7 fails silently; RCL? still reports slot 5.
        drv = self._drv(keithley_mod, "5")
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.set_model(7)
        msg = str(excinfo.value)
        assert "appears to be empty" in msg
        assert "active model: 5" in msg
        assert "'models'" in msg  # points at the new catalog command

    def test_empty_slot_with_builtin_active_gives_no_reply(self, keithley_mod):
        # Previous model was a built-in, so RCL? is silent both before and
        # after the failed recall — still a verification failure.
        drv = self._drv(keithley_mod, "")
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.set_model(7)
        assert "unchanged (no reply)" in str(excinfo.value)

    def test_builtin_success_echoes_name(self, keithley_mod):
        # RCL? echoes the built-in's name on success (hardware-verified).
        drv = self._drv(keithley_mod, "LI_ION4_2")
        drv.set_model("LI_ION4_2")
        assert drv.writes == [":BATT:MOD:RCL LI_ION4_2"]
        assert drv._active_model_name == "LI_ION4_2"

    def test_builtin_hyphen_input_sends_underscore(self, keithley_mod):
        # The manual prints LI-ION4_2, but a hyphen is a SCPI syntax error
        # (-102, hardware-verified) — accept it and send the underscore form.
        drv = self._drv(keithley_mod, "LEAD_ACID12")
        drv.set_model("lead-acid12")
        assert drv.writes == [":BATT:MOD:RCL LEAD_ACID12"]
        assert drv._active_model_name == "LEAD_ACID12"

    def test_builtin_failure_still_reports_previous_slot(self, keithley_mod):
        drv = self._drv(keithley_mod, "5")
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.set_model("LI_ION4_2")
        assert "model slot 5" in str(excinfo.value)


class TestListModelsAction:
    def _fake_resolver(self, dispatcher_mod, catalog):
        class FakeDriver:
            def model_catalog(self):
                return catalog

        dispatcher_mod._dispatcher._resolve_net_and_driver = (
            lambda netname: (FakeDriver(), 1))

    def test_returns_structured_payload(self, dispatcher_mod, capsys):
        catalog = [
            {"slot": 1, "name": None},
            {"slot": None, "name": "LI-ION4_2"},
        ]
        self._fake_resolver(dispatcher_mod, catalog)
        result = dispatcher_mod.list_models("batt1")
        # HTTP callers of /battery/command receive the catalog in the
        # response payload, like the 'state' action's structured dict.
        assert result == {"models": catalog}
        out = capsys.readouterr().out
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
            {"slot": 4, "name": None},
            {"slot": None, "name": "NIMH12"},
        ])
        lines = text.splitlines()
        assert lines[0] == "Slot  Model"
        assert lines[1].startswith("4") and "(custom model)" in lines[1]
        assert lines[2].startswith("-") and "NIMH12 (built-in)" in lines[2]
        assert lines[-1] == (
            "Slots and names above are valid inputs to the 'model' command.")

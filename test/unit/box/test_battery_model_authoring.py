# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for battery model authoring (create/export of memory slots).

Ground truth (hardware-verified on a 2281S, firmware 01.08b):
- :BATT:MOD<n>:VOC? / :RES? answer with the SAVED slot's points directly,
  independent of the active model — export needs no :BATT:MOD:RCL and never
  changes the active model. Empty slots answer all-zeros, so occupancy is
  the :BATT:MOD<n>:VOC:STEPs? probe (0 = empty), same as model_catalog.
- Models are staged per element (plain write + :APPend chunks, or exactly 11
  points via :SIMPlify which interpolates to 101) and persisted with
  :BATT:MOD:SAVE:INTernal <slot>, which silently overwrites. There is no
  SCPI to delete/empty a slot.

Modules are spec-loaded with stubbed ``lager.*`` parents so the tests run in
the CLI test environment without box-only dependencies (pyvisa etc.),
following the pattern in test_battery_model_catalog.py. Note: stub
registration uses sys.modules.setdefault, so in full-suite runs the real
lager.exceptions classes (str() prefixed "[Battery] ") may be live — no
error-message assertion below anchors to the string start.
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
    """Spec-load a module with stubbed dependency modules."""
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

    pkg_name = "kba_pkg"
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

    pkg_name = "kba_pkg"
    path = os.path.join(_REPO_ROOT, "box/lager/power/battery/dispatcher.py")
    spec = importlib.util.spec_from_file_location(pkg_name + ".dispatcher", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name + ".dispatcher"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def csv_mod():
    """The CLI's CSV helper is click-free stdlib; spec-load it directly."""
    return _load_module(
        "battery_model_csv_under_test",
        "cli/commands/power/battery_model_csv.py")


# ---------------------------------------------------------------------------
# Curve fixtures
# ---------------------------------------------------------------------------

def _curve(n):
    """A valid n-point curve: voc rising 3.0->4.2 V, res falling 0.30->0.15 Ω."""
    voc = [3.0 + 1.2 * i / (n - 1) for i in range(n)]
    res = [0.30 - 0.15 * i / (n - 1) for i in range(n)]
    return voc, res


def _authoring_driver(keithley_mod, *, replies=None, output_on=False,
                      drain_errors=None):
    """KeithleyBattery with a fake transport for define_model/read_model.

    ``replies`` maps query -> response. ``drain_errors`` is a list of
    exceptions popped by successive _drain_error_queue calls (None = clean).
    All writes and queries are recorded.
    """
    drv = object.__new__(keithley_mod.KeithleyBattery)
    drv.writes = []
    drv.queries = []
    drain_errors = list(drain_errors or [])

    def write(cmd, check_errors=True):
        drv.writes.append(cmd)

    def query(cmd):
        drv.queries.append(cmd)
        return (replies or {}).get(cmd, "0")

    def drain(ignore_codes=()):
        if drain_errors:
            err = drain_errors.pop(0)
            if err is not None:
                raise err

    drv._write = write
    drv._query = query
    drv._drain_error_queue = drain
    drv._is_batt_output_on = lambda: output_on
    drv._ensure_batt_mode = lambda: None
    return drv


def _readonly_driver(keithley_mod, replies):
    """Driver whose write paths are forbidden: read_model must be read-only."""
    drv = _authoring_driver(keithley_mod, replies=replies)

    def forbidden(*args, **kwargs):
        raise AssertionError("read_model must not write to the instrument")

    drv._write = forbidden
    drv._tolerant_write = forbidden
    drv._set_with_output_management = forbidden
    return drv


# ---------------------------------------------------------------------------
# define_model — validation (all rejected before any instrument traffic)
# ---------------------------------------------------------------------------

class TestDefineModelValidation:
    def _assert_rejected(self, keithley_mod, match, slot=9, voc=None, resistance=None):
        drv = _authoring_driver(keithley_mod)
        if voc is None and resistance is None:
            voc, resistance = _curve(11)
        with pytest.raises(keithley_mod.BatteryBackendError, match=match):
            drv.define_model(slot, voc, resistance)
        assert drv.writes == [] and drv.queries == [], \
            "validation failures must not touch the instrument"

    def test_slot_zero_rejected(self, keithley_mod):
        # Slot 0 is DISCHARGE; the save command cannot target it.
        self._assert_rejected(keithley_mod, "1 to 9", slot=0)

    def test_slot_ten_rejected(self, keithley_mod):
        self._assert_rejected(keithley_mod, "1 to 9", slot=10)

    def test_non_numeric_slot_rejected(self, keithley_mod):
        self._assert_rejected(keithley_mod, "1 to 9", slot="first")

    def test_wrong_length_rejected(self, keithley_mod):
        voc, res = _curve(12)
        self._assert_rejected(keithley_mod, "exactly 11", voc=voc, resistance=res)

    def test_mismatched_lengths_rejected(self, keithley_mod):
        voc, res = _curve(11)
        self._assert_rejected(
            keithley_mod, "one resistance per VOC", voc=voc, resistance=res[:-1])

    def test_decreasing_voc_rejected(self, keithley_mod):
        voc, res = _curve(11)
        voc[5] = voc[4] - 0.01
        self._assert_rejected(keithley_mod, "non-decreasing", voc=voc, resistance=res)

    def test_increasing_resistance_rejected(self, keithley_mod):
        voc, res = _curve(11)
        res[5] = res[4] + 0.01
        self._assert_rejected(keithley_mod, "non-increasing", voc=voc, resistance=res)

    def test_voc_above_60v_rejected(self, keithley_mod):
        voc, res = _curve(11)
        voc[-1] = 60.5
        self._assert_rejected(keithley_mod, "out of\\s+range", voc=voc, resistance=res)

    def test_zero_voc_rejected(self, keithley_mod):
        voc, res = _curve(11)
        voc[0] = 0.0
        self._assert_rejected(keithley_mod, "out of\\s+range", voc=voc, resistance=res)

    def test_resistance_above_100_rejected(self, keithley_mod):
        voc, res = _curve(11)
        res[0] = 101.0
        self._assert_rejected(keithley_mod, "out of\\s+range", voc=voc, resistance=res)

    def test_nan_rejected(self, keithley_mod):
        # NaN compares false to everything, so the monotonicity checks alone
        # would let it straight through to the instrument.
        voc, res = _curve(11)
        voc[3] = float("nan")
        self._assert_rejected(keithley_mod, "finite", voc=voc, resistance=res)

    def test_non_numeric_points_rejected(self, keithley_mod):
        voc, res = _curve(11)
        self._assert_rejected(
            keithley_mod, "must be numbers",
            voc=["3.0v"] + voc[1:], resistance=res)

    def test_constant_curves_accepted(self, keithley_mod):
        # Flat voc/resistance satisfy non-decreasing/non-increasing.
        drv = _authoring_driver(keithley_mod, replies={
            ":BATT:MOD9:VOC:STEP?": "101", ":BATT:MOD9:RES:STEP?": "101"})
        drv.define_model(9, [3.7] * 11, [0.2] * 11)
        assert any("SIMP" in w for w in drv.writes)


# ---------------------------------------------------------------------------
# define_model — instrument write sequences
# ---------------------------------------------------------------------------

class TestDefineModelWrites:
    def test_11_point_path_uses_simplify(self, keithley_mod):
        voc, res = _curve(11)
        drv = _authoring_driver(keithley_mod, replies={
            ":BATT:MOD9:VOC:STEP?": "101", ":BATT:MOD9:RES:STEP?": "101"})
        drv.define_model(9, voc, res)

        assert len(drv.writes) == 3
        assert drv.writes[0].startswith(':BATT:MOD9:VOC:SIMP "')
        assert drv.writes[1].startswith(':BATT:MOD9:RES:SIMP "')
        assert drv.writes[2] == ":BATT:MOD:SAVE:INTERNAL 9"
        # Each SIMP payload carries exactly the 11 points.
        for write in drv.writes[:2]:
            payload = write.split('"')[1]
            assert len(payload.split(",")) == 11
        # Post-save verification probed both elements.
        assert ":BATT:MOD9:VOC:STEP?" in drv.queries
        assert ":BATT:MOD9:RES:STEP?" in drv.queries

    def test_101_point_path_chunks_with_append(self, keithley_mod):
        voc, res = _curve(101)
        drv = _authoring_driver(keithley_mod, replies={
            ":BATT:MOD3:VOC:STEP?": "101", ":BATT:MOD3:RES:STEP?": "101"})
        drv.define_model(3, voc, res)

        assert drv.writes[-1] == ":BATT:MOD:SAVE:INTERNAL 3"
        staged = drv.writes[:-1]
        for element, values in (("VOC", voc), ("RES", res)):
            plain = [w for w in staged if w.startswith(f':BATT:MOD3:{element} "')]
            appends = [w for w in staged if w.startswith(f':BATT:MOD3:{element}:APPEND "')]
            assert len(plain) == 1, f"{element}: exactly one non-APPend opener"
            assert appends, f"{element}: the 101-point curve must be chunked"
            # The opener must come before every APPend for this element.
            element_writes = [w for w in staged if f":{element}" in w]
            assert element_writes[0] == plain[0]
            # Chunks reassemble to exactly the input curve, in order.
            sent = []
            for write in [plain[0]] + appends:
                payload = write.split('"')[1]
                assert len(f'"{payload}"') <= 2048
                sent.extend(float(v) for v in payload.split(","))
            assert len(sent) == 101
            assert sent == pytest.approx(values)

    def test_no_simplify_on_101_point_path(self, keithley_mod):
        voc, res = _curve(101)
        drv = _authoring_driver(keithley_mod, replies={
            ":BATT:MOD3:VOC:STEP?": "101", ":BATT:MOD3:RES:STEP?": "101"})
        drv.define_model(3, voc, res)
        assert not any("SIMP" in w for w in drv.writes)

    def test_output_disabled_and_restored_around_writes(self, keithley_mod):
        # Config writes can raise 704 while the model is running; mirror
        # _set_with_output_management around the whole sequence.
        voc, res = _curve(11)
        drv = _authoring_driver(keithley_mod, output_on=True, replies={
            ":BATT:MOD9:VOC:STEP?": "101", ":BATT:MOD9:RES:STEP?": "101"})
        drv.define_model(9, voc, res)
        assert drv.writes[0] == ":BATT:OUTP OFF"
        assert drv.writes[-1] == ":BATT:OUTP ON"
        assert ":BATT:MOD:SAVE:INTERNAL 9" in drv.writes

    def test_output_restored_even_when_a_write_fails(self, keithley_mod):
        voc, res = _curve(11)
        err = keithley_mod.BatteryBackendError('710,"Illegal model data setting"')
        drv = _authoring_driver(keithley_mod, output_on=True, drain_errors=[err])
        with pytest.raises(keithley_mod.BatteryBackendError):
            drv.define_model(9, voc, res)
        assert drv.writes[-1] == ":BATT:OUTP ON"

    def test_instrument_model_errors_are_translated(self, keithley_mod):
        # Defensive: client-side validation should prevent 701/710, but if
        # the instrument still rejects, the user must not see a raw code.
        voc, res = _curve(11)
        for code, needle in ((701, "too short"), (702, "too long"),
                             (710, "non-decreasing")):
            err = keithley_mod.BatteryBackendError(f'{code},"model error"')
            drv = _authoring_driver(keithley_mod, drain_errors=[err])
            with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
                drv.define_model(9, voc, res)
            assert needle in str(excinfo.value)
            assert f'{code},' not in str(excinfo.value)

    def test_unrecognized_instrument_error_passes_through(self, keithley_mod):
        voc, res = _curve(11)
        err = keithley_mod.BatteryBackendError('-350,"Queue overflow"')
        drv = _authoring_driver(keithley_mod, drain_errors=[err])
        with pytest.raises(keithley_mod.BatteryBackendError, match="Queue overflow"):
            drv.define_model(9, voc, res)

    def test_post_save_steps_mismatch_raises(self, keithley_mod):
        voc, res = _curve(11)
        drv = _authoring_driver(keithley_mod, replies={
            ":BATT:MOD9:VOC:STEP?": "101",
            ":BATT:MOD9:RES:STEP?": "11",   # RES element did not complete
        })
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.define_model(9, voc, res)
        assert "did not verify" in str(excinfo.value)
        assert "RES" in str(excinfo.value)


# ---------------------------------------------------------------------------
# read_model — read-only export
# ---------------------------------------------------------------------------

class TestReadModel:
    def test_normal_export(self, keithley_mod):
        drv = _readonly_driver(keithley_mod, replies={
            ":BATT:MOD5:VOC:STEP?": "3",
            ":BATT:MOD5:VOC?": "15.7855,16.4673,19.8385",
            ":BATT:MOD5:RES?": "0.6070,0.5040,0.2400",
        })
        model = drv.read_model(5)
        assert model == {"slot": 5, "points": [
            {"voc": 15.7855, "resistance": 0.6070},
            {"voc": 16.4673, "resistance": 0.5040},
            {"voc": 19.8385, "resistance": 0.2400},
        ]}
        # Hardware-verified: per-slot queries read the SAVED slot directly,
        # so export must never recall (which would change the active model).
        assert not any("RCL" in q for q in drv.queries)

    def test_quoted_reply_accepted(self, keithley_mod):
        drv = _readonly_driver(keithley_mod, replies={
            ":BATT:MOD1:VOC:STEP?": "2",
            ":BATT:MOD1:VOC?": '"3.0,4.2"',
            ":BATT:MOD1:RES?": '"0.3,0.2"',
        })
        points = drv.read_model(1)["points"]
        assert [p["voc"] for p in points] == [3.0, 4.2]

    def test_string_slot_accepted(self, keithley_mod):
        # The CLI sends ints, but the dispatcher path may carry "5".
        drv = _readonly_driver(keithley_mod, replies={
            ":BATT:MOD5:VOC:STEP?": "1",
            ":BATT:MOD5:VOC?": "3.7",
            ":BATT:MOD5:RES?": "0.2",
        })
        assert drv.read_model("5")["slot"] == 5

    def test_empty_slot_raises(self, keithley_mod):
        # Empty slots answer VOC? with all-zeros rather than an error
        # (hardware-verified), so STEPs? == 0 is the emptiness signal.
        drv = _readonly_driver(keithley_mod, replies={":BATT:MOD7:VOC:STEP?": "0"})
        with pytest.raises(keithley_mod.BatteryBackendError) as excinfo:
            drv.read_model(7)
        assert "empty" in str(excinfo.value)
        assert "'models'" in str(excinfo.value)
        # Bailed before ever asking for curve data.
        assert drv.queries == [":BATT:MOD7:VOC:STEP?"]

    def test_bad_slot_rejected_without_queries(self, keithley_mod):
        drv = _readonly_driver(keithley_mod, replies={})
        for bad in (0, 10, "nope"):
            with pytest.raises(keithley_mod.BatteryBackendError, match="1 to 9"):
                drv.read_model(bad)
        assert drv.queries == []

    def test_truncated_readback_raises(self, keithley_mod):
        drv = _readonly_driver(keithley_mod, replies={
            ":BATT:MOD5:VOC:STEP?": "3",
            ":BATT:MOD5:VOC?": "3.0,3.5",          # 2 of 3
            ":BATT:MOD5:RES?": "0.3,0.2,0.1",
        })
        with pytest.raises(keithley_mod.BatteryBackendError, match="incomplete"):
            drv.read_model(5)

    def test_garbage_readback_raises(self, keithley_mod):
        drv = _readonly_driver(keithley_mod, replies={
            ":BATT:MOD5:VOC:STEP?": "2",
            ":BATT:MOD5:VOC?": "3.0,junk",
            ":BATT:MOD5:RES?": "0.3,0.2",
        })
        with pytest.raises(keithley_mod.BatteryBackendError, match="Unexpected"):
            drv.read_model(5)


# ---------------------------------------------------------------------------
# Dispatcher actions
# ---------------------------------------------------------------------------

class TestExportModelAction:
    def test_returns_structured_payload(self, dispatcher_mod, capsys):
        model = {"slot": 5, "points": [
            {"voc": 3.0, "resistance": 0.3},
            {"voc": 4.2, "resistance": 0.2},
        ]}

        class FakeDriver:
            def read_model(self, slot):
                assert slot == 5
                return model

        dispatcher_mod._dispatcher._resolve_net_and_driver = (
            lambda netname: (FakeDriver(), 1))
        result = dispatcher_mod.export_model("batt1", slot=5)
        assert result == model
        out = capsys.readouterr().out
        assert "slot 5" in out
        assert "2 points" in out
        assert "3-4.2 V" in out
        assert "0.2-0.3" in out

    def test_backend_error_propagates(self, dispatcher_mod):
        class FailingDriver:
            def read_model(self, slot):
                raise dispatcher_mod.BatteryBackendError("slot 7 is empty")

        dispatcher_mod._dispatcher._resolve_net_and_driver = (
            lambda netname: (FailingDriver(), 1))
        with pytest.raises(dispatcher_mod.BatteryBackendError):
            dispatcher_mod.export_model("batt1", slot=7)


class TestCreateModelAction:
    def test_returns_slot_and_count(self, dispatcher_mod, capsys):
        voc, res = _curve(11)
        calls = []

        class FakeDriver:
            def define_model(self, slot, v, r):
                calls.append((slot, v, r))

        dispatcher_mod._dispatcher._resolve_net_and_driver = (
            lambda netname: (FakeDriver(), 1))
        result = dispatcher_mod.create_model(
            "batt1", slot=9, voc=voc, resistance=res)
        assert calls == [(9, voc, res)]
        assert result == {"slot": 9, "points": 11}
        assert "saved to slot 9" in capsys.readouterr().out

    def test_backend_error_propagates(self, dispatcher_mod):
        class FailingDriver:
            def define_model(self, slot, v, r):
                raise dispatcher_mod.BatteryBackendError("bad data")

        dispatcher_mod._dispatcher._resolve_net_and_driver = (
            lambda netname: (FailingDriver(), 1))
        voc, res = _curve(11)
        with pytest.raises(dispatcher_mod.BatteryBackendError):
            dispatcher_mod.create_model("batt1", slot=9, voc=voc, resistance=res)


class TestFormatModelSummary:
    def test_summary_shape(self, dispatcher_mod):
        text = dispatcher_mod.format_model_summary({"slot": 5, "points": [
            {"voc": 3.0, "resistance": 0.3},
            {"voc": 4.2, "resistance": 0.15},
        ]})
        assert text == ("Battery model slot 5: 2 points, "
                        "VOC 3-4.2 V, resistance 0.15-0.3 Ω")

    def test_empty_points(self, dispatcher_mod):
        text = dispatcher_mod.format_model_summary({"slot": 2, "points": []})
        assert text == "Battery model slot 2: 0 points"


# ---------------------------------------------------------------------------
# CLI CSV helper
# ---------------------------------------------------------------------------

def _write_csv(tmp_path, name, lines):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _curve_lines(n, header=True):
    voc, res = _curve(n)
    lines = ["voc,resistance"] if header else []
    lines.extend(f"{v:.4f},{r:.4f}" for v, r in zip(voc, res))
    return lines


class TestParseModelCsv:
    def test_valid_11_rows_with_header(self, csv_mod, tmp_path):
        path = _write_csv(tmp_path, "m.csv", _curve_lines(11))
        voc, res = csv_mod.parse_model_csv(path)
        assert len(voc) == len(res) == 11
        assert voc[0] == pytest.approx(3.0)
        assert res[-1] == pytest.approx(0.15)

    def test_valid_101_rows_without_header(self, csv_mod, tmp_path):
        path = _write_csv(tmp_path, "m.csv", _curve_lines(101, header=False))
        voc, res = csv_mod.parse_model_csv(path)
        assert len(voc) == len(res) == 101

    def test_blank_lines_ignored(self, csv_mod, tmp_path):
        lines = _curve_lines(11)
        lines.insert(3, "")
        lines.append("")
        path = _write_csv(tmp_path, "m.csv", lines)
        voc, _ = csv_mod.parse_model_csv(path)
        assert len(voc) == 11

    def test_wrong_row_count(self, csv_mod, tmp_path):
        path = _write_csv(tmp_path, "m.csv", _curve_lines(12))
        with pytest.raises(ValueError, match="11 or 101"):
            csv_mod.parse_model_csv(path)

    def test_wrong_column_count_names_line(self, csv_mod, tmp_path):
        lines = _curve_lines(11)
        lines[5] = "3.5,0.2,extra"
        path = _write_csv(tmp_path, "m.csv", lines)
        with pytest.raises(ValueError, match="line 6.*2 columns"):
            csv_mod.parse_model_csv(path)

    def test_non_numeric_cell_names_line(self, csv_mod, tmp_path):
        lines = _curve_lines(11)
        lines[4] = "abc,0.25"
        path = _write_csv(tmp_path, "m.csv", lines)
        with pytest.raises(ValueError, match="line 5"):
            csv_mod.parse_model_csv(path)

    def test_decreasing_voc_names_line(self, csv_mod, tmp_path):
        lines = _curve_lines(11)
        lines[6] = "1.0,0.23"  # below the previous voc
        path = _write_csv(tmp_path, "m.csv", lines)
        with pytest.raises(ValueError, match="line 7.*non-decreasing"):
            csv_mod.parse_model_csv(path)

    def test_increasing_resistance_names_line(self, csv_mod, tmp_path):
        lines = _curve_lines(11)
        cells = lines[6].split(",")
        lines[6] = f"{cells[0]},0.9"  # above the previous resistance
        path = _write_csv(tmp_path, "m.csv", lines)
        with pytest.raises(ValueError, match="line 7.*non-increasing"):
            csv_mod.parse_model_csv(path)

    def test_out_of_range_voc_names_line(self, csv_mod, tmp_path):
        lines = _curve_lines(11)
        lines[11] = "61.0,0.15"  # last row, voc above 60 V
        path = _write_csv(tmp_path, "m.csv", lines)
        with pytest.raises(ValueError, match="line 12.*out of range"):
            csv_mod.parse_model_csv(path)

    def test_zero_resistance_rejected(self, csv_mod, tmp_path):
        lines = _curve_lines(11)
        lines[11] = "4.2,0"
        path = _write_csv(tmp_path, "m.csv", lines)
        with pytest.raises(ValueError, match="line 12.*out of range"):
            csv_mod.parse_model_csv(path)

    def test_export_roundtrip(self, csv_mod, tmp_path):
        # write_model_csv output must parse straight back (export -> edit ->
        # create loop).
        voc, res = _curve(11)
        points = [{"voc": v, "resistance": r} for v, r in zip(voc, res)]
        path = str(tmp_path / "export.csv")
        csv_mod.write_model_csv(path, points)
        voc2, res2 = csv_mod.parse_model_csv(path)
        assert voc2 == pytest.approx(voc)
        assert res2 == pytest.approx(res)

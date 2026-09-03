# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The supply settle helper must not mistake a paused transient for a settled one.

`test/api/power/test_supply_Rigol_DP821.py` asserts that an unloaded channel
draws under 100 mA. That assertion is correct and the measurements support it:
the channel reads a clean 0.0000 A once settled, across 96 samples at four
setpoints. What kept failing was not the assertion but the read that fed it.

`_wait_for_regulation` waits for the output to reach its setpoint and then for
the current readback to stop moving. Its first version returned as soon as two
consecutive reads agreed to within 5 mA. On 2026-09-01 the diagnostic added by
#405 fired for the first time and captured why that is not the same claim:

    t=0.01s  current()=0.17     <- the read the assertion used
    t=0.07s  current()=0.17     <- agrees with the previous one
    t=0.13s  current()=0.0      <- and it was a transient all along
    ...eight further reads at 0.0
    voltage()=5.0  power()=0.0  measure(ch=2)={'current': '0.00', ...}

The pair agreed with each other while the output was still discharging into the
ADC fixture wired to that channel. `power()` already read 0.0 and the atomic
`measure()` reported 0.00, so the two readback paths disagreed by a whole
transient. A pause is not a settle, and two samples cannot tell them apart.

These tests drive the helper with a scripted readback rather than an
instrument, so the sampling rule can be exercised without a bench. The first
one replays the captured sequence verbatim: it is the regression.

The other half matters as much. The helper deliberately does NOT wait for the
current to fall below any threshold -- that would assert the very thing the
caller is about to test, and would turn a genuine steady load into a pass. So
these also pin that a constant non-zero draw settles immediately and is handed
to the caller unchanged.
"""

import importlib.util
import pathlib
import sys
import types

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SUITE = REPO_ROOT / "test" / "api" / "power" / "test_supply_Rigol_DP821.py"


def _load_suite(monkeypatch):
    """Import the API suite with its box-only imports stubbed out.

    The file runs on a box under `lager python`, where `lager` is the box
    package. Nothing here touches that surface -- the settle helper takes a
    duck-typed psu -- so a stub is enough to import the module.
    """
    lager = types.ModuleType("lager")
    lager.Net = object
    lager.NetType = types.SimpleNamespace(PowerSupply=object())
    monkeypatch.setitem(sys.modules, "lager", lager)

    spec = importlib.util.spec_from_file_location("_dp821_suite", SUITE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def suite(monkeypatch):
    return _load_suite(monkeypatch)


class FakePsu:
    """Replays a scripted current sequence; voltage is always at setpoint."""

    def __init__(self, currents, volts=5.0):
        self._currents = list(currents)
        self._volts = volts
        self.reads = 0

    def voltage(self):
        return self._volts

    def current(self):
        self.reads += 1
        if self._currents:
            return self._currents.pop(0)
        return 0.0


# The capture from the failing run, verbatim. Reads are ~60 ms apart there and
# the helper polls at 100 ms, so this is the shape rather than the exact clock.
CAPTURED_TRANSIENT = [0.17, 0.17, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def test_the_captured_transient_is_not_treated_as_settled(suite, monkeypatch):
    """The regression: two agreeing reads inside a transient must not return."""
    monkeypatch.setattr(suite.time, "sleep", lambda _s: None)
    psu = FakePsu(CAPTURED_TRANSIENT)

    suite._wait_for_regulation(psu, 5.0)

    # It must have read past the 0.17 plateau. Two reads would have been the
    # old behaviour and is exactly what produced the failed assertion.
    assert psu.reads > 2, (
        "returned while the readback was still on the 0.17 A plateau -- this "
        "is the two-sample bug that failed the unloaded-current assertion"
    )
    assert psu.current() == 0.0


def test_two_agreeing_reads_are_not_enough_on_their_own(suite, monkeypatch):
    """Directly pin the sample count, independent of the captured values."""
    monkeypatch.setattr(suite.time, "sleep", lambda _s: None)
    psu = FakePsu([0.42, 0.42, 0.0, 0.0, 0.0, 0.0, 0.0])

    suite._wait_for_regulation(psu, 5.0)

    assert psu.reads >= suite._CURRENT_STABLE_READS


def test_a_steady_load_settles_and_is_left_for_the_caller_to_judge(suite, monkeypatch):
    """The constraint the helper must not break.

    A real steady draw reads the same value every time, so it settles at once.
    The helper must hand it back unchanged rather than wait for it to fall --
    waiting on a threshold would assert the caller's assertion for it.
    """
    monkeypatch.setattr(suite.time, "sleep", lambda _s: None)
    psu = FakePsu([0.8] * 12)

    suite._wait_for_regulation(psu, 5.0)

    assert psu.current() == 0.8, "the helper must not wait for a load to vanish"
    assert psu.reads <= suite._CURRENT_STABLE_READS + 1, (
        "a genuinely steady reading should settle in the minimum sample count"
    )


def test_a_reading_that_pauses_then_moves_restarts_the_count(suite, monkeypatch):
    """Credit earned before a step must not carry across it."""
    monkeypatch.setattr(suite.time, "sleep", lambda _s: None)
    # Agrees twice, steps, agrees twice, steps again, then finally settles.
    psu = FakePsu([0.30, 0.30, 0.20, 0.20, 0.10, 0.0, 0.0, 0.0, 0.0])

    suite._wait_for_regulation(psu, 5.0)

    assert psu.current() == 0.0


def test_a_readback_that_never_answers_falls_back_rather_than_hanging(suite, monkeypatch):
    """A settle helper must not be what takes a hardware suite down."""
    slept = []
    monkeypatch.setattr(suite.time, "sleep", lambda s: slept.append(s))

    class Broken:
        def voltage(self):
            raise RuntimeError("query not implemented")

        def current(self):
            raise RuntimeError("query not implemented")

    suite._wait_for_regulation(Broken(), 5.0)
    assert suite._SETTLE_FALLBACK in slept


class TestFixtureNaming:
    """A red night must not cost someone re-deriving the bench wiring."""

    def test_the_wired_channel_names_its_fixture(self, monkeypatch):
        monkeypatch.setenv("SUPPLY_NET", "supply3")
        monkeypatch.setenv("USB202_SUPPLY_NET", "supply3")
        monkeypatch.setenv("USB202_SUPPLY_ADC_NET", "adc15")
        suite = _load_suite(monkeypatch)

        detail = suite._unloaded_detail(0.17, passed=False)
        assert "0.1700 A" in detail
        assert "supply3" in detail and "adc15" in detail
        assert "USB202_SUPPLY_NET" in detail

    def test_a_passing_assertion_is_not_annotated(self, monkeypatch):
        """`_record` prints its detail on both outcomes.

        So a note built unconditionally went out on every pass -- three lines
        per CH2 run, on every nightly, explaining a fixture that was not
        causing anything. The note is for the failure it was written for.
        """
        monkeypatch.setenv("SUPPLY_NET", "supply3")
        monkeypatch.setenv("USB202_SUPPLY_NET", "supply3")
        monkeypatch.setenv("USB202_SUPPLY_ADC_NET", "adc15")
        suite = _load_suite(monkeypatch)

        detail = suite._unloaded_detail(0.0, passed=True)
        assert detail == "measured=0.0000 A"
        assert "USB202_SUPPLY_NET" not in detail

    def test_an_unwired_channel_says_only_what_it_measured(self, monkeypatch):
        monkeypatch.setenv("SUPPLY_NET", "supply2")
        monkeypatch.setenv("USB202_SUPPLY_NET", "supply3")
        suite = _load_suite(monkeypatch)

        detail = suite._unloaded_detail(0.17, passed=False)
        assert detail == "measured=0.1700 A"

    def test_no_fixture_declared_is_silent(self, monkeypatch):
        monkeypatch.setenv("SUPPLY_NET", "supply3")
        monkeypatch.delenv("USB202_SUPPLY_NET", raising=False)
        suite = _load_suite(monkeypatch)

        assert suite._unloaded_detail(0.17, passed=False) == "measured=0.1700 A"

    def test_the_note_never_changes_the_verdict(self, monkeypatch):
        """It annotates a failure; it must not be able to excuse one."""
        monkeypatch.setenv("SUPPLY_NET", "supply3")
        monkeypatch.setenv("USB202_SUPPLY_NET", "supply3")
        suite = _load_suite(monkeypatch)

        assert suite.MAX_UNLOADED_CURRENT == 0.1
        assert "0.1700 A" in suite._unloaded_detail(0.17, passed=False)

    def test_both_call_sites_pass_their_verdict(self):
        """A note gated on a parameter nobody passes is a note that never fires.

        The two call sites compute `passed` on the line above the `_record`
        call, so the guard is only real if both hand it on.
        """
        text = SUITE.read_text()
        calls = [ln for ln in text.splitlines() if "_unloaded_detail(" in ln
                 and not ln.lstrip().startswith("def ")]
        assert len(calls) == 2, f"expected 2 call sites, found {len(calls)}: {calls}"
        for call in calls:
            assert "passed" in call, f"call site does not pass its verdict: {call!r}"

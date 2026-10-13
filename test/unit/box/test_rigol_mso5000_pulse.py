# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The Rigol MSO5000 pulse trigger, from the CLI handler down to the SCPI.

The mapper's pulse class called eight driver methods that did not exist, so
every configured `lager scope|logic NET trigger pulse` failed on a box with
"Function not found" (#418). The handler also sent `--upper` to the
greater-than condition and `--lower` to the less-than one, the reverse of how
the instrument reads them, and accepted polarity names it could not apply
(#499).

The SCPI pinned here was run on an MSO5074 (firmware 00.01.03.00.03) and read
back; see the comment above the pulse methods in the driver.
"""

import importlib
import types
from unittest import mock

import pytest

from lager.instrument_wrappers.rigol_mso5000_defines import (
    TriggerPulseCondition,
    TriggerPulseSource,
)
from lager.measurement.scope import rigol_mso5000
from lager.measurement.scope.rigol_mso5000 import RigolMso5000
from lager.nets.mappers import rigol_mso5000 as mapper

impl = importlib.import_module("cli.impl.measurement.scope")


class FakeInstrument:
    """Records writes; answers queries from a canned table."""

    def __init__(self, answers=None):
        self.writes = []
        self.answers = answers or {}
        self.timeout = 0

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        return self.answers.get(cmd, "0")


@pytest.fixture
def scope():
    fake = FakeInstrument({
        ":TRIGger:PULSe:LEVel?": "5.000000E-1",
        ":TRIGger:PULSe:LWIDth?": "1.000000E-6",
        ":TRIGger:PULSe:UWIDth?": "2.000000E-6",
    })
    with mock.patch.object(rigol_mso5000, "get_instrument", return_value=fake):
        dev = RigolMso5000(address="USB0::FAKE::INSTR", channel=1)
        dev._fake = fake
        yield dev


# --------------------------------------------------------------------------
# Driver: the SCPI each method sends
# --------------------------------------------------------------------------

@pytest.mark.parametrize("call, expected", [
    (lambda d: d.set_trigger_pulse_source(TriggerPulseSource.Channel1), ":TRIGger:PULSe:SOURce CHAN1"),
    (lambda d: d.set_trigger_pulse_source(TriggerPulseSource.D0), ":TRIGger:PULSe:SOURce D0"),
    (lambda d: d.set_trigger_pulse_level(0.4), ":TRIGger:PULSe:LEVel 0.4"),
    (lambda d: d.set_trigger_pulse_when(TriggerPulseCondition.Greater), ":TRIGger:PULSe:WHEN GRE"),
    (lambda d: d.set_trigger_pulse_when(TriggerPulseCondition.Less), ":TRIGger:PULSe:WHEN LESS"),
    (lambda d: d.set_trigger_pulse_when(TriggerPulseCondition.Box), ":TRIGger:PULSe:WHEN GLES"),
    (lambda d: d.set_trigger_pulse_lower(1e-6), ":TRIGger:PULSe:LWIDth 1e-06"),
    (lambda d: d.set_trigger_pulse_upper(5e-6), ":TRIGger:PULSe:UWIDth 5e-06"),
])
def test_setters_send_the_verified_scpi(scope, call, expected):
    call(scope)
    assert scope._fake.writes == [expected]


def test_an_enum_that_crossed_the_wire_sends_the_same_token(scope):
    """Through the Device proxy an enum arrives as EnumEncoder's dict."""
    scope.set_trigger_pulse_when({"__enum__": {"value": "GRE"}})
    assert scope._fake.writes == [":TRIGger:PULSe:WHEN GRE"]


def test_getters_read_their_own_node(scope):
    assert float(scope.get_trigger_pulse_level()) == 0.5
    assert float(scope.get_trigger_pulse_lower()) == 1e-6
    assert float(scope.get_trigger_pulse_upper()) == 2e-6


def test_the_driver_sends_no_polarity_command(scope):
    """The instrument has no polarity node; a write to one is a -100 error."""
    for name in dir(scope):
        assert "pulse_polarity" not in name


# --------------------------------------------------------------------------
# Mapper over the real driver: which width each condition writes
# --------------------------------------------------------------------------

def pulse_mapper(scope):
    return mapper.TriggerSettingsPulse_RigolMSO5000FunctionMapper(None, None, scope)


def test_greater_than_writes_the_lower_width(scope):
    pulse_mapper(scope).set_trigger_on_pulse_greater_than_width(3e-6)
    assert scope._fake.writes == [":TRIGger:PULSe:WHEN GRE", ":TRIGger:PULSe:LWIDth 3e-06"]


def test_less_than_writes_the_upper_width(scope):
    pulse_mapper(scope).set_trigger_on_pulse_less_than_width(3e-6)
    assert scope._fake.writes == [":TRIGger:PULSe:WHEN LESS", ":TRIGger:PULSe:UWIDth 3e-06"]


def test_between_writes_both_widths_lower_first(scope):
    pulse_mapper(scope).set_trigger_on_pulse_less_than_greater_than(
        max_pulse_width=5e-6, min_pulse_width=2e-6)
    assert scope._fake.writes == [":TRIGger:PULSe:WHEN GLES",
                                  ":TRIGger:PULSe:LWIDth 2e-06",
                                  ":TRIGger:PULSe:UWIDth 5e-06"]


def test_the_level_reads_back_as_a_float(scope):
    assert pulse_mapper(scope).get_level() == 0.5


# --------------------------------------------------------------------------
# Handler: width pairing, polarity names and the clamped level
# --------------------------------------------------------------------------

class RecordingPulse:
    def __init__(self, level_answer=None):
        self.calls = []
        self.level_answer = level_answer

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.level_answer if name == "get_level" else None
        return record


def run(trigger_on=None, upper=None, lower=None, level=None, level_answer=None):
    pulse = RecordingPulse(level_answer)
    settings = types.SimpleNamespace(
        set_mode_normal=lambda: None, set_coupling_DC=lambda: None,
        set_type=lambda t: None, pulse=pulse)
    net = types.SimpleNamespace(enable=lambda: None, trigger_settings=settings)
    with mock.patch.object(impl, "get_rigol_net", return_value=net), \
            mock.patch.object(impl, "get_source_net", return_value=None):
        impl.trigger_pulse(netname="net1", mode="normal", coupling="dc", source=None,
                           level=level, trigger_on=trigger_on, upper=upper, lower=lower)
    return pulse.calls


@pytest.mark.parametrize("trigger_on, method, width", [
    ("gt", "set_trigger_on_pulse_greater_than_width", 0.001),
    ("lt", "set_trigger_on_pulse_less_than_width", 0.002),
])
def test_each_condition_takes_the_width_it_compares_against(trigger_on, method, width):
    calls = run(trigger_on, upper=0.002, lower=0.001)
    assert calls == [(method, (width,), {})]


def test_gt_without_lower_changes_nothing_and_names_the_option(capsys):
    assert run("gt", upper=0.002) == []
    assert "gt needs --lower" in capsys.readouterr().out


def test_lt_without_upper_changes_nothing_and_names_the_option(capsys):
    assert run("lt", lower=0.001) == []
    assert "lt needs --upper" in capsys.readouterr().out


@pytest.mark.parametrize("legacy, condition, method", [
    ("positive", "gt", "set_trigger_on_pulse_greater_than_width"),
    ("negative", "gt", "set_trigger_on_pulse_greater_than_width"),
    ("positive_greater", "gt", "set_trigger_on_pulse_greater_than_width"),
    ("negative_greater", "gt", "set_trigger_on_pulse_greater_than_width"),
    ("positive_less", "lt", "set_trigger_on_pulse_less_than_width"),
    ("negative_less", "lt", "set_trigger_on_pulse_less_than_width"),
])
def test_a_polarity_name_applies_the_width_condition_and_says_polarity_was_not_set(
        legacy, condition, method, capsys):
    calls = run(legacy, upper=0.002, lower=0.001)
    assert [c[0] for c in calls] == [method]
    out = capsys.readouterr().out
    assert "Pulse polarity cannot be set remotely" in out
    assert f"applied as '{condition}'" in out


def test_the_plain_conditions_print_no_polarity_note(capsys):
    run("gtlt", upper=0.002, lower=0.001)
    assert "polarity" not in capsys.readouterr().out


def test_a_clamped_level_is_reported(capsys):
    calls = run(level=1.25, level_answer=0.5)
    assert ("set_level", (1.25,), {}) in calls
    out = capsys.readouterr().out
    assert "set the trigger level to 0.5 V, not 1.25 V" in out


def test_an_applied_level_is_not_reported(capsys):
    run(level=0.4, level_answer=0.4)
    assert "trigger level" not in capsys.readouterr().out


def test_an_unreadable_level_is_not_reported(capsys):
    run(level=0.4, level_answer=None)
    assert "trigger level" not in capsys.readouterr().out

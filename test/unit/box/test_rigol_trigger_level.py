# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
The Rigol's edge trigger level, which was being written in a form that does
not exist.

`:TRIGger:EDGE:LEVel` takes one real number, in the units of whichever source
`:TRIGger:EDGE:SOURce` currently names -- see the MSO5000 programming guide,
which gives the parameter as a single Real and the example as
`:TRIGger:EDGE:LEVel 0.16`. The driver sent the channel as a second argument.

Nothing surfaced it. A malformed SCPI write does not raise: the instrument
sets a bit in its status register and carries on, so `set_trigger_level`
returned a cheerful dict while the scope ignored it, and the matching query
asked in the same invalid form. There was no test on either.

These pin the exact strings, because the whole failure was that the strings
were wrong and everything above them looked fine.
"""
from __future__ import annotations

from unittest import mock

import pytest

from lager.measurement.scope import rigol_mso5000
from lager.measurement.scope.rigol_mso5000 import RigolMso5000


class FakeInstrument:
    """Records writes; answers queries from a canned table."""

    def __init__(self, answers=None):
        self.writes = []
        self.queries = []
        self.answers = answers or {}
        self.timeout = 0

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.queries.append(cmd)
        return self.answers.get(cmd, "0")


@pytest.fixture
def scope():
    """A driver on channel 2, so a leaked net channel would be visible."""
    fake = FakeInstrument(answers={":TRIGger:EDGe:LEVel?": "1.600000E-1"})
    with mock.patch.object(rigol_mso5000, "get_instrument", return_value=fake):
        dev = RigolMso5000(address="USB0::FAKE::INSTR", channel=2)
        dev._fake = fake
        yield dev


class TestTheLevelIsWrittenInAFormTheInstrumentHas:

    def test_it_is_one_argument(self, scope):
        scope.set_trigger_level(1.5)

        assert scope._fake.writes == [":TRIGger:EDGe:LEVel 1.5"]

    def test_the_channel_is_not_appended(self, scope):
        """The bug: `:TRIGger:EDGe:LEVel 1.5,CHANnel2`, silently discarded."""
        scope.set_trigger_level(1.5)

        written = scope._fake.writes[0]
        assert "," not in written
        assert "CHANnel" not in written

    def test_the_query_takes_no_argument(self, scope):
        assert scope.get_trigger_level() == pytest.approx(0.16)
        assert scope._fake.queries == [":TRIGger:EDGe:LEVel?"]

    def test_asking_for_a_source_does_not_malform_the_query(self, scope):
        """One level exists, so a source cannot change what is asked."""
        scope.get_trigger_level(source="CHANnel1")

        assert scope._fake.queries == [":TRIGger:EDGe:LEVel?"]


class TestOnlyWhatIsNamedChanges:

    def test_a_level_alone_leaves_the_source_where_it_is(self, scope):
        """It used to target the net's own channel implicitly."""
        scope.set_trigger_level(1.5)

        assert not any("SOURce" in w for w in scope._fake.writes)

    def test_a_named_source_is_selected_first(self, scope):
        """Order matters: the level is read in the source's units."""
        scope.set_trigger_level(1.5, source="CHANnel3")

        assert scope._fake.writes == [
            ":TRIGger:EDGe:SOURce CHANnel3",
            ":TRIGger:EDGe:LEVel 1.5",
        ]

    def test_the_return_value_no_longer_claims_a_source(self, scope):
        """It reported the channel it had silently aimed at."""
        assert scope.set_trigger_level(1.5) == {"trigger_level": 1.5}


class TestItMatchesThePicoScopeDriver:
    """Both are reached through the same handler, so they must agree."""

    def test_both_take_level_then_optional_source(self):
        import inspect

        from lager.measurement.scope.picoscope import PicoScope

        rigol = inspect.signature(RigolMso5000.set_trigger_level)
        pico = inspect.signature(PicoScope.set_trigger_level)
        assert list(rigol.parameters) == list(pico.parameters)

    def test_neither_changes_the_source_unless_told(self, scope):
        """The PicoScope driver only sets a source when one is passed."""
        import inspect

        from lager.measurement.scope.picoscope import PicoScope

        body = inspect.getsource(PicoScope.set_trigger_level)
        assert "if source is not None:" in body

        scope.set_trigger_level(0.5)
        assert not any("SOURce" in w for w in scope._fake.writes)

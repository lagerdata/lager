# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the ``lager nets state`` display table.

Validates that the State column shows live state strings, "–" for unknown,
and that the table still renders instrument groupings correctly.
"""
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from cli.commands.box.nets import _display_table

_LONG_ADDR = "USB0::0x10C4::0xEA60::3e6fe522e591ef11a56e3ec5cc16735d::INSTR"


class DisplayTableWithStateTest(unittest.TestCase):
    def _render(self, records, state_map):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _display_table(records, state_map=state_map)
        return buf.getvalue()

    def test_state_column_shown(self):
        out = self._render(
            [{"name": "usb1", "role": "usb", "pin": "1",
              "instrument": "Acroname_8Port", "address": "NA"}],
            {"usb1": "enabled"},
        )
        self.assertIn("State", out)
        self.assertIn("enabled", out)

    def test_dash_for_missing_state(self):
        out = self._render(
            [{"name": "swd", "role": "debug", "pin": "STM32@A",
              "instrument": "STLink_v2", "address": "USB0::INSTR"}],
            {},
        )
        self.assertIn("–", out)

    def test_multiple_roles(self):
        records = [
            {"name": "usb1", "role": "usb", "pin": "1",
             "instrument": "Acroname_8Port", "address": "NA"},
            {"name": "gpi1", "role": "gpio", "pin": "FIO0",
             "instrument": "LabJack_T7", "address": "ANY"},
            {"name": "supply1", "role": "power-supply", "pin": "1",
             "instrument": "Rigol_DP831", "address": "USB0::INSTR"},
        ]
        state_map = {
            "usb1": "disabled",
            "gpi1": "HIGH (1)",
            "supply1": "CH1/on/3.30V/0.12A",
        }
        out = self._render(records, state_map)
        self.assertIn("disabled", out)
        self.assertIn("HIGH (1)", out)
        self.assertIn("CH1/on/3.30V/0.12A", out)

    def test_no_records(self):
        out = self._render([], {})
        self.assertIn("No saved nets", out)

    def test_null_state_shows_dash(self):
        out = self._render(
            [{"name": "uart1", "role": "uart", "pin": "/dev/ttyUSB0",
              "instrument": "FTDI_FT232R", "address": "USB0::INSTR"}],
            {"uart1": None},
        )
        self.assertIn("–", out)


class NullReasonFootnoteTest(unittest.TestCase):
    """A "–" used to mean three unrelated things (issue #196).

    The reasons go in a footnote rather than the column: they are too long for
    a cell. It goes to stderr so a redirected table stays parseable.
    """

    RECS = [
        {"name": "usb1", "role": "usb", "pin": "0",
         "instrument": "Acroname_4Port", "address": "NA"},
        {"name": "usb2", "role": "usb", "pin": "1",
         "instrument": "Acroname_4Port", "address": "NA"},
        {"name": "uart1", "role": "uart", "pin": "/dev/ttyUSB0",
         "instrument": "FTDI_FT232R", "address": "NA"},
    ]

    def _render(self, state_map, reason_map):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            _display_table(self.RECS, state_map=state_map, reason_map=reason_map)
        return out.getvalue(), err.getvalue()

    def test_reason_is_reported_for_a_null(self):
        _, err = self._render({"usb1": None}, {"usb1": "deadline"})
        self.assertIn("usb1", err)
        self.assertIn("deadline", err)

    def test_nets_sharing_a_reason_are_listed_together(self):
        _, err = self._render(
            {"usb1": None, "usb2": None},
            {"usb1": "deadline", "usb2": "deadline"},
        )
        self.assertEqual(err.count("usb1, usb2: deadline"), 1,
                         "one line per reason, not one per net")

    def test_a_role_with_no_probe_is_not_reported_as_a_problem(self):
        """uart/spi/i2c report no state by design. Listing those would put a
        footnote on every bench and it would stop being read."""
        _, err = self._render({"uart1": None}, {"uart1": "no probe for role"})
        self.assertNotIn("uart1", err)

    def test_deadline_says_the_budget_is_shared(self):
        """Otherwise it reads as 'this instrument is slow', which is the wrong
        thing to go and investigate."""
        _, err = self._render({"usb1": None}, {"usb1": "deadline"})
        self.assertIn("shared", err)

    def test_an_older_box_sending_no_reasons_prints_no_footnote(self):
        _, err = self._render({"usb1": None}, {})
        self.assertEqual(err.strip(), "")

    def test_unreadable_detail_survives_to_the_user(self):
        _, err = self._render(
            {"usb1": None},
            {"usb1": "unreadable: PortStateError: Acroname error code 12"},
        )
        self.assertIn("Acroname error code 12", err)

    def test_the_table_itself_is_unchanged_on_stdout(self):
        out, _ = self._render({"usb1": None}, {"usb1": "deadline"})
        self.assertIn("–", out)
        self.assertNotIn("deadline", out,
                         "reasons belong in the footnote, not the cell")


if __name__ == "__main__":
    unittest.main()

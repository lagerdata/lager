# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the ``lager nets state`` display table (_display_table_with_state).

Validates that the State column shows live state strings, "–" for unknown,
and that the table still renders instrument groupings correctly.
"""
import io
import unittest
from contextlib import redirect_stdout

from cli.commands.box.nets import _display_table_with_state

_LONG_ADDR = "USB0::0x10C4::0xEA60::3e6fe522e591ef11a56e3ec5cc16735d::INSTR"


class DisplayTableWithStateTest(unittest.TestCase):
    def _render(self, records, state_map):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _display_table_with_state(records, state_map)
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


if __name__ == "__main__":
    unittest.main()

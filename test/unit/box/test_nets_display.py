# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Regression tests for the `lager nets` table: uart channel paths and the
bracketed instrument address must never be truncated (0.27.x cut uart
pins at 10 chars and addresses at 45).
"""
import io
import unittest
from contextlib import redirect_stdout

from cli.commands.box.nets import _display_table

_LONG_ADDR = "USB0::0x10C4::0xEA60::3e6fe522e591ef11a56e3ec5cc16735d::INSTR"


class DisplayTableNoTruncation(unittest.TestCase):
    def _render(self, records):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _display_table(records)
        return buf.getvalue()

    def test_uart_pin_shown_in_full(self):
        out = self._render([{
            "name": "PODSIM",
            "role": "uart",
            "pin": "/dev/ttyUSB0",
            "instrument": "SiLabs_CP210x",
            "address": _LONG_ADDR,
        }])
        self.assertIn("/dev/ttyUSB0", out)

    def test_address_shown_in_full(self):
        out = self._render([{
            "name": "UART",
            "role": "uart",
            "pin": "/dev/ttyUSB1",
            "instrument": "SiLabs_CP210x",
            "address": _LONG_ADDR,
        }])
        self.assertIn(f"[{_LONG_ADDR}]", out)
        self.assertNotIn("...", out)


if __name__ == "__main__":
    unittest.main()

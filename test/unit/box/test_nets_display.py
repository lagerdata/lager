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


class DisplayTablePurposeColumn(unittest.TestCase):
    """The Purpose column is conditional, so an undescribed bench is unchanged.

    Metadata only became visible in `lager nets` once the control plane could
    write it, and most benches have none. Always reserving a column would widen
    every table for a field that is usually empty.
    """

    def _render(self, records):
        buf = io.StringIO()
        with redirect_stdout(buf):
            _display_table(records)
        return buf.getvalue()

    def _net(self, name, **extra):
        rec = {
            "name": name, "role": "uart", "pin": "/dev/ttyUSB0",
            "instrument": "SiLabs_CP210x", "address": _LONG_ADDR,
        }
        rec.update(extra)
        return rec

    def test_column_absent_when_no_net_has_a_purpose(self):
        self.assertNotIn("Purpose", self._render([self._net("uart1")]))

    def test_column_appears_when_any_net_has_a_purpose(self):
        out = self._render([self._net("uart1", purpose="DUT console")])
        self.assertIn("Purpose", out)
        self.assertIn("DUT console", out)

    def test_undescribed_net_shows_a_placeholder_not_a_blank(self):
        out = self._render([
            self._net("uart1", purpose="DUT console"),
            self._net("uart2"),
        ])
        self.assertIn("Purpose", out)
        # A blank cell would read as a rendering fault next to a filled one.
        uart2_line = [ln for ln in out.splitlines() if "uart2" in ln][0]
        self.assertTrue(uart2_line.rstrip().endswith("-"))

    def test_long_purpose_is_ellipsised_not_wrapped(self):
        long_purpose = "This sentence is considerably longer than the column allows and must be cut"
        out = self._render([self._net("uart1", purpose=long_purpose)])
        line = [ln for ln in out.splitlines() if "uart1" in ln][0]
        self.assertIn("…", line)
        self.assertNotIn(long_purpose, out)
        # One net stays one row; wrapping would break the tree alignment.
        self.assertEqual(len([ln for ln in out.splitlines() if "uart1" in ln]), 1)

    def test_newlines_in_a_purpose_do_not_break_the_table(self):
        out = self._render([self._net("uart1", purpose="first line\nsecond line")])
        line = [ln for ln in out.splitlines() if "uart1" in ln][0]
        self.assertIn("first line second line", line)

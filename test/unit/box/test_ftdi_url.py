# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Addressing one channel of a multi-channel FTDI as GPIO/I2C/SPI.

The three drivers used to hardcode ``ftdi://ftdi:232h[:serial]/1``, pinning
both the product and the interface. That made an FT2232H gpio/i2c/spi net
impossible to open even though `INSTRUMENT_NET_MAP` advertised one, and left
no way at all to reach channel B/C/D.

The two things most worth pinning here:

* **Existing single-channel FT232H nets must be byte-identical.** Those URLs
  are the only thing standing between this change and every FTDI net on every
  bench. The default-path assertions below are the regression guard.
* **The base-0/base-1 split.** pyftdi interfaces are 1-based; OpenOCD's
  ``ftdi channel`` is 0-based. Everything in lager speaks 0-based and the +1
  happens once, in ``build_ftdi_url``. Asserted against the debug path's
  parser so the two cannot drift apart.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
PROBES_PATH = os.path.join(REPO_ROOT, "box", "lager", "debug", "probes.py")

from lager.util import ftdi_url  # noqa: E402


def _load_probes():
    key = "_probes_for_ftdi_url_tests"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, PROBES_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


class BackCompatTests(unittest.TestCase):
    """Every URL an existing net produced today, produced identically."""

    def test_serial_only_matches_the_old_hardcoded_url(self):
        self.assertEqual(
            ftdi_url.build_ftdi_url(serial="FT123"),
            "ftdi://ftdi:232h:FT123/1")

    def test_no_serial_matches_the_old_hardcoded_url(self):
        self.assertEqual(ftdi_url.build_ftdi_url(), "ftdi://ftdi:232h/1")

    def test_ft232h_pid_is_the_same_as_no_pid(self):
        self.assertEqual(
            ftdi_url.build_ftdi_url(serial="FT123", pid="6014"),
            ftdi_url.build_ftdi_url(serial="FT123"))

    def test_unknown_pid_falls_back_rather_than_failing(self):
        """A net with an address we cannot classify keeps working."""
        self.assertEqual(
            ftdi_url.build_ftdi_url(serial="FT123", pid="dead"),
            "ftdi://ftdi:232h:FT123/1")


class ProductTests(unittest.TestCase):

    def test_each_supported_pid(self):
        for pid, product in (("6014", "232h"), ("6010", "2232h"),
                             ("6011", "4232h")):
            with self.subTest(pid=pid):
                self.assertEqual(ftdi_url.product_for_pid(pid), product)

    def test_pid_accepted_in_every_form_a_record_carries_it(self):
        for form in ("6011", "0x6011", "0X6011", " 6011 ", 0x6011):
            with self.subTest(form=form):
                self.assertEqual(ftdi_url.product_for_pid(form), "4232h")

    def test_ft2232h_gpio_url_is_now_reachable(self):
        """The bug that was already shipping: advertised, impossible to open."""
        self.assertEqual(
            ftdi_url.build_ftdi_url(serial="FT2A", pid="6010"),
            "ftdi://ftdi:2232h:FT2A/1")


class InterfaceNumberingTests(unittest.TestCase):

    def test_letters_and_indices_agree(self):
        for letter, idx in (("A", 0), ("B", 1), ("C", 2), ("D", 3)):
            with self.subTest(letter=letter):
                self.assertEqual(ftdi_url.parse_interface(letter), idx)
                self.assertEqual(ftdi_url.parse_interface(letter.lower()), idx)
                self.assertEqual(ftdi_url.parse_interface(idx), idx)
                self.assertEqual(ftdi_url.parse_interface(str(idx)), idx)
                self.assertEqual(ftdi_url.parse_interface("@" + letter), idx)

    def test_none_and_empty_mean_default(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(ftdi_url.parse_interface(value))

    def test_a_typo_raises_rather_than_silently_driving_channel_a(self):
        for bad in ("E", "Z", "9", 4, -1, "b1", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ftdi_url.FtdiUrlError):
                    ftdi_url.parse_interface(bad)

    def test_url_interface_is_one_based(self):
        """The off-by-one that would silently drive the wrong pins."""
        self.assertEqual(
            ftdi_url.build_ftdi_url(serial="F", pid="6011", interface=0),
            "ftdi://ftdi:4232h:F/1")
        self.assertEqual(
            ftdi_url.build_ftdi_url(serial="F", pid="6011", interface=2),
            "ftdi://ftdi:4232h:F/3")

    def test_letter_map_agrees_with_the_debug_path(self):
        """Deliberate duplicate of probes._CHANNEL_LETTER_TO_INDEX.

        probes.py is import-standalone on purpose, so ftdi_url cannot import
        it. This is what keeps the user-facing vocabulary identical on both
        paths: '@B' must mean the same channel to OpenOCD and to pyftdi.
        """
        probes = _load_probes()
        self.assertEqual(ftdi_url._CHANNEL_LETTER_TO_INDEX,
                         probes._CHANNEL_LETTER_TO_INDEX)

    def test_same_letter_resolves_the_same_on_both_paths(self):
        probes = _load_probes()
        for letter in "ABCD":
            with self.subTest(letter=letter):
                _target, openocd_channel = probes.parse_device_field(
                    f"STM32F4x@{letter}")
                ours = ftdi_url.parse_interface(letter)
                self.assertEqual(ours, openocd_channel)
                # ...and pyftdi's URL carries that same channel, plus one.
                url = ftdi_url.build_ftdi_url(pid="6011", interface=ours)
                self.assertTrue(url.endswith(f"/{openocd_channel + 1}"))


class ChannelValidationTests(unittest.TestCase):

    def test_single_channel_part_refuses_interface_b(self):
        with self.assertRaises(ftdi_url.FtdiUrlError):
            ftdi_url.validate_interface("232h", 1)

    def test_ft2232h_has_two_channels(self):
        ftdi_url.validate_interface("2232h", 1)
        with self.assertRaises(ftdi_url.FtdiUrlError):
            ftdi_url.validate_interface("2232h", 2)

    def test_ft4232h_has_four_channels_for_gpio(self):
        for idx in range(4):
            with self.subTest(idx=idx):
                ftdi_url.validate_interface("4232h", idx)

    def test_ft4232h_c_and_d_have_no_mpsse(self):
        """I2C/SPI are MPSSE protocols; C and D on this part are not.

        Without this the net is accepted and then fails deep inside pyftdi
        with an error that never mentions the channel.
        """
        for idx in (2, 3):
            with self.subTest(idx=idx):
                with self.assertRaises(ftdi_url.FtdiUrlError) as ctx:
                    ftdi_url.validate_interface("4232h", idx,
                                                require_mpsse=True)
                self.assertIn("MPSSE", str(ctx.exception))

    def test_ft4232h_a_and_b_do_have_mpsse(self):
        for idx in (0, 1):
            with self.subTest(idx=idx):
                ftdi_url.validate_interface("4232h", idx, require_mpsse=True)

    def test_gpio_is_allowed_where_mpsse_is_not(self):
        """The distinction that makes the 4-channel fixture work at all."""
        ftdi_url.validate_interface("4232h", 2)
        with self.assertRaises(ftdi_url.FtdiUrlError):
            ftdi_url.validate_interface("4232h", 2, require_mpsse=True)

    def test_pin_width_has_no_acbus_on_the_ft4232h(self):
        self.assertEqual(ftdi_url.pin_width("232h"), 16)
        self.assertEqual(ftdi_url.pin_width("2232h"), 16)
        self.assertEqual(ftdi_url.pin_width("4232h"), 8)


if __name__ == "__main__":
    unittest.main()

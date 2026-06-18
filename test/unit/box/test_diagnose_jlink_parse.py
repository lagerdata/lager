#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the box-side J-Link diagnose parsers in
``box/lager/http_handlers/diagnose.py``: ``_parse_emu_list`` (JLinkExe
``ShowEmuList`` output), ``_serial_in_emu_list`` (zero-pad-tolerant probe
matching), and ``_parse_connect_output`` (classifying a ``connect`` attempt
into target-power / comms / locked / wrong-device buckets).

These back the CLI's ``_classify_jlink`` decision tree, so they're pinned with
captured-style JLinkExe text and run without any hardware. The module is loaded
straight from its file (it only needs flask, which the box env ships) to avoid
dragging in the rest of the ``lager`` package.
"""

import importlib.util
import os
import unittest

HERE = os.path.dirname(__file__)
DIAGNOSE_PATH = os.path.normpath(
    os.path.join(HERE, "..", "..", "..", "box", "lager", "http_handlers", "diagnose.py")
)

_spec = importlib.util.spec_from_file_location("_diag_jlink_under_test", DIAGNOSE_PATH)
diag = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diag)


# Real JLinkExe output captured from PRD-1 (unpowered nRF7002-DK on a J-Link
# ULTRA+). No "VTref=" number is printed on this REPL path — just the phrase.
UNPOWERED_CONNECT = (
    'Device "NRF5340_XXAA_APP" selected.\r\n\r\n\r\n'
    'Connecting to target via SWD\r\n'
    'Target voltage too low. Please check '
    'https://kb.segger.com/J-Link_cannot_connect_to_the_CPU#Target_connection.\r\n'
    'Error occurred: Could not connect to the target device.\r\n'
    'For troubleshooting steps visit: https://kb.segger.com/J-Link_Troubleshooting\r\n'
)


class ParseEmuListTests(unittest.TestCase):

    def test_parses_multiple_probes(self):
        text = (
            "J-Link[0]: Connection: USB, Serial number: 000504402175, ProductName: J-Link Plus\n"
            "J-Link[1]: Connection: USB, Serial number: 51014439, ProductName: J-Link"
        )
        probes = diag._parse_emu_list(text)
        self.assertEqual(len(probes), 2)
        self.assertEqual(probes[0], {'serial': '000504402175', 'product': 'J-Link Plus'})
        self.assertEqual(probes[1]['serial'], '51014439')

    def test_trims_trailing_nickname_field(self):
        # Newer JLinkExe appends ", Nickname: <not set>" — it must not leak into
        # the product string (captured verbatim from PRD-1).
        text = ("J-Link[0]: Connection: USB, Serial number: 504402175, "
                "ProductName: J-Link ULTRA+, Nickname: <not set>")
        probes = diag._parse_emu_list(text)
        self.assertEqual(probes[0], {'serial': '504402175', 'product': 'J-Link ULTRA+'})

    def test_empty_and_none(self):
        self.assertEqual(diag._parse_emu_list(""), [])
        self.assertEqual(diag._parse_emu_list(None), [])

    def test_no_probes_connected_line(self):
        # JLinkExe prints a count line and no per-probe rows when nothing's there.
        self.assertEqual(diag._parse_emu_list("J-Link[0]: not present\n"), [])


class SerialMatchTests(unittest.TestCase):

    def setUp(self):
        self.emus = [
            {'serial': '000504402175', 'product': 'J-Link Plus'},
            {'serial': '51014439', 'product': 'J-Link'},
        ]

    def test_exact_match(self):
        self.assertTrue(diag._serial_in_emu_list('51014439', self.emus))

    def test_zero_pad_tolerant_both_directions(self):
        # Address serial carries leading zeros; ShowEmuList may drop them (or vice-versa).
        self.assertTrue(diag._serial_in_emu_list('000504402175', self.emus))
        self.assertTrue(diag._serial_in_emu_list('504402175', self.emus))

    def test_absent_serial(self):
        self.assertFalse(diag._serial_in_emu_list('999999', self.emus))

    def test_no_substring_false_positive(self):
        # Exact match only — a short serial must NOT match a longer one that
        # merely ends with it (the old endswith logic false-positived here).
        emus = [{'serial': '51014431', 'product': 'J-Link'}]
        self.assertFalse(diag._serial_in_emu_list('1', emus))
        self.assertFalse(diag._serial_in_emu_list('431', emus))

    def test_zero_serial_does_not_match_everything(self):
        # serial "0" normalises to "" — must NOT match every visible probe.
        self.assertFalse(diag._serial_in_emu_list('0', self.emus))

    def test_no_emus_is_false(self):
        self.assertFalse(diag._serial_in_emu_list('51014439', []))

    def test_unparseable_serial_with_single_probe_is_visible(self):
        # No serial from the address (unprogrammed EEPROM): a single visible
        # probe is, by elimination, this net's probe.
        self.assertTrue(diag._serial_in_emu_list(None, [{'serial': 'x', 'product': 'J-Link'}]))
        self.assertFalse(diag._serial_in_emu_list(None, []))


class ParseConnectOutputTests(unittest.TestCase):

    def test_healthy_connect(self):
        out = diag._parse_connect_output(
            "Connecting to target via SWD\nVTref=3.300V\nCortex-M33 identified.\nConnected to target"
        )
        self.assertTrue(out['connect_ok'])
        self.assertEqual(out['connect_error_class'], 'ok')
        self.assertEqual(out['vtref_mv'], 3300)
        self.assertEqual(out['core'], 'Cortex-M33')

    def test_real_unpowered_dk_is_no_target_power(self):
        # The captured PRD-1 unpowered output: "Target voltage too low" + "Could
        # not connect to the target device". No-power must win over the comms line.
        out = diag._parse_connect_output(UNPOWERED_CONNECT)
        self.assertFalse(out['connect_ok'])
        self.assertEqual(out['connect_error_class'], 'no_target_power')

    def test_vtarget_alt_format_and_low_voltage(self):
        # Some firmwares print "VTarget = 0.10 V" with spaces; <0.3V → no power.
        out = diag._parse_connect_output("VTarget = 0.10 V\nCould not connect to the target device")
        self.assertEqual(out['vtref_mv'], 100)
        self.assertEqual(out['connect_error_class'], 'no_target_power')

    def test_no_target_comms_with_power_present(self):
        # Powered (good VTref) but no core found → comms/wiring, not power.
        out = diag._parse_connect_output(
            "VTref=3.300V\nCould not connect to the target device.\n"
            "Could not find core in Coresight setup"
        )
        self.assertEqual(out['connect_error_class'], 'no_target_comms')

    def test_locked_device(self):
        out = diag._parse_connect_output("VTref=3.300V\nDevice is locked. Need IDCODE to unlock.")
        self.assertEqual(out['connect_error_class'], 'locked')

    def test_locked_via_access_port_protection(self):
        out = diag._parse_connect_output("VTref=3.3V\nError: AP access port protection is enabled")
        self.assertEqual(out['connect_error_class'], 'locked')

    def test_wrong_device(self):
        out = diag._parse_connect_output("Unknown device selected.")
        self.assertEqual(out['connect_error_class'], 'wrong_device')

    def test_power_outranks_generic_comms_failure(self):
        # VTref≈0 should win over a generic "cannot connect" so the user is told
        # the real root cause (no power), not a wiring red herring.
        out = diag._parse_connect_output("VTref=0.001V\nCannot connect to target.")
        self.assertEqual(out['connect_error_class'], 'no_target_power')

    def test_empty(self):
        out = diag._parse_connect_output("")
        self.assertFalse(out['connect_ok'])
        self.assertEqual(out['connect_error_class'], 'other')
        self.assertIsNone(out['vtref_mv'])


if __name__ == '__main__':
    unittest.main()

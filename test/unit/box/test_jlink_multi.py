# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/debug/probes.py — the helpers that let multiple
J-Link probes run concurrently on the same box (one GDB slot per probe).

These tests load probes.py directly via importlib so they don't pull in the
full lager.debug package (which imports hardware drivers / pyvisa).
"""

import importlib.util
import os
import unittest


HERE = os.path.dirname(__file__)
PROBES_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug', 'probes.py')
)


def _load_probes():
    spec = importlib.util.spec_from_file_location('probes', PROBES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probes = _load_probes()


class ParseJLinkSerialTests(unittest.TestCase):
    def test_standard_visa_resource(self):
        self.assertEqual(
            probes.parse_jlink_serial('USB0::0x1366::0x0101::000051014439::INSTR'),
            '000051014439',
        )

    def test_no_usb_index(self):
        self.assertEqual(
            probes.parse_jlink_serial('USB::0x1366::0x0101::000051017734::INSTR'),
            '000051017734',
        )

    def test_alphanumeric_serial(self):
        self.assertEqual(
            probes.parse_jlink_serial('USB1::0x1366::0x0102::ABC123::INSTR'),
            'ABC123',
        )

    def test_case_insensitive_segments(self):
        self.assertEqual(
            probes.parse_jlink_serial('usb0::0X1366::0x0101::000051014439::instr'),
            '000051014439',
        )

    def test_wrong_vendor_id_returns_none(self):
        self.assertIsNone(
            probes.parse_jlink_serial('USB0::0xDEAD::0x0101::000051014439::INSTR')
        )

    def test_garbage_input_returns_none(self):
        self.assertIsNone(probes.parse_jlink_serial('not a visa string'))

    def test_empty_or_none_returns_none(self):
        self.assertIsNone(probes.parse_jlink_serial(None))
        self.assertIsNone(probes.parse_jlink_serial(''))
        self.assertIsNone(probes.parse_jlink_serial('   '))


class ResolveSerialFromNetTests(unittest.TestCase):
    def test_full_net_dict(self):
        net = {
            'name': 'debug1',
            'role': 'debug',
            'address': 'USB0::0x1366::0x0101::000051014439::INSTR',
        }
        self.assertEqual(probes.resolve_serial_from_net(net), '000051014439')

    def test_missing_address(self):
        self.assertIsNone(probes.resolve_serial_from_net({'name': 'debug1'}))

    def test_non_dict_input(self):
        self.assertIsNone(probes.resolve_serial_from_net(None))
        self.assertIsNone(probes.resolve_serial_from_net('string'))


class ComputeSlotTests(unittest.TestCase):
    def test_assigns_by_sorted_order(self):
        serials = ['000051017734', '000051014439']
        # Sorted: 000051014439, 000051017734 → slots 0, 1
        self.assertEqual(probes.compute_slot('000051014439', serials), 0)
        self.assertEqual(probes.compute_slot('000051017734', serials), 1)

    def test_input_order_does_not_matter(self):
        a = probes.compute_slot('B', ['A', 'B', 'C'])
        b = probes.compute_slot('B', ['C', 'B', 'A'])
        self.assertEqual(a, b)

    def test_serial_not_in_list_falls_back_to_zero(self):
        self.assertEqual(probes.compute_slot('Z', ['A', 'B']), 0)

    def test_none_serial_is_slot_zero(self):
        self.assertEqual(probes.compute_slot(None, ['A', 'B']), 0)

    def test_skips_empty_entries(self):
        self.assertEqual(probes.compute_slot('A', ['', None, 'A']), 0)


class PortHelpersTests(unittest.TestCase):
    def test_gdb_port_for_slot(self):
        self.assertEqual(probes.gdb_port_for_slot(0), 2331)
        self.assertEqual(probes.gdb_port_for_slot(1), 2332)
        self.assertEqual(probes.gdb_port_for_slot(2), 2333)
        self.assertEqual(probes.gdb_port_for_slot(3), 2334)

    def test_rtt_port_for_slot_reserves_two_channels_per_probe(self):
        self.assertEqual(probes.rtt_port_for_slot(0), 9090)
        self.assertEqual(probes.rtt_port_for_slot(1), 9092)
        self.assertEqual(probes.rtt_port_for_slot(2), 9094)
        self.assertEqual(probes.rtt_port_for_slot(3), 9096)


class PathHelpersTests(unittest.TestCase):
    def test_legacy_paths_when_serial_is_none(self):
        self.assertEqual(probes.jlink_gdbserver_pidfile(None), '/tmp/jlink_gdbserver.pid')
        self.assertEqual(probes.jlink_gdbserver_logfile(None), '/tmp/jlink_gdbserver.log')
        self.assertEqual(probes.jlink_pidfile(None), '/tmp/jlink.pid')
        self.assertEqual(probes.jlink_logfile(None), '/tmp/jlink.log')

    def test_per_serial_paths(self):
        self.assertEqual(
            probes.jlink_gdbserver_pidfile('000051014439'),
            '/tmp/jlink_gdbserver_000051014439.pid',
        )
        self.assertEqual(
            probes.jlink_gdbserver_logfile('000051014439'),
            '/tmp/jlink_gdbserver_000051014439.log',
        )
        self.assertEqual(
            probes.jlink_pidfile('000051014439'),
            '/tmp/jlink_000051014439.pid',
        )
        self.assertEqual(
            probes.jlink_logfile('000051014439'),
            '/tmp/jlink_000051014439.log',
        )

    def test_two_serials_map_to_distinct_paths(self):
        a = probes.jlink_gdbserver_pidfile('000051014439')
        b = probes.jlink_gdbserver_pidfile('000051017734')
        self.assertNotEqual(a, b)


if __name__ == '__main__':
    unittest.main()

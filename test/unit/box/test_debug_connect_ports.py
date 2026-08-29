# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Port overrides on POST /debug/connect are coerced at the boundary.

``gdb_port``/``swo_port``/``telnet_port``/``rtt_telnet_port`` are client
overrides, and they are used to build the debug backend's command line. A port
is an integer, so the boundary coerces and range-checks it rather than
forwarding whatever arrived.

Hardware-only deps are stubbed the way test_jlink_multi_gdbserver_select.py
does it.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'pexpect', 'pexpect.replwrap', 'pexpect.exceptions',
    'pygdbmi', 'pygdbmi.gdbcontroller', 'pygdbmi.constants',
]:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.debug.service import _port_or_reject  # noqa: E402


class PortOrReject(unittest.TestCase):
    def test_absent_key_uses_the_slot_default(self):
        self.assertEqual(_port_or_reject({}, 'gdb_port', 2331), 2331)

    def test_integer_override_passes_through(self):
        self.assertEqual(_port_or_reject({'gdb_port': 3333}, 'gdb_port', 2331), 3333)

    def test_numeric_string_is_coerced(self):
        # The CLI sends JSON; a caller that quotes the number still works.
        self.assertEqual(_port_or_reject({'gdb_port': '3333'}, 'gdb_port', 2331), 3333)

    def test_absent_key_with_a_none_default_stays_none(self):
        # An absent key yields the default untouched, whatever it is.
        self.assertIsNone(_port_or_reject({}, 'telnet_port', None))

    def test_explicit_null_is_refused(self):
        # Distinct from an absent key: a null sent on the wire used to flow on
        # and fail later in `gdb_port + 1` rather than being named here.
        with self.assertRaises(ValueError) as ctx:
            _port_or_reject({'gdb_port': None}, 'gdb_port', 2331)
        self.assertIn('must be an integer', str(ctx.exception))

    def test_booleans_are_not_ports(self):
        # bool subclasses int, so `true` would otherwise coerce to port 1.
        for bad in (True, False):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    _port_or_reject({'gdb_port': bad}, 'gdb_port', 2331)

    def test_non_numeric_is_refused(self):
        for bad in ('3333\nshutdown', '2331; reboot', 'abc', '[exec ls]', {}, []):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as ctx:
                    _port_or_reject({'gdb_port': bad}, 'gdb_port', 2331)
                self.assertIn('must be an integer', str(ctx.exception))

    def test_out_of_range_is_refused(self):
        for bad in (0, -1, 65536, 999999):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError) as ctx:
                    _port_or_reject({'gdb_port': bad}, 'gdb_port', 2331)
                self.assertIn('between 1 and 65535', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()

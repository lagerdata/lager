# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``box/lager/debug/probes.py`` VISA address parsing.

Regression coverage for the empty-serial slot case (FTDIs whose EEPROM
was never programmed): the box scanner emits
``USB0::0x0403::0x6011::::INSTR`` for those, and the parser must still
extract VID/PID so backend resolution picks OpenOCD instead of
defaulting to J-Link.
"""

import importlib.util
import os
import sys
import types
import unittest


HERE = os.path.dirname(__file__)
PROBES_PATH = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug', 'probes.py')
)


def _load_probes():
    """Load ``probes.py`` standalone so we don't drag in the rest of the
    ``lager.debug`` package (which has hardware-driver imports that
    aren't available in CI)."""
    spec = importlib.util.spec_from_file_location('probes_under_test', PROBES_PATH)
    module = importlib.util.module_from_spec(spec)
    # The module only imports stdlib + ``lager.cache``-free helpers, but
    # set a parent package anyway so any future relative imports keep
    # working.
    pkg = types.ModuleType('stub_probes_pkg')
    pkg.__path__ = [os.path.dirname(PROBES_PATH)]
    sys.modules.setdefault('stub_probes_pkg', pkg)
    module.__package__ = 'stub_probes_pkg'
    sys.modules['probes_under_test'] = module
    spec.loader.exec_module(module)
    return module


class TestParseProbeAddress(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.probes = _load_probes()

    def test_full_address_with_serial(self):
        vid, pid, serial = self.probes.parse_probe_address(
            'USB0::0x0403::0x6011::FT5XYZAB::INSTR'
        )
        self.assertEqual(vid, '0403')
        self.assertEqual(pid, '6011')
        self.assertEqual(serial, 'FT5XYZAB')

    def test_empty_serial_slot_still_parses_vid_pid(self):
        """Regression: FT4232H without programmed EEPROM serial.

        Before the regex was relaxed, ``([^:]+)`` rejected ``::::`` and
        ``parse_probe_address`` returned ``(None, None, None)``. That
        broke ``resolve_backend`` for those probes and fell through to
        the J-Link default, producing the canned "Failed to connect"
        error users saw when ``lager debug gdbserver`` was run against
        a serial-less FT4232H.
        """
        vid, pid, serial = self.probes.parse_probe_address(
            'USB0::0x0403::0x6011::::INSTR'
        )
        self.assertEqual(vid, '0403')
        self.assertEqual(pid, '6011')
        self.assertIsNone(serial)

    def test_garbage_address_returns_none_tuple(self):
        self.assertEqual(
            self.probes.parse_probe_address('not-a-visa-address'),
            (None, None, None),
        )

    def test_none_and_non_string_handled(self):
        self.assertEqual(
            self.probes.parse_probe_address(None),
            (None, None, None),
        )
        self.assertEqual(
            self.probes.parse_probe_address(12345),
            (None, None, None),
        )

    def test_resolve_backend_picks_openocd_for_empty_serial_ftdi(self):
        """The whole point of the regex relaxation: an FT4232H with no
        EEPROM serial must still be classified as OpenOCD-backed."""
        backend = self.probes.resolve_backend({
            'address': 'USB0::0x0403::0x6011::::INSTR',
        })
        self.assertEqual(backend, self.probes.BACKEND_OPENOCD)

    def test_resolve_backend_picks_jlink_when_vid_matches(self):
        backend = self.probes.resolve_backend({
            'address': 'USB0::0x1366::0x1024::123456789::INSTR',
        })
        self.assertEqual(backend, self.probes.BACKEND_JLINK)


if __name__ == '__main__':
    unittest.main()

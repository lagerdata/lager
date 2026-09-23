# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The shared erase-range rules: ``lager.debug.erase_bounds`` and the pieces
around it that turn a requested range into a backend operation.

``lager debug <net> erase --erase-start/--erase-size`` and
``DebugNet.erase(start, length)`` reach two backends. Both check the range
with ``validate_bounds`` and both report it with ``format_bounds``, so the
rules live in one module and are pinned here once:

* the range must be a positive number of bytes inside the 32-bit address
  space, and on a DA1469x inside the QSPI XIP window;
* the DA1469x loader takes flash offsets, so ``xip_range_to_flash_offset``
  translates an absolute XIP range and refuses one that leaves the window
  (``xip_to_flash_offset`` cannot serve: it treats ``0`` as "start of QSPI"
  and checks only the start);
* every erase timeout was sized for the 1 MiB default and scales with the
  range: the loader poll, the OpenOCD ``flash erase_address`` wait, and the
  J-Link Commander wait (pexpect's default 30 s per command);
* ``jlink.py`` cannot import this module (tests load it standalone), so its
  resolution of request > script line > default is checked here too.

Modules load by path into a synthetic package, as ``test_openocd_flash.py``
does, so no hardware SDK is imported.
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(__file__)
DEBUG_DIR = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug')
)


def _load_module(name, path, package=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_pkg_name = 'stub_erase_bounds_pkg'
_pkg = types.ModuleType(_pkg_name)
_pkg.__path__ = [DEBUG_DIR]
sys.modules[_pkg_name] = _pkg
probes = _load_module(f'{_pkg_name}.probes', os.path.join(DEBUG_DIR, 'probes.py'), package=_pkg_name)
openocd = _load_module(f'{_pkg_name}.openocd', os.path.join(DEBUG_DIR, 'openocd.py'), package=_pkg_name)
loader = _load_module(
    f'{_pkg_name}.da1469x_loader', os.path.join(DEBUG_DIR, 'da1469x_loader.py'), package=_pkg_name)
bounds = _load_module(
    f'{_pkg_name}.erase_bounds', os.path.join(DEBUG_DIR, 'erase_bounds.py'), package=_pkg_name)
# Standalone, with no parent package, as jlink.py's own tests load it.
jlink = _load_module('jlink_erase_bounds_copy', os.path.join(DEBUG_DIR, 'jlink.py'))

XIP = loader.QSPI_XIP_BASE          # 0x16000000
XIP_END = loader.QSPI_XIP_END       # 0x18000000, exclusive
KIB = 1 << 10
MIB = 1 << 20


class ValidateBoundsTests(unittest.TestCase):

    def test_a_range_inside_the_window_passes_on_a_da1469x(self):
        bounds.validate_bounds(XIP, 2 * MIB, da1469x=True)
        bounds.validate_bounds(XIP_END - 4 * KIB, 4 * KIB, da1469x=True)

    def test_the_same_values_pass_anywhere_on_another_part(self):
        bounds.validate_bounds(0, 4 * KIB, da1469x=False)
        bounds.validate_bounds(0x08000000, MIB, da1469x=False)

    def test_only_ints_are_accepted(self):
        for start, length in [(True, MIB), (XIP, False), ('0x16000000', MIB),
                              (XIP, 1.5), (None, MIB), (XIP, None)]:
            with self.subTest(start=start, length=length):
                with self.assertRaises(ValueError):
                    bounds.validate_bounds(start, length, da1469x=False)

    def test_a_negative_start_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'negative'):
            bounds.validate_bounds(-1, MIB, da1469x=False)

    def test_a_zero_or_negative_size_is_refused(self):
        for length in (0, -4096):
            with self.subTest(length=length):
                with self.assertRaisesRegex(ValueError, 'greater than 0'):
                    bounds.validate_bounds(XIP, length, da1469x=False)

    def test_a_range_past_the_32_bit_address_space_is_refused(self):
        with self.assertRaisesRegex(ValueError, '32-bit'):
            bounds.validate_bounds(0xFFFFF000, 8 * KIB, da1469x=False)
        bounds.validate_bounds(0xFFFFF000, 4 * KIB, da1469x=False)  # ends exactly at 2**32

    def test_a_da1469x_range_below_the_window_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'QSPI XIP window') as ctx:
            bounds.validate_bounds(0x15000000, MIB, da1469x=True)
        self.assertIn('0x16000000-0x17FFFFFF', str(ctx.exception))

    def test_a_da1469x_range_past_the_window_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'QSPI XIP window'):
            bounds.validate_bounds(XIP_END - MIB, 2 * MIB, da1469x=True)

    def test_zero_is_not_an_alias_for_the_start_of_qspi(self):
        with self.assertRaisesRegex(ValueError, 'QSPI XIP window'):
            bounds.validate_bounds(0, MIB, da1469x=True)


class FormatTests(unittest.TestCase):

    def test_size_units_are_exact(self):
        self.assertEqual(bounds.format_size(2 * MIB), '2 MiB')
        self.assertEqual(bounds.format_size(512 * KIB), '512 KiB')
        self.assertEqual(bounds.format_size(3 * MIB + 512 * KIB), '3584 KiB')
        self.assertEqual(bounds.format_size(4100), '4100 bytes')

    def test_bounds_name_an_inclusive_end(self):
        self.assertEqual(bounds.format_bounds(XIP, 2 * MIB), '0x16000000-0x161FFFFF (2 MiB)')
        self.assertEqual(bounds.format_bounds(0x08000000, 4 * KIB), '0x08000000-0x08000FFF (4 KiB)')

    def test_bounds_dict_is_what_the_service_reports(self):
        self.assertEqual(bounds.bounds_dict(XIP, MIB, 'default'), {
            'start': XIP,
            'end': XIP + MIB - 1,
            'length': MIB,
            'source': 'default',
            'text': '0x16000000-0x160FFFFF (1 MiB)',
        })


class ScaledTimeoutTests(unittest.TestCase):

    def test_one_mib_or_less_keeps_the_base(self):
        self.assertEqual(bounds.scaled_timeout_s(60.0, MIB), 60.0)
        self.assertEqual(bounds.scaled_timeout_s(60.0, 1), 60.0)

    def test_larger_ranges_get_a_budget_per_mib_rounded_up(self):
        self.assertEqual(bounds.scaled_timeout_s(60.0, 2 * MIB), 120.0)
        self.assertEqual(bounds.scaled_timeout_s(60.0, MIB + 1), 120.0)


class LoaderOffsetTests(unittest.TestCase):
    """``xip_range_to_flash_offset``: absolute XIP range in, flash offset out."""

    def test_the_start_of_qspi_is_offset_zero(self):
        self.assertEqual(loader.xip_range_to_flash_offset(XIP, 2 * MIB), 0)

    def test_an_offset_range_translates(self):
        self.assertEqual(loader.xip_range_to_flash_offset(XIP + 0x200000, MIB), 0x200000)

    def test_a_start_below_the_window_is_refused(self):
        with self.assertRaises(loader.Da1469xLoaderError) as ctx:
            loader.xip_range_to_flash_offset(XIP - 1, MIB)
        self.assertIn('QSPI XIP window', str(ctx.exception))

    def test_an_end_past_the_window_is_refused(self):
        with self.assertRaises(loader.Da1469xLoaderError):
            loader.xip_range_to_flash_offset(XIP_END - MIB, MIB + 1)
        self.assertEqual(loader.xip_range_to_flash_offset(XIP_END - MIB, MIB), XIP_END - MIB - XIP)

    def test_zero_is_refused_here_too(self):
        # ``xip_to_flash_offset`` maps 0 to "start of QSPI" for a .bin with no
        # address; an erase range is never implicit.
        with self.assertRaises(loader.Da1469xLoaderError):
            loader.xip_range_to_flash_offset(0, MIB)

    def test_a_zero_length_is_refused(self):
        with self.assertRaises(loader.Da1469xLoaderError):
            loader.xip_range_to_flash_offset(XIP, 0)


class LoaderEraseTimeoutTests(unittest.TestCase):

    def test_the_constant_is_unchanged_and_scales_per_call(self):
        self.assertEqual(loader._FL_ERASE_TIMEOUT_S, 60.0)
        self.assertEqual(loader._fl_erase_timeout_s(MIB), 60.0)
        self.assertEqual(loader._fl_erase_timeout_s(4100), 60.0)
        self.assertEqual(loader._fl_erase_timeout_s(2 * MIB), 120.0)
        self.assertEqual(loader._fl_erase_timeout_s(2 * MIB + 1), 180.0)


class OpenOcdEraseAddressTests(unittest.TestCase):

    def test_the_wait_grows_with_the_range(self):
        rpc = openocd.OpenOcdRpc(port=6666, device='NRF52840_XXAA')
        rpc.cmd = mock.Mock(return_value='')
        rpc.flash_erase_range(0x08000000, 3 * MIB)
        rpc.cmd.assert_called_once_with('flash erase_address 0x8000000 0x82fffff', timeout=180)

    def test_a_small_range_keeps_the_bank_erase_budget(self):
        rpc = openocd.OpenOcdRpc(port=6666, device='NRF52840_XXAA')
        rpc.cmd = mock.Mock(return_value='')
        rpc.flash_erase_range(0x08000000, 4 * KIB)
        rpc.cmd.assert_called_once_with('flash erase_address 0x8000000 0x8000fff', timeout=120)


class JLinkResolutionTests(unittest.TestCase):
    """``jlink.resolve_erase_range``: request > script line > default."""

    def _script(self, text):
        handle = tempfile.NamedTemporaryFile('w', suffix='.JLinkScript', delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_another_part_with_no_request_erases_the_whole_chip(self):
        self.assertIsNone(jlink.resolve_erase_range('NRF52840_XXAA', None))

    def test_a_da1469x_with_no_request_and_no_script_takes_the_default(self):
        self.assertEqual(jlink.resolve_erase_range('DA14695', None), (XIP, MIB, 'default'))

    def test_a_script_line_is_an_inclusive_range(self):
        script = self._script('// attach\nLAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        self.assertEqual(jlink.resolve_erase_range('DA14695', script), (XIP, 2 * MIB, 'script'))

    def test_a_request_wins_over_the_script_line(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        self.assertEqual(
            jlink.resolve_erase_range('DA14695', script, XIP + MIB, MIB),
            (XIP + MIB, MIB, 'request'),
        )

    def test_a_request_applies_to_any_part(self):
        self.assertEqual(
            jlink.resolve_erase_range('NRF52840_XXAA', None, 0x08000000, 4 * KIB),
            (0x08000000, 4 * KIB, 'request'),
        )

    def test_the_script_line_is_ignored_on_another_part(self):
        script = self._script('LAGER_ERASE_RANGE: 0x16000000 0x161FFFFF\n')
        self.assertIsNone(jlink.resolve_erase_range('NRF52840_XXAA', script))

    def test_the_commander_wait_scales_with_the_range(self):
        self.assertEqual(jlink._erase_timeout_s(MIB), 30)
        self.assertEqual(jlink._erase_timeout_s(4 * KIB), 30)
        self.assertEqual(jlink._erase_timeout_s(2 * MIB), 60)
        self.assertEqual(jlink._erase_timeout_s(MIB + MIB // 2), 60)


if __name__ == '__main__':
    unittest.main()

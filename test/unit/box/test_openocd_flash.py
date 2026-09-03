# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for box/lager/debug/openocd_flash.py and the flash guard on
``OpenOcdRpc``.

``openocd_flash`` is the one place that decides how an OpenOCD-backed target
is programmed and erased: OpenOCD's own ``program`` / bank erase for most
parts, the RAM-resident flash_loader for a DA1469x, which has no QSPI driver
in mainline OpenOCD. The HTTP service and the Net API both route through it
-- ``test_debug_flash_dispatch_parity.py`` pins that. These tests pin the
decision itself, the XIP-to-offset translation, the error shapes, and the
RPC layer's refusal to send a generic flash command for a part it cannot
flash.

Loaded through the same stub-package trick as ``test_da1469x_loader.py`` so
the real ``lager`` package's hardware imports stay out of the test
environment.
"""

import importlib.util
import os
import sys
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


_pkg_name = 'stub_openocd_flash_pkg'
_pkg = types.ModuleType(_pkg_name)
_pkg.__path__ = [DEBUG_DIR]
sys.modules[_pkg_name] = _pkg
probes = _load_module(f'{_pkg_name}.probes', os.path.join(DEBUG_DIR, 'probes.py'), package=_pkg_name)
openocd = _load_module(f'{_pkg_name}.openocd', os.path.join(DEBUG_DIR, 'openocd.py'), package=_pkg_name)
loader = _load_module(
    f'{_pkg_name}.da1469x_loader', os.path.join(DEBUG_DIR, 'da1469x_loader.py'), package=_pkg_name)
openocd_flash = _load_module(
    f'{_pkg_name}.openocd_flash', os.path.join(DEBUG_DIR, 'openocd_flash.py'), package=_pkg_name)

XIP = loader.QSPI_XIP_BASE


class _Rpc:
    """Records the generic commands. Never reaches a socket."""

    def __init__(self):
        self.calls = []

    def program(self, file_path, verify=True, reset_after=True, address=None):
        self.calls.append(('program', file_path, verify, reset_after, address))
        return 'programmed'

    def flash_erase_all(self):
        self.calls.append(('flash_erase_all',))
        return 'erased'


class _Loader:
    """Fake ``flash_image`` / ``erase_range`` recording every dispatch."""

    def __init__(self):
        self.calls = []

    def flash_image(self, rpc, image_path, *, family, flash_id=0, offset=0):
        self.calls.append(('flash_image', image_path, family, flash_id, offset))
        yield 'Preparing DA1469x flash_loader from /stub/flash_loader.elf'
        yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes=4'
        yield 'Programmed 4 bytes successfully'

    def erase_range(self, rpc, *, family, flash_id=0, offset=0, length):
        self.calls.append(('erase_range', family, flash_id, offset, length))
        yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes={length}'
        yield f'Erased {length} bytes successfully'


class _DispatchCase(unittest.TestCase):
    def setUp(self):
        self.rpc = _Rpc()
        self.loader = _Loader()
        patches = [
            mock.patch.object(openocd_flash, 'flash_image', self.loader.flash_image),
            mock.patch.object(openocd_flash, 'erase_range', self.loader.erase_range),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def flash(self, device, path='/tmp/app.bin', address=None):
        return list(openocd_flash.flash_target(self.rpc, device, path, address=address))

    def erase(self, device):
        return list(openocd_flash.erase_target(self.rpc, device))


class FlashDispatchTests(_DispatchCase):
    def test_da1469x_goes_to_the_loader_with_a_flash_relative_offset(self):
        out = self.flash('DA14695', '/tmp/xl.img.bin', address=XIP)
        self.assertEqual(
            self.loader.calls,
            [('flash_image', '/tmp/xl.img.bin', 'da1469x', 0, 0)],
            'XIP 0x16000000 must reach the loader as flash offset 0x0',
        )
        self.assertEqual(self.rpc.calls, [], "must not fall through to rpc.program")
        # Progress streams through unchanged for the caller to log/collect.
        self.assertEqual(out[0], 'Preparing DA1469x flash_loader from /stub/flash_loader.elf')
        self.assertEqual(out[-1], 'Programmed 4 bytes successfully')

    def test_da1469x_nonzero_xip_address_translates(self):
        self.flash('DA14695', address=XIP + 0x8000)
        self.assertEqual(self.loader.calls[0][4], 0x8000)

    def test_da1469x_without_an_address_flashes_from_the_start_of_qspi(self):
        # .hex / .elf carry their own address; the loader gets offset 0.
        self.flash('DA14695', '/tmp/app.hex', address=None)
        self.assertEqual(self.loader.calls[0][4], 0)

    def test_da1469x_address_outside_the_xip_window_is_refused_before_any_io(self):
        with self.assertRaises(loader.Da1469xLoaderError) as ctx:
            self.flash('DA14695', address=0x08000000)
        self.assertIn('outside the DA1469x QSPI XIP window', str(ctx.exception))
        self.assertEqual(self.loader.calls, [])
        self.assertEqual(self.rpc.calls, [])

    def test_family_match_ignores_case_and_channel_suffix(self):
        for device in ('da14695', 'DA14699@A', 'DA14691@2', 'Da14697'):
            with self.subTest(device=device):
                self.loader.calls.clear()
                self.flash(device, address=XIP)
                self.assertEqual(self.loader.calls[0][0], 'flash_image')

    def test_other_device_uses_program_with_the_given_address(self):
        out = self.flash('STM32F446RE', '/tmp/app.bin', address=0x08000000)
        self.assertEqual(out, ['programmed'])
        self.assertEqual(
            self.rpc.calls,
            [('program', '/tmp/app.bin', True, True, 0x08000000)],
        )
        self.assertEqual(self.loader.calls, [], 'loader must not run for non-DA1469x')

    def test_other_device_hex_passes_no_address(self):
        self.flash('NRF52840_XXAA', '/tmp/app.hex', address=None)
        self.assertIsNone(self.rpc.calls[0][4])

    def test_program_with_empty_output_yields_nothing(self):
        self.rpc.program = lambda *a, **k: ''
        self.assertEqual(self.flash('NRF52840_XXAA', address=0), [])


class EraseDispatchTests(_DispatchCase):
    def test_da1469x_goes_to_the_loader_range_erase(self):
        out = self.erase('DA14695')
        self.assertEqual(
            self.loader.calls,
            [('erase_range', 'da1469x', 0, 0, loader.DEFAULT_ERASE_LENGTH)],
        )
        self.assertEqual(self.rpc.calls, [], "flash_erase_all can't reach QSPI NOR")
        self.assertEqual(out[-1], f'Erased {loader.DEFAULT_ERASE_LENGTH} bytes successfully')

    def test_other_device_erases_every_bank(self):
        self.assertEqual(self.erase('NRF52840_XXAA'), ['erased'])
        self.assertEqual(self.rpc.calls, [('flash_erase_all',)])
        self.assertEqual(self.loader.calls, [])

    def test_erase_with_empty_output_yields_nothing(self):
        self.rpc.flash_erase_all = lambda: ''
        self.assertEqual(self.erase('NRF52840_XXAA'), [])


class LoaderFailureTests(_DispatchCase):
    """A failed loader run must raise a message naming the loader failure --
    not a raw OpenOCD tcl traceback -- and say when the board may be blank."""

    def test_program_failure_after_erase_warns_board_may_be_blank(self):
        def failing_flash(rpc, image_path, *, family, flash_id=0, offset=0):
            yield 'Preparing DA1469x flash_loader from /stub/flash_loader.elf'
            yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes=4'
            raise loader.Da1469xLoaderError('fl_cmd_status=3 (program error)')

        with mock.patch.object(openocd_flash, 'flash_image', failing_flash):
            with self.assertRaises(loader.Da1469xLoaderError) as ctx:
                self.flash('DA14695', address=XIP)
        msg = str(ctx.exception)
        self.assertIn('DA1469x flash_loader flash failed', msg)
        self.assertIn('fl_cmd_status=3', msg)
        self.assertIn("after 'Erasing flash_id=0", msg, 'should name the last progress line')
        self.assertIn('blank', msg, "erase ran but program didn't -- say so")

    def test_early_failure_has_no_blank_warning(self):
        def failing_flash(rpc, image_path, *, family, flash_id=0, offset=0):
            raise loader.Da1469xLoaderError('loader artefacts not found')
            yield  # pragma: no cover -- makes this a generator

        with mock.patch.object(openocd_flash, 'flash_image', failing_flash):
            with self.assertRaises(loader.Da1469xLoaderError) as ctx:
                self.flash('DA14695', address=XIP)
        msg = str(ctx.exception)
        self.assertIn('loader artefacts not found', msg)
        self.assertNotIn('blank', msg, "nothing was erased; don't cry wolf")
        self.assertNotIn('after', msg.split(':')[0])

    def test_rpc_error_is_wrapped_with_loader_context(self):
        def failing_flash(rpc, image_path, *, family, flash_id=0, offset=0):
            yield 'Preparing DA1469x flash_loader from /stub/flash_loader.elf'
            raise openocd.OpenOcdRpcError('tcl connection reset')

        with mock.patch.object(openocd_flash, 'flash_image', failing_flash):
            with self.assertRaises(loader.Da1469xLoaderError) as ctx:
                self.flash('DA14695', address=XIP)
        self.assertIn('tcl connection reset', str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, openocd.OpenOcdRpcError)

    def test_erase_failure_names_the_erase(self):
        def failing_erase(rpc, *, family, flash_id=0, offset=0, length):
            yield f'Erasing flash_id={flash_id} offset={hex(offset)} bytes={length}'
            raise loader.Da1469xLoaderError('fl_cmd_status=2 (erase error)')

        with mock.patch.object(openocd_flash, 'erase_range', failing_erase):
            with self.assertRaises(loader.Da1469xLoaderError) as ctx:
                self.erase('DA14695')
        msg = str(ctx.exception)
        self.assertIn('DA1469x flash_loader erase failed', msg)
        self.assertIn('fl_cmd_status=2', msg)
        self.assertNotIn('blank', msg, 'an erase is not a flash that lost its image')

    def test_generic_path_failures_pass_through_untouched(self):
        # Not the loader's error to rewrite: the service and the Net API
        # already know how to present an OpenOcdRpcError.
        def failing_program(*a, **k):
            raise openocd.OpenOcdRpcError('** Programming Failed **')

        self.rpc.program = failing_program
        with self.assertRaises(openocd.OpenOcdRpcError) as ctx:
            self.flash('NRF52840_XXAA', address=0)
        self.assertEqual(str(ctx.exception), '** Programming Failed **')

    def test_flash_errors_is_the_pair_callers_catch(self):
        self.assertEqual(
            set(openocd_flash.FLASH_ERRORS),
            {loader.Da1469xLoaderError, openocd.OpenOcdRpcError},
        )


class RpcFlashGuardTests(unittest.TestCase):
    """``OpenOcdRpc`` built for a DA1469x refuses the generic flash commands
    before sending anything, naming the part and the missing driver. This
    is the safety net under the dispatch: a caller that bypasses
    ``openocd_flash`` gets a sentence instead of a ten-second stall and a
    startup.tcl traceback."""

    def _rpc(self, device):
        rpc = openocd.OpenOcdRpc(port=6666, device=device)
        rpc.cmd = mock.Mock(side_effect=AssertionError('must not reach the daemon'))
        return rpc

    def test_program_is_refused_by_name(self):
        rpc = self._rpc('DA14695')
        with self.assertRaises(openocd.OpenOcdNoFlashDriverError) as ctx:
            rpc.program('/tmp/xl.img.bin', address=XIP)
        msg = str(ctx.exception)
        self.assertIn('DA14695', msg)
        self.assertIn('DA1469x', msg)
        self.assertIn('0x16000000', msg)
        self.assertIn('no flash driver for that QSPI', msg)
        self.assertIn("'program'", msg)
        self.assertIn('openocd_flash', msg, 'must point at the path that works')
        rpc.cmd.assert_not_called()

    def test_erase_all_is_refused(self):
        rpc = self._rpc('da14699@B')
        with self.assertRaises(openocd.OpenOcdNoFlashDriverError) as ctx:
            rpc.flash_erase_all()
        self.assertIn("'flash erase_sector'", str(ctx.exception))
        rpc.cmd.assert_not_called()

    def test_erase_range_is_refused(self):
        rpc = self._rpc('DA14695')
        with self.assertRaises(openocd.OpenOcdNoFlashDriverError) as ctx:
            rpc.flash_erase_range(XIP, 0x1000)
        self.assertIn("'flash erase_address'", str(ctx.exception))
        rpc.cmd.assert_not_called()

    def test_refusal_is_an_openocd_rpc_error(self):
        # Every existing ``except OpenOcdRpcError`` (the service's 500 path,
        # the Net API's self-heal) keeps handling it.
        self.assertTrue(issubclass(openocd.OpenOcdNoFlashDriverError, openocd.OpenOcdRpcError))

    def test_other_devices_still_send_the_command(self):
        rpc = openocd.OpenOcdRpc(port=6666, device='NRF52840_XXAA')
        rpc.cmd = mock.Mock(return_value='** Programming Finished **\n')
        rpc.program('/tmp/app.hex')
        rpc.cmd.assert_called_once()
        self.assertTrue(rpc.cmd.call_args[0][0].startswith('program "/tmp/app.hex"'))

    def test_device_less_rpc_is_unchanged(self):
        rpc = openocd.OpenOcdRpc(port=6666)
        self.assertIsNone(rpc.device)
        rpc.cmd = mock.Mock(return_value='** Programming Finished **\n')
        rpc.program('/tmp/app.hex')
        rpc.cmd.assert_called_once()

    def test_dispatch_never_trips_its_own_guard(self):
        # The dispatch decides by device before touching the RPC, so an RPC
        # that knows its device is a DA1469x is never asked to ``program``.
        rpc = self._rpc('DA14695')
        seen = []

        def fake_flash_image(r, image_path, *, family, flash_id=0, offset=0):
            seen.append(r)
            yield 'ok'

        with mock.patch.object(openocd_flash, 'flash_image', fake_flash_image):
            list(openocd_flash.flash_target(rpc, rpc.device, '/tmp/xl.img.bin', address=XIP))
        self.assertEqual(seen, [rpc])
        rpc.cmd.assert_not_called()


class FamilyPredicateTests(unittest.TestCase):
    def test_members(self):
        for device in ('DA14695', 'da14691', 'DA14697@A', 'DA14699@3', 'Da1469x'):
            with self.subTest(device=device):
                self.assertTrue(probes.is_da1469x(device))

    def test_non_members(self):
        for device in (None, '', 'DA14531', 'DA14683', 'NRF52840_XXAA', 'STM32F446RE', 0):
            with self.subTest(device=device):
                self.assertFalse(probes.is_da1469x(device))

    def test_dispatch_and_rpc_use_the_same_predicate(self):
        self.assertIs(openocd_flash.is_da1469x, probes.is_da1469x)
        self.assertIs(openocd.is_da1469x, probes.is_da1469x)


class TimeoutTests(unittest.TestCase):
    def test_flash_budget_covers_a_full_loader_run(self):
        # ~75 s for a 700 KiB image on the bench; keep headroom for slow probes.
        self.assertGreaterEqual(openocd_flash.FLASH_RPC_TIMEOUT_S, 300)
        self.assertGreaterEqual(openocd_flash.ERASE_RPC_TIMEOUT_S, 120)


if __name__ == '__main__':
    unittest.main()

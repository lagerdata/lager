# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/debug/openocd.py — interface .cfg dispatch and
the user-cfg-suppresses-auto behavior in _build_openocd_command.

Loads openocd.py through a stub package so the real lager.debug
hardware-driver imports don't get pulled in.
"""

import importlib.util
import os
import sys
import types
import unittest


HERE = os.path.dirname(__file__)
DEBUG_DIR = os.path.normpath(
    os.path.join(HERE, '..', '..', '..', 'box', 'lager', 'debug')
)
PROBES_PATH = os.path.join(DEBUG_DIR, 'probes.py')
OPENOCD_PATH = os.path.join(DEBUG_DIR, 'openocd.py')


def _load_module(name, path, package=None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if package:
        module.__package__ = package
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Build a minimal stub package so openocd.py's `from .probes import ...` works.
_pkg = types.ModuleType('stub_debug_pkg')
_pkg.__path__ = [DEBUG_DIR]
sys.modules['stub_debug_pkg'] = _pkg
_load_module('stub_debug_pkg.probes', PROBES_PATH, package='stub_debug_pkg')
openocd = _load_module('stub_debug_pkg.openocd', OPENOCD_PATH, package='stub_debug_pkg')


class InterfaceConfigDispatchTests(unittest.TestCase):
    def _addr(self, vid, pid):
        return f'USB0::0x{vid}::0x{pid}::SERIAL123::INSTR'

    # ---- FTDI ----------------------------------------------------------------

    def test_ft232h_picks_c232hm_cfg(self):
        cfg = openocd.interface_config_for_address(self._addr('0403', '6014'))
        self.assertEqual(cfg, 'interface/ftdi/c232hm.cfg')

    def test_ft2232h_picks_olimex_arm_usb_ocd_h(self):
        cfg = openocd.interface_config_for_address(self._addr('0403', '6010'))
        self.assertEqual(cfg, 'interface/ftdi/olimex-arm-usb-ocd-h.cfg')

    def test_ft4232h_returns_none(self):
        # Too many wiring variants for a safe default — caller must supply
        # an openocd_config.
        cfg = openocd.interface_config_for_address(self._addr('0403', '6011'))
        self.assertIsNone(cfg)

    def test_unknown_ftdi_pid_returns_none(self):
        cfg = openocd.interface_config_for_address(self._addr('0403', '6015'))
        self.assertIsNone(cfg)

    def test_olimex_vid_unchanged(self):
        # Olimex publishes its own VID for the ARM-USB-OCD-H even though
        # the underlying chip is an FT2232H.
        cfg = openocd.interface_config_for_address(self._addr('15ba', '002a'))
        self.assertEqual(cfg, 'interface/ftdi/olimex-arm-usb-ocd-h.cfg')

    # ---- Non-FTDI families (no PID dispatch, but PID change must not break) -

    def test_stlink_v2(self):
        cfg = openocd.interface_config_for_address(self._addr('0483', '3748'))
        self.assertEqual(cfg, 'interface/stlink.cfg')

    def test_stlink_v3(self):
        cfg = openocd.interface_config_for_address(self._addr('0483', '374e'))
        self.assertEqual(cfg, 'interface/stlink.cfg')

    def test_rp2040_picoprobe(self):
        cfg = openocd.interface_config_for_address(self._addr('2e8a', '000c'))
        self.assertEqual(cfg, 'interface/cmsis-dap.cfg')

    def test_atmel_edbg(self):
        cfg = openocd.interface_config_for_address(self._addr('03eb', '2111'))
        self.assertEqual(cfg, 'interface/cmsis-dap.cfg')

    def test_daplink(self):
        cfg = openocd.interface_config_for_address(self._addr('0d28', '0204'))
        self.assertEqual(cfg, 'interface/cmsis-dap.cfg')

    def test_unknown_vid_returns_none(self):
        cfg = openocd.interface_config_for_address(self._addr('1209', 'beef'))
        self.assertIsNone(cfg)

    def test_unparseable_address_returns_none(self):
        self.assertIsNone(openocd.interface_config_for_address(''))
        self.assertIsNone(openocd.interface_config_for_address(None))
        self.assertIsNone(openocd.interface_config_for_address('not-a-visa-address'))


class BuildOpenOcdCommandTests(unittest.TestCase):
    """Verify _build_openocd_command honours user_config_path precedence.

    We invoke the private helper directly so we don't need a running OpenOCD
    or a real probe — argv assembly is pure.
    """

    BASE_KWARGS = dict(
        openocd_exe='/usr/bin/openocd',
        scripts_dir='/usr/share/openocd/scripts',
        device='ESP32',
        transport='JTAG',
        speed='2000',
        halt=False,
        gdb_port=2331,
        telnet_port=4444,
        tcl_port=6666,
        rtt_telnet_port=9090,
        log_file='/tmp/openocd.log',
        probe_channel=None,
    )

    def _build(self, **overrides):
        kwargs = dict(self.BASE_KWARGS)
        kwargs.update(overrides)
        return openocd._build_openocd_command(**kwargs)

    def test_auto_interface_used_when_no_user_cfg(self):
        # FT232H probe, no user cfg → c232hm.cfg should appear.
        cmd = self._build(
            address='USB0::0x0403::0x6014::FTA3W13P::INSTR',
            user_config_path=None,
        )
        self.assertIn('interface/ftdi/c232hm.cfg', cmd)

    def test_user_cfg_suppresses_auto_interface(self):
        # When user supplies a cfg, the auto-detected one must NOT appear.
        # The auto-dispatch would normally pick c232hm.cfg for this address.
        user_cfg = os.path.abspath(__file__)  # any existing file works
        cmd = self._build(
            address='USB0::0x0403::0x6014::FTA3W13P::INSTR',
            user_config_path=user_cfg,
        )
        self.assertNotIn('interface/ftdi/c232hm.cfg', cmd)
        self.assertIn(user_cfg, cmd)

    def test_raises_when_no_interface_and_no_user_cfg(self):
        # Unknown VID with no user cfg → must error actionably, not start.
        with self.assertRaises(FileNotFoundError) as ctx:
            self._build(
                address='USB0::0x1209::0xbeef::SOMESN::INSTR',
                user_config_path=None,
            )
        msg = str(ctx.exception)
        self.assertIn('openocd_config', msg)

    def test_unknown_vid_with_user_cfg_succeeds(self):
        # Unknown VID is fine as long as the user supplies their own cfg —
        # this is the escape hatch for Black Magic Probe, Glasgow, FT4232H, …
        user_cfg = os.path.abspath(__file__)
        cmd = self._build(
            address='USB0::0x1209::0xbeef::SOMESN::INSTR',
            user_config_path=user_cfg,
        )
        self.assertIn(user_cfg, cmd)

    def test_user_cfg_loads_before_adapter_serial(self):
        # ``adapter serial`` requires ``adapter driver <X>`` to already be
        # set. With a user cfg, the driver only appears inside that cfg, so
        # the ``-f user.cfg`` must precede ``-c adapter serial ...``.
        user_cfg = os.path.abspath(__file__)
        cmd = self._build(
            address='USB0::0x0403::0x6011::FT4XYZW::INSTR',  # FT4232H
            device='custom-chip',  # no target.cfg auto-match
            user_config_path=user_cfg,
        )
        user_cfg_idx = cmd.index(user_cfg)
        serial_idx = next(
            i for i, arg in enumerate(cmd) if arg.startswith('adapter serial')
        )
        self.assertLess(user_cfg_idx, serial_idx,
                        msg='user cfg must load before `adapter serial`')

    def test_user_cfg_suppresses_auto_transport_select(self):
        # User cfgs almost always call ``transport select`` themselves at
        # the top; a second select trips "Transport already selected".
        user_cfg = os.path.abspath(__file__)
        cmd = self._build(
            address='USB0::0x0403::0x6011::FT4XYZW::INSTR',
            device='custom-chip',
            transport='SWD',
            user_config_path=user_cfg,
        )
        self.assertFalse(
            any(arg.startswith('transport select') for arg in cmd),
            msg='auto `transport select` must be skipped when user cfg is set',
        )

    def test_auto_transport_select_still_emitted_without_user_cfg(self):
        # Regression: the user-cfg gating must not strip transport select
        # from the normal auto-detected path.
        cmd = self._build(
            address='USB0::0x0403::0x6014::FTA3W13P::INSTR',  # FT232H
            transport='SWD',
            user_config_path=None,
        )
        self.assertTrue(
            any(arg == 'transport select swd' for arg in cmd),
            msg='auto `transport select` should still appear in non-user-cfg path',
        )


class OpenOcdRpcProgramTests(unittest.TestCase):
    """``OpenOcdRpc.program`` must surface OpenOCD ``program_error`` markers.

    The TCL/RPC channel returns the ``program`` proc's stdout as plain text
    even when the underlying flash write/verify failed — there's no
    out-of-band success/failure flag. ``program_error`` echoes
    ``** <Something> Failed **`` (e.g. ``** Programming Failed **``,
    ``** Verify Failed **``); the wrapper must spot those and raise so
    callers don't tell the user "Flashed!" after a bad write.
    """

    def _rpc_with_canned_output(self, output):
        rpc = openocd.OpenOcdRpc(host='127.0.0.1', port=6666)
        rpc.cmd = lambda command, timeout=None: output  # noqa: ARG005
        return rpc

    def test_success_returns_output(self):
        success_out = (
            '** Programming Started **\n'
            'wrote 65536 bytes from file foo.bin in 1.23s\n'
            '** Programming Finished **\n'
            '** Verify Started **\n'
            '** Verified OK **\n'
            '** Resetting Target **\n'
        )
        rpc = self._rpc_with_canned_output(success_out)
        self.assertEqual(rpc.program('foo.bin'), success_out)

    def test_programming_failed_marker_raises(self):
        failure_out = (
            '** Programming Started **\n'
            'embedded:startup.tcl:1516: Error: ** Programming Failed **\n'
            "in procedure 'program'\n"
        )
        rpc = self._rpc_with_canned_output(failure_out)
        with self.assertRaises(openocd.OpenOcdRpcError) as ctx:
            rpc.program('xl.bin', address=0x16000000)
        # Caller-friendly message: includes the file we tried to flash and
        # the OpenOCD output so the operator can see the real reason.
        msg = str(ctx.exception)
        self.assertIn('xl.bin', msg)
        self.assertIn('** Programming Failed **', msg)

    def test_verify_failed_marker_raises(self):
        # ``program ... verify`` can succeed at write but fail verify; the
        # ``program_error`` marker is ``** Verify Failed **`` in that case.
        failure_out = (
            '** Programming Started **\n'
            '** Programming Finished **\n'
            '** Verify Started **\n'
            'Error: ** Verify Failed **\n'
        )
        rpc = self._rpc_with_canned_output(failure_out)
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.program('xl.bin')

    def test_benign_text_with_failed_word_does_not_raise(self):
        # The marker pattern requires the ``** ... **`` framing — a stray
        # ``Failed`` in unrelated log text must not be treated as a fault.
        benign_out = (
            '** Programming Started **\n'
            'note: Failed attempts logged separately\n'
            '** Programming Finished **\n'
        )
        rpc = self._rpc_with_canned_output(benign_out)
        self.assertEqual(rpc.program('xl.bin'), benign_out)


if __name__ == '__main__':
    unittest.main()

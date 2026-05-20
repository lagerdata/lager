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

    def test_user_cfg_suppresses_auto_adapter_speed(self):
        # When a user cfg is attached they manage adapter setup themselves
        # and almost always set ``adapter speed`` inside the cfg. Lager's
        # OpenOCD argv sources the user cfg early, but the speed override
        # was previously appended *last* — so an ``adapter speed 500`` in
        # the cfg would be silently clobbered to ``4000`` (the CLI default
        # for ``lager debug SWD flash``), which on the DA1469x flash_loader
        # path manifests as the chunked program loop hanging on the first
        # write while pings/erase happen to survive. Mirror the same gating
        # used for ``transport select``.
        user_cfg = os.path.abspath(__file__)
        cmd = self._build(
            address='USB0::0x0403::0x6011::FT4XYZW::INSTR',  # FT4232H
            device='custom-chip',
            speed='4000',
            user_config_path=user_cfg,
        )
        self.assertFalse(
            any(arg.startswith('adapter speed') for arg in cmd),
            msg=f'auto `adapter speed` must be skipped when user cfg is set; '
                f'got: {[a for a in cmd if a.startswith("adapter speed")]}',
        )

    def test_auto_adapter_speed_still_emitted_without_user_cfg(self):
        # Regression: the user-cfg gating must not strip ``adapter speed``
        # from the normal auto-detected path — that's where lager *is* the
        # source of truth for adapter setup.
        cmd = self._build(
            address='USB0::0x0403::0x6014::FTA3W13P::INSTR',  # FT232H
            speed='4000',
            user_config_path=None,
        )
        self.assertIn('adapter speed 4000', cmd)

    def test_adaptive_speed_never_emits_adapter_speed(self):
        # Sanity: the legacy ``adaptive`` value is intentionally a no-op
        # under both the auto and user-cfg branches.
        for user_cfg in (None, os.path.abspath(__file__)):
            cmd = self._build(
                address='USB0::0x0403::0x6014::FTA3W13P::INSTR',
                speed='adaptive',
                user_config_path=user_cfg,
            )
            self.assertFalse(
                any(arg.startswith('adapter speed') for arg in cmd),
                msg=f'`adapter speed` must not be emitted for adaptive speed '
                    f'(user_cfg={user_cfg!r}); got: '
                    f'{[a for a in cmd if a.startswith("adapter speed")]}',
            )

    def test_bindto_all_interfaces_is_set(self):
        # OpenOCD ≥ 0.11 defaults bindto to 127.0.0.1 — that combined with
        # docker's port forward leaves off-box gdb clients unable to reach
        # the gdb_port even though it shows as listening to ``ss`` inside
        # the container. We must explicitly widen the bind via ``bindto
        # 0.0.0.0`` so the J-Link parity (``JLinkGDBServer`` binds all
        # interfaces by default) holds for OpenOCD too. Sits adjacent to
        # the port-setup ``-c`` block so it always applies before any
        # init that would otherwise lock the bind to the default.
        for address, user_cfg in (
            ('USB0::0x0403::0x6014::FTA3W13P::INSTR', None),       # auto
            ('USB0::0x0403::0x6011::FT4XYZW::INSTR', __file__),    # user cfg
        ):
            cmd = self._build(address=address, user_config_path=user_cfg)
            bindto_idx = next(
                (i for i, arg in enumerate(cmd) if arg == 'bindto 0.0.0.0'),
                None,
            )
            self.assertIsNotNone(
                bindto_idx,
                msg=f'expected `bindto 0.0.0.0` in argv for address={address}, '
                    f'user_cfg={user_cfg!r}; got: {cmd}',
            )
            # Must be wired through ``-c`` so OpenOCD evaluates it as TCL,
            # not treated as a positional config file path.
            self.assertEqual(cmd[bindto_idx - 1], '-c')


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

    def test_program_raises_on_tcl_error_line(self):
        # Some failures (e.g. ``no flash bank found``) come out as plain
        # ``Error:`` lines without the ``** ... Failed **`` framing — those
        # used to leak through and print ``Flashed!`` despite a fault.
        failure_out = (
            '** Programming Started **\n'
            "Error: no flash bank found for address 0x16000000\n"
        )
        rpc = self._rpc_with_canned_output(failure_out)
        with self.assertRaises(openocd.OpenOcdRpcError) as ctx:
            rpc.program('xl.bin', address=0x16000000)
        self.assertIn('no flash bank found', str(ctx.exception))


class OpenOcdRpcHelperTests(unittest.TestCase):
    """Cover the small TCL-RPC helpers used by ``da1469x_loader``.

    Each helper emits one OpenOCD command and parses (or doesn't) its
    response. The DI seam is ``OpenOcdRpc.cmd``, monkey-patched to a stub
    that records the issued command and returns canned output.
    """

    def _rpc(self, responder):
        """``responder(cmd_str) -> str`` — one canned response per cmd."""
        rpc = openocd.OpenOcdRpc(host='127.0.0.1', port=6666)
        captured = []

        def fake_cmd(command, timeout=None):  # noqa: ARG001
            captured.append(command)
            if callable(responder):
                return responder(command)
            return responder
        rpc.cmd = fake_cmd
        return rpc, captured

    # ---- mww / mwb -----------------------------------------------------------

    def test_mww_emits_hex_addr_and_value(self):
        rpc, sent = self._rpc('')
        rpc.mww(0x20000000, 0xCAFEF00D)
        self.assertEqual(sent, ['mww 0x20000000 0xcafef00d'])

    def test_mww_raises_on_tcl_error(self):
        rpc, _ = self._rpc('Error: target not halted\n')
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.mww(0x20000000, 0)

    def test_mwb_emits_byte_value_only(self):
        rpc, sent = self._rpc('')
        rpc.mwb(0x20000004, 0x123)  # masked to 0x23
        self.assertEqual(sent, ['mwb 0x20000004 0x23'])

    # ---- mdw -----------------------------------------------------------------

    def test_mdw_returns_int_for_single_word(self):
        rpc, sent = self._rpc('0x20000000: cafef00d\n')
        self.assertEqual(rpc.mdw(0x20000000), 0xCAFEF00D)
        self.assertEqual(sent, ['mdw 0x20000000 1'])

    def test_mdw_returns_list_for_multiple_words(self):
        rpc, _ = self._rpc('0x20000000: 11111111 22222222 33333333 44444444\n')
        self.assertEqual(
            rpc.mdw(0x20000000, count=4),
            [0x11111111, 0x22222222, 0x33333333, 0x44444444],
        )

    def test_mdw_raises_on_unparseable_output(self):
        rpc, _ = self._rpc('something unexpected\n')
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.mdw(0x20000000)

    def test_mdw_raises_on_tcl_error(self):
        rpc, _ = self._rpc("Error: address 0xdead can't be read\n")
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.mdw(0xDEAD)

    # ---- load_image ----------------------------------------------------------

    def test_load_image_uses_bin_format_by_default(self):
        rpc, sent = self._rpc(
            '8192 bytes written at address 0x20000000\n'
            'downloaded 8192 bytes in 0.001s (8000.00 KiB/s)\n'
        )
        rpc.load_image('/tmp/loader.bin', 0x20000000)
        self.assertEqual(sent, ['load_image /tmp/loader.bin 0x20000000 bin'])

    def test_load_image_raises_on_error(self):
        rpc, _ = self._rpc("Error: can't open file 'missing'\n")
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.load_image('missing', 0x20000000)

    # ---- registers -----------------------------------------------------------

    def test_reg_write(self):
        rpc, sent = self._rpc('')
        rpc.reg_write('pc', 0x20000401)
        self.assertEqual(sent, ['reg pc 0x20000401'])

    def test_reg_read_parses_value(self):
        rpc, _ = self._rpc('pc (/32): 0x20000401\n')
        self.assertEqual(rpc.reg_read('pc'), 0x20000401)

    def test_reg_read_raises_when_no_value(self):
        rpc, _ = self._rpc('register pc not present\n')
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.reg_read('pc')

    # ---- breakpoints / control flow -----------------------------------------

    def test_bp_sets_hardware_breakpoint(self):
        rpc, sent = self._rpc('')
        rpc.bp(0x20001234, length=4, hw=True)
        self.assertEqual(sent, ['bp 0x20001234 4 hw'])

    def test_rbp_removes_breakpoint(self):
        rpc, sent = self._rpc('')
        rpc.rbp(0x20001234)
        self.assertEqual(sent, ['rbp 0x20001234'])

    def test_bp_list_empty(self):
        # Newer OpenOCDs print nothing when no breakpoints are set; older
        # ones might print a blank line. Both must yield an empty list.
        rpc, _ = self._rpc('')
        self.assertEqual(rpc.bp_list(), [])
        rpc, _ = self._rpc('\n')
        self.assertEqual(rpc.bp_list(), [])

    def test_bp_list_parses_iva_format(self):
        out = (
            'Breakpoint(IVA): 0x20001234, 0x4, hard\n'
            'Breakpoint(IVA): 0x20005678, 0x2, hard\n'
        )
        rpc, _ = self._rpc(out)
        self.assertEqual(rpc.bp_list(), [0x20001234, 0x20005678])

    def test_bp_list_parses_legacy_format(self):
        # Older OpenOCDs drop the ``(IVA)`` qualifier; the address is still
        # the first hex token after ``Breakpoint``.
        out = 'Breakpoint: 0x08000400, 0x2, hard\n'
        rpc, _ = self._rpc(out)
        self.assertEqual(rpc.bp_list(), [0x08000400])

    def test_bp_list_ignores_unrelated_lines(self):
        out = (
            'Some preamble line with 0xdeadbeef in it\n'
            'Breakpoint(IVA): 0x20001234, 0x4, hard\n'
        )
        rpc, _ = self._rpc(out)
        self.assertEqual(rpc.bp_list(), [0x20001234])

    def test_bp_list_raises_on_tcl_error(self):
        rpc, _ = self._rpc('Error: target not examined yet\n')
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.bp_list()

    def test_wait_halt_passes_timeout(self):
        rpc, sent = self._rpc('')
        rpc.wait_halt(timeout_ms=2500)
        self.assertEqual(sent, ['wait_halt 2500'])

    def test_wait_halt_raises_on_timeout_error(self):
        rpc, _ = self._rpc('Error: timed out while waiting for target halted\n')
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.wait_halt(timeout_ms=10)

    def test_sleep_ms(self):
        rpc, sent = self._rpc('')
        rpc.sleep_ms(50)
        self.assertEqual(sent, ['sleep 50'])

    def test_resume_with_address(self):
        rpc, sent = self._rpc('')
        rpc.resume(0x20000400)
        self.assertEqual(sent, ['resume 0x20000400'])

    def test_resume_without_address(self):
        rpc, sent = self._rpc('')
        rpc.resume()
        self.assertEqual(sent, ['resume'])


class OpenOcdRpcEraseAllTests(unittest.TestCase):
    """``flash_erase_all`` must propagate TCL ``Error:`` lines instead of
    silently returning success — the erase-side analog of the program
    failure scrub. Without this, ``lager debug SWD erase`` printed
    ``Erase complete!`` on rigs whose target.cfg had no flash bank at all.
    """

    def _rpc_scripted(self, responses):
        """Each call to ``cmd`` returns the next entry from *responses*
        (or the last entry once exhausted)."""
        rpc = openocd.OpenOcdRpc(host='127.0.0.1', port=6666)
        idx = {'i': 0}
        captured = []

        def fake_cmd(command, timeout=None):  # noqa: ARG001
            captured.append(command)
            i = min(idx['i'], len(responses) - 1)
            idx['i'] += 1
            return responses[i]
        rpc.cmd = fake_cmd
        return rpc, captured

    def test_no_banks_falls_back_and_raises_on_tcl_error(self):
        # ``flash banks`` returns nothing (no bank declared); fallback to
        # bank 0 then erase_address. Both raise ``Error:`` -> we surface.
        rpc, sent = self._rpc_scripted([
            '\n',  # flash banks: empty
            'Error: no flash bank found for address 0x0\n',  # erase_sector 0 0 last
            'Error: no flash bank found for address 0x0\n',  # erase_address fallback
        ])
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.flash_erase_all()
        self.assertEqual(sent[0], 'flash banks')
        self.assertIn('flash erase_sector 0 0 last', sent[1])
        self.assertIn('flash erase_address 0 0xFFFFFFFF', sent[2])

    def test_single_bank_erase_returns_output(self):
        rpc, sent = self._rpc_scripted([
            '#0 : stm32h7x.bank1 (stm32h7x) at 0x08000000, size 0x00100000, ...\n',
            'erased sectors 0 through 15 on flash bank 0 in 0.5s\n',
        ])
        out = rpc.flash_erase_all()
        self.assertIn('erased sectors', out)
        self.assertEqual(sent[0], 'flash banks')
        self.assertIn('flash erase_sector 0 0 last', sent[1])

    def test_flash_erase_range_raises_on_error(self):
        rpc = openocd.OpenOcdRpc(host='127.0.0.1', port=6666)
        rpc.cmd = lambda c, timeout=None: 'Error: not enough flash banks\n'  # noqa: ARG005
        with self.assertRaises(openocd.OpenOcdRpcError):
            rpc.flash_erase_range(0, 0x10000)


if __name__ == '__main__':
    unittest.main()

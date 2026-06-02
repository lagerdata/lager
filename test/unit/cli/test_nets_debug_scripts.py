#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the smart ``lager nets set-script`` family.

Covers:
  * The pure helpers (probe backend resolver, file sniffer, signal
    reconciler, exclusivity clearer) in isolation.
  * The Click commands end-to-end with ``_run_net_py`` mocked so the box
    round-trip is replaced by an in-memory net DB.

These tests pin the contract that:
  * Auto-detection routes ``.cfg``/``.tcl`` to ``openocd_config`` on
    OpenOCD probes and ``.JLinkScript`` to ``jlink_script`` on J-Link probes.
  * Probe-vs-file mismatches refuse without ``--force`` / ``--backend``.
  * ``set-script`` enforces mutual exclusivity (clears the other field).
  * Stdin (``SCRIPT_PATH='-'``) works.
  * ``--backend jlink|openocd`` is the explicit override and short-circuits
    the probe/file reconciliation so the caller's intent always wins.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import sys
import tempfile

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

nets_mod = importlib.import_module('cli.commands.box.nets')
from cli.commands.box.nets import nets as nets_group  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure-helper tests                                                           #
# --------------------------------------------------------------------------- #

class TestProbeBackend:
    """``_probe_backend_for_net`` mirrors box-side ``resolve_backend`` but
    returns ``None`` instead of defaulting to J-Link when the VID is
    unknown — the smart ``set-script`` flow relies on that distinction to
    fall back cleanly on file sniffing."""

    def test_jlink_vid(self):
        net = {'address': 'USB0::0x1366::0x0101::000051014439::INSTR'}
        assert nets_mod._probe_backend_for_net(net) == 'jlink'

    def test_ftdi_vid(self):
        net = {'address': 'USB0::0x0403::0x6011::FT4232ABCD::INSTR'}
        assert nets_mod._probe_backend_for_net(net) == 'openocd'

    def test_stlink_vid(self):
        net = {'address': 'USB0::0x0483::0x374B::066BFF::INSTR'}
        assert nets_mod._probe_backend_for_net(net) == 'openocd'

    def test_unknown_vid_returns_none(self):
        net = {'address': 'USB0::0xDEAD::0xBEEF::xxx::INSTR'}
        assert nets_mod._probe_backend_for_net(net) is None

    def test_explicit_debug_backend_wins(self):
        net = {
            'address': 'USB0::0x1366::0x0101::xxx::INSTR',  # would be J-Link
            'debug_backend': 'openocd',
        }
        assert nets_mod._probe_backend_for_net(net) == 'openocd'

    def test_no_address_returns_none(self):
        assert nets_mod._probe_backend_for_net({}) is None

    def test_none_input(self):
        assert nets_mod._probe_backend_for_net(None) is None


class TestSniffScriptBackend:
    """``_sniff_script_backend`` prefers the extension; content sniff is a
    tie-breaker for stdin and abstains when markers conflict."""

    def test_jlinkscript_extension(self):
        assert nets_mod._sniff_script_backend('foo.JLinkScript', b'') == 'jlink'

    def test_cfg_extension(self):
        assert nets_mod._sniff_script_backend('foo.cfg', b'') == 'openocd'

    def test_tcl_extension(self):
        assert nets_mod._sniff_script_backend('foo.tcl', b'') == 'openocd'

    def test_ocd_extension(self):
        assert nets_mod._sniff_script_backend('foo.ocd', b'') == 'openocd'

    def test_content_sniff_openocd(self):
        body = b'adapter driver ftdi\ntransport select swd\n'
        assert nets_mod._sniff_script_backend('<stdin>', body) == 'openocd'

    def test_content_sniff_jlink(self):
        body = b'void Reset() { MEM_WriteU32(0x40000000, 1); }\n'
        assert nets_mod._sniff_script_backend('<stdin>', body) == 'jlink'

    def test_ambiguous_content_abstains(self):
        assert nets_mod._sniff_script_backend('<stdin>', b'# just a comment') is None

    def test_extension_dominates_content(self):
        # A .cfg with J-Link-looking text in a comment must NOT be misclassified.
        body = b'# old reference: void Reset(){}\nadapter driver ftdi\n'
        assert nets_mod._sniff_script_backend('weird.cfg', body) == 'openocd'


class TestChooseScriptBackend:
    """Three-signal reconciler. The contract is: explicit wins, two known
    signals must agree, a single known signal is trusted, no signals -> None."""

    def test_explicit_overrides_everything(self):
        c, _r, m = nets_mod._choose_script_backend(
            explicit='jlink', probe='openocd', file='openocd',
        )
        assert c == 'jlink' and not m

    def test_probe_and_file_agree(self):
        c, _r, m = nets_mod._choose_script_backend(
            explicit=None, probe='openocd', file='openocd',
        )
        assert c == 'openocd' and not m

    def test_mismatch_returns_none_with_flag(self):
        c, r, m = nets_mod._choose_script_backend(
            explicit=None, probe='jlink', file='openocd',
        )
        assert c is None
        assert m is True
        assert 'jlink' in r and 'openocd' in r

    def test_only_file_known(self):
        c, _r, m = nets_mod._choose_script_backend(
            explicit=None, probe=None, file='openocd',
        )
        assert c == 'openocd' and not m

    def test_only_probe_known(self):
        c, _r, m = nets_mod._choose_script_backend(
            explicit=None, probe='openocd', file=None,
        )
        assert c == 'openocd' and not m

    def test_no_signals(self):
        c, _r, m = nets_mod._choose_script_backend(
            explicit=None, probe=None, file=None,
        )
        assert c is None and not m


class TestClearOtherScriptField:
    """``_clear_other_script_field`` enforces the "at most one debug script
    per net" invariant by dropping the other field when present."""

    def _b64(self, s: bytes) -> str:
        return base64.b64encode(s).decode('ascii')

    def test_clears_openocd_when_setting_jlink(self):
        target = {'jlink_script': self._b64(b'x'), 'openocd_config': self._b64(b'hello')}
        cleared, n = nets_mod._clear_other_script_field(target, 'jlink_script')
        assert cleared == 'openocd_config'
        assert n == 5
        assert 'openocd_config' not in target

    def test_clears_jlink_when_setting_openocd(self):
        target = {'jlink_script': self._b64(b'abcde'), 'openocd_config': self._b64(b'x')}
        cleared, n = nets_mod._clear_other_script_field(target, 'openocd_config')
        assert cleared == 'jlink_script'
        assert n == 5

    def test_noop_when_other_absent(self):
        target = {'jlink_script': self._b64(b'x')}
        cleared, n = nets_mod._clear_other_script_field(target, 'jlink_script')
        assert cleared is None and n == 0


# --------------------------------------------------------------------------- #
# Click end-to-end tests with ``_run_net_py`` mocked.                          #
# --------------------------------------------------------------------------- #

def _make_db():
    return [
        {
            'name': 'SWD',
            'role': 'debug',
            'instrument': 'FTDI_FT4232H',
            'address': 'USB0::0x0403::0x6011::FT4232ABCD::INSTR',
        },
        {
            'name': 'JLINK1',
            'role': 'debug',
            'instrument': 'J-Link',
            'address': 'USB0::0x1366::0x0101::000051014439::INSTR',
        },
    ]


@pytest.fixture
def fake_box(monkeypatch):
    """Replace ``_run_net_py`` + ``_resolve_box`` with an in-memory DB.

    Returns a ``state`` dict so each test can inspect ``state['saves']``
    (chronological list of save payloads) and tweak ``state['db']`` for
    "legacy record" setups.
    """
    state = {'db': _make_db(), 'saves': []}

    def fake_run_net_py(ctx, box, *args):
        if args[0] == 'list':
            return json.dumps(state['db'])
        if args[0] == 'save':
            rec = json.loads(args[1])
            state['saves'].append(rec)
            for i, r in enumerate(state['db']):
                if r.get('name') == rec.get('name'):
                    state['db'][i] = rec
                    break
            return ''
        raise AssertionError(f'unexpected args: {args!r}')

    def fake_resolve_box(ctx, box_opt=None):
        return 'TESTBOX'

    monkeypatch.setattr(nets_mod, '_run_net_py', fake_run_net_py)
    monkeypatch.setattr(nets_mod, '_resolve_box', fake_resolve_box)
    return state


@pytest.fixture
def cfg_file():
    """Temp ``.cfg`` file that looks like an OpenOCD config."""
    f = tempfile.NamedTemporaryFile('w', suffix='.cfg', delete=False)
    f.write('adapter driver ftdi\ntransport select swd\n')
    f.close()
    yield f.name
    os.unlink(f.name)


@pytest.fixture
def jlink_file():
    """Temp ``.JLinkScript`` file that looks like a J-Link script."""
    f = tempfile.NamedTemporaryFile('w', suffix='.JLinkScript', delete=False)
    f.write('void Reset() { MEM_WriteU32(0x40000000, 1); }\n')
    f.close()
    yield f.name
    os.unlink(f.name)


class TestSetScriptAutoDetect:
    def test_cfg_on_ftdi_net_routes_to_openocd_config(self, fake_box, cfg_file):
        runner = CliRunner()
        res = runner.invoke(nets_group, ['set-script', 'SWD', cfg_file, '--box', 'test-box'])
        assert res.exit_code == 0, res.output
        assert 'OpenOCD config' in res.output
        saved = fake_box['saves'][-1]
        assert 'openocd_config' in saved and 'jlink_script' not in saved
        assert base64.b64decode(saved['openocd_config']).startswith(b'adapter driver')

    def test_jlinkscript_on_jlink_net_routes_to_jlink_script(self, fake_box, jlink_file):
        runner = CliRunner()
        res = runner.invoke(
            nets_group, ['set-script', 'JLINK1', jlink_file, '--box', 'test-box'],
        )
        assert res.exit_code == 0, res.output
        saved = fake_box['saves'][-1]
        assert 'jlink_script' in saved and 'openocd_config' not in saved


class TestSetScriptMismatchHandling:
    def test_cfg_on_jlink_probe_refused_without_override(self, fake_box, cfg_file):
        runner = CliRunner()
        res = runner.invoke(
            nets_group, ['set-script', 'JLINK1', cfg_file, '--box', 'test-box'],
        )
        assert res.exit_code != 0
        assert "probe says 'jlink'" in res.output
        assert "file says 'openocd'" in res.output
        # Nothing saved.
        assert not fake_box['saves']

    def test_backend_flag_overrides_mismatch(self, fake_box, cfg_file):
        runner = CliRunner()
        res = runner.invoke(nets_group, [
            'set-script', 'JLINK1', cfg_file,
            '--backend', 'openocd', '--box', 'test-box',
        ])
        assert res.exit_code == 0, res.output
        assert 'OpenOCD config' in res.output

    def test_mismatch_error_message_points_at_backend_flag(self, fake_box, cfg_file):
        # The error must actually tell the user how to recover; --backend is
        # the only supported override (no --force on purpose).
        runner = CliRunner()
        res = runner.invoke(nets_group, [
            'set-script', 'JLINK1', cfg_file, '--box', 'test-box',
        ])
        assert res.exit_code != 0
        assert '--backend' in res.output


class TestSetScriptStdin:
    def test_dash_reads_from_stdin(self, fake_box):
        runner = CliRunner()
        res = runner.invoke(
            nets_group, ['set-script', 'SWD', '-', '--box', 'test-box'],
            input='adapter driver ftdi\n',
        )
        assert res.exit_code == 0, res.output
        saved = fake_box['saves'][-1]
        assert base64.b64decode(saved['openocd_config']) == b'adapter driver ftdi\n'

    def test_empty_stdin_still_writes_when_probe_known(self, fake_box):
        # No file extension, no content to sniff -> file_be is None. Probe says
        # openocd (FTDI), so the chooser picks openocd from the probe alone.
        runner = CliRunner()
        res = runner.invoke(
            nets_group, ['set-script', 'SWD', '-', '--box', 'test-box'], input='',
        )
        assert res.exit_code == 0, res.output
        assert 'matched probe' in res.output


class TestSetScriptInputErrors:
    def test_nonexistent_path(self, fake_box):
        runner = CliRunner()
        res = runner.invoke(
            nets_group,
            ['set-script', 'SWD', '/no/such/file.cfg', '--box', 'test-box'],
        )
        assert res.exit_code != 0
        assert 'not found' in res.output.lower()

    def test_no_backend_signal_errors_clearly(self, fake_box):
        # Force an unknown-VID record so the probe signal is None too.
        fake_box['db'][0]['address'] = 'USB0::0xDEAD::0xBEEF::xxx::INSTR'
        runner = CliRunner()
        res = runner.invoke(
            nets_group, ['set-script', 'SWD', '-', '--box', 'test-box'],
            input='# nothing here\n',
        )
        assert res.exit_code != 0
        assert '--backend' in res.output
        assert 'no backend signal' in res.output.lower()


class TestSetScriptExclusivity:
    def test_setting_openocd_clears_existing_jlink_script(self, fake_box, cfg_file):
        # Pre-populate jlink_script (a legacy / broken record).
        fake_box['db'][0]['jlink_script'] = base64.b64encode(b'old jlink').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, ['set-script', 'SWD', cfg_file, '--box', 'test-box'])
        assert res.exit_code == 0, res.output
        assert 'Cleared existing jlink_script' in res.output
        saved = fake_box['saves'][-1]
        assert 'jlink_script' not in saved
        assert 'openocd_config' in saved


class TestShowScript:
    def test_shows_whichever_field_is_set(self, fake_box):
        fake_box['db'][0]['openocd_config'] = base64.b64encode(b'adapter driver ftdi\n').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, ['show-script', 'SWD', '--box', 'test-box'])
        assert res.exit_code == 0, res.output
        assert 'adapter driver ftdi' in res.stdout

    def test_errors_when_both_fields_set_without_backend(self, fake_box):
        fake_box['db'][0]['jlink_script'] = base64.b64encode(b'a').decode('ascii')
        fake_box['db'][0]['openocd_config'] = base64.b64encode(b'b').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, ['show-script', 'SWD', '--box', 'test-box'])
        assert res.exit_code != 0
        assert 'both' in res.output.lower()
        assert '--backend' in res.output

    def test_backend_filter_errors_when_filtered_field_missing(self, fake_box):
        # Only openocd_config set, but --backend jlink should refuse cleanly.
        fake_box['db'][0]['openocd_config'] = base64.b64encode(b'x').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, [
            'show-script', 'SWD', '--backend', 'jlink', '--box', 'test-box',
        ])
        assert res.exit_code != 0
        assert 'J-Link script' in res.output


class TestRemoveScript:
    def test_removes_openocd_config_when_only_one_present(self, fake_box):
        fake_box['db'][0]['openocd_config'] = base64.b64encode(b'x').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, ['remove-script', 'SWD', '--box', 'test-box'])
        assert res.exit_code == 0, res.output
        assert 'Removed openocd_config' in res.output
        saved = fake_box['saves'][-1]
        assert 'openocd_config' not in saved

    def test_removes_both_legacy_fields(self, fake_box):
        fake_box['db'][0]['jlink_script'] = base64.b64encode(b'a').decode('ascii')
        fake_box['db'][0]['openocd_config'] = base64.b64encode(b'b').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, ['remove-script', 'SWD', '--box', 'test-box'])
        assert res.exit_code == 0, res.output
        saved = fake_box['saves'][-1]
        assert 'jlink_script' not in saved
        assert 'openocd_config' not in saved

    def test_noop_when_nothing_attached(self, fake_box):
        runner = CliRunner()
        res = runner.invoke(nets_group, ['remove-script', 'SWD', '--box', 'test-box'])
        assert res.exit_code == 0
        assert 'does not have' in res.output.lower()
        assert not fake_box['saves']


class TestExplicitBackendFlag:
    """When the user passes --backend openocd explicitly, the impl must
    skip the probe<->file mismatch check (the flag IS the override) but
    still enforce mutual exclusivity on the resulting write."""

    def test_backend_openocd_on_jlink_probe_writes_openocd_config(
        self, fake_box, cfg_file,
    ):
        runner = CliRunner()
        res = runner.invoke(nets_group, [
            'set-script', 'JLINK1', cfg_file,
            '--backend', 'openocd', '--box', 'test-box',
        ])
        assert res.exit_code == 0, res.output
        saved = fake_box['saves'][-1]
        assert 'openocd_config' in saved
        assert 'jlink_script' not in saved

    def test_show_backend_filter_only_returns_matching_field(self, fake_box):
        # Both legacy fields set; --backend openocd must only return that one.
        fake_box['db'][0]['jlink_script'] = base64.b64encode(b'jl').decode('ascii')
        fake_box['db'][0]['openocd_config'] = base64.b64encode(b'adapter driver ftdi\n').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, [
            'show-script', 'SWD', '--backend', 'openocd', '--box', 'test-box',
        ])
        assert res.exit_code == 0, res.output
        assert 'adapter driver ftdi' in res.stdout

    def test_remove_backend_filter_leaves_other_field_alone(self, fake_box):
        fake_box['db'][0]['jlink_script'] = base64.b64encode(b'jl').decode('ascii')
        fake_box['db'][0]['openocd_config'] = base64.b64encode(b'oc').decode('ascii')
        runner = CliRunner()
        res = runner.invoke(nets_group, [
            'remove-script', 'SWD', '--backend', 'openocd', '--box', 'test-box',
        ])
        assert res.exit_code == 0, res.output
        saved = fake_box['saves'][-1]
        assert 'openocd_config' not in saved
        assert 'jlink_script' in saved

# Copyright 2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the host-OS CLI install helpers shared by `lager install` and
`lager update`: the reconcile decision table, the --check status labels, the
install command's contract (exit codes, non-editable install), the probe
snippet executed under a real shell, and the drift guard pinning the deploy
scripts' mirrored shell to _host_cli.py.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

import cli as cli_pkg
from cli.commands.utility._host_cli import (
    HOST_CLI_EXIT_MESSAGES,
    HOST_CLI_PROBE_SNIPPET,
    HOST_VENV_APT_CMD,
    host_cli_check_status,
    host_cli_failure_message,
    host_cli_install_cmd,
    host_cli_reconcile_action,
    host_python_supported,
)
from cli.commands.utility.update import _parse_probe_output

PY_OK = {'HOST_PY_VERSION': '3.11'}


class TestReconcileAction:
    @pytest.mark.parametrize('host_v, box_v, code_changed, expected', [
        # Fast path: matching versions genuinely mean matching code.
        ('0.32.5', '0.32.5', False, 'skip'),
        # Rebuild path: the ref changed this run — a branch deploy can change
        # code without bumping __version__, so matching strings prove nothing.
        ('0.32.5', '0.32.5', True, 'install'),
        ('0.32.4', '0.32.5', False, 'install'),
        ('0.32.4', '0.32.5', True, 'install'),
        # Empty host version = missing or broken venv.
        ('', '0.32.5', False, 'install'),
        ('', '', False, 'install'),
        ('', '0.32.5', True, 'install'),
        # Unreadable box version fails open on the fast path only.
        ('0.32.5', '', False, 'skip-unknown'),
        ('0.32.5', '', True, 'install'),
    ])
    def test_matrix(self, host_v, box_v, code_changed, expected):
        assert host_cli_reconcile_action(
            host_v, box_v, code_changed=code_changed
        ) == expected


class TestHostPythonSupported:
    @pytest.mark.parametrize('raw, expected', [
        ('3.10', True),
        ('3.12', True),
        ('4.0', True),
        ('3.9', False),
        ('3.8', False),
        ('2.7', False),
        ('', None),
        ('garbage', None),
        ('3', None),
    ])
    def test_versions(self, raw, expected):
        assert host_python_supported({'HOST_PY_VERSION': raw}) is expected

    def test_absent_key_is_unknown(self):
        assert host_python_supported({}) is None


class TestCheckStatus:
    def test_python_too_old_skips_without_will_change(self):
        # A state `lager update` cannot fix must not flip --check's exit code
        # to "would change" forever.
        label, will_change = host_cli_check_status(
            {'HOST_PY_VERSION': '3.8'}, '0.32.5', True
        )
        assert label.startswith('skipped (host python3 3.8')
        assert will_change is False

    def test_python_unknown_skips_without_will_change(self):
        label, will_change = host_cli_check_status({}, '0.32.5', True)
        assert label == 'skipped (host python3 version unknown)'
        assert will_change is False

    def test_code_out_of_sync_defers_to_update(self):
        label, will_change = host_cli_check_status(PY_OK, '0.32.5', False)
        assert label == 'will install/upgrade (update will run)'
        assert will_change is True

    def test_missing_install(self):
        facts = {**PY_OK, 'HOST_CLI_VERSION': '', 'HOST_VENV_DIR': '0'}
        label, will_change = host_cli_check_status(facts, '0.32.5', True)
        assert label == 'will install (not present)'
        assert will_change is True

    def test_broken_venv_reinstall(self):
        facts = {**PY_OK, 'HOST_CLI_VERSION': '', 'HOST_VENV_DIR': '1'}
        label, will_change = host_cli_check_status(facts, '0.32.5', True)
        assert label == 'will reinstall (venv broken)'
        assert will_change is True

    def test_unreadable_box_version_fails_open(self):
        facts = {**PY_OK, 'HOST_CLI_VERSION': '0.32.5'}
        label, will_change = host_cli_check_status(facts, '', True)
        assert label == 'unknown (box version unreadable)'
        assert will_change is False

    def test_in_sync(self):
        facts = {**PY_OK, 'HOST_CLI_VERSION': '0.32.5'}
        label, will_change = host_cli_check_status(facts, '0.32.5', True)
        assert label == 'in sync (0.32.5)'
        assert will_change is False

    def test_upgrade(self):
        facts = {**PY_OK, 'HOST_CLI_VERSION': '0.32.4'}
        label, will_change = host_cli_check_status(facts, '0.32.5', True)
        assert label == 'will upgrade (0.32.4 -> 0.32.5)'
        assert will_change is True


class TestInstallCmdContract:
    def test_sparse_checkout_guard_comes_first(self):
        first = host_cli_install_cmd().splitlines()[0]
        assert 'test -d "$HOME/box/cli"' in first
        assert 'git -C "$HOME/box" sparse-checkout add cli' in first

    def test_load_bearing_pieces(self):
        cmd = host_cli_install_cmd()
        assert 'python3 -m venv' in cmd
        assert 'install --quiet "$HOME/box/cli"' in cmd
        assert 'ln -sfn "$HOME/.lager/venv/bin/lager" "$HOME/.local/bin/lager"' in cmd
        assert 'import cli; print(cli.__version__)' in cmd

    def test_non_editable_install(self):
        # An editable install would advance the host CLI on every git pull,
        # not just when a deploy actually runs the install command.
        assert ' -e ' not in host_cli_install_cmd()

    def test_distinct_exit_codes_present(self):
        cmd = host_cli_install_cmd()
        for code in HOST_CLI_EXIT_MESSAGES:
            assert f'exit {code}' in cmd

    def test_lager_mcp_symlink_is_guarded_and_best_effort(self):
        # Older deployed refs don't ship the lager-mcp entry point; the link
        # must be conditional so no dangling symlink is created, and its
        # absence must not fail the command.
        mcp_line = next(
            line for line in host_cli_install_cmd().splitlines()
            if 'lager-mcp' in line
        )
        assert 'test -x' in mcp_line
        assert mcp_line.rstrip().endswith('|| true')

    def test_failure_messages(self):
        for code, message in HOST_CLI_EXIT_MESSAGES.items():
            assert host_cli_failure_message(code) == message
        assert 'exit 7' in host_cli_failure_message(7)


class TestProbeSnippetShellExecution:
    """Run the real probe snippet under `sh` with a scratch HOME, pinning the
    facts' shell-level contract (and guarding against a quoting error)."""

    def _facts(self, home):
        env = dict(os.environ, HOME=str(home))
        result = subprocess.run(
            ['sh'], input=HOST_CLI_PROBE_SNIPPET, text=True,
            capture_output=True, env=env, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return _parse_probe_output(result.stdout)

    def test_bare_host_reports_nothing_installed(self, tmp_path):
        facts = self._facts(tmp_path)
        assert facts['HOST_CLI_VERSION'] == ''
        assert facts['HOST_VENV_DIR'] == '0'
        assert facts['HOST_BOX_CLI_DIR'] == '0'
        # Environment-dependent values: only the keys are pinned.
        assert 'HOST_PY_VERSION' in facts
        assert facts['HOST_ENSUREPIP'] in ('0', '1')

    def test_venv_version_passes_through(self, tmp_path):
        venv_bin = tmp_path / '.lager' / 'venv' / 'bin'
        venv_bin.mkdir(parents=True)
        shim = venv_bin / 'python'
        shim.write_text('#!/bin/sh\necho 9.9.9\n')
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        facts = self._facts(tmp_path)
        assert facts['HOST_CLI_VERSION'] == '9.9.9'
        assert facts['HOST_VENV_DIR'] == '1'

    def test_box_cli_dir_detected(self, tmp_path):
        (tmp_path / 'box' / 'cli').mkdir(parents=True)
        facts = self._facts(tmp_path)
        assert facts['HOST_BOX_CLI_DIR'] == '1'


class TestDeployScriptDrift:
    """setup_and_deploy_box.sh mirrors the install sequence in shell (it must
    stay standalone-runnable); these assertions pin its load-bearing literals
    to _host_cli.py so the two can't drift silently."""

    @pytest.fixture()
    def deploy_script(self):
        path = Path(cli_pkg.__file__).parent / 'deployment' / 'scripts' / 'setup_and_deploy_box.sh'
        return path.read_text()

    @pytest.fixture()
    def convert_script(self):
        path = Path(cli_pkg.__file__).parent / 'deployment' / 'scripts' / 'convert_to_sparse_checkout.sh'
        return path.read_text()

    def test_sparse_checkout_includes_cli_in_both_paths(self, deploy_script):
        # Fresh-clone and existing-repo paths must both materialize cli/.
        assert deploy_script.count('git sparse-checkout set box cli') == 2
        assert 'git sparse-checkout set box &&' not in deploy_script

    def test_install_sequence_literals(self, deploy_script):
        for literal in (
            '.lager/venv',
            '.local/bin/lager',
            'python3 -m venv',
            'python3 -Im ensurepip',
            'import cli; print(cli.__version__)',
        ):
            assert literal in deploy_script, literal

    def test_venv_apt_package_matches(self, deploy_script):
        assert 'python3-venv' in deploy_script
        assert 'python3-venv' in HOST_VENV_APT_CMD

    def test_convert_script_includes_cli(self, convert_script):
        assert 'git sparse-checkout set box cli' in convert_script

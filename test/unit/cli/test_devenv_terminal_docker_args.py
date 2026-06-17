#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the ``docker run`` command assembled by ``lager devenv
terminal`` and ``lager exec`` (cli/commands/development/devenv.py,
cli/commands/utility/exec_.py).

Covers:
  - the ``-v/--volume`` and ``-e/--env`` / ``--passenv`` passthrough options,
  - the ``.lager`` ``volumes`` / ``environment`` config keys (honored by both
    commands), and
  - user/group handling.

REGRESSION: ``terminal`` previously emitted a bare ``--group <g>`` flag, which
``docker run`` does not accept. user and group must be combined into
``--user user:group`` (matching ``lager exec``).
"""

from __future__ import annotations

import importlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

devenv_mod = importlib.import_module('cli.commands.development.devenv')
exec_mod = importlib.import_module('cli.commands.utility.exec_')
from cli.commands.development.devenv import terminal  # noqa: E402


BASE_CFG = {'image': 'example/img', 'mount_dir': '/app', 'shell': '/bin/bash'}


class _FakeProc:
    returncode = 0


def _values_for(args, flag):
    """Return every token that immediately follows ``flag`` in ``args``."""
    return [args[i + 1] for i, tok in enumerate(args[:-1]) if tok == flag]


def _run_terminal(tmp_path, monkeypatch, cli_args, devenv_config, debug=False):
    """Invoke ``terminal`` in a controlled environment; return (result, argv)."""
    # Deterministic environment: no SSH agent, empty HOME (so no ssh-key mounts).
    monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))

    config_path = str(tmp_path / '.lager')
    data = {'DEVENV': devenv_config}
    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        return _FakeProc()

    # global-config path points at something that does not exist -> mount skipped.
    missing_global = str(tmp_path / 'nope' / '.lager')

    with patch.object(devenv_mod, 'get_devenv_json', return_value=(config_path, data)), \
            patch.object(devenv_mod, 'get_global_config_file_path', return_value=missing_global), \
            patch.object(devenv_mod.subprocess, 'run', side_effect=fake_run):
        runner = CliRunner()
        result = runner.invoke(terminal, cli_args, obj=SimpleNamespace(debug=debug))

    return result, captured.get('argv', [])


def test_plain_run_mounts_project_dir(tmp_path, monkeypatch):
    result, argv = _run_terminal(tmp_path, monkeypatch, [], BASE_CFG)

    assert result.exit_code == 0, result.output
    assert argv[:4] == ['docker', 'run', '-it', '--init']
    assert f'{tmp_path}:/app' in _values_for(argv, '-v')
    assert argv[-1] == 'example/img'
    # No custom env/volumes requested.
    assert '--env' not in argv


def test_cli_volume_env_and_passenv(tmp_path, monkeypatch):
    monkeypatch.setenv('MY_HOST_VAR', 'hostval')
    result, argv = _run_terminal(
        tmp_path, monkeypatch,
        ['-v', 'cursor-data:/root/.cursor',
         '-e', 'HISTFILE=/root/.local/state/bash/history',
         '--passenv', 'MY_HOST_VAR'],
        BASE_CFG,
    )

    assert result.exit_code == 0, result.output
    assert 'cursor-data:/root/.cursor' in _values_for(argv, '-v')
    envs = _values_for(argv, '--env')
    assert 'HISTFILE=/root/.local/state/bash/history' in envs
    assert 'MY_HOST_VAR=hostval' in envs


def test_passenv_absent_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv('NOT_SET_VAR', raising=False)
    result, argv = _run_terminal(
        tmp_path, monkeypatch, ['--passenv', 'NOT_SET_VAR'], BASE_CFG,
    )

    assert result.exit_code == 0, result.output
    assert not any(tok.startswith('NOT_SET_VAR=') for tok in _values_for(argv, '--env'))


def test_config_volumes_and_environment(tmp_path, monkeypatch):
    cfg = {**BASE_CFG,
           'volumes': ['cfgvol:/data', '/host/cache:/root/.cache:ro'],
           'environment': ['FOO=bar', 'DEBUG=1']}
    result, argv = _run_terminal(tmp_path, monkeypatch, [], cfg)

    assert result.exit_code == 0, result.output
    vols = _values_for(argv, '-v')
    assert 'cfgvol:/data' in vols
    assert '/host/cache:/root/.cache:ro' in vols
    envs = _values_for(argv, '--env')
    assert 'FOO=bar' in envs
    assert 'DEBUG=1' in envs


def test_config_then_cli_ordering(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'volumes': ['cfgvol:/data']}
    result, argv = _run_terminal(tmp_path, monkeypatch, ['-v', 'clivol:/extra'], cfg)

    assert result.exit_code == 0, result.output
    vols = _values_for(argv, '-v')
    assert vols.index('cfgvol:/data') < vols.index('clivol:/extra')


def test_user_and_group_combined_into_user(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'user': 'root', 'group': '1000'}
    result, argv = _run_terminal(tmp_path, monkeypatch, [], cfg)

    assert result.exit_code == 0, result.output
    assert 'root:1000' in _values_for(argv, '--user')
    # Regression: docker run has no --group flag.
    assert '--group' not in argv


def test_group_only_yields_user_colon_group(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'group': '1000'}
    result, argv = _run_terminal(tmp_path, monkeypatch, [], cfg)

    assert result.exit_code == 0, result.output
    assert ':1000' in _values_for(argv, '--user')
    assert '--group' not in argv


def test_debug_prints_docker_command(tmp_path, monkeypatch):
    result, _ = _run_terminal(tmp_path, monkeypatch, [], BASE_CFG, debug=True)

    assert result.exit_code == 0, result.output
    assert 'Docker command:' in result.output


def test_exec_volumes_and_environment(tmp_path, monkeypatch):
    """``lager exec`` honors config volumes/environment and the --volume flag."""
    monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
    section = {'image': 'example/img', 'mount_dir': '/app', 'shell': '/bin/bash',
               'volumes': ['cfgvol:/v'], 'environment': ['CFG=1']}
    config_path = str(tmp_path / '.lager')
    missing_global = str(tmp_path / 'nope' / '.lager')
    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        return _FakeProc()

    with patch.object(exec_mod, 'get_global_config_file_path', return_value=missing_global), \
            patch.object(exec_mod.subprocess, 'run', side_effect=fake_run):
        rc = exec_mod._run_command_local(
            section, config_path, 'echo hi', None, (), False, True, True,
            None, None, ('CLI=2',), (), ('clivol:/w',),
        )

    assert rc == 0
    argv = captured['argv']
    vols = _values_for(argv, '-v')
    assert 'cfgvol:/v' in vols
    assert 'clivol:/w' in vols
    assert '--env=CFG=1' in argv
    assert '--env=CLI=2' in argv

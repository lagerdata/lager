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


def _run_terminal(tmp_path, monkeypatch, cli_args, devenv_config):
    """Invoke ``terminal`` in a controlled environment; return (result, argv).

    ``argv`` is empty when the command exits before launching (e.g. --info)."""
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
        result = runner.invoke(terminal, cli_args, obj=SimpleNamespace(debug=False))

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


def test_network_platform_ports_from_config(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'network': 'host', 'platform': 'linux/amd64',
           'ports': ['8080:8080', '9090:9090']}
    result, argv = _run_terminal(tmp_path, monkeypatch, [], cfg)

    assert result.exit_code == 0, result.output
    assert '--network=host' in argv
    assert 'linux/amd64' in _values_for(argv, '--platform')
    ports = _values_for(argv, '-p')
    assert '8080:8080' in ports
    assert '9090:9090' in ports


def test_cli_network_overrides_config(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'network': 'host'}
    result, argv = _run_terminal(tmp_path, monkeypatch, ['--network', 'bridge'], cfg)

    assert result.exit_code == 0, result.output
    assert '--network=bridge' in argv
    assert '--network=host' not in argv


def test_config_and_cli_ports_combined(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'ports': ['8080:8080']}
    result, argv = _run_terminal(tmp_path, monkeypatch, ['-p', '7000:7000'], cfg)

    assert result.exit_code == 0, result.output
    ports = _values_for(argv, '-p')
    assert '8080:8080' in ports
    assert '7000:7000' in ports


def test_info_prints_command_and_does_not_launch(tmp_path, monkeypatch):
    result, argv = _run_terminal(tmp_path, monkeypatch, ['--info'], BASE_CFG)

    assert result.exit_code == 0, result.output
    assert 'Resolved docker command:' in result.output
    assert 'nothing was launched' in result.output
    # subprocess.run must not have been called.
    assert argv == []


def test_entrypoint_from_config(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'entrypoint': '/bin/bash'}
    result, argv = _run_terminal(tmp_path, monkeypatch, [], cfg)

    assert result.exit_code == 0, result.output
    assert '/bin/bash' in _values_for(argv, '--entrypoint')


def test_cli_entrypoint_overrides_config(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'entrypoint': '/bin/bash'}
    result, argv = _run_terminal(tmp_path, monkeypatch, ['--entrypoint', '/bin/sh'], cfg)

    assert result.exit_code == 0, result.output
    assert _values_for(argv, '--entrypoint') == ['/bin/sh']


def test_cli_user_overrides_config(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'user': 'root'}
    result, argv = _run_terminal(tmp_path, monkeypatch, ['--user', '1000'], cfg)

    assert result.exit_code == 0, result.output
    assert '1000' in _values_for(argv, '--user')
    assert 'root' not in _values_for(argv, '--user')


def test_volume_expands_project_root_and_home(tmp_path, monkeypatch):
    cfg = {**BASE_CFG, 'volumes': ['${PROJECT_ROOT}:/workspace', '~/data:/data']}
    result, argv = _run_terminal(tmp_path, monkeypatch, [], cfg)

    assert result.exit_code == 0, result.output
    vols = _values_for(argv, '-v')
    # ${PROJECT_ROOT} -> dir containing .lager (tmp_path); ~ -> HOME (tmp_path).
    assert f'{tmp_path}:/workspace' in vols
    assert f'{tmp_path}/data:/data' in vols


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

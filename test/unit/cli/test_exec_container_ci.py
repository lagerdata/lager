#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for how ``lager exec`` chooses between starting a container and running
the command in place (cli/commands/utility/exec_.py).

REGRESSION: ``exec`` used to pick between two runners -- ``docker run`` on a developer's
machine, and running the command directly when it detected a container-based CI job, on
the reasoning that such a job is already inside the devenv image. The second path was
lost when the command moved from cli/exec/commands.py to cli/commands/utility/exec_.py,
leaving ``is_container_ci()`` defined, exported, and called by nothing. Anyone whose CI
job runs inside the devenv image got "Docker is not installed or not in PATH", because a
job container has neither a Docker binary nor a socket.

Covers:
  - container-based CI (GitHub, GitLab, Drone, Bitbucket) runs in place, spawning no docker,
  - a plain host still assembles the ``docker run`` command line,
  - Jenkins and a bare ``CI=true`` are host-shaped, not container-shaped,
  - ``LAGER_CI_OVERRIDE`` forces the Docker path back on even under CI,
  - the in-place path does not chdir, and applies ``--env`` / the ``environment`` key,
  - container-only flags warn instead of being silently dropped.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

exec_mod = importlib.import_module('cli.commands.utility.exec_')
from cli.commands.utility.exec_ import exec_  # noqa: E402


BASE_CFG = {'image': 'example/img', 'mount_dir': '/app', 'shell': '/bin/bash'}

# Every CI system whose jobs run inside a container image, and the variables that identify it.
CONTAINER_CI_ENVS = {
    'github': {'CI': 'true', 'GITHUB_RUN_ID': '1'},
    'drone': {'CI': 'true', 'DRONE': 'true'},
    'bitbucket': {'CI': 'true', 'BITBUCKET_BUILD_NUMBER': '7'},
    'gitlab': {'CI': 'true', 'CI_SERVER_NAME': 'GitLab'},
}

# CI, but running on a host with a real Docker: these keep the container path.
HOST_SHAPED_ENVS = {
    'jenkins': {'CI': 'true', 'BUILD_TAG': 'jenkins-job-1'},
    'generic': {'CI': 'true'},
    'developer machine': {},
}

# Variables any of the above set; cleared so the host cases are genuinely host cases.
_ALL_CI_VARS = ('CI', 'GITHUB_RUN_ID', 'DRONE', 'BITBUCKET_BUILD_NUMBER',
                'CI_SERVER_NAME', 'BUILD_TAG', 'LAGER_CI_OVERRIDE')


class _FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _run_exec(tmp_path, monkeypatch, cli_args, ci_env, devenv_config=None, returncode=0):
    """Invoke ``exec`` with a .lager in tmp_path; return (result, argv, kwargs).

    ``argv`` is the command handed to subprocess.run -- a ``docker run ...`` list on the
    container path, a ``[shell, '-c', cmd]`` list on the in-place path.
    """
    for var in _ALL_CI_VARS:
        monkeypatch.delenv(var, raising=False)
    for name, value in ci_env.items():
        monkeypatch.setenv(name, value)

    config_path = str(tmp_path / '.lager')
    data = {'DEVENV': dict(devenv_config or BASE_CFG)}
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(data, f)

    captured = {}

    def fake_run(argv, **kwargs):
        captured['argv'] = argv
        captured['kwargs'] = kwargs
        return _FakeProc(returncode)

    with patch.object(exec_mod, 'get_devenv_json', return_value=(config_path, data)), \
         patch.object(exec_mod.subprocess, 'run', side_effect=fake_run):
        result = CliRunner().invoke(
            exec_, cli_args, obj=SimpleNamespace(debug=False), catch_exceptions=False)

    if 'argv' not in captured:
        raise AssertionError(f'exec exited without running anything: {result.output}')
    return result, captured['argv'], captured['kwargs']


@pytest.mark.parametrize('ci_name', sorted(CONTAINER_CI_ENVS))
def test_container_ci_runs_in_place_and_never_spawns_docker(tmp_path, monkeypatch, ci_name):
    """The whole point: inside a CI job container, run the command, do not start one."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], CONTAINER_CI_ENVS[ci_name], cfg)

    assert result.exit_code == 0
    assert argv == ['/bin/bash', '-c', 'make all']
    assert 'docker' not in argv


@pytest.mark.parametrize('env_name', sorted(HOST_SHAPED_ENVS))
def test_host_shaped_environments_still_start_a_container(tmp_path, monkeypatch, env_name):
    """A Jenkins agent and a plain runner are hosts: they have Docker and should use it."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], HOST_SHAPED_ENVS[env_name], cfg)

    assert result.exit_code == 0
    assert argv[:3] == ['docker', 'run', '--rm']
    assert argv[-3:] == ['/bin/bash', '-c', 'make all']


def test_lager_ci_override_forces_the_docker_path(tmp_path, monkeypatch):
    """The documented escape hatch, for anyone who does want a container from inside CI."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    env = dict(CONTAINER_CI_ENVS['github'], LAGER_CI_OVERRIDE='1')
    result, argv, _ = _run_exec(tmp_path, monkeypatch, ['build'], env, cfg)

    assert result.exit_code == 0
    assert argv[:3] == ['docker', 'run', '--rm']


def test_in_place_path_does_not_chdir(tmp_path, monkeypatch):
    """The Docker path bind-mounts the checkout at mount_dir and sets -w. In place, the
    checkout is wherever CI put it, so imposing mount_dir would break every relative path."""
    cfg = dict(BASE_CFG, **{'mount_dir': '/app', 'cmd.build': 'make all'})
    _, _, kwargs = _run_exec(
        tmp_path, monkeypatch, ['build'], CONTAINER_CI_ENVS['github'], cfg)

    assert kwargs.get('cwd') is None


def test_in_place_path_applies_env_and_config_environment(tmp_path, monkeypatch):
    """--env and the `environment` config key are the two options that still mean
    something without a container, so they must reach the child."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all', 'environment': ['FROM_CONFIG=1']})
    _, _, kwargs = _run_exec(
        tmp_path, monkeypatch, ['build', '--env', 'FROM_FLAG=2'],
        CONTAINER_CI_ENVS['github'], cfg)

    child_env = kwargs.get('env')
    assert child_env['FROM_CONFIG'] == '1'
    assert child_env['FROM_FLAG'] == '2'
    # Inherited, which is also what makes --passenv a no-op on this path.
    assert 'PATH' in child_env


def test_in_place_path_propagates_the_exit_code(tmp_path, monkeypatch):
    """A failing build has to fail the CI step."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    result, _, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], CONTAINER_CI_ENVS['github'], cfg, returncode=2)

    assert result.exit_code == 2


def test_container_only_flags_warn_rather_than_vanish(tmp_path, monkeypatch):
    """Silence is what let this go unnoticed. Asking for --user in place gets an answer."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build', '--user', 'root', '--volume', '/a:/b'],
        CONTAINER_CI_ENVS['github'], cfg)

    assert result.exit_code == 0
    assert argv == ['/bin/bash', '-c', 'make all']
    assert '--user' in result.output
    assert '--volume' in result.output


def test_config_user_alone_does_not_warn(tmp_path, monkeypatch):
    """A .lager is shared between a developer's machine and CI, so carrying `user` is
    normal. Warning on it would fire on every CI run and teach people to ignore warnings."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all', 'user': 'root'})
    result, _, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], CONTAINER_CI_ENVS['github'], cfg)

    assert result.exit_code == 0
    assert 'Warning' not in result.output


@pytest.mark.parametrize('shell_value', [None, ''], ids=['absent', 'empty'])
def test_shell_defaults_when_the_config_does_not_give_one(tmp_path, monkeypatch, shell_value):
    """The legacy in-container path read a bare section.get('shell') and would have handed
    None to subprocess.run. Match the Docker path's /bin/bash default instead, and treat an
    explicit empty string the same way -- a .lager can carry one."""
    cfg = {'image': 'example/img', 'mount_dir': '/app', 'cmd.build': 'make all'}
    if shell_value is not None:
        cfg['shell'] = shell_value
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], CONTAINER_CI_ENVS['github'], cfg)

    assert result.exit_code == 0
    assert argv[0] == '/bin/bash'


def test_raw_command_also_runs_in_place(tmp_path, monkeypatch):
    """`--command` takes the same path as a saved command name."""
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['--command', 'echo hi'], CONTAINER_CI_ENVS['github'])

    assert result.exit_code == 0
    assert argv == ['/bin/bash', '-c', 'echo hi']


def test_extra_args_are_appended_in_place(tmp_path, monkeypatch):
    """EXTRA_ARGS are appended at runtime on the Docker path; keep that here."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make'})
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build', 'target', '-j4'],
        CONTAINER_CI_ENVS['github'], cfg)

    assert result.exit_code == 0
    assert argv == ['/bin/bash', '-c', 'make target -j4']

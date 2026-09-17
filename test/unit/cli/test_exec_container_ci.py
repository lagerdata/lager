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
                'CI_SERVER_NAME', 'BUILD_TAG', 'LAGER_CI_OVERRIDE',
                'LAGER_EXEC_IN_PLACE')


class _FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode


def _run_exec(tmp_path, monkeypatch, cli_args, ci_env, devenv_config=None, returncode=0,
              in_container=True):
    """Invoke ``exec`` with a .lager in tmp_path; return (result, argv, kwargs).

    ``argv`` is the command handed to subprocess.run -- a ``docker run ...`` list on the
    container path, a ``[shell, '-c', cmd]`` list on the in-place path.

    ``in_container`` is the detection seam. The real probe reads /.dockerenv and
    /proc/1/cgroup, neither of which exists on the machine most of these tests run on,
    so it is patched rather than simulated here; `running_in_container` has its own
    tests against a fabricated root. It defaults to True because that is the job shape
    the CI variables above were always taken to mean, and the cases that set it False
    are the ones this file exists to pin down.
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
         patch.object(exec_mod, 'running_in_container', return_value=in_container), \
         patch.object(exec_mod.subprocess, 'run', side_effect=fake_run):
        result = CliRunner().invoke(
            exec_, cli_args, obj=SimpleNamespace(debug=False), catch_exceptions=False)

    if 'argv' not in captured:
        raise AssertionError(f'exec exited without running anything: {result.output}')
    return result, captured['argv'], captured['kwargs']


@pytest.mark.parametrize('ci_name', sorted(CONTAINER_CI_ENVS))
def test_container_ci_runs_in_place_and_never_spawns_docker(tmp_path, monkeypatch, ci_name):
    """The whole point: inside a CI job container, run the command, do not start one.

    Note `in_container` defaults to True here: this is a job that declared an image.
    The same CI variables with no image take the Docker path, which is the case
    directly below.
    """
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


# ---------------------------------------------------------------------------
# A container-based CI job that declared no container (#504)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('ci_name', sorted(CONTAINER_CI_ENVS))
def test_a_ci_job_with_no_container_uses_docker_and_says_so(tmp_path, monkeypatch, ci_name):
    """The defect: the CI variables were read as proof of a container.

    A GitHub Actions job with no `container:` block runs on the runner host. Running
    the command in place there used the runner's own filesystem and toolchain while
    the user believed they had a devenv build, and nothing was printed about it.
    Hosted runners have Docker, so the Docker path is available and honest.
    """
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], CONTAINER_CI_ENVS[ci_name], cfg,
        in_container=False)

    assert result.exit_code == 0
    assert argv[:3] == ['docker', 'run', '--rm']
    assert 'declares no container' in result.output


def test_the_override_runs_in_place_on_a_job_with_no_container(tmp_path, monkeypatch):
    """The way back, for a runner that has no Docker to fall back to."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    env = dict(CONTAINER_CI_ENVS['github'], LAGER_EXEC_IN_PLACE='1')
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], env, cfg, in_container=False)

    assert result.exit_code == 0
    assert argv == ['/bin/bash', '-c', 'make all']
    assert 'declares no container' not in result.output


def test_the_override_runs_in_place_outside_ci_entirely(tmp_path, monkeypatch):
    """It is an answer, not a modifier: no CI variable has to be present."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], {'LAGER_EXEC_IN_PLACE': 'yes'}, cfg,
        in_container=False)

    assert argv == ['/bin/bash', '-c', 'make all']


def test_the_override_set_to_zero_starts_a_container(tmp_path, monkeypatch):
    """`0` means Docker here.

    The opposite of LAGER_CI_OVERRIDE, where any non-empty value including `0`
    turns CI detection off. That asymmetry is why this is a separate variable
    rather than another reading of the old one.
    """
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    env = dict(CONTAINER_CI_ENVS['github'], LAGER_EXEC_IN_PLACE='0')
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], env, cfg, in_container=True)

    assert argv[:3] == ['docker', 'run', '--rm']


def test_an_unrecognized_override_value_is_ignored(tmp_path, monkeypatch):
    """Guessing at `maybe` is worse than falling back to what was detected."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    env = dict(CONTAINER_CI_ENVS['github'], LAGER_EXEC_IN_PLACE='maybe')
    result, argv, _ = _run_exec(
        tmp_path, monkeypatch, ['build'], env, cfg, in_container=True)

    assert argv == ['/bin/bash', '-c', 'make all']


def test_the_old_override_still_works_and_names_its_cost(tmp_path, monkeypatch):
    """LAGER_CI_OVERRIDE keeps working, and now says what else it changes."""
    cfg = dict(BASE_CFG, **{'cmd.build': 'make all'})
    env = dict(CONTAINER_CI_ENVS['github'], LAGER_CI_OVERRIDE='1')
    result, argv, _ = _run_exec(tmp_path, monkeypatch, ['build'], env, cfg)

    assert argv[:3] == ['docker', 'run', '--rm']
    assert 'every lager command' in result.output
    assert 'LAGER_EXEC_IN_PLACE=0' in result.output


def test_the_exec_override_does_not_touch_lock_behavior(monkeypatch):
    """The reason this variable exists at all.

    LAGER_CI_OVERRIDE reaches `get_ci_environment`, so it also makes the lock
    holder a plain user name and drops the collision wait from the CI default to
    the developer one. A job that set it for its exec step lost CI lock queueing
    for its hardware steps. This one answers only `lager exec`.
    """
    from cli.context.ci_detection import CIEnvironment, get_ci_environment

    for var in _ALL_CI_VARS:
        monkeypatch.delenv(var, raising=False)
    for name, value in CONTAINER_CI_ENVS['github'].items():
        monkeypatch.setenv(name, value)

    monkeypatch.setenv('LAGER_EXEC_IN_PLACE', '1')
    assert get_ci_environment() is CIEnvironment.GITHUB
    monkeypatch.setenv('LAGER_EXEC_IN_PLACE', '0')
    assert get_ci_environment() is CIEnvironment.GITHUB

    # The old variable, for contrast: this is the behavior being split away.
    monkeypatch.setenv('LAGER_CI_OVERRIDE', '0')
    assert get_ci_environment() is CIEnvironment.HOST


# ---------------------------------------------------------------------------
# running_in_container(), against a fabricated filesystem root
# ---------------------------------------------------------------------------

def _no_container_env(monkeypatch):
    monkeypatch.delenv('container', raising=False)


def test_a_dockerenv_file_means_a_container(tmp_path, monkeypatch):
    from cli.context.ci_detection import running_in_container
    _no_container_env(monkeypatch)
    (tmp_path / '.dockerenv').write_text('')
    assert running_in_container(tmp_path) is True


def test_a_containerenv_file_means_a_container(tmp_path, monkeypatch):
    from cli.context.ci_detection import running_in_container
    _no_container_env(monkeypatch)
    (tmp_path / 'run').mkdir()
    (tmp_path / 'run' / '.containerenv').write_text('')
    assert running_in_container(tmp_path) is True


def test_the_container_env_var_means_a_container(tmp_path, monkeypatch):
    from cli.context.ci_detection import running_in_container
    monkeypatch.setenv('container', 'podman')
    assert running_in_container(tmp_path) is True


def test_a_runtime_named_in_pid_ones_cgroup_means_a_container(tmp_path, monkeypatch):
    from cli.context.ci_detection import running_in_container
    _no_container_env(monkeypatch)
    proc = tmp_path / 'proc' / '1'
    proc.mkdir(parents=True)
    (proc / 'cgroup').write_text(
        '12:pids:/docker/3c1f0a\n11:memory:/docker/3c1f0a\n')
    assert running_in_container(tmp_path) is True


def test_a_bare_cgroup_v2_path_is_not_evidence(tmp_path, monkeypatch):
    """Inside many cgroup-v2 containers PID 1 reports `0::/`, same as a host.

    So this probe cannot be the one that decides; the marker files carry it.
    Answering True here would call every modern Linux host a container.
    """
    from cli.context.ci_detection import running_in_container
    _no_container_env(monkeypatch)
    proc = tmp_path / 'proc' / '1'
    proc.mkdir(parents=True)
    (proc / 'cgroup').write_text('0::/\n')
    assert running_in_container(tmp_path) is False


def test_a_docker_host_is_not_a_container(tmp_path, monkeypatch):
    """The false positive that would have preserved the bug.

    Any Linux host that has run a container has /var/lib/docker paths in
    /proc/self/mountinfo, so a probe that matched "docker" there would report
    a plain CI runner as a container -- exactly the wrong answer, and exactly
    what #504 is about. PID 1's cgroup on a host names the init system.
    """
    from cli.context.ci_detection import running_in_container
    _no_container_env(monkeypatch)
    proc = tmp_path / 'proc'
    (proc / '1').mkdir(parents=True)
    (proc / '1' / 'cgroup').write_text('0::/init.scope\n')
    (proc / 'self').mkdir()
    (proc / 'self' / 'mountinfo').write_text(
        '31 24 0:26 / /var/lib/docker/overlay2 rw shared:15 - ext4 /dev/sda1 rw\n'
        '44 31 0:39 / /var/lib/docker/containers rw - overlay overlay rw\n')
    assert running_in_container(tmp_path) is False


def test_an_empty_root_is_not_a_container(tmp_path, monkeypatch):
    from cli.context.ci_detection import running_in_container
    _no_container_env(monkeypatch)
    assert running_in_container(tmp_path) is False


def test_a_root_that_cannot_be_read_never_raises(tmp_path, monkeypatch):
    """False means "no evidence". It is a probe, not an authority."""
    from cli.context.ci_detection import running_in_container
    _no_container_env(monkeypatch)
    assert running_in_container(tmp_path / 'does-not-exist') is False

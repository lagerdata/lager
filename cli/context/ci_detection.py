# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.context.ci_detection

    CI environment detection utilities
"""
import os
from enum import Enum
from pathlib import Path


class CIEnvironment(Enum):
    """
    Enum representing supported CI systems
    """
    HOST = 'host'
    DRONE = 'drone'
    GITHUB = 'github'
    BITBUCKET = 'bitbucket'
    GITLAB = 'gitlab'
    GENERIC_CI = 'ci'
    JENKINS = 'jenkins'


_CONTAINER_CI = set((
    CIEnvironment.DRONE,
    CIEnvironment.GITHUB,
    CIEnvironment.BITBUCKET,
    CIEnvironment.GITLAB,
))


def is_container_ci(ci_env):
    """
    Check if the CI environment usually runs the job in a container.

    Supported container-based CI solutions include:
    - Drone CI
    - GitHub Actions
    - Bitbucket Pipelines
    - GitLab CI

    This answers "which CI system is this", from environment variables, and
    nothing more. It does NOT answer "is this process in a container": a
    GitHub Actions job with no `container:` block runs on the runner host and
    still lands in this set. Pair it with `running_in_container()` before
    treating the job as already inside an image.
    """
    return ci_env in _CONTAINER_CI


# Paths a container runtime leaves behind, relative to the filesystem root.
_CONTAINER_MARKER_FILES = ('.dockerenv', 'run/.containerenv')

# Runtime names that appear in PID 1's cgroup path inside a container. On a
# host, PID 1 is the init system and its cgroup names none of these.
_CGROUP_MARKERS = ('docker', 'containerd', 'kubepods', 'lxc', 'libpod',
                   'crio', 'garden')


def running_in_container(root='/'):
    """
    Whether this process is inside a container.

    Cheap, best-effort, and never raises: every probe is guarded, and an
    answer of False only means "no evidence", which is the safe direction.
    Being wrong towards False starts a container that was not needed; being
    wrong towards True runs a build command on a CI runner's host filesystem
    and calls it a devenv build, which is the defect this exists to stop.

    `root` is the filesystem to inspect, and exists so tests can point it at
    a fabricated tree -- macOS has no /proc at all, so there is otherwise
    nothing to assert against.

    /proc/self/mountinfo is deliberately NOT consulted, although it is the
    usual third probe. Any Linux host that has run a container has
    /var/lib/docker paths mounted, so matching "docker" there reports a
    plain CI runner as a container -- which is exactly the wrong answer, and
    exactly the bug this function was added to fix.
    """
    base = Path(root)

    for marker in _CONTAINER_MARKER_FILES:
        try:
            if (base / marker).exists():
                return True
        except OSError:
            pass

    # systemd-nspawn, podman and LXC export this; Docker does not.
    if (os.environ.get('container') or '').strip():
        return True

    try:
        cgroup = (base / 'proc' / '1' / 'cgroup').read_text(errors='ignore')
    except OSError:
        return False
    for line in cgroup.splitlines():
        # cgroup v2 reports a single `0::<path>`, and inside many containers
        # that path is just `/` -- indistinguishable from a host, which is
        # why the marker files above carry most of the weight.
        path = line.rpartition(':')[2]
        if any(marker in path for marker in _CGROUP_MARKERS):
            return True
    return False


# Exec-only override. Unlike LAGER_CI_OVERRIDE it changes nothing but the
# choice `lager exec` makes: box locking, the lock holder and the collision
# wait are all untouched by it.
EXEC_IN_PLACE_ENV = 'LAGER_EXEC_IN_PLACE'
_TRUTHY = ('1', 'true', 'yes')
_FALSEY = ('0', 'false', 'no')


def exec_in_place_override():
    """
    True to force running in place, False to force Docker, None if unset.

    An unrecognized value returns None rather than guessing. `0` means
    Docker here, which is the opposite of LAGER_CI_OVERRIDE, where any
    non-empty value including `0` turns CI detection off entirely.
    """
    raw = (os.environ.get(EXEC_IN_PLACE_ENV) or '').strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    return None


def get_ci_environment():
    """
    Determine whether we are running in CI or not.

    Returns the appropriate CIEnvironment enum value based on
    environment variables set by various CI systems.

    LAGER_CI_OVERRIDE answers HOST for every caller, which is why it changes
    box locking as well as `lager exec`: the lock holder becomes a plain
    user name with no job identity, and the collision wait drops from the CI
    default to the developer one. To change only `lager exec`, set
    LAGER_EXEC_IN_PLACE instead.
    """
    if os.getenv('LAGER_CI_OVERRIDE'):
        return CIEnvironment.HOST

    if os.getenv('CI') == 'true':
        if os.getenv('DRONE') == 'true':
            return CIEnvironment.DRONE
        if os.getenv('GITHUB_RUN_ID'):
            return CIEnvironment.GITHUB
        if os.getenv('BITBUCKET_BUILD_NUMBER'):
            return CIEnvironment.BITBUCKET
        if 'gitlab' in os.getenv('CI_SERVER_NAME', '').lower():
            return CIEnvironment.GITLAB
        if 'jenkins' in os.getenv('BUILD_TAG', '').lower():
            return CIEnvironment.JENKINS
        return CIEnvironment.GENERIC_CI

    return CIEnvironment.HOST

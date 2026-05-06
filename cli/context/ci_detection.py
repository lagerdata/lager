# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.context.ci_detection

    CI environment detection utilities
"""
import os
from enum import Enum


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
    Check if the CI environment is container-based.

    Supported container-based CI solutions include:
    - Drone CI
    - GitHub Actions
    - Bitbucket Pipelines
    - GitLab CI
    """
    return ci_env in _CONTAINER_CI


def get_ci_environment():
    """
    Determine whether we are running in CI or not.

    Returns the appropriate CIEnvironment enum value based on
    environment variables set by various CI systems.
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

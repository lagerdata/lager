#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `_ssh_failure_line` — the guard that stops `lager update`
reporting a lost SSH connection as a Docker build failure.

Reported from the field: an operator's VPN dropped mid-update, the build
step's SSH found a dead control master and then could not reach port 22,
and `lager update` answered "Failed to rebuild Docker container". Every
hint in that handler's chain is about the box's Docker daemon, and none of
them matched — so the message named the wrong subsystem and offered nothing.

The false-positive guard matters as much as the detection: a build that
cannot reach an apt mirror prints its own timeouts, and those ARE Docker
build failures. Matching is on ssh's own message formats only.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

from cli.commands.utility.update import _ssh_failure_line


# The tail of the reported run, with the address generalized.
VPN_DROP_OUTPUT = [
    'mux_client_request_session: read from master failed: Broken pipe',
    'ssh: connect to host 192.168.1.50 port 22: Operation timed out',
]


class TransportLossIsNotABuildFailure(unittest.TestCase):

    def test_reports_the_vpn_drop_output_as_transport(self):
        kind, line = _ssh_failure_line(VPN_DROP_OUTPUT)
        self.assertEqual(kind, 'transport')
        self.assertIn('mux_client_request_session', line)

    def test_reports_a_bare_connect_timeout_as_transport(self):
        kind, line = _ssh_failure_line(
            ['ssh: connect to host box.local port 22: Connection timed out'])
        self.assertEqual(kind, 'transport')
        self.assertTrue(line.startswith('ssh: connect to host'))

    def test_reports_a_dropped_session_as_transport(self):
        kind, _ = _ssh_failure_line(
            ['client_loop: send disconnect: Broken pipe'])
        self.assertEqual(kind, 'transport')

    def test_reports_a_serveralive_giveup_as_transport(self):
        kind, _ = _ssh_failure_line(
            ['Timeout, server 192.168.1.50 not responding.'])
        self.assertEqual(kind, 'transport')

    def test_strips_surrounding_whitespace_from_the_reported_line(self):
        kind, line = _ssh_failure_line(
            ['   ssh: connect to host box port 22: No route to host  '])
        self.assertEqual(kind, 'transport')
        self.assertEqual(line, 'ssh: connect to host box port 22: No route to host')


class AuthLossIsHeldApartFromTransportLoss(unittest.TestCase):
    """The box answered and refused the key. The remedy is a key, not a
    network — so the two cannot share one message."""

    def test_reports_publickey_refusal_as_auth(self):
        kind, _ = _ssh_failure_line(
            ['Permission denied (publickey,password).'])
        self.assertEqual(kind, 'auth')

    def test_reports_host_key_verification_as_auth(self):
        kind, _ = _ssh_failure_line(['Host key verification failed.'])
        self.assertEqual(kind, 'auth')


class RealBuildFailuresStayBuildFailures(unittest.TestCase):
    """The false-positive guard. Every line here is Docker output that
    contains a word the naive check would have matched on."""

    def test_apt_mirror_timeout_is_not_an_ssh_failure(self):
        self.assertEqual(
            _ssh_failure_line([
                '#12 5.4 Err:1 http://archive.ubuntu.com/ubuntu noble InRelease',
                '#12 5.4   Could not connect to archive.ubuntu.com:80, '
                'connection timed out',
                '#12 ERROR: process did not complete successfully',
            ]),
            (None, None),
        )

    def test_pip_read_timeout_is_not_an_ssh_failure(self):
        self.assertEqual(
            _ssh_failure_line([
                '#15 61.2 WARNING: Retrying after connection broken by '
                "'ReadTimeoutError(\"HTTPSConnectionPool(host='pypi.org')\")'",
            ]),
            (None, None),
        )

    def test_docker_permission_error_is_not_an_ssh_auth_failure(self):
        # The handler already has a Docker-daemon hint for this one; it must
        # keep reaching it rather than being claimed as an SSH key problem.
        self.assertEqual(
            _ssh_failure_line([
                "#4 ERROR: open /var/lib/docker/tmp/x: permission denied",
            ]),
            (None, None),
        )

    def test_no_space_left_is_not_an_ssh_failure(self):
        self.assertEqual(
            _ssh_failure_line(
                ['#9 12.0 write /root/.cargo: No space left on device']),
            (None, None),
        )

    def test_empty_and_missing_output_are_not_ssh_failures(self):
        self.assertEqual(_ssh_failure_line([]), (None, None))
        self.assertEqual(_ssh_failure_line(None), (None, None))
        self.assertEqual(_ssh_failure_line(['', None]), (None, None))


if __name__ == '__main__':
    unittest.main()

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for cli/commands/box/_host_ops.py.

Drives every branch of apt_install + sysctl_apply via injected SshRunners.
Mirrors test_mount_prep's style: no real SSH, no real subprocess.
"""

import unittest

from cli.commands.box import _host_ops as ops


def _runner_returning(rc, stdout="", stderr=""):
    calls = []

    def runner(box_ip, cmd):
        calls.append((box_ip, cmd))
        return rc, stdout, stderr

    return runner, calls


class AptInstall(unittest.TestCase):
    def test_no_packages_is_noop(self):
        runner, calls = _runner_returning(0)
        result = ops.apt_install("1.2.3.4", [], ssh_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "noop")
        self.assertEqual(calls, [])

    def test_happy_path_runs_apt_get(self):
        runner, calls = _runner_returning(0)
        result = ops.apt_install("1.2.3.4", ["tcpdump", "iptables-persistent"], ssh_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "installed")
        self.assertEqual(len(calls), 1)
        _, cmd = calls[0]
        self.assertIn("apt-get install -y", cmd)
        self.assertIn("tcpdump", cmd)
        self.assertIn("iptables-persistent", cmd)
        self.assertIn("DEBIAN_FRONTEND=noninteractive", cmd)
        self.assertIn("--no-install-recommends", cmd)

    def test_sudo_failure_surfaces_bootstrap_message(self):
        runner, _ = _runner_returning(
            1, stderr="sudo: a password is required",
        )
        result = ops.apt_install("1.2.3.4", ["tcpdump"], ssh_runner=runner)
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "failed")
        self.assertIn("passwordless sudo is not configured", result.message)
        self.assertIn("/etc/sudoers.d/lager-box-config", result.message)
        self.assertIsNotNone(result.manual_fix)
        self.assertIn("sudo apt-get install", result.manual_fix)

    def test_apt_real_failure_does_not_show_bootstrap(self):
        runner, _ = _runner_returning(
            100, stderr="E: Unable to locate package fake-package",
        )
        result = ops.apt_install("1.2.3.4", ["fake-package"], ssh_runner=runner)
        self.assertFalse(result.ok)
        self.assertIn("Unable to locate package", result.message)
        # Bootstrap noise would only confuse users when the real problem is
        # a typo'd package name.
        self.assertNotIn("/etc/sudoers.d/lager-box-config", result.message)

    def test_packages_are_shell_quoted(self):
        # Even though the validator rejects shell metacharacters, defense-in-
        # depth: anything that reaches apt_install gets quoted.
        runner, calls = _runner_returning(0)
        ops.apt_install("1.2.3.4", ["weird name"], ssh_runner=runner)
        _, cmd = calls[0]
        self.assertIn("'weird name'", cmd)


class SysctlApply(unittest.TestCase):
    def test_empty_clears_conf(self):
        runner, calls = _runner_returning(0)
        result = ops.sysctl_apply("1.2.3.4", {}, ssh_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "cleared")
        self.assertEqual(len(calls), 1)
        _, cmd = calls[0]
        self.assertIn("rm -f", cmd)
        self.assertIn("99-lager-box-config.conf", cmd)
        self.assertIn("sysctl --system", cmd)

    def test_writes_keys_in_sorted_order(self):
        captured = []

        def runner(box_ip, cmd):
            captured.append(cmd)
            return 0, "", ""

        result = ops.sysctl_apply(
            "1.2.3.4",
            {"net.ipv4.ip_forward": "1", "kernel.shmmax": "68719476736"},
            ssh_runner=runner,
        )
        self.assertTrue(result.ok)
        # The injected runner sees a special encoded form when stdin data is
        # piped (see _run_with_stdin). The body is included so we can verify
        # ordering.
        self.assertEqual(len(captured), 1)
        encoded = captured[0]
        self.assertIn("__STDIN__", encoded)
        # Sorted by key: kernel.* then net.*
        self.assertLess(
            encoded.index("kernel.shmmax"),
            encoded.index("net.ipv4.ip_forward"),
        )
        self.assertIn("kernel.shmmax = 68719476736", encoded)
        self.assertIn("net.ipv4.ip_forward = 1", encoded)
        self.assertIn("tee", encoded)
        self.assertIn("sysctl --system", encoded)

    def test_sudo_failure_returns_bootstrap(self):
        def runner(box_ip, cmd):
            return 1, "", "sudo: a password is required"

        result = ops.sysctl_apply(
            "1.2.3.4", {"net.ipv4.ip_forward": "1"}, ssh_runner=runner,
        )
        self.assertFalse(result.ok)
        self.assertIn("passwordless sudo is not configured", result.message)
        self.assertIn("/etc/sudoers.d/lager-box-config", result.message)
        self.assertIsNotNone(result.manual_fix)


if __name__ == "__main__":
    unittest.main()

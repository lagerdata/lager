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

    def runner(box_ip, cmd, *, stdin=None):
        calls.append((box_ip, cmd, stdin))
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
        _, cmd, _ = calls[0]
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
        _, cmd, _ = calls[0]
        self.assertIn("'weird name'", cmd)


class SysctlApply(unittest.TestCase):
    def test_empty_clears_conf(self):
        runner, calls = _runner_returning(0)
        result = ops.sysctl_apply("1.2.3.4", {}, ssh_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "cleared")
        self.assertEqual(len(calls), 1)
        _, cmd, _ = calls[0]
        self.assertIn("rm -f", cmd)
        self.assertIn("99-lager-box-config.conf", cmd)
        self.assertIn("sysctl --system", cmd)

    def test_writes_keys_in_sorted_order(self):
        captured = []

        def runner(box_ip, cmd, *, stdin=None):
            captured.append((cmd, stdin))
            return 0, "", ""

        result = ops.sysctl_apply(
            "1.2.3.4",
            {"net.ipv4.ip_forward": "1", "kernel.shmmax": "68719476736"},
            ssh_runner=runner,
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(captured), 1)
        cmd, body = captured[0]
        self.assertIsNotNone(body)
        # Sorted by key: kernel.* then net.*
        self.assertLess(
            body.index("kernel.shmmax"),
            body.index("net.ipv4.ip_forward"),
        )
        self.assertIn("kernel.shmmax = 68719476736", body)
        self.assertIn("net.ipv4.ip_forward = 1", body)
        self.assertIn("tee", cmd)
        self.assertIn("sysctl --system", cmd)

    def test_sudo_failure_returns_bootstrap(self):
        def runner(box_ip, cmd, *, stdin=None):
            return 1, "", "sudo: a password is required"

        result = ops.sysctl_apply(
            "1.2.3.4", {"net.ipv4.ip_forward": "1"}, ssh_runner=runner,
        )
        self.assertFalse(result.ok)
        self.assertIn("passwordless sudo is not configured", result.message)
        self.assertIn("/etc/sudoers.d/lager-box-config", result.message)
        self.assertIsNotNone(result.manual_fix)


class RenderUdevRulesFile(unittest.TestCase):
    def test_permission_only(self):
        body = ops.render_udev_rules_file([{"vid": "1209", "pid": "0001", "mode": "0666"}])
        self.assertIn("# Managed by `lager box config udev`", body)
        self.assertIn('ATTRS{idVendor}=="1209"', body)
        self.assertIn('ATTRS{idProduct}=="0001"', body)
        self.assertIn('MODE="0666"', body)  # explicit mode override is honored
        self.assertIn('GROUP="lager"', body)
        self.assertNotIn("usbtmc/unbind", body)

    def test_default_mode_is_group_scoped(self):
        body = ops.render_udev_rules_file([{"vid": "1209", "pid": "0001"}])
        self.assertIn('MODE="0660"', body)
        self.assertIn('GROUP="lager"', body)

    def test_usbtmc_emits_unbind(self):
        body = ops.render_udev_rules_file(
            [{"vid": "1ab1", "pid": "0e11", "mode": "0660", "usbtmc": True}]
        )
        self.assertIn('MODE="0660"', body)
        self.assertIn("usbtmc/unbind", body)
        self.assertIn('DRIVER=="usbtmc"', body)

    def test_empty_is_header_only(self):
        body = ops.render_udev_rules_file([])
        self.assertEqual(body.strip(), "# Managed by `lager box config udev`; manual edits are overwritten on apply.")


class UdevApply(unittest.TestCase):
    def test_happy_path_installs_and_reloads(self):
        runner, calls = _runner_returning(0)
        result = ops.udev_apply(
            "1.2.3.4", [{"vid": "1209", "pid": "0001", "mode": "0666"}], ssh_runner=runner,
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "applied")
        self.assertEqual(len(calls), 1)
        _, cmd, stdin = calls[0]
        # File body is piped via stdin to tee — never interpolated into the cmd.
        self.assertIn("tee /tmp/99-lager-user.rules", cmd)
        self.assertIn('MODE="0666"', stdin)
        # cp/chmod/udevadm shapes must match the lagerdata-udev NOPASSWD specs:
        # absolute binary paths, cp into the directory (the glob's dest).
        self.assertIn("sudo -n /bin/cp /tmp/99-lager-user.rules /etc/udev/rules.d/", cmd)
        self.assertIn("sudo -n /bin/chmod 644 /etc/udev/rules.d/99-lager-user.rules", cmd)
        self.assertIn("sudo -n /usr/bin/udevadm control --reload-rules", cmd)
        self.assertIn("sudo -n /usr/bin/udevadm trigger", cmd)

    def test_empty_clears(self):
        runner, calls = _runner_returning(0)
        result = ops.udev_apply("1.2.3.4", [], ssh_runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "cleared")
        # Still cp's a (header-only) file — no `rm` permission is needed.
        _, cmd, _ = calls[0]
        self.assertIn("/bin/cp", cmd)

    def test_sudo_failure_returns_bootstrap(self):
        def runner(box_ip, cmd, *, stdin=None):
            return 1, "", "sudo: a password is required"

        result = ops.udev_apply(
            "1.2.3.4", [{"vid": "1209", "pid": "0001"}], ssh_runner=runner,
        )
        self.assertFalse(result.ok)
        self.assertIn("passwordless sudo is not configured", result.message)
        self.assertIn("/etc/sudoers.d/lagerdata-udev", result.message)
        self.assertIsNotNone(result.manual_fix)


if __name__ == "__main__":
    unittest.main()

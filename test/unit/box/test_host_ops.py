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


class BoxcfgSudoers(unittest.TestCase):
    # For the default user, the generated bootstrap command must stay
    # byte-identical to the command `lager install`/`lager update` shipped
    # before the rule content was centralized here (v0.31.2) — re-running
    # against an already-provisioned lagerdata box must be a no-op overwrite.
    LEGACY_CMD = (
        "printf '%s\\n' "
        "'lagerdata ALL=(root) NOPASSWD: SETENV: /usr/bin/apt-get' "
        "'lagerdata ALL=(root) NOPASSWD: /bin/mkdir, /bin/chown, "
        "/usr/sbin/sysctl --system, /sbin/sysctl --system, "
        "/usr/bin/tee /etc/sysctl.d/99-lager-box-config.conf, "
        "/bin/rm -f /etc/sysctl.d/99-lager-box-config.conf, "
        "/bin/cp /etc/lager/box_config.applied.json /etc/lager/box_config.json' "
        "| sudo tee /etc/sudoers.d/lager-box-config >/dev/null "
        "&& sudo chmod 440 /etc/sudoers.d/lager-box-config "
        "&& sudo touch /etc/lager/.boxcfg-sudoers-v2 "
        "&& sudo chmod 644 /etc/lager/.boxcfg-sudoers-v2"
    )

    def test_default_user_matches_legacy_command(self):
        self.assertEqual(ops.boxcfg_sudoers_bootstrap_cmd(), self.LEGACY_CMD)

    def test_rules_name_the_given_user(self):
        # The whole point of parameterizing: on a box whose login user isn't
        # `lagerdata`, the rule must grant that user, or `sudo -n apt-get`
        # never matches.
        rules = ops.boxcfg_sudoers_rules("benchtest")
        self.assertEqual(len(rules), 2)
        for rule in rules:
            self.assertTrue(rule.startswith("benchtest ALL=(root) NOPASSWD: "), rule)
        self.assertNotIn("lagerdata", " ".join(rules))

    def test_bootstrap_cmd_interpolates_user(self):
        cmd = ops.boxcfg_sudoers_bootstrap_cmd("benchtest")
        self.assertIn("'benchtest ALL=(root) NOPASSWD: SETENV: /usr/bin/apt-get'", cmd)
        self.assertNotIn("lagerdata", cmd)

    def test_rules_raise_on_invalid_user(self):
        # Backstop for executed sudoers content: install/update validate
        # before calling, so a raise here means a future caller forgot —
        # fail loudly instead of writing tainted root-owned sudoers.
        for user in ["", "a'b", "a\nb ALL=(ALL) NOPASSWD: ALL", "$(reboot)"]:
            with self.assertRaises(ValueError, msg=repr(user)):
                ops.boxcfg_sudoers_rules(user)

    def test_bootstrap_cmd_raises_on_invalid_user(self):
        with self.assertRaises(ValueError):
            ops.boxcfg_sudoers_bootstrap_cmd("a;b")


class UsernameValidation(unittest.TestCase):
    def test_accepts_valid_usernames(self):
        for user in ["lagerdata", "benchtest", "lab-2", "_svc", "a.b-c_d", "Host$"]:
            self.assertTrue(ops.is_valid_unix_username(user), user)

    def test_rejects_injection_and_junk(self):
        for user in [
            None,
            "",
            "bad user",                      # space splits sudoers fields
            "a'b",                           # would close the shell quote
            "a\nb ALL=(ALL) NOPASSWD: ALL",  # sudoers line injection
            "$(reboot)",
            "a;b",
            "-flag",
            "1starts-with-digit",
        ]:
            self.assertFalse(ops.is_valid_unix_username(user), repr(user))


class BootstrapTexts(unittest.TestCase):
    def test_sudoers_bootstrap_names_user(self):
        text = ops.sudoers_bootstrap("benchtest")
        self.assertIn("'benchtest ALL=(root) NOPASSWD: SETENV: /usr/bin/apt-get'", text)
        self.assertNotIn("lagerdata", text)

    def test_udev_bootstrap_names_user(self):
        text = ops.udev_sudoers_bootstrap("benchtest")
        self.assertIn(
            "benchtest ALL=(ALL) NOPASSWD: /bin/cp /tmp/*.rules /etc/udev/rules.d/", text
        )
        # The sudoers *filename* is historical and stays lagerdata-udev
        # (matching what setup_and_deploy_box.sh writes); only the rule
        # lines must name the login user.
        self.assertIn("/etc/sudoers.d/lagerdata-udev", text)

    def test_bootstrap_texts_fall_back_on_invalid_user(self):
        # These render inside error messages, so they must never raise and
        # never interpolate an unvalidated (box-storage) username into a
        # paste-into-root-shell snippet: invalid names fall back to the
        # historical default.
        evil = "a\nb ALL=(ALL) NOPASSWD: ALL"
        for renderer in [ops.sudoers_bootstrap, ops.udev_sudoers_bootstrap]:
            text = renderer(evil)
            self.assertNotIn(evil, text, renderer.__name__)
            self.assertIn("lagerdata", text, renderer.__name__)

    def test_failure_messages_carry_user(self):
        runner, _ = _runner_returning(1, stderr="sudo: a password is required")
        result = ops.apt_install("1.2.3.4", ["tcpdump"], ssh_runner=runner, user="benchtest")
        self.assertIn("benchtest ALL=(root)", result.message)
        result = ops.sysctl_apply("1.2.3.4", {"vm.dirty_ratio": "10"}, ssh_runner=runner, user="benchtest")
        self.assertIn("benchtest ALL=(root)", result.message)
        result = ops.udev_apply(
            "1.2.3.4", [{"vid": "1209", "pid": "0001"}], ssh_runner=runner, user="benchtest"
        )
        self.assertIn("benchtest ALL=(ALL)", result.message)


if __name__ == "__main__":
    unittest.main()

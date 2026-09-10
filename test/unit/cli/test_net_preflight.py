# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The host-networking pre-flight: cli/commands/box/_net_preflight.py.

This is the check that turns "switch to host networking" from an irreversible
mistake into a refused no-op, so its decision table is pinned here rather than
left to the hardware pass. Two conditions strand a box, and both are invisible
from the CLI host:

  * ufw stops being bypassed once the container leaves published ports, so the
    blanket `ufw deny <port>/tcp` starts applying to the operator's own route.
  * a port-publishing gateway already owns the ports the container would bind.

The ufw fixtures are real `ufw status` output from a production box, not
invented: that box allows 5000 on lo/docker0/tailscale0 and has no rule at all
for 9000, which is exactly the shape that made the check necessary.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from cli.commands.box._net_preflight import (
    CONTROL_PLANE_PORTS,
    PreflightResult,
    _port_allowed_on,
    _probe_source,
    evaluate,
)


# Verbatim from a provisioned box. Note 9000 has no rule of any kind.
UFW_REAL = """Status: active

To                         Action      From
--                         ------      ----
5000 on lo                 ALLOW       Anywhere                   # Lager service (localhost)
5000 on docker0            ALLOW       Anywhere                   # Lager service (Docker)
5000 on tailscale0         ALLOW       Anywhere                   # Lager service (Tailscale)
5000/tcp                   DENY        Anywhere                   # Lager service (block external)
8081:8090 on tailscale0    ALLOW       Anywhere                   # Lager service (Tailscale)
5000 (v6) on tailscale0    ALLOW       Anywhere (v6)              # Lager service (Tailscale)
"""


# The shape secure_box_firewall.sh actually leaves behind: per-interface allows,
# then a blanket deny for the same port.
UFW_WITH_DENY = """Status: active

To                         Action      From
--                         ------      ----
5000 on lo                 ALLOW       Anywhere                   # Lager service (localhost)
5000/tcp                   DENY        Anywhere                   # Lager service (block external)
9000 on lo                 ALLOW       Anywhere                   # Lager service (localhost)
9000/tcp                   DENY        Anywhere                   # Lager service (block external)
"""


# Allows appended after secure_box_firewall.sh's rules, which is where a plain
# `ufw allow` lands. ufw reaches the blanket deny first, so eth0 stays closed
# even though an allow for each port is listed.
UFW_APPENDED_ALLOWS = UFW_WITH_DENY + (
    "5000/tcp on eth0           ALLOW       Anywhere\n"
    "9000/tcp on eth0           ALLOW       Anywhere\n"
)


# The same two rules inserted at position 1, the form the remediation prints.
UFW_INSERTED_ALLOWS = UFW_WITH_DENY.replace(
    "--                         ------      ----\n",
    "--                         ------      ----\n"
    "5000/tcp on eth0           ALLOW       Anywhere                   # Lager service (eth0)\n"
    "9000/tcp on eth0           ALLOW       Anywhere                   # Lager service (eth0)\n",
)


def _result(**data):
    base = {
        "client_ip": "10.0.0.9", "iface": "eth0",
        "ufw_present": True, "ufw_readable": True, "ufw_active": False,
        "ufw_path": "/usr/sbin/ufw", "user": "benchtest",
        "ufw_status": "", "no_publish": False, "bound_ports": [],
        "publishers": {},
    }
    base.update(data)
    return PreflightResult(True, data=base)


def _normal_box(**data):
    """A box in its ordinary state: the lager container publishing its own
    ports. This is what the check has to stay quiet on, and its absence from
    the fixture set is why a false positive shipped."""
    base = dict(
        bound_ports=[22, 5000, 9000],
        publishers={str(p): ["lager"] for p in CONTROL_PLANE_PORTS},
    )
    base.update(data)
    return _result(**base)


class PortAllowedParsing(unittest.TestCase):
    def test_finds_an_interface_scoped_allow(self):
        self.assertTrue(_port_allowed_on(UFW_REAL, 5000, "tailscale0"))

    def test_a_port_with_no_rule_is_not_allowed(self):
        """The case that stranded a real box: 9000 has no rule, and ufw's
        default is deny incoming."""
        self.assertFalse(_port_allowed_on(UFW_REAL, 9000, "tailscale0"))

    def test_an_allow_on_a_different_interface_does_not_count(self):
        self.assertFalse(_port_allowed_on(UFW_REAL, 5000, "eth0"))

    def test_a_global_deny_is_not_read_as_an_allow(self):
        self.assertFalse(_port_allowed_on("5000/tcp DENY Anywhere", 5000, "eth0"))

    def test_a_range_covers_ports_inside_it(self):
        self.assertTrue(_port_allowed_on(UFW_REAL, 8085, "tailscale0"))
        self.assertFalse(_port_allowed_on(UFW_REAL, 8091, "tailscale0"))


class RuleOrder(unittest.TestCase):
    """ufw applies the first rule that matches. The parse used to count any
    allow for the port on the interface wherever it sat, so an allow listed
    behind the blanket deny read as open. That is the one case this check
    exists to catch, because it is where a plain `ufw allow` lands."""

    def test_an_allow_behind_the_blanket_deny_does_not_admit(self):
        self.assertFalse(_port_allowed_on(UFW_APPENDED_ALLOWS, 9000, "eth0"))

    def test_a_box_carrying_appended_allows_is_refused(self):
        r = evaluate(_normal_box(ufw_active=True, ufw_status=UFW_APPENDED_ALLOWS))
        self.assertFalse(r.ok)
        self.assertTrue(any("5000" in b and "9000" in b for b in r.blockers))

    def test_the_same_allows_ahead_of_the_deny_admit(self):
        self.assertTrue(_port_allowed_on(UFW_INSERTED_ALLOWS, 9000, "eth0"))
        self.assertTrue(
            evaluate(_normal_box(ufw_active=True, ufw_status=UFW_INSERTED_ALLOWS)).ok)

    def test_a_deny_on_another_interface_does_not_block(self):
        status = ("5000/tcp on wlan0          DENY        Anywhere\n"
                  "5000 on eth0               ALLOW       Anywhere\n")
        self.assertTrue(_port_allowed_on(status, 5000, "eth0"))

    def test_an_outbound_rule_does_not_govern_inbound(self):
        status = ("9000/tcp                   DENY OUT    Anywhere\n"
                  "9000/tcp on eth0           ALLOW IN    Anywhere\n")
        self.assertTrue(_port_allowed_on(status, 9000, "eth0"))

    def test_a_udp_rule_does_not_govern_tcp(self):
        status = ("9000/udp                   DENY        Anywhere\n"
                  "9000 on eth0               ALLOW       Anywhere\n")
        self.assertTrue(_port_allowed_on(status, 9000, "eth0"))

    def test_an_interface_wide_allow_admits_every_port(self):
        self.assertTrue(_port_allowed_on(
            "Anywhere on eth0           ALLOW       Anywhere\n", 9000, "eth0"))

    def test_a_port_list_is_read(self):
        status = "80,443,9000/tcp on eth0    ALLOW       Anywhere\n"
        self.assertTrue(_port_allowed_on(status, 9000, "eth0"))
        self.assertFalse(_port_allowed_on(status, 8000, "eth0"))


class RuleFamilyAndSource(unittest.TestCase):
    """A rule counts only for the traffic it covers: its address family and,
    where it names one, its source. Where that cannot be placed, an allow does
    not count and a deny still does."""

    V6_ALLOW = "9000 (v6) on eth0          ALLOW       Anywhere (v6)\n"
    SUBNET_ALLOW = "9000 on eth0               ALLOW       10.0.0.0/24\n"

    def test_an_ipv6_rule_does_not_admit_an_ipv4_client(self):
        self.assertFalse(
            _port_allowed_on(self.V6_ALLOW, 9000, "eth0", client_ip="10.0.0.9"))

    def test_an_ipv6_rule_governs_an_ipv6_client(self):
        self.assertTrue(
            _port_allowed_on(self.V6_ALLOW, 9000, "eth0", client_ip="fd00::9"))

    def test_an_allow_for_the_operators_subnet_admits(self):
        self.assertTrue(
            _port_allowed_on(self.SUBNET_ALLOW, 9000, "eth0", client_ip="10.0.0.9"))

    def test_an_allow_for_another_subnet_does_not(self):
        self.assertFalse(
            _port_allowed_on(self.SUBNET_ALLOW, 9000, "eth0", client_ip="192.168.1.5"))

    def test_a_source_limited_allow_does_not_count_without_a_client_address(self):
        self.assertFalse(_port_allowed_on(self.SUBNET_ALLOW, 9000, "eth0"))

    def test_a_deny_for_another_source_does_not_block(self):
        status = ("9000/tcp                   DENY        203.0.113.7\n"
                  "9000 on eth0               ALLOW       Anywhere\n")
        self.assertTrue(_port_allowed_on(status, 9000, "eth0", client_ip="10.0.0.9"))

    def test_a_deny_that_can_cover_the_operator_still_decides(self):
        status = ("9000/tcp                   DENY        203.0.113.0/24\n"
                  "9000 on eth0               ALLOW       Anywhere\n")
        self.assertFalse(_port_allowed_on(status, 9000, "eth0"))

    def test_a_destination_scoped_deny_for_another_port_is_ignored(self):
        status = ("10.0.0.1 22/tcp            DENY        Anywhere\n"
                  "9000 on eth0               ALLOW       Anywhere\n")
        self.assertTrue(_port_allowed_on(status, 9000, "eth0"))

    def test_the_verdict_uses_the_address_the_box_saw(self):
        status = "Status: active\n" + "".join(
            f"{p} on eth0  ALLOW  10.0.0.0/24\n" for p in CONTROL_PLANE_PORTS)
        self.assertTrue(evaluate(_result(
            ufw_active=True, ufw_status=status, client_ip="10.0.0.9")).ok)
        self.assertFalse(evaluate(_result(
            ufw_active=True, ufw_status=status, client_ip="192.168.1.5")).ok)


class FirewallVerdict(unittest.TestCase):
    def test_inactive_ufw_is_no_obstacle(self):
        self.assertTrue(evaluate(_result(ufw_active=False)).ok)

    def test_no_ufw_at_all_is_no_obstacle(self):
        self.assertTrue(evaluate(_result(ufw_present=False)).ok)

    def test_active_ufw_without_an_allow_refuses(self):
        r = evaluate(_result(ufw_active=True, ufw_status=UFW_REAL, iface="tailscale0"))
        self.assertFalse(r.ok)
        self.assertTrue(any("9000" in b for b in r.blockers))

    def test_the_remediation_is_interface_scoped_not_a_blanket_open(self):
        """Opening the port to every interface is a security posture change.
        The refusal must offer only the interface the operator already uses."""
        r = evaluate(_result(ufw_active=True, ufw_status=UFW_REAL, iface="tailscale0"))
        self.assertTrue(r.remediation)
        for cmd in r.remediation:
            self.assertIn("on tailscale0", cmd)
            self.assertNotIn("allow 9000", cmd)

    def test_the_remediation_inserts_ahead_of_the_blanket_deny(self):
        """secure_box_firewall.sh writes its allows first and a blanket
        `deny <port>/tcp` last, and ufw is first-match -- so an APPENDED allow
        lands after the deny and does nothing. Measured on a box: identical rule
        content at position 14 blocked, at position 10 reachable."""
        r = evaluate(_result(ufw_active=True, ufw_status=UFW_WITH_DENY, iface="eth0"))
        self.assertTrue(r.remediation)
        inserts = [c for c in r.remediation if "insert 1" in c]
        self.assertTrue(inserts, msg=r.remediation)
        for cmd in inserts:
            self.assertIn("ufw insert 1 allow in on eth0", cmd)

    def test_no_bare_append_form_is_emitted(self):
        """A plain `ufw allow ...` is the form that does not work here."""
        r = evaluate(_result(ufw_active=True, ufw_status=UFW_WITH_DENY, iface="eth0"))
        for cmd in r.remediation:
            if "allow" in cmd and "delete" not in cmd:
                self.assertIn("insert 1", cmd)

    def test_a_delete_precedes_each_insert(self):
        """ufw dedupes: an operator who already followed the old, broken advice
        has an appended allow, and `insert` then answers `Skipping inserting
        existing rule` and silently does nothing."""
        r = evaluate(_result(ufw_active=True, ufw_status=UFW_WITH_DENY, iface="eth0"))
        first_insert = next(i for i, c in enumerate(r.remediation) if "insert 1" in c)
        self.assertIn("delete", r.remediation[first_insert - 1])

    def test_the_expected_delete_noise_is_explained(self):
        r = evaluate(_result(ufw_active=True, ufw_status=UFW_WITH_DENY, iface="eth0"))
        self.assertTrue(any("non-existent" in n for n in r.notes))

    def test_a_fully_allowed_interface_passes(self):
        allowed = "Status: active\n" + "".join(
            f"{p} on wg0  ALLOW  Anywhere\n" for p in CONTROL_PLANE_PORTS)
        self.assertTrue(
            evaluate(_result(ufw_active=True, ufw_status=allowed, iface="wg0")).ok)

    def test_it_works_for_any_vpn_not_just_the_one_the_script_knows(self):
        """The firewall script hardcodes tailscale0 and takes one extra VPN by
        name. The pre-flight must not inherit that: it asks the box which
        interface this connection arrived on."""
        allowed = "Status: active\n" + "".join(
            f"{p} on ppp0  ALLOW  Anywhere\n" for p in CONTROL_PLANE_PORTS)
        self.assertTrue(
            evaluate(_result(ufw_active=True, ufw_status=allowed, iface="ppp0")).ok)

    def test_unreadable_ufw_refuses_rather_than_assuming_the_best(self):
        r = evaluate(_result(ufw_present=True, ufw_readable=False))
        self.assertFalse(r.ok)
        self.assertTrue(any("could not be read" in b for b in r.blockers))

    def test_an_undeterminable_interface_refuses(self):
        r = evaluate(_result(ufw_active=True, ufw_status=UFW_REAL, iface=""))
        self.assertFalse(r.ok)


_VISUDO = shutil.which("visudo") or (
    "/usr/sbin/visudo" if os.path.exists("/usr/sbin/visudo") else None
)


class UnreadableFirewall(unittest.TestCase):
    """Lager grants no `ufw status`: adding it to a sudoers file Lager owns would
    cost every box an interactive sudo prompt on its next update. So a box
    without passwordless sudo is refused, and the refusal names exactly what
    lets the check run -- a blocker with no way forward pushes people onto the
    override flag. It prints the grant line only. Which file the line goes in
    is the operator's call, and Lager names only the sudoers files it owns."""

    RULE = "benchtest ALL=(root) NOPASSWD: /usr/sbin/ufw status"

    def _refused(self, **data):
        r = evaluate(_result(ufw_present=True, ufw_readable=False, **data))
        self.assertFalse(r.ok)
        return r

    def test_the_refusal_names_the_command_that_needs_a_password(self):
        r = self._refused()
        self.assertTrue(
            any("sudo -n /usr/sbin/ufw status" in b for b in r.blockers), r.blockers)

    def test_it_prints_the_grant_line_for_the_login_user(self):
        self.assertIn(self.RULE, self._refused().notes)

    def test_it_says_to_add_the_line_with_visudo(self):
        text = " ".join(self._refused().notes)
        self.assertIn("visudo -f", text)
        self.assertIn("does not grant", text)

    def test_it_runs_nothing_and_names_no_sudoers_file(self):
        """The ownership contract: Lager writes and names only its own files
        under /etc/sudoers.d/ (test/unit/box/test_sudoers_contract.py)."""
        r = self._refused()
        self.assertEqual(r.remediation, [])
        text = " ".join(r.blockers + r.notes)
        self.assertEqual(re.findall(r"/etc/sudoers\.d/([A-Za-z0-9_.-]+)", text), [])

    def test_the_grant_names_the_path_the_box_reported(self):
        self.assertIn("benchtest ALL=(root) NOPASSWD: /sbin/ufw status",
                      self._refused(ufw_path="/sbin/ufw").notes)

    def test_an_unusual_path_is_not_printed_into_sudoers(self):
        notes = self._refused(ufw_path="/opt/ufw, ALL").notes
        self.assertIn(self.RULE, notes)
        self.assertFalse(any("/opt/ufw" in n for n in notes))

    def test_an_unusable_username_is_not_interpolated(self):
        notes = self._refused(user="a b;reboot").notes
        self.assertFalse(any("reboot" in n for n in notes))
        self.assertIn("<box-user> ALL=(root) NOPASSWD: /usr/sbin/ufw status", notes)

    @unittest.skipUnless(_VISUDO, "visudo not available on this machine")
    def test_the_printed_grant_passes_visudo(self):
        rule = next(n for n in self._refused().notes if "NOPASSWD" in n)
        self.assertEqual(rule, self.RULE)
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(rule + "\n")
            path = f.name
        try:
            proc = subprocess.run(
                [str(_VISUDO), "-c", "-f", path], capture_output=True, text=True)
        finally:
            os.unlink(path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class ProbeFindsUfw(unittest.TestCase):
    def test_the_probe_is_valid_python(self):
        """It is a string until it runs on the box, so nothing else would catch
        a syntax error before hardware does."""
        compile(_probe_source(), "<net-preflight-probe>", "exec")

    def test_it_asks_sudo_for_ufw_by_path(self):
        """sudoers matches a command by its path, so the probe runs the path a
        grant names rather than a bare `ufw` resolved through PATH."""
        src = _probe_source()
        self.assertIn("sudo -n %s status", src)
        self.assertNotIn("sudo -n ufw status", src)

    def test_it_looks_past_a_path_without_sbin(self):
        """A non-interactive SSH session can have a PATH without /usr/sbin, and
        a missed ufw would read as "no firewall" and pass."""
        src = _probe_source()
        self.assertIn("/usr/sbin/ufw", src)
        self.assertIn("/sbin/ufw", src)


class GatewayCollision(unittest.TestCase):
    """A port is only taken if something OTHER than the lager container holds
    it. The first version compared against every listening port, so the lager
    container's own published 5000 and 9000 made it refuse every normal box --
    and with no remediation, which pushed operators onto the override flag and
    defeated the guard entirely."""

    def test_a_normal_box_passes(self):
        """The case that was missing. The lager container publishes 5000 and
        9000 on any ordinary box; `apply` stops it before starting the
        replacement, so those ports are its own and about to be freed."""
        self.assertTrue(evaluate(_normal_box()).ok)

    def test_no_publish_marker_refuses(self):
        r = evaluate(_normal_box(no_publish=True))
        self.assertFalse(r.ok)
        self.assertTrue(any("gateway" in b for b in r.blockers))

    def test_another_container_holding_a_port_refuses(self):
        r = evaluate(_result(
            bound_ports=[22, 5000, 9000],
            publishers={"5000": ["stout-gateway"], "9000": ["stout-gateway"]}))
        self.assertFalse(r.ok)
        self.assertTrue(any("5000" in b and "9000" in b for b in r.blockers))

    def test_a_port_bound_by_a_non_docker_process_refuses(self):
        """Bound, but no container publishes it -- a host process. It will
        still be there after the container stops."""
        r = evaluate(_result(bound_ports=[9000], publishers={"9000": []}))
        self.assertFalse(r.ok)
        self.assertTrue(any("9000" in b for b in r.blockers))

    def test_a_mixed_box_names_only_the_contended_port(self):
        r = evaluate(_result(
            bound_ports=[5000, 9000],
            publishers={"5000": ["lager"], "9000": ["stout-gateway"]}))
        self.assertFalse(r.ok)
        self.assertTrue(any("9000" in b and "5000" not in b for b in r.blockers))

    def test_unrelated_bound_ports_are_ignored(self):
        self.assertTrue(evaluate(_result(bound_ports=[22, 53, 8472])).ok)

    def test_a_gateway_blocker_still_tells_the_operator_what_to_do(self):
        """No firewall rule fixes a gateway conflict, but a blocker with no way
        forward is exactly what drives people to --skip-host-network-check."""
        r = evaluate(_normal_box(no_publish=True))
        self.assertEqual(r.remediation, [], "no shell command can fix this")
        self.assertTrue(r.notes, "must still say what the options are")
        self.assertTrue(any("lagernet" in n for n in r.notes))


class ProbeFailure(unittest.TestCase):
    def test_an_unprobeable_box_refuses(self):
        r = evaluate(PreflightResult(False, error="ssh timed out"))
        self.assertFalse(r.ok)
        self.assertTrue(any("ssh timed out" in b for b in r.blockers))

    def test_both_conditions_are_reported_together(self):
        """An operator fixing one blocker should already know about the other
        rather than discovering it on the next attempt."""
        r = evaluate(_normal_box(
            ufw_active=True, ufw_status=UFW_REAL, iface="tailscale0",
            no_publish=True))
        self.assertEqual(len(r.blockers), 2)


if __name__ == "__main__":
    unittest.main()

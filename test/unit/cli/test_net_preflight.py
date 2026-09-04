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

import unittest

from cli.commands.box._net_preflight import (
    CONTROL_PLANE_PORTS,
    PreflightResult,
    _port_allowed_on,
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


def _result(**data):
    base = {
        "client_ip": "10.0.0.9", "iface": "eth0",
        "ufw_present": True, "ufw_readable": True, "ufw_active": False,
        "ufw_status": "", "no_publish": False, "bound_ports": [],
    }
    base.update(data)
    return PreflightResult(True, data=base)


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
            self.assertIn("allow in on tailscale0", cmd)
            self.assertNotIn("allow 9000", cmd)

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


class GatewayCollision(unittest.TestCase):
    def test_no_publish_marker_refuses(self):
        r = evaluate(_result(no_publish=True))
        self.assertFalse(r.ok)
        self.assertTrue(any("gateway" in b for b in r.blockers))

    def test_a_bound_control_port_refuses(self):
        """Under host networking the container binds these itself, so anything
        already listening is an EADDRINUSE waiting to happen."""
        r = evaluate(_result(bound_ports=[22, 5000, 9000]))
        self.assertFalse(r.ok)
        self.assertTrue(any("5000" in b and "9000" in b for b in r.blockers))

    def test_unrelated_bound_ports_are_ignored(self):
        self.assertTrue(evaluate(_result(bound_ports=[22, 53, 8472])).ok)

    def test_no_remediation_is_offered_for_a_gateway(self):
        """A firewall rule can be added; a gateway owning the port cannot be
        fixed by one, so offering a command would be misleading."""
        self.assertEqual(evaluate(_result(no_publish=True)).remediation, [])


class ProbeFailure(unittest.TestCase):
    def test_an_unprobeable_box_refuses(self):
        r = evaluate(PreflightResult(False, error="ssh timed out"))
        self.assertFalse(r.ok)
        self.assertTrue(any("ssh timed out" in b for b in r.blockers))

    def test_both_conditions_are_reported_together(self):
        """An operator fixing one blocker should already know about the other
        rather than discovering it on the next attempt."""
        r = evaluate(_result(
            ufw_active=True, ufw_status=UFW_REAL, iface="tailscale0",
            no_publish=True))
        self.assertEqual(len(r.blockers), 2)


if __name__ == "__main__":
    unittest.main()

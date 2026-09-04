# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Pre-flight for switching a box's container to host networking.

Why this exists: `network-mode set host` + `apply` made a box unreachable to
the CLI, and the documented undo could not recover it, because the undo travels
the same route the switch had just closed. Recovery needed SSH and a hand-edit.

Two independent conditions do that, and both are invisible from the CLI host:

1. **The host firewall starts applying.** `secure_box_firewall.sh` allows the
   Lager ports per interface -- `lo`, `docker0`, optionally `tailscale0`, and an
   operator-named `--corporate-vpn IFACE` -- and then writes a blanket
   `ufw deny <port>/tcp`. While the container published its ports, Docker's DNAT
   put those packets through FORWARD and ufw's INPUT rules never saw them, so
   none of that mattered. Host networking removes the DNAT, the deny rule starts
   applying, and the operator's own route is cut. The script is run by
   `lager install` by default, so most boxes carry these rules.

2. **A port-publishing gateway already owns the ports.** On a box fronted by a
   gateway container, `/etc/lager/no_publish` is set and the gateway publishes
   5000/8080/9000 and friends on the host. Under host networking the lager
   container binds those directly, hits EADDRINUSE, and its control plane never
   starts. Verified on a production box.

The interface that matters is the one carrying the operator's own traffic, not
a guessed "primary" NIC. The box can read it straight off the live SSH
connection, so this asks rather than guesses -- which also means it works for
any VPN, not just the one the firewall script happens to know by name.

Everything here refuses; nothing opens a port. Whether Lager's control plane
should be reachable from a LAN is a policy question that is tracked separately,
and a BLE change is not the place to answer it.
"""
from __future__ import annotations

import json
from typing import List, Optional

# The two ports the CLI itself needs to keep talking to a box: 5000 is the
# Python execution service every box_config verb travels over, 9000 the net/HTTP
# API. Losing either strands the box. The other Lager ports matter to features
# rather than to reachability, and keeping this list short keeps the refusal
# specific -- the firewall script owns the full set.
CONTROL_PLANE_PORTS = (5000, 9000)

# Emitted on the box; prints one JSON object. python3 is always present (the
# container image is python-based and the host runs the deploy scripts with it).
# The container whose published ports are its own and therefore not a conflict.
LAGER_CONTAINER = "lager"

_PROBE_TEMPLATE = r"""
import json, os, subprocess

def sh(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return 1, "", str(e)

out = {}

# The interface carrying this very connection is the one that must stay open.
conn = os.environ.get("SSH_CONNECTION", "")
client = conn.split()[0] if conn else ""
out["client_ip"] = client
out["iface"] = ""
if client:
    rc, so, _ = sh("ip route get %s" % client)
    if rc == 0:
        parts = so.split()
        if "dev" in parts:
            out["iface"] = parts[parts.index("dev") + 1]

rc, so, _ = sh("command -v ufw")
out["ufw_present"] = (rc == 0 and bool(so.strip()))
out["ufw_active"] = False
out["ufw_readable"] = False
out["ufw_status"] = ""
if out["ufw_present"]:
    rc, so, _ = sh("sudo -n ufw status 2>/dev/null")
    if rc == 0 and so.strip():
        out["ufw_readable"] = True
        out["ufw_status"] = so
        out["ufw_active"] = ("status: active" in so.lower())

out["no_publish"] = os.path.exists("/etc/lager/no_publish")

# Which container, if any, publishes each control port. A port the lager
# container publishes itself is NOT a conflict: apply stops the old container
# before starting the new one, so that port is about to be freed. Without this
# attribution the check fires on every normal box, because docker-proxy binds
# 5000 and 9000 there as a matter of course.
out["publishers"] = {}
for _p in __PORTS__:
    rc, so, _ = sh("docker ps --filter publish=%d --format '{{.Names}}'" % _p)
    out["publishers"][str(_p)] = (
        [n.strip() for n in so.splitlines() if n.strip()] if rc == 0 else []
    )

# Host ports already bound. Under host networking the container binds these
# itself, so anything already here is a collision.
bound = set()
rc, so, _ = sh("ss -ltnH 2>/dev/null || ss -ltn 2>/dev/null")
if rc == 0:
    for line in so.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        local = cols[3]
        if ":" not in local:
            continue
        port = local.rsplit(":", 1)[1]
        if port.isdigit():
            bound.add(int(port))
out["bound_ports"] = sorted(bound)

print(json.dumps(out))
"""


def _probe_source(ports=CONTROL_PLANE_PORTS) -> str:
    return _PROBE_TEMPLATE.replace("__PORTS__", repr(list(ports)))


class PreflightResult:
    """What the box reported, plus the verdict.

    `blockers` non-empty means refuse. `remediation` is what the operator can
    run to clear the firewall blocker; it is printed, never executed.
    """

    def __init__(self, probed: bool, data: Optional[dict] = None,
                 error: str = "") -> None:
        self.probed = probed
        self.data = data or {}
        self.error = error
        self.blockers: List[str] = []
        self.remediation: List[str] = []
        # Advice that is not a pasteable command. A gateway conflict cannot be
        # fixed by a firewall rule, but the operator still needs to be told what
        # their options are -- a blocker with no way forward is what pushes
        # people onto the override flag.
        self.notes: List[str] = []

    @property
    def ok(self) -> bool:
        return not self.blockers

    @property
    def iface(self) -> str:
        return self.data.get("iface") or ""


def probe(box_ip: str, *, runner=None, timeout: int = 30,
          ports=CONTROL_PLANE_PORTS) -> PreflightResult:
    """Gather the box-side facts. Never raises."""
    from ._ssh import default_ssh_runner

    run = runner or default_ssh_runner
    try:
        rc, stdout, stderr = run(box_ip, "python3 -",
                                 stdin=_probe_source(ports), timeout=timeout)
    except Exception as e:  # transport blew up in a way the runner didn't map
        return PreflightResult(False, error=str(e))
    if rc != 0:
        return PreflightResult(False, error=(stderr or stdout or "").strip()[:300])
    try:
        return PreflightResult(True, data=json.loads(stdout.strip().splitlines()[-1]))
    except Exception:
        return PreflightResult(False, error=f"unparseable probe output: {stdout!r}"[:300])


def _port_allowed_on(status_text: str, port: int, iface: str) -> bool:
    """True if `ufw status` shows an ALLOW for `port` on `iface`.

    ufw renders an interface-scoped rule as `5000/tcp on tailscale0  ALLOW IN`.
    Matching is deliberately narrow: a rule that is not clearly an allow for
    this port on this interface does not count, because the cost of a false
    'allowed' is a box the operator can no longer reach.
    """
    needle_iface = f" on {iface}"
    for line in status_text.splitlines():
        if "ALLOW" not in line.upper():
            continue
        if needle_iface not in line:
            continue
        head = line.split()[0]
        spec = head.split("/")[0]
        if spec == str(port):
            return True
        if ":" in spec:  # a range, e.g. 8081:8090
            lo, _, hi = spec.partition(":")
            if lo.isdigit() and hi.isdigit() and int(lo) <= port <= int(hi):
                return True
    return False


def evaluate(result: PreflightResult, *, ports=CONTROL_PLANE_PORTS) -> PreflightResult:
    """Fill in blockers/remediation. Returns the same object for chaining."""
    if not result.probed:
        result.blockers.append(
            "Cannot check the box's firewall and port state over SSH "
            f"({result.error or 'no detail'}). Host networking can make the box "
            "unreachable, and this check is what makes that recoverable."
        )
        return result

    d = result.data

    # 1. Something other than the lager container already owns the ports.
    bound = set(d.get("bound_ports") or [])
    publishers = d.get("publishers") or {}
    collisions = []
    for port in ports:
        if port not in bound:
            continue
        owners = publishers.get(str(port)) or []
        if owners and all(o == LAGER_CONTAINER for o in owners):
            # Our own published port. `apply` stops this container before
            # starting the replacement, so the port is about to be freed --
            # treating it as taken refuses every normal box.
            continue
        collisions.append(port)

    if d.get("no_publish") or collisions:
        detail = (
            "ports held by something else: "
            + ", ".join(str(p) for p in collisions)
            if collisions else "/etc/lager/no_publish is set"
        )
        result.blockers.append(
            "This box is fronted by a port-publishing gateway "
            f"({detail}). On host networking the lager container binds those "
            "ports itself and will fail to start. Host mode and a publishing "
            "gateway cannot both own the same ports."
        )
        result.notes.append(
            "No firewall rule fixes this. Either leave this box on lagernet, or "
            "move the gateway off the Lager ports first."
        )

    # 2. The firewall cuts the operator's own route.
    if d.get("ufw_present") and not d.get("ufw_readable"):
        result.blockers.append(
            "ufw is installed but its status could not be read (needs "
            "passwordless sudo). Cannot confirm the box stays reachable."
        )
    elif d.get("ufw_active"):
        iface = result.iface
        if not iface:
            result.blockers.append(
                "ufw is active but the interface carrying this connection could "
                "not be determined, so the ports that must stay open are unknown."
            )
        else:
            status = d.get("ufw_status") or ""
            missing = [p for p in ports if not _port_allowed_on(status, p, iface)]
            if missing:
                result.blockers.append(
                    f"ufw is active and does not admit {', '.join(str(p) for p in missing)} "
                    f"on {iface}, the interface you reach this box on. Published "
                    "ports bypass ufw; host networking does not, so applying this "
                    "cuts your own route to the box."
                )
                # `insert 1`, not a plain allow. secure_box_firewall.sh writes
                # its per-interface allows first and a blanket `deny <port>/tcp`
                # last, and ufw is first-match -- so an APPENDED allow lands
                # after that deny and does nothing. Measured: identical rule
                # content at position 14 blocked, at position 10 reachable.
                #
                # Position 1 is ahead of the deny whatever else the box has, so
                # these survive being pasted in any order; several inserts at 1
                # just stack, all still ahead of the deny. The delete clears any
                # earlier appended attempt, because ufw dedupes and would
                # otherwise answer `Skipping inserting existing rule`.
                result.remediation = []
                for p in missing:
                    result.remediation.append(
                        f"sudo ufw --force delete allow in on {iface} "
                        f"to any port {p} proto tcp"
                    )
                    result.remediation.append(
                        f"sudo ufw insert 1 allow in on {iface} "
                        f"to any port {p} proto tcp "
                        f"comment 'Lager service ({iface})'"
                    )
                result.notes.append(
                    "The delete lines report 'Could not delete non-existent rule' "
                    "when there was no earlier attempt. That is expected."
                )
    return result


def check(box_ip: str, *, runner=None, ports=CONTROL_PLANE_PORTS) -> PreflightResult:
    return evaluate(probe(box_ip, runner=runner), ports=ports)

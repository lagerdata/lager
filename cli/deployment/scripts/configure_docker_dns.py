#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Choose usable upstream resolvers for Docker's container DNS.

Runs on the box under the system python3, so stdlib only.

Docker validates every entry of daemon.json's "dns" list as a bare IP address and
refuses to start -- not warn, not skip the entry -- if one of them does not parse.
A resolver learned over IPv6 router advertisement is a link-local address carrying
a zone id (``fe80::1%3``), which does not parse. Writing one out therefore takes
the daemon down, and it stays down across reboots, because daemon.json is
persistent.

Link-local resolvers are dropped rather than stripped of their zone: a container's
network namespace cannot reach the host's link-local scope, so such an address is
useless to the containers this list exists to serve.
"""

import argparse
import ipaddress
import json
import os

FALLBACKS = ("1.1.1.1", "8.8.8.8")
MAX_UPSTREAM = 3
DEFAULT_RESOLV_CONF = "/run/systemd/resolve/resolv.conf"
DEFAULT_DAEMON_JSON = "/etc/docker/daemon.json"


def rejection_reason(value):
    """Why `value` is unusable as a Docker DNS server, or None if it is usable."""
    address = value.split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "not an IP address"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    return None


def _nameservers(text):
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            yield fields[1]


def usable_resolvers(text, max_upstream=MAX_UPSTREAM):
    """Usable resolvers from resolv.conf `text`, in order, deduplicated."""
    servers = []
    for value in _nameservers(text):
        if rejection_reason(value) or value in servers:
            continue
        servers.append(value)
        if len(servers) == max_upstream:
            break
    return servers


def rejected_resolvers(text):
    """(value, reason) pairs for every nameserver we refuse to hand to Docker."""
    return [(v, rejection_reason(v)) for v in _nameservers(text) if rejection_reason(v)]


def merge_dns(cfg, servers):
    """Return `cfg` with "dns" set, preserving every operator-set key."""
    dns = list(servers)
    for fallback in FALLBACKS:
        if fallback not in dns:
            dns.append(fallback)
    merged = dict(cfg)
    merged["dns"] = dns
    return merged


def load_daemon_json(path):
    """Existing daemon.json, or {} if it is absent, unreadable, or not an object."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        # Docker cannot parse it either, so there is nothing worth preserving.
        return {}
    # Valid JSON that isn't an object (a list, say) is not a config Docker can use,
    # and would break the merge. Same treatment as unparseable.
    return cfg if isinstance(cfg, dict) else {}


def main():
    parser = argparse.ArgumentParser(description="Build Docker's daemon.json DNS config")
    parser.add_argument("--resolv-conf", default=DEFAULT_RESOLV_CONF)
    parser.add_argument("--daemon-json", default=DEFAULT_DAEMON_JSON)
    parser.add_argument("--out", required=True, help="where to write the merged daemon.json")
    args = parser.parse_args()

    try:
        with open(args.resolv_conf) as fh:
            resolv = fh.read()
    except OSError:
        resolv = ""

    cfg = merge_dns(load_daemon_json(args.daemon_json), usable_resolvers(resolv))

    with open(args.out, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")

    for value, reason in rejected_resolvers(resolv):
        print("Ignoring unusable resolver {} ({})".format(value, reason))
    print("Docker container DNS ->", ", ".join(cfg["dns"]))


if __name__ == "__main__":
    main()

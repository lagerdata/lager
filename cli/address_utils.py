# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Validation helpers for Lager box network addresses.

A box address may be one of:

* An IPv4 address (``192.168.1.100``, ``10.0.0.1``)
* An IPv6 address (``2001:db8::1``, ``fe80::1``)
* A Tailscale IP (``100.x.x.x``)
* A DNS hostname (``my-box``, ``box.example.com``, ``box-1.tailXYZ.ts.net``)

The rest of the CLI talks to boxes by appending its own ``:port`` and
request paths (``http://{addr}:5000/...``) — so the validator rejects any
input that already contains a scheme, port, or path, to keep that
composition unambiguous.

Hostnames are validated per RFC 1123 (DNS): dot-separated labels of 1-63
alphanumeric/hyphen characters with no leading or trailing hyphen,
total length ≤ 253 characters. Single-label names are allowed for
Tailscale MagicDNS short names.
"""

from __future__ import annotations

import ipaddress
import re

# RFC 1123 label: 1-63 chars, alphanumeric and hyphens, no leading/trailing
# hyphen. Allow single-character labels (e.g. ``a.example.com``).
_HOSTNAME_LABEL_RE = re.compile(
    r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$"
)

# User-facing cheatsheet printed after a validation failure. Single source of
# truth so the four CLI call sites can't drift out of sync. Indented two
# spaces to match the surrounding "Error: ..." lines.
VALID_FORMATS_CHEATSHEET: tuple[str, ...] = (
    "Valid formats:",
    "  IPv4: 192.168.1.100, 10.0.0.1",
    "  IPv6: 2001:db8::1, fe80::1",
    "  Tailscale: 100.x.x.x (get from 'tailscale status')",
    "  Hostname: my-box, box.example.com, box-1.tailXYZ.ts.net",
)


def validate_ip_or_hostname(value: str | None) -> str:
    """Return ``value`` (stripped) if it's a valid IP address or DNS hostname.

    Raises :class:`ValueError` with a user-facing message otherwise.
    """
    if value is None or not value.strip():
        raise ValueError("address cannot be empty")

    addr = value.strip()

    try:
        ipaddress.ip_address(addr)
        return addr
    except ValueError:
        pass

    if "://" in addr:
        raise ValueError(
            f"'{addr}' looks like a URL — pass a bare hostname or IP without the scheme"
        )
    if "/" in addr:
        raise ValueError(
            f"'{addr}' contains a path — pass a bare hostname or IP without paths"
        )
    if ":" in addr:
        # Multiple colons or brackets => the user is attempting IPv6 syntax,
        # but ip_address() above already accepted every well-formed bare IPv6,
        # so this is malformed, bracketed, or bracket-with-port. The user
        # advice is the same in all three cases: drop brackets/port.
        if addr.count(":") > 1 or "[" in addr or "]" in addr:
            raise ValueError(
                f"'{addr}' is not a valid IPv6 address — pass a bare address without brackets or port"
            )
        raise ValueError(
            f"'{addr}' contains a port — pass a bare hostname or IP; the CLI appends its own port"
        )

    if len(addr) > 253:
        raise ValueError(
            f"'{addr}' is too long for a hostname (max 253 characters)"
        )

    labels = addr.split(".")
    for label in labels:
        if not label:
            raise ValueError(
                f"'{addr}' has an empty label (consecutive dots or a leading/trailing dot)"
            )
        if not _HOSTNAME_LABEL_RE.match(label):
            raise ValueError(
                f"'{addr}' contains an invalid hostname label '{label}' "
                "(labels must be 1-63 alphanumeric or hyphen characters, "
                "no leading or trailing hyphen)"
            )

    return addr

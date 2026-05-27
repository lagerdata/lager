#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``cli.address_utils.validate_ip_or_hostname``.

Pins the contract that box addresses accept any of:
* IPv4 / IPv6 / Tailscale IPs (the historical behaviour)
* DNS hostnames (added so a Lager box can sit behind a hostname like
  ``box.example.com`` or a Tailscale MagicDNS name)

…and reject anything that already carries a scheme, port, or path,
since the rest of the CLI composes ``http://{addr}:port/...`` itself.
"""

from __future__ import annotations

import os
import sys

import pytest

# Add cli/ to path so the import resolves from a checkout without
# editable install — matches sibling test files.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _REPO_ROOT)

from cli.address_utils import validate_ip_or_hostname  # noqa: E402


@pytest.mark.parametrize(
    "value",
    [
        "192.168.1.100",
        "10.0.0.1",
        "127.0.0.1",
        "100.64.1.42",         # Tailscale CGNAT range
        "2001:db8::1",
        "fe80::1",
        "::1",
    ],
)
def test_accepts_ip_addresses(value):
    assert validate_ip_or_hostname(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "my-box",                          # single-label MagicDNS
        "lager-demo",
        "box.example.com",
        "box-1.tailXYZ.ts.net",
        "a.b.c.d.example.com",             # multiple labels
        "x",                               # single character single label
        "a1b2c3.example",                  # mixed alphanumeric
    ],
)
def test_accepts_hostnames(value):
    assert validate_ip_or_hostname(value) == value


def test_strips_surrounding_whitespace():
    assert validate_ip_or_hostname("  10.0.0.1  ") == "10.0.0.1"
    assert validate_ip_or_hostname("  box.example.com\n") == "box.example.com"


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_rejects_empty(value):
    with pytest.raises(ValueError, match="empty"):
        validate_ip_or_hostname(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://box.example.com",
        "https://box.example.com",
        "ws://box.example.com",
    ],
)
def test_rejects_urls_with_scheme(value):
    with pytest.raises(ValueError, match="URL"):
        validate_ip_or_hostname(value)


def test_rejects_paths():
    with pytest.raises(ValueError, match="path"):
        validate_ip_or_hostname("box.example.com/api")


def test_rejects_explicit_ports():
    # We accept bare IPv6 ("::1") via ip_address parsing, but reject
    # anything else with a ":" because the CLI appends its own port.
    with pytest.raises(ValueError, match="port"):
        validate_ip_or_hostname("box.example.com:5000")
    with pytest.raises(ValueError, match="port"):
        validate_ip_or_hostname("box-1:9000")


@pytest.mark.parametrize(
    "value",
    [
        "-leading-hyphen.com",
        "trailing-hyphen-.com",
        "label..double-dot.com",
        ".leading-dot.com",
        "trailing-dot.com.",
        "spaces in name.com",
        "underscore_label.com",            # underscores are RFC-1123-invalid
        "exclaim!.com",
        "a" * 64 + ".com",                 # label > 63 chars
    ],
)
def test_rejects_malformed_hostnames(value):
    with pytest.raises(ValueError):
        validate_ip_or_hostname(value)


def test_rejects_overly_long_hostname():
    # 254 chars total → too long
    too_long = ("a" * 60 + ".") * 4 + "abcdefghij"
    assert len(too_long) > 253
    with pytest.raises(ValueError, match="too long"):
        validate_ip_or_hostname(too_long)


@pytest.mark.parametrize(
    "value",
    [
        "2001:db8::g",        # malformed IPv6 (invalid hexit)
        "2001:db8:::1",       # malformed IPv6 (triple-colon)
        "[2001:db8::1]",      # bracketed IPv6 (legal in URLs, not here)
        "[2001:db8::1]:5000", # bracketed IPv6 with port
        "[::1]",              # bracketed loopback
    ],
)
def test_rejects_ipv6_shaped_with_precise_error(value):
    """Anything that *looks* like IPv6 but isn't a bare valid address should
    say so explicitly instead of falling into the generic 'contains a port'
    branch. The user advice is identical for all three sub-cases (malformed,
    bracketed, bracketed-with-port), so they share one message."""
    with pytest.raises(ValueError, match="not a valid IPv6 address"):
        validate_ip_or_hostname(value)


def test_single_colon_host_port_still_reports_port():
    # Non-IPv6-shaped "host:port" must keep the original port-specific message
    # so we don't regress users typing a stray port on a hostname.
    with pytest.raises(ValueError, match="contains a port"):
        validate_ip_or_hostname("box.example.com:5000")
    with pytest.raises(ValueError, match="contains a port"):
        validate_ip_or_hostname("box-1:9000")

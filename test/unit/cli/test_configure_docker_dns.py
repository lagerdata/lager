#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``cli.deployment.scripts.configure_docker_dns``.

Docker validates every entry of daemon.json's ``dns`` list as a bare IP address and
refuses to start if one does not parse. The install step that populates that list
reads the box's resolvers straight out of systemd-resolved's resolv.conf, so
whatever the box's network hands it ends up in front of the daemon.

On a network using IPv6 router advertisement, that includes a link-local resolver
carrying a zone id (``fe80::1%3``). Writing one out stopped Docker from starting at
all -- and, because daemon.json is persistent, kept it from starting on every
subsequent boot. These tests pin what the filter accepts and, above all, that
everything it emits is something Docker can parse.
"""

import ipaddress
import json

import pytest

from cli.deployment.scripts.configure_docker_dns import (
    FALLBACKS,
    load_daemon_json,
    merge_dns,
    rejected_resolvers,
    rejection_reason,
    usable_resolvers,
)


def test_link_local_resolver_is_dropped_and_real_one_kept():
    """The exact resolv.conf shape that took a box's Docker daemon down."""
    resolv = "nameserver 192.168.100.1\nnameserver fe80::1%3\n"

    assert usable_resolvers(resolv) == ["192.168.100.1"]
    assert merge_dns({}, usable_resolvers(resolv))["dns"] == [
        "192.168.100.1",
        "1.1.1.1",
        "8.8.8.8",
    ]


@pytest.mark.parametrize(
    "value, reason",
    [
        ("fe80::1%3", "link-local"),          # zone id as an interface index
        ("fe80::1%enp3s0", "link-local"),     # zone id as an interface name
        ("fe80::1", "link-local"),            # parses, but unreachable from a container
        ("169.254.1.1", "link-local"),        # IPv4 link-local
        ("127.0.0.53", "loopback"),           # the systemd-resolved stub
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("0.0.0.0", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("not-an-ip", "not an IP address"),
        ("", "not an IP address"),
    ],
)
def test_unusable_resolvers_are_rejected_with_a_reason(value, reason):
    assert rejection_reason(value) == reason


@pytest.mark.parametrize("value", ["192.168.100.1", "8.8.4.4", "2606:4700:4700::1111"])
def test_routable_resolvers_are_kept(value):
    assert rejection_reason(value) is None
    assert usable_resolvers("nameserver {}\n".format(value)) == [value]


def test_every_emitted_server_is_parseable_by_docker():
    """The invariant that matters: nothing we emit can stop the daemon starting.

    Docker parses each ``dns`` entry with the equivalent of ``ip_address()`` and
    refuses to start if any one of them fails. Feed the filter a resolv.conf full
    of things that break it and assert the output is still entirely clean.
    """
    resolv = "\n".join(
        [
            "nameserver fe80::1%3",
            "nameserver 127.0.0.53",
            "nameserver 169.254.1.1",
            "nameserver garbage",
            "nameserver 192.168.1.1",
            "search example.com",
            "options edns0",
        ]
    )

    dns = merge_dns({}, usable_resolvers(resolv))["dns"]

    assert dns == ["192.168.1.1", "1.1.1.1", "8.8.8.8"]
    for server in dns:
        ipaddress.ip_address(server)  # raises ValueError if Docker would reject it


def test_resolvers_are_capped_and_deduplicated():
    resolv = "".join(
        "nameserver {}\n".format(ip)
        for ip in ["10.0.0.1", "10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"]
    )

    assert usable_resolvers(resolv) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_no_usable_resolvers_falls_back_to_public_ones():
    """A box whose resolv.conf yields nothing still gets a working container DNS."""
    resolv = "nameserver 127.0.0.53\nnameserver fe80::1%3\n"

    assert merge_dns({}, usable_resolvers(resolv))["dns"] == list(FALLBACKS)
    assert merge_dns({}, usable_resolvers(""))["dns"] == list(FALLBACKS)


def test_fallbacks_are_not_duplicated_when_already_upstream():
    resolv = "nameserver 1.1.1.1\n"

    assert merge_dns({}, usable_resolvers(resolv))["dns"] == ["1.1.1.1", "8.8.8.8"]


def test_operator_set_keys_are_preserved():
    """We own the "dns" key and nothing else on the box's daemon.json."""
    cfg = {"log-driver": "json-file", "dns": ["fe80::1%3"]}

    merged = merge_dns(cfg, ["192.168.1.1"])

    assert merged["log-driver"] == "json-file"
    assert merged["dns"] == ["192.168.1.1", "1.1.1.1", "8.8.8.8"]
    assert cfg["dns"] == ["fe80::1%3"], "input config should not be mutated"


def test_rejected_resolvers_are_reported_for_the_install_log():
    resolv = "nameserver 192.168.1.1\nnameserver fe80::1%3\nnameserver 127.0.0.53\n"

    assert rejected_resolvers(resolv) == [
        ("fe80::1%3", "link-local"),
        ("127.0.0.53", "loopback"),
    ]


def test_load_daemon_json_tolerates_missing_and_corrupt_files(tmp_path):
    assert load_daemon_json(str(tmp_path / "absent.json")) == {}

    corrupt = tmp_path / "daemon.json"
    corrupt.write_text("{not json")
    assert load_daemon_json(str(corrupt)) == {}

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"log-driver": "journald"}))
    assert load_daemon_json(str(valid)) == {"log-driver": "journald"}


@pytest.mark.parametrize("content", ["[1, 2]", '"a string"', "42", "null"])
def test_load_daemon_json_rejects_valid_json_that_is_not_an_object(tmp_path, content):
    """A daemon.json that isn't an object is no more usable to us than to Docker.

    Returning it would blow up the merge rather than fall back cleanly.
    """
    path = tmp_path / "daemon.json"
    path.write_text(content)

    assert load_daemon_json(str(path)) == {}
    assert merge_dns(load_daemon_json(str(path)), ["192.168.1.1"])["dns"] == [
        "192.168.1.1",
        "1.1.1.1",
        "8.8.8.8",
    ]

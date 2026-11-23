# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager boxes add-all` against a real `.lager` and a stubbed `tailscale status`.

`add-all` used to overwrite a saved box whose IP changed without asking, and
to add a box whose IP another saved box already had, so two entries shared an
IP (#851). It now follows `lager boxes add`: both changes need a confirm or
`--yes`, the box that had the IP is replaced, and a saved user is kept.
"""

import json
import subprocess
import types

import pytest


TAILSCALE = (
    "192.0.2.10   bench1   user@  linux  -\n"
    "192.0.2.20   bench2   user@  linux  -\n"
    "100.64.0.1   laptop-of-someone user@ macOS -\n"
)


@pytest.fixture
def lager_file(fake_box, tmp_path, monkeypatch):
    path = tmp_path / ".lager"

    def write(boxes):
        path.write_text(json.dumps({"BOXES": boxes}))

    def fake_run(cmd, **kwargs):
        assert cmd == ["tailscale", "status"]
        return types.SimpleNamespace(stdout=TAILSCALE, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    write({})
    return path, write


def _boxes(path):
    return json.loads(path.read_text())["BOXES"]


def test_new_boxes_are_added(lager, lager_file):
    path, _ = lager_file
    result = lager("boxes", "add-all", "--yes")
    assert result.exit_code == 0, result.output
    assert _boxes(path) == {"BENCH1": "192.0.2.10", "BENCH2": "192.0.2.20"}
    assert "Added:   2" in result.output


def test_unchanged_box_is_skipped(lager, lager_file):
    path, write = lager_file
    write({"BENCH1": "192.0.2.10"})
    result = lager("boxes", "add-all", "--yes")
    assert "BENCH1: already exists (skipped)" in result.output
    assert "Skipped: 1" in result.output


def test_changed_ip_asks_and_declining_keeps_the_saved_ip(lager, lager_file):
    path, write = lager_file
    write({"BENCH1": {"ip": "192.0.2.99", "user": "benchuser"}})
    # Confirm the batch, decline the BENCH1 update.
    result = lager("boxes", "add-all", input="y\nn\n")
    assert result.exit_code == 0, result.output
    assert "saved with IP 192.0.2.99; Tailscale reports 192.0.2.10" in result.output
    assert _boxes(path)["BENCH1"] == {"ip": "192.0.2.99", "user": "benchuser"}
    assert "BENCH1: not changed (skipped)" in result.output


def test_changed_ip_confirmed_keeps_the_saved_user(lager, lager_file):
    path, write = lager_file
    write({"BENCH1": {"ip": "192.0.2.99", "user": "benchuser"}})
    result = lager("boxes", "add-all", input="y\ny\n")
    assert result.exit_code == 0, result.output
    assert _boxes(path)["BENCH1"] == {"ip": "192.0.2.10", "user": "benchuser"}
    assert "Updated: 1" in result.output


def test_an_ip_another_box_has_is_never_shared(lager, lager_file):
    path, write = lager_file
    write({"OLDNAME": "192.0.2.20"})
    result = lager("boxes", "add-all", "--yes")
    assert result.exit_code == 0, result.output
    boxes = _boxes(path)
    assert "OLDNAME" not in boxes
    assert boxes["BENCH2"] == "192.0.2.20"
    ips = [b if isinstance(b, str) else b["ip"] for b in boxes.values()]
    assert len(ips) == len(set(ips))


def test_declining_a_shared_ip_adds_nothing_for_that_box(lager, lager_file):
    path, write = lager_file
    write({"OLDNAME": "192.0.2.20"})
    # Batch yes; BENCH1 is new (no prompt); BENCH2 shares OLDNAME's IP: decline.
    result = lager("boxes", "add-all", input="y\nn\n")
    assert result.exit_code == 0, result.output
    assert "IP 192.0.2.20 is already saved as OLDNAME" in result.output
    boxes = _boxes(path)
    assert boxes["OLDNAME"] == "192.0.2.20"
    assert "BENCH2" not in boxes
    assert boxes["BENCH1"] == "192.0.2.10"

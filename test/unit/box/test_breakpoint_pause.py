# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for lager.pause() — the interactive script breakpoint.

box/lager/breakpoint.py is pure stdlib, so we load it standalone (bypassing
lager/__init__.py and its hardware-only transitive imports) and point its
PROCESS_DIR_BASE at a tmp dir.

Covered:
  - _enabled() honours the LAGER_BREAKPOINTS off-switch
  - _resolve_timeout() precedence: arg > env > default
  - pause() is a safe no-op outside `lager python` (no LAGER_PROCESS_ID)
  - pause() no-ops when disabled, writing nothing
  - a resume marker unblocks pause() and the coordination files are cleaned up
  - pause() auto-resumes after its timeout
  - breakpoint.json carries the caller location + label
"""

from __future__ import annotations

import os
import json
import time
import threading
import importlib.util

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BP_PATH = os.path.abspath(
    os.path.join(_HERE, "..", "..", "..", "box", "lager", "breakpoint.py")
)
_spec = importlib.util.spec_from_file_location("lager_breakpoint_under_test", _BP_PATH)
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

PID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(bp, "_POLL_INTERVAL", 0.01)
    monkeypatch.delenv("LAGER_BREAKPOINTS", raising=False)
    monkeypatch.delenv("LAGER_BREAKPOINT_TIMEOUT", raising=False)


@pytest.mark.parametrize(
    "val,expected",
    [("off", False), ("0", False), ("false", False), ("no", False),
     ("OFF", False), ("1", True), ("on", True), ("", True)],
)
def test_enabled(monkeypatch, val, expected):
    monkeypatch.setenv("LAGER_BREAKPOINTS", val)
    assert bp._enabled() is expected


def test_enabled_when_unset(monkeypatch):
    monkeypatch.delenv("LAGER_BREAKPOINTS", raising=False)
    assert bp._enabled() is True


def test_resolve_timeout_precedence(monkeypatch):
    monkeypatch.delenv("LAGER_BREAKPOINT_TIMEOUT", raising=False)
    assert bp._resolve_timeout(None) == bp.DEFAULT_TIMEOUT
    assert bp._resolve_timeout(10) == 10          # arg wins
    monkeypatch.setenv("LAGER_BREAKPOINT_TIMEOUT", "42")
    assert bp._resolve_timeout(None) == 42         # env over default
    assert bp._resolve_timeout(7) == 7             # arg still wins
    monkeypatch.setenv("LAGER_BREAKPOINT_TIMEOUT", "not-an-int")
    assert bp._resolve_timeout(None) == bp.DEFAULT_TIMEOUT


def test_noop_without_process_id(monkeypatch):
    monkeypatch.delenv("LAGER_PROCESS_ID", raising=False)
    # Must return promptly and not raise even with a long timeout.
    start = time.monotonic()
    bp.pause("x", timeout=999)
    assert time.monotonic() - start < 1


def test_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "PROCESS_DIR_BASE", str(tmp_path))
    monkeypatch.setenv("LAGER_PROCESS_ID", PID)
    monkeypatch.setenv("LAGER_BREAKPOINTS", "off")
    start = time.monotonic()
    bp.pause("x", timeout=999)
    assert time.monotonic() - start < 1
    assert not (tmp_path / PID).exists()  # wrote nothing


def test_resume_marker_unblocks_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "PROCESS_DIR_BASE", str(tmp_path))
    monkeypatch.setenv("LAGER_PROCESS_ID", PID)
    proc_dir = tmp_path / PID
    captured = {}

    def writer():
        state = proc_dir / "breakpoint.json"
        for _ in range(500):
            if state.exists():
                captured.update(json.loads(state.read_text()))
                break
            time.sleep(0.005)
        (proc_dir / "resume").write_text("1")

    t = threading.Thread(target=writer)
    t.start()
    start = time.monotonic()
    bp.pause("check DUT", timeout=10)  # writer resumes well before 10s
    elapsed = time.monotonic() - start
    t.join(timeout=2)

    assert elapsed < 5
    assert captured.get("paused") is True
    assert captured.get("label") == "check DUT"
    assert captured.get("file", "").endswith("test_breakpoint_pause.py")
    assert isinstance(captured.get("line"), int)
    assert captured.get("console_port") is None
    # coordination files removed on resume
    assert not (proc_dir / "breakpoint.json").exists()
    assert not (proc_dir / "resume").exists()


def test_auto_resume_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(bp, "PROCESS_DIR_BASE", str(tmp_path))
    monkeypatch.setenv("LAGER_PROCESS_ID", PID)
    proc_dir = tmp_path / PID
    start = time.monotonic()
    bp.pause("x", timeout=0.3)
    elapsed = time.monotonic() - start
    assert 0.2 <= elapsed < 5
    assert not (proc_dir / "breakpoint.json").exists()
    assert not (proc_dir / "resume").exists()


def test_stale_resume_marker_is_cleared(tmp_path, monkeypatch):
    """A leftover resume marker from a prior breakpoint must not auto-skip."""
    monkeypatch.setattr(bp, "PROCESS_DIR_BASE", str(tmp_path))
    monkeypatch.setenv("LAGER_PROCESS_ID", PID)
    proc_dir = tmp_path / PID
    proc_dir.mkdir(parents=True)
    (proc_dir / "resume").write_text("1")  # stale

    start = time.monotonic()
    bp.pause("x", timeout=0.3)  # should auto-resume on timeout, not the stale marker
    assert time.monotonic() - start >= 0.2

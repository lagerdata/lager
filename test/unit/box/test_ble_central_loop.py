# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`Central()` with no loop passed gets a loop of its own.

It used to call `asyncio.get_event_loop()`, which has been deprecated since
3.10 for this use, raises `RuntimeError` when no current loop is set (any
thread but the main one, or after `set_event_loop(None)`), and on 3.14 raises
whenever there is no current loop at all -- the normal state of a synchronous
`lager python` script and of `protocols/ble/scan.py`. The BluFi client already
makes its own with `asyncio.new_event_loop()`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys
import threading
import types
import warnings

import pytest

CLIENT_PY = (pathlib.Path(__file__).resolve().parents[3]
             / "box" / "lager" / "protocols" / "ble" / "client.py")


@pytest.fixture
def ble_client(monkeypatch):
    """box/lager/protocols/ble/client.py, loaded by path with bleak stubbed."""
    bleak = types.ModuleType("bleak")
    bleak.BleakScanner = object  # type: ignore[attr-defined]
    bleak.BleakClient = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bleak", bleak)
    spec = importlib.util.spec_from_file_location("_ble_client_under_test", CLIENT_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _answer():
    return 42


def test_no_current_loop_gives_a_usable_loop_without_warnings(ble_client):
    asyncio.set_event_loop(None)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        central = ble_client.Central()
    try:
        assert central.loop.run_until_complete(_answer()) == 42
    finally:
        central.loop.close()


def test_it_works_off_the_main_thread(ble_client):
    result = {}

    def build():
        try:
            central = ble_client.Central()
            result["value"] = central.loop.run_until_complete(_answer())
            central.loop.close()
        except Exception as exc:  # recorded for the assertion below
            result["error"] = exc

    worker = threading.Thread(target=build)
    worker.start()
    worker.join(10)
    assert result == {"value": 42}


def test_a_loop_passed_in_is_used(ble_client):
    loop = asyncio.new_event_loop()
    try:
        assert ble_client.Central(loop=loop).loop is loop
    finally:
        loop.close()


# --- #847: notify completion count and the client connect/pair return ---


def test_notify_event_set_on_exactly_max_messages(ble_client):
    evt = asyncio.Event()
    messages = []
    seen = []
    for i in range(3):
        ble_client.notify_handler(evt, messages, lambda h, d: seen.append(d), 3, 1, bytes([i]))
    assert evt.is_set()
    assert messages == [b"\x00", b"\x01", b"\x02"]
    assert seen == messages


def test_notify_not_set_before_max_messages(ble_client):
    evt = asyncio.Event()
    messages = []
    for i in range(2):
        ble_client.notify_handler(evt, messages, ble_client.noop_handler, 3, 1, bytes([i]))
    assert not evt.is_set()
    assert len(messages) == 2


def test_notify_ignores_messages_after_completion(ble_client):
    evt = asyncio.Event()
    messages = []
    seen = []
    for i in range(5):
        ble_client.notify_handler(evt, messages, lambda h, d: seen.append(d), 2, 1, bytes([i]))
    assert messages == [b"\x00", b"\x01"]
    assert seen == messages


class _FakeBleak:
    """Stands in for BleakClient: connect/pair resolve to True."""

    def __init__(self, address):
        self.address = address
        self.calls = []

    async def connect(self, *args, **kwargs):
        self.calls.append("connect")
        return True

    async def pair(self, *args, **kwargs):
        self.calls.append("pair")
        return True

    async def start_notify(self, char_specifier, handler):
        for i in range(3):
            handler(7, bytes([i]))


@pytest.mark.parametrize("method", ["connect", "pair"])
def test_central_connect_and_pair_return_the_client(ble_client, monkeypatch, method):
    monkeypatch.setattr(ble_client, "BleakClient", _FakeBleak)
    central = ble_client.Central()
    try:
        client = getattr(central, method)("AA:BB:CC:DD:EE:FF")
        assert isinstance(client, ble_client.Client)
        assert client._client.address == "AA:BB:CC:DD:EE:FF"
        assert client._client.calls == [method]
        assert client.loop is central.loop
    finally:
        central.loop.close()


def test_start_notify_returns_after_exactly_max_messages(ble_client):
    loop = asyncio.new_event_loop()
    try:
        client = ble_client.Client(_FakeBleak("AA"), loop=loop)
        timed_out, messages = client.start_notify("uuid", max_messages=3, timeout=1)
        assert timed_out is False
        assert messages == [b"\x00", b"\x01", b"\x02"]
    finally:
        loop.close()

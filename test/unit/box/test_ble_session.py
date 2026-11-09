# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Held BLE GATT session actions on POST /ble/command.

Covers session_open/pair/read/write/notify_*/close, the single-session and
adapter-ownership rules (one-shot BLE + BluFi refuse while open), idempotent
and forced close, peer disconnect, and the 300 s lifetime reaper. Bleak is
faked. Most tests run coroutines on a fresh loop per call; the "bleak loop"
class uses the real run_bleak so the client lives on the persistent loop.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        key = ".".join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


_HARDWARE_STUBS = [
    "pyvisa",
    "pyvisa.constants",
    "pyvisa_py",
    "usb",
    "usb.util",
    "usb.core",
    "pigpio",
    "labjack",
    "labjack.ljm",
    "nidaqmx",
    "phidget22",
    "phidget22.Phidget",
    "phidget22.Net",
    "bleak",
    "picoscope",
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "spidev",
    "smbus",
    "smbus2",
    "RPi",
    "RPi.GPIO",
    "gpiod",
    "flask_socketio",
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "box")
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask
from lager.http_handlers import ble as ble_handler
from lager.http_handlers import blufi as blufi_handler

_real_ensure_reaper = ble_handler._ensure_reaper

ADDR = "AA:BB:CC:DD:EE:01"
CHAR = "00002a19-0000-1000-8000-00805f9b34fb"
CHAR2 = "00002a1a-0000-1000-8000-00805f9b34fb"


class _FakeBleakClient:
    """Async BleakClient stand-in with held-connection connect/disconnect."""

    instances = []

    def __init__(self, address, timeout=None, disconnected_callback=None):
        self.address = address
        self.timeout = timeout
        self.disconnected_callback = disconnected_callback
        self.calls = []
        self.connected = False
        self.read_value = bytearray(b"\x64")
        self._notify_callbacks = {}
        self.notifications_on_start = {}
        _FakeBleakClient.instances.append(self)

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()
        return False

    async def connect(self):
        self.calls.append("connect")
        self.connected = True

    async def disconnect(self):
        self.calls.append("disconnect")
        self.connected = False
        if self.disconnected_callback is not None:
            self.disconnected_callback(self)

    async def read_gatt_char(self, char):
        self.calls.append(("read", char))
        return self.read_value

    async def write_gatt_char(self, char, data, response=None):
        self.calls.append(("write", char, bytes(data), response))

    async def start_notify(self, char, callback):
        self.calls.append(("start_notify", char))
        self._notify_callbacks[char] = callback
        for data in self.notifications_on_start.get(char, []):
            callback(char, bytearray(data))

    async def stop_notify(self, char):
        self.calls.append(("stop_notify", char))
        self._notify_callbacks.pop(char, None)

    async def pair(self):
        self.calls.append("pair")
        return True

    def drop_link(self):
        """Peer-initiated disconnect, as bleak reports it."""
        self.connected = False
        self.disconnected_callback(self)

    def push_notify(self, char, data: bytes):
        cb = self._notify_callbacks.get(char)
        if cb is None:
            raise AssertionError("no notify callback for %s" % char)
        cb(char, bytearray(data))


def _run_on_new_loop(coro, timeout):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _SessionHarness:
    """Shared setup; subclasses choose how run_bleak executes coroutines."""

    real_bleak_loop = False

    def setUp(self):
        app = Flask(__name__)
        ble_handler.register_ble_routes(app)
        self.app = app
        self.client = app.test_client()
        _FakeBleakClient.instances = []
        self._clear_sessions()

        bleak = sys.modules["bleak"]
        patcher = patch.object(bleak, "BleakClient", _FakeBleakClient, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        if not self.real_bleak_loop:
            runner = patch.object(ble_handler, "run_bleak", _run_on_new_loop)
            runner.start()
            self.addCleanup(runner.stop)
        reaper = patch.object(ble_handler, "_ensure_reaper")
        reaper.start()
        self.addCleanup(reaper.stop)
        self.addCleanup(self._close_leftover_session)
        self.addCleanup(self._clear_sessions)

    def _clear_sessions(self):
        with ble_handler._sessions_lock:
            ble_handler._sessions.clear()
            ble_handler._ended.clear()

    def _close_leftover_session(self):
        self._post("session_close", force=True)

    def _post(self, action, **params):
        return self.client.post(
            "/ble/command", json={"action": action, "params": params}
        )

    def _open(self, address=ADDR):
        r = self._post("session_open", address=address)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["value"]["session_id"]

    def _blufi_status(self):
        app = Flask(__name__)
        blufi_handler.register_blufi_routes(app)
        fake = types.ModuleType("lager.blufi")
        fake.BlufiClient = MagicMock()
        import lager as lager_pkg

        with (
            patch.dict(sys.modules, {"lager.blufi": fake}),
            patch.object(lager_pkg, "blufi", fake, create=True),
        ):
            return app.test_client().post(
                "/blufi/command",
                json={"action": "status", "params": {"device_name": "x"}},
            )

    def _device(self):
        self.assertTrue(_FakeBleakClient.instances)
        return _FakeBleakClient.instances[-1]


class TestBleSession(_SessionHarness, unittest.TestCase):
    def test_open_returns_id_without_services_and_holds_the_link(self):
        r = self._post("session_open", address=ADDR)
        self.assertEqual(r.status_code, 200)
        value = r.get_json()["value"]
        self.assertEqual(set(value.keys()), {"session_id", "address"})
        self.assertEqual(value["address"], ADDR)
        sid = value["session_id"]

        data = self._post("session_read", session_id=sid, char=CHAR).get_json()
        self.assertEqual(data["value"]["data"], "64")
        # Still connected — no disconnect between open and read.
        self.assertEqual(self._device().calls, ["connect", ("read", CHAR)])

    def test_second_open_is_409(self):
        sid = self._open()
        r = self._post("session_open", address="11:22:33:44:55:66")
        self.assertEqual(r.status_code, 409)
        self.assertIn(sid, r.get_json()["error"])
        self.assertEqual(len(_FakeBleakClient.instances), 1)

    def test_one_shot_gatt_refuses_while_session_open(self):
        sid = self._open()
        for action, params in (
            ("scan", {}),
            ("read", {"address": ADDR, "char": CHAR}),
            ("pair", {"address": ADDR}),
        ):
            r = self._post(action, **params)
            self.assertEqual(r.status_code, 409, action)
            self.assertIn(sid, r.get_json()["error"])

    def test_session_pair_read_write_on_held_client(self):
        sid = self._open()
        r = self._post("session_pair", session_id=sid)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["value"]["paired"])

        r = self._post(
            "session_write", session_id=sid, char=CHAR, data="01ff", response=True
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"]["length"], 2)

        r = self._post("session_read", session_id=sid, char=CHAR)
        self.assertEqual(r.get_json()["value"]["data"], "64")

        self.assertEqual(
            self._device().calls,
            ["connect", "pair", ("write", CHAR, b"\x01\xff", True), ("read", CHAR)],
        )

    def test_multi_char_notify_poll(self):
        sid = self._open()
        device = self._device()

        self.assertEqual(
            self._post("session_notify_start", session_id=sid, char=CHAR).status_code,
            200,
        )
        self.assertEqual(
            self._post("session_notify_start", session_id=sid, char=CHAR2).status_code,
            200,
        )

        device.push_notify(CHAR, b"\x01")
        device.push_notify(CHAR2, b"\x02")
        device.push_notify(CHAR, b"\x03")

        r = self._post("session_notify_poll", session_id=sid, max_messages=10, wait=0)
        self.assertEqual(r.status_code, 200)
        messages = r.get_json()["value"]["messages"]
        self.assertEqual(
            messages,
            [
                {"char": CHAR, "data": "01"},
                {"char": CHAR2, "data": "02"},
                {"char": CHAR, "data": "03"},
            ],
        )
        self.assertTrue(r.get_json()["value"]["timed_out"])

        r = self._post(
            "session_notify_poll", session_id=sid, char=CHAR2, max_messages=5, wait=0
        )
        # Nothing left for CHAR2.
        self.assertEqual(r.get_json()["value"]["messages"], [])

        self._post("session_notify_stop", session_id=sid, char=CHAR)
        self._post("session_notify_stop", session_id=sid, char=CHAR2)
        self.assertIn(("stop_notify", CHAR), device.calls)
        self.assertIn(("stop_notify", CHAR2), device.calls)

    def test_notify_start_twice_is_400(self):
        sid = self._open()
        self.assertEqual(
            self._post("session_notify_start", session_id=sid, char=CHAR).status_code,
            200,
        )
        r = self._post("session_notify_start", session_id=sid, char=CHAR)
        self.assertEqual(r.status_code, 400)

    def test_close_is_idempotent(self):
        sid = self._open()
        r = self._post("session_close", session_id=sid)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["value"]["closed"])
        self.assertIn("disconnect", self._device().calls)

        r = self._post("session_close", session_id=sid)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["value"]["closed"])

        # One-shot works again after close.
        r = self._post("pair", address=ADDR)
        self.assertEqual(r.status_code, 200)

    def test_unknown_session_ops_are_400(self):
        r = self._post("session_read", session_id="deadbeef", char=CHAR)
        self.assertEqual(r.status_code, 400)

    def _fake_clock(self):
        now = [1000.0]
        patcher = patch.object(ble_handler, "_monotonic", lambda: now[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        return now

    def test_reaper_closes_expired_session_without_a_request(self):
        now = self._fake_clock()
        sid = self._open()
        now[0] += ble_handler.SESSION_MAX_LIFETIME_S - 1
        ble_handler.reap_expired_sessions()
        self.assertNotIn("disconnect", self._device().calls)

        now[0] += 1
        ble_handler.reap_expired_sessions()
        self.assertIn("disconnect", self._device().calls)
        with ble_handler._sessions_lock:
            self.assertEqual(ble_handler._sessions, {})

        r = self._post("session_read", session_id=sid, char=CHAR)
        self.assertEqual(r.status_code, 400)
        self.assertIn("ended: lifetime expired", r.get_json()["error"])
        self.assertEqual(self._post("session_open", address=ADDR).status_code, 200)

    def test_ensure_reaper_starts_one_thread(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = []

        def blocking_loop():
            started.append(threading.current_thread())
            release.wait(10)

        with (
            patch.object(ble_handler, "_reaper_thread", None),
            patch.object(ble_handler, "_reaper_loop", blocking_loop),
        ):
            _real_ensure_reaper()
            _real_ensure_reaper()
            thread = ble_handler._reaper_thread
            thread.join(0.05)
            self.assertTrue(thread.is_alive())
        release.set()
        thread.join(5)
        self.assertEqual(len(started), 1)

    def test_blufi_refuses_while_session_open(self):
        sid = self._open()
        r = self._blufi_status()
        self.assertEqual(r.status_code, 409)
        self.assertIn(sid, r.get_json()["error"])

    def test_peer_disconnect_frees_adapter_and_reports_reason(self):
        sid = self._open()
        self._device().drop_link()

        r = self._post("session_read", session_id=sid, char=CHAR)
        self.assertEqual(r.status_code, 400)
        self.assertIn("ended: peer disconnected", r.get_json()["error"])
        self.assertEqual(self._post("pair", address=ADDR).status_code, 200)
        self.assertEqual(self._post("session_close", session_id=sid).status_code, 200)

    def test_our_own_close_keeps_closed_reason(self):
        sid = self._open()
        self._post("session_close", session_id=sid)
        r = self._post("session_read", session_id=sid, char=CHAR)
        self.assertIn("ended: closed", r.get_json()["error"])

    def test_ended_sessions_remembered_up_to_a_bound(self):
        first = self._open()
        self._post("session_close", session_id=first)
        for _ in range(ble_handler._ENDED_SESSIONS_KEPT):
            self._post("session_close", session_id=self._open())
        r = self._post("session_read", session_id=first, char=CHAR)
        self.assertIn("unknown BLE session_id", r.get_json()["error"])

    def test_close_without_id_needs_force_while_open(self):
        sid = self._open()
        r = self._post("session_close")
        self.assertEqual(r.status_code, 400)

        r = self._post("session_close", force=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["value"]["session_id"], sid)
        self.assertIn("disconnect", self._device().calls)
        self.assertEqual(self._post("pair", address=ADDR).status_code, 200)

    def test_close_without_id_succeeds_when_none_open(self):
        for params in ({}, {"force": True}):
            r = self._post("session_close", **params)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["value"]["closed"])

    def test_failed_notify_start_releases_the_char(self):
        sid = self._open()
        device = self._device()
        original_start = device.start_notify

        async def failing_start(char, callback):
            raise RuntimeError("CCCD write failed")

        device.start_notify = failing_start
        r = self._post("session_notify_start", session_id=sid, char=CHAR)
        self.assertEqual(r.status_code, 502)

        device.start_notify = original_start
        r = self._post("session_notify_start", session_id=sid, char=CHAR)
        self.assertEqual(r.status_code, 200)

    def test_notify_stop_on_inactive_char_is_400(self):
        sid = self._open()
        r = self._post("session_notify_stop", session_id=sid, char=CHAR)
        self.assertEqual(r.status_code, 400)

    def test_poll_filtered_to_unknown_char_is_400(self):
        sid = self._open()
        self._post("session_notify_start", session_id=sid, char=CHAR)
        r = self._post("session_notify_poll", session_id=sid, char=CHAR2, wait=0)
        self.assertEqual(r.status_code, 400)

    def test_poll_wait_is_clamped_to_remaining_lifetime(self):
        now = self._fake_clock()
        sid = self._open()
        self._post("session_notify_start", session_id=sid, char=CHAR)
        now[0] += ble_handler.SESSION_MAX_LIFETIME_S - 0.2
        r = self._post("session_notify_poll", session_id=sid, wait=300)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["value"]["timed_out"])

    def test_session_op_timeout_param_is_range_checked(self):
        sid = self._open()
        r = self._post("session_read", session_id=sid, char=CHAR, timeout=0)
        self.assertEqual(r.status_code, 400)


class TestBleSessionOnBleakLoop(_SessionHarness, unittest.TestCase):
    """Real run_bleak: the client is held on the persistent bleak loop."""

    real_bleak_loop = True
    BOUNDED_S = 10.0
    FAST_S = 3.0

    def setUp(self):
        super().setUp()
        self.poll_entered = threading.Event()
        original = ble_handler._session_notify_poll_async

        async def announcing_poll(*args):
            self.poll_entered.set()
            return await original(*args)

        announce = patch.object(
            ble_handler, "_session_notify_poll_async", announcing_poll
        )
        announce.start()
        self.addCleanup(announce.stop)

    def _start_notifying_session(self):
        sid = self._open()
        self._post("session_notify_start", session_id=sid, char=CHAR)
        return sid

    def _poll_in_background(self, sid, wait=30):
        """Returns (thread, result) where result gets 'response' when done."""
        result = {}

        def run():
            client = self.app.test_client()
            result["response"] = client.post(
                "/ble/command",
                json={
                    "action": "session_notify_poll",
                    "params": {"session_id": sid, "wait": wait},
                },
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(self.poll_entered.wait(self.BOUNDED_S))
        return thread, result

    def _finish_poll(self, thread, result):
        thread.join(self.BOUNDED_S)
        self.assertFalse(thread.is_alive())
        return result["response"]

    def test_concurrent_notify_start_for_same_char_subscribes_once(self):
        sid = self._open()
        device = self._device()
        entered = threading.Event()
        release = asyncio.Event()
        original_start = device.start_notify

        async def gated_start(char, callback):
            entered.set()
            await release.wait()
            await original_start(char, callback)

        device.start_notify = gated_start
        result = {}

        def first_start():
            client = self.app.test_client()
            result["response"] = client.post(
                "/ble/command",
                json={
                    "action": "session_notify_start",
                    "params": {"session_id": sid, "char": CHAR},
                },
            )

        thread = threading.Thread(target=first_start, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(self.BOUNDED_S))

        second = self._post("session_notify_start", session_id=sid, char=CHAR)
        self.assertEqual(second.status_code, 400)

        ble_handler.get_bleak_loop().call_soon_threadsafe(release.set)
        thread.join(self.BOUNDED_S)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["response"].status_code, 200)
        self.assertEqual(device.calls.count(("start_notify", CHAR)), 1)

    def test_poll_returns_notification_pushed_from_another_thread(self):
        sid = self._start_notifying_session()
        device = self._device()
        thread, result = self._poll_in_background(sid, wait=30)

        loop = ble_handler.get_bleak_loop()
        loop.call_soon_threadsafe(device.push_notify, CHAR, b"\x07")

        response = self._finish_poll(thread, result)
        value = response.get_json()["value"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(value["messages"], [{"char": CHAR, "data": "07"}])
        self.assertFalse(value["timed_out"])

    def test_one_shot_and_blufi_get_409_during_poll(self):
        sid = self._start_notifying_session()
        thread, result = self._poll_in_background(sid)

        started = time.monotonic()
        read = self._post("read", address=ADDR, char=CHAR)
        blufi = self._blufi_status()
        elapsed = time.monotonic() - started

        self.assertEqual(read.status_code, 409)
        self.assertEqual(blufi.status_code, 409)
        self.assertLess(elapsed, self.FAST_S)
        self._post("session_close", session_id=sid)
        self._finish_poll(thread, result)

    def test_write_proceeds_during_poll(self):
        sid = self._start_notifying_session()
        thread, result = self._poll_in_background(sid)

        r = self._post("session_write", session_id=sid, char=CHAR, data="01")
        self.assertEqual(r.status_code, 200)
        self._post("session_close", session_id=sid)
        self._finish_poll(thread, result)

    def test_close_during_poll_returns_promptly_and_ends_poll(self):
        sid = self._start_notifying_session()
        thread, result = self._poll_in_background(sid)

        started = time.monotonic()
        closed = self._post("session_close", session_id=sid)
        elapsed = time.monotonic() - started
        self.assertEqual(closed.status_code, 200)
        self.assertLess(elapsed, self.FAST_S)

        response = self._finish_poll(thread, result)
        self.assertEqual(response.status_code, 400)
        self.assertIn("ended: closed", response.get_json()["error"])

    def test_peer_disconnect_ends_inflight_poll_and_frees_adapter(self):
        sid = self._start_notifying_session()
        device = self._device()
        thread, result = self._poll_in_background(sid)

        ble_handler.get_bleak_loop().call_soon_threadsafe(device.drop_link)

        response = self._finish_poll(thread, result)
        self.assertEqual(response.status_code, 400)
        self.assertIn("ended: peer disconnected", response.get_json()["error"])
        self.assertEqual(self._post("pair", address=ADDR).status_code, 200)


if __name__ == "__main__":
    unittest.main()

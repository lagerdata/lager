# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the websocket UART session read loop (_uart_read_loop).

Regression coverage for the re-enumeration staleness bug: when a read failed,
the old loop emitted an error and stopped — but left the open fd in place
(pinning the old tty number so the device came back under a new one) and left
the session registered in active_uart_sessions, so the per-net/per-device
guards refused clean reconnects until the socket dropped.

The new loop must: heal device-gone errors in place via driver.reconnect()
(with uart_status events), and on ANY exit evict its own session and close
the port. Driven directly with fake driver/socketio objects; flask_socketio
is stubbed (pattern from test_box_http_server_capabilities.py).
"""

import importlib.util
import os
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()
    return mod


for _dep in ('flask', 'flask_socketio'):
    if _dep not in sys.modules:
        try:
            __import__(_dep)
        except ImportError:
            sys.modules[_dep] = _make_module(_dep)


def _load_module(dotted, filepath):
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


uart_handlers = _load_module(
    "uart_session_cleanup_ut",
    os.path.join(BOX_DIR, "lager", "http_handlers", "uart.py"),
)


class GoneError(Exception):
    """Marker the fake driver classifies as device-gone."""


class FakeConn:
    """serial_conn stand-in driven by a script of actions.

    Actions: bytes -> returned from read(); an Exception instance -> raised;
    'stop' -> sets the stop_event (after a beat so pending buffer flushes).
    An exhausted script also sets the stop_event.
    """

    def __init__(self, script, stop_event):
        self.script = list(script)
        self.stop_event = stop_event
        self.in_waiting = 0

    def read(self, _size):
        if not self.script:
            self.stop_event.set()
            return b''
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        if action == 'stop':
            time.sleep(0.06)  # let the emit interval elapse so buffers flush
            self.stop_event.set()
            return b''
        return action


class FakeDriver:
    def __init__(self, stop_event, script=(), reconnect_result=False,
                 reconnect_script=()):
        self.stop_event = stop_event
        self.serial_conn = FakeConn(script, stop_event)
        self.reconnect_result = reconnect_result
        self.reconnect_script = reconnect_script
        self.reconnect_calls = []
        self.cleanup_calls = 0
        self.opost = False
        self.device_path = '/dev/ttyUSB0'
        self.baudrate = 115200

    @staticmethod
    def is_device_gone(exc):
        return isinstance(exc, GoneError)

    def reconnect(self, stop_check=None, on_status=None, total_timeout=None):
        self.reconnect_calls.append(total_timeout)
        if stop_check and stop_check():
            return False
        if self.reconnect_result:
            self.device_path = '/dev/ttyUSB1'  # healed onto the new node
            self.serial_conn = FakeConn(self.reconnect_script, self.stop_event)
        return self.reconnect_result

    def _cleanup(self):
        self.cleanup_calls += 1


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload, namespace=None, room=None):
        self.events.append((event, payload))

    def of(self, event_name):
        return [p for (e, p) in self.events if e == event_name]


class UartReadLoopTests(unittest.TestCase):
    SID = 'sid-1'

    def setUp(self):
        uart_handlers.active_uart_sessions.clear()
        self.stop_event = threading.Event()
        self.sio = FakeSocketIO()

    def tearDown(self):
        uart_handlers.active_uart_sessions.clear()

    def _register(self, driver):
        uart_handlers.active_uart_sessions[self.SID] = {
            'driver': driver,
            'stop_event': self.stop_event,
            'netname': 'uart1',
        }

    def _run(self, driver):
        uart_handlers._uart_read_loop(
            self.sio, self.SID, 'uart1', driver, self.stop_event)

    def test_streams_data(self):
        driver = FakeDriver(self.stop_event, script=[b'hi', 'stop'])
        self._register(driver)
        self._run(driver)
        payloads = self.sio.of('uart_data')
        self.assertTrue(payloads, "expected uart_data to be emitted")
        self.assertEqual(bytes.fromhex(payloads[0]['data']), b'hi')

    def test_device_gone_reconnects_and_resumes(self):
        driver = FakeDriver(self.stop_event,
                            script=[GoneError('[Errno 19] No such device')],
                            reconnect_result=True,
                            reconnect_script=[b'back', 'stop'])
        self._register(driver)
        self._run(driver)

        self.assertEqual(driver.reconnect_calls,
                         [uart_handlers.UART_RECONNECT_TIMEOUT])
        statuses = [p['status'] for p in self.sio.of('uart_status')]
        self.assertEqual(statuses, ['reconnecting', 'reconnected'])
        reconnected = self.sio.of('uart_status')[1]
        self.assertEqual(reconnected['device_path'], '/dev/ttyUSB1')
        # Streaming resumed on the new connection
        data = b''.join(bytes.fromhex(p['data']) for p in self.sio.of('uart_data'))
        self.assertEqual(data, b'back')
        # No terminal error
        self.assertEqual(self.sio.of('error'), [])

    def test_reconnect_failure_emits_error_and_evicts(self):
        driver = FakeDriver(self.stop_event,
                            script=[GoneError('gone')],
                            reconnect_result=False)
        self._register(driver)
        self._run(driver)

        errors = self.sio.of('error')
        self.assertEqual(len(errors), 1)
        self.assertIn('did not return', errors[0]['message'])
        # THE fix: session evicted + fd released so nothing stays wedged.
        self.assertNotIn(self.SID, uart_handlers.active_uart_sessions)
        self.assertGreaterEqual(driver.cleanup_calls, 1)

    def test_non_device_error_no_reconnect(self):
        driver = FakeDriver(self.stop_event,
                            script=[ValueError('bad read')])
        self._register(driver)
        self._run(driver)

        self.assertEqual(driver.reconnect_calls, [])
        errors = self.sio.of('error')
        self.assertEqual(len(errors), 1)
        self.assertIn('Read error', errors[0]['message'])
        self.assertNotIn(self.SID, uart_handlers.active_uart_sessions)
        self.assertGreaterEqual(driver.cleanup_calls, 1)

    def test_stop_during_reconnect_exits_without_error(self):
        driver = FakeDriver(self.stop_event,
                            script=[GoneError('gone')],
                            reconnect_result=False)

        real_reconnect = driver.reconnect

        def stopping_reconnect(**kwargs):
            self.stop_event.set()  # client hit stop mid-reconnect
            return real_reconnect(**kwargs)

        driver.reconnect = stopping_reconnect
        self._register(driver)
        self._run(driver)

        self.assertEqual(self.sio.of('error'), [])
        self.assertNotIn(self.SID, uart_handlers.active_uart_sessions)

    def test_clean_stop_evicts_and_cleans_up(self):
        driver = FakeDriver(self.stop_event, script=['stop'])
        self._register(driver)
        self._run(driver)
        self.assertNotIn(self.SID, uart_handlers.active_uart_sessions)
        self.assertGreaterEqual(driver.cleanup_calls, 1)

    def test_eviction_is_identity_guarded(self):
        # A replacement session under the same sid must not be evicted by the
        # old thread's teardown.
        driver = FakeDriver(self.stop_event, script=[ValueError('boom')])
        other_driver = FakeDriver(threading.Event())
        uart_handlers.active_uart_sessions[self.SID] = {
            'driver': other_driver,
            'netname': 'uart1',
        }
        self._run(driver)
        self.assertIn(self.SID, uart_handlers.active_uart_sessions)
        self.assertIs(
            uart_handlers.active_uart_sessions[self.SID]['driver'], other_driver)
        # The exiting thread still closed its own port
        self.assertGreaterEqual(driver.cleanup_calls, 1)


class StaleSessionReclaimTests(unittest.TestCase):
    """Auto-reclaim of a phantom UART session (registered, guarding its net,
    but with no live read loop behind it) so a fresh start_uart heals instead
    of hitting a permanent "already in use"."""

    SID = 'sid-stale'
    STALE = None  # set in setUp from the module constant

    def setUp(self):
        uart_handlers.active_uart_sessions.clear()
        self.sio = FakeSocketIO()
        self.STALE = uart_handlers.STALE_SESSION_TIMEOUT

    def tearDown(self):
        uart_handlers.active_uart_sessions.clear()

    @staticmethod
    def _dead_thread():
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        return t

    def test_stale_when_thread_dead(self):
        # Thread exited but (defensively) the session is still registered.
        session = {'driver': FakeDriver(threading.Event()), 'netname': 'uart1',
                   'thread': self._dead_thread(), 'last_activity': time.time()}
        self.assertTrue(uart_handlers._session_is_stale(session))

    def test_stale_when_heartbeat_aged(self):
        # Thread technically alive but wedged: heartbeat stopped advancing.
        session = {'driver': FakeDriver(threading.Event()), 'netname': 'uart1',
                   'last_activity': time.time() - (self.STALE + 5)}
        self.assertTrue(uart_handlers._session_is_stale(session))

    def test_live_session_not_stale(self):
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        try:
            session = {'driver': FakeDriver(threading.Event()),
                       'netname': 'uart1', 'thread': alive,
                       'last_activity': time.time()}
            self.assertFalse(uart_handlers._session_is_stale(session))
        finally:
            gate.set()
            alive.join(1.0)

    def test_setup_window_not_stale(self):
        # Freshly created: heartbeat seeded, thread not yet stored.
        session = {'driver': FakeDriver(threading.Event()), 'netname': 'uart1',
                   'last_activity': time.time()}
        self.assertFalse(uart_handlers._session_is_stale(session))

    def test_reclaim_tears_down_stale(self):
        stop = threading.Event()
        driver = FakeDriver(stop)
        session = {'driver': driver, 'stop_event': stop, 'netname': 'uart1',
                   'last_activity': time.time() - (self.STALE + 5)}
        uart_handlers.active_uart_sessions[self.SID] = session

        self.assertTrue(uart_handlers._reclaim_if_stale(self.SID, session))
        self.assertNotIn(self.SID, uart_handlers.active_uart_sessions)
        self.assertTrue(stop.is_set())            # wedged thread told to exit
        self.assertGreaterEqual(driver.cleanup_calls, 1)  # fd + flock released

    def test_reclaim_keeps_live_session(self):
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        stop = threading.Event()
        driver = FakeDriver(threading.Event())
        try:
            session = {'driver': driver, 'stop_event': stop, 'netname': 'uart1',
                       'thread': alive, 'last_activity': time.time()}
            uart_handlers.active_uart_sessions[self.SID] = session

            self.assertFalse(uart_handlers._reclaim_if_stale(self.SID, session))
            self.assertIn(self.SID, uart_handlers.active_uart_sessions)
            self.assertFalse(stop.is_set())       # live session left untouched
            self.assertEqual(driver.cleanup_calls, 0)
        finally:
            gate.set()
            alive.join(1.0)

    def test_read_loop_refreshes_heartbeat(self):
        # A running loop must keep last_activity fresh so a live session is
        # never misjudged stale by the reclaim path.
        stop = threading.Event()
        started = threading.Event()

        class BlockingConn:
            in_waiting = 0

            def read(self, _size):
                started.set()
                # emulate the real 0.1s read timeout in short, stoppable slices
                for _ in range(200):
                    if stop.is_set():
                        return b''
                    time.sleep(0.005)
                return b''

        driver = FakeDriver(stop)
        driver.serial_conn = BlockingConn()
        uart_handlers.active_uart_sessions[self.SID] = {
            'driver': driver, 'stop_event': stop, 'netname': 'uart1',
            'last_activity': time.time() - 999,  # begins stale
        }
        t = threading.Thread(
            target=uart_handlers._uart_read_loop,
            args=(self.sio, self.SID, 'uart1', driver, stop),
            daemon=True)
        t.start()
        try:
            self.assertTrue(started.wait(2.0), "read loop never started")
            last = uart_handlers.active_uart_sessions[self.SID].get('last_activity')
            self.assertIsNotNone(last, "heartbeat was never set")
            self.assertLess(time.time() - last, self.STALE,
                            "heartbeat should be fresh while the loop runs")
        finally:
            stop.set()
            t.join(2.0)
        self.assertFalse(t.is_alive(), "loop did not exit on stop")


if __name__ == "__main__":
    unittest.main()

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the /rtt WebSocket handlers (bi-directional RTT).

Covers the read loop (_rtt_read_loop), the J-Link banner stripper, the
stale-session reclaim, and shutdown cleanup — the box-side pieces behind
`lager debug <net> gdbserver --rtt --interactive`.

Driven directly with fake session/socketio objects; flask_socketio is
stubbed (pattern from test_uart_session_cleanup.py). The fake RTT session
mirrors the surface shared by ``lager.debug.RTT`` and ``_OpenOcdRtt``:
``read_some(timeout)`` (bytes | None, never raising on idle), ``write``,
and context-manager exit.
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


rtt_handlers = _load_module(
    "rtt_handlers_ut",
    os.path.join(BOX_DIR, "lager", "http_handlers", "rtt.py"),
)

JLINK_BANNER = (
    b'SEGGER J-Link V7.94a - Real time terminal output\r\n'
    b'J-Link OB-K22-NordicSemi compiled Oct 30 2023\r\n'
    b'Process: JLinkGDBServerCLExe\r\n'
)


class FakeRttSession:
    """RTT session stand-in driven by a script of actions.

    Actions: bytes -> returned from read_some(); None -> returned (idle
    interval / reconnect pending); an Exception instance -> raised; 'stop' ->
    sets the stop_event. An exhausted script also sets the stop_event.
    """

    def __init__(self, script, stop_event):
        self.script = list(script)
        self.stop_event = stop_event
        self.exit_calls = 0
        self.writes = []

    def read_some(self, timeout=1.0):
        if not self.script:
            self.stop_event.set()
            return None
        action = self.script.pop(0)
        if isinstance(action, Exception):
            raise action
        if action == 'stop':
            self.stop_event.set()
            return None
        return action

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.exit_calls += 1
        return False


class FakeSocketIO:
    def __init__(self):
        self.events = []

    def emit(self, event, payload, namespace=None, room=None):
        self.events.append((event, payload))

    def of(self, event_name):
        return [p for (e, p) in self.events if e == event_name]

    def data_bytes(self):
        return b''.join(bytes.fromhex(p['data']) for p in self.of('rtt_data'))


class BannerStripTests(unittest.TestCase):
    def test_strips_three_line_banner(self):
        chunk = JLINK_BANNER + b'real rtt bytes'
        self.assertEqual(
            rtt_handlers._strip_jlink_banner(chunk), b'real rtt bytes')

    def test_banner_only_chunk_becomes_empty(self):
        self.assertEqual(rtt_handlers._strip_jlink_banner(JLINK_BANNER), b'')

    def test_non_banner_data_passes_through(self):
        chunk = b'\x01\x02plain defmt frame'
        self.assertEqual(rtt_handlers._strip_jlink_banner(chunk), chunk)


class RttReadLoopTests(unittest.TestCase):
    SID = 'sid-rtt-1'

    def setUp(self):
        rtt_handlers.active_rtt_sessions.clear()
        self.stop_event = threading.Event()
        self.sio = FakeSocketIO()

    def tearDown(self):
        rtt_handlers.active_rtt_sessions.clear()

    def _register(self, session):
        rtt_handlers.active_rtt_sessions[self.SID] = {
            'session': session,
            'stop_event': self.stop_event,
            'netname': 'dbg',
            'serial': '000051014439',
            'channel': 0,
        }

    def _run(self, session, strip_banner=False):
        rtt_handlers._rtt_read_loop(
            self.sio, self.SID, 'dbg', session, self.stop_event, strip_banner)

    def test_streams_data(self):
        session = FakeRttSession([b'hello', 'stop'], self.stop_event)
        self._register(session)
        self._run(session)
        self.assertEqual(self.sio.data_bytes(), b'hello')

    def test_none_reads_do_not_terminate(self):
        # read_some returning None is an idle interval (or a reconnect in
        # progress across a flash/reset) and must never end the stream.
        session = FakeRttSession(
            [None, b'a', None, None, b'b', 'stop'], self.stop_event)
        self._register(session)
        self._run(session)
        self.assertEqual(self.sio.data_bytes(), b'ab')

    def test_jlink_banner_stripped_from_first_data(self):
        session = FakeRttSession(
            [JLINK_BANNER + b'boot log', b'more', 'stop'], self.stop_event)
        self._register(session)
        self._run(session, strip_banner=True)
        self.assertEqual(self.sio.data_bytes(), b'boot logmore')

    def test_banner_only_first_chunk_skipped_entirely(self):
        session = FakeRttSession(
            [JLINK_BANNER, b'after', 'stop'], self.stop_event)
        self._register(session)
        self._run(session, strip_banner=True)
        self.assertEqual(self.sio.data_bytes(), b'after')

    def test_openocd_data_never_stripped(self):
        # OpenOCD emits no banner; payload that happens to start with
        # 'SEGGER' must pass through untouched.
        chunk = b'SEGGER-lookalike payload\r\nx\r\ny\r\nz'
        session = FakeRttSession([chunk, 'stop'], self.stop_event)
        self._register(session)
        self._run(session, strip_banner=False)
        self.assertEqual(self.sio.data_bytes(), chunk)

    def test_read_error_emits_error_and_evicts(self):
        session = FakeRttSession([ValueError('boom')], self.stop_event)
        self._register(session)
        self._run(session)
        errors = self.sio.of('error')
        self.assertEqual(len(errors), 1)
        self.assertIn('RTT read error', errors[0]['message'])
        self.assertNotIn(self.SID, rtt_handlers.active_rtt_sessions)
        self.assertGreaterEqual(session.exit_calls, 1)

    def test_clean_stop_evicts_and_closes_session(self):
        session = FakeRttSession(['stop'], self.stop_event)
        self._register(session)
        self._run(session)
        self.assertNotIn(self.SID, rtt_handlers.active_rtt_sessions)
        self.assertGreaterEqual(session.exit_calls, 1)

    def test_eviction_is_identity_guarded(self):
        # A replacement session under the same sid must not be evicted by the
        # old thread's teardown.
        session = FakeRttSession([ValueError('boom')], self.stop_event)
        other = FakeRttSession([], threading.Event())
        rtt_handlers.active_rtt_sessions[self.SID] = {
            'session': other,
            'netname': 'dbg',
        }
        self._run(session)
        self.assertIn(self.SID, rtt_handlers.active_rtt_sessions)
        self.assertIs(
            rtt_handlers.active_rtt_sessions[self.SID]['session'], other)
        # The exiting thread still closed its own session
        self.assertGreaterEqual(session.exit_calls, 1)

    def test_tail_buffer_flushed_on_exit(self):
        # Data read in the same iteration the stop lands must still reach the
        # client (finally-block flush).
        session = FakeRttSession([b'tail'], self.stop_event)
        # Exhausting the script sets stop_event, so 'tail' is buffered and
        # the loop exits on the next iteration before the 50ms emit interval.
        self._register(session)
        self._run(session)
        self.assertEqual(self.sio.data_bytes(), b'tail')

    def test_read_loop_refreshes_heartbeat(self):
        stop = threading.Event()
        started = threading.Event()

        class BlockingSession(FakeRttSession):
            def read_some(self, timeout=1.0):
                started.set()
                for _ in range(200):
                    if stop.is_set():
                        return None
                    time.sleep(0.005)
                return None

        session = BlockingSession([], stop)
        rtt_handlers.active_rtt_sessions[self.SID] = {
            'session': session, 'stop_event': stop, 'netname': 'dbg',
            'last_activity': time.monotonic() - 999,  # begins stale
        }
        t = threading.Thread(
            target=rtt_handlers._rtt_read_loop,
            args=(self.sio, self.SID, 'dbg', session, stop, False),
            daemon=True)
        t.start()
        try:
            self.assertTrue(started.wait(2.0), "read loop never started")
            last = rtt_handlers.active_rtt_sessions[self.SID].get('last_activity')
            self.assertIsNotNone(last, "heartbeat was never set")
            self.assertLess(time.monotonic() - last,
                            rtt_handlers.STALE_SESSION_TIMEOUT,
                            "heartbeat should be fresh while the loop runs")
        finally:
            stop.set()
            t.join(2.0)
        self.assertFalse(t.is_alive(), "loop did not exit on stop")


class StaleSessionReclaimTests(unittest.TestCase):
    SID = 'sid-rtt-stale'

    def setUp(self):
        rtt_handlers.active_rtt_sessions.clear()
        self.STALE = rtt_handlers.STALE_SESSION_TIMEOUT

    def tearDown(self):
        rtt_handlers.active_rtt_sessions.clear()

    @staticmethod
    def _dead_thread():
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        return t

    def test_stale_when_thread_dead(self):
        session = {'session': FakeRttSession([], threading.Event()),
                   'netname': 'dbg', 'thread': self._dead_thread(),
                   'last_activity': time.monotonic()}
        self.assertTrue(rtt_handlers._session_is_stale(session))

    def test_stale_when_heartbeat_aged(self):
        session = {'session': FakeRttSession([], threading.Event()),
                   'netname': 'dbg',
                   'last_activity': time.monotonic() - (self.STALE + 5)}
        self.assertTrue(rtt_handlers._session_is_stale(session))

    def test_setup_window_not_stale(self):
        session = {'session': FakeRttSession([], threading.Event()),
                   'netname': 'dbg', 'last_activity': time.monotonic()}
        self.assertFalse(rtt_handlers._session_is_stale(session))

    def test_reclaim_tears_down_stale(self):
        stop = threading.Event()
        rtt_session = FakeRttSession([], stop)
        session = {'session': rtt_session, 'stop_event': stop, 'netname': 'dbg',
                   'last_activity': time.monotonic() - (self.STALE + 5)}
        rtt_handlers.active_rtt_sessions[self.SID] = session

        self.assertTrue(rtt_handlers._reclaim_if_stale(self.SID, session))
        self.assertNotIn(self.SID, rtt_handlers.active_rtt_sessions)
        self.assertTrue(stop.is_set())
        self.assertGreaterEqual(rtt_session.exit_calls, 1)

    def test_reclaim_keeps_live_session(self):
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        stop = threading.Event()
        rtt_session = FakeRttSession([], threading.Event())
        try:
            session = {'session': rtt_session, 'stop_event': stop,
                       'netname': 'dbg', 'thread': alive,
                       'last_activity': time.monotonic()}
            rtt_handlers.active_rtt_sessions[self.SID] = session

            self.assertFalse(rtt_handlers._reclaim_if_stale(self.SID, session))
            self.assertIn(self.SID, rtt_handlers.active_rtt_sessions)
            self.assertFalse(stop.is_set())
            self.assertEqual(rtt_session.exit_calls, 0)
        finally:
            gate.set()
            alive.join(1.0)


class FakeManager:
    """Stand-in for python-socketio's connection manager."""

    def __init__(self, connected):
        self.connected = set(connected)
        self.queried = []

    def is_connected(self, sid, namespace):
        self.queried.append((sid, namespace))
        return sid in self.connected


class ManagedSocketIO(FakeSocketIO):
    """FakeSocketIO that also exposes a `.server.manager`, as the real one does."""

    def __init__(self, connected):
        super().__init__()
        self.manager = FakeManager(connected)
        self.server = types.SimpleNamespace(manager=self.manager)


class DepartedClientTests(unittest.TestCase):
    """A client can vanish without Socket.IO noticing.

    `disconnect` only fires once the transport is known to be gone. If the
    client's host suspends or its network drops, that takes until the engine.io
    ping timeout (the box configures 25s interval + 60s timeout). The read
    loop's own heartbeat cannot stand in for this: the loop stays healthy and
    keeps refreshing it, so `_session_is_stale` is False the whole time and the
    session holds its RTT port against every new start.
    """

    SID = 'sid-departed'

    def setUp(self):
        rtt_handlers.active_rtt_sessions.clear()

    def tearDown(self):
        rtt_handlers.active_rtt_sessions.clear()

    def test_client_gone_reports_disconnected_sid(self):
        sio = ManagedSocketIO(connected=['someone-else'])
        self.assertTrue(rtt_handlers._client_gone(sio, self.SID))

    def test_client_gone_reports_connected_sid(self):
        sio = ManagedSocketIO(connected=[self.SID])
        self.assertFalse(rtt_handlers._client_gone(sio, self.SID))
        self.assertEqual(sio.manager.queried, [(self.SID, '/rtt')])

    def test_unintrospectable_socketio_never_reports_gone(self):
        # No `.server`: a fake in tests, or an async_mode without a manager.
        # An unknown answer must never tear down a live session.
        self.assertFalse(rtt_handlers._client_gone(FakeSocketIO(), self.SID))

    def test_manager_error_never_reports_gone(self):
        class Exploding:
            def is_connected(self, sid, namespace):
                raise RuntimeError('manager blew up')

        sio = FakeSocketIO()
        sio.server = types.SimpleNamespace(manager=Exploding())
        self.assertFalse(rtt_handlers._client_gone(sio, self.SID))

    def test_read_loop_exits_and_frees_port_when_client_departs(self):
        """The regression: an endlessly-readable session whose client is gone.

        Before the liveness check this loop ran forever, kept its heartbeat
        fresh, and blocked every `start_rtt` on port 9090 until the ping
        timeout expired.
        """
        stop = threading.Event()

        class EndlessSession(FakeRttSession):
            def read_some(self, timeout=1.0):
                time.sleep(0.01)
                return None          # idle: normal, never ends the loop

        session = EndlessSession([], stop)
        rtt_handlers.active_rtt_sessions[self.SID] = {
            'session': session, 'stop_event': stop, 'netname': 'dbg',
            'serial': 'JLINK123', 'channel': 0, 'rtt_port': 9090,
            'last_activity': time.monotonic(),
        }
        sio = ManagedSocketIO(connected=[])   # client already gone

        t = threading.Thread(
            target=rtt_handlers._rtt_read_loop,
            args=(sio, self.SID, 'dbg', session, stop, False),
            daemon=True)
        t.start()
        t.join(5.0)

        self.assertFalse(t.is_alive(), "loop did not exit for a departed client")
        self.assertNotIn(self.SID, rtt_handlers.active_rtt_sessions,
                         "departed client's session was not evicted")
        self.assertGreaterEqual(session.exit_calls, 1,
                                "RTT telnet port was not released")
        self.assertIsNone(rtt_handlers._find_port_conflict(9090),
                          "port 9090 still blocks a new session")

    def test_read_loop_keeps_running_while_client_connected(self):
        stop = threading.Event()
        reads = threading.Event()

        class EndlessSession(FakeRttSession):
            def read_some(self, timeout=1.0):
                reads.set()
                time.sleep(0.01)
                return None

        session = EndlessSession([], stop)
        rtt_handlers.active_rtt_sessions[self.SID] = {
            'session': session, 'stop_event': stop, 'netname': 'dbg',
            'rtt_port': 9090, 'last_activity': time.monotonic(),
        }
        sio = ManagedSocketIO(connected=[self.SID])

        t = threading.Thread(
            target=rtt_handlers._rtt_read_loop,
            args=(sio, self.SID, 'dbg', session, stop, False),
            daemon=True)
        t.start()
        try:
            self.assertTrue(reads.wait(2.0), "loop never read")
            time.sleep(0.1)
            self.assertTrue(t.is_alive(),
                            "loop exited even though the client is connected")
            self.assertIn(self.SID, rtt_handlers.active_rtt_sessions)
        finally:
            stop.set()
            t.join(2.0)


class PortConflictTests(unittest.TestCase):
    """The guard is keyed on the RTT telnet port, the single-client resource.

    Both backends listen at ``rtt_telnet_port + channel``, and the base comes
    from the probe's slot (``9090 + 2 * slot``), so the port — not the net name
    — is what two sessions can genuinely fight over.
    """

    def setUp(self):
        rtt_handlers.active_rtt_sessions.clear()

    def tearDown(self):
        rtt_handlers.active_rtt_sessions.clear()

    def _live(self, sid, netname, rtt_port):
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait)
        alive.start()
        self.addCleanup(lambda: (gate.set(), alive.join(1.0)))
        rtt_handlers.active_rtt_sessions[sid] = {
            'session': FakeRttSession([], threading.Event()),
            'stop_event': threading.Event(), 'netname': netname,
            'rtt_port': rtt_port, 'thread': alive,
            'last_activity': time.monotonic(),
        }

    def test_port_for_channel_offsets_the_slot_base(self):
        dbg = types.SimpleNamespace(rtt_telnet_port=9092)   # slot 1
        self.assertEqual(rtt_handlers._rtt_port_for(dbg, 0), 9092)
        self.assertEqual(rtt_handlers._rtt_port_for(dbg, 1), 9093)

    def test_port_unknown_when_attribute_absent(self):
        self.assertIsNone(
            rtt_handlers._rtt_port_for(types.SimpleNamespace(), 0))

    def test_same_port_conflicts(self):
        self._live('sid-a', 'netA', 9090)
        self.assertEqual(rtt_handlers._find_port_conflict(9090), 'sid-a')

    def test_other_channel_on_same_net_is_a_different_port(self):
        # What --rtt-channel is for: channel 0 and channel 1 of one net are
        # separate ports and must both be able to run.
        self._live('sid-a', 'netA', 9090)
        self.assertIsNone(rtt_handlers._find_port_conflict(9091))

    def test_other_probe_is_a_different_port(self):
        self._live('sid-a', 'netA', 9090)          # slot 0
        self.assertIsNone(rtt_handlers._find_port_conflict(9092))  # slot 1

    def test_serial_less_nets_share_slot_zero_and_conflict(self):
        # allocate_probe_slot() returns 0 for any falsy serial, so two
        # serial-less nets really do land on the same port. Blocking is right.
        self._live('sid-a', 'netA', 9090)
        self.assertEqual(rtt_handlers._find_port_conflict(9090), 'sid-a')

    def test_unknown_ports_conflict_with_each_other(self):
        self._live('sid-a', 'netA', None)
        self.assertEqual(rtt_handlers._find_port_conflict(None), 'sid-a')

    def test_stale_holder_is_reclaimed_instead_of_blocking(self):
        stop = threading.Event()
        rtt_session = FakeRttSession([], stop)
        rtt_handlers.active_rtt_sessions['sid-stale'] = {
            'session': rtt_session, 'stop_event': stop, 'netname': 'netA',
            'rtt_port': 9090,
            'last_activity': (time.monotonic()
                              - rtt_handlers.STALE_SESSION_TIMEOUT - 5),
        }
        self.assertIsNone(rtt_handlers._find_port_conflict(9090))
        self.assertNotIn('sid-stale', rtt_handlers.active_rtt_sessions)
        self.assertTrue(stop.is_set())
        self.assertGreaterEqual(rtt_session.exit_calls, 1)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        rtt_handlers.active_rtt_sessions.clear()

    def tearDown(self):
        rtt_handlers.active_rtt_sessions.clear()

    def test_cleanup_closes_all_sessions(self):
        stops = []
        sessions = []
        for i in range(3):
            stop = threading.Event()
            rtt_session = FakeRttSession([], stop)
            stops.append(stop)
            sessions.append(rtt_session)
            rtt_handlers.active_rtt_sessions[f'sid-{i}'] = {
                'session': rtt_session, 'stop_event': stop, 'netname': f'dbg{i}',
            }

        rtt_handlers.cleanup_rtt_sessions()

        self.assertEqual(rtt_handlers.active_rtt_sessions, {})
        for stop in stops:
            self.assertTrue(stop.is_set())
        for rtt_session in sessions:
            self.assertGreaterEqual(rtt_session.exit_calls, 1)


if __name__ == "__main__":
    unittest.main()

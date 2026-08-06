# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
RTT WebSocket handlers for the Lager Box server.

Bi-directional RTT (SEGGER Real-Time Transfer) over a Socket.IO namespace,
mirroring the interactive UART pattern in ``http_handlers/uart.py``:

* ``start_rtt``  -- attach to a debug net's RTT telnet port (up + down channel)
* ``rtt_data``   -- server -> client: raw RTT up-channel bytes (hex encoded)
* ``rtt_write``  -- client -> server: raw bytes for the RTT down-channel (hex)
* ``stop_rtt``   -- tear down the session

The heavy lifting (control-block detection, J-Link vs OpenOCD dispatch,
reconnect-across-flash) is all in ``DebugNet.rtt()`` — this module only
bridges that session object to a WebSocket so remote clients (``lager debug
<net> gdbserver --rtt --interactive``) get the same read/write surface that
on-box Python scripts already have.

The gdbserver must already be running for the net's probe (the CLI starts it
via ``POST /debug/connect`` on the debug service before connecting here); this
handler never starts or stops a gdbserver itself.
"""
import logging
import threading
import time

from flask_socketio import SocketIO, emit
from flask import request

logger = logging.getLogger(__name__)

# Global dictionary to track active RTT sessions
# Format: {session_id: {'session': rtt_session, 'thread': thread_obj,
#                       'stop_event': event_obj, 'netname': str,
#                       'serial': str, 'channel': int, 'last_activity': float}}
active_rtt_sessions = {}
active_rtt_sessions_lock = threading.Lock()

# A live read loop refreshes its session's 'last_activity' every iteration
# (~0.1s). If a session's heartbeat ages past this, the loop is no longer
# making progress and the session is a phantom: still registered, still
# holding its per-net/per-probe guard, with no live reader behind it.
# Reclaiming it lets a fresh `start_rtt` for the same net succeed instead of
# hitting a permanent "already in use". Must stay comfortably above the
# loop's iteration period. (Same rationale as uart.py's STALE_SESSION_TIMEOUT;
# RTT's read_some can block up to ~0.25s while a gdbserver bounce heals, so
# the same 30s bound is safely conservative.)
STALE_SESSION_TIMEOUT = 30.0


def _strip_jlink_banner(data_chunk):
    """Strip the J-Link RTT telnet greeting from the first data of a stream.

    J-Link's RTTTelnetPort prefixes every new client connection with
    "SEGGER J-Link V... - Real time terminal output\\r\\n...\\r\\nProcess:
    JLinkGDBServerCLExe\\r\\n" (3 lines). OpenOCD's ``rtt server`` emits no
    banner. Downstream consumers (defmt-print etc.) must see clean RTT bytes,
    so the banner is dropped here — same heuristic as the HTTP streaming path
    in ``debug/service.py``.

    Returns the chunk with the banner removed (possibly ``b''``).
    """
    if data_chunk.startswith(b'SEGGER') or b'terminal output' in data_chunk:
        lines = data_chunk.split(b'\r\n')
        if len(lines) >= 3:
            data_chunk = b'\r\n'.join(lines[3:])
    if data_chunk == b'\r\n':
        return b''
    return data_chunk


def _rtt_read_loop(socketio, session_id, netname, rtt_session, stop_event,
                   strip_banner):
    """Body of the per-session RTT read thread.

    Module-level (rather than a closure in handle_start_rtt) so the cleanup
    behavior is unit-testable. Guarantees, mirroring ``_uart_read_loop``:

    - However the loop exits, it evicts the session it owns from
      active_rtt_sessions and closes the RTT session (releasing the
      single-client RTT telnet port so a new session can attach).
    - ``read_some`` returning ``None`` is normal (idle interval, or the
      reader is transparently re-attaching across a flash/reset gdbserver
      bounce) and never terminates the loop.
    """
    read_buffer = bytearray()
    last_emit_time = time.time()
    BUFFER_SIZE = 4096
    EMIT_INTERVAL = 0.05  # Emit every 50ms to allow batching

    # J-Link prepends its telnet greeting on every fresh telnet connection;
    # strip until the first real RTT bytes have flowed.
    banner_pending = strip_banner

    def emit_buffer():
        nonlocal last_emit_time
        socketio.emit('rtt_data',
                      {'data': bytes(read_buffer).hex()},
                      namespace='/rtt',
                      room=session_id)
        read_buffer.clear()
        last_emit_time = time.time()

    def touch():
        # Heartbeat: prove this session's read loop is still making progress
        # so a live session is never mistaken for a wedged one. A single dict
        # get/set is atomic under the GIL; identity-guard so a session that
        # stop_rtt/disconnect already replaced under this sid isn't touched
        # by the old thread.
        sess = active_rtt_sessions.get(session_id)
        if sess is not None and sess.get('session') is rtt_session:
            sess['last_activity'] = time.monotonic()

    try:
        while not stop_event.is_set():
            touch()
            try:
                data = rtt_session.read_some(timeout=0.1)
            except Exception as e:
                logger.error(f"Error reading RTT: {e}")
                socketio.emit('error',
                              {'message': f'RTT read error: {str(e)}'},
                              namespace='/rtt',
                              room=session_id)
                break

            if data:
                if banner_pending:
                    data = _strip_jlink_banner(data)
                    banner_pending = False
                    if not data:
                        continue
                read_buffer.extend(data)

            should_emit = (
                len(read_buffer) > 0 and (
                    len(read_buffer) >= BUFFER_SIZE or
                    (time.time() - last_emit_time) >= EMIT_INTERVAL
                )
            )
            if should_emit:
                emit_buffer()
    finally:
        # Flush whatever is left so the client sees the tail of the stream.
        if read_buffer:
            try:
                emit_buffer()
            except Exception:  # noqa: BLE001 — teardown must not raise
                pass
        # Evict the session we own (identity-guarded: stop_rtt/disconnect may
        # already have replaced or removed it) and release the telnet port.
        with active_rtt_sessions_lock:
            sess = active_rtt_sessions.get(session_id)
            if sess is not None and sess.get('session') is rtt_session:
                del active_rtt_sessions[session_id]
        try:
            rtt_session.__exit__(None, None, None)
        except Exception as e:
            logger.error(f"Error closing RTT session for {session_id}: {e}")
        logger.info(f"RTT read thread stopped for session {session_id}")


def _session_is_stale(session) -> bool:
    """True if *session* has no live read loop behind it.

    Same shape as uart.py's staleness check: a dead thread, or a heartbeat
    older than STALE_SESSION_TIMEOUT (loop wedged). A session still being set
    up (thread not yet stored, heartbeat seeded at creation) reads as NOT
    stale, so an in-flight legitimate start is never reclaimed.
    """
    thread = session.get('thread')
    if thread is not None and not thread.is_alive():
        return True
    last = session.get('last_activity')
    if last is not None and (time.monotonic() - last) > STALE_SESSION_TIMEOUT:
        return True
    return False


def _reclaim_if_stale(session_id, session) -> bool:
    """Tear down *session* if it has no live reader; report whether it blocks.

    Call while holding active_rtt_sessions_lock. Returns True when the
    session was stale and has been reclaimed (its net/probe is now free) so
    the caller should NOT reject the incoming start; False when the session
    is genuinely live and must still block a colliding start.
    """
    if not _session_is_stale(session):
        return False
    logger.warning(
        "Reclaiming stale RTT session %s (netname=%s): no live read loop; "
        "releasing its RTT port so a new session can start",
        session_id, session.get('netname'))
    stop_event = session.get('stop_event')
    if stop_event is not None:
        stop_event.set()
    rtt_session = session.get('session')
    if rtt_session is not None:
        try:
            rtt_session.__exit__(None, None, None)
        except Exception as e:
            logger.error(
                "Error cleaning up reclaimed RTT session %s: %s", session_id, e)
    active_rtt_sessions.pop(session_id, None)
    return True


def _find_debug_net(netname):
    """Return the saved-net record for *netname* (role 'debug'), or None."""
    from lager.core import get_saved_nets
    for rec in get_saved_nets():
        if rec.get('role') == 'debug' and rec.get('name') == netname:
            return rec
    return None


def register_rtt_socketio(socketio: SocketIO) -> None:
    """
    Register RTT WebSocket handlers with SocketIO.

    Args:
        socketio: Flask-SocketIO instance
    """

    @socketio.on('connect', namespace='/rtt')
    def handle_rtt_connect():
        """Handle WebSocket connection for RTT."""
        logger.info(f"RTT WebSocket client connected: {request.sid}")
        emit('connected', {'status': 'ready', 'session_id': request.sid})

    @socketio.on('disconnect', namespace='/rtt')
    def handle_rtt_disconnect():
        """Handle WebSocket disconnection for RTT."""
        logger.info(f"RTT WebSocket client disconnected: {request.sid}")
        with active_rtt_sessions_lock:
            session = active_rtt_sessions.pop(request.sid, None)
        if session is None:
            return
        if 'stop_event' in session:
            session['stop_event'].set()
        if session.get('session') is not None:
            try:
                session['session'].__exit__(None, None, None)
            except Exception as e:
                logger.error(f"Error cleaning up RTT session: {e}")
        logger.info(f"Cleaned up RTT session: {request.sid}")

    @socketio.on('start_rtt', namespace='/rtt')
    def handle_start_rtt(data):
        """
        Start bi-directional RTT session.

        Expected data:
        {
            "netname": "dbg",
            "channel": 0,                # optional, default 0
            "search_addr": 536870912,    # optional, RTT control-block search
            "search_size": 65536,        # optional
            "chunk_size": 4096           # optional (J-Link only)
        }
        """
        try:
            from lager.nets.debug_net import make_debug, _debug_available

            netname = (data or {}).get('netname')
            channel = (data or {}).get('channel', 0)
            search_addr = (data or {}).get('search_addr')
            search_size = (data or {}).get('search_size')
            chunk_size = (data or {}).get('chunk_size')

            if not netname:
                emit('error', {'message': 'netname is required'})
                return
            if not _debug_available:
                emit('error', {'message': 'Debug module not available on this box'})
                return

            net_info = _find_debug_net(netname)
            if net_info is None:
                emit('error', {'message': f"Debug net '{netname}' not found"})
                return

            try:
                dbg = make_debug(netname, net_info)
            except Exception as e:
                emit('error', {'message': f"Invalid debug net '{netname}': {e}"})
                return

            # Per-connection + per-probe guard. The RTT telnet port accepts a
            # single client, so a second session on the same probe+channel
            # would either be refused or silently steal the stream. Sessions
            # with no live read loop behind them are reclaimed rather than
            # blocking forever (same healing as UART).
            with active_rtt_sessions_lock:
                existing = active_rtt_sessions.get(request.sid)
                if existing is not None and not _reclaim_if_stale(request.sid, existing):
                    emit('error', {'message': 'RTT session already active'})
                    return
                # list(): _reclaim_if_stale may pop entries as we scan.
                for sid, sess in list(active_rtt_sessions.items()):
                    same_net = sess.get('netname') == netname
                    same_probe = (sess.get('serial') == dbg.serial
                                  and sess.get('channel') == channel)
                    if (same_net or same_probe) and not _reclaim_if_stale(sid, sess):
                        emit('error', {'message': (
                            f"RTT for net '{netname}' is already in use by another session")})
                        return

            # The gdbserver must already be up (the CLI starts it through the
            # debug service's /debug/connect before connecting here). Check
            # explicitly so the user gets an actionable message instead of a
            # telnet connect failure.
            try:
                running = bool(dbg.status().get('running'))
            except Exception:
                running = False
            if not running:
                emit('error', {'message': (
                    f"No debugger connection found for net '{netname}'. "
                    f"Start one first: lager debug {netname} gdbserver")})
                return

            # Attach to the RTT telnet port. DebugNet.rtt() handles J-Link vs
            # OpenOCD dispatch, control-block detection, and retries.
            try:
                rtt_session = dbg.rtt(
                    channel=channel,
                    search_addr=search_addr,
                    search_size=search_size,
                    chunk_size=chunk_size,
                )
                rtt_session.__enter__()
            except Exception as e:
                emit('error', {'message': f'Failed to start RTT: {str(e)}'})
                return

            stop_event = threading.Event()
            session_id = request.sid  # request context unavailable in threads

            with active_rtt_sessions_lock:
                active_rtt_sessions[session_id] = {
                    'session': rtt_session,
                    'stop_event': stop_event,
                    'netname': netname,
                    'serial': dbg.serial,
                    'channel': channel,
                    'last_activity': time.monotonic(),
                }

            emit('rtt_connected', {
                'netname': netname,
                'channel': channel,
                'backend': dbg.backend,
                'message': f'RTT attached to {netname} (channel {channel}, {dbg.backend})',
            })

            # Only J-Link's telnet server emits a greeting banner.
            strip_banner = (dbg.backend != 'openocd')
            read_thread = threading.Thread(
                target=_rtt_read_loop,
                args=(socketio, session_id, netname, rtt_session, stop_event,
                      strip_banner),
                daemon=True)
            read_thread.start()

            with active_rtt_sessions_lock:
                if session_id in active_rtt_sessions:
                    active_rtt_sessions[session_id]['thread'] = read_thread

        except Exception as e:
            logger.exception("Error in handle_start_rtt")
            emit('error', {'message': str(e)})

    @socketio.on('rtt_write', namespace='/rtt')
    def handle_rtt_write(data):
        """
        Write data to the target's RTT down-channel.

        Expected data:
        {
            "data": "hex_string"  # Data to write as hex string
        }
        """
        try:
            with active_rtt_sessions_lock:
                session = active_rtt_sessions.get(request.sid)
                if session is None:
                    emit('error', {'message': 'No active RTT session'})
                    return
                rtt_session = session['session']

            hex_data = (data or {}).get('data', '')
            if not hex_data:
                return

            try:
                bytes_data = bytes.fromhex(hex_data)
            except ValueError as e:
                emit('error', {'message': f'Invalid hex data: {str(e)}'})
                return

            try:
                rtt_session.write(bytes_data)
            except Exception as e:
                logger.error(f"Error writing to RTT: {e}")
                emit('error', {'message': f'Write error: {str(e)}'})

        except Exception as e:
            logger.exception("Error in handle_rtt_write")
            emit('error', {'message': str(e)})

    @socketio.on('stop_rtt', namespace='/rtt')
    def handle_stop_rtt():
        """Stop the RTT session."""
        try:
            with active_rtt_sessions_lock:
                session = active_rtt_sessions.pop(request.sid, None)
            if session is not None:
                if 'stop_event' in session:
                    session['stop_event'].set()
                if session.get('session') is not None:
                    try:
                        session['session'].__exit__(None, None, None)
                    except Exception as e:
                        logger.error(f"Error closing RTT session: {e}")

            emit('rtt_stopped', {'message': 'RTT session stopped'})
            logger.info(f"RTT session stopped: {request.sid}")

        except Exception as e:
            logger.exception("Error in handle_stop_rtt")
            emit('error', {'message': str(e)})


def cleanup_rtt_sessions() -> None:
    """
    Clean up all active RTT sessions.

    Called during server shutdown to close all RTT telnet connections and
    stop all reading threads.
    """
    with active_rtt_sessions_lock:
        for session_id, session in list(active_rtt_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
                if session.get('session') is not None:
                    session['session'].__exit__(None, None, None)
            except Exception as e:
                logger.error(f"Error cleaning up RTT session {session_id}: {e}")
        active_rtt_sessions.clear()

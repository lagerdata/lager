# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART HTTP and WebSocket handlers for the Lager Box server.

This module contains all UART-related HTTP endpoints and WebSocket handlers,
extracted from box_http_server.py for better modularity.
"""
import logging
import threading
import time

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)

# Global dictionary to track active UART sessions
# Format: {session_id: {'driver': driver_obj, 'thread': thread_obj, 'stop_event': event_obj}}
active_uart_sessions = {}
active_uart_sessions_lock = threading.Lock()

# The SocketIO instance the /uart namespace is registered on, stashed by
# register_uart_socketio(). The HTTP session routes need it to answer "is that
# holder's client still connected?" via _client_gone(), and Flask routes get no
# socketio argument of their own. None until the namespace is registered (and
# in tests that only exercise the HTTP layer), which reads as "liveness
# unknown" rather than an error.
_socketio = None

# How long a live session waits for a re-enumerating device to come back
# before giving up and reporting a terminal error.
UART_RECONNECT_TIMEOUT = 60.0

# A live read loop refreshes its session's 'last_activity' every iteration
# (~0.1s, and at least every 0.25s while a device re-enumerates). If a
# session's heartbeat ages past this, the loop is no longer making progress —
# wedged in a blocking kernel read on a USB-serial adapter that vanished
# without raising a device-gone error, so the reconnect/eviction paths never
# ran. Such a session is a phantom: still registered, still holding its
# per-net/per-device guard, with no live reader behind it. Reclaiming it lets
# a fresh `start_uart` for the same net succeed instead of hitting a permanent
# "already in use". Must stay comfortably above the loop's iteration period so
# a merely-slow (not wedged) loop is never misjudged.
#
# This covers a *wedged reader* only. It cannot detect a departed client: the
# heartbeat is written by the read loop itself, so a client that dies leaves a
# perfectly healthy loop refreshing it forever. That case is handled by
# _client_gone() below, not here. (Same split as rtt.py's.)
STALE_SESSION_TIMEOUT = 30.0


def _readable_open_error(netname: str, driver, exc: Exception) -> str:
    """Turn a serial-open failure into a human message.

    The UART bridge opens the port with exclusive=True (pyserial flock), so a
    second opener — another dashboard socket.io session or `lager uart` from the
    CLI while a Workbench session is live — fails the lock instead of silently
    interleaving reads. pyserial surfaces that as a SerialException whose text
    carries a raw errno (EAGAIN/EWOULDBLOCK = 11, EBUSY = 16); detect those and
    return a clear "in use" message rather than leaking the errno to the UI.
    """
    target = getattr(driver, 'device_path', None) or f"net '{netname}'"
    text = str(exc)
    low = text.lower()
    if ('lock' in low or 'errno 11' in low or 'errno 16' in low
            or 'resource temporarily unavailable' in low or 'busy' in low):
        return (f"UART device {target} is already in use "
                f"(locked by another session or the `lager uart` CLI)")
    return f"Failed to open UART {target}: {text}"


def _uart_read_loop(socketio, session_id, netname, driver, stop_event):
    """Body of the per-session UART read thread.

    Module-level (rather than a closure in handle_start_uart) so the
    reconnect and cleanup behavior is unit-testable.

    Two guarantees this loop must keep:
    - A device re-enumeration (hub power-cycle, DUT reflash, replug) heals in
      place: the dead fd is closed, the device is re-resolved by its durable
      USB identity, and streaming resumes — with `uart_status` events telling
      the client what is happening (old CLIs silently drop the unknown event).
    - However the loop exits, it evicts the session it owns from
      active_uart_sessions and closes the port. Leaving the fd open pinned
      the old tty number (forcing the device back under a new one) and left
      the per-net/per-device guards in handle_start_uart wedged on a dead
      session until the socket dropped.
    """
    # Buffer for accumulating data before emitting
    read_buffer = bytearray()
    last_emit_time = time.time()
    BUFFER_SIZE = 4096  # Read up to 4KB at a time
    EMIT_INTERVAL = 0.05  # Emit every 50ms to allow proper batching

    def emit_buffer():
        nonlocal last_emit_time
        data_to_emit = bytes(read_buffer)
        if driver.opost:
            data_to_emit = data_to_emit.replace(b'\n', b'\r\n')
        socketio.emit('uart_data',
                      {'data': data_to_emit.hex()},
                      namespace='/uart',
                      room=session_id)
        read_buffer.clear()
        last_emit_time = time.time()

    def emit_status(status, **extra):
        payload = {'status': status, 'netname': netname}
        payload.update(extra)
        socketio.emit('uart_status', payload, namespace='/uart', room=session_id)

    def touch():
        # Heartbeat: prove this session's read loop is still making progress so
        # a live session is never mistaken for a wedged one. Monotonic clock:
        # last_activity is only ever read as an elapsed interval, so it must not
        # step with NTP/manual clock changes (a forward jump could otherwise
        # falsely age a live session). A single dict get/set is atomic under the
        # GIL, so no lock is needed; identity-guard so a session that
        # stop_uart/disconnect already replaced under this sid isn't touched by
        # the old thread.
        sess = active_uart_sessions.get(session_id)
        if sess is not None and sess.get('driver') is driver:
            sess['last_activity'] = time.monotonic()

    def reconnect_stop_check():
        # reconnect() polls this every <=0.25s; piggyback the heartbeat so a
        # genuine 60s re-enumeration keeps the session fresh and un-reclaimable.
        #
        # Deliberately does NOT consult _client_gone(): a re-enumeration is
        # exactly when the manager's answer is least worth acting on, and
        # aborting here would turn a healable hiccup into a dropped session.
        # A client that left during a reconnect is caught by the check at the
        # top of the loop as soon as reconnect() returns, so the net is held
        # for at most one reconnect budget rather than indefinitely.
        touch()
        return stop_event.is_set()

    try:
        while not stop_event.is_set():
            touch()
            # Before reading more: is anyone still there? Emitting into an
            # empty room is a silent no-op, so without this a session whose
            # `disconnect` already ran before it was registered would stream
            # happily to nobody and hold the net -- and the exclusive flock on
            # the tty -- until the box restarted. See _client_gone().
            if _client_gone(socketio, session_id):
                logger.info(
                    "UART client %s is gone; releasing net %s (device %s)",
                    session_id, netname, driver.device_path)
                break
            try:
                # Read data with consistent buffer size
                waiting = driver.serial_conn.in_waiting

                if waiting > 0:
                    # Read all available data (up to BUFFER_SIZE to prevent memory issues)
                    read_size = min(waiting, BUFFER_SIZE)
                    data = driver.serial_conn.read(read_size)

                    if data:
                        read_buffer.extend(data)

                    # After reading, give a short time for more data to arrive
                    # This helps batch rapid bursts of data
                    time.sleep(0.001)  # 1ms delay to let data accumulate

                else:
                    # No data immediately available
                    # Only emit if we have buffered data and interval has passed
                    if len(read_buffer) > 0 and (time.time() - last_emit_time) >= EMIT_INTERVAL:
                        emit_buffer()

                    # Wait for data with timeout (blocking)
                    # This returns after timeout (0.1s) or when data arrives
                    data = driver.serial_conn.read(1)
                    if data:
                        read_buffer.extend(data)

                # Check if we should emit based on buffer size or time interval
                should_emit = (
                    len(read_buffer) > 0 and (
                        len(read_buffer) >= BUFFER_SIZE or  # Buffer full
                        (time.time() - last_emit_time) >= EMIT_INTERVAL  # Time elapsed
                    )
                )

                if should_emit:
                    emit_buffer()

            except Exception as e:
                if driver.is_device_gone(e) and not stop_event.is_set():
                    # The adapter re-enumerated (hub power-cycle, DUT reflash,
                    # replug). Flush what we have, then re-resolve and reopen.
                    logger.warning(
                        f"UART device lost for session {session_id} ({e}); reconnecting")
                    if read_buffer:
                        emit_buffer()
                    emit_status('reconnecting',
                                message='UART device disconnected; waiting for it to re-enumerate...')
                    if driver.reconnect(stop_check=reconnect_stop_check,
                                        total_timeout=UART_RECONNECT_TIMEOUT):
                        emit_status('reconnected',
                                    device_path=driver.device_path,
                                    baudrate=driver.baudrate,
                                    message=f'Reconnected to {driver.device_path}')
                        continue
                    if stop_event.is_set():
                        break
                    socketio.emit('error',
                                  {'message': (
                                      f'UART device did not return after re-enumeration '
                                      f'({int(UART_RECONNECT_TIMEOUT)}s): {str(e)}')},
                                  namespace='/uart',
                                  room=session_id)
                    break
                logger.error(f"Error reading UART: {e}")
                socketio.emit('error',
                              {'message': f'Read error: {str(e)}'},
                              namespace='/uart',
                              room=session_id)
                break
    finally:
        # Evict the session we own (identity-guarded: stop_uart/disconnect may
        # already have replaced or removed it) and release the fd.
        with active_uart_sessions_lock:
            sess = active_uart_sessions.get(session_id)
            if sess is not None and sess.get('driver') is driver:
                del active_uart_sessions[session_id]
        try:
            driver._cleanup()
        except Exception as e:
            logger.error(f"Error cleaning up UART driver for session {session_id}: {e}")
        logger.info(f"UART read thread stopped for session {session_id}")


def _client_gone(socketio, session_id) -> bool:
    """True when *session_id* is no longer connected to the /uart namespace.

    The read loop's heartbeat proves the loop is iterating, not that anyone is
    still listening, so the stale-session reclaim is blind to a client that
    went away: the loop stays healthy, 'last_activity' stays fresh, and the
    session keeps its per-net/per-device guard and the exclusive flock on the
    tty.

    The case this actually rescues is a session registered *after* its own
    client disconnected. `start_uart` and `disconnect` are dispatched on
    different threads (async_mode='threading'), so a client that opens a
    session and closes immediately can have its disconnect handler run first,
    find nothing registered, and return -- and then `start_uart` registers a
    session with no client and no remaining cleanup path. The heartbeat can
    never age (the loop is healthy), `disconnect` has already been and gone,
    and the net stays held until the box restarts. Measured against a box
    without this check: still held after 246s, with no sign of clearing.
    With this check the same race releases the net in ~2ms.

    What it does NOT shorten is a client that stops answering without closing
    (host suspended, Wi-Fi or VPN dropped). The connection manager still
    reports that sid as connected until engine.io's ping timeout expires --
    ping_interval 25s + ping_timeout 60s on this box -- so the net stays held
    for ~85s either way. Measured: 92s unpatched, 89s patched. `lager uart
    --force` is the answer for that one, not this check. (rtt.py's copy of this
    docstring claims it collapses the 85s window; that claim is wrong, and its
    call site has the same shape as this one.)

    Returns False when the manager cannot be introspected (a fake socketio in
    tests, or an async_mode that exposes no manager), so an unknown answer can
    never tear down a live session.

    Counterpart to rtt.py's _client_gone; keep the two in step.
    """
    try:
        manager = socketio.server.manager
    except AttributeError:
        return False
    try:
        return not manager.is_connected(session_id, '/uart')
    except Exception:  # noqa: BLE001 — liveness is advisory, never fatal
        return False


def _session_is_stale(session) -> bool:
    """True if *session* has no live read loop behind it.

    Either signal is sufficient:
    - the read thread has exited (belt-and-suspenders: the loop's finally
      normally evicts on exit, so this catches only an exit that somehow
      skipped eviction), or
    - the heartbeat has aged past STALE_SESSION_TIMEOUT, meaning the loop is no
      longer iterating (thread wedged in a blocking read on a device that went
      away without raising a device-gone error, so neither the reconnect nor
      the eviction path ever ran).

    A session still being set up (thread not yet stored, heartbeat seeded at
    creation) reads as NOT stale, so an in-flight legitimate start is never
    reclaimed out from under itself.

    Says nothing about whether a *client* is still listening — an orphaned but
    healthy loop refreshes the heartbeat forever. See _client_gone().
    """
    thread = session.get('thread')
    if thread is not None and not thread.is_alive():
        return True
    last = session.get('last_activity')
    if last is not None and (time.monotonic() - last) > STALE_SESSION_TIMEOUT:
        return True
    return False


def _release_session(session_id, session, reason: str) -> None:
    """Tear *session* down and free its net/device. Idempotent.

    Call while holding active_uart_sessions_lock. Shared by the stale-session
    reclaim and the operator-driven force release so the two cannot drift —
    they differ only in how they decide, never in how they tear down.
    """
    logger.warning(
        "Releasing UART session %s (netname=%s): %s",
        session_id, session.get('netname'), reason)
    stop_event = session.get('stop_event')
    if stop_event is not None:
        # If the wedged thread ever unblocks, tell it to exit rather than
        # resume reading a device a new session now owns.
        stop_event.set()
    driver = session.get('driver')
    if driver is not None:
        try:
            # Closing the fd here also releases the exclusive flock (and tends
            # to unblock a thread wedged in read()); _cleanup() is idempotent.
            driver._cleanup()
        except Exception as e:
            logger.error(
                "Error cleaning up released UART session %s: %s", session_id, e)
    active_uart_sessions.pop(session_id, None)


def _reclaim_if_stale(session_id, session) -> bool:
    """Tear down *session* if it has no live reader; report whether it blocks.

    Call while holding active_uart_sessions_lock. Returns True when the session
    was stale and has been reclaimed (its net/device is now free to reuse) so
    the caller should NOT reject the incoming start; False when the session is
    genuinely live and must still block a colliding start.
    """
    if not _session_is_stale(session):
        return False
    _release_session(session_id, session,
                     "no live read loop; releasing its device so a new "
                     "session can start")
    return True


def register_uart_routes(app: Flask) -> None:
    """
    Register UART HTTP routes with the Flask app.

    Args:
        app: Flask application instance
    """

    @app.route('/uart/nets/list', methods=['GET'])
    def uart_nets_list():
        """
        List all saved nets on the box.

        Returns:
        {
            "nets": [
                {
                    "name": "uart1",
                    "role": "uart",
                    "instrument": "Prolific_USB_Serial",
                    "pin": "ABCD12345",
                    "channel": "0",
                    "params": {...}
                },
                ...
            ]
        }
        """
        try:
            from lager.core import get_saved_nets
            nets = get_saved_nets()
            return jsonify({'nets': nets})
        except Exception as e:
            logger.exception("Error listing nets")
            return jsonify({'error': str(e), 'nets': []}), 500

    @app.route('/uart/net/stream', methods=['POST'])
    def uart_net_stream():
        """
        Stream UART communication using net configuration (read-only HTTP).

        Request body:
        {
            "netname": "uart_net",
            "overrides": {...},
            "interactive": false
        }
        """
        try:
            from lager.protocols.uart.views import handle_uart_stream
            return handle_uart_stream(request)
        except Exception as e:
            logger.exception("Error in uart_net_stream")
            return jsonify({'error': str(e)}), 500

    @app.route('/uart/sessions', methods=['GET'])
    def uart_sessions_list():
        """
        List the UART sessions currently holding a net.

        Read-only: answers "who has my net, and is anyone actually there?"
        without touching a single session. `client_connected` is null when
        liveness cannot be determined (the /uart namespace is not registered,
        or the async_mode exposes no connection manager).

        Returns:
        {
            "sessions": [
                {
                    "netname": "UART",
                    "device_path": "/dev/ttyACM1",
                    "sid": "abc123",
                    "heartbeat_age_seconds": 0.05,
                    "client_connected": true,
                    "reader_alive": true
                },
                ...
            ]
        }
        """
        try:
            now = time.monotonic()
            sessions = []
            with active_uart_sessions_lock:
                for sid, sess in active_uart_sessions.items():
                    driver = sess.get('driver')
                    last = sess.get('last_activity')
                    thread = sess.get('thread')
                    connected = None
                    if _socketio is not None:
                        connected = not _client_gone(_socketio, sid)
                    sessions.append({
                        'netname': sess.get('netname'),
                        'device_path': getattr(driver, 'device_path', None),
                        'sid': sid,
                        'heartbeat_age_seconds': (
                            round(now - last, 3) if last is not None else None),
                        'client_connected': connected,
                        # A session mid-setup has no thread yet; that is not
                        # the same as a dead reader, so report it as unknown.
                        'reader_alive': (
                            thread.is_alive() if thread is not None else None),
                    })
            return jsonify({'sessions': sessions})
        except Exception as e:
            logger.exception("Error listing UART sessions")
            return jsonify({'error': str(e), 'sessions': []}), 500

    @app.route('/uart/sessions/<netname>', methods=['DELETE'])
    def uart_session_release(netname):
        """
        Force-release the session holding *netname*.

        The operator escape hatch for a net held by a session that should be
        gone. Mirrors `POST /unlock {"force": true}` for the box lock: the
        holder loses the net, and its read thread exits on its next iteration
        (or when its blocking read unblocks, since closing the fd releases the
        flock).

        Returns 200 with the released sessions, or 404 when nothing holds the
        net — a no-op is reported as such rather than as success, so a
        misspelled netname is visible.
        """
        try:
            released = []
            with active_uart_sessions_lock:
                # list(): _release_session pops entries as we go.
                for sid, sess in list(active_uart_sessions.items()):
                    if sess.get('netname') != netname:
                        continue
                    released.append(sid)
                    _release_session(
                        sid, sess, f"force-released via DELETE /uart/sessions/{netname}")
            if not released:
                return jsonify({
                    'error': f"No UART session is holding net '{netname}'",
                    'released': [],
                }), 404
            return jsonify({'released': released, 'netname': netname})
        except Exception as e:
            logger.exception("Error releasing UART session")
            return jsonify({'error': str(e)}), 500


def register_uart_socketio(socketio: SocketIO) -> None:
    """
    Register UART WebSocket handlers with SocketIO.

    Args:
        socketio: Flask-SocketIO instance
    """
    global _socketio
    _socketio = socketio

    @socketio.on('connect', namespace='/uart')
    def handle_uart_connect():
        """Handle WebSocket connection for UART."""
        logger.info(f"UART WebSocket client connected: {request.sid}")
        emit('connected', {'status': 'ready', 'session_id': request.sid})

    @socketio.on('disconnect', namespace='/uart')
    def handle_uart_disconnect():
        """Handle WebSocket disconnection for UART.

        Cleans up whatever is registered under this sid at the moment it runs.
        Under async_mode='threading' this can race an in-flight start_uart for
        the same sid — disconnect finds nothing, then start_uart registers a
        session whose client is already gone. Nothing here can fix that
        ordering; the read loop's own _client_gone() check does, evicting the
        orphan on its first iteration. Do not reach for an identity guard
        (as _uart_read_loop's finally uses): sids are per-connection and not
        reused, so there is no second session under this sid to protect.
        """
        logger.info(f"UART WebSocket client disconnected: {request.sid}")

        # Clean up any active UART session
        with active_uart_sessions_lock:
            if request.sid in active_uart_sessions:
                session = active_uart_sessions[request.sid]

                # Stop the reading thread
                if 'stop_event' in session:
                    session['stop_event'].set()

                # Close the UART driver
                if 'driver' in session:
                    try:
                        session['driver']._cleanup()
                    except Exception as e:
                        logger.error(f"Error cleaning up UART driver: {e}")

                # Remove from active sessions
                del active_uart_sessions[request.sid]
                logger.info(f"Cleaned up UART session: {request.sid}")

    @socketio.on('start_uart', namespace='/uart')
    def handle_start_uart(data):
        """
        Start interactive UART session.

        Expected data:
        {
            "netname": "uart1",
            "overrides": {"baudrate": 115200, ...}
        }
        """
        try:
            from lager.protocols.uart.dispatcher import _resolve_net_and_driver, UARTBackendError

            netname = data.get('netname')
            overrides = data.get('overrides', {})

            if not netname:
                emit('error', {'message': 'netname is required'})
                return

            # Per-connection + per-net guard. Sessions are keyed by request.sid
            # (one socket.io connection), so the sid check only stops the same
            # connection from starting twice; the netname scan stops a *second*
            # connection from grabbing a net another session already holds, and
            # gives a clear error instead of letting the exclusive open race.
            # A colliding session with no live read loop behind it (thread
            # wedged/dead) is reclaimed rather than blocking forever — the
            # phantom "already in use" then heals on the next start.
            with active_uart_sessions_lock:
                existing = active_uart_sessions.get(request.sid)
                if existing is not None and not _reclaim_if_stale(request.sid, existing):
                    emit('error', {'message': 'UART session already active'})
                    return
                # list(): _reclaim_if_stale may pop entries as we scan.
                for sid, sess in list(active_uart_sessions.items()):
                    if sess.get('netname') == netname and not _reclaim_if_stale(sid, sess):
                        # 'code'/'netname' let a current CLI print the
                        # take-over command with the --box the user actually
                        # typed (which the box cannot know). Older CLIs ignore
                        # the extra fields and print 'message' as before.
                        emit('error', {
                            'message': f"UART net '{netname}' is already in "
                                       f"use by another session",
                            'code': 'net_in_use',
                            'netname': netname,
                        })
                        return

            # Resolve net and create driver
            try:
                driver = _resolve_net_and_driver(netname, overrides)

                # Per-device guard: two different nets can map to the same
                # /dev/tty*. Reject before the exclusive open so the error names
                # the conflict instead of surfacing a lock errno.
                device_path = getattr(driver, 'device_path', None)
                if device_path:
                    with active_uart_sessions_lock:
                        # list(): _reclaim_if_stale may pop entries as we scan.
                        for sid, sess in list(active_uart_sessions.items()):
                            other = sess.get('driver')
                            if (other is not None
                                    and getattr(other, 'device_path', None) == device_path
                                    and not _reclaim_if_stale(sid, sess)):
                                emit('error', {
                                    'message': f"UART device {device_path} is "
                                               f"already in use by another "
                                               f"session",
                                    'code': 'net_in_use',
                                    'netname': netname,
                                })
                                return

                driver._connect()
            except UARTBackendError as e:
                emit('error', {'message': str(e)})
                return
            except FileNotFoundError as e:
                emit('error', {'message': f'UART device not found: {str(e)}'})
                return
            except Exception as e:
                emit('error', {'message': _readable_open_error(netname, locals().get('driver'), e)})
                return

            # Create stop event for thread control
            stop_event = threading.Event()

            # Capture session ID before starting thread (request context not available in threads)
            session_id = request.sid

            # Store session info
            with active_uart_sessions_lock:
                active_uart_sessions[session_id] = {
                    'driver': driver,
                    'stop_event': stop_event,
                    'netname': netname,
                    'last_activity': time.monotonic(),
                }

            # Send connection success
            emit('uart_connected', {
                'netname': netname,
                'device_path': driver.device_path,
                'baudrate': driver.baudrate,
                'message': f'Connected to {driver.device_path} at {driver.baudrate} baud'
            })

            # Start reading thread
            read_thread = threading.Thread(
                target=_uart_read_loop,
                args=(socketio, session_id, netname, driver, stop_event),
                daemon=True)
            read_thread.start()

            # Store thread reference
            with active_uart_sessions_lock:
                if session_id in active_uart_sessions:
                    active_uart_sessions[session_id]['thread'] = read_thread

        except Exception as e:
            logger.exception("Error in handle_start_uart")
            emit('error', {'message': str(e)})

    @socketio.on('uart_write', namespace='/uart')
    def handle_uart_write(data):
        """
        Write data to UART.

        Expected data:
        {
            "data": "hex_string"  # Data to write as hex string
        }
        """
        try:
            with active_uart_sessions_lock:
                if request.sid not in active_uart_sessions:
                    emit('error', {'message': 'No active UART session'})
                    return

                session = active_uart_sessions[request.sid]
                driver = session['driver']

            # Get data to write
            hex_data = data.get('data', '')
            if not hex_data:
                return

            # Convert hex string to bytes
            try:
                bytes_data = bytes.fromhex(hex_data)
            except ValueError as e:
                emit('error', {'message': f'Invalid hex data: {str(e)}'})
                return

            # Write to UART
            try:
                driver.serial_conn.write(bytes_data)
                driver.serial_conn.flush()
            except Exception as e:
                logger.error(f"Error writing to UART: {e}")
                emit('error', {'message': f'Write error: {str(e)}'})

        except Exception as e:
            logger.exception("Error in handle_uart_write")
            emit('error', {'message': str(e)})

    @socketio.on('stop_uart', namespace='/uart')
    def handle_stop_uart():
        """Stop the UART session."""
        try:
            with active_uart_sessions_lock:
                if request.sid in active_uart_sessions:
                    session = active_uart_sessions[request.sid]

                    # Stop the reading thread
                    if 'stop_event' in session:
                        session['stop_event'].set()

                    # Close the driver
                    if 'driver' in session:
                        session['driver']._cleanup()

                    # Remove from active sessions
                    del active_uart_sessions[request.sid]

            emit('uart_stopped', {'message': 'UART session stopped'})
            logger.info(f"UART session stopped: {request.sid}")

        except Exception as e:
            logger.exception("Error in handle_stop_uart")
            emit('error', {'message': str(e)})


def cleanup_uart_sessions() -> None:
    """
    Clean up all active UART sessions.

    This function should be called during server shutdown to properly
    close all UART connections and stop all reading threads.
    """
    with active_uart_sessions_lock:
        for session_id, session in list(active_uart_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
                if 'driver' in session:
                    session['driver']._cleanup()
            except Exception as e:
                logger.error(f"Error cleaning up UART session {session_id}: {e}")
        active_uart_sessions.clear()

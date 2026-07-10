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

# How long a live session waits for a re-enumerating device to come back
# before giving up and reporting a terminal error.
UART_RECONNECT_TIMEOUT = 60.0


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

    try:
        while not stop_event.is_set():
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
                    if driver.reconnect(stop_check=stop_event.is_set,
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


def register_uart_socketio(socketio: SocketIO) -> None:
    """
    Register UART WebSocket handlers with SocketIO.

    Args:
        socketio: Flask-SocketIO instance
    """

    @socketio.on('connect', namespace='/uart')
    def handle_uart_connect():
        """Handle WebSocket connection for UART."""
        logger.info(f"UART WebSocket client connected: {request.sid}")
        emit('connected', {'status': 'ready', 'session_id': request.sid})

    @socketio.on('disconnect', namespace='/uart')
    def handle_uart_disconnect():
        """Handle WebSocket disconnection for UART."""
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
            with active_uart_sessions_lock:
                if request.sid in active_uart_sessions:
                    emit('error', {'message': 'UART session already active'})
                    return
                for sess in active_uart_sessions.values():
                    if sess.get('netname') == netname:
                        emit('error', {'message': f"UART net '{netname}' is already in use by another session"})
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
                        for sess in active_uart_sessions.values():
                            other = sess.get('driver')
                            if other is not None and getattr(other, 'device_path', None) == device_path:
                                emit('error', {'message': f"UART device {device_path} is already in use by another session"})
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
                    'netname': netname
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

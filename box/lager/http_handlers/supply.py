# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Supply HTTP and WebSocket handlers for the Lager Box HTTP+WebSocket Server.

This module handles power supply monitoring and control:
- HTTP endpoint for supply commands (reuses TUI session)
- WebSocket namespace /supply for real-time monitoring and control
"""
import logging
import threading
import time
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit

from lager.dispatchers.helpers import resolve_net_proxy
from lager.exceptions import SupplyBackendError
from lager.nets.device import ConnectionFailed, DeviceError, Device, describe_error

from .state import (
    active_supply_sessions,
    active_supply_sessions_lock,
    conflicting_other_role_session,
    format_cross_role_conflict_message,
)

logger = logging.getLogger(__name__)


def _resolve_supply_proxy(netname: str):
    """
    Build a transient supply Device proxy + channel + hardware limits for
    `netname` by routing through hardware_service.py:/invoke. Used by the
    HTTP command endpoint when no TUI WebSocket session is active so that
    hardware_service.py remains the sole owner of the pyvisa session — a
    direct in-process pyvisa session would race with hardware_service for
    the USB device and produce "[Errno 16] Resource busy".
    """
    device_name, net_info, channel = resolve_net_proxy(
        netname, "power-supply", SupplyBackendError
    )
    supply = Device(device_name, net_info)
    try:
        limits = supply.get_channel_limits(channel)
        voltage_max = limits.get('voltage_max', 0)
        current_max = limits.get('current_max', 0)
    except (AttributeError, NotImplementedError):
        voltage_max = 0
        current_max = 0
    return supply, channel, voltage_max, current_max


# Wire shape of the supply state dict (minus the netname/channel envelope).
# build_supply_state pre-fills every key with None so consumers always get a
# full-shaped dict even when the driver returns a partial one or the gather
# fails outright.
SUPPLY_STATE_FIELDS = (
    'voltage', 'current', 'power', 'enabled', 'mode',
    'voltage_set', 'current_set', 'voltage_max', 'current_max',
    'ocp_limit', 'ocp_tripped', 'ovp_limit', 'ovp_tripped',
)


def build_supply_state(supply, channel, netname):
    """Build the structured supply state dict. Used by both the SocketIO
    monitor and the HTTP /supply/command 'state' action so they never drift.

    The field-by-field logic (including per-field _safe fallbacks) lives in
    the driver's get_monitor_state(); this wrapper adds the netname/channel
    envelope that both transports emit.

    Never raises: always returns a full-shaped dict (every field key present,
    None where a read failed). If the single get_monitor_state /invoke fails
    entirely — hardware_service down, stale-session retry exhausted, transient
    "[Errno 16] Resource busy" — the data fields stay None and 'error' carries
    the transport-level detail; otherwise 'error' is None.
    """
    state = {'netname': netname, 'channel': channel, 'error': None}
    state.update({field: None for field in SUPPLY_STATE_FIELDS})
    try:
        state.update(supply.get_monitor_state(channel))
    except ConnectionFailed as e:
        state['error'] = f'Hardware service unreachable: {describe_error(e)}'
    except DeviceError as e:
        state['error'] = f'Hardware service error: {describe_error(e)}'
    except Exception as e:
        state['error'] = f'Monitoring error: {describe_error(e)}'
    if state['error']:
        logger.warning(f"Supply state gather failed for {netname}: {state['error']}")
    return state


def register_supply_routes(app: Flask) -> None:
    """
    Register supply HTTP routes with the Flask app.

    Args:
        app: Flask application instance
    """

    # Supply HTTP endpoint for non-WebSocket commands
    @app.route('/supply/command', methods=['POST'])
    def supply_command_http():
        """
        Execute a supply command via hardware_service.py:/invoke.

        If a TUI WebSocket session is already active for this net, reuse its
        cached Device proxy (and cached limits). Otherwise, build a transient
        proxy via resolve_net_proxy() — also routed through hardware_service —
        so the call still goes through the single pyvisa-owning service rather
        than opening a competing in-process pyvisa session.

        Request body:
        {
            "netname": "supply1",
            "action": "voltage" | "current" | "enable" | "disable" | "ocp" | "ovp"
                      | "clear_ocp" | "clear_ovp" | "state",
            "params": {"value": 3.3}  # Optional, for set operations
        }

        Returns:
        {
            "success": true,
            "action": "voltage",
            "message": "Voltage set to 3.3V"
        }

        The 'state' action additionally returns a structured "state" object —
        the same dict the SocketIO monitor emits (built by build_supply_state)
        — so HTTP-only clients can render a live view by polling.
        """
        try:
            data = request.get_json()
            netname = data.get('netname')
            action = data.get('action')
            params = data.get('params', {})

            if not netname or not action:
                return jsonify({'success': False, 'error': 'netname and action are required'}), 400

            # Cross-role conflict check: if a battery TUI is currently
            # monitoring the same physical Keithley, refuse with a clear
            # message rather than letting the SCPI entry-function tug-of-war
            # surface as Resource busy. (Sequential CLI cross-role workflows
            # never populate active_*_sessions and so are unaffected.)
            try:
                _, _conflict_net_info, _ = resolve_net_proxy(
                    netname, "power-supply", SupplyBackendError
                )
                _conflict_address = (_conflict_net_info or {}).get('address')
            except SupplyBackendError:
                _conflict_address = None
            if _conflict_address:
                _conflict = conflicting_other_role_session(
                    'power-supply', _conflict_address
                )
                if _conflict:
                    other_role, other_netname = _conflict
                    msg = format_cross_role_conflict_message(
                        'power-supply', netname, _conflict_address,
                        other_role, other_netname,
                    )
                    logger.warning(
                        f"[HTTP] cross-role conflict: '{netname}' (power-supply) "
                        f"vs '{other_netname}' ({other_role}) at {_conflict_address}"
                    )
                    return jsonify({'success': False, 'error': msg}), 200

            # Find an active WebSocket session for this netname.
            # SCPI serialization is now handled inside hardware_service.py
            # by per-cache-key locks, so the old per-netname instrument_lock
            # is no longer needed here.
            session_id = None
            supply = None
            channel = None
            voltage_max = 0
            current_max = 0

            with active_supply_sessions_lock:
                for sid, session_info in active_supply_sessions.items():
                    if session_info.get('netname') == netname:
                        session_id = sid
                        supply = session_info.get('supply')
                        channel = session_info.get('channel')
                        voltage_max = session_info.get('voltage_max', 0)
                        current_max = session_info.get('current_max', 0)
                        break

            if not supply:
                # No active TUI session: build a transient hardware_service-routed
                # proxy. Using a fresh in-process pyvisa session here would race
                # with hardware_service.py's cached session for the same USB
                # device (e.g. left over from a recently-exited TUI) and produce
                # "[Errno 16] Resource busy".
                try:
                    supply, channel, voltage_max, current_max = _resolve_supply_proxy(netname)
                except SupplyBackendError as e:
                    return jsonify({'success': False, 'error': str(e)}), 404
                except (ConnectionFailed, DeviceError) as e:
                    logger.exception(
                        f"[HTTP] Could not build transient supply proxy for {netname}"
                    )
                    return jsonify({
                        'success': False,
                        'error': f'Hardware service error: {e}',
                    }), 502

            # Execute the command using the same logic as WebSocket commands
            result = {'success': True, 'action': action}
            value = params.get('value')
            ocp = params.get('ocp')
            ovp = params.get('ovp')

            try:
                if action == 'voltage':
                    if value is not None and voltage_max > 0 and value > voltage_max:
                        return jsonify({'success': False, 'error': f'Voltage {value}V exceeds hardware limit {voltage_max}V'}), 400
                    if ovp is not None and voltage_max > 0 and ovp > voltage_max:
                        return jsonify({'success': False, 'error': f'OVP {ovp}V exceeds hardware voltage limit {voltage_max}V'}), 400
                    if ocp is not None and current_max > 0 and ocp > current_max:
                        return jsonify({'success': False, 'error': f'OCP {ocp}A exceeds hardware current limit {current_max}A'}), 400
                    if value is not None or ocp is not None or ovp is not None:
                        supply.voltage(value=value, ocp=ocp, ovp=ovp)
                        applied = []
                        if value is not None:
                            applied.append(f'Voltage set to {value}V')
                        if ocp is not None:
                            applied.append(f'OCP {ocp}A')
                        if ovp is not None:
                            applied.append(f'OVP {ovp}V')
                        result['message'] = ', '.join(applied)
                    else:
                        v_set = float(supply.get_channel_voltage(source=channel))
                        result['message'] = f'Voltage setpoint: {v_set}V'

                elif action == 'current':
                    if value is not None and current_max > 0 and value > current_max:
                        return jsonify({'success': False, 'error': f'Current {value}A exceeds hardware limit {current_max}A'}), 400
                    if ovp is not None and voltage_max > 0 and ovp > voltage_max:
                        return jsonify({'success': False, 'error': f'OVP {ovp}V exceeds hardware voltage limit {voltage_max}V'}), 400
                    if ocp is not None and current_max > 0 and ocp > current_max:
                        return jsonify({'success': False, 'error': f'OCP {ocp}A exceeds hardware current limit {current_max}A'}), 400
                    if value is not None or ocp is not None or ovp is not None:
                        supply.current(value=value, ocp=ocp, ovp=ovp)
                        applied = []
                        if value is not None:
                            applied.append(f'Current limit set to {value}A')
                        if ocp is not None:
                            applied.append(f'OCP {ocp}A')
                        if ovp is not None:
                            applied.append(f'OVP {ovp}V')
                        result['message'] = ', '.join(applied)
                    else:
                        i_set = float(supply.get_channel_current(source=channel))
                        result['message'] = f'Current setpoint: {i_set}A'

                elif action == 'enable':
                    supply.enable()
                    result['message'] = 'Supply output enabled'

                elif action == 'disable':
                    supply.disable()
                    result['message'] = 'Supply output disabled'

                elif action == 'ocp':
                    if value is not None and current_max > 0 and value > current_max:
                        return jsonify({'success': False, 'error': f'OCP {value}A exceeds hardware current limit {current_max}A'}), 400
                    if value is not None:
                        supply.set_overcurrent_protection_value(value, channel=channel)
                        supply.enable_overcurrent_protection(channel=channel)
                        result['message'] = f'OCP limit set to {value}A'
                    else:
                        ocp = supply.get_overcurrent_protection_value(channel)
                        result['message'] = f'OCP limit: {ocp}A'

                elif action == 'ovp':
                    if value is not None and voltage_max > 0 and value > voltage_max:
                        return jsonify({'success': False, 'error': f'OVP {value}V exceeds hardware voltage limit {voltage_max}V'}), 400
                    if value is not None:
                        supply.set_overvoltage_protection_value(value, channel=channel)
                        supply.enable_overvoltage_protection(channel=channel)
                        result['message'] = f'OVP limit set to {value}V'
                    else:
                        ovp = supply.get_overvoltage_protection_value(channel)
                        result['message'] = f'OVP limit: {ovp}V'

                elif action == 'state':
                    # One build_supply_state call — a single /invoke — supplies
                    # both the structured state and the human-readable message.
                    # The builder never raises and always returns a full-shaped
                    # dict (None for fields that failed), so the response
                    # always includes 'state' even on a degraded read.
                    state = build_supply_state(supply, channel, netname)
                    result['state'] = state

                    def _fmt(value, unit):
                        return 'n/a' if value is None else f'{value}{unit}'

                    enabled = state['enabled']
                    state_str = 'UNKNOWN' if enabled is None else ('ON' if enabled else 'OFF')
                    msg = (
                        f"Channel {channel}: {state_str}, "
                        f"Set: {_fmt(state['voltage_set'], 'V')}/{_fmt(state['current_set'], 'A')}, "
                        f"Measured: {_fmt(state['voltage'], 'V')}/{_fmt(state['current'], 'A')}"
                    )
                    if state['ocp_limit'] is not None:
                        msg += f", OCP: {state['ocp_limit']}A"
                    if state['ovp_limit'] is not None:
                        msg += f", OVP: {state['ovp_limit']}V"
                    if state['error']:
                        msg += f" (degraded: {state['error']})"
                    result['message'] = msg

                elif action == 'clear_ocp':
                    # Uniform driver wrapper — takes no channel kwarg (the EA
                    # driver's granular clear_*_trip methods don't accept one).
                    supply.clear_ocp()
                    result['message'] = 'OCP trip cleared'

                elif action == 'clear_ovp':
                    supply.clear_ovp()
                    result['message'] = 'OVP trip cleared'

                elif action == 'set_mode':
                    supply.set_mode()
                    result['message'] = 'Power supply mode set'

                else:
                    return jsonify({'success': False, 'error': f'Unknown action: {action}'}), 400

                logger.info(f"[HTTP] Supply command executed: {action} on {netname} (session {session_id})")
                return jsonify(result)
            except (ConnectionFailed, DeviceError) as e:
                logger.exception(f"[HTTP] Supply command {action} on {netname} failed at hardware_service")
                return jsonify({'success': False, 'error': f'Hardware service error: {e}'}), 502

        except Exception as e:
            logger.exception(f"[HTTP] Error executing supply command")
            return jsonify({'success': False, 'error': str(e)}), 500

def register_supply_socketio(socketio: SocketIO) -> None:
    """
    Register supply WebSocket handlers with SocketIO.

    Args:
        socketio: Flask-SocketIO instance
    """

    @socketio.on('connect', namespace='/supply')
    def handle_supply_connect():
        """Handle WebSocket connection for supply monitoring."""
        logger.info(f"Supply WebSocket client connected: {request.sid}")
        emit('connected', {'status': 'ready', 'session_id': request.sid})

    @socketio.on('disconnect', namespace='/supply')
    def handle_supply_disconnect():
        """Handle WebSocket disconnection for supply monitoring."""
        logger.info(f"Supply WebSocket client disconnected: {request.sid}")

        # Clean up any active supply monitoring session
        with active_supply_sessions_lock:
            if request.sid in active_supply_sessions:
                session = active_supply_sessions[request.sid]

                # Stop the monitoring thread
                if 'stop_event' in session:
                    session['stop_event'].set()

                # Remove from active sessions
                del active_supply_sessions[request.sid]
                logger.info(f"Cleaned up supply session: {request.sid}")

    @socketio.on('start_supply_monitor', namespace='/supply')
    def handle_start_supply_monitor(data):
        """
        Start supply monitoring session.

        Expected data:
        {
            "netname": "supply1",
            "interval": 1.0  # Update interval in seconds (default: 1.0)
        }
        """
        try:
            netname = data.get('netname')
            # Coerce once at ingestion: a string interval (e.g. "2") would
            # otherwise raise TypeError at max(interval, tick_duration) in
            # the monitor loop and silently kill the monitor thread.
            try:
                interval = max(0.1, float(data.get('interval', 1.0)))
            except (TypeError, ValueError):
                interval = 1.0

            if not netname:
                emit('error', {'message': 'netname is required'})
                return

            # Check if session already exists
            with active_supply_sessions_lock:
                if request.sid in active_supply_sessions:
                    emit('error', {'message': 'Supply monitoring session already active'})
                    return

            # Resolve VISA address synchronously so the cross-role conflict
            # check below — and any concurrent battery_command_http call
            # racing against this start — can read it from the session dict
            # immediately, rather than waiting for the monitor thread to
            # populate it. Failures are non-fatal here; the monitor thread
            # will surface the same error properly.
            target_address = None
            try:
                _, _net_info, _ = resolve_net_proxy(
                    netname, "power-supply", SupplyBackendError
                )
                target_address = (_net_info or {}).get('address')
            except Exception:
                target_address = None

            # Cross-role conflict check: refuse to start a supply TUI on a
            # Keithley already being monitored as a battery (and vice-versa).
            # See state.conflicting_other_role_session for the full rationale.
            if target_address:
                conflict = conflicting_other_role_session(
                    'power-supply', target_address
                )
                if conflict:
                    other_role, other_netname = conflict
                    msg = format_cross_role_conflict_message(
                        'power-supply', netname, target_address,
                        other_role, other_netname,
                    )
                    logger.warning(
                        f"[WS] cross-role conflict: '{netname}' (power-supply) "
                        f"vs '{other_netname}' ({other_role}) at {target_address}"
                    )
                    emit('error', {'message': msg})
                    return

            # Create stop event for thread control
            stop_event = threading.Event()

            # Capture session ID before starting thread
            session_id = request.sid

            # Store session info. The supply driver (a Device proxy) will be
            # added by the monitoring thread once it has resolved net_info.
            # SCPI serialization is handled inside hardware_service.py by
            # per-cache-key locks; no per-netname lock needed here anymore.
            with active_supply_sessions_lock:
                active_supply_sessions[session_id] = {
                    'netname': netname,
                    'address': target_address,  # for cross-role conflict checks
                    'stop_event': stop_event,
                    'interval': interval,
                    'supply': None,  # Will be set by monitoring thread
                    'channel': None,  # Will be set by monitoring thread
                }

            # Send connection success
            emit('supply_monitor_started', {
                'netname': netname,
                'interval': interval,
                'message': f'Started monitoring supply: {netname}'
            })

            # Start monitoring thread.
            #
            # The supply driver is a Device HTTP proxy that POSTs to
            # hardware_service.py:/invoke. hardware_service owns the pyvisa
            # session and serializes concurrent calls per cache key, so this
            # thread no longer needs the USB/pyvisa import dance, the
            # dispatcher cache clears, the v0.16.5 /cache/clear band-aid,
            # the per-netname instrument_lock, or a driver-close finally.
            def monitor_supply():
                """Monitor supply state and emit updates to WebSocket."""
                import threading as thread_module

                thread_name = thread_module.current_thread().name
                logger.info(f"[MONITOR-{thread_name}] Monitoring thread starting for {netname}")

                try:
                    device_name, net_info, channel = resolve_net_proxy(
                        netname, "power-supply", SupplyBackendError
                    )
                    supply = Device(device_name, net_info)
                    logger.info(
                        f"[MONITOR-{thread_name}] Got supply proxy: {device_name} "
                        f"channel {channel} address {net_info.get('address')}"
                    )
                except Exception as resolve_err:
                    import traceback
                    logger.error(
                        f"[MONITOR-{thread_name}] Failed to resolve supply '{netname}': {resolve_err}"
                    )
                    logger.error(f"[MONITOR-{thread_name}] Traceback: {traceback.format_exc()}")
                    socketio.emit(
                        'error',
                        {'message': f"Could not resolve supply '{netname}': {resolve_err}"},
                        namespace='/supply',
                        room=session_id,
                    )
                    return

                # Get hardware limits via the proxy. AttributeError/NotImplementedError
                # mean the driver legitimately doesn't expose limits (treated as 0).
                # ConnectionFailed (hardware_service down) or DeviceError (proxy
                # surfaced an error) means the first SCPI query failed — surface
                # to client rather than silently timing out.
                try:
                    limits = supply.get_channel_limits(channel)
                    voltage_max = limits.get('voltage_max', 0)
                    current_max = limits.get('current_max', 0)
                except (AttributeError, NotImplementedError):
                    voltage_max = 0
                    current_max = 0
                except (ConnectionFailed, DeviceError) as limits_err:
                    import traceback
                    logger.error(
                        f"[MONITOR-{thread_name}] First SCPI query failed for {netname}: {limits_err}"
                    )
                    logger.error(f"[MONITOR-{thread_name}] Traceback: {traceback.format_exc()}")
                    socketio.emit(
                        'error',
                        {'message': (
                            f"Supply '{netname}' is not responding "
                            f"(first query failed): {limits_err}"
                        )},
                        namespace='/supply',
                        room=session_id,
                    )
                    return

                # Store supply, channel, and limits in session for command execution.
                try:
                    with active_supply_sessions_lock:
                        if session_id in active_supply_sessions:
                            active_supply_sessions[session_id]['supply'] = supply
                            active_supply_sessions[session_id]['channel'] = channel
                            active_supply_sessions[session_id]['voltage_max'] = voltage_max
                            active_supply_sessions[session_id]['current_max'] = current_max
                            logger.info(
                                f"[MONITOR-{thread_name}] Stored supply proxy in session "
                                f"(limits: {voltage_max}V, {current_max}A)"
                            )
                except Exception as store_err:
                    import traceback
                    logger.error(
                        f"[MONITOR-{thread_name}] Failed to store supply session for {netname}: {store_err}"
                    )
                    logger.error(f"[MONITOR-{thread_name}] Traceback: {traceback.format_exc()}")
                    socketio.emit(
                        'error',
                        {'message': f"Internal error registering supply '{netname}' session: {store_err}"},
                        namespace='/supply',
                        room=session_id,
                    )
                    return

                # Notify client that driver is ready for commands
                socketio.emit('supply_driver_ready', {
                    'netname': netname,
                    'channel': channel,
                    'voltage_max': voltage_max,
                    'current_max': current_max,
                    'message': f'Supply driver ready for {netname}'
                }, namespace='/supply', room=session_id)
                logger.info(f"[MONITOR-{thread_name}] Emitted supply_driver_ready event")

                while not stop_event.is_set():
                    tick_started = time.monotonic()
                    try:
                        # ONE /invoke POST — and one per-device lock
                        # acquisition inside hardware_service — per tick.
                        # The previous per-field version made ~12 separate
                        # /invoke calls per tick; on slow instruments each
                        # call held the shared device lock long enough to
                        # starve interactive TUI commands, and on the
                        # Keithley 2281S in battery mode the mode-enforcing
                        # queries blew the proxy's 10s HTTP budget outright.
                        state = build_supply_state(supply, channel, netname)

                        # The builder never raises: a total gather failure
                        # comes back as state['error'] with all data fields
                        # None. Emit the error event (as before) rather than
                        # an all-None state update, so the TUI keeps its last
                        # good display instead of blanking on a transient.
                        if state['error']:
                            logger.error(f"[MONITOR-{thread_name}] {state['error']}")
                            socketio.emit('error',
                                        {'message': state['error']},
                                        namespace='/supply',
                                        room=session_id)
                        else:
                            socketio.emit('supply_state_update',
                                        {'state': state},
                                        namespace='/supply',
                                        room=session_id)
                    except Exception as e:
                        detail = describe_error(e)
                        logger.error(f"[MONITOR-{thread_name}] Error monitoring supply: {detail}")
                        socketio.emit('error',
                                    {'message': f'Monitoring error: {detail}'},
                                    namespace='/supply',
                                    room=session_id)

                    # Adaptive cadence: never let polling occupy more than
                    # ~half the device's time. A gather that took 3s on a
                    # slow instrument is followed by >=3s of idle, leaving
                    # interactive commands a window instead of a permanently
                    # saturated lock queue.
                    tick_duration = time.monotonic() - tick_started
                    stop_event.wait(max(interval, tick_duration))

                logger.info(f"Supply monitoring thread stopped for session {session_id}")

            # Start monitoring thread
            monitor_thread = threading.Thread(target=monitor_supply, daemon=True)
            monitor_thread.start()

            # Store thread reference
            with active_supply_sessions_lock:
                if session_id in active_supply_sessions:
                    active_supply_sessions[session_id]['thread'] = monitor_thread

        except Exception as e:
            logger.exception("Error in handle_start_supply_monitor")
            emit('error', {'message': str(e)})

    @socketio.on('supply_command', namespace='/supply')
    def handle_supply_command(data):
        """
        Execute a supply command.

        Expected data:
        {
            "netname": "supply1",
            "action": "voltage" | "current" | "enable" | "disable" | "ocp" | "ovp" | "clear_ocp" | "clear_ovp" | "state",
            "params": {...}  # Action-specific parameters
        }
        """
        try:
            netname = data.get('netname')
            action = data.get('action')
            params = data.get('params', {})

            if not netname:
                emit('supply_command_response', {'success': False, 'error': 'netname is required'})
                return

            if not action:
                emit('supply_command_response', {'success': False, 'error': 'action is required'})
                return

            import threading as thread_module

            thread_name = thread_module.current_thread().name
            logger.info(f"[COMMAND-{thread_name}] Processing command: action={action}, netname={netname}, params={params}")

            # Get supply proxy, channel, and limits from session (set by monitoring thread).
            # SCPI serialization is handled inside hardware_service.py per cache key,
            # so the old per-netname instrument_lock is no longer needed here.
            session_id = request.sid
            supply = None
            channel = None
            voltage_max = 0
            current_max = 0

            with active_supply_sessions_lock:
                if session_id in active_supply_sessions:
                    supply = active_supply_sessions[session_id].get('supply')
                    channel = active_supply_sessions[session_id].get('channel')
                    voltage_max = active_supply_sessions[session_id].get('voltage_max', 0)
                    current_max = active_supply_sessions[session_id].get('current_max', 0)

            if not supply:
                emit('supply_command_response', {
                    'success': False,
                    'error': 'Supply driver not available. Is monitoring active?'
                })
                return

            logger.info(f"[COMMAND-{thread_name}] Using supply proxy on channel {channel}, limits: {voltage_max}V, {current_max}A")

            result = {'success': True, 'action': action}

            try:
                if action == 'voltage':
                    value = params.get('value')
                    # Validate voltage against hardware limit (only when setting)
                    if value is not None and voltage_max > 0 and value > voltage_max:
                        result['success'] = False
                        result['error'] = f'Voltage {value}V exceeds hardware limit {voltage_max}V'
                        emit('supply_command_response', result)
                        return
                    logger.info(f"[COMMAND-{thread_name}] {'Setting' if value else 'Reading'} voltage {'to ' + str(value) + 'V' if value else ''} on channel {channel}...")
                    if value is not None:
                        supply.voltage(value=value)
                        logger.info(f"[COMMAND-{thread_name}] Voltage set successfully")
                        result['message'] = f'Voltage set to {value}V'
                    else:
                        # Read voltage setpoint
                        v_set = float(supply.get_channel_voltage(source=channel))
                        logger.info(f"[COMMAND-{thread_name}] Voltage read successfully: {v_set}V")
                        result['message'] = f'Voltage setpoint: {v_set}V'

                elif action == 'current':
                    value = params.get('value')
                    # Validate current against hardware limit (only when setting)
                    if value is not None and current_max > 0 and value > current_max:
                        result['success'] = False
                        result['error'] = f'Current {value}A exceeds hardware limit {current_max}A'
                        emit('supply_command_response', result)
                        return
                    logger.info(f"[COMMAND-{thread_name}] {'Setting' if value else 'Reading'} current {'to ' + str(value) + 'A' if value else ''} on channel {channel}...")
                    if value is not None:
                        supply.current(value=value)
                        logger.info(f"[COMMAND-{thread_name}] Current set successfully")
                        result['message'] = f'Current limit set to {value}A'
                    else:
                        # Read current setpoint
                        i_set = float(supply.get_channel_current(source=channel))
                        logger.info(f"[COMMAND-{thread_name}] Current read successfully: {i_set}A")
                        result['message'] = f'Current setpoint: {i_set}A'

                elif action == 'enable':
                    logger.info(f"[COMMAND-{thread_name}] Enabling output on channel {channel}...")
                    supply.enable()
                    logger.info(f"[COMMAND-{thread_name}] Output enabled successfully")
                    result['message'] = 'Supply output enabled'

                elif action == 'disable':
                    logger.info(f"[COMMAND-{thread_name}] Disabling output on channel {channel}...")
                    supply.disable()
                    logger.info(f"[COMMAND-{thread_name}] Output disabled successfully")
                    result['message'] = 'Supply output disabled'

                elif action == 'ocp':
                    value = params.get('value')
                    # Validate OCP against hardware current limit (only when setting)
                    if value is not None and current_max > 0 and value > current_max:
                        result['success'] = False
                        result['error'] = f'OCP {value}A exceeds hardware current limit {current_max}A'
                        emit('supply_command_response', result)
                        return
                    if value is not None:
                        logger.info(f"[COMMAND-{thread_name}] Setting OCP to {value}A on channel {channel}...")
                        supply.set_overcurrent_protection_value(value, channel=channel)
                        supply.enable_overcurrent_protection(channel=channel)
                        logger.info(f"[COMMAND-{thread_name}] OCP set successfully")
                        result['message'] = f'OCP limit set to {value}A'
                    else:
                        logger.info(f"[COMMAND-{thread_name}] Reading OCP on channel {channel}...")
                        ocp = supply.get_overcurrent_protection_value(channel)
                        logger.info(f"[COMMAND-{thread_name}] OCP read successfully: {ocp}A")
                        result['message'] = f'OCP limit: {ocp}A'

                elif action == 'ovp':
                    value = params.get('value')
                    # Validate OVP against hardware voltage limit (only when setting)
                    if value is not None and voltage_max > 0 and value > voltage_max:
                        result['success'] = False
                        result['error'] = f'OVP {value}V exceeds hardware voltage limit {voltage_max}V'
                        emit('supply_command_response', result)
                        return
                    if value is not None:
                        logger.info(f"[COMMAND-{thread_name}] Setting OVP to {value}V on channel {channel}...")
                        supply.set_overvoltage_protection_value(value, channel=channel)
                        supply.enable_overvoltage_protection(channel=channel)
                        logger.info(f"[COMMAND-{thread_name}] OVP set successfully")
                        result['message'] = f'OVP limit set to {value}V'
                    else:
                        logger.info(f"[COMMAND-{thread_name}] Reading OVP on channel {channel}...")
                        ovp = supply.get_overvoltage_protection_value(channel)
                        logger.info(f"[COMMAND-{thread_name}] OVP read successfully: {ovp}V")
                        result['message'] = f'OVP limit: {ovp}V'

                elif action == 'clear_ocp':
                    logger.info(f"[COMMAND-{thread_name}] Clearing OCP on channel {channel}...")
                    supply.clear_overcurrent_protection_trip(channel=channel)
                    logger.info(f"[COMMAND-{thread_name}] OCP cleared successfully")
                    result['message'] = 'OCP trip cleared'

                elif action == 'clear_ovp':
                    logger.info(f"[COMMAND-{thread_name}] Clearing OVP on channel {channel}...")
                    supply.clear_overvoltage_protection_trip(channel=channel)
                    logger.info(f"[COMMAND-{thread_name}] OVP cleared successfully")
                    result['message'] = 'OVP trip cleared'

                elif action == 'state':
                    logger.info(f"[COMMAND-{thread_name}] Getting state for channel {channel}...")
                    try:
                        enabled = supply.output_is_enabled(channel=channel)
                        voltage_set = float(supply.get_channel_voltage(source=channel))
                        current_set = float(supply.get_channel_current(source=channel))
                        if enabled:
                            try:
                                voltage_meas = float(supply.measure_voltage(channel))
                                current_meas = float(supply.measure_current(channel))
                            except Exception:
                                voltage_meas = 0.0
                                current_meas = 0.0
                        else:
                            voltage_meas = 0.0
                            current_meas = 0.0
                        state_str = "ON" if enabled else "OFF"
                        msg = (
                            f'Channel {channel}: {state_str}, Set: {voltage_set}V/{current_set}A, '
                            f'Measured: {voltage_meas}V/{current_meas}A'
                        )
                        try:
                            ocp_s = float(supply.get_overcurrent_protection_value(channel))
                            msg += f', OCP: {ocp_s}A'
                        except Exception:
                            pass
                        try:
                            ovp_s = float(supply.get_overvoltage_protection_value(channel))
                            msg += f', OVP: {ovp_s}V'
                        except Exception:
                            pass
                        result['message'] = msg
                    except Exception as e:
                        result['message'] = f'State summary unavailable: {e}'

                else:
                    result['success'] = False
                    result['error'] = f'Unknown action: {action}'

                logger.info(f"[COMMAND-{thread_name}] Command completed successfully: {result}")
                emit('supply_command_response', result)

            except (ConnectionFailed, DeviceError) as e:
                logger.error(f"[COMMAND-{thread_name}] hardware_service error: {e}")
                emit('supply_command_response', {
                    'success': False,
                    'error': f'Hardware service error: {e}'
                })
            except Exception as e:
                import traceback
                logger.error(f"[COMMAND-{thread_name}] Command execution error: {e}")
                logger.error(f"[COMMAND-{thread_name}] Traceback: {traceback.format_exc()}")
                emit('supply_command_response', {
                    'success': False,
                    'error': str(e)
                })

        except Exception as e:
            logger.exception("Error in handle_supply_command")
            emit('supply_command_response', {
                'success': False,
                'error': str(e)
            })

    @socketio.on('stop_supply_monitor', namespace='/supply')
    def handle_stop_supply_monitor():
        """Stop the supply monitoring session."""
        try:
            with active_supply_sessions_lock:
                if request.sid in active_supply_sessions:
                    session = active_supply_sessions[request.sid]

                    # Stop the monitoring thread
                    if 'stop_event' in session:
                        session['stop_event'].set()

                    # Remove from active sessions
                    del active_supply_sessions[request.sid]

            emit('supply_monitor_stopped', {'message': 'Supply monitoring stopped'})
            logger.info(f"Supply monitoring stopped: {request.sid}")

        except Exception as e:
            logger.exception("Error in handle_stop_supply_monitor")
            emit('error', {'message': str(e)})


def cleanup_supply_sessions():
    """Clean up all active supply sessions. Called during server shutdown."""
    with active_supply_sessions_lock:
        for session_id, session in list(active_supply_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
            except Exception as e:
                logger.error(f"Error cleaning up supply session {session_id}: {e}")
        active_supply_sessions.clear()

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Battery HTTP and WebSocket handlers for Lager Box.

This module contains all battery simulator-related endpoints:
- HTTP endpoint for non-WebSocket commands (/battery/command)
- WebSocket handlers for real-time monitoring and control (/battery namespace)
"""
import logging
import threading

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit

from lager.dispatchers.helpers import resolve_net_proxy
from lager.exceptions import BatteryBackendError
from lager.nets.device import ConnectionFailed, DeviceError, Device

from .state import (
    active_battery_sessions,
    active_battery_sessions_lock,
)

logger = logging.getLogger(__name__)


def _resolve_battery_proxy(netname: str):
    """
    Build a transient battery Device proxy + channel for `netname` by routing
    through hardware_service.py:/invoke. Used by the HTTP command endpoint
    when no TUI WebSocket session is active so that hardware_service.py
    remains the sole owner of the pyvisa session — a direct in-process pyvisa
    session would race with hardware_service for the USB device and produce
    "[Errno 16] Resource busy".
    """
    device_name, net_info, channel = resolve_net_proxy(
        netname, "battery", BatteryBackendError
    )
    battery = Device(device_name, net_info)
    return battery, channel


def register_battery_routes(app: Flask) -> None:
    """
    Register battery HTTP routes on the Flask app.

    Args:
        app: Flask application instance
    """

    @app.route('/battery/command', methods=['POST'])
    def battery_command_http():
        """
        Execute a battery command via hardware_service.py:/invoke.

        If a TUI WebSocket session is already active for this net, reuse its
        cached Device proxy. Otherwise, build a transient proxy via
        resolve_net_proxy() — also routed through hardware_service — so the
        call still goes through the single pyvisa-owning service rather than
        opening a competing in-process pyvisa session.

        Request body:
        {
            "netname": "battery1",
            "action": "soc" | "voc" | "enable" | "disable" | "ocp" | "ovp" | etc.,
            "params": {"value": 80}  # Optional, for set operations
        }

        Returns:
        {
            "success": true,
            "action": "soc",
            "message": "SOC set to 80%"
        }
        """
        try:
            data = request.get_json()
            netname = data.get('netname')
            action = data.get('action')
            params = data.get('params', {})

            if not netname or not action:
                return jsonify({'success': False, 'error': 'netname and action are required'}), 400

            # Find an active WebSocket session for this netname.
            # SCPI serialization is now handled inside hardware_service.py
            # by per-cache-key locks, so the old per-netname instrument_lock
            # is no longer needed here.
            session_id = None
            battery = None
            channel = None

            with active_battery_sessions_lock:
                for sid, session_info in active_battery_sessions.items():
                    if session_info.get('netname') == netname:
                        session_id = sid
                        battery = session_info.get('battery')
                        channel = session_info.get('channel')
                        break

            if not battery:
                # No active TUI session: build a transient hardware_service-routed
                # proxy. Using a fresh in-process pyvisa session here would race
                # with hardware_service.py's cached session for the same USB
                # device (e.g. left over from a recently-exited TUI) and produce
                # "[Errno 16] Resource busy".
                try:
                    battery, channel = _resolve_battery_proxy(netname)
                except BatteryBackendError as e:
                    return jsonify({'success': False, 'error': str(e)}), 404
                except (ConnectionFailed, DeviceError) as e:
                    logger.exception(
                        f"[HTTP] Could not build transient battery proxy for {netname}"
                    )
                    return jsonify({
                        'success': False,
                        'error': f'Hardware service error: {e}',
                    }), 502

            # Execute the command using the same logic as WebSocket commands
            result = {'success': True, 'action': action}
            value = params.get('value')

            try:
                if action == 'set_soc':
                    if value is not None:
                        battery.set_soc(value)
                        result['message'] = f'SOC set to {value}%'
                    else:
                        soc = battery._safe_query(':BATT:SIM:SOC?', '0')
                        result['message'] = f'SOC: {soc}%'

                elif action == 'set_voc':
                    if value is not None:
                        battery.set_voc(value)
                        result['message'] = f'VOC set to {value}V'
                    else:
                        voc = battery._safe_query(':BATT:SIM:VOC?', '0')
                        result['message'] = f'VOC: {voc}V'

                elif action == 'set_volt_full':
                    if value is not None:
                        battery.set_volt_full(value)
                        result['message'] = f'Battery full voltage set to {value}V'
                    else:
                        volt_full = battery._safe_query(':BATT:SIM:VOC:FULL?', '4.2')
                        result['message'] = f'Battery full voltage: {volt_full}V'

                elif action == 'set_volt_empty':
                    if value is not None:
                        battery.set_volt_empty(value)
                        result['message'] = f'Battery empty voltage set to {value}V'
                    else:
                        volt_empty = battery._safe_query(':BATT:SIM:VOC:EMPT?', '3.0')
                        result['message'] = f'Battery empty voltage: {volt_empty}V'

                elif action == 'set_capacity':
                    if value is not None:
                        battery.set_capacity(value)
                        result['message'] = f'Capacity set to {value}Ah'
                    else:
                        capacity = battery._safe_query(':BATT:SIM:CAP:LIM?', '1.0')
                        result['message'] = f'Capacity: {capacity}Ah'

                elif action == 'set_current_limit':
                    if value is not None:
                        battery.set_current_limit(value)
                        result['message'] = f'Current limit set to {value}A'
                    else:
                        curr_lim = battery._safe_query(':BATT:SIM:CURR:LIM?', '1.0')
                        result['message'] = f'Current limit: {curr_lim}A'

                elif action == 'set_ocp':
                    if value is not None:
                        battery.set_ocp(value)
                        result['message'] = f'OCP limit set to {value}A'
                    else:
                        ocp = battery._safe_query(':BATT:SIM:CURR:PROT?', '2.0')
                        result['message'] = f'OCP limit: {ocp}A'

                elif action == 'set_ovp':
                    if value is not None:
                        battery.set_ovp(value)
                        result['message'] = f'OVP limit set to {value}V'
                    else:
                        ovp = battery._safe_query(':BATT:SIM:TVOL:PROT?', '4.5')
                        result['message'] = f'OVP limit: {ovp}V'

                elif action == 'set_mode':
                    mode_type = params.get('mode_type')
                    if mode_type is not None:
                        battery.set_mode(mode_type)
                        result['message'] = f'Mode set to {mode_type}'
                    else:
                        mode = battery._mode_string()
                        result['message'] = f'Mode: {mode}'

                elif action == 'set_model':
                    partnumber = params.get('partnumber')
                    if partnumber is not None:
                        battery.set_model(partnumber)
                        result['message'] = f'Model set to {partnumber}'
                    else:
                        model = battery._safe_query(':BATT:STAT?', '') or 'Custom'
                        result['message'] = f'Model: {model}'

                elif action == 'enable_battery':
                    battery.enable()
                    result['message'] = 'Battery output enabled'

                elif action == 'disable_battery':
                    battery.disable()
                    result['message'] = 'Battery output disabled'

                elif action == 'set_to_battery_mode':
                    battery.set_to_battery_mode()
                    result['message'] = 'Battery simulator mode initialized'

                elif action == 'clear_ocp':
                    battery.clear_ocp()
                    result['message'] = 'OCP trip cleared'

                elif action == 'clear_ovp':
                    battery.clear_ovp()
                    result['message'] = 'OVP trip cleared'

                elif action == 'clear':
                    battery.clear()
                    result['message'] = 'Protection trips cleared'

                elif action in ('state', 'print_state'):
                    # Get current state from the battery. Accept both action
                    # names: the supply CLI sends 'state' and the battery CLI
                    # sends 'print_state' (the dispatcher function name).
                    # Without the alias the CLI fell through to the python:5000
                    # dispatcher path, which opened a second pyvisa session and
                    # raced with hardware_service's shared session — surfaced
                    # as "Could not open instrument at ...: failed to set
                    # configuration [Errno 16] Resource busy" right after a
                    # successful supply command.
                    enabled = battery._is_batt_output_on()
                    mode_str = battery._mode_string()
                    model_str = battery._safe_query(':BATT:STAT?', '') or 'Custom'
                    soc = battery._safe_query(':BATT:SIM:SOC?', '0')
                    tvol = battery._safe_query(':BATT:SIM:TVOL?', '0')
                    curr = battery._safe_query(':BATT:SIM:CURR?', '0')

                    state_str = "ON" if enabled else "OFF"
                    result['message'] = f'Channel {channel}: {state_str}, Mode: {mode_str}, Model: {model_str}, SOC: {soc}%, Voltage: {tvol}V, Current: {curr}A'

                else:
                    return jsonify({'success': False, 'error': f'Unknown action: {action}'}), 400

                logger.info(f"[HTTP] Battery command executed: {action} on {netname} (session {session_id})")
                return jsonify(result)
            except (ConnectionFailed, DeviceError) as e:
                logger.exception(f"[HTTP] Battery command {action} on {netname} failed at hardware_service")
                return jsonify({'success': False, 'error': f'Hardware service error: {e}'}), 502

        except Exception as e:
            logger.exception(f"[HTTP] Error executing battery command")
            return jsonify({'success': False, 'error': str(e)}), 500


def register_battery_socketio(socketio: SocketIO) -> None:
    """
    Register battery WebSocket handlers on the SocketIO instance.

    Args:
        socketio: Flask-SocketIO instance
    """

    @socketio.on('connect', namespace='/battery')
    def handle_battery_connect():
        """Handle WebSocket connection for battery monitoring."""
        logger.info(f"Battery WebSocket client connected: {request.sid}")
        emit('connected', {'status': 'ready', 'session_id': request.sid})

    @socketio.on('disconnect', namespace='/battery')
    def handle_battery_disconnect():
        """Handle WebSocket disconnection for battery monitoring."""
        logger.info(f"Battery WebSocket client disconnected: {request.sid}")

        # Clean up any active battery monitoring session
        with active_battery_sessions_lock:
            if request.sid in active_battery_sessions:
                session = active_battery_sessions[request.sid]

                # Stop the monitoring thread
                if 'stop_event' in session:
                    session['stop_event'].set()

                # Remove from active sessions
                del active_battery_sessions[request.sid]
                logger.info(f"Cleaned up battery session: {request.sid}")

    @socketio.on('start_battery_monitor', namespace='/battery')
    def handle_start_battery_monitor(data):
        """
        Start battery monitoring session.

        Expected data:
        {
            "netname": "battery1",
            "interval": 1.0  # Update interval in seconds (default: 1.0)
        }
        """
        try:
            netname = data.get('netname')
            interval = data.get('interval', 1.0)

            if not netname:
                emit('error', {'message': 'netname is required'})
                return

            # Check if session already exists
            with active_battery_sessions_lock:
                if request.sid in active_battery_sessions:
                    emit('error', {'message': 'Battery monitoring session already active'})
                    return

            # Create stop event for thread control
            stop_event = threading.Event()

            # Capture session ID before starting thread
            session_id = request.sid

            # Store session info. The battery driver (a Device proxy) will be
            # added by the monitoring thread once it has resolved net_info.
            # SCPI serialization is handled inside hardware_service.py by
            # per-cache-key locks; no per-netname lock needed here anymore.
            with active_battery_sessions_lock:
                active_battery_sessions[session_id] = {
                    'netname': netname,
                    'stop_event': stop_event,
                    'interval': interval,
                    'battery': None,  # Will be set by monitoring thread
                    'channel': None,  # Will be set by monitoring thread
                }

            # Send connection success
            emit('battery_monitor_started', {
                'netname': netname,
                'interval': interval,
                'message': f'Started monitoring battery: {netname}'
            })

            # Start monitoring thread.
            #
            # The battery driver is a Device HTTP proxy that POSTs to
            # hardware_service.py:/invoke. hardware_service owns the pyvisa
            # session and serializes concurrent calls per cache key, so this
            # thread no longer needs the USB/pyvisa import dance, the
            # per-netname instrument_lock, or a driver-close finally.
            def monitor_battery():
                """Monitor battery state and emit updates to WebSocket."""
                import threading

                thread_name = threading.current_thread().name
                logger.info(f"[BATTERY-MONITOR-{thread_name}] Monitoring thread starting for {netname}")

                try:
                    device_name, net_info, channel = resolve_net_proxy(
                        netname, "battery", BatteryBackendError
                    )
                    battery = Device(device_name, net_info)
                    logger.info(
                        f"[BATTERY-MONITOR-{thread_name}] Got battery proxy: {device_name} "
                        f"channel {channel} address {net_info.get('address')}"
                    )
                except Exception as resolve_err:
                    import traceback
                    logger.error(
                        f"[BATTERY-MONITOR-{thread_name}] Failed to resolve battery '{netname}': {resolve_err}"
                    )
                    logger.error(f"[BATTERY-MONITOR-{thread_name}] Traceback: {traceback.format_exc()}")
                    socketio.emit(
                        'error',
                        {'message': f"Could not resolve battery '{netname}': {resolve_err}"},
                        namespace='/battery',
                        room=session_id,
                    )
                    return

                # Store battery proxy and channel in session for command execution.
                try:
                    with active_battery_sessions_lock:
                        if session_id in active_battery_sessions:
                            active_battery_sessions[session_id]['battery'] = battery
                            active_battery_sessions[session_id]['channel'] = channel
                            logger.info(f"[BATTERY-MONITOR-{thread_name}] Stored battery proxy in session")
                except Exception as store_err:
                    import traceback
                    logger.error(
                        f"[BATTERY-MONITOR-{thread_name}] Failed to store battery session for {netname}: {store_err}"
                    )
                    logger.error(f"[BATTERY-MONITOR-{thread_name}] Traceback: {traceback.format_exc()}")
                    socketio.emit(
                        'error',
                        {'message': f"Internal error registering battery '{netname}' session: {store_err}"},
                        namespace='/battery',
                        room=session_id,
                    )
                    return

                # Notify client that driver is ready for commands. Mirrors the
                # supply monitor's supply_driver_ready event for client symmetry.
                socketio.emit('battery_driver_ready', {
                    'netname': netname,
                    'channel': channel,
                    'message': f'Battery driver ready for {netname}'
                }, namespace='/battery', room=session_id)
                logger.info(f"[BATTERY-MONITOR-{thread_name}] Emitted battery_driver_ready event")

                while not stop_event.is_set():
                    try:
                        # Each call below is one /invoke POST; hardware_service
                        # serializes per-device internally. Underscore methods
                        # (_safe_query, _is_batt_output_on, _mode_string) proxy
                        # through Device.__getattr__ since Python only suppresses
                        # dunder lookups, not single-underscore names.
                        enabled = battery._is_batt_output_on()
                        mode_str = battery._mode_string()
                        model_str = battery._safe_query(":BATT:STAT?", "") or "Custom"

                        state = {
                            'netname': netname,
                            'channel': channel,
                            'terminal_voltage': float(battery._safe_query(":BATT:SIM:TVOL?", "0")),
                            'current': float(battery._safe_query(":BATT:SIM:CURR?", "0")),
                            'esr': float(battery._safe_query(":BATT:SIM:RES?", "0.067")),
                            'soc': float(battery._safe_query(":BATT:SIM:SOC?", "0")),
                            'voc': float(battery._safe_query(":BATT:SIM:VOC?", "0")),
                            'enabled': enabled,
                            'mode': mode_str,
                            'model': model_str,
                            'capacity': float(battery._safe_query(":BATT:SIM:CAP:LIM?", "1.0")),
                            'current_limit': float(battery._safe_query(":BATT:SIM:CURR:LIM?", "1.0")),
                            'ocp_limit': float(battery._safe_query(":BATT:SIM:CURR:PROT?", "2.0")),
                            'ovp_limit': float(battery._safe_query(":BATT:SIM:TVOL:PROT?", "4.5")),
                            'volt_full': float(battery._safe_query(":BATT:SIM:VOC:FULL?", "4.2")),
                            'volt_empty': float(battery._safe_query(":BATT:SIM:VOC:EMPT?", "3.0")),
                        }

                        # Get protection trip status
                        trip = (battery._safe_query(":OUTP:PROT:TRIP?", "").upper() or "")
                        state['ocp_tripped'] = (trip == "OCP")
                        state['ovp_tripped'] = (trip == "OVP")

                        socketio.emit('battery_state_update',
                                    {'state': state},
                                    namespace='/battery',
                                    room=session_id)
                    except ConnectionFailed as e:
                        logger.error(f"[BATTERY-MONITOR-{thread_name}] hardware_service unreachable: {e}")
                        socketio.emit('error',
                                    {'message': f'Hardware service unreachable: {e}'},
                                    namespace='/battery',
                                    room=session_id)
                    except DeviceError as e:
                        logger.error(f"[BATTERY-MONITOR-{thread_name}] hardware_service error: {e}")
                        socketio.emit('error',
                                    {'message': f'Hardware service error: {e}'},
                                    namespace='/battery',
                                    room=session_id)
                    except Exception as e:
                        logger.error(f"[BATTERY-MONITOR-{thread_name}] Error monitoring battery: {e}")
                        socketio.emit('error',
                                    {'message': f'Monitoring error: {str(e)}'},
                                    namespace='/battery',
                                    room=session_id)

                    # Wait for interval or stop event
                    stop_event.wait(interval)

                logger.info(f"Battery monitoring thread stopped for session {session_id}")

            # Start monitoring thread
            monitor_thread = threading.Thread(target=monitor_battery, daemon=True)
            monitor_thread.start()

            # Store thread reference
            with active_battery_sessions_lock:
                if session_id in active_battery_sessions:
                    active_battery_sessions[session_id]['thread'] = monitor_thread

        except Exception as e:
            logger.exception("Error in handle_start_battery_monitor")
            emit('error', {'message': str(e)})

    @socketio.on('battery_command', namespace='/battery')
    def handle_battery_command(data):
        """
        Execute a battery command.

        Expected data:
        {
            "netname": "battery1",
            "action": "soc" | "voc" | "enable" | "disable" | "ocp" | "ovp" | "mode" | "model" | etc.,
            "params": {...}  # Action-specific parameters
        }
        """
        try:
            netname = data.get('netname')
            action = data.get('action')
            params = data.get('params', {})

            if not netname:
                emit('battery_command_response', {'success': False, 'error': 'netname is required'})
                return

            if not action:
                emit('battery_command_response', {'success': False, 'error': 'action is required'})
                return

            import threading

            thread_name = threading.current_thread().name
            logger.info(f"[BATTERY-COMMAND-{thread_name}] Processing command: action={action}, netname={netname}, params={params}")

            # Get battery proxy and channel from session (set by monitoring thread).
            # SCPI serialization is handled inside hardware_service.py per cache key,
            # so the old per-netname instrument_lock is no longer needed here.
            session_id = request.sid
            battery = None
            channel = None

            with active_battery_sessions_lock:
                if session_id in active_battery_sessions:
                    battery = active_battery_sessions[session_id].get('battery')
                    channel = active_battery_sessions[session_id].get('channel')

            if not battery:
                emit('battery_command_response', {
                    'success': False,
                    'error': 'Battery driver not available. Is monitoring active?'
                })
                return

            logger.info(f"[BATTERY-COMMAND-{thread_name}] Using battery proxy on channel {channel}")

            result = {'success': True, 'action': action}

            try:
                if action == 'soc':
                    value = params.get('value')
                    if value is not None:
                        battery.set_soc(value)
                        result['message'] = f'SOC set to {value}%'
                    else:
                        soc = battery._safe_query(':BATT:SIM:SOC?', '0')
                        result['message'] = f'SOC: {soc}%'

                elif action == 'voc':
                    value = params.get('value')
                    if value is not None:
                        battery.set_voc(value)
                        result['message'] = f'VOC set to {value}V'
                    else:
                        voc = battery._safe_query(':BATT:SIM:VOC?', '0')
                        result['message'] = f'VOC: {voc}V'

                elif action == 'batt_full':
                    value = params.get('value')
                    if value is not None:
                        battery.set_volt_full(value)
                        result['message'] = f'Battery full voltage set to {value}V'
                    else:
                        volt_full = battery._safe_query(':BATT:SIM:VOC:FULL?', '4.2')
                        result['message'] = f'Battery full voltage: {volt_full}V'

                elif action == 'batt_empty':
                    value = params.get('value')
                    if value is not None:
                        battery.set_volt_empty(value)
                        result['message'] = f'Battery empty voltage set to {value}V'
                    else:
                        volt_empty = battery._safe_query(':BATT:SIM:VOC:EMPT?', '3.0')
                        result['message'] = f'Battery empty voltage: {volt_empty}V'

                elif action == 'capacity':
                    value = params.get('value')
                    if value is not None:
                        battery.set_capacity(value)
                        result['message'] = f'Capacity set to {value}Ah'
                    else:
                        capacity = battery._safe_query(':BATT:SIM:CAP:LIM?', '1.0')
                        result['message'] = f'Capacity: {capacity}Ah'

                elif action == 'current_limit':
                    value = params.get('value')
                    if value is not None:
                        battery.set_current_limit(value)
                        result['message'] = f'Current limit set to {value}A'
                    else:
                        curr_lim = battery._safe_query(':BATT:SIM:CURR:LIM?', '1.0')
                        result['message'] = f'Current limit: {curr_lim}A'

                elif action == 'ocp':
                    value = params.get('value')
                    if value is not None:
                        battery.set_ocp(value)
                        result['message'] = f'OCP limit set to {value}A'
                    else:
                        ocp = battery._safe_query(':BATT:SIM:CURR:PROT?', '2.0')
                        result['message'] = f'OCP limit: {ocp}A'

                elif action == 'ovp':
                    value = params.get('value')
                    if value is not None:
                        battery.set_ovp(value)
                        result['message'] = f'OVP limit set to {value}V'
                    else:
                        ovp = battery._safe_query(':BATT:SIM:TVOL:PROT?', '4.5')
                        result['message'] = f'OVP limit: {ovp}V'

                elif action == 'mode':
                    mode_type = params.get('mode_type')
                    if mode_type is not None:
                        battery.set_mode(mode_type)
                        result['message'] = f'Mode set to {mode_type}'
                    else:
                        mode = battery._mode_string()
                        result['message'] = f'Mode: {mode}'

                elif action == 'model':
                    partnumber = params.get('partnumber')
                    if partnumber is not None:
                        battery.set_model(partnumber)
                        result['message'] = f'Model set to {partnumber}'
                    else:
                        model = battery._safe_query(':BATT:STAT?', '') or 'Custom'
                        result['message'] = f'Model: {model}'

                elif action == 'enable':
                    battery.enable()
                    result['message'] = 'Battery output enabled'

                elif action == 'disable':
                    battery.disable()
                    result['message'] = 'Battery output disabled'

                elif action == 'set':
                    battery.set_to_battery_mode()
                    result['message'] = 'Battery simulator mode initialized'

                elif action == 'clear_ocp':
                    battery.clear_ocp()
                    result['message'] = 'OCP trip cleared'

                elif action == 'clear_ovp':
                    battery.clear_ovp()
                    result['message'] = 'OVP trip cleared'

                elif action == 'clear':
                    battery.clear()
                    result['message'] = 'Protection trips cleared'

                elif action == 'state':
                    # State is already being monitored and pushed to client
                    result['message'] = 'State retrieved (see monitor updates)'

                else:
                    result['success'] = False
                    result['error'] = f'Unknown action: {action}'

                logger.info(f"[BATTERY-COMMAND-{thread_name}] Command completed successfully: {result}")
                emit('battery_command_response', result)

            except (ConnectionFailed, DeviceError) as e:
                logger.error(f"[BATTERY-COMMAND-{thread_name}] hardware_service error: {e}")
                emit('battery_command_response', {
                    'success': False,
                    'error': f'Hardware service error: {e}'
                })
            except Exception as e:
                import traceback
                logger.error(f"[BATTERY-COMMAND-{thread_name}] Command execution error: {e}")
                logger.error(f"[BATTERY-COMMAND-{thread_name}] Traceback: {traceback.format_exc()}")
                emit('battery_command_response', {
                    'success': False,
                    'error': str(e)
                })

        except Exception as e:
            logger.exception("Error in handle_battery_command")
            emit('battery_command_response', {
                'success': False,
                'error': str(e)
            })

    @socketio.on('stop_battery_monitor', namespace='/battery')
    def handle_stop_battery_monitor():
        """Stop the battery monitoring session."""
        try:
            with active_battery_sessions_lock:
                if request.sid in active_battery_sessions:
                    session = active_battery_sessions[request.sid]

                    # Stop the monitoring thread
                    if 'stop_event' in session:
                        session['stop_event'].set()

                    # Remove from active sessions
                    del active_battery_sessions[request.sid]

            emit('battery_monitor_stopped', {'message': 'Battery monitoring stopped'})
            logger.info(f"Battery monitoring stopped: {request.sid}")

        except Exception as e:
            logger.exception("Error in handle_stop_battery_monitor")
            emit('error', {'message': str(e)})


def cleanup_battery_sessions():
    """Clean up all active battery sessions. Called during graceful shutdown."""
    with active_battery_sessions_lock:
        for session_id, session in list(active_battery_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
            except Exception as e:
                logger.error(f"Error cleaning up battery session {session_id}: {e}")
        active_battery_sessions.clear()

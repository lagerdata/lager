# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Supply HTTP and WebSocket handlers for the Lager Box HTTP+WebSocket Server.

This module handles power supply monitoring and control:
- HTTP endpoint for supply commands (reuses TUI session)
- WebSocket namespace /supply for real-time monitoring and control
"""
import logging
import threading
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit

from .state import (
    active_supply_sessions,
    active_supply_sessions_lock,
    get_instrument_lock,
)

logger = logging.getLogger(__name__)


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
        Execute a supply command using an active WebSocket session's supply driver.

        This allows CLI commands from other terminals to work while the TUI is running,
        by reusing the WebSocket session's USB connection instead of trying to open a new one.

        Request body:
        {
            "netname": "supply1",
            "action": "voltage" | "current" | "enable" | "disable" | "ocp" | "ovp",
            "params": {"value": 3.3}  # Optional, for set operations
        }

        Returns:
        {
            "success": true,
            "action": "voltage",
            "message": "Voltage set to 3.3V"
        }
        """
        try:
            data = request.get_json()
            netname = data.get('netname')
            action = data.get('action')
            params = data.get('params', {})

            if not netname or not action:
                return jsonify({'success': False, 'error': 'netname and action are required'}), 400

            # Find an active WebSocket session for this netname and get instrument lock
            session_id = None
            supply = None
            channel = None
            voltage_max = 0
            current_max = 0
            instr_lock = None

            with active_supply_sessions_lock:
                for sid, session_info in active_supply_sessions.items():
                    if session_info.get('netname') == netname:
                        session_id = sid
                        supply = session_info.get('supply')
                        channel = session_info.get('channel')
                        voltage_max = session_info.get('voltage_max', 0)
                        current_max = session_info.get('current_max', 0)
                        instr_lock = session_info.get('instrument_lock')
                        break

            if not supply:
                return jsonify({
                    'success': False,
                    'error': f'No active WebSocket session found for {netname}. Start the TUI first with: lager supply {netname} tui'
                }), 404

            # CRITICAL: Lock instrument access to prevent concurrent queries
            if instr_lock:
                instr_lock.acquire()

            # Execute the command using the same logic as WebSocket commands
            result = {'success': True, 'action': action}
            value = params.get('value')

            try:
                if action == 'voltage':
                    if value is not None and voltage_max > 0 and value > voltage_max:
                        return jsonify({'success': False, 'error': f'Voltage {value}V exceeds hardware limit {voltage_max}V'}), 400
                    if value is not None:
                        supply.voltage(value=value)
                        result['message'] = f'Voltage set to {value}V'
                    else:
                        v_set = float(supply.get_channel_voltage(source=channel))
                        result['message'] = f'Voltage setpoint: {v_set}V'

                elif action == 'current':
                    if value is not None and current_max > 0 and value > current_max:
                        return jsonify({'success': False, 'error': f'Current {value}A exceeds hardware limit {current_max}A'}), 400
                    if value is not None:
                        supply.current(value=value)
                        result['message'] = f'Current limit set to {value}A'
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
                    # Get current state from the supply
                    enabled = supply.output_is_enabled(channel=channel)
                    voltage_set = float(supply.get_channel_voltage(source=channel))
                    current_set = float(supply.get_channel_current(source=channel))

                    # Try to get measurements if output is on
                    try:
                        if enabled:
                            voltage_meas = float(supply.measure_voltage())
                            current_meas = float(supply.measure_current())
                        else:
                            voltage_meas = 0.0
                            current_meas = 0.0
                    except Exception:
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

                else:
                    return jsonify({'success': False, 'error': f'Unknown action: {action}'}), 400

                logger.info(f"[HTTP] Supply command executed: {action} on {netname} (session {session_id})")
                return jsonify(result)
            finally:
                # CRITICAL: Always release instrument lock
                if instr_lock and instr_lock.locked():
                    instr_lock.release()

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
            interval = data.get('interval', 1.0)

            if not netname:
                emit('error', {'message': 'netname is required'})
                return

            # Check if session already exists
            with active_supply_sessions_lock:
                if request.sid in active_supply_sessions:
                    emit('error', {'message': 'Supply monitoring session already active'})
                    return

            # Create stop event for thread control
            stop_event = threading.Event()

            # Capture session ID before starting thread
            session_id = request.sid

            # Get or create instrument lock for this netname (prevents concurrent SCPI queries)
            instr_lock = get_instrument_lock(netname)

            # Store session info (supply driver will be added by monitoring thread)
            with active_supply_sessions_lock:
                active_supply_sessions[session_id] = {
                    'netname': netname,
                    'stop_event': stop_event,
                    'interval': interval,
                    'supply': None,  # Will be set by monitoring thread
                    'channel': None,  # Will be set by monitoring thread
                    'instrument_lock': instr_lock  # Shared lock for this instrument
                }

            # Send connection success
            emit('supply_monitor_started', {
                'netname': netname,
                'interval': interval,
                'message': f'Started monitoring supply: {netname}'
            })

            # Start monitoring thread
            def monitor_supply():
                """Monitor supply state and emit updates to WebSocket."""
                import time
                import json
                import sys
                import threading as thread_module

                thread_name = thread_module.current_thread().name
                logger.info(f"[MONITOR-{thread_name}] Monitoring thread starting for {netname}")

                # CRITICAL: Import USB modules BEFORE importing dispatcher
                # The dispatcher imports driver classes which import pyvisa at module level
                # If USB isn't available when pyvisa loads, it permanently caches the failure
                try:
                    logger.info(f"[MONITOR-{thread_name}] Importing USB modules...")
                    # CRITICAL: Import parent package first, then use 'from usb import' for submodules
                    # Direct 'import usb.util' fails in Flask-SocketIO threads!
                    import usb
                    logger.info(f"[MONITOR-{thread_name}] Imported usb package: {usb}")
                    from usb import util, core
                    logger.info(f"[MONITOR-{thread_name}] USB submodules imported: util={util}, core={core}")

                    sys.modules['usb'] = usb
                    sys.modules['usb.util'] = util
                    sys.modules['usb.core'] = core
                    logger.info(f"[MONITOR-{thread_name}] USB modules registered in sys.modules")

                    # Now import pyvisa to ensure it sees USB modules
                    logger.info(f"[MONITOR-{thread_name}] Importing pyvisa...")
                    import pyvisa
                    logger.info(f"[MONITOR-{thread_name}] PyVISA imported: {pyvisa}")

                    # Check if pyvisa can access USB backend
                    rm = pyvisa.ResourceManager()
                    logger.info(f"[MONITOR-{thread_name}] PyVISA ResourceManager created: {rm}")

                    # Finally safe to import dispatcher (which imports drivers -> pyvisa)
                    logger.info(f"[MONITOR-{thread_name}] Importing dispatcher...")
                    from lager.power.supply.dispatcher import _resolve_net_and_driver
                    logger.info(f"[MONITOR-{thread_name}] Dispatcher imported successfully")

                    # Clear ALL stale caches to ensure fresh connection
                    try:
                        # Clear Keysight E36xxx VISA resource cache (unified driver)
                        from lager.power.supply.keysight_e36000 import clear_resource_cache as clear_keysight_cache
                        clear_keysight_cache()
                        logger.info(f"[MONITOR-{thread_name}] Cleared Keysight E36xxx resource cache")
                    except Exception as cache_err:
                        logger.debug(f"[MONITOR-{thread_name}] Keysight cache clear: {cache_err}")

                    try:
                        # Clear dispatcher's driver cache
                        from lager.power.supply.dispatcher import SupplyDispatcher
                        SupplyDispatcher.clear_cache()
                        logger.info(f"[MONITOR-{thread_name}] Cleared dispatcher driver cache")
                    except Exception as cache_err:
                        logger.debug(f"[MONITOR-{thread_name}] Dispatcher cache clear: {cache_err}")

                    # Clear the hardware_service.py device cache (port 8080).
                    # That service holds an open VISA session per device for /invoke calls
                    # (e.g. `lager supply <net> state`). Some instruments (Rigol DP821)
                    # cannot tolerate a second concurrent VISA session — without this clear,
                    # the fresh session opened a few lines below by _resolve_net_and_driver()
                    # hangs or fails silently, and supply_driver_ready never fires.
                    try:
                        import requests
                        requests.post('http://127.0.0.1:8080/cache/clear', timeout=2.0)
                        logger.info(f"[MONITOR-{thread_name}] Cleared hardware_service device cache")
                    except Exception as cache_err:
                        logger.warning(
                            f"[MONITOR-{thread_name}] Could not clear hardware_service cache "
                            f"(continuing anyway): {cache_err}"
                        )
                except Exception as e:
                    import traceback
                    logger.error(f"[MONITOR-{thread_name}] Import error: {e}")
                    logger.error(f"[MONITOR-{thread_name}] Traceback: {traceback.format_exc()}")
                    socketio.emit('error',
                                {'message': f'Import error in monitoring thread: {str(e)}'},
                                namespace='/supply',
                                room=session_id)
                    return

                supply = None
                try:
                    try:
                        logger.info(f"[MONITOR-{thread_name}] Resolving net and driver for {netname}...")
                        supply, channel = _resolve_net_and_driver(netname)
                        logger.info(f"[MONITOR-{thread_name}] Got supply driver: {type(supply).__name__}, channel: {channel}")
                    except Exception as resolve_err:
                        import traceback
                        logger.error(
                            f"[MONITOR-{thread_name}] Failed to resolve supply driver for {netname}: {resolve_err}"
                        )
                        logger.error(
                            f"[MONITOR-{thread_name}] Traceback: {traceback.format_exc()}"
                        )
                        socketio.emit(
                            'error',
                            {
                                'message': (
                                    f"Could not open supply '{netname}': {resolve_err}"
                                )
                            },
                            namespace='/supply',
                            room=session_id,
                        )
                        return

                    # Get hardware limits for validation. AttributeError/NotImplementedError
                    # mean the driver legitimately doesn't expose limits — fall back to 0.
                    # Anything else (VISA timeout, "Resource busy", "Query INTERRUPTED", etc.)
                    # is a real failure on the very first SCPI query and the client deserves
                    # a visible error rather than a 15s silent timeout.
                    try:
                        limits = supply.get_channel_limits(channel)
                        voltage_max = limits.get('voltage_max', 0)
                        current_max = limits.get('current_max', 0)
                    except (AttributeError, NotImplementedError):
                        voltage_max = 0
                        current_max = 0
                    except Exception as limits_err:
                        import traceback
                        logger.error(
                            f"[MONITOR-{thread_name}] First SCPI query failed for {netname}: {limits_err}"
                        )
                        logger.error(
                            f"[MONITOR-{thread_name}] Traceback: {traceback.format_exc()}"
                        )
                        socketio.emit(
                            'error',
                            {
                                'message': (
                                    f"Supply '{netname}' opened but is not responding "
                                    f"(first query failed): {limits_err}"
                                )
                            },
                            namespace='/supply',
                            room=session_id,
                        )
                        return

                    # Store supply, channel, and limits in session for command execution.
                    # Wrap so a corrupt session map doesn't kill the thread silently.
                    try:
                        with active_supply_sessions_lock:
                            if session_id in active_supply_sessions:
                                active_supply_sessions[session_id]['supply'] = supply
                                active_supply_sessions[session_id]['channel'] = channel
                                active_supply_sessions[session_id]['voltage_max'] = voltage_max
                                active_supply_sessions[session_id]['current_max'] = current_max
                                logger.info(f"[MONITOR-{thread_name}] Stored supply driver in session (limits: {voltage_max}V, {current_max}A)")
                    except Exception as store_err:
                        import traceback
                        logger.error(
                            f"[MONITOR-{thread_name}] Failed to store supply session for {netname}: {store_err}"
                        )
                        logger.error(
                            f"[MONITOR-{thread_name}] Traceback: {traceback.format_exc()}"
                        )
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

                    # Get instrument lock for thread-safe SCPI queries
                    with active_supply_sessions_lock:
                        instr_lock_local = active_supply_sessions.get(session_id, {}).get('instrument_lock')

                    while not stop_event.is_set():
                        try:
                            # CRITICAL: Lock instrument access to prevent concurrent queries
                            # "Query INTERRUPTED" errors occur when multiple threads query simultaneously
                            with instr_lock_local:
                                # Get all state information using SCPI methods
                                state = {
                                    'netname': netname,
                                    'channel': channel,
                                    'voltage': float(supply.measure_voltage(channel)),
                                    'current': float(supply.measure_current(channel)),
                                    'power': float(supply.measure_power(channel)),
                                    'enabled': supply.output_is_enabled(channel),
                                    'mode': supply.get_output_mode(channel) if hasattr(supply, 'get_output_mode') else 'CV',
                                    'voltage_set': float(supply.get_channel_voltage(source=channel)),
                                    'current_set': float(supply.get_channel_current(source=channel)),
                                }

                                # Get channel limits
                                try:
                                    limits = supply.get_channel_limits(channel)
                                    state['voltage_max'] = limits.get('voltage_max', 0)
                                    state['current_max'] = limits.get('current_max', 0)
                                except (AttributeError, NotImplementedError):
                                    state['voltage_max'] = 0
                                    state['current_max'] = 0

                                # Get OCP/OVP if available
                                try:
                                    state['ocp_limit'] = float(supply.get_overcurrent_protection_value(channel))
                                    state['ocp_tripped'] = supply.overcurrent_protection_is_tripped(channel)
                                except (AttributeError, NotImplementedError):
                                    state['ocp_limit'] = None
                                    state['ocp_tripped'] = None

                                try:
                                    state['ovp_limit'] = float(supply.get_overvoltage_protection_value(channel))
                                    state['ovp_tripped'] = supply.overvoltage_protection_is_tripped(channel)
                                except (AttributeError, NotImplementedError):
                                    state['ovp_limit'] = None
                                    state['ovp_tripped'] = None

                            # Emit state to client (outside lock - network I/O doesn't need instrument)
                            socketio.emit('supply_state_update',
                                        {'state': state},
                                        namespace='/supply',
                                        room=session_id)
                        except Exception as e:
                            logger.error(f"Error monitoring supply: {e}")
                            socketio.emit('error',
                                        {'message': f'Monitoring error: {str(e)}'},
                                        namespace='/supply',
                                        room=session_id)

                        # Wait for interval or stop event
                        stop_event.wait(interval)
                finally:
                    # Close supply driver when done. ``supply`` may be None if
                    # _resolve_net_and_driver raised before assigning it.
                    if supply is not None:
                        try:
                            if hasattr(supply, 'close'):
                                supply.close()
                            elif hasattr(supply, 'instrument') and hasattr(supply.instrument, 'close'):
                                supply.instrument.close()
                        except Exception as e:
                            logger.error(f"Error closing supply driver: {e}")
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

            # CRITICAL: Import USB modules BEFORE importing dispatcher
            # The dispatcher imports driver classes which import pyvisa at module level
            import sys
            import threading as thread_module

            thread_name = thread_module.current_thread().name
            logger.info(f"[COMMAND-{thread_name}] Processing command: action={action}, netname={netname}, params={params}")

            try:
                logger.info(f"[COMMAND-{thread_name}] Importing USB modules...")
                # CRITICAL: Import parent package first, then use 'from usb import' for submodules
                # Direct 'import usb.util' fails in Flask-SocketIO threads!
                import usb
                logger.info(f"[COMMAND-{thread_name}] Imported usb package: {usb}")
                from usb import util, core
                logger.info(f"[COMMAND-{thread_name}] USB submodules imported: util={util}, core={core}")

                sys.modules['usb'] = usb
                sys.modules['usb.util'] = util
                sys.modules['usb.core'] = core
                logger.info(f"[COMMAND-{thread_name}] USB modules registered in sys.modules")

                # Import pyvisa to ensure it sees USB modules
                logger.info(f"[COMMAND-{thread_name}] Importing pyvisa...")
                import pyvisa
                logger.info(f"[COMMAND-{thread_name}] PyVISA imported")

                # Execute command using dispatcher functions
                logger.info(f"[COMMAND-{thread_name}] Importing dispatcher...")
                from lager.power.supply import dispatcher
                logger.info(f"[COMMAND-{thread_name}] Dispatcher imported successfully")
            except Exception as e:
                import traceback
                logger.error(f"[COMMAND-{thread_name}] Import error: {e}")
                logger.error(f"[COMMAND-{thread_name}] Traceback: {traceback.format_exc()}")
                emit('supply_command_response', {
                    'success': False,
                    'error': f'Import error: {str(e)}'
                })
                return

            # Get supply driver, limits, and instrument lock from session (shared with monitoring thread)
            session_id = request.sid
            supply = None
            channel = None
            voltage_max = 0
            current_max = 0
            instr_lock = None

            with active_supply_sessions_lock:
                if session_id in active_supply_sessions:
                    supply = active_supply_sessions[session_id].get('supply')
                    channel = active_supply_sessions[session_id].get('channel')
                    voltage_max = active_supply_sessions[session_id].get('voltage_max', 0)
                    current_max = active_supply_sessions[session_id].get('current_max', 0)
                    instr_lock = active_supply_sessions[session_id].get('instrument_lock')

            if not supply:
                emit('supply_command_response', {
                    'success': False,
                    'error': 'Supply driver not available. Is monitoring active?'
                })
                return

            logger.info(f"[COMMAND-{thread_name}] Using shared supply driver: {type(supply).__name__}, channel: {channel}, limits: {voltage_max}V, {current_max}A")

            result = {'success': True, 'action': action}

            try:
                # CRITICAL: Lock instrument access to prevent concurrent queries with monitoring thread
                if instr_lock:
                    logger.info(f"[COMMAND-{thread_name}] Acquiring instrument lock...")
                    instr_lock.acquire()
                    logger.info(f"[COMMAND-{thread_name}] Instrument lock acquired")
            except Exception as e:
                logger.error(f"[COMMAND-{thread_name}] Failed to acquire instrument lock: {e}")

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

            except Exception as e:
                import traceback
                logger.error(f"[COMMAND-{thread_name}] Command execution error: {e}")
                logger.error(f"[COMMAND-{thread_name}] Traceback: {traceback.format_exc()}")
                emit('supply_command_response', {
                    'success': False,
                    'error': str(e)
                })
            finally:
                # CRITICAL: Always release instrument lock
                if instr_lock and instr_lock.locked():
                    logger.info(f"[COMMAND-{thread_name}] Releasing instrument lock...")
                    instr_lock.release()
                    logger.info(f"[COMMAND-{thread_name}] Instrument lock released")

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

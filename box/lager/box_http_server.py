#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Lager Box HTTP+WebSocket Server

Standalone Flask+SocketIO server for the Python container that provides direct access
to hardware control functions without requiring the controller container.

This server runs independently and handles multiple hardware subsystems:
- UART: Serial communication (read-only HTTP streaming and interactive WebSocket)
- Supply: Power supply monitoring and control (WebSocket with real-time updates)
- Additional hardware services can be added here

Supports both HTTP and WebSocket for bidirectional communication.
"""
import sys
import os
import logging
import threading
import signal
from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, disconnect

# Add box_python to path
sys.path.insert(0, '/app/box_python')

# Pre-import modules that might have issues in threading context
# This fixes "No module named 'usb.util'" errors in Flask-SocketIO threads
try:
    # CRITICAL: Import parent package first, then submodules using 'from import'
    # Direct 'import usb.util' can fail in Flask-SocketIO threading context
    import usb
    from usb import util, core

    import pyvisa

    # Ensure these modules are in sys.modules so threads can find them
    sys.modules['usb'] = usb
    sys.modules['usb.util'] = util
    sys.modules['usb.core'] = core

    # Try to check if pyvisa can see USB
    try:
        import pyvisa_py
    except ImportError:
        pass

except ImportError as e:
    import traceback
    logging.getLogger(__name__).error("Could not pre-import USB modules: %s\n%s", e, traceback.format_exc())

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max request size
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or os.urandom(24).hex()

# Initialize SocketIO
# Force threading mode since eventlet 0.35.2 has issues with Python 3.12
socketio = SocketIO(
    app,
    cors_allowed_origins="*",  # Allow all origins for now (can restrict later)
    async_mode='threading',  # Force threading mode (eventlet has Python 3.12 compatibility issues)
    logger=False,
    engineio_logger=False,
    ping_timeout=60,
    ping_interval=25
)

# Import UART handlers from modular http package
from lager.http_handlers.uart import (
    active_uart_sessions,
    active_uart_sessions_lock,
    register_uart_routes,
    register_uart_socketio,
    cleanup_uart_sessions,
)

# Import supply handlers from modular http package
from lager.http_handlers.supply import (
    register_supply_routes,
    register_supply_socketio,
    cleanup_supply_sessions,
)
# Import battery handlers from modular http package (mirrors supply migration)
from lager.http_handlers.battery import (
    register_battery_routes,
    register_battery_socketio,
    cleanup_battery_sessions,
)

# Import nets handler
try:
    from lager.http_handlers.nets_handler import register_nets_routes
    _has_nets = True
except Exception as e:
    logger.warning("Nets handlers not available: %s", e)
    _has_nets = False

# Import instruments handler
try:
    from lager.http_handlers.instruments_handler import register_instruments_routes
    _has_instruments = True
except Exception as e:
    logger.warning("Instruments handlers not available: %s", e)
    _has_instruments = False

# Import lock handler
try:
    from lager.http_handlers.lock_handler import register_lock_routes
    _has_lock = True
except Exception as e:
    logger.warning("Lock handlers not available: %s", e)
    _has_lock = False

# Import SSH authorization handler
try:
    from lager.http_handlers.ssh_handler import register_ssh_routes
    _has_ssh = True
except Exception as e:
    logger.warning("SSH handlers not available: %s", e)
    _has_ssh = False

# Global dictionary to track active supply monitoring sessions
# Format: {session_id: {'netname': str, 'stop_event': event_obj, 'thread': thread_obj, 'instrument_lock': Lock}}
active_supply_sessions = {}
active_supply_sessions_lock = threading.Lock()

# Global dictionary to track active battery monitoring sessions
# Format: {session_id: {'netname': str, 'stop_event': event_obj, 'thread': thread_obj, 'instrument_lock': Lock}}
active_battery_sessions = {}
active_battery_sessions_lock = threading.Lock()

# Global dictionary to track instrument locks (one lock per netname to prevent concurrent SCPI queries)
# Format: {netname: threading.Lock()}
instrument_locks = {}
instrument_locks_lock = threading.Lock()


# Health check endpoint
@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        'status': 'healthy',
        'service': 'lager-python-box',
        'version': '1.0.0',
        'websocket': 'enabled'
    })


@app.route('/hello', methods=['GET'])
def hello():
    """Basic connectivity test."""
    return jsonify({
        'message': 'Hello from Lager Python Box!',
        'service': 'lager-python-box',
        'websocket': 'enabled'
    })


@app.route('/status', methods=['GET'])
def status():
    """Return box status for control plane probing."""
    import json as _json
    from lager.nets.constants import NetType

    version = 'unknown'
    try:
        with open('/etc/lager/version', 'r') as f:
            version_content = f.read().strip()
            version = version_content.split('|', 1)[0] if '|' in version_content else version_content
    except (FileNotFoundError, IOError):
        pass

    nets = []
    try:
        with open('/etc/lager/saved_nets.json', 'r') as f:
            saved_nets = _json.load(f)
        for net in saved_nets:
            role = net.get('role', '')
            try:
                net_type = NetType.from_role(role).name
            except (KeyError, ValueError):
                net_type = role
            nets.append({'name': net.get('name', ''), 'type': net_type})
    except (FileNotFoundError, _json.JSONDecodeError, TypeError):
        pass

    return jsonify({
        'healthy': True,
        'version': version,
        'nets': nets,
    })


# Register supply HTTP and WebSocket handlers from modular http package
register_supply_routes(app)
register_supply_socketio(socketio)

# Register battery HTTP and WebSocket handlers from modular http package
register_battery_routes(app)
register_battery_socketio(socketio)

# Register nets REST handlers (if available)
if _has_nets:
    register_nets_routes(app)
    logger.info("Nets REST endpoints registered")
    print("[INIT] Nets REST endpoints registered", flush=True)
else:
    print("[INIT] Nets REST endpoints NOT available", flush=True)

# Register instruments REST handlers (if available)
if _has_instruments:
    register_instruments_routes(app)
    logger.info("Instruments REST endpoints registered")
    print("[INIT] Instruments REST endpoints registered", flush=True)
else:
    print("[INIT] Instruments REST endpoints NOT available", flush=True)

# Register lock REST handlers (if available)
if _has_lock:
    register_lock_routes(app)
    logger.info("Lock REST endpoints registered")
    print("[INIT] Lock REST endpoints registered", flush=True)
else:
    print("[INIT] Lock REST endpoints NOT available", flush=True)

# Register SSH authorization REST handlers (if available)
if _has_ssh:
    register_ssh_routes(app)
    logger.info("SSH authorization REST endpoints registered")
    print("[INIT] SSH authorization REST endpoints registered", flush=True)
else:
    print("[INIT] SSH authorization REST endpoints NOT available", flush=True)


# Supply HTTP endpoint (handled by supply.py module, this is now commented out)
# @app.route('/supply/command', methods=['POST'])
def _supply_command_http_disabled():
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
                result['message'] = f'Channel {channel}: {state_str}, Set: {voltage_set}V/{current_set}A, Measured: {voltage_meas}V/{current_meas}A'

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


# Error handlers
@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors."""
    return jsonify({
        'error': 'Not found',
        'message': 'The requested endpoint does not exist'
    }), 404


@app.errorhandler(500)
def internal_error(e):
    """Handle 500 errors."""
    logger.exception("Internal server error")
    return jsonify({
        'error': 'Internal server error',
        'message': str(e)
    }), 500


def signal_handler(signum, frame):
    """Handle termination signals gracefully."""
    sig_name = signal.Signals(signum).name
    logger.info(f"Received signal {sig_name}, shutting down gracefully...")
    # Cleanup any active UART sessions (using modular cleanup function)
    cleanup_uart_sessions()
    # Cleanup any active supply sessions (using modular cleanup function)
    cleanup_supply_sessions()
    cleanup_battery_sessions()
    # Cleanup any active battery sessions
    with active_battery_sessions_lock:
        for session_id, session in list(active_battery_sessions.items()):
            try:
                if 'stop_event' in session:
                    session['stop_event'].set()
            except Exception as e:
                logger.error(f"Error cleaning up battery session {session_id}: {e}")
        active_battery_sessions.clear()
    logger.info("Cleanup complete, exiting")
    sys.exit(0)


def main():
    """
    Start the Flask + SocketIO server in standalone mode.

    Note: This is only used when running the script directly (e.g., for testing).
    In production, the app is run via Gunicorn with gevent workers.
    See: start-services.sh
    """
    # Ignore SIGPIPE to prevent server crash when client disconnects during streaming
    # SIGPIPE occurs when writing to a closed socket (e.g., HTTP streaming when client presses Ctrl+C)
    # By ignoring it, we let the Python exception handling (BrokenPipeError) deal with it gracefully
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Get port from environment or use default
    port = int(os.environ.get('LAGER_HTTP_PORT', 9000))
    host = os.environ.get('LAGER_HTTP_HOST', '0.0.0.0')

    logger.info(f"Starting Lager Python Box HTTP+WebSocket server on {host}:{port}")
    logger.info("Available endpoints:")
    logger.info("  GET  /health - Health check")
    logger.info("  GET  /hello - Connectivity test")
    logger.info("  GET  /uart/nets/list - List all saved nets")
    logger.info("  POST /uart/net/stream - UART streaming (HTTP, read-only)")
    logger.info("  POST /supply/command - Supply command (HTTP, reuses TUI session)")
    logger.info("  POST /battery/command - Battery command (HTTP, reuses TUI session)")
    logger.info("  WS   /uart - Interactive UART (WebSocket, bidirectional)")
    logger.info("  WS   /supply - Supply monitoring and control (WebSocket, bidirectional)")
    logger.info("  WS   /battery - Battery monitoring and control (WebSocket, bidirectional)")

    try:
        # Run SocketIO server (handles both HTTP and WebSocket)
        # Note: socketio.run() automatically uses threaded mode for better connection handling
        socketio.run(
            app,
            host=host,
            port=port,
            debug=False,
            use_reloader=False,
            allow_unsafe_werkzeug=True  # OK for low-traffic box use; consider Gunicorn for high-traffic deployments
        )
    except KeyboardInterrupt:
        logger.info("Server interrupted by user")
        signal_handler(signal.SIGINT, None)
    except Exception as e:
        logger.exception(f"Server crashed with error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()

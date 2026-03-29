#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Lager Debug Service - Persistent Box Service

This service runs continuously on the box, eliminating:
- Python interpreter startup overhead (~400ms)
- Module import overhead (~800ms)
- Script upload overhead (~200ms)

Total savings: ~1.4s per command

The service exposes a simple HTTP API for debug commands.
"""
import sys
import json
import logging
import signal
import time
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Dict, Any
import traceback

# Import all debug functions at startup (once!)
from lager.debug import (
    connect_jlink,
    disconnect,
    flash_device,
    chip_erase,
    get_jlink_status,
)
from lager.debug.gdb import get_controller, get_arch, reset as gdb_reset
from lager.debug.api import JLinkNotRunning
from lager.debug.jlink import JLink
from lager.debug.gdbserver import (
    start_jlink_gdbserver,
    stop_jlink_gdbserver,
    get_jlink_gdbserver_status,
)

# Temp path for J-Link script file (written during connect)
JLINK_SCRIPT_TEMP_PATH = '/tmp/lager_jlink_script.JLinkScript'


def _resolve_device_type(net: Dict[str, Any]) -> str:
    """Extract device type from net config. Raises ValueError if unresolvable."""
    device = net.get('channel') or net.get('pin')
    if not device or device == 'unknown':
        raise ValueError(
            f"Cannot determine device type from net '{net.get('name', '?')}'. "
            f"Check net configuration (expected 'channel' or 'pin' field)."
        )
    return device


def _get_script_file(net=None):
    """Write J-Link script to temp path and return it, or None.

    When *net* is a dict, prefer (in order): embedded ``jlink_script`` from the
    request body, then NetsCache by ``net['name']``. Only then fall back to an
    existing temp file from a previous connect — this avoids stale scripts and
    fixes erase/flash when the POST body carries the script but NetsCache lags.

    When *net* is a str, treat it as a net name (legacy callers).

    When *net* is None, only return the temp path if it already exists.
    """
    import os
    import base64
    from lager.cache import get_nets_cache

    def _write_b64_script(b64: str, source: str) -> str:
        with open(JLINK_SCRIPT_TEMP_PATH, 'wb') as f:
            f.write(base64.b64decode(b64))
        logger.info(f'Wrote J-Link script to {JLINK_SCRIPT_TEMP_PATH} ({source})')
        return JLINK_SCRIPT_TEMP_PATH

    if isinstance(net, dict):
        emb = net.get('jlink_script')
        if isinstance(emb, str) and emb.strip():
            try:
                return _write_b64_script(emb, 'POST net body')
            except Exception as e:
                logger.warning(f'Failed to write embedded jlink_script: {e}')
        name = net.get('name')
        if name:
            try:
                saved = get_nets_cache().find_by_name(name)
                if saved and saved.get('jlink_script'):
                    return _write_b64_script(saved['jlink_script'], f'NetsCache:{name}')
            except Exception as e:
                logger.warning(f'Failed to reconstruct J-Link script from NetsCache: {e}')
    elif isinstance(net, str) and net.strip():
        try:
            saved = get_nets_cache().find_by_name(net)
            if saved and saved.get('jlink_script'):
                return _write_b64_script(saved['jlink_script'], f'NetsCache:{net}')
        except Exception as e:
            logger.warning(f'Failed to reconstruct J-Link script from NetsCache: {e}')

    if os.path.exists(JLINK_SCRIPT_TEMP_PATH):
        logger.debug(f'Using existing J-Link script file {JLINK_SCRIPT_TEMP_PATH}')
        return JLINK_SCRIPT_TEMP_PATH
    return None


# Configure logging
# Log to /tmp which is writable by all users in the container
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('/tmp/lager-debug-service.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Service configuration
SERVICE_HOST = '0.0.0.0'  # Listen on all interfaces (container is isolated)
SERVICE_PORT = 8765
SERVICE_VERSION = '1.0.0'

# Track active connections (with thread safety for concurrent access)
active_connections = {}
connections_lock = threading.Lock()

# Service start time
start_time = None


class DebugServiceHandler(BaseHTTPRequestHandler):
    """HTTP request handler for debug service."""

    def log_message(self, format, *args):
        """Override to use our logger."""
        logger.info(format % args)

    def send_json_response(self, status_code: int, data: Dict[str, Any]):
        """Send JSON response."""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()

        response_json = json.dumps(data, indent=2)
        self.wfile.write(response_json.encode('utf-8'))

    def send_error_response(self, status_code: int, message: str):
        """Send error response."""
        self.send_json_response(status_code, {
            'error': message,
            'status': 'error'
        })

    def do_GET(self):
        """Handle GET requests."""
        if self.path == '/health':
            # Health check endpoint
            self.send_json_response(200, {
                'status': 'healthy',
                'version': SERVICE_VERSION,
                'uptime': time.time() - start_time,
            })
        elif self.path == '/health/detailed':
            # Detailed health check endpoint with connection state
            try:
                gdbserver_status = get_jlink_gdbserver_status()

                # Get GDB controller cache stats
                from lager.debug.gdb import _gdb_controller_cache, _gdb_use_counts
                gdb_controllers = len(_gdb_controller_cache)
                max_use_count = max(_gdb_use_counts.values()) if _gdb_use_counts else 0

                uptime_seconds = time.time() - start_time
                uptime_days = uptime_seconds / 86400

                health_data = {
                    'status': 'healthy',
                    'version': SERVICE_VERSION,
                    'jlink_gdbserver_running': gdbserver_status['running'],
                    'jlink_gdbserver_pid': gdbserver_status.get('pid'),
                    'gdb_controllers_cached': gdb_controllers,
                    'gdb_max_use_count': max_use_count,
                    'service_uptime_seconds': uptime_seconds,
                    'service_uptime_days': uptime_days,
                    'active_connections': len(active_connections),
                }

                # Warnings for operational issues
                warnings = []
                if uptime_days > 7:
                    warnings.append('Service has been running for >7 days, consider restart for consistency')
                if max_use_count >= 8:
                    warnings.append(f'GDB controller used {max_use_count} times, nearing refresh threshold')
                if gdb_controllers > 3:
                    warnings.append(f'{gdb_controllers} GDB controllers cached, may indicate multiple devices')

                if warnings:
                    health_data['warnings'] = warnings

                self.send_json_response(200, health_data)
            except Exception as e:
                logger.error(f"Error getting detailed health: {e}", exc_info=True)
                self.send_error_response(500, str(e))
        elif self.path == '/status':
            # Debug status endpoint
            try:
                gdbserver_status = get_jlink_gdbserver_status()

                with connections_lock:
                    conn_list = list(active_connections.keys())

                self.send_json_response(200, {
                    'jlink_gdbserver': gdbserver_status,
                    'active_connections': conn_list,
                })
            except Exception as e:
                logger.error(f"Error getting status: {e}", exc_info=True)
                self.send_error_response(500, str(e))
        else:
            self.send_error_response(404, 'Not Found')

    def do_POST(self):
        """Handle POST requests."""
        try:
            # Parse request body
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self.send_error_response(400, 'Empty request body')
                return

            body = self.rfile.read(content_length)
            request_data = json.loads(body.decode('utf-8'))

            # Route to appropriate handler
            if self.path == '/debug/connect':
                self.handle_connect(request_data)
            elif self.path == '/debug/disconnect':
                self.handle_disconnect(request_data)
            elif self.path == '/debug/reset':
                self.handle_reset(request_data)
            elif self.path == '/debug/flash':
                self.handle_flash(request_data)
            elif self.path == '/debug/erase':
                self.handle_erase(request_data)
            elif self.path == '/debug/memrd':
                self.handle_memrd(request_data)
            elif self.path == '/debug/info':
                self.handle_info(request_data)
            elif self.path == '/debug/status':
                self.handle_debug_status(request_data)
            elif self.path == '/debug/rtt':
                self.handle_rtt(request_data)
            else:
                self.send_error_response(404, f'Unknown endpoint: {self.path}')

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            self.send_error_response(400, f'Invalid JSON: {e}')
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def _get_jlink_script_from_net(self, net):
        """Resolve jlink_script: POST net body first, then saved_nets via NetsCache."""
        if not net:
            return None
        embedded = net.get('jlink_script')
        if isinstance(embedded, str) and embedded.strip():
            return embedded
        net_name = net.get('name')
        if not net_name:
            return None
        try:
            from lager.cache import get_nets_cache
            saved_net = get_nets_cache().find_by_name(net_name)
            if saved_net:
                return saved_net.get('jlink_script')
        except Exception as e:
            logger.warning(f'Failed to look up jlink_script from NetsCache: {e}')
        return None

    def handle_connect(self, data: Dict[str, Any]):
        """Handle debug connect command - starts JLinkGDBServer."""
        try:
            import base64

            net = data.get('net', {})
            device_type = _resolve_device_type(net)
            speed = data.get('speed', 'adaptive')
            force = data.get('force', False)
            halt = data.get('halt', False)
            gdb = data.get('gdb', True)  # Default to starting GDB server
            gdb_port = data.get('gdb_port', 2331)

            # Handle J-Link script file
            # Priority 1: Script sent in POST body (local .lager config override)
            # Priority 2: Script stored with net in saved_nets.json (NetsCache)
            jlink_script = data.get('jlink_script')
            if not jlink_script:
                jlink_script = self._get_jlink_script_from_net(net)
            script_file_path = None
            if jlink_script:
                try:
                    script_content = base64.b64decode(jlink_script)
                    script_file_path = '/tmp/lager_jlink_script.JLinkScript'
                    with open(script_file_path, 'wb') as f:
                        f.write(script_content)
                    logger.info(f'Wrote J-Link script to {script_file_path}')
                except Exception as e:
                    logger.warning(f'Failed to write J-Link script file: {e}')
                    script_file_path = None

            # Check if JLinkGDBServer is already running
            status = get_jlink_gdbserver_status()
            if status['running'] and not force:
                # Verify the running server matches the requested device
                net_name = net.get('name', 'unknown')
                connection_id = f"{net_name}:{device_type}"
                with connections_lock:
                    existing = active_connections.get(connection_id)

                if existing and existing.get('device') == device_type:
                    # Same device, reuse existing connection
                    logger.info(f'Reusing existing JLinkGDBServer connection (PID {status["pid"]})')
                    self.send_json_response(200, {
                        'status': 'connected',
                        'device': device_type,
                        'probe': net.get('instrument'),
                        'message': 'JLinkGDBServer ready for operations',
                        'pid': status['pid'],
                        'gdb_server': {
                            'status': 'already_running',
                            'gdb_port': gdb_port,
                            'pid': status['pid']
                        }
                    })
                    return
                else:
                    # Different device or unknown state -- restart
                    logger.info(f'Existing JLinkGDBServer does not match requested device {device_type}, restarting')
                    stop_jlink_gdbserver()
                    time.sleep(0.3)

            # Stop existing connection if force=True
            if status['running'] and force:
                logger.info('Force reconnect: stopping existing JLinkGDBServer')
                stop_jlink_gdbserver()
                time.sleep(0.3)  # Give hardware time to settle

            # Start JLinkGDBServer
            logger.info(f'Starting JLinkGDBServer for device {device_type}, speed {speed}')
            result = start_jlink_gdbserver(
                device=device_type,
                speed=speed,
                transport='SWD',
                halt=halt,
                gdb_port=gdb_port,
                script_file=script_file_path
            )

            # Track connection (thread-safe)
            net_name = net.get('name', 'unknown')
            connection_id = f"{net_name}:{device_type}"
            with connections_lock:
                active_connections[connection_id] = {
                    'timestamp': time.time(),
                    'device': device_type,
                    'pid': result['pid']
                }

            logger.info(f'JLinkGDBServer started successfully (PID {result["pid"]})')
            self.send_json_response(200, {
                'status': 'connected',
                'device': device_type,
                'probe': net.get('instrument'),
                'message': 'JLinkGDBServer started successfully',
                'pid': result['pid'],
                'gdb_server': {
                    'status': 'started',
                    'gdb_port': gdb_port,
                    'pid': result['pid']
                }
            })

        except Exception as e:
            logger.error(f"Connect failed: {e}", exc_info=True)

            # Provide more specific error messages based on the exception
            error_msg = str(e)
            if "Could not connect to target" in error_msg or "Connecting to target failed" in error_msg:
                error_msg = "Failed to connect to target device. Check that debug probe is connected and target is powered."
            elif "No J-Link device found" in error_msg or "JLinkGDBServerCLExe not found" in error_msg:
                error_msg = "JLinkGDBServer not found. Check that J-Link is installed on box."
            elif "device" in error_msg.lower() and "not found" in error_msg.lower():
                error_msg = f"Unknown device type '{device_type}'. Check net configuration."

            self.send_error_response(500, error_msg)

    def handle_disconnect(self, data: Dict[str, Any]):
        """Handle debug disconnect command - stops JLinkGDBServer."""
        try:
            # Check if user wants to keep server running
            keep_jlink_running = data.get('keep_jlink_running', False)

            if not keep_jlink_running:
                # Stop JLinkGDBServer
                logger.info('Stopping JLinkGDBServer')
                stop_jlink_gdbserver()

            # Clear connection tracking (thread-safe)
            net = data.get('net', {})
            try:
                device_type = _resolve_device_type(net)
            except ValueError:
                device_type = 'unknown'
            connection_id = f"{net.get('name', 'default')}:{device_type}"
            with connections_lock:
                active_connections.pop(connection_id, None)

            message = 'JLinkGDBServer still running' if keep_jlink_running else 'JLinkGDBServer stopped'
            self.send_json_response(200, {
                'status': 'disconnected',
                'message': message
            })

        except Exception as e:
            logger.error(f"Disconnect failed: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def handle_reset(self, data: Dict[str, Any]):
        """Handle debug reset command."""
        try:
            halt = data.get('halt', False)

            # Check if J-Link GDB server is running
            gdbserver_status = get_jlink_gdbserver_status()
            if not gdbserver_status['running']:
                self.send_error_response(400, 'No debugger connection found')
                return

            # Get the cmdline from the running gdbserver process
            # This is needed to construct the JLink object with correct device/speed args
            pid = gdbserver_status.get('pid')
            if not pid:
                self.send_error_response(400, 'No debugger connection found')
                return

            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = [part.decode() for part in f.read().split(b'\x00')]
            except (OSError, IOError) as e:
                logger.error(f"Failed to read process cmdline: {e}")
                self.send_error_response(400, 'No debugger connection found')
                return

            # Use J-Link Commander approach (same as Python API)
            # This avoids the device type resolution issue with the GDB-based approach
            try:
                jlink = JLink(cmdline, script_file=_get_script_file(data.get('net') or {}))
            except (ValueError, KeyError) as e:
                logger.error(f"Failed to create JLink from cmdline: {e}")
                raise JLinkNotRunning()

            reset_output = list(jlink.reset(halt))
            logger.info(f"[RESET] J-Link reset complete, halt={halt}")

            self.send_json_response(200, {
                'status': 'reset_complete',
                'halt': halt,
                'output': reset_output,
            })

        except JLinkNotRunning:
            self.send_error_response(400, 'No debugger connection found')
        except Exception as e:
            logger.error(f"Reset failed: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def handle_flash(self, data: Dict[str, Any]):
        """Handle debug flash command."""
        try:
            import base64
            import tempfile
            import os

            # HEALTH CHECK: Verify GDB server is running before flashing
            gdbserver_status = get_jlink_gdbserver_status()
            if not gdbserver_status['running']:
                self.send_error_response(400, 'No debugger connection found. Connect first with: lager debug <net> connect')
                return

            # HEALTH CHECK: Verify target is still responsive via GDB
            # Note: Some debug probes (e.g., J-Link) don't return 'console' type responses
            # for 'monitor version', so we make this check informational only.
            # The J-Link status check above is the primary health indicator.
            try:
                net = data.get('net', {})
                device_type = _resolve_device_type(net)
                from lager.debug.gdb import get_controller
                gdbmi = get_controller(device=device_type)

                # Test connection with a simple monitor command (fast, non-intrusive)
                # This is primarily to ensure the GDB controller is still alive
                test_responses = gdbmi.write('monitor version', timeout_sec=2.0, raise_error_on_timeout=False)
                connection_ok = False
                for resp in test_responses:
                    # Accept any non-error response type as success
                    # J-Link may not return 'console' but will return other response types
                    if resp.get('type') in ('console', 'result', 'done'):
                        connection_ok = True
                        break

                if not connection_ok:
                    # Log warning but don't fail - J-Link is running and that's sufficient
                    logger.warning('GDB health check did not return expected response, but continuing since J-Link is running')

            except Exception as e:
                # Log warning but don't fail - if J-Link is running (checked above), flash should work
                logger.warning(f"GDB health check failed: {e}, but continuing since J-Link is running")

            hexfile = data.get('hexfile')
            elffile = data.get('elffile')
            binfile = data.get('binfile')
            verbose = data.get('verbose', False)

            temp_files = []
            hexfiles = []
            binfiles = []
            elffiles = []

            try:
                if hexfile:
                    # Decode and write hex file
                    content = base64.b64decode(hexfile['content'])
                    with tempfile.NamedTemporaryFile(mode='wb', suffix='.hex', delete=False) as f:
                        f.write(content)
                        temp_files.append(f.name)
                        hexfiles.append(f.name)

                elif elffile:
                    # Decode and write elf file
                    content = base64.b64decode(elffile['content'])
                    with tempfile.NamedTemporaryFile(mode='wb', suffix='.elf', delete=False) as f:
                        f.write(content)
                        temp_files.append(f.name)
                        elffiles.append(f.name)

                elif binfile:
                    # Decode and write bin file
                    content = base64.b64decode(binfile['content'])
                    address = binfile.get('address', 0x08000000)
                    with tempfile.NamedTemporaryFile(mode='wb', suffix='.bin', delete=False) as f:
                        f.write(content)
                        temp_files.append(f.name)
                        binfiles.append((f.name, address))

                else:
                    raise ValueError("No firmware file provided")

                # Ensure J-Link script temp file exists for flash operation
                script_path = _get_script_file(net)

                # Call flash_device with correct parameters
                # files parameter is a tuple: (hexfiles, binfiles, elffiles)
                # use_gdb: True for fast mode (1.6s), False for verbose mode (2-3s)
                # Collect all output from the flash_device generator
                flash_output = []
                files = (hexfiles, binfiles, elffiles)
                for output in flash_device(
                    files, run_after=True, mcu=device_type, use_gdb=(not verbose), script_file=script_path
                ):
                    logger.info(f"[FLASH] {output}")  # Log flash progress
                    flash_output.append(output)  # Collect for client

                self.send_json_response(200, {
                    'status': 'flash_complete',
                    'output': flash_output,  # Include verbose output
                })

            finally:
                # Clean up temp files
                for temp_file in temp_files:
                    try:
                        os.unlink(temp_file)
                    except OSError:
                        pass

        except Exception as e:
            logger.error(f"Flash failed: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def handle_erase(self, data: Dict[str, Any]):
        """Handle debug erase command.

        chip_erase() stops J-Link processes so JLinkExe can use the probe; the CLI
        reconnects afterwards. Clear active_connections since the GDB server is gone.
        """
        try:
            net = data.get('net', {})
            device_type = _resolve_device_type(net)
            speed = data.get('speed', '4000')
            transport = data.get('transport', 'SWD')

            script_path = _get_script_file(net)

            # chip_erase() returns a generator - must consume it to execute
            # NOTE: chip_erase() stops running J-Link / JLinkGDBServer so JLinkExe
            # has exclusive USB access.
            erase_output = list(chip_erase(
                device=device_type,
                speed=speed,
                transport=transport,
                mcu=None,
                script_file=script_path,
            ))

            with connections_lock:
                active_connections.clear()

            self.send_json_response(200, {
                'status': 'erase_complete',
                'output': '\n'.join(erase_output) if erase_output else 'Erase completed'
            })

        except Exception as e:
            logger.error(f"Erase failed: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def handle_memrd(self, data: Dict[str, Any]):
        """Handle memory read command using J-Link Commander.

        Uses J-Link Commander directly instead of GDB MI to avoid the
        "Truncated register" errors that occur with some Cortex-M33 devices
        (like nRF5340) due to XML target description parsing issues.
        """
        try:
            start_addr = data.get('start_addr')
            length = data.get('length', 256)

            if start_addr is None:
                self.send_error_response(400, 'Missing start_addr parameter')
                return

            # Check if GDB server is running
            gdbserver_status = get_jlink_gdbserver_status()
            if not gdbserver_status['running']:
                self.send_error_response(400, 'No debugger connection found')
                return

            # Get the cmdline from the running gdbserver process
            pid = gdbserver_status.get('pid')
            if not pid:
                self.send_error_response(400, 'No debugger connection found')
                return

            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = [part.decode() for part in f.read().split(b'\x00')]
            except (OSError, IOError) as e:
                logger.error(f"Failed to read process cmdline: {e}")
                self.send_error_response(400, 'Cannot determine J-Link configuration')
                return

            # Create JLink instance from the running GDB server's configuration
            try:
                jlink = JLink(cmdline, script_file=_get_script_file(data.get('net') or {}))
            except (ValueError, KeyError) as e:
                logger.error(f"Failed to create JLink from cmdline: {e}")
                self.send_error_response(500, f"Failed to initialize J-Link: {e}")
                return

            # Read memory using J-Link Commander (bypasses GDB entirely)
            memory_data = jlink.read_memory(start_addr, length)

            if not memory_data:
                self.send_error_response(500, "Memory read returned no data")
                return

            # Convert bytes to hex string
            memory_hex = memory_data.hex()

            self.send_json_response(200, {
                'status': 'read_complete',
                'address': hex(start_addr),
                'length': length,
                'data': memory_hex,
            })

        except Exception as e:
            logger.error(f"Memory read failed: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def handle_info(self, data: Dict[str, Any]):
        """Handle info command."""
        try:
            net = data.get('net', {})
            device_type = _resolve_device_type(net)

            # Try to get architecture
            try:
                arch = get_arch(device_type)
            except Exception:
                arch = "Unknown"

            # Get current debugger status via GDB server
            gdbserver_status = get_jlink_gdbserver_status()

            self.send_json_response(200, {
                'net_name': net.get('name', 'unknown'),
                'device': device_type,
                'arch': arch,
                'probe': net.get('instrument', ''),
                'connected': gdbserver_status['running'],
            })

        except Exception as e:
            logger.error(f"Info failed: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def handle_debug_status(self, data: Dict[str, Any]):
        """Handle status command."""
        try:
            gdbserver_status = get_jlink_gdbserver_status()

            self.send_json_response(200, {
                'connected': gdbserver_status['running'],
                'pid': gdbserver_status.get('pid') if gdbserver_status['running'] else None,
            })

        except Exception as e:
            logger.error(f"Status failed: {e}", exc_info=True)
            self.send_error_response(500, str(e))

    def handle_rtt(self, data: Dict[str, Any]):
        """Handle RTT streaming command."""
        import socket
        import select

        try:
            channel = data.get('channel', 0)
            timeout_seconds = data.get('timeout', None)  # None = stream until connection closes

            # Check if GDB server is running
            gdbserver_status = get_jlink_gdbserver_status()
            if not gdbserver_status['running']:
                self.send_error_response(400, 'No debugger connection found')
                return

            # AUTO-DETECT RTT CONTROL BLOCK
            # This is the right time to detect RTT because:
            # 1. Firmware has already booted (after reset + 3.5s delay in CLI)
            # 2. We're about to connect to RTT, so J-Link needs to know the address
            # 3. Detection at connect time was too early (firmware not yet booted)
            from .api import detect_and_configure_rtt
            net = data.get('net', {})
            device_type = _resolve_device_type(net)

            rtt_kwargs = {'device_type': device_type}
            if 'search_addr' in data:
                rtt_kwargs['search_addr'] = data['search_addr']
            if 'search_size' in data:
                rtt_kwargs['search_size'] = data['search_size']
            if 'chunk_size' in data:
                rtt_kwargs['chunk_size'] = data['chunk_size']
            rtt_result = detect_and_configure_rtt(**rtt_kwargs)
            if rtt_result['found']:
                logger.info(f"RTT auto-detection successful: {rtt_result['address']}")
            elif rtt_result['error']:
                logger.warning(f"RTT auto-detection failed (continuing anyway): {rtt_result['error']}")

            # Determine RTT port based on channel (9090 for channel 0, 9091 for channel 1)
            rtt_port = 9090 + channel

            # Connect to J-Link RTT telnet server with retry logic
            # This is needed because after a device reset, the RTT telnet port may not be
            # immediately available as J-Link needs to re-scan for the RTT control block
            import time

            # For channel 0, retry multiple times (RTT may be initializing)
            # For channel != 0, only try once (channel likely doesn't exist)
            max_retries = 5 if channel == 0 else 1
            retry_delay = 0.5
            rtt_socket = None

            for attempt in range(max_retries):
                try:
                    # Try IPv6 first (::1), then fall back to IPv4 (127.0.0.1)
                    # J-Link may bind to either depending on system configuration
                    rtt_socket = None
                    last_error = None

                    for family, addr in [(socket.AF_INET6, '::1'), (socket.AF_INET, '127.0.0.1')]:
                        try:
                            rtt_socket = socket.socket(family, socket.SOCK_STREAM)
                            rtt_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                            rtt_socket.settimeout(2.0)
                            rtt_socket.connect((addr, rtt_port))
                            logger.info(f"RTT connection established on attempt {attempt + 1} using {addr}:{rtt_port}")
                            break  # Connection successful
                        except (ConnectionRefusedError, socket.timeout, OSError) as e:
                            last_error = e
                            if rtt_socket:
                                try:
                                    rtt_socket.close()
                                except OSError:
                                    pass
                                rtt_socket = None

                    if rtt_socket:
                        break  # Successfully connected
                    else:
                        raise last_error  # Raise the last error to trigger retry logic
                except (ConnectionRefusedError, socket.timeout, OSError) as conn_err:
                    if rtt_socket:
                        try:
                            rtt_socket.close()
                        except OSError:
                            pass
                        rtt_socket = None

                    if attempt < max_retries - 1:
                        logger.warning(f"RTT connection attempt {attempt + 1}/{max_retries} failed: {conn_err}. Retrying in {retry_delay:.1f}s...")
                        time.sleep(retry_delay)
                        retry_delay *= 1.5  # Exponential backoff
                    else:
                        logger.error(f"RTT connection failed after {max_retries} attempts: {conn_err}")
                        # Provide different error messages for channel 0 vs other channels
                        if channel == 0:
                            error_msg = f'Cannot connect to RTT telnet port (localhost:{rtt_port}) after {max_retries} attempts. Device may not have RTT initialized yet.'
                        else:
                            error_msg = f'RTT channel {channel} is not available. The firmware may not have this channel configured. Try channel 0.'
                        self.send_error_response(500, error_msg)
                        return

            if not rtt_socket:
                self.send_error_response(500, 'Failed to establish RTT connection')
                return

            # Set non-blocking for streaming
            rtt_socket.setblocking(False)

            # Send chunked transfer encoding response headers
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()

            # Stream RTT data using chunked encoding
            import time
            start_time = time.time()
            bytes_streamed = 0
            banner_skipped = False  # Track if we've skipped the J-Link telnet banner

            try:
                while True:
                    # Check timeout
                    if timeout_seconds and (time.time() - start_time >= timeout_seconds):
                        break

                    # Use select to wait for data with timeout
                    ready = select.select([rtt_socket], [], [], 0.1)
                    if ready[0]:
                        try:
                            data_chunk = rtt_socket.recv(4096)
                            if not data_chunk:
                                # Connection closed by remote (J-Link)
                                # This can happen if:
                                # 1. J-Link detected no RTT control block in RAM
                                # 2. Device was reset and RTT needs reinitialization
                                # 3. J-Link server is shutting down
                                logger.warning("RTT telnet connection closed by J-Link (no data received)")
                                break

                            # Skip J-Link telnet banner on first chunk
                            # Banner format: "SEGGER J-Link V7.94a - Real time terminal output\r\n..."
                            # We need to skip everything up to and including the banner lines
                            if not banner_skipped:
                                # Look for the end of the banner (typically ends with "Process: JLinkGDBServerCLExe\r\n")
                                # Skip any data that starts with "SEGGER" or contains telnet banner text
                                if data_chunk.startswith(b'SEGGER') or b'terminal output' in data_chunk:
                                    # Find the last newline in the banner section
                                    # Banner typically has 3 lines ending with \r\n
                                    lines = data_chunk.split(b'\r\n')
                                    if len(lines) >= 3:
                                        # Skip first 3 lines (banner), keep the rest
                                        data_chunk = b'\r\n'.join(lines[3:])
                                banner_skipped = True
                                # If the entire chunk was banner, skip it
                                if len(data_chunk) == 0 or data_chunk == b'\r\n':
                                    continue

                            # Send chunk in HTTP chunked transfer encoding format
                            # Format: size in hex + CRLF + data + CRLF
                            chunk_size = hex(len(data_chunk))[2:].encode('ascii')
                            self.wfile.write(chunk_size + b'\r\n')
                            self.wfile.write(data_chunk)
                            self.wfile.write(b'\r\n')
                            self.wfile.flush()

                            bytes_streamed += len(data_chunk)

                        except socket.error as e:
                            logger.error(f"RTT socket error: {e}")
                            break
                    else:
                        # No data available, continue waiting
                        pass

            except (BrokenPipeError, ConnectionResetError) as e:
                # Client disconnected (Ctrl+C)
                logger.info(f"RTT client disconnected: {e}")
            finally:
                # Send final zero-length chunk to end chunked encoding
                try:
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
                except OSError:
                    pass

                # Properly close and cleanup the RTT telnet socket
                # Use shutdown() before close() to ensure graceful TCP teardown
                try:
                    rtt_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # Socket might already be closed

                try:
                    rtt_socket.close()
                except OSError:
                    pass

                logger.info(f"RTT stream ended. Bytes streamed: {bytes_streamed}")

                # Small delay to ensure telnet connection is fully closed on J-Link side
                # This prevents "connection already active" errors on immediate reconnect
                time.sleep(1.0)

        except Exception as e:
            logger.error(f"RTT streaming failed: {e}", exc_info=True)
            # Can't send error response after headers are sent
            pass


class DebugService:
    """Debug service manager."""

    def __init__(self, host=SERVICE_HOST, port=SERVICE_PORT):
        self.host = host
        self.port = port
        self.server = None
        self.running = False

    def start(self):
        """Start the debug service."""
        global start_time
        start_time = time.time()

        logger.info(f"Starting Lager Debug Service v{SERVICE_VERSION}")
        logger.info(f"Listening on {self.host}:{self.port} (accessible via port forwarding)")

        try:
            self.server = ThreadingHTTPServer((self.host, self.port), DebugServiceHandler)
            self.running = True

            # Register signal handlers
            signal.signal(signal.SIGTERM, self._handle_signal)
            signal.signal(signal.SIGINT, self._handle_signal)

            logger.info("Service started successfully")

            # Serve forever
            self.server.serve_forever()

        except Exception as e:
            logger.error(f"Failed to start service: {e}", exc_info=True)
            sys.exit(1)

    def stop(self):
        """Stop the debug service."""
        logger.info("Stopping Lager Debug Service")
        self.running = False

        if self.server:
            self.server.shutdown()
            self.server.server_close()

        logger.info("Service stopped")

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down")
        self.stop()
        sys.exit(0)


def main():
    """Main entry point."""
    service = DebugService()
    service.start()


if __name__ == '__main__':
    main()

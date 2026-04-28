# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
WebSocket client for battery monitoring and control.

This module provides a WebSocket-based client for real-time battery monitoring
and command execution with the box Python container.
"""
import socketio
import threading
from typing import Optional, Callable, Dict, Any


class BatteryWebSocketClient:
    """WebSocket client for battery monitoring sessions."""

    def __init__(self, box_url: str, netname: str, update_interval: float = 1.0):
        """
        Initialize Battery WebSocket client.

        Args:
            box_url: Box WebSocket URL (e.g., http://box-ip:9000)
            netname: Name of the battery net
            update_interval: Update interval in seconds (default: 1.0)
        """
        self.box_url = box_url
        self.netname = netname
        self.update_interval = update_interval
        self.connected = False
        self.monitoring_active = False
        self.driver_ready = False
        self.stop_event = threading.Event()

        # Callbacks
        self.on_state_update: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_command_response: Optional[Callable[[Dict[str, Any]], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_driver_ready: Optional[Callable[[Dict[str, Any]], None]] = None

        # Command response tracking
        self._command_response_event = threading.Event()
        self._command_response_data = None
        self._command_lock = threading.Lock()

        # Create SocketIO client
        self.sio = socketio.Client(
            logger=False,
            engineio_logger=False,
            reconnection=True,
            reconnection_attempts=5,
            reconnection_delay=1,
            reconnection_delay_max=5
        )

        # Register event handlers
        self.sio.on('connect', self._on_connect, namespace='/battery')
        self.sio.on('disconnect', self._on_disconnect, namespace='/battery')
        self.sio.on('connected', self._on_connected_event, namespace='/battery')
        self.sio.on('battery_monitor_started', self._on_monitor_started, namespace='/battery')
        self.sio.on('battery_driver_ready', self._on_driver_ready, namespace='/battery')
        self.sio.on('battery_state_update', self._on_state_update, namespace='/battery')
        self.sio.on('battery_command_response', self._on_command_response, namespace='/battery')
        self.sio.on('battery_monitor_stopped', self._on_monitor_stopped, namespace='/battery')
        self.sio.on('error', self._on_error_event, namespace='/battery')

    def _on_connect(self):
        """Handle WebSocket connection."""
        self.connected = True

    def _on_disconnect(self):
        """Handle WebSocket disconnection."""
        self.connected = False
        self.monitoring_active = False
        self.stop_event.set()
        if self.on_disconnected:
            self.on_disconnected()

    def _on_connected_event(self, data):
        """Handle initial connection acknowledgment."""
        if self.on_connected:
            self.on_connected()

    def _on_monitor_started(self, data):
        """Handle monitoring start confirmation."""
        self.monitoring_active = True

    def _on_driver_ready(self, data):
        """Handle battery driver ready confirmation - driver is now ready for commands."""
        self.driver_ready = True
        if self.on_driver_ready:
            self.on_driver_ready(data)

    def _on_state_update(self, data):
        """Handle battery state update."""
        if self.on_state_update:
            state = data.get('state', {})
            self.on_state_update(state)

    def _on_command_response(self, data):
        """Handle command response."""
        # Store response for synchronous command execution
        with self._command_lock:
            self._command_response_data = data
            self._command_response_event.set()

        # Also call callback if registered
        if self.on_command_response:
            self.on_command_response(data)

    def _on_monitor_stopped(self, data):
        """Handle monitoring stop confirmation."""
        self.monitoring_active = False
        self.stop_event.set()

    def _on_error_event(self, data):
        """Handle error messages."""
        message = data.get('message', 'Unknown error')
        if self.on_error:
            self.on_error(message)
        self.stop_event.set()

    def connect(self, timeout: float = 10.0) -> bool:
        """
        Connect to WebSocket server.

        Args:
            timeout: Connection timeout in seconds

        Returns:
            True if connected successfully, False otherwise
        """
        try:
            self.sio.connect(
                self.box_url,
                namespaces=['/battery'],
                wait_timeout=timeout
            )
            return True
        except Exception as e:
            if self.on_error:
                self.on_error(f"Could not connect to box at {self.box_url}: {str(e)}")
            return False

    def start_monitoring(self) -> bool:
        """
        Start battery monitoring session.

        Returns:
            True if monitoring started successfully, False otherwise
        """
        if not self.connected:
            if self.on_error:
                self.on_error("Not connected to box")
            return False

        try:
            # Reset driver_ready flag before starting
            self.driver_ready = False

            self.sio.emit('start_battery_monitor', {
                'netname': self.netname,
                'interval': self.update_interval
            }, namespace='/battery')

            # Wait for driver to be ready (not just monitoring started). Driver
            # initialization takes time for the first SCPI query through
            # hardware_service. Mirrors supply's wait pattern.
            timeout = 15.0
            while not self.driver_ready and not self.stop_event.is_set() and timeout > 0:
                self.sio.sleep(0.1)
                timeout -= 0.1

            if not self.driver_ready and not self.stop_event.is_set():
                if self.on_error:
                    self.on_error("Timeout waiting for battery driver to initialize")
                return False

            return self.driver_ready
        except Exception as e:
            if self.on_error:
                self.on_error(f"Failed to start monitoring: {str(e)}")
            return False

    def send_command(self, action: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        """
        Send a command to the battery and wait for response.

        Args:
            action: Command action (soc, voc, enable, disable, etc.)
            params: Command parameters
            timeout: Response timeout in seconds

        Returns:
            Command response dict or None if timeout/error
        """
        if not self.connected:
            if self.on_error:
                self.on_error("Not connected to box")
            return None

        with self._command_lock:
            # Clear previous response
            self._command_response_data = None
            self._command_response_event.clear()

            # Send command
            try:
                self.sio.emit('battery_command', {
                    'netname': self.netname,
                    'action': action,
                    'params': params or {}
                }, namespace='/battery')
            except Exception as e:
                if self.on_error:
                    self.on_error(f"Failed to send command: {str(e)}")
                return None

        # Wait for response
        if self._command_response_event.wait(timeout):
            with self._command_lock:
                return self._command_response_data
        else:
            if self.on_error:
                self.on_error(f"Command timeout: {action}")
            return None

    def stop_monitoring(self):
        """Stop the monitoring session."""
        if self.connected:
            try:
                self.sio.emit('stop_battery_monitor', namespace='/battery')
                self.sio.sleep(0.5)  # Give time for stop to process
            except Exception:
                pass

    def disconnect(self):
        """Disconnect from WebSocket server."""
        self.stop_monitoring()
        if self.connected:
            try:
                self.sio.disconnect()
            except Exception:
                pass

    def wait_for_stop(self):
        """Wait for stop event (blocking)."""
        self.stop_event.wait()

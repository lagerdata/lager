# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
WebSocket client for interactive UART communication.

This module provides a WebSocket-based client for bidirectional UART communication
with the box Python container.
"""
import sys
import threading
import termios
import tty
import select
import os
from typing import Optional
import socketio
import click


class UARTWebSocketClient:
    """WebSocket client for interactive UART sessions."""

    def __init__(self, box_url: str, netname: str, overrides: dict, interactive: bool = True):
        """
        Initialize UART WebSocket client.

        Args:
            box_url: Box WebSocket URL (e.g., ws://box:9000)
            netname: Name of the UART net
            overrides: Parameter overrides (baudrate, parity, etc.)
            interactive: Enable interactive mode (bidirectional, requires TTY)
        """
        self.box_url = box_url
        self.netname = netname
        self.overrides = overrides
        self.interactive = interactive
        self.connected = False
        self.uart_active = False
        self.stop_event = threading.Event()
        self.old_tty_settings = None
        self.line_buffer = bytearray()  # Buffer for backspace support
        self.output_lock = threading.Lock()  # Lock for stdout access
        self.suppress_next_line = False  # Suppress device echo of command
        self.echo_buffer = bytearray()  # Buffer for accumulating echo line

        # Convert line_ending string to bytes
        line_ending = overrides.get('line_ending', 'lf')
        self.line_ending_bytes = {
            'lf': b'\n',
            'crlf': b'\r\n',
            'cr': b'\r'
        }.get(line_ending, b'\n')

        # Create SocketIO client
        self.sio = socketio.Client(
            logger=False,
            engineio_logger=False,
            reconnection=False
        )

        # Register event handlers
        self.sio.on('connect', self._on_connect, namespace='/uart')
        self.sio.on('disconnect', self._on_disconnect, namespace='/uart')
        self.sio.on('connected', self._on_connected, namespace='/uart')
        self.sio.on('uart_connected', self._on_uart_connected, namespace='/uart')
        self.sio.on('uart_data', self._on_uart_data, namespace='/uart')
        self.sio.on('uart_status', self._on_uart_status, namespace='/uart')
        self.sio.on('uart_stopped', self._on_uart_stopped, namespace='/uart')
        self.sio.on('error', self._on_error, namespace='/uart')

    def _on_connect(self):
        """Handle WebSocket connection."""
        self.connected = True
        # Don't print here - wait for uart_connected

    def _on_disconnect(self):
        """Handle WebSocket disconnection."""
        self.connected = False
        self.uart_active = False
        self.stop_event.set()

    def _on_connected(self, data):
        """Handle initial connection acknowledgment."""
        # Server is ready, now start UART
        pass

    def _on_uart_connected(self, data):
        """Handle UART connection success."""
        self.uart_active = True
        # Print connection message
        device_path = data.get('device_path', 'unknown')
        baudrate = data.get('baudrate', 'unknown')
        mode = 'interactive' if self.interactive else 'read-only'
        msg = f"\033[32mConnected to {device_path} at {baudrate} baud [{mode}]\033[0m\r\n"
        sys.stderr.buffer.write(msg.encode())
        sys.stderr.buffer.flush()  # Flush immediately to ensure message appears first
        msg = "\033[33mPress Ctrl+C to disconnect\033[0m\r\n\n"
        sys.stderr.buffer.write(msg.encode())
        sys.stderr.buffer.flush()  # Flush immediately
        if self.interactive:
            with self.output_lock:
                msg = ">> "
                sys.stdout.buffer.write(msg.encode())
                sys.stdout.buffer.flush()

    def _on_uart_data(self, data):
        """Handle incoming UART data."""
        try:
            hex_data = data.get('data', '')
            if hex_data:
                # Convert hex string to bytes
                bytes_data = bytes.fromhex(hex_data)

                # If suppressing echo, accumulate until we see complete line
                if self.suppress_next_line:
                    for idx, byte in enumerate(bytes_data):
                        self.echo_buffer.append(byte)
                        # Check if we've completed the echo line
                        if byte == ord('\n') or byte == ord('\r'):
                            # Echo line complete - discard it and stop suppressing
                            self.echo_buffer.clear()
                            self.suppress_next_line = False
                            # Skip all bytes up to and including this newline
                            bytes_data = bytes_data[idx + 1:]
                            break
                    # If still suppressing (haven't seen newline yet), don't output anything
                    if self.suppress_next_line:
                        return
                    # If no data left after suppressing echo, return
                    if not bytes_data:
                        return

                # Write to stdout with lock
                with self.output_lock:
                    # Write UART data
                    sys.stdout.buffer.write(bytes_data)
                    sys.stdout.buffer.flush()
        except Exception as e:
            click.secho(f"\nError processing UART data: {e}", fg='red', err=True)

    def _on_uart_status(self, data):
        """Handle box-side session status (device re-enumeration healing).

        Purely informational: the box is re-resolving/reopening the device and
        the session stays alive, so this must NOT set stop_event. Boxes
        predating the event never send it.
        """
        status = (data or {}).get('status')
        if status == 'reconnecting':
            msg = "\r\n\033[33m[UART device disconnected - reconnecting...]\033[0m\r\n"
        elif status == 'reconnected':
            device_path = (data or {}).get('device_path', 'device')
            msg = f"\033[32m[reconnected to {device_path}]\033[0m\r\n"
        else:
            return
        sys.stderr.buffer.write(msg.encode())
        sys.stderr.buffer.flush()

    def _on_uart_stopped(self, data):
        """Handle UART stop confirmation."""
        self.uart_active = False
        self.stop_event.set()

    def _on_error(self, data):
        """Handle error messages."""
        message = data.get('message', 'Unknown error')
        click.secho(f"\n\033[31mError: {message}\033[0m", err=True)
        self.stop_event.set()

    def _setup_terminal(self):
        """Set terminal to cbreak mode for character-by-character input."""
        # Only set cbreak mode for interactive sessions
        if self.interactive and sys.stdin.isatty():
            try:
                fd = sys.stdin.fileno()
                self.old_tty_settings = termios.tcgetattr(fd)
                tty.setcbreak(fd)
            except Exception:
                self.old_tty_settings = None

    def _restore_terminal(self):
        """Restore terminal to original settings."""
        if self.old_tty_settings and sys.stdin.isatty():
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old_tty_settings)
            except Exception:
                pass

    def _read_stdin_thread(self):
        """Thread to read from stdin and send to UART with local line editing."""
        fd_in = sys.stdin.fileno()

        while not self.stop_event.is_set() and self.uart_active:
            try:
                # Check if stdin has data
                readable, _, _ = select.select([fd_in], [], [], 0.1)
                if not readable:
                    continue

                # Read character(s)
                data = os.read(fd_in, 1024)
                if not data:
                    break

                # Process each character
                for byte in data:
                    # Ctrl+C (0x03) - exit
                    if byte == 0x03:
                        self.stop_event.set()
                        return

                    # Backspace/Delete (0x7F or 0x08)
                    elif byte == 0x7F or byte == 0x08:
                        if len(self.line_buffer) > 0:
                            # Remove last character from buffer
                            self.line_buffer.pop()
                            # Display backspace visually (move back, erase, move back)
                            with self.output_lock:
                                sys.stdout.buffer.write(b'\b \b')
                                sys.stdout.buffer.flush()
                            # Don't send to device - handle locally only

                    # Enter (0x0D or 0x0A)
                    elif byte == 0x0D or byte == 0x0A:
                        # Send buffered line + configured line ending to UART
                        if self.line_buffer:
                            line_to_send = self.line_buffer + self.line_ending_bytes
                            hex_data = line_to_send.hex()
                            self.sio.emit('uart_write', {'data': hex_data}, namespace='/uart')
                            self.line_buffer.clear()
                            # Suppress device echo of this command
                            self.suppress_next_line = True
                            self.echo_buffer.clear()
                        else:
                            # Empty line, just send configured line ending
                            hex_data = self.line_ending_bytes.hex()
                            self.sio.emit('uart_write', {'data': hex_data}, namespace='/uart')
                        # Display newline locally
                        with self.output_lock:
                            sys.stdout.buffer.write(b'\n')
                            sys.stdout.buffer.flush()

                    # Regular printable character
                    else:
                        # Add to buffer and echo locally
                        self.line_buffer.append(byte)
                        with self.output_lock:
                            sys.stdout.buffer.write(bytes([byte]))
                            sys.stdout.buffer.flush()

            except Exception as e:
                click.secho(f"\nError reading stdin: {e}", fg='red', err=True)
                break

    def connect_and_run(self):
        """Connect to WebSocket and run interactive UART session."""
        try:
            # Set terminal to cbreak mode
            self._setup_terminal()

            # Connect to WebSocket
            try:
                from ...gateway_auth import auth_headers_for_url
                self.sio.connect(
                    self.box_url,
                    namespaces=['/uart'],
                    wait_timeout=10,
                    headers=auth_headers_for_url(self.box_url)
                )
            except Exception as e:
                # Preserve the return-code contract (don't sys.exit / .die here):
                # the caller relies on the return value and the finally-block
                # terminal restore below.
                from ...errors import connection_error
                from urllib.parse import urlparse
                host = urlparse(self.box_url).hostname or self.box_url
                connection_error(e, host=host).show()
                return 1

            # Start UART session
            self.sio.emit('start_uart', {
                'netname': self.netname,
                'overrides': self.overrides
            }, namespace='/uart')

            # Wait for UART to become active
            timeout = 5
            while not self.uart_active and not self.stop_event.is_set() and timeout > 0:
                self.sio.sleep(0.1)
                timeout -= 0.1

            if not self.uart_active:
                click.secho("Error: UART session failed to start", fg='red', err=True)
                return 1

            # Start stdin reading thread only for interactive mode
            if self.interactive:
                stdin_thread = threading.Thread(target=self._read_stdin_thread, daemon=True)
                stdin_thread.start()

            # Wait for stop event
            while not self.stop_event.is_set():
                self.sio.sleep(0.1)

            # Clean shutdown
            if self.uart_active:
                self.sio.emit('stop_uart', namespace='/uart')
                self.sio.sleep(0.5)  # Give time for stop to process

            return 0

        except KeyboardInterrupt:
            click.secho('\n\033[31mDisconnected\033[0m', err=True)
            return 0
        except Exception as e:
            click.secho(f"\nError: {str(e)}", fg='red', err=True)
            return 1
        finally:
            # Restore terminal
            self._restore_terminal()

            # Disconnect WebSocket
            if self.connected:
                try:
                    self.sio.disconnect()
                except Exception:
                    pass


def connect_uart_interactive(box_url: str, netname: str, overrides: dict) -> int:
    """
    Connect to interactive UART session via WebSocket.

    Args:
        box_url: Box WebSocket URL (e.g., ws://box:9000)
        netname: Name of the UART net
        overrides: Parameter overrides

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    client = UARTWebSocketClient(box_url, netname, overrides, interactive=True)
    return client.connect_and_run()


def connect_uart_readonly(box_url: str, netname: str, overrides: dict) -> int:
    """
    Connect to read-only UART session via WebSocket.

    Args:
        box_url: Box WebSocket URL (e.g., ws://box:9000)
        netname: Name of the UART net
        overrides: Parameter overrides

    Returns:
        Exit code (0 for success, non-zero for error)
    """
    client = UARTWebSocketClient(box_url, netname, overrides, interactive=False)
    return client.connect_and_run()

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
UART Bridge hardware driver.
Communicates with UART bridges by serial number and port.
"""
from __future__ import annotations

import sys
import select
import termios
import signal
import atexit
import time
import os
import threading
import collections
from pathlib import Path
from typing import Optional

import serial


class UARTBridge:
    """Driver for UART bridge devices."""

    def __init__(
        self,
        bridge_serial: str,
        port: str,
        device_path: str | None = None,
        baudrate: int = 115200,
        bytesize: int = 8,
        parity: str = "none",
        stopbits: str = "1",
        xonxoff: bool = False,
        rtscts: bool = False,
        dsrdtr: bool = False,
        opost: bool = False,
        line_ending: str = 'lf',
        **kwargs
    ):
        """
        Initialize UART bridge driver.

        Args:
            bridge_serial: USB serial number of the UART bridge
            port: Port number on the bridge (for multi-port bridges)
            baudrate: Baud rate (default 115200)
            bytesize: Number of data bits (default 8)
            parity: Parity checking (none/even/odd/mark/space)
        stopbits: Number of stop bits (1/1.5/2)
        xonxoff: Enable software flow control
        rtscts: Enable RTS/CTS hardware flow control
        dsrdtr: Enable DSR/DTR hardware flow control
        opost: Enable output post-processing (convert \n to \r\n)
        line_ending: Line ending for commands (lf/crlf/cr)
        device_path: Optional direct /dev/tty* path for adapters without USB serial numbers
        """
        self.bridge_serial = bridge_serial
        self.port = port
        self.device_path_override = device_path
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.xonxoff = xonxoff
        self.rtscts = rtscts
        self.dsrdtr = dsrdtr
        self.opost = opost
        self.line_ending = line_ending

        # Convert line_ending string to bytes
        self.line_ending_bytes = {
            'lf': b'\n',
            'crlf': b'\r\n',
            'cr': b'\r'
        }.get(line_ending, b'\n')

        # Find device path
        if self.device_path_override:
            self.device_path = self.device_path_override
        else:
            # If the "serial" looks like a direct device path, treat it as such.
            if bridge_serial and isinstance(bridge_serial, str) and bridge_serial.startswith("/dev/"):
                self.device_path = bridge_serial
                self.bridge_serial = ""
            else:
                self.device_path = self._find_device_by_serial(bridge_serial)

        if not self.device_path:
            raise FileNotFoundError(
                f"UART bridge "
                f"{'with serial ' + bridge_serial if bridge_serial else 'by device path'} not found. "
                f"Check that the device is connected."
            )

        self.serial_conn = None

    def _find_device_by_serial(self, serial_number: str) -> Optional[str]:
        """
        Find the tty device path for a USB serial adapter by USB serial number.
        Cross-platform: lager.usb_enum dispatches to sysfs on Linux and to
        pyserial.tools.list_ports on macOS.
        """
        try:
            from ...usb_enum import get_tty_for_usb_serial
        except ImportError:
            return None
        return get_tty_for_usb_serial(serial_number)

    def _connect(self):
        """Open the serial connection."""
        # Map parity string to pyserial constant
        parity_map = {
            "none": serial.PARITY_NONE,
            "even": serial.PARITY_EVEN,
            "odd": serial.PARITY_ODD,
            "mark": serial.PARITY_MARK,
            "space": serial.PARITY_SPACE,
        }
        parity_val = parity_map.get(self.parity.lower(), serial.PARITY_NONE)

        # Map stopbits string to pyserial constant
        stopbits_val = serial.STOPBITS_ONE
        stopbits_float = float(self.stopbits)
        if stopbits_float == 1.5:
            stopbits_val = serial.STOPBITS_ONE_POINT_FIVE
        elif stopbits_float == 2:
            stopbits_val = serial.STOPBITS_TWO

        # Kill any existing processes using this port
        try:
            import subprocess
            subprocess.run(
                ["fuser", "-k", self.device_path],
                capture_output=True,
                timeout=1
            )
            time.sleep(0.2)
        except Exception:
            pass

        # Open serial connection
        self.serial_conn = serial.Serial(
            port=self.device_path,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=parity_val,
            stopbits=stopbits_val,
            xonxoff=self.xonxoff,
            rtscts=self.rtscts,
            dsrdtr=self.dsrdtr,
            timeout=0.1,
            exclusive=True
        )

        self.serial_conn.reset_input_buffer()
        self.serial_conn.reset_output_buffer()

    def _cleanup(self):
        """Close serial connection."""
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception:
                pass

    def monitor(self):
        """
        Monitor UART output (read-only mode).
        Continuously reads from the serial port and prints to stdout.
        """
        self._connect()

        # Configure terminal settings for opost
        # Disable ONLCR (output newline to CR+NL conversion) when opost is False
        if not self.opost and sys.stdout.isatty():
            try:
                fd = sys.stdout.fileno()
                attrs = termios.tcgetattr(fd)
                # Disable ONLCR flag (bit in oflag that converts \n to \r\n)
                attrs[1] = attrs[1] & ~termios.ONLCR  # attrs[1] is oflag
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except Exception:
                pass

        # Setup signal handlers
        def signal_handler(signum, frame):
            self._cleanup()
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        atexit.register(self._cleanup)

        msg = f"\033[32mConnected to {self.device_path} at {self.baudrate} baud [read-only]\033[0m\r\n"
        sys.stderr.buffer.write(msg.encode())
        msg = "\033[33mPress Ctrl+C to exit\033[0m\r\n\n"
        sys.stderr.buffer.write(msg.encode())
        sys.stderr.buffer.flush()

        try:
            while True:
                data = self.serial_conn.read(self.serial_conn.in_waiting or 1)
                if data:
                    # Apply output post-processing if enabled
                    if self.opost:
                        data = data.replace(b'\n', b'\r\n')
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()
            sys.stderr.buffer.write(b"\r\n\033[31mDisconnected\033[0m\r\n")
            sys.stderr.buffer.flush()

    def monitor_interactive(self):
        """
        Monitor UART with bidirectional communication.
        Allows both reading from and writing to the serial port.
        """
        self._connect()

        # Configure terminal settings for opost
        # Disable ONLCR (output newline to CR+NL conversion) when opost is False
        if not self.opost and sys.stdout.isatty():
            try:
                fd = sys.stdout.fileno()
                attrs = termios.tcgetattr(fd)
                # Disable ONLCR flag (bit in oflag that converts \n to \r\n)
                attrs[1] = attrs[1] & ~termios.ONLCR  # attrs[1] is oflag
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except Exception:
                pass

        # Put stdin into non-canonical, no-echo mode (cbreak-ish) so we get bytes immediately.
        old_stdin_attrs = None
        if sys.stdin.isatty():
            try:
                fd = sys.stdin.fileno()
                old_stdin_attrs = termios.tcgetattr(fd)
                attrs = termios.tcgetattr(fd)

                lflag = attrs[3]
                # Disable canonical input processing and echo
                lflag &= ~(termios.ICANON | termios.ECHO)
                attrs[3] = lflag
                # Make reads return as soon as 1 byte is available
                attrs[6][termios.VMIN] = 1
                attrs[6][termios.VTIME] = 0
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
            except Exception:
                old_stdin_attrs = None

        def restore_stdin():
            if old_stdin_attrs and sys.stdin.isatty():
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_stdin_attrs)
                except Exception:
                    pass

        # Ensure we restore stdin on exit
        atexit.register(restore_stdin)

        # Setup signal handlers
        stop_event = threading.Event()

        def signal_handler(signum, frame):
            stop_event.set()

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        atexit.register(self._cleanup)

        msg = f"\033[32mConnected to {self.device_path} at {self.baudrate} baud [interactive]\033[0m\r\n"
        sys.stderr.buffer.write(msg.encode())
        msg = "\033[33mPress Ctrl+C to exit\033[0m\r\n\n"
        sys.stderr.buffer.write(msg.encode())
        sys.stderr.buffer.flush()

        # Shared state for both threads
        current_line = bytearray()
        current_line_lock = threading.Lock()
        last_char_was_newline = False

        # Queue of bytes we sent to device; used to suppress device echo.
        pending_echo: collections.deque[int] = collections.deque()

        # Thread to read from serial and write to stdout (with echo suppression)
        def serial_to_stdout():
            nonlocal last_char_was_newline

            while not stop_event.is_set():
                try:
                    data = self.serial_conn.read(self.serial_conn.in_waiting or 1)
                    if not data:
                        # No data available - if we're waiting after a newline, show prompt
                        with current_line_lock:
                            if last_char_was_newline and len(current_line) == 0:
                                sys.stdout.buffer.write(b"\n>> ")
                                sys.stdout.buffer.flush()
                                last_char_was_newline = False
                        continue

                    out = bytearray()
                    for b in data:
                        if pending_echo and pending_echo[0] == b:
                            pending_echo.popleft()  # suppress echoed byte from device
                        else:
                            out.append(b)

                    if out:
                        # Apply output post-processing if enabled
                        if self.opost:
                            out = out.replace(b'\n', b'\r\n')
                        sys.stdout.buffer.write(out)
                        sys.stdout.buffer.flush()

                        if out[-1:] in (b'\n', b'\r'):
                            last_char_was_newline = True
                        else:
                            last_char_was_newline = False

                except Exception:
                    break

        # Thread to read from stdin and write to serial (explicit local echo)
        def stdin_to_serial():
            nonlocal current_line

            def _erase_one_char_on_stdout():
                # Move cursor back, overwrite with space, move back again
                sys.stdout.buffer.write(b'\b \b')
                sys.stdout.buffer.flush()

            fd_in = sys.stdin.fileno()

            while not stop_event.is_set():
                try:
                    readable, _, _ = select.select([fd_in], [], [], 0.1)
                    if not readable:
                        continue

                    # Read whatever is available immediately (non-canonical)
                    data = os.read(fd_in, 1024)
                    if not data:
                        break

                    # Ctrl+C (0x03) ends session
                    if b'\x03' in data:
                        stop_event.set()
                        return

                    for byte_val in data:
                        byte = bytes([byte_val])

                        # Enter pressed (CR or LF)
                        if byte_val in (0x0d, 0x0a):
                            # Local echo of newline before device responds
                            sys.stdout.buffer.write(b'\r\n')
                            sys.stdout.buffer.flush()

                            # Send configured line ending to the device
                            self.serial_conn.write(self.line_ending_bytes)
                            self.serial_conn.flush()

                            # Record the line ending for echo suppression
                            for eb in self.line_ending_bytes:
                                pending_echo.append(eb)

                            # Clear our edit buffer
                            with current_line_lock:
                                current_line.clear()

                        # Backspace/Delete
                        elif byte_val in (0x08, 0x7f):
                            with current_line_lock:
                                if len(current_line) > 0:
                                    current_line.pop()
                                    # Local visual erase
                                    _erase_one_char_on_stdout()
                                    # Forward a backspace to the device (if it does its own editing)
                                    self.serial_conn.write(b'\x08')
                                    self.serial_conn.flush()
                                    pending_echo.append(0x08)

                        # Regular character
                        else:
                            with current_line_lock:
                                current_line.append(byte_val)

                            # Local echo of the typed character
                            sys.stdout.buffer.write(byte)
                            sys.stdout.buffer.flush()

                            # Send to the device immediately
                            self.serial_conn.write(byte)
                            self.serial_conn.flush()

                            # Track for echo suppression
                            pending_echo.append(byte_val)

                except Exception:
                    break

        # Show initial prompt
        sys.stdout.buffer.write(b">> ")
        sys.stdout.buffer.flush()

        # Start threads
        try:
            t1 = threading.Thread(target=serial_to_stdout, daemon=False)
            t2 = threading.Thread(target=stdin_to_serial, daemon=False)
            t1.start()
            t2.start()
            t2.join()
            stop_event.set()
            t1.join(timeout=1)
        except KeyboardInterrupt:
            stop_event.set()
        finally:
            time.sleep(0.2)
            self._cleanup()
            restore_stdin()
            sys.stderr.buffer.write(b"\r\n\033[31mDisconnected\033[0m\r\n")
            sys.stderr.buffer.flush()

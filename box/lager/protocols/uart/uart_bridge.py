# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART Bridge hardware driver.
Communicates with UART bridges by serial number and port.
"""
from __future__ import annotations

import errno
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
        usb_identity: dict | None = None,
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
        usb_identity: Optional durable USB identity snapshot ({vid, pid,
            serial, port_path, interface}) recorded when the net was saved;
            preferred over the stored path because /dev/tty* numbers do not
            survive USB re-enumeration
        """
        self.bridge_serial = bridge_serial
        self.port = port
        self.device_path_override = device_path
        self.usb_identity = usb_identity
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
        self.device_path = self._resolve_device_path_initial()

        if not self.device_path:
            raise FileNotFoundError(
                f"UART bridge "
                f"{'with serial ' + bridge_serial if bridge_serial else 'by device path'} not found. "
                f"Check that the device is connected."
            )

        self.serial_conn = None

    def _find_device_by_serial(self, serial_number: str) -> Optional[str]:
        """
        Find the /dev/tty* device path for a USB serial adapter by serial number.
        Uses sysfs to map USB device serial to tty device.
        """
        sys_tty = Path("/sys/class/tty")
        if not sys_tty.exists():
            return None

        for tty_dev in sys_tty.iterdir():
            try:
                # Skip non-USB ttys
                if not tty_dev.name.startswith(("ttyUSB", "ttyACM")):
                    continue

                device_path = tty_dev / "device"
                if not device_path.exists():
                    continue

                # Resolve symlink to get real device path, then navigate up to find USB device with serial number
                usb_device = device_path.resolve()
                for _ in range(10):  # Search up to 10 levels
                    serial_path = usb_device / "serial"
                    if serial_path.exists():
                        dev_serial = serial_path.read_text().strip()
                        if dev_serial == serial_number:
                            return f"/dev/{tty_dev.name}"
                        break

                    # Move up one level
                    usb_device = usb_device.parent
                    if not usb_device or usb_device == Path("/sys"):
                        break
            except Exception:
                continue

        return None

    def _resolve_device_path_initial(self) -> Optional[str]:
        """Resolve the tty node for this bridge at construction time.

        Preference order: durable usb_identity snapshot (survives USB
        re-enumeration), explicit device-path override, a /dev/* path stored
        in the pin field, then sysfs lookup by USB serial. An unresolvable
        identity falls through to the legacy order so a net that the old
        logic could still open keeps working.
        """
        if self.usb_identity:
            path = self._resolve_identity_path()
            if path:
                return path
        if self.device_path_override:
            return self.device_path_override
        if (self.bridge_serial and isinstance(self.bridge_serial, str)
                and self.bridge_serial.startswith("/dev/")):
            # The "serial" is a direct device path; treat it as such.
            path = self.bridge_serial
            self.bridge_serial = ""
            return path
        if self.bridge_serial:
            return self._find_device_by_serial(self.bridge_serial)
        return None

    def _resolve_identity_path(self) -> Optional[str]:
        """Live tty for the usb_identity snapshot, or None. Never raises."""
        try:
            from lager.devices import serial_id
            return serial_id.resolve_identity(self.usb_identity)
        except Exception:
            return None

    # A locked-but-alive port (EBUSY/EAGAIN from the exclusive=True flock
    # arbitration, see _connect) must never be classified as device-gone or a
    # reconnect loop would churn against a healthy concurrent holder.
    _DEVICE_GONE_ERRNOS = frozenset({errno.ENODEV, errno.ENOENT, errno.ENXIO, errno.EIO})
    _DEVICE_GONE_KEYWORDS = (
        'no such device', 'errno 19', 'enodev',
        'device disconnected', 'returned no data',
        'input/output error', 'errno 5',
        'no such file or directory', 'errno 2',
    )
    _DEVICE_BUSY_KEYWORDS = (
        'errno 11', 'errno 16', 'resource temporarily unavailable', 'busy', 'lock',
    )

    @classmethod
    def is_device_gone(cls, exc: Exception) -> bool:
        """True if *exc* indicates the underlying USB device went away.

        pyserial often wraps the OSError into a SerialException string, so a
        text match backs up the errno check.
        """
        if isinstance(exc, OSError) and exc.errno in cls._DEVICE_GONE_ERRNOS:
            return True
        text = str(exc).lower()
        if any(k in text for k in cls._DEVICE_BUSY_KEYWORDS):
            return False
        return any(k in text for k in cls._DEVICE_GONE_KEYWORDS)

    def try_reopen(self) -> bool:
        """One reopen attempt after the device vanished. Never raises.

        Closes the dead handle first (releasing the fd lets the kernel reuse
        the tty number), re-resolves the device by its durable identity, and
        reopens. Returns True when a fresh connection is open.
        """
        self._cleanup()
        path = None
        if self.usb_identity:
            path = self._resolve_identity_path()
            if not path and self.bridge_serial:
                path = self._find_device_by_serial(self.bridge_serial)
        elif self.bridge_serial:
            path = self._find_device_by_serial(self.bridge_serial)
        else:
            # No durable identity available (never successfully opened and no
            # USB serial): all we can do is retry the stored path once it
            # exists again — a symlink (/dev/serial/by-*) re-resolves at open.
            raw = self.device_path_override or self.device_path
            if raw and os.path.exists(raw):
                path = raw
        if not path:
            return False
        try:
            self.device_path = path
            self._connect()
            return True
        except Exception:
            self._cleanup()
            return False

    def reconnect(self, *, stop_check=None, on_status=None,
                  total_timeout: float = 60.0) -> bool:
        """Re-resolve and reopen after a re-enumeration, with bounded backoff.

        Args:
            stop_check: callable polled between attempts; returning True
                aborts the wait (session shutdown)
            on_status: callable(status: str) invoked with 'reconnecting' once
                up front and 'reconnected' on success; exceptions ignored
            total_timeout: give up after this many seconds

        Returns True when the port is open again.
        """
        def _status(status):
            if on_status:
                try:
                    on_status(status)
                except Exception:
                    pass

        _status('reconnecting')
        deadline = time.monotonic() + total_timeout
        delay = 0.5
        while True:
            if stop_check and stop_check():
                return False
            if self.try_reopen():
                _status('reconnected')
                return True
            if time.monotonic() >= deadline:
                return False
            # Sleep in short slices so stop_check aborts promptly.
            slice_end = min(time.monotonic() + delay, deadline)
            while time.monotonic() < slice_end:
                if stop_check and stop_check():
                    return False
                time.sleep(min(0.25, max(0.0, slice_end - time.monotonic())))
            delay = min(delay * 2, 5.0)

    def _reconnect_with_notices(self, stop_check=None) -> bool:
        """reconnect() with the stderr notices used by the monitor modes."""
        sys.stderr.buffer.write(
            b"\r\n\033[33m[device disconnected - reconnecting...]\033[0m\r\n")
        sys.stderr.buffer.flush()
        if not self.reconnect(stop_check=stop_check):
            sys.stderr.buffer.write(b"\r\n\033[31m[device did not return]\033[0m\r\n")
            sys.stderr.buffer.flush()
            return False
        msg = f"\033[32m[reconnected to {self.device_path}]\033[0m\r\n"
        sys.stderr.buffer.write(msg.encode())
        sys.stderr.buffer.flush()
        return True

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

        # NOTE: we intentionally do NOT `fuser -k` the device before opening.
        # We open with exclusive=True (pyserial flock) so a second opener fails
        # fast instead of interleaving reads. Killing the current holder would
        # invert that arbitration — a `lager uart` CLI open would kill the
        # box_http_server holding the port (and vice versa). flock is released
        # automatically when the holding process dies, so a stale lock from a
        # crashed opener does not persist and needs no forced kill.

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

        # Snapshot the durable USB identity of whatever we actually opened so
        # a later re-enumeration can be healed even when the net stored only a
        # raw /dev/tty* path (whose number may point elsewhere afterwards).
        try:
            from lager.devices import serial_id
            ident = serial_id.identity_for_tty(self.device_path)
            if ident:
                self.usb_identity = ident
        except Exception:
            pass

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
                try:
                    data = self.serial_conn.read(self.serial_conn.in_waiting or 1)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    if not self.is_device_gone(exc):
                        raise
                    if not self._reconnect_with_notices():
                        break
                    continue
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
                    try:
                        data = self.serial_conn.read(self.serial_conn.in_waiting or 1)
                    except Exception as exc:
                        if stop_event.is_set() or not self.is_device_gone(exc):
                            raise
                        if not self._reconnect_with_notices(stop_check=stop_event.is_set):
                            # Device never came back: end the whole session so
                            # the stdin thread stops too.
                            stop_event.set()
                            break
                        continue
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

                except Exception as exc:
                    if stop_event.is_set():
                        break
                    if self.is_device_gone(exc) or not (
                            self.serial_conn and self.serial_conn.is_open):
                        # The reader thread is reconnecting after a device
                        # re-enumeration; drop typed bytes until the port is
                        # back instead of killing the session.
                        time.sleep(0.1)
                        continue
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

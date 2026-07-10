# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART Net class for the lager Python API.

Provides clean access to UART nets from Python scripts running on the box.
"""
from __future__ import annotations

import os
from typing import Optional
import serial


def usb_identity_for_net_record(record: dict) -> Optional[dict]:
    """Best-effort durable USB identity snapshot for a saved uart net record.

    ``pin`` is either a raw ``/dev/tty*`` path (snapshot that node directly)
    or a USB serial number (snapshot the cable's tty matching the record's
    channel/interface). Returns None — never raises — when the device is not
    currently attached, so saving an offline net still succeeds unchanged.
    """
    try:
        from lager.devices import serial_id
        pin = record.get('device_path') or record.get('pin') or ''
        if isinstance(pin, str) and pin.startswith('/dev/'):
            return serial_id.identity_for_tty(pin)
        if pin:
            pin = str(pin)  # tolerate numeric serials stored as JSON numbers
            channel = str(record.get('channel', '0') or '0')
            want = int(channel) if channel.isdigit() else 0
            for cable in serial_id.list_cables():
                if cable.get('serial') == pin:
                    ident = serial_id.identity_for_tty(cable['tty'])
                    if ident and ident.get('interface') in (None, want):
                        return ident
    except Exception:
        pass
    return None


# Pins under here are udev-managed symlinks that re-point at the live node
# after a re-enumeration, so they are resolvable without a usb_identity.
_STABLE_PIN_PREFIX = '/dev/serial/'


def _stable_pin(record: dict) -> str:
    pin = record.get('device_path') or record.get('pin') or ''
    if isinstance(pin, str) and pin.startswith(_STABLE_PIN_PREFIX):
        return pin
    return ''


def has_durable_identity(record: dict) -> bool:
    """True when a uart record can be resolved to its live tty node."""
    return bool(record.get('usb_identity')) or bool(_stable_pin(record))


def live_uart_path(record: dict) -> Optional[str]:
    """Where a saved uart net's device lives right now, or None if absent.

    Mirrors the connect-time resolution order: durable usb_identity first,
    then a stable /dev/serial/by-* symlink. Raw /dev/tty* pins without an
    identity are not resolvable (the stored label is all we know), so callers
    should only attach this for records where has_durable_identity() holds.
    Display-only; never raises.
    """
    try:
        from lager.devices import serial_id
        ident = record.get('usb_identity')
        if ident:
            path = serial_id.resolve_identity(ident)
            if path:
                return path
        pin = _stable_pin(record)
        if pin:
            real = os.path.realpath(pin)
            if real != pin and os.path.exists(real):
                return real
    except Exception:
        pass
    return None


class UARTNet:
    """
    Represents a UART net configuration.

    Provides clean API for accessing UART serial ports from Python scripts.

    Example:
        from lager import Net, NetType

        # Get the UART net
        uart_net = Net.get('TARGET_SERIAL', NetType.UART)

        # Get the device path
        device_path = uart_net.get_path()  # Returns "/dev/ttyUSB0"

        # Use with pyserial
        import serial
        ser = serial.Serial(device_path, baudrate=115200)

        # Or use the convenience method
        ser = uart_net.connect(baudrate=115200)
    """

    def __init__(self, name: str, net_config: dict):
        """
        Initialize UART net.

        Args:
            name: Net name
            net_config: Net configuration dict from saved_nets.json
        """
        self.name = name
        self._config = net_config

        # Extract configuration
        self.usb_serial = net_config.get('pin', '')
        self.channel = net_config.get('channel', '0')
        self.params = net_config.get('params', {})

        # Cache the device path (lazy loaded)
        self._device_path: Optional[str] = None

    def get_path(self) -> str:
        """
        Get the device path for this UART net.

        Returns:
            Device path like "/dev/ttyUSB0"

        Raises:
            FileNotFoundError: If the UART device is not connected
        """
        if self._device_path is not None and not os.path.exists(self._device_path):
            # The cached node vanished (USB re-enumeration); re-resolve.
            self._device_path = None

        if self._device_path is None:
            # Use the dispatcher to resolve the device path
            from .uart_bridge import UARTBridge

            # Create a temporary bridge instance just to get the device path
            # The bridge __init__ resolves the USB serial to a /dev path
            bridge = UARTBridge(
                bridge_serial=self.usb_serial,
                port=self.channel,
                device_path=self._config.get('device_path'),
                usb_identity=self._config.get('usb_identity'),
                baudrate=self.params.get('baudrate', 115200)
            )
            self._device_path = bridge.device_path

        return self._device_path

    def connect(self, **overrides) -> serial.Serial:
        """
        Connect to the UART serial port with pyserial.

        Args:
            **overrides: Parameter overrides (baudrate, bytesize, parity, stopbits, etc.)

        Returns:
            Connected pyserial Serial object

        Example:
            ser = uart_net.connect(baudrate=115200, timeout=1.0)
            data = ser.read(100)
            ser.write(b"hello\\n")
        """
        from .uart_bridge import UARTBridge

        # Merge params with overrides
        final_params = {**self.params, **overrides}

        # Create bridge driver
        bridge = UARTBridge(
            bridge_serial=self.usb_serial,
            port=self.channel,
            device_path=self._config.get('device_path'),
            usb_identity=self._config.get('usb_identity'),
            **final_params
        )

        # Connect and return the serial connection
        bridge._connect()
        return bridge.serial_conn

    def get_baudrate(self) -> int:
        """Get the configured baudrate for this net."""
        return self.params.get('baudrate', 115200)

    def get_config(self) -> dict:
        """Get the raw net configuration dict."""
        return self._config.copy()

    def __str__(self):
        return f'<UARTNet name="{self.name}" path="{self._device_path or "not connected"}">'

    def __repr__(self):
        return str(self)

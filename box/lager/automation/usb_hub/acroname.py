# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
AcronameUSBNet – driver for USBHub2x4 / USBHub3p / USBHub3c.

Implements: enable / disable / toggle
Lazy-imports BrainStem to minimise start-up cost.
"""

from __future__ import annotations

import atexit
import signal

from .usb_net import USBNet, LibraryMissingError, DeviceNotFoundError, PortStateError


class AcronameUSBNet(USBNet):
    """USBNet driver for Acroname STEM hubs (0-based port numbers).

    Each net binds the *specific* hub named by its address serial, so a box
    with more than one Acroname hub routes every net to the right hardware.
    Connections are cached per-serial (not one global hub).
    """

    _cached_hubs: dict = {}   # serial (int) | None  ->  connected hub
    _brainstem = None         # cached vendor module
    _Result = None            # brainstem.result.Result alias
    _cleanup_registered = False

    # ------------------------------------------------------------------ #
    # helper: import BrainStem only when needed
    # ------------------------------------------------------------------ #
    def _require_library(self):
        if AcronameUSBNet._brainstem is not None:
            return  # already imported

        try:
            import brainstem  # pylint: disable=import-error
            from brainstem.result import Result
        except ModuleNotFoundError as exc:
            raise LibraryMissingError(
                "BrainStem Python SDK not installed inside the box "
                "(pip install brainstem)."
            ) from exc

        AcronameUSBNet._brainstem = brainstem
        AcronameUSBNet._Result = Result

    # ------------------------------------------------------------------ #
    # graceful cleanup
    # ------------------------------------------------------------------ #
    @classmethod
    def disconnect(cls):
        """Disconnect every cached hub so the USB devices are released."""
        for hub in cls._cached_hubs.values():
            try:
                hub.disconnect()
            except Exception:
                pass
        cls._cached_hubs = {}

    @classmethod
    def _register_cleanup(cls):
        """Install atexit/SIGTERM cleanup exactly once."""
        if cls._cleanup_registered:
            return
        cls._cleanup_registered = True
        atexit.register(cls.disconnect)
        # SIGTERM doesn't run atexit by default — install a handler that calls
        # sys.exit() so atexit handlers fire, but only if no custom handler has
        # been installed already.
        current = signal.getsignal(signal.SIGTERM)
        if current in (signal.SIG_DFL, None):
            def _sigterm_exit(signum, frame):
                raise SystemExit(1)
            signal.signal(signal.SIGTERM, _sigterm_exit)

    # ------------------------------------------------------------------ #
    # address parsing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_address(address):
        """Pull (serial, pid) out of a VISA-style address.

        e.g. 'USB0::0x24FF::0x0013::BFABDDC4::INSTR' -> (0xBFABDDC4, 0x0013).
        Returns (None, None) for anything that doesn't match.
        """
        if not address:
            return (None, None)
        parts = str(address).split("::")
        serial = pid = None
        if len(parts) >= 4:
            try:
                serial = int(parts[3], 16)
            except ValueError:
                serial = None
        if len(parts) >= 3:
            try:
                pid = int(parts[2], 16)
            except ValueError:
                pid = None
        return (serial, pid)

    # ------------------------------------------------------------------ #
    # lazy hub discovery (cached per serial)
    # ------------------------------------------------------------------ #
    def _connect_hub(self):
        self._require_library()
        serial = self._serial
        hub = AcronameUSBNet._cached_hubs.get(serial)
        if hub is not None:
            return hub

        stem = self._brainstem.stem
        spec = self._brainstem.link.Spec.USB

        # Order the hub classes so a port-capable class is tried first: binding
        # an 8-port hub with the 4-port class would truncate ports 4-7. The PID
        # from the address picks the best starting class; the rest are tried as
        # a fallback. Connecting by serial guarantees we bind THIS net's hub.
        hub3 = (stem.USBHub3p, stem.USBHub3c)   # 8-port families
        hub2 = (stem.USBHub2x4,)                # 4-port family
        classes = (hub2 + hub3) if self._pid == 0x0011 else (hub3 + hub2)

        for cls in classes:
            candidate = cls()
            if serial is not None:
                rc = candidate.discoverAndConnect(spec, serial)
            else:
                rc = candidate.discoverAndConnect(spec)
            if rc == self._Result.NO_ERROR:
                AcronameUSBNet._cached_hubs[serial] = candidate
                self._register_cleanup()
                return candidate

        where = f" with serial 0x{serial:08X}" if serial is not None else ""
        raise DeviceNotFoundError(f"No Acroname hub detected on USB{where}")

    # ------------------------------------------------------------------ #
    # constructor — remembers which physical hub this net belongs to
    # ------------------------------------------------------------------ #
    def __init__(self, net_info: dict | None = None) -> None:
        self._serial, self._pid = self._parse_address(
            (net_info or {}).get("address"))

    # ------------------------------------------------------------------ #
    # internal – decode enable+power bits
    # ------------------------------------------------------------------ #
    @staticmethod
    def _port_enabled(raw_state: int) -> bool:
        return (raw_state & 0b11) == 0b11

    # ------------------------------------------------------------------ #
    # USBNet interface
    # ------------------------------------------------------------------ #
    def enable(self, net_name: str, port: int) -> None:  # type: ignore[override]
        hub = self._connect_hub()
        hub.usb.setPortEnable(port)

    def disable(self, net_name: str, port: int) -> None:  # type: ignore[override]
        hub = self._connect_hub()
        hub.usb.setPortDisable(port)

    def _read_enabled(self, hub, port: int) -> bool:
        """Read the live enabled/disabled state of a port from the hub."""
        res = hub.usb.getPortState(port)
        if res.error != self._Result.NO_ERROR:
            raise PortStateError(f"Acroname error code {res.error}")
        return self._port_enabled(res.value)

    def state(self, net_name: str, port: int) -> bool:  # type: ignore[override]
        hub = self._connect_hub()
        return self._read_enabled(hub, port)

    def toggle(self, net_name: str, port: int) -> bool:  # type: ignore[override]
        hub = self._connect_hub()
        currently_on = self._read_enabled(hub, port)
        if currently_on:
            hub.usb.setPortDisable(port)
        else:
            hub.usb.setPortEnable(port)
        return not currently_on

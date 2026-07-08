# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
AcronameUSBNet – driver for USBHub2x4 / USBHub3p / USBHub3c.

Implements: enable / disable / toggle
Lazy-imports BrainStem to minimise start-up cost.
"""

from __future__ import annotations

from .usb_net import (
    USBNet,
    LibraryMissingError,
    DeviceNotFoundError,
    PortStateError,
    hub_access,
)

# BrainStem/USB access to a hub is EXCLUSIVE, and the hub is driven from several
# box processes — box_http_server (the `lager usb` path), the MCP server, and
# each `lager python` test (its own subprocess). Serialise their access with the
# shared cross-process device lock (fcntl.flock, shared /tmp) and NEVER hold a
# hub connected between operations, so no process pins it open and blocks the
# others. Mirrors the YKUSH driver; see ykush.py.
_LOCK_TIMEOUT_S = 10.0


class AcronameUSBNet(USBNet):
    """USBNet driver for Acroname STEM hubs (0-based port numbers).

    Each net binds the *specific* hub named by its address serial, so a box
    with more than one Acroname hub routes every net to the right hardware. A
    fresh connection is opened per operation and disconnected immediately after
    (under a cross-process lock), so the hub is never left claimed — which would
    otherwise block another process (e.g. a `lager python` test) from opening it.
    """

    _brainstem = None         # cached vendor MODULE (an import, not a handle)
    _Result = None            # brainstem.result.Result alias

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
    # constructor — remembers which physical hub this net belongs to
    # ------------------------------------------------------------------ #
    def __init__(self, net_info: dict | None = None) -> None:
        net_info = net_info or {}
        self.address = net_info.get("address")
        self._serial, self._pid = self._parse_address(self.address)

    # ------------------------------------------------------------------ #
    # cross-process lock + open/operate/close  (never cache a live hub)
    # ------------------------------------------------------------------ #
    def _lock_key(self) -> str:
        """Lock key identifying the *physical* hub, so all nets on one hub
        serialise but different hubs don't block each other."""
        return self.address or f"acroname::{self._serial}"

    def _open_hub(self):
        """Discover and connect THIS net's hub. Never cached — the caller
        disconnects it via ``_close_hub`` as soon as the operation completes."""
        self._require_library()
        serial = self._serial

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
                return candidate

        where = f" with serial 0x{serial:08X}" if serial is not None else ""
        raise DeviceNotFoundError(f"No Acroname hub detected on USB{where}")

    @staticmethod
    def _close_hub(hub) -> None:
        """Disconnect the hub, releasing the USB claim. Best-effort."""
        if hub is None:
            return
        try:
            hub.disconnect()
        except Exception:
            pass

    def _with_hub(self, fn):
        """Serialise across threads and processes, open a fresh hub connection,
        run ``fn(hub)``, and always disconnect so the hub is never left claimed."""
        with hub_access(self._lock_key(), timeout=_LOCK_TIMEOUT_S):
            hub = None
            try:
                hub = self._open_hub()
                return fn(hub)
            finally:
                self._close_hub(hub)

    # ------------------------------------------------------------------ #
    # internal – decode enable+power bits
    # ------------------------------------------------------------------ #
    @staticmethod
    def _port_enabled(raw_state: int) -> bool:
        return (raw_state & 0b11) == 0b11

    def _read_enabled(self, hub, port: int) -> bool:
        """Read the live enabled/disabled state of a port from the hub."""
        res = hub.usb.getPortState(port)
        if res.error != self._Result.NO_ERROR:
            raise PortStateError(f"Acroname error code {res.error}")
        return self._port_enabled(res.value)

    # ------------------------------------------------------------------ #
    # USBNet interface
    # ------------------------------------------------------------------ #
    def enable(self, net_name: str, port: int) -> None:  # type: ignore[override]
        self._with_hub(lambda hub: hub.usb.setPortEnable(port))

    def disable(self, net_name: str, port: int) -> None:  # type: ignore[override]
        self._with_hub(lambda hub: hub.usb.setPortDisable(port))

    def state(self, net_name: str, port: int) -> bool:  # type: ignore[override]
        return self._with_hub(lambda hub: self._read_enabled(hub, port))

    def toggle(self, net_name: str, port: int) -> bool:  # type: ignore[override]
        def _do(hub):
            currently_on = self._read_enabled(hub, port)
            if currently_on:
                hub.usb.setPortDisable(port)
            else:
                hub.usb.setPortEnable(port)
            return not currently_on

        return self._with_hub(_do)

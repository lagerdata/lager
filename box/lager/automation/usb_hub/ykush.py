# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import re
import subprocess
from typing import Any, Callable, Sequence

from .usb_net import LibraryMissingError, USBNet

# ────────────────────────────────────────────────────────────────────
#  helpers – regex, constants
# ────────────────────────────────────────────────────────────────────
_SERIAL_RE = re.compile(r"::([^:]+)::INSTR$")


def _serial_from_address(addr: str | None) -> str | None:
    """Extract the hub serial ('YK26395') from a VISA-style address."""
    if addr:
        match = _SERIAL_RE.search(addr)
        if match:
            return match.group(1)
    return None


# ────────────────────────────────────────────────────────────────────
#  dynamic import of Yepkit API   (supports both layouts)
#  Deferred until class is actually used to allow module import
# ────────────────────────────────────────────────────────────────────
_YKUSH_CLS: type | None = None
_PORT_UP: int | None = None
_PORT_DOWN: int | None = None
_LIBRARY_CHECKED: bool = False


def _first_ok(seq: Sequence[str], getter: Callable[[str], Any]) -> Any | None:
    """Return first non-None getter(module_name) across *seq* (or None)."""
    for name in seq:
        try:
            mod = importlib.import_module(name)
            val = getter(mod)
            if val is not None:
                return val
        except ImportError:
            pass
    return None


def _ensure_library() -> None:
    """Load the Yepkit library lazily, raising LibraryMissingError if not available."""
    global _YKUSH_CLS, _PORT_UP, _PORT_DOWN, _LIBRARY_CHECKED

    if _LIBRARY_CHECKED:
        if _YKUSH_CLS is None:
            raise LibraryMissingError(
                "Could not import Yepkit API. Please install with:\n"
                "    pip install git+https://github.com/Yepkit/pykush@master pyusb hidapi"
            )
        return

    # Try root-level package, then the wheels' pykush.pykush submodule
    _API_MODULES = ("pykush", "pykush.pykush")

    _YKUSH_CLS = _first_ok(_API_MODULES, lambda m: getattr(m, "YKUSH", None))
    _PORT_UP = _first_ok(_API_MODULES, lambda m: getattr(m, "YKUSH_PORT_STATE_UP", None))
    _PORT_DOWN = _first_ok(_API_MODULES, lambda m: getattr(m, "YKUSH_PORT_STATE_DOWN", None))

    _LIBRARY_CHECKED = True

    if _YKUSH_CLS is None or _PORT_UP is None or _PORT_DOWN is None:
        raise LibraryMissingError(
            "Could not import Yepkit API. Please install with:\n"
            "    pip install git+https://github.com/Yepkit/pykush@master pyusb hidapi"
        )

    # type-check assists - convert to int after validation
    _PORT_UP = int(_PORT_UP)
    _PORT_DOWN = int(_PORT_DOWN)


# ────────────────────────────────────────────────────────────────────
#  concrete driver
# ────────────────────────────────────────────────────────────────────
class YKUSHUSBNet(USBNet):
    """USBNet implementation for Yepkit YKUSH hubs."""

    # Live YKUSH handles, keyed by serial. A handle is dropped and rebuilt on
    # first failure (see _with_device), so a power-cycled hub or a transient
    # USB/HID read error self-heals instead of staying wedged until a restart.
    _devices: dict = {}

    @classmethod
    def _device_for(cls, serial: str | None):
        dev = cls._devices.get(serial)
        if dev is None:
            _ensure_library()
            dev = _YKUSH_CLS(serial=serial) if serial else _YKUSH_CLS()
            cls._devices[serial] = dev
        return dev

    @classmethod
    def _invalidate(cls, serial: str | None) -> None:
        """Drop the cached handle for *serial*, closing it best-effort."""
        dev = cls._devices.pop(serial, None)
        if dev is None:
            return
        for closer in ("close", "disconnect"):
            fn = getattr(dev, closer, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass

    def _with_device(self, fn):
        """Run ``fn(dev)``, retrying once with a fresh handle if the cached one
        fails. This recovers a YKUSH that was power-cycled or hit a transient
        USB/HID read error without needing a hardware-service restart."""
        _ensure_library()
        try:
            return fn(self._device_for(self.serial))
        except LibraryMissingError:
            raise
        except Exception:
            # Cached handle is likely stale (device re-enumerated). Drop it and
            # try once more with a fresh connection.
            self._invalidate(self.serial)
            return fn(self._device_for(self.serial))

    # ----------------------------------------------------------------
    def __init__(self, net_info: dict | None = None) -> None:
        # Don't check library here - defer until first use
        net_info = net_info or {}
        self.serial = (
            net_info.get("serial")
            or net_info.get("uid")
            or net_info.get("serial_number")
            or _serial_from_address(net_info.get("address"))
        )

    # ----------------------------------------------------------------
    @staticmethod
    def _validate_port(port: int) -> None:
        if port < 1:
            raise ValueError("Port number must be ≥ 1")

    # ----------------------------------------------------------------
    def _set_state(self, dev, port: int, state: int) -> None:
        # New API
        if hasattr(dev, "set_port_state"):
            if not dev.set_port_state(port, state):
                raise RuntimeError(f"Failed to set port {port} to state {state}")
            return

        # Legacy helpers
        if state == _PORT_UP and hasattr(dev, "switch_port_on"):
            dev.switch_port_on(port)
            return
        if state == _PORT_DOWN and hasattr(dev, "switch_port_off"):
            dev.switch_port_off(port)
            return

        # Last-ditch: CLI utility
        self._shell_fallback(port, state == _PORT_UP)

    @staticmethod
    def _shell_fallback(port: int, turn_on: bool) -> None:
        cmd = ["ykushcmd", "-u" if turn_on else "-d", str(port)]
        try:
            subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(f"ykushcmd fallback failed: {exc}") from exc

    # ----------------------------------------------------------------
    #  USBNet interface
    # ----------------------------------------------------------------
    def enable(self, net_name: str, port: int) -> None:        # type: ignore[override]
        self._validate_port(port)
        self._with_device(lambda dev: self._set_state(dev, port, _PORT_UP))

    def disable(self, net_name: str, port: int) -> None:       # type: ignore[override]
        self._validate_port(port)
        self._with_device(lambda dev: self._set_state(dev, port, _PORT_DOWN))

    @staticmethod
    def _read_enabled(dev, port: int) -> bool:
        """Read the live enabled/disabled state of a port from the device."""
        try:
            return bool(dev.get_port_state(port))
        except AttributeError:
            return bool(getattr(dev, "switch_port_state_get", lambda p: 0)(port))

    def state(self, net_name: str, port: int) -> bool:        # type: ignore[override]
        self._validate_port(port)
        return self._with_device(lambda dev: self._read_enabled(dev, port))

    def toggle(self, net_name: str, port: int) -> bool:        # type: ignore[override]
        self._validate_port(port)

        def _do(dev):
            currently_on = self._read_enabled(dev, port)
            target = _PORT_DOWN if currently_on else _PORT_UP
            self._set_state(dev, port, target)
            return target == _PORT_UP

        return self._with_device(_do)


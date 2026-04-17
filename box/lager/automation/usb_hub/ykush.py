# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import re
import subprocess
from functools import lru_cache
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

    # Simple LRU cache: key → YKUSH instance (max 16 hubs)
    @staticmethod
    @lru_cache(maxsize=16)
    def _device_for(serial: str | None):
        _ensure_library()
        return _YKUSH_CLS(serial=serial) if serial else _YKUSH_CLS()

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
    def _set_state(self, port: int, state: int) -> None:
        _ensure_library()
        dev = self._device_for(self.serial)

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
        import sys as _sys
        if _sys.platform == "darwin":
            raise RuntimeError(
                "ykushcmd CLI fallback is not available on macOS. "
                "Ensure the pykush Python library is installed with working "
                "set_port_state() / switch_port_on() / switch_port_off() methods."
            )
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
        _ensure_library()
        self._set_state(port, _PORT_UP)

    def disable(self, net_name: str, port: int) -> None:       # type: ignore[override]
        self._validate_port(port)
        _ensure_library()
        self._set_state(port, _PORT_DOWN)

    def toggle(self, net_name: str, port: int) -> None:        # type: ignore[override]
        self._validate_port(port)
        _ensure_library()
        dev = self._device_for(self.serial)

        try:
            currently_on = bool(dev.get_port_state(port))
        except AttributeError:
            currently_on = bool(getattr(dev, "switch_port_state_get", lambda p: 0)(port))

        target = _PORT_DOWN if currently_on else _PORT_UP
        self._set_state(port, target)


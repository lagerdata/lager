# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import re
import subprocess
from typing import Any, Callable, Sequence

from .usb_net import LibraryMissingError, USBNet, hub_access

# ────────────────────────────────────────────────────────────────────
#  helpers – regex, constants
# ────────────────────────────────────────────────────────────────────
_SERIAL_RE = re.compile(r"::([^:]+)::INSTR$")

# How long to wait for the cross-process hub lock before giving up. libusb
# access to the hub is EXCLUSIVE, so box_http_server (the `lager usb` path),
# the MCP server, and each `lager python` test (its own subprocess) must not
# open the same hub at once; device_lock (fcntl.flock, shared /tmp) serialises
# them. Generous because a genuinely stuck holder is rare and releasing per op
# keeps real hold times to milliseconds.
_LOCK_TIMEOUT_S = 10.0


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

    @staticmethod
    def _release(dev) -> None:
        """Close the pykush handle deterministically so the libusb/usbfs claim
        on the hub is released the moment the operation finishes.

        pykush only frees the device in ``__del__``. Relying on GC means a
        long-lived process (box_http_server, the MCP server) keeps the hub
        claimed indefinitely after the first use, which makes every *other*
        process — notably an in-container ``lager python`` test running in its
        own subprocess — fail to open the same hub with "OSError: open failed".
        Closing here (and nulling ``_devhandle`` so a later ``__del__`` is a
        no-op) hands the hub back immediately."""
        if dev is None:
            return
        handle = getattr(dev, "_devhandle", None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
            try:
                dev._devhandle = None
            except Exception:
                pass

    def _run_once(self, fn):
        """Open a fresh YKUSH connection, run ``fn(dev)``, and always release
        the handle — never cache it (see ``_release``)."""
        _ensure_library()
        dev = None
        try:
            dev = _YKUSH_CLS(serial=self.serial) if self.serial else _YKUSH_CLS()
            return fn(dev)
        finally:
            self._release(dev)

    def _lock_key(self) -> str:
        """Cross-process lock key identifying the *physical* hub, so every net
        on one YKUSH serialises but different hubs don't block each other. The
        VISA address is unique per hub; fall back to the serial."""
        return self.address or f"ykush::{self.serial or 'default'}"

    def _with_device(self, fn):
        """Run ``fn(dev)`` against a freshly-opened hub, retrying once if the
        first attempt fails. A fresh handle per call self-heals a power-cycled
        hub or a transient USB/HID error, and releasing it after each call means
        the hub is never pinned open. The whole open→operate→close cycle (and
        the retry) runs under the shared cross-process device lock so concurrent
        callers — e.g. box_http_server and a ``lager python`` test — don't
        collide on the hub's exclusive libusb claim."""
        _ensure_library()
        with hub_access(self._lock_key(), timeout=_LOCK_TIMEOUT_S):
            try:
                return self._run_once(fn)
            except LibraryMissingError:
                raise
            except Exception:
                # Transient (power-cycled hub / stale enumeration). The first
                # handle was already released; try once more, still holding the
                # lock so no other process slips in between attempts.
                return self._run_once(fn)

    # ----------------------------------------------------------------
    def __init__(self, net_info: dict | None = None) -> None:
        # Don't check library here - defer until first use
        net_info = net_info or {}
        self.address = net_info.get("address")
        self.serial = (
            net_info.get("serial")
            or net_info.get("uid")
            or net_info.get("serial_number")
            or _serial_from_address(self.address)
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


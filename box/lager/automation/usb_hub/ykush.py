# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib
import logging
import re
import subprocess
import time
from typing import Any, Callable, Sequence

from .usb_net import (
    HUB_OP_TIMEOUT_S,
    LibraryMissingError,
    USBNet,
    hub_access,
    run_hub_op,
)

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

# Above this, a completed cycle logs at INFO instead of DEBUG — same threshold
# and line shape as the Acroname driver, so one grep reads both.
_SLOW_CYCLE_INFO_S = 2.0

logger = logging.getLogger(__name__)


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
    """USBNet implementation for Yepkit YKUSH hubs.

    A fresh device handle is opened per operation and released immediately
    (see ``_release``), so the hub is never pinned. To keep each open cheap
    the resolved HID device *path* is cached per hub — opening by path skips
    pykush's full HID enumeration. A stale path (hub re-enumerated) fails
    the open, is dropped from the cache, and the open-by-serial path runs.
    """

    # Per-hub cache: lock key -> HID device path (metadata only, never a
    # live handle).
    _path_cache: dict = {}
    # Once a pykush build rejects the ``path=`` keyword (TypeError), stop
    # trying — otherwise every later op would re-cache ``_path``, fail the
    # path= open, and re-enumerate forever.
    _path_open_supported: bool = True

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

    def _open_device(self):
        """Open the hub, preferring the cached HID path (no enumeration).

        Best-effort: pykush builds without a ``path`` keyword, or a stale
        cached path, just fall through to the open-by-serial enumeration
        (and refresh the cache from the freshly opened device).
        """
        key = self._lock_key()
        path = YKUSHUSBNet._path_cache.get(key)
        if path is not None and YKUSHUSBNet._path_open_supported:
            try:
                return _YKUSH_CLS(path=path)
            except TypeError:
                # Constructor has no ``path=`` kwarg on this build.
                YKUSHUSBNet._path_open_supported = False
                YKUSHUSBNet._path_cache.clear()
            except Exception:
                YKUSHUSBNet._path_cache.pop(key, None)
        dev = _YKUSH_CLS(serial=self.serial) if self.serial else _YKUSH_CLS()
        if YKUSHUSBNet._path_open_supported:
            new_path = getattr(dev, "_path", None)
            if new_path:
                YKUSHUSBNet._path_cache[key] = new_path
        return dev

    def _run_once(self, fn):
        """Open a fresh YKUSH connection, run ``fn(dev)``, and always release
        the handle — never cache it (see ``_release``).

        Accumulates open/op/close time into ``self._cycle_phases`` (set up by
        ``_with_device``) — accumulates, because the retry runs this twice.
        Written via ``getattr`` so a test that stubs this method out, or a
        direct caller, costs nothing and breaks nothing."""
        _ensure_library()
        phases = getattr(self, "_cycle_phases", None)

        def _mark(phase, t0):
            if phases is not None:
                phases[phase] = phases.get(phase, 0.0) + (time.monotonic() - t0)

        dev = None
        try:
            t0 = time.monotonic()
            try:
                dev = self._open_device()
            finally:
                _mark("open", t0)
            t0 = time.monotonic()
            result = fn(dev)
            _mark("op", t0)
            return result
        finally:
            t0 = time.monotonic()
            self._release(dev)
            _mark("close", t0)

    def _lock_key(self) -> str:
        """Cross-process lock key identifying the *physical* hub, so every net
        on one YKUSH serialises but different hubs don't block each other. The
        VISA address is unique per hub; fall back to the serial."""
        return self.address or f"ykush::{self.serial or 'default'}"

    def _log_cycle(self, what, key, phases, outcome):
        """One line per completed cycle, mirroring the Acroname driver's:
        DEBUG normally, INFO when the total says the cycle paid enumeration.
        No open-path label here — pykush has only the two shapes (cached HID
        path vs enumeration) and the open time alone separates them."""
        total = sum(phases.values())
        ms = {k: int(phases.get(k, 0.0) * 1000)
              for k in ("lock", "open", "op", "close")}
        level = logging.INFO if total >= _SLOW_CYCLE_INFO_S else logging.DEBUG
        logger.log(
            level,
            "YKUSH %s: %s cycle %dms (lock %dms, open %dms, op %dms, "
            "close %dms) -> %s",
            key, what, int(total * 1000), ms["lock"], ms["open"], ms["op"],
            ms["close"], outcome,
        )

    def _with_device(self, fn, *, timeout=None, what="op"):
        """Run ``fn(dev)`` against a freshly-opened hub, retrying once if the
        first attempt fails. A fresh handle per call self-heals a power-cycled
        hub or a transient USB/HID error, and releasing it after each call means
        the hub is never pinned open. The whole open→operate→close cycle (and
        the retry) runs under the shared cross-process device lock so concurrent
        callers — e.g. box_http_server and a ``lager python`` test — don't
        collide on the hub's exclusive libusb claim.

        Both attempts share ONE deadline: a hub whose HID link is wedged blocks
        pykush's open indefinitely rather than failing, so bounding each attempt
        separately would still let the pair run forever, and the retry only ever
        existed for errors that surface fast."""
        _ensure_library()
        phases = {}

        def _attempt_with_retry():
            try:
                return self._run_once(fn)
            except LibraryMissingError:
                raise
            except Exception:
                # Transient (power-cycled hub / stale enumeration). The first
                # handle was already released; try once more, still holding the
                # lock so no other process slips in between attempts.
                return self._run_once(fn)

        key = self._lock_key()
        start = time.monotonic()
        try:
            if timeout is None:
                with hub_access(key, timeout=_LOCK_TIMEOUT_S):
                    phases["lock"] = time.monotonic() - start
                    # Handed to _run_once via the instance under the hub lock,
                    # so concurrent cycles on other hubs cannot mix phases.
                    self._cycle_phases = phases
                    result = run_hub_op(key, _attempt_with_retry,
                                        timeout=HUB_OP_TIMEOUT_S)
            else:
                # A caller-supplied budget bounds the WHOLE cycle: the lock
                # wait and both open attempts count against it, so a contended
                # lock cannot silently double the caller's bound (issue #205).
                budget = min(timeout, HUB_OP_TIMEOUT_S)
                with hub_access(key, timeout=min(_LOCK_TIMEOUT_S, budget)):
                    phases["lock"] = time.monotonic() - start
                    self._cycle_phases = phases
                    remaining = max(0.5, budget - (time.monotonic() - start))
                    result = run_hub_op(key, _attempt_with_retry,
                                        timeout=remaining)
        except Exception as e:
            self._log_cycle(what, key, phases, outcome=type(e).__name__)
            raise
        self._log_cycle(what, key, phases, outcome="ok")
        return result

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
        self._with_device(lambda dev: self._set_state(dev, port, _PORT_UP),
                          what="enable")

    def disable(self, net_name: str, port: int) -> None:       # type: ignore[override]
        self._validate_port(port)
        self._with_device(lambda dev: self._set_state(dev, port, _PORT_DOWN),
                          what="disable")

    @staticmethod
    def _read_enabled(dev, port: int) -> bool:
        """Read the live enabled/disabled state of a port from the device."""
        try:
            return bool(dev.get_port_state(port))
        except AttributeError:
            return bool(getattr(dev, "switch_port_state_get", lambda p: 0)(port))

    def state(self, net_name: str, port: int) -> bool:        # type: ignore[override]
        self._validate_port(port)
        return self._with_device(lambda dev: self._read_enabled(dev, port),
                                 what="state")

    def states(self, ports, *, timeout=None) -> dict:          # type: ignore[override]
        """Read every requested port inside ONE device session.

        ``_with_device`` opens a fresh handle, operates, and closes, all under
        this hub's lock. Reading ports one net at a time therefore pays that
        whole cycle per port; here it is paid once.

        A port that fails validation or reads badly comes back as None rather
        than taking the other ports down with it.
        """
        def _read_all(dev):
            out = {}
            for port in ports:
                try:
                    self._validate_port(port)
                    out[port] = self._read_enabled(dev, port)
                except Exception:
                    out[port] = None
            return out

        return self._with_device(_read_all, timeout=timeout, what="states")

    def toggle(self, net_name: str, port: int) -> bool:        # type: ignore[override]
        self._validate_port(port)

        def _do(dev):
            currently_on = self._read_enabled(dev, port)
            target = _PORT_DOWN if currently_on else _PORT_UP
            self._set_state(dev, port, target)
            return target == _PORT_UP

        return self._with_device(_do, what="toggle")


# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Global LabJack UD-series handle manager -- the U3/U6 counterpart to
``labjack_handle.py``.

Why this is a sibling module and not a parameter on the existing one: the two
device families share a vendor and nothing else.

    LabJack T7      ->  LJM (libLabJackM)   ->  ``labjack.ljm``, integer handle,
                                                named Modbus registers
    LabJack U3/U6   ->  Exodriver           ->  ``u3`` / ``u6``, device object,
                        (liblabjackusb)         command/response Feedback objects

LJM does not talk to the U3/U6 at all -- it is T-series and Digit only -- so
there is no register name, no ``eReadName``, and no integer handle to share.

What this module keeps from ``labjack_handle.py``: the singleton, the
reference count, the ``atexit`` sweep, and ``LAGER_LABJACK_DEBUG``. Three
things it does differently:

1. **Devices are keyed by (model, serial).** The LJM manager holds exactly one
   handle and passes ``identifier="ANY"``, letting LJM pick whichever device it
   finds. That is safe while a box has one LabJack. It is not safe on a mixed
   bench, so this manager keys a dict and opens by serial when one is given.
2. **The handle is an object, not an int.** Liveness cannot be probed by
   reading a ``SERIAL_NUMBER`` register; ``configU3()`` serves instead.
3. **It owns pin-mux state.** See below -- this is the part with no T7
   analogue, and the part most likely to cause a silent wrong reading.

Pin mux
-------
On a UD device a flexible line is analog *or* digital, selected by the
``FIOAnalog``/``EIOAnalog`` bitmasks in ``configIO()``. There is no T7
equivalent: a T7's AIN and DIO are separate hardware.

Those masks are **whole-device state shared across every net and every role**.
An ADC net on AIN5 and a GPIO net on FIO5 are the same physical pin, and a
driver that wrote its own mask would silently flip another net's pin into the
wrong mode -- producing a plausible number rather than an error. So the
read-modify-write lives here, under the same lock that guards the device, and
never in an individual driver.

Usage:
    from lager.io.labjack_ud_handle import get_ud_device, set_channel_mode

    dev = get_ud_device(model="u3", serial="320012345")
    set_channel_mode(dev, dio=5, analog=True)
"""
from __future__ import annotations

import atexit
import importlib
import os
import re
import sys
import threading
from typing import Any, Dict, Optional, Tuple

DEBUG = bool(os.environ.get("LAGER_LABJACK_DEBUG"))

# Models this manager knows how to open, mapped to their LabJackPython module.
# A U6 is one entry away: the module exposes the same Feedback classes and the
# same configIO/getFeedback surface. It is deliberately absent until its USB
# product id has been confirmed against real hardware -- a wrong pid in
# SUPPORTED_USB is a silent discovery failure, not a loud one.
SUPPORTED_MODELS = {
    "u3": "u3",
}


def _debug(msg: str) -> None:
    """Debug logging when LAGER_LABJACK_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"LABJACK_UD_HANDLE: {msg}\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Import shim
# ---------------------------------------------------------------------------
# LabJackPython installs TOP-LEVEL modules -- ``u3``, ``u6``, ``LabJackPython``
# -- not a package namespace. That is a wider shadowing target than
# ``labjack.ljm``: any file called ``u3.py`` anywhere on sys.path wins, and a
# box script named after the device it drives is not far-fetched. Same defence
# as labjack_handle.py's, aimed at a different name.
# ---------------------------------------------------------------------------


def _prefer_dist_path(dist_name: str, wanted_rel: str) -> None:
    """Put *dist_name*'s install root at the front of sys.path.

    Best-effort: a missing distribution or an unreadable file list leaves
    sys.path untouched and lets the normal import fail with its own message.
    """
    try:
        from importlib.metadata import distribution
    except Exception:
        return
    try:
        dist = distribution(dist_name)
        for f in (dist.files or []):
            if str(f).endswith(wanted_rel):
                parent = os.path.dirname(str(dist.locate_file(f)))
                if parent and parent not in sys.path:
                    sys.path.insert(0, parent)
                return
    except Exception:
        pass  # best-effort


def _demote_shadowing_paths(module_name: str) -> list:
    """Move any sys.path entry holding a shadowing ``<module_name>.py`` to the end."""
    demoted = []
    for p in list(sys.path):
        try:
            if os.path.isfile(os.path.join(p, f"{module_name}.py")):
                demoted.append(p)
        except Exception:
            pass
    for p in demoted:
        try:
            sys.path.remove(p)
            sys.path.append(p)
        except ValueError:
            pass
    return demoted


_module_cache: Dict[str, Any] = {}
_module_errors: Dict[str, BaseException] = {}


def load_ud_module(model: str):
    """Import and cache the LabJackPython module for *model* (e.g. ``"u3"``).

    Raises:
        RuntimeError: if the model is unknown, or the module cannot be
            imported. The underlying ImportError is chained, because
            "LabJackPython is not installed" and "a local u3.py shadowed it"
            need different fixes and the message is the only thing that
            distinguishes them on a box.
    """
    model = (model or "").lower()
    if model not in SUPPORTED_MODELS:
        raise RuntimeError(
            f"Unsupported LabJack UD model {model!r}. "
            f"Known: {sorted(SUPPORTED_MODELS)}"
        )
    if model in _module_cache:
        return _module_cache[model]
    if model in _module_errors:
        raise RuntimeError(
            f"LabJackPython module {model!r} unavailable: {_module_errors[model]}"
        ) from _module_errors[model]

    module_name = SUPPORTED_MODELS[model]
    try:
        mod = importlib.import_module(module_name)
    except Exception as first_exc:
        _debug(f"first import of {module_name} failed: {first_exc!r}")
        demoted = _demote_shadowing_paths(module_name)
        if demoted:
            _debug(f"demoted shadowing paths: {demoted}")
        _prefer_dist_path("LabJackPython", f"{module_name}.py")
        sys.modules.pop(module_name, None)
        try:
            mod = importlib.import_module(module_name)
        except Exception as second_exc:
            _module_errors[model] = second_exc
            raise RuntimeError(
                f"Could not import LabJackPython's {module_name!r} module. "
                f"Install LabJackPython and the Exodriver "
                f"(liblabjackusb) in the box container. "
                f"first={first_exc!r} second={second_exc!r}"
            ) from second_exc

    _module_cache[model] = mod
    return mod


# ---------------------------------------------------------------------------
# Pin naming
# ---------------------------------------------------------------------------
# UD DIO numbering, which the Feedback commands take directly:
#     FIO0-7 -> 0-7,  EIO0-7 -> 8-15,  CIO0-3 -> 16-19
# The U3 has no MIO; the T7's PinRegistry.dio_to_name covers 0-22 and agrees
# with this over the range they share.
# ---------------------------------------------------------------------------

_PIN_RE = re.compile(r"^\s*(FIO|EIO|CIO)\s*(\d+)\s*$", re.IGNORECASE)
_PIN_BASE = {"FIO": 0, "EIO": 8, "CIO": 16}
_PIN_WIDTH = {"FIO": 8, "EIO": 8, "CIO": 4}

# Highest DIO on a U3. CIO3 is 19; there is no MIO.
MAX_DIO = 19

# CIO is dedicated digital -- it has no bit in either analog mask.
FIRST_DIGITAL_ONLY_DIO = 16


def pin_to_dio(pin) -> int:
    """Convert a pin identifier to a UD DIO number.

    Accepts an int (used as the DIO number directly) or a name such as
    ``"EIO3"``. Raises ValueError on anything else, rather than guessing --
    a mis-parsed pin silently drives the wrong line.
    """
    if isinstance(pin, bool):
        raise ValueError(f"Invalid LabJack UD pin: {pin!r}")
    if isinstance(pin, int):
        dio = pin
    else:
        match = _PIN_RE.match(str(pin))
        if not match:
            try:
                dio = int(str(pin).strip())
            except (TypeError, ValueError):
                raise ValueError(
                    f"Invalid LabJack UD pin {pin!r}. Expected a DIO number "
                    f"0-{MAX_DIO} or a name like FIO4 / EIO0 / CIO2."
                ) from None
        else:
            prefix = match.group(1).upper()
            index = int(match.group(2))
            if index >= _PIN_WIDTH[prefix]:
                raise ValueError(
                    f"{prefix}{index} does not exist -- "
                    f"{prefix} runs 0-{_PIN_WIDTH[prefix] - 1}."
                )
            dio = _PIN_BASE[prefix] + index
    if not 0 <= dio <= MAX_DIO:
        raise ValueError(
            f"LabJack UD DIO {dio} out of range (0-{MAX_DIO}; "
            f"FIO0-7, EIO0-7, CIO0-3)."
        )
    return dio


def dio_to_pin(dio: int) -> str:
    """Convert a UD DIO number back to its pin name."""
    if 0 <= dio <= 7:
        return f"FIO{dio}"
    if 8 <= dio <= 15:
        return f"EIO{dio - 8}"
    if 16 <= dio <= 19:
        return f"CIO{dio - 16}"
    return f"DIO{dio}"


class LabJackUDHandleManager:
    """Thread-safe registry of open UD devices, keyed by (model, serial).

    One entry per physical device. Every role -- ADC, DAC, GPIO -- on the same
    device shares one entry, because they share one USB claim and one pin-mux
    configuration.
    """

    _instance: Optional['LabJackUDHandleManager'] = None
    _singleton_lock = threading.Lock()

    # Annotated, not assigned: a class-level dict literal would be shared
    # state, and __new__ gives each (only) instance its own.
    _devices: Dict[Tuple[str, Optional[str]], Any]
    _ref_counts: Dict[Tuple[str, Optional[str]], int]
    _pin_modes: Dict[Tuple[Any, int], bool]
    _first_found: Dict[str, str]
    _lock: threading.RLock

    def __new__(cls) -> 'LabJackUDHandleManager':
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._devices = {}      # (model, serial) -> device object
                    inst._ref_counts = {}   # (model, serial) -> int
                    # (device serial, dio) -> True if analog. Avoids a USB
                    # round trip on every read; see set_channel_mode.
                    inst._pin_modes = {}
                    # model -> real serial that a firstFound open landed on.
                    # "First found" names one specific device, not "any", so
                    # this is what a None request may reuse.
                    inst._first_found = {}
                    inst._lock = threading.RLock()
                    cls._instance = inst
        return cls._instance

    # -- device lifecycle ------------------------------------------------

    def _resolve_open_key(self, model: str, serial: Optional[str],
                          unique_ok: bool = False
                          ) -> Optional[Tuple[str, Optional[str]]]:
        """Find an already-open entry that satisfies a request. Caller holds the lock.

        Entries are keyed by the device's REAL serial, never by what the caller
        asked for. A U3 reports no USB serial, so the scanner writes an empty
        serial slot and its nets resolve to None ("first found"), while a
        hand-written or migrated record may carry the real serial -- two ways
        of naming one device. Keying on the request made those two separate
        entries, so the second open raced the claim this process already held
        and failed outright (NullHandleException / LabJackException), in both
        orders, until close_all.

        A None request means "first found", so any open device of this model
        is a correct answer -- and reopening one we already hold is a
        guaranteed claim conflict rather than a second device.
        """
        if serial is not None:
            key = (model, serial)
            return key if key in self._devices else None
        # A None request means "first found", which names one specific device
        # and not "any device". Reusing whichever entry happened to be open
        # would, on a box with two U3s, bind a serial-less net to the
        # instrument some other net opened. Reuse only the device a previous
        # firstFound open actually landed on; otherwise open and let the
        # claim-conflict fallback in get_device settle it.
        known = self._first_found.get(model)
        if known is not None:
            key = (model, known)
            if key in self._devices:
                return key
        if unique_ok:
            # release/force_close only: with exactly one device of this model
            # open there is nothing to be ambiguous about, and refusing to
            # resolve would leak the claim. get_device does not take this
            # shortcut -- there, picking the wrong one of two drives the wrong
            # instrument rather than merely closing it.
            mine = [k for k in self._devices if k[0] == model]
            if len(mine) == 1:
                return mine[0]
        return None

    def get_device(self, model: str = "u3", serial: Optional[str] = None) -> Any:
        """Open (or reuse) the device for *model*/*serial* and return it.

        Args:
            model: ``"u3"`` today; see SUPPORTED_MODELS.
            serial: the device's serial number as a string, or None to take
                the first one found. None is correct for a box with a single
                UD device and ambiguous for a box with two -- the caller
                (the dispatcher, via the net's address) is what resolves it.

        Returns:
            The LabJackPython device object.
        """
        model = (model or "u3").lower()
        want = str(serial) if serial else None
        with self._lock:
            key = self._resolve_open_key(model, want)
            if key is not None:
                existing = self._devices[key]
                if self._is_alive(existing):
                    self._ref_counts[key] = self._ref_counts.get(key, 0) + 1
                    _debug(f"reusing {key}, refs={self._ref_counts[key]}")
                    return existing
                _debug(f"{key} is stale; reopening")
                self._discard(key)

            mod = load_ud_module(model)
            device_cls = getattr(mod, model.upper())
            device = device_cls(autoOpen=False)
            try:
                if want:
                    device.open(firstFound=False, serial=int(want))
                else:
                    device.open(firstFound=True)
            except Exception:
                # A firstFound open that cannot claim anything, on a box where
                # this process already holds exactly one device of this model,
                # is that same device: the Exodriver claims exclusively, so
                # reopening it is a guaranteed conflict rather than a second
                # instrument. With two devices the open above would have landed
                # on the free one and we would not be here. Adopting it is what
                # keeps a serial-keyed net and a serial-less net on one U3 from
                # locking each other out.
                if want is None:
                    mine = [k for k in self._devices if k[0] == model]
                    if len(mine) == 1:
                        adopted = self._devices[mine[0]]
                        self._first_found[model] = mine[0][1]
                        self._ref_counts[mine[0]] = \
                            self._ref_counts.get(mine[0], 0) + 1
                        _debug(f"adopted already-open {mine[0]} for a "
                               f"first-found request")
                        return adopted
                raise
            # open() populates serialNumber/deviceName/isHV via configU3.
            # Read the config once here so a driver never has to.
            device.configU3()
            _debug(
                f"opened {getattr(device, 'deviceName', model)} "
                f"serial={getattr(device, 'serialNumber', '?')}"
            )
            # Key on the serial the DEVICE reports, not the one requested, so
            # a later request naming it the other way finds this same entry
            # instead of trying to claim a device we already hold.
            real = str(getattr(device, "serialNumber", "") or "") or None
            key = (model, real)
            if want is None and real is not None:
                self._first_found[model] = real
            self._devices[key] = device
            self._ref_counts[key] = 1
            return device

    @staticmethod
    def _is_alive(device: Any) -> bool:
        """Cheapest honest liveness probe the UD API offers.

        There is no ``SERIAL_NUMBER`` register to read as LJM has, so this
        round-trips a config read. False on any exception: a device that
        cannot answer its own configuration is one we should reopen.
        """
        try:
            device.configU3()
            return True
        except Exception as e:
            _debug(f"liveness probe failed: {e}")
            return False

    def _discard(self, key: Tuple[str, Optional[str]]) -> None:
        """Close and forget one entry. Caller holds the lock."""
        device = self._devices.pop(key, None)
        self._ref_counts.pop(key, None)
        if device is not None:
            # Drop the pin-mux memo first. A reopened device re-reads its masks
            # from hardware, and a stale memo would let the first read after a
            # reconnect skip a configIO it actually needs.
            serial = getattr(device, "serialNumber", None)
            for cached in [k for k in self._pin_modes if k[0] == serial]:
                self._pin_modes.pop(cached, None)
            try:
                device.close()
            except Exception as e:
                _debug(f"error closing {key}: {e}")

    def release_device(self, model: str = "u3",
                       serial: Optional[str] = None) -> None:
        """Drop one reference.

        Deliberately does NOT close at zero, matching the LJM manager: a UD
        open costs a USB enumerate plus a calibration read, and roles reopen
        constantly. The device closes on force_close/close_all or at exit.
        """
        with self._lock:
            # Same resolution as get_device: the caller releases by the name it
            # asked with, which may not be the real serial the entry is under.
            key = self._resolve_open_key((model or "u3").lower(),
                                         str(serial) if serial else None,
                                         unique_ok=True)
            if key is not None and self._ref_counts.get(key, 0) > 0:
                self._ref_counts[key] -= 1
                _debug(f"released {key}, refs={self._ref_counts[key]}")

    def force_close(self, model: Optional[str] = None,
                    serial: Optional[str] = None) -> None:
        """Close one device regardless of reference count, or all of them."""
        with self._lock:
            if model is None:
                self.close_all()
                return
            key = self._resolve_open_key((model or "u3").lower(),
                                         str(serial) if serial else None,
                                         unique_ok=True)
            if key is not None:
                self._discard(key)

    def close_all(self) -> int:
        """Close every open UD device. Returns how many were closed."""
        with self._lock:
            keys = list(self._devices)
            for key in keys:
                self._discard(key)
            _debug(f"closed {len(keys)} UD device(s)")
            return len(keys)

    # -- pin mux ---------------------------------------------------------

    def set_channel_mode(self, device: Any, dio: int, analog: bool) -> None:
        """Put one flexible line into analog or digital mode.

        Read-modify-write of the whole-device ``FIOAnalog``/``EIOAnalog``
        masks, under the manager lock so two roles configuring two different
        pins cannot lose each other's bit.

        A no-op when the bit already has the wanted value: ``configIO`` is a
        USB round trip, and every ADC read would otherwise pay for one.

        Raises:
            ValueError: for a CIO line (dedicated digital, no analog bit) when
                analog is requested, and for an attempt to make a U3-HV's
                FIO0-3 digital -- those pins are the fixed high-voltage analog
                inputs and no mask bit will change that. Failing loudly beats
                writing a mask the hardware ignores and then reading a number
                that looks fine.
        """
        if dio >= FIRST_DIGITAL_ONLY_DIO:
            if analog:
                raise ValueError(
                    f"{dio_to_pin(dio)} is a dedicated digital line on this "
                    f"device and cannot be an analog input."
                )
            return  # already digital, nothing to configure

        if dio <= 3 and getattr(device, "isHV", False):
            if not analog:
                raise ValueError(
                    f"{dio_to_pin(dio)} is a fixed high-voltage analog input "
                    f"on a {getattr(device, 'deviceName', 'U3-HV')} and cannot "
                    f"be used as digital I/O. Use FIO4-FIO7, EIO0-EIO7 or "
                    f"CIO0-CIO3."
                )
            return  # permanently analog, nothing to configure

        register = "FIOAnalog" if dio < 8 else "EIOAnalog"
        bit = 1 << (dio if dio < 8 else dio - 8)
        cache_key = (getattr(device, "serialNumber", None), dio)

        with self._lock:
            # Process-local memo of what we last set. Without it every ADC read
            # pays a configU3 round trip just to discover the bit is already
            # right -- the same cost the T7 driver's _configured set avoids.
            #
            # Every mode change on this device goes through this method, so the
            # memo cannot drift within the process. It CAN drift if another
            # process reconfigures the device, which is the same assumption the
            # T7 driver makes about sticky AIN registers; the mask write below
            # is idempotent, so the cost of being wrong is a redundant write,
            # not a wrong reading.
            if self._pin_modes.get(cache_key) == analog:
                return

            # configIO, NOT configU3. They are different commands over
            # different state: configU3 carries the power-up defaults (and the
            # identity fields isHV/serialNumber/deviceName), while configIO
            # carries the LIVE pin mux the hardware actually acts on. Writing
            # configU3 here is accepted, and reads back through configU3 as if
            # it worked, but leaves the pin in its old mode -- getAIN then
            # fails with PIN_CONFIGURED_FOR_DIGITAL (98), whose own text says
            # "Use a command like ConfigIO to set the pin to analog".
            current = int(device.configIO().get(register, 0))
            wanted = (current | bit) if analog else (current & ~bit)
            if wanted != current:
                _debug(
                    f"{register}: {current:#010b} -> {wanted:#010b} "
                    f"({dio_to_pin(dio)} -> {'analog' if analog else 'digital'})"
                )
                device.configIO(**{register: wanted})
            self._pin_modes[cache_key] = analog


# ---------------------------------------------------------------------------
# Module-level convenience functions -- mirrors labjack_handle.py's surface so
# the two managers read the same way at their call sites.
# ---------------------------------------------------------------------------

_manager: Optional[LabJackUDHandleManager] = None


def _get_manager() -> LabJackUDHandleManager:
    """Get the singleton manager instance."""
    global _manager
    if _manager is None:
        _manager = LabJackUDHandleManager()
    return _manager


def get_ud_device(model: str = "u3", serial: Optional[str] = None) -> Any:
    """Open or reuse a UD device from the global manager."""
    return _get_manager().get_device(model, serial)


def release_ud_device(model: str = "u3", serial: Optional[str] = None) -> None:
    """Release one reference to a UD device."""
    _get_manager().release_device(model, serial)


def force_close_ud(model: Optional[str] = None,
                   serial: Optional[str] = None) -> None:
    """Force close one UD device, or all of them when model is None."""
    _get_manager().force_close(model, serial)


def close_all_ud_devices() -> int:
    """Close every open UD device."""
    return _get_manager().close_all()


def set_channel_mode(device: Any, dio: int, analog: bool) -> None:
    """Set one flexible line's analog/digital mode on *device*."""
    _get_manager().set_channel_mode(device, dio, analog)


def serial_from_address(address: Optional[str]) -> Optional[str]:
    """Pull a serial number out of a scanner VISA-style address.

    The scanner writes ``USB0::0x0CD5::0x0003::<serial>::INSTR``. The serial
    slot is routinely EMPTY for a LabJack, which is why this returns None
    rather than an empty string -- None means "first found", and that is the
    honest answer for a record that never carried a serial.

    Mirrors the parsing in ``io/adc/usb202.py``; kept here rather than shared
    because that one is a constructor detail of a different driver.
    """
    if not address:
        return None
    if "::" not in address:
        return address.strip() or None
    parts = address.split("::")
    if len(parts) > 3 and parts[3].strip():
        return parts[3].strip()
    return None


def _cleanup_on_exit() -> None:
    """Close all UD devices when the process exits."""
    global _manager
    if _manager is not None:
        try:
            _debug("process exiting - closing all UD devices")
            _manager.close_all()
        except Exception as e:
            _debug(f"error during exit cleanup: {e}")


atexit.register(_cleanup_on_exit)


__all__ = [
    'LabJackUDHandleManager',
    'SUPPORTED_MODELS',
    'MAX_DIO',
    'load_ud_module',
    'pin_to_dio',
    'dio_to_pin',
    'get_ud_device',
    'release_ud_device',
    'force_close_ud',
    'close_all_ud_devices',
    'set_channel_mode',
    'serial_from_address',
]

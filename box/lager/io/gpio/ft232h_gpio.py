# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
FTDI FT232H GPIO driver implementing the abstract GPIOBase interface.

Provides digital I/O operations for the FT232H USB-to-MPSSE adapter
(VID:PID 0403:6014) using the pyftdi library's GpioAsyncController.

Pin naming follows the FT232H convention:
- ADBUS pins: AD0-AD7 (numbers 0-7)
- ACBUS pins: AC0-AC7 (numbers 8-15)

NOTE: The FT232H's GPIO mode claims the entire FTDI interface.
It cannot be shared simultaneously with I2C or SPI on the same device.
Use a separate FT232H or run commands sequentially.

Output levels outlive the process that set them. Stock pyftdi resets the
channel to UART mode on every open and every close, which let every pin float
HIGH at the end of each ``lager python`` run and whenever hardware_service
handed its claims over to a script. This driver therefore:

* never sends an SIO reset or a bit-mode reset (``_HoldSafeFtdi``);
* on close, releases the USB interface and nothing else -- the channel stays
  in bit-bang mode with its latch and direction intact, and ``ftdi_sio`` is
  not re-attached, so the channel's tty stays gone until the part is
  re-plugged;
* marks a channel it has set up by its latency timer (``HOLD_LATENCY``), so
  the next open can tell "still driven" from "power-cycled" without trusting
  the cache;
* reads output pins from the pin register, not the cache;
* shares one controller per channel per process, so two nets on one channel
  share one direction mask;
* keeps the output cache under /etc/lager, which survives a box reboot.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
import threading
import time
from typing import Optional, Tuple

from .gpio_net import GPIOBase
from lager.exceptions import GPIOBackendError
from lager.util import ftdi_url, ftdi_usb

DEBUG = bool(os.environ.get("LAGER_GPIO_DEBUG"))


def _debug(msg: str) -> None:
    """Debug logging when LAGER_GPIO_DEBUG environment variable is set."""
    if DEBUG:
        sys.stderr.write(f"GPIO_DEBUG(FT232H): {msg}\n")
        sys.stderr.flush()


# Maximum retries for USB disconnect recovery
MAX_USB_RETRIES = 3

# Named pin mapping: AD0-AD7 -> 0-7, AC0-AC7 -> 8-15
_PIN_NAMES = {}
for _i in range(8):
    _PIN_NAMES[f"AD{_i}"] = _i
    _PIN_NAMES[f"AC{_i}"] = _i + 8

# Latency-timer value that marks a channel this driver has set up. The part
# powers up at 16, and pyftdi itself only ever writes 1, 16 or 255, so
# finding 77 at open means the channel is still in bit-bang mode with the
# latch and direction the last process left. It is written only after the
# outputs are in place, so a process killed mid-restore leaves 16 and the
# next open restores again.
HOLD_LATENCY = 77

# Timeout for the one control transfer made before pyftdi opens a channel.
_PROBE_TIMEOUT_MS = 1000

_hold_safe_ftdi_cls = None


def _hold_safe_ftdi():
    """Build (once) and return the pyftdi ``Ftdi`` subclass the driver uses.

    Defined lazily so this module imports without pyftdi, as the unit tests
    and the dispatcher's import path both need.
    """
    global _hold_safe_ftdi_cls
    if _hold_safe_ftdi_cls is not None:
        return _hold_safe_ftdi_cls

    from pyftdi.ftdi import Ftdi
    from pyftdi.usbtools import UsbTools
    import usb.util

    class _HoldSafeFtdi(Ftdi):
        """``Ftdi`` that never resets a channel and closes by releasing it.

        pyftdi's ``open_from_device`` sends an SIO reset and a bit-mode
        reset, and its ``close`` resets the bit mode and re-attaches
        ``ftdi_sio``. Each one drops the channel back to UART mode, where
        every pin idles HIGH.
        """

        def _reset_device(self):
            _debug("skipping SIO reset on open")

        def set_bitmode(self, bitmask, mode):
            if mode == Ftdi.BitMode.RESET:
                _debug("skipping bit-mode reset")
                return
            super().set_bitmode(bitmask, mode)

        def close(self, freeze: bool = False) -> None:
            """Release the USB interface; leave the channel as it is."""
            dev = self._usb_dev
            if not dev:
                return
            try:
                if self._is_pyusb_handle_active():
                    usb.util.release_interface(dev, self._index - 1)
            except Exception as exc:  # device gone: nothing left to release
                _debug(f"release_interface failed (device gone?): {exc}")
            finally:
                self._usb_dev = None
                UsbTools.release_device(dev)
            _debug("released USB interface; channel left in bit-bang mode, "
                   "ftdi_sio not re-attached")

    _hold_safe_ftdi_cls = _HoldSafeFtdi
    return _hold_safe_ftdi_cls


def _probe_channel(url: str) -> Tuple[Optional[int], Optional[str]]:
    """Read a channel's latency timer and the part's serial, changing nothing.

    Runs before pyftdi opens the channel, so the open path can depend on
    what it finds. A vendor control request on endpoint 0 needs no claim on
    the interface. Returns ``(None, None)`` on any failure; the open that
    follows reports the real error.
    """
    try:
        from pyftdi.ftdi import Ftdi
        from pyftdi.usbtools import UsbTools
    except ImportError:
        return None, None
    try:
        devdesc, interface = Ftdi.get_identifiers(url)
        dev = UsbTools.get_device(devdesc)
    except Exception as exc:
        _debug(f"probe: could not find {url}: {exc}")
        return None, None
    latency = serial = None
    try:
        try:
            serial = dev.serial_number
        except Exception:
            pass
        data = dev.ctrl_transfer(Ftdi.REQ_IN, Ftdi.SIO_REQ_GET_LATENCY_TIMER,
                                 0, max(int(interface), 1), 1,
                                 _PROBE_TIMEOUT_MS)
        if data:
            latency = int(data[0])
    except Exception as exc:
        _debug(f"probe: latency read failed on {url}: {exc}")
    finally:
        UsbTools.release_device(dev)
    return latency, serial


class FT232HGPIO(GPIOBase):
    """
    FTDI FT232H GPIO implementation using pyftdi GpioAsyncController.

    Provides GPIO operations on ADBUS (AD0-AD7) pins.

    Pin naming:
    - Integer: 0-7 (AD0-AD7), 8-15 (AC0-AC7)
    - String numeric: "4", "12"
    - String named: "AD0"-"AD7", "AC0"-"AC7"
    """

    # Callers can test for this to tell a driver that keeps output levels
    # across processes from the one that reset the channel on every open.
    HOLD_SAFE = True

    # File-based cache of output states, read when a channel has to be set
    # up again (power cycle, re-plug). /etc/lager is bind-mounted from the
    # host, so it survives a box reboot as well as a container restart.
    _CACHE_FILE = "/etc/lager/ftdi_gpio_cache.json"
    # Where the cache lived before; read only while the new file is missing.
    _LEGACY_CACHE_FILE = "/tmp/ft232h_gpio_cache.json"

    # One controller per channel (pyftdi URL) per process. Each
    # GpioAsyncController keeps its own direction mask, so two nets on one
    # channel with a controller each would each write a mask missing the
    # other's pin, and turn that pin back into an input.
    _controllers: dict = {}
    _atexit_urls: set = set()
    _lock = threading.RLock()

    def __init__(
        self,
        name: str,
        pin: int | str,
        serial: Optional[str] = None,
        pid: Optional[str] = None,
        interface=None,
        url: Optional[str] = None,
    ) -> None:
        """
        Initialize FTDI GPIO interface.

        Args:
            name: Human-readable name for this GPIO net
            pin: Pin identifier (0-15, "AD0"-"AD7", "AC0"-"AC7"). Bits 8-15
                are ACBUS and exist on the FT232H/FT2232H only.
            serial: USB serial number string (for multi-device setups).
                    If None, uses the first available matching device.
            pid: USB PID from the net's VISA address, selecting the part
                (6014 FT232H / 6010 FT2232H / 6011 FT4232H). Defaults to the
                FT232H, so a net that never carried one is unchanged.
            interface: Which MPSSE channel to drive -- "A"-"D" or 0-3. None
                means A, the only choice on a single-channel part.
            url: A complete pyftdi URL, used verbatim. The escape hatch for
                wiring we cannot describe from a net record.
        """
        super().__init__(name, pin)
        self._serial = serial
        self._url = url
        self._product = ftdi_url.product_for_pid(pid)
        self._interface = ftdi_url.parse_interface(interface)
        ftdi_url.validate_interface(self._product, self._interface)
        self._pin_num = self._parse_pin(pin)
        self._check_pin_width()

        _debug(f"FT232HGPIO initialized: name={name}, pin={pin} -> bit {self._pin_num}, "
               f"serial={serial}, product={self._product}, "
               f"interface={self._interface}")

    def _check_pin_width(self) -> None:
        """Refuse an ACBUS pin on a part whose channels are 8 bits wide.

        The FT4232H has no ACBUS: bits 8-15 are not "unused there", they do
        not exist. Configuring one would set a direction bit the device
        silently drops, and the net would read back whatever floats.
        """
        width = ftdi_url.pin_width(self._product)
        if self._pin_num >= width:
            raise GPIOBackendError(
                f"FTDI {self._product} channels are {width} bits wide; pin "
                f"'{self._pin_num}' does not exist. ACBUS pins (8-15) are "
                f"available on the FT232H and FT2232H only."
            )

    @staticmethod
    def _parse_pin(pin: int | str) -> int:
        """
        Parse a pin identifier to a bit number (0-15).

        Args:
            pin: Integer 0-15, string "0"-"15", or named "AD0"-"AD7"/"AC0"-"AC7"

        Returns:
            Bit number (0-15)

        Raises:
            GPIOBackendError: If pin identifier is invalid
        """
        # Integer
        if isinstance(pin, int):
            if 0 <= pin <= 15:
                return pin
            raise GPIOBackendError(
                f"Invalid FT232H pin number {pin}. Valid range: 0-15 "
                f"(0-7 for AD0-AD7, 8-15 for AC0-AC7)."
            )

        # String
        if isinstance(pin, str):
            pin_stripped = pin.strip()

            # Try numeric string
            try:
                num = int(pin_stripped)
                if 0 <= num <= 15:
                    return num
                raise GPIOBackendError(
                    f"Invalid FT232H pin number {num}. Valid range: 0-15 "
                    f"(0-7 for AD0-AD7, 8-15 for AC0-AC7)."
                )
            except ValueError:
                pass

            # Try named pin (AD0-AD7, AC0-AC7)
            upper = pin_stripped.upper()
            if upper in _PIN_NAMES:
                return _PIN_NAMES[upper]

            raise GPIOBackendError(
                f"Invalid FT232H pin '{pin}'. Use 0-15, 'AD0'-'AD7', or 'AC0'-'AC7'."
            )

        raise GPIOBackendError(
            f"Invalid FT232H pin type: {type(pin).__name__}. Expected int or str."
        )

    @staticmethod
    def _get_pyftdi():
        """Lazy-import the pyftdi GPIO module."""
        try:
            from pyftdi.gpio import GpioAsyncController
            return GpioAsyncController
        except ImportError:
            raise GPIOBackendError(
                "pyftdi library not available. "
                "Install with: pip install pyftdi"
            )

    def _build_url(self) -> str:
        """Build the pyftdi device URL."""
        if self._url:
            return self._url
        return ftdi_url.build_ftdi_url(
            serial=self._serial,
            product=self._product,
            interface=self._interface,
        )

    def _ensure_open(self):
        """
        Return this channel's controller, opening the channel if needed.

        The controller is shared by every driver on the same channel in this
        process (see ``_controllers``). Opening never resets the channel:

        * latency timer at ``HOLD_LATENCY``: the channel is still as the last
          process left it, so open with the cached direction mask, which
          rewrites neither the latch nor any pin;
        * anything else (power-up, re-plug, another driver used it): open
          with every pin an input, write the cached output values to the
          latch, then turn the cached outputs on.

        Either way the latency timer is set to ``HOLD_LATENCY`` last.

        If the part was re-plugged while this process held a stale entry in
        pyftdi's device table, the whole open -- probe included, since the
        probe's lookup was stale too -- runs once more after a flush (see
        ``lager.util.ftdi_usb``).
        """
        url = self._build_url()
        cls = type(self)
        with cls._lock:
            ctrl = cls._controllers.get(url)
            if ctrl is not None:
                return ctrl
            ctrl = ftdi_usb.open_fresh(lambda: self._open_channel(url),
                                       log=_debug)
            cls._controllers[url] = ctrl
            if url not in cls._atexit_urls:
                cls._atexit_urls.add(url)
                atexit.register(cls._release_channel, url)
            return ctrl

    def _open_channel(self, url: str):
        """Open ``url`` without resetting it (see ``_ensure_open``)."""
        GpioAsyncController = self._get_pyftdi()
        _debug(f"Opening FT232H GPIO at URL: {url}")

        latency, probed_serial = self._probe_channel(url)
        # The cache is keyed by serial. Learn it before reading the cache,
        # even when the net record carried none.
        if not self._serial and probed_serial:
            self._serial = probed_serial
            _debug(f"Detected FT232H serial: {self._serial}")

        direction, values = self._cached_outputs()
        held = latency == HOLD_LATENCY
        _debug(f"channel state: latency={latency} -> "
               f"{'still held' if held else 'needs setup'}; cached "
               f"direction=0x{direction:04x} value=0x{values:04x}")

        try:
            ctrl = GpioAsyncController()
            ctrl._ftdi = self._make_ftdi()
            ctrl.configure(url, direction=direction if held else 0)
        except Exception as exc:
            raise GPIOBackendError(
                f"Failed to open FT232H GPIO at {url}: {exc}"
            ) from exc

        if not self._serial:
            try:
                self._serial = ctrl._ftdi.usb_dev.serial_number
                _debug(f"Detected FT232H serial: {self._serial}")
                direction, values = self._cached_outputs()
            except Exception:
                _debug("Could not detect FT232H serial, using default")

        if held:
            _debug(f"kept direction 0x{direction:04x}; latch untouched")
        else:
            self._restore_cached_outputs(ctrl, direction, values)

        try:
            ctrl._ftdi.set_latency_timer(HOLD_LATENCY)
        except Exception as exc:
            _debug(f"Warning: could not mark channel as set up: {exc}")
        return ctrl

    # Seams for the unit tests, which run without pyftdi or hardware.
    @staticmethod
    def _probe_channel(url: str) -> Tuple[Optional[int], Optional[str]]:
        return _probe_channel(url)

    @staticmethod
    def _make_ftdi():
        return _hold_safe_ftdi()()

    def _cached_outputs(self) -> Tuple[int, int]:
        """Return ``(direction_mask, output_value)`` cached for this channel.

        Filtered by ``serial:channel:``, not by serial alone: on a
        multi-channel part pin N of channel C and pin N of channel D are
        different pins and must not overwrite each other.
        """
        prefix = self._get_cache_key().rsplit(":", 1)[0] + ":"
        direction_mask = 0
        output_value = 0
        for key, state in self._load_cache().items():
            if not key.startswith(prefix):
                continue
            if not isinstance(state, dict) or not state.get("is_output"):
                continue
            try:
                pin_num = int(key[len(prefix):])
            except ValueError:
                continue
            if not (0 <= pin_num <= 15):
                continue
            pin_mask = 1 << pin_num
            direction_mask |= pin_mask
            if state.get("value"):
                output_value |= pin_mask
        return direction_mask, output_value

    def _restore_cached_outputs(self, ctrl, direction_mask: int,
                                output_value: int) -> None:
        """Re-apply the cached outputs to a channel that has just been set up.

        Writes the output values BEFORE enabling pins as outputs. After a
        power cycle the latch is undefined (often 0xFF / all-HIGH); setting
        the direction first would drive the stale latch until write()
        corrected it.
        """
        if not direction_mask:
            _debug("no cached outputs for this channel; all pins inputs")
            return
        try:
            ctrl.write(output_value)
            ctrl.set_direction(direction_mask, direction_mask)
            _debug(f"Restored cached outputs: direction=0x{direction_mask:04x}, "
                   f"value=0x{output_value:04x}")
        except Exception as e:
            _debug(f"Warning: failed to restore cached outputs: {e}")

    @classmethod
    def _release_channel(cls, url: str) -> None:
        """Release one channel's USB interface, leaving its pins driven."""
        with cls._lock:
            ctrl = cls._controllers.pop(url, None)
        if ctrl is None:
            return
        try:
            ctrl.close()
            _debug(f"released {url}")
        except Exception as e:
            _debug(f"Error releasing {url}: {e}")

    @classmethod
    def release_all_channels(cls) -> int:
        """Release every channel this process holds; return how many.

        For hardware_service before a ``lager python`` run. Draining the
        dispatcher's driver cache is not enough on its own: the controllers
        are shared and outlive any one driver object, so a driver evicted
        without ``_close()`` would otherwise keep its channel claimed.
        """
        with cls._lock:
            urls = list(cls._controllers)
        for url in urls:
            cls._release_channel(url)
        return len(urls)

    def _close(self):
        """Release this net's channel so another process can open it.

        hardware_service calls this before a ``lager python`` run. It
        releases the shared controller, so every other net on the channel
        reopens on its next call -- without a reset, so nothing moves.
        """
        self._release_channel(self._build_url())

    def _reconnect(self):
        """Release and reopen the channel for USB disconnect recovery."""
        _debug("Attempting reconnect after USB error...")
        self._release_channel(self._build_url())
        self._ensure_open()

    def _parse_level(self, level: int | str) -> int:
        """
        Parse level input to 0 or 1.

        Args:
            level: String or integer level specification

        Returns:
            0 for LOW, 1 for HIGH
        """
        if isinstance(level, str):
            level_str = level.strip().lower()
            return 1 if level_str in ("1", "on", "high", "true") else 0
        return 1 if int(level) else 0

    # ---------- file-based state cache ----------

    def _get_cache_key(self) -> str:
        """Get cache key for this pin.

        Keyed by interface as well as device: AD0 on channel A and AD0 on
        channel B of one FT4232H are different pins, and without the channel
        in the key they would share a cached state and clobber each other
        between CLI invocations.
        """
        device_id = self._serial or "default_ft232h"
        iface = 'A' if self._interface is None else 'ABCD'[self._interface]
        return f"{device_id}:{iface}:{self._pin_num}"

    def _load_cache(self) -> dict:
        """Load the cache, falling back to the pre-/etc/lager location.

        The legacy /tmp file is read only while the persistent one does not
        exist yet; the first save writes everything to the new path, so the
        migration happens once.
        """
        for path in (self._CACHE_FILE, self._LEGACY_CACHE_FILE):
            if not path:
                continue
            try:
                with open(path, "r") as f:
                    data = json.load(f)
            except FileNotFoundError:
                continue
            except (OSError, json.JSONDecodeError) as e:
                _debug(f"Warning: unreadable cache {path}: {e}")
                return {}
            if path != self._CACHE_FILE:
                _debug(f"migrating GPIO cache from {path}")
            return data if isinstance(data, dict) else {}
        return {}

    def _save_cache(self, cache: dict) -> None:
        """Save the cache atomically.

        It now outlives reboots, so a write torn by a power cut must not
        leave a file that parses as nothing and silently loses every level.
        """
        tmp = f"{self._CACHE_FILE}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(cache, f)
            os.replace(tmp, self._CACHE_FILE)
        except Exception as e:
            _debug(f"Warning: Failed to save cache: {e}")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _get_cached_state(self) -> dict:
        """Get cached state for this pin."""
        cache = self._load_cache()
        key = self._get_cache_key()
        return cache.get(key, {"value": None, "is_output": False})

    def _set_cached_state(self, value: int, is_output: bool) -> None:
        """Update cached state for this pin."""
        cache = self._load_cache()
        key = self._get_cache_key()
        cache[key] = {"value": value, "is_output": is_output}
        self._save_cache(cache)

    # ---------- GPIO operations ----------

    def input(self) -> int:
        """
        Read the current state of the GPIO pin from the pin register.

        An output pin is read where it is, without changing its direction,
        so reading back an output neither releases it nor reports a level
        the pin no longer has. Any other pin is made an input first.

        Returns:
            0 for LOW, 1 for HIGH

        Raises:
            GPIOBackendError: If pyftdi is not available or device error
        """
        pin_mask = 1 << self._pin_num
        last_error = None

        for attempt in range(MAX_USB_RETRIES):
            try:
                with self._lock:
                    ctrl = self._ensure_open()
                    # The open restored the cached direction mask, so this
                    # is "cached as an output" without a second cache read.
                    is_output = bool(ctrl.direction & pin_mask)
                    if not is_output:
                        ctrl.set_direction(pin_mask, 0)
                    pins = ctrl.read()
                value = 1 if (pins & pin_mask) else 0

                _debug(f"Pin {self._pin_num} input "
                       f"({'output' if is_output else 'input'} pin): "
                       f"raw=0x{pins:04x}, value={value}")
                if not is_output:
                    self._set_cached_state(value, is_output=False)
                return value

            except GPIOBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on input attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise GPIOBackendError(
                    f"FT232H GPIO input on pin {self._pin_num} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    def output(self, level: int | str) -> None:
        """
        Set the output state of the GPIO pin.

        Uses read-modify-write to avoid clobbering other pins' output state.

        Args:
            level: Output level - accepts int (0/1) or str ("low"/"high", "off"/"on", "0"/"1")

        Raises:
            GPIOBackendError: If pyftdi is not available or device error
        """
        bit_value = self._parse_level(level)
        pin_mask = 1 << self._pin_num
        last_error = None

        for attempt in range(MAX_USB_RETRIES):
            try:
                # Locked: two nets on one channel share this controller, and
                # their read-modify-writes must not interleave.
                with self._lock:
                    ctrl = self._ensure_open()

                    # Read-modify-write: set the value in the output latch
                    # BEFORE enabling the pin as output to avoid a brief
                    # glitch where the pin drives a stale latch value (often
                    # 0xFF after a power cycle).
                    current_pins = ctrl.read()
                    if bit_value:
                        new_pins = current_pins | pin_mask
                    else:
                        new_pins = current_pins & ~pin_mask

                    ctrl.write(new_pins)
                    ctrl.set_direction(pin_mask, pin_mask)

                _debug(f"Pin {self._pin_num} output: {bit_value} "
                       f"(pins 0x{current_pins:04x} -> 0x{new_pins:04x})")
                self._set_cached_state(bit_value, is_output=True)
                return

            except GPIOBackendError:
                raise
            except Exception as e:
                last_error = e
                _debug(f"USB error on output attempt {attempt + 1}/{MAX_USB_RETRIES}: {e}")
                if attempt < MAX_USB_RETRIES - 1:
                    time.sleep(1.0 * (2 ** attempt))
                    self._reconnect()
                    continue
                raise GPIOBackendError(
                    f"FT232H GPIO output on pin {self._pin_num} failed after "
                    f"{MAX_USB_RETRIES} attempts: {last_error}"
                )

    # No __del__: a net object going away must not release a channel other
    # nets in this process still use. The shared controller is released by
    # _close() (hardware_service, before a script) or by the atexit hook.

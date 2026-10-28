# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
GPIO pins 1-3 of a Yepkit YKUSH3 hub.

pykush has no GPIO API, so this sends the hub's own commands (from Yepkit's
ykushcmd, src/ykush3/ykush3.cpp) over the HID channel pykush opens:

    0x30 <pin>          read   -> reply 01 30 <pin> <level>
    0x31 <pin> <0|1>    write  -> reply 01 31 <pin> <level>

Every operation goes through ``YKUSHUSBNet._with_device``: a fresh handle,
under the hub's cross-process lock, so GPIO serialises with the hub's usb
nets.

Never sends 0x32 01 (``ykushcmd ykush3 --gpio enable``). Despite the name,
that hands the USB ports to the GPIO pins as inputs: the hub then ignores the
host's port commands while acknowledging them.

Not yet verified on hardware: the pins' voltage level, and whether a written
level is held. On a bench with nothing wired to the pins, a pin written high
read back 1 for about 100 ms and 0 by 0.5 s.
"""

from __future__ import annotations

from lager.automation.usb_hub import ykush as _ykush
from lager.exceptions import GPIOBackendError

from .gpio_net import GPIOBase

_READ = 0x30
_WRITE = 0x31
_OK = 0x01
PINS = (1, 2, 3)


def _parse_level(level) -> int:
    if isinstance(level, str):
        word = level.strip().lower()
        if word in ("1", "high", "on", "true"):
            return 1
        if word in ("0", "low", "off", "false"):
            return 0
        raise GPIOBackendError(f"Invalid GPIO level {level!r}: use 0/1, low/high or off/on")
    return 1 if int(level) else 0


def _hex(packet) -> str:
    return " ".join("%02x" % b for b in packet)


class YKUSH3GPIO(GPIOBase):
    """One of the YKUSH3's three GPIO pins."""

    def __init__(self, name: str, pin: int | str, address: str | None = None) -> None:
        try:
            number = int(pin)
        except (TypeError, ValueError):
            number = None
        if number not in PINS:
            raise GPIOBackendError(f"YKUSH3 GPIO pin must be 1, 2 or 3, got {pin!r}")
        super().__init__(name, number)
        self.address = address
        self._hub = _ykush.YKUSHUSBNet({"address": address})

    def _command(self, packet, what):
        """Send ``packet`` and return the reply, raising unless the hub
        acknowledged this command for this pin."""
        def _do(dev):
            return list(dev._raw_sendreceive(list(packet)))[:4]

        reply = self._hub._with_device(_do, what=what)
        if len(reply) < 4 or reply[0] != _OK or reply[1] != packet[0] or reply[2] != packet[1]:
            raise GPIOBackendError(
                f"YKUSH3 did not acknowledge GPIO {what} on pin {self.pin}: "
                f"sent {_hex(packet)}, got {_hex(reply)}")
        return reply

    def input(self) -> int:
        reply = self._command([_READ, self.pin], "read")
        return 1 if reply[3] else 0

    def output(self, level: int | str) -> None:
        value = _parse_level(level)
        self._command([_WRITE, self.pin, value], "write")

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Build a pyftdi device URL for an FTDI net.

The GPIO/I2C/SPI drivers all hardcoded ``ftdi://ftdi:232h[:serial]/1``, which
pins two things that are not actually fixed:

* **The product.** ``232h`` selects the FT232H (PID 0x6014). It does not match
  an FT2232H (0x6010) or FT4232H (0x6011), so those chips could not be opened
  at all -- even though ``INSTRUMENT_NET_MAP`` has advertised ``spi``/``i2c``/
  ``gpio`` on ``FTDI_FT2232H`` the whole time.
* **The interface.** ``/1`` is interface A. A multi-channel FTDI wired with,
  say, comms on A and control on B had no way to say so.

Both are recoverable from the net record: its ``address`` is a VISA string
(``USB0::0x0403::0x6011::<serial>::INSTR``) carrying the PID, and the interface
comes from ``params.interface``.

**Interface numbering is not the same on both sides of lager.** pyftdi
interfaces are 1-based (``/1`` is A); OpenOCD's ``ftdi channel <N>`` is 0-based
(``0`` is A). Everything here speaks the 0-based index, matching
``probes.parse_device_field``, and :func:`build_ftdi_url` adds the +1 at the
last moment. Get that wrong and the pins driven are silently the wrong ones.

``_CHANNEL_LETTER_TO_INDEX`` is deliberately duplicated from
``lager.debug.probes``: that module is import-standalone (it pulls in nothing
but ``re``, which several tests rely on), and importing it here would drag in
the ``lager.debug`` package __init__ -- and with it pygdbmi -- on the GPIO
path, which has no business requiring a debug stack.
``test/unit/box/test_ftdi_url.py`` asserts the two maps agree.
"""

from __future__ import annotations

from typing import Optional

# Keep in lockstep with ``_CHANNEL_LETTER_TO_INDEX`` in lager/debug/probes.py.
_CHANNEL_LETTER_TO_INDEX = {'A': 0, 'B': 1, 'C': 2, 'D': 3}

# USB PID -> the pyftdi product token that selects it. Only chips lager can
# actually drive as GPIO/I2C/SPI are listed; an unknown PID falls back to the
# historical default so existing nets are unaffected.
_PID_TO_PRODUCT = {
    '6014': '232h',    # FT232H  -- 1 channel
    '6010': '2232h',   # FT2232H -- 2 channels
    '6011': '4232h',   # FT4232H -- 4 channels
}

DEFAULT_PRODUCT = '232h'

# Total channels each part exposes.
_PRODUCT_CHANNELS = {'232h': 1, '2232h': 2, '4232h': 4}

# Channels with an MPSSE engine, which is a SMALLER set on the FT4232H: only
# A and B have one; C and D are UART/FIFO only (the same fact
# ``usb_scanner.SUPPORTED_USB`` records when it advertises ``debug`` on that
# part). I2C and SPI are MPSSE protocols, so they are limited to this set.
# GPIO is not -- pyftdi's GpioAsyncController uses asynchronous bitbang, which
# every channel supports -- so GPIO validates against _PRODUCT_CHANNELS.
_PRODUCT_MPSSE_CHANNELS = {'232h': 1, '2232h': 2, '4232h': 2}

# Usable GPIO bit width per channel. The FT232H and FT2232H expose ADBUS0-7
# plus ACBUS0-7 (bits 8-15); the FT4232H's channels are 8 pins wide with no
# ACBUS at all, so bits 8-15 do not exist there and must be refused rather
# than written into thin air.
_PRODUCT_PIN_WIDTH = {'232h': 16, '2232h': 16, '4232h': 8}


class FtdiUrlError(ValueError):
    """An interface or pin that the addressed FTDI part does not have."""


def product_for_pid(pid: Optional[str]) -> str:
    """pyftdi product token for a USB PID, or the historical default.

    Accepts the forms a net record actually carries -- ``'6011'`` and
    ``'0x6011'``, as split out of the VISA address -- plus the ``0x6011``
    integer literal, which is read as **hex**. USB PIDs are written in hex
    universally, so a bare int is far likelier to be ``0x6011`` (24593) than
    a decimal 6011; the alternative reading would silently classify the
    literal as unknown.

    An unrecognised PID returns the default rather than raising: a net whose
    address we cannot classify keeps behaving exactly as it did before this
    function existed.
    """
    if pid is None:
        return DEFAULT_PRODUCT
    if isinstance(pid, int) and not isinstance(pid, bool):
        text = format(pid, '04x')
    else:
        text = str(pid).strip().lower()
        if text.startswith('0x'):
            text = text[2:]
    return _PID_TO_PRODUCT.get(text.zfill(4), DEFAULT_PRODUCT)


def parse_interface(value) -> Optional[int]:
    """Normalise an interface selector to a 0-based index, or None.

    Accepts ``'A'``-``'D'`` (case-insensitive), ``'@B'``, ``0``-``3``, and
    their string forms — the same vocabulary ``probes.parse_device_field``
    accepts on the debug path, so a user does not have to learn a second one.

    Returns None for None/empty, meaning "the driver's default" (interface A).
    Anything present but unrecognised raises: a typo'd interface would
    otherwise silently drive channel A's pins.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; not an interface
        raise FtdiUrlError(f"Invalid FTDI interface: {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 3:
            return value
        raise FtdiUrlError(
            f"FTDI interface index {value} out of range; expected 0-3 (A-D)."
        )
    text = str(value).strip().lstrip('@')
    if not text:
        return None
    upper = text.upper()
    if upper in _CHANNEL_LETTER_TO_INDEX:
        return _CHANNEL_LETTER_TO_INDEX[upper]
    if text.isdigit():
        idx = int(text)
        if 0 <= idx <= 3:
            return idx
    raise FtdiUrlError(
        f"Invalid FTDI interface {value!r}; expected A-D or 0-3."
    )


def channel_count(product: str) -> int:
    """How many channels *product* exposes in total."""
    return _PRODUCT_CHANNELS.get(product, 1)


def mpsse_channel_count(product: str) -> int:
    """How many of *product*'s channels have an MPSSE engine.

    Differs from :func:`channel_count` on the FT4232H, whose C and D channels
    are UART/FIFO only.
    """
    return _PRODUCT_MPSSE_CHANNELS.get(product, 1)


def pin_width(product: str) -> int:
    """Usable GPIO bit width per channel for *product*."""
    return _PRODUCT_PIN_WIDTH.get(product, 16)


def validate_interface(product: str, interface: Optional[int], *,
                       require_mpsse: bool = False) -> None:
    """Raise if *product* has no such interface.

    Set *require_mpsse* for I2C and SPI: those are MPSSE protocols, and on an
    FT4232H only channels A and B have an MPSSE engine. Without this an SPI
    net on channel C would be accepted and then fail deep inside pyftdi with
    a message that says nothing about which channel is at fault.
    """
    if interface is None:
        return
    count = channel_count(product)
    if interface >= count:
        letter = 'ABCD'[interface]
        raise FtdiUrlError(
            f"FTDI {product} has {count} channel(s); interface "
            f"{letter} ({interface}) does not exist."
        )
    if require_mpsse:
        mpsse = mpsse_channel_count(product)
        if interface >= mpsse:
            letter = 'ABCD'[interface]
            usable = ', '.join('ABCD'[i] for i in range(mpsse))
            raise FtdiUrlError(
                f"FTDI {product} interface {letter} has no MPSSE engine, so "
                f"it cannot run I2C or SPI. MPSSE channels: {usable}. "
                f"(GPIO uses asynchronous bitbang and works on {letter}.)"
            )


def build_ftdi_url(serial: Optional[str] = None,
                   pid: Optional[str] = None,
                   interface: Optional[int] = None,
                   product: Optional[str] = None) -> str:
    """Compose the pyftdi URL for one FTDI channel.

    Args:
        serial: USB serial, or None to take the first matching device.
        pid: USB PID from the net's VISA address; ignored when *product* is
            given explicitly.
        interface: 0-based channel index, or None for A.
        product: pyftdi product token, overriding what *pid* resolves to.

    Returns:
        e.g. ``ftdi://ftdi:4232h:FT123/3`` — note the ``/3``: interface C,
        0-based here, 1-based in the URL.
    """
    product = product or product_for_pid(pid)
    validate_interface(product, interface)
    iface = 1 if interface is None else interface + 1
    if serial:
        return f"ftdi://ftdi:{product}:{serial}/{iface}"
    return f"ftdi://ftdi:{product}/{iface}"

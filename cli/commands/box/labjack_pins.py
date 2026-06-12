# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Shared LabJack T7 DIO pin helpers for i2c/spi net creation.

The LabJack T7 can run its built-in I2C/SPI masters on any DIO pin:
FIO0-7 = DIO 0-7, EIO0-7 = 8-15, CIO0-3 = 16-19, MIO0-2 = 20-22.

Used by both ``lager nets add`` (cli/commands/box/nets.py) and the
Net-Manager TUI pin-picker dialog (cli/commands/box/net_tui.py). Lives in
its own module because nets.py imports net_tui at module scope, so the TUI
cannot import back from nets.py without a cycle.
"""
from __future__ import annotations

import re
from typing import Optional

PIN_PREFIXES = (
    # (prefix, dio offset, count)
    ("FIO", 0, 8),
    ("EIO", 8, 8),
    ("CIO", 16, 4),
    ("MIO", 20, 3),
)
MAX_DIO = 22

#: All 23 DIO pin names in DIO-number order.
ALL_PIN_NAMES = [
    f"{prefix}{i}" for prefix, _offset, count in PIN_PREFIXES for i in range(count)
]

#: Sentinel for "no CS pin" (3-pin SPI with user-managed chip select).
NO_CS = "none"

# Signal display order per role, the params keys the box dispatchers read,
# and the historical default pin mapping (what the hardcoded FIO4-FIO5 /
# FIO0-FIO3 channel strings decode to).
I2C_SIGNALS = ("SDA", "SCL")
SPI_SIGNALS = ("CS", "SCK", "MOSI", "MISO")
PARAM_KEYS = {
    "SDA": "sda_pin", "SCL": "scl_pin",
    "CS": "cs_pin", "SCK": "clk_pin", "MOSI": "mosi_pin", "MISO": "miso_pin",
}
I2C_DEFAULT_PINS = {"SDA": "FIO4", "SCL": "FIO5"}
SPI_DEFAULT_PINS = {"CS": "FIO0", "SCK": "FIO1", "MOSI": "FIO2", "MISO": "FIO3"}


def pin_name(dio: int) -> str:
    """Convert a DIO number to its canonical LabJack pin name."""
    for prefix, offset, count in PIN_PREFIXES:
        if offset <= dio < offset + count:
            return f"{prefix}{dio - offset}"
    return f"DIO{dio}"


def try_parse_pin(value) -> Optional[int]:
    """Convert a pin name (FIO4/EIO0/CIO1/MIO2, case-insensitive) or DIO
    number to a DIO int. Returns None when *value* is not a valid pin."""
    text = str(value).strip().upper()
    for prefix, offset, count in PIN_PREFIXES:
        if text.startswith(prefix):
            try:
                idx = int(text[len(prefix):])
            except ValueError:
                return None
            return offset + idx if 0 <= idx < count else None
    try:
        dio = int(text)
    except ValueError:
        return None
    return dio if 0 <= dio <= MAX_DIO else None


_LABELED_TOKEN_RE = re.compile(
    r"(?:SDA|SCL|CS|SCK|MOSI|MISO):([A-Z]+\d+)", re.IGNORECASE
)
_I2C_RANGE_RE = re.compile(r"FIO(\d+)-FIO(\d+)", re.IGNORECASE)


def claimed_pins_from_chan(role: str, chan: str) -> list[str]:
    """Best-effort list of DIO pin names a saved LabJack net's channel
    string claims. Covers gpio pins, the default spi/i2c pin strings, and
    the labeled custom-pin summaries (``SDA:EIO0 SCL:EIO1``)."""
    chan = str(chan or "").strip()
    if not chan:
        return []

    if role == "gpio":
        dio = try_parse_pin(chan)
        return [pin_name(dio)] if dio is not None else []

    if role == "spi":
        if chan == "FIO0-FIO3":
            return ["FIO0", "FIO1", "FIO2", "FIO3"]
        if chan == "FIO1-FIO3":
            return ["FIO1", "FIO2", "FIO3"]
    elif role == "i2c":
        m = _I2C_RANGE_RE.fullmatch(chan)
        if m:
            return [pin_name(int(m.group(1))), pin_name(int(m.group(2)))]
    else:
        return []

    # Labeled custom-pin summary (both roles).
    pins = []
    for token in _LABELED_TOKEN_RE.findall(chan):
        dio = try_parse_pin(token)
        if dio is not None:
            pins.append(pin_name(dio))
    return pins


def resolve_pin_selection(role: str, chosen: dict[str, str]):
    """Turn a pin-picker selection into the net record fields.

    Args:
        role: "i2c" or "spi".
        chosen: signal name -> pin name, e.g. {"SDA": "EIO0", "SCL": "EIO1"}.
                For spi, "CS" may be :data:`NO_CS` (3-pin / manual CS).

    Returns:
        ``(label, params, error)``:

        * error is an error string when the selection is invalid (duplicate
          pins); label/params are None in that case.
        * label/params are both None when the selection equals the
          historical defaults — callers keep the legacy channel string so
          the saved record is byte-identical to a default add.
        * otherwise label is the ``pin`` display summary and params is the
          dict the box dispatchers consume.
    """
    signals = I2C_SIGNALS if role == "i2c" else SPI_SIGNALS
    defaults = I2C_DEFAULT_PINS if role == "i2c" else SPI_DEFAULT_PINS

    seen: dict[int, str] = {}
    for signal in signals:
        value = chosen.get(signal)
        if signal == "CS" and value == NO_CS:
            continue
        dio = try_parse_pin(value)
        if dio is None:
            return None, None, f"Invalid pin '{value}' for {signal}."
        if dio in seen:
            return None, None, (
                f"{seen[dio]} and {signal} both use {pin_name(dio)}; "
                f"each signal needs its own pin."
            )
        seen[dio] = signal

    if all(chosen.get(s) == defaults[s] for s in signals):
        return None, None, None

    params: dict[str, int] = {}
    label_parts: list[str] = []
    for signal in signals:
        value = chosen.get(signal)
        if signal == "CS" and value == NO_CS:
            continue
        dio = try_parse_pin(value)
        params[PARAM_KEYS[signal]] = dio
        label_parts.append(f"{signal}:{pin_name(dio)}")

    return " ".join(label_parts), params, None

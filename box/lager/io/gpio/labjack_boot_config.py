# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Power-up (boot) levels for LabJack GPIO nets.

A LabJack keeps an I/O configuration in flash that it applies at power-up.
From the factory, every DIO comes up as an input with a pull-up, so a load
wired to a gpio net sees HIGH from power-on until something drives the pin.
A net can declare ``power_up: "low" | "high"``. This module computes, checks
and writes the boot configuration for one LabJack from every net declared on
it. The whole device is handled at once, because the boot configuration is
device-wide.

T7: there is no register that saves DIO defaults on their own. The T7 saves its
entire *current* I/O configuration (every DIO direction and state, the DACs,
the AIN and DIO_EF settings) when ``IO_CONFIG_SET_DEFAULT_TO_CURRENT`` is
written. So apply drives the declared pins first, then saves. It refuses when
a pin no net declares is currently an output, because the save would freeze
that pin's present level too. The T7 cannot read its stored DIO levels back
(the config CRC excludes them), so the box records what it saved, by serial.

U3: ``configU3`` reads and writes the power-up defaults directly, separate
from the live state. LabJackPython sends 0 for any field of a written group
that the call leaves out, so apply reads every field first and writes them
all back with only the declared bits changed. A declared pin's analog bit is
cleared too: a line that powers up analog ignores its direction and state.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Dict, Optional, Tuple

# Vendor-neutral helpers, shared with every power-up backend. Re-exported here
# because this module's callers and tests have always found them here.
from .power_up.base import LEVELS, iter_declared, normalize_level, public
from .power_up.base import PowerUpError as BootConfigError

# T7 DIO numbering: FIO0-7 = 0-7, EIO0-7 = 8-15, CIO0-3 = 16-19, MIO0-2 = 20-22.
_T7_BLOCKS = {"FIO": (0, 8), "EIO": (8, 8), "CIO": (16, 4), "MIO": (20, 3)}
_T7_PIN_RE = re.compile(r"^\s*(FIO|EIO|CIO|MIO)(\d+)\s*$", re.IGNORECASE)
T7_DIO_MASK = (1 << 23) - 1

# Register names, older name first: the LJM in the box image rejects the
# newer BOOTUP spellings, and firmware that knows both accepts either.
T7_SAVE_REGISTERS = ("IO_CONFIG_SET_DEFAULT_TO_CURRENT",
                     "IO_CONFIG_SET_BOOTUP_TO_CURRENT")
T7_FACTORY_CHECK = "IO_CONFIG_CHECK_FOR_FACTORY"

# Fields of a no-argument configU3() read that apply writes back unchanged
# (apart from the declared bits). Every one must be present in the read, or
# apply refuses: a field written as a default 0 can reconfigure the device.
U3_FIELDS = ("FIOAnalog", "FIODirection", "FIOState", "EIOAnalog",
             "EIODirection", "EIOState", "CIODirection", "CIOState",
             "TimerCounterConfig")
# Which configU3 byte holds a DIO's bit, by block.
_U3_BLOCKS = ((0, "FIO", True), (8, "EIO", True), (16, "CIO", False))

RECORD_FILE = os.environ.get("LAGER_LABJACK_BOOT_RECORD",
                             "/etc/lager/labjack_boot_config.json")




def t7_pin_to_dio(pin) -> int:
    """T7 DIO number for a gpio net's pin (``"FIO3"``, ``"EIO0"``, or 0-7)."""
    if isinstance(pin, bool):
        raise ValueError(f"Invalid LabJack T7 pin: {pin!r}")
    try:
        number = int(str(pin).strip())
    except (TypeError, ValueError):
        number = None
    if number is not None:
        # The T7 driver writes a numeric pin as FIO<n>, so only 0-7 are real.
        if 0 <= number <= 7:
            return number
        raise ValueError(f"LabJack T7 pin {pin!r}: a numeric pin is FIO0-FIO7; "
                         f"name EIO/CIO/MIO pins explicitly")
    match = _T7_PIN_RE.match(str(pin))
    if not match:
        raise ValueError(f"Invalid LabJack T7 pin {pin!r}. Expected FIO0-7, "
                         f"EIO0-7, CIO0-3 or MIO0-2.")
    block, index = match.group(1).upper(), int(match.group(2))
    base, width = _T7_BLOCKS[block]
    if index >= width:
        raise ValueError(f"{block}{index} does not exist on a T7 "
                         f"({block} runs 0-{width - 1}).")
    return base + index


def t7_dio_name(dio: int) -> str:
    for block, (base, width) in _T7_BLOCKS.items():
        if base <= dio < base + width:
            return f"{block}{dio - base}"
    return f"DIO{dio}"


def masks(declared: Dict[int, str]) -> Tuple[int, int]:
    """``(declared mask, high mask)`` for ``{dio: "low"|"high"}``."""
    mask = high = 0
    for dio, level in declared.items():
        mask |= 1 << dio
        if level == "high":
            high |= 1 << dio
    return mask, high


def _level_from_masks(dio: int, direction: int, state: int) -> str:
    bit = 1 << dio
    if not direction & bit:
        return "input"
    return "high" if state & bit else "low"


# ---------------------------------------------------------------------------
# Box-side record of what was saved to each T7 (it cannot be read back)
# ---------------------------------------------------------------------------

def load_records(path: Optional[str] = None) -> Dict[str, Any]:
    path = path or RECORD_FILE
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_record(serial: str, record: Dict[str, Any], path: Optional[str] = None) -> None:
    path = path or RECORD_FILE
    data = load_records(path)
    data[str(serial)] = record
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".labjack_boot.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# T7
# ---------------------------------------------------------------------------

def t7_plan(ljm, handle, declared: Dict[int, str],
            record_path: Optional[str] = None) -> Dict[str, Any]:
    """Read-only: the T7's boot-config state against the declared levels."""
    serial = str(int(ljm.eReadName(handle, "SERIAL_NUMBER")))
    direction = int(ljm.eReadName(handle, "DIO_DIRECTION")) & T7_DIO_MASK
    state = int(ljm.eReadName(handle, "DIO_STATE")) & T7_DIO_MASK
    factory = int(ljm.eReadName(handle, T7_FACTORY_CHECK)) == 1

    mask, high = masks(declared)
    target_dir = direction | mask
    target_state = (state & ~mask) | high

    record = None if factory else load_records(record_path).get(serial)
    stored_known = factory or record is not None

    def stored(dio):
        if factory:
            return "input"
        if record is None:
            return "unknown"
        return _level_from_masks(dio, int(record.get("dio_direction", 0)),
                                 int(record.get("dio_state", 0)))

    # In sync means every declared pin is stored at its declared level. Pins no
    # net declares are not compared: their live state changes with ordinary
    # gpo use, and comparing it would report drift the declared levels lack.
    in_sync = all(stored(dio) == level for dio, level in declared.items())

    undeclared_outputs = direction & ~mask
    return {
        "model": "T7",
        "serial": serial,
        "factory": factory,
        "stored_known": stored_known,
        "pins": {dio: {"dio_name": t7_dio_name(dio), "declared": level,
                       "stored": stored(dio)}
                 for dio, level in sorted(declared.items())},
        "undeclared_outputs": [t7_dio_name(d) for d in range(23)
                               if undeclared_outputs & (1 << d)],
        "target_direction": target_dir,
        "target_state": target_state & target_dir,
        "in_sync": in_sync,
    }


def t7_apply(ljm, handle, declared: Dict[int, str], drive,
             allow_undeclared: bool = False, record_path: Optional[str] = None,
             nets: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    """Drive the declared pins, then save the T7's config as its boot config.

    ``drive(dio, level)`` drives one declared pin through its gpio driver.
    Writes flash only when the stored config differs from the target.
    """
    plan = t7_plan(ljm, handle, declared, record_path)
    plan["written"] = False
    if plan["in_sync"]:
        return plan
    if plan["undeclared_outputs"] and not allow_undeclared:
        raise BootConfigError(
            "LabJack T7 %s: pins %s are outputs that no net declares a power-up "
            "level for. The T7 saves every pin's current state, so saving now "
            "would also freeze these pins. Declare a level for each, set them "
            "back to inputs, or pass --allow-undeclared to save them as they are."
            % (plan["serial"], ", ".join(plan["undeclared_outputs"])))

    for dio, level in sorted(declared.items()):
        drive(dio, level)

    errors = []
    used = None
    for register in T7_SAVE_REGISTERS:
        try:
            ljm.eWriteName(handle, register, 1)
        except Exception as exc:  # LJMError on a name this LJM does not know
            errors.append(f"{register}: {exc}")
            continue
        used = register
        break
    if used is None:
        raise BootConfigError("LabJack T7 did not accept a boot-config save: "
                              + "; ".join(errors))
    if int(ljm.eReadName(handle, T7_FACTORY_CHECK)) != 0:
        raise BootConfigError(
            f"LabJack T7 {plan['serial']}: wrote {used}, but the T7 still reports "
            f"its factory boot config")

    direction = int(ljm.eReadName(handle, "DIO_DIRECTION")) & T7_DIO_MASK
    state = int(ljm.eReadName(handle, "DIO_STATE")) & T7_DIO_MASK
    save_record(plan["serial"], {
        "dio_direction": direction,
        "dio_state": state & direction,
        "declared": {t7_dio_name(d): lv for d, lv in sorted(declared.items())},
        "nets": {t7_dio_name(d): n for d, n in sorted((nets or {}).items())},
        "register": used,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, record_path)

    after = t7_plan(ljm, handle, declared, record_path)
    after["written"] = True
    return after


# ---------------------------------------------------------------------------
# U3
# ---------------------------------------------------------------------------

def _u3_slot(dio: int) -> Tuple[str, bool, int]:
    """``(block prefix, has analog bit, bit)`` for a U3 DIO number."""
    for base, prefix, analog in reversed(_U3_BLOCKS):
        if dio >= base:
            return prefix, analog, 1 << (dio - base)
    raise ValueError(f"Invalid U3 DIO {dio}")


def u3_target(current: Dict[str, Any], declared: Dict[int, str]) -> Dict[str, int]:
    """Every U3_FIELDS value, with only the declared pins' bits changed."""
    missing = [f for f in U3_FIELDS if f not in current]
    if missing:
        raise BootConfigError(
            "configU3() did not report %s; refusing to write the U3's power-up "
            "defaults without them" % ", ".join(missing))
    target = {f: int(current[f]) for f in U3_FIELDS}
    for dio, level in declared.items():
        prefix, analog, bit = _u3_slot(dio)
        if analog:
            target[prefix + "Analog"] &= ~bit & 0xFF
        target[prefix + "Direction"] |= bit
        if level == "high":
            target[prefix + "State"] |= bit
        else:
            target[prefix + "State"] &= ~bit & 0xFF
    return target


def _u3_stored(cfg: Dict[str, Any], dio: int) -> str:
    prefix, analog, bit = _u3_slot(dio)
    if analog and int(cfg.get(prefix + "Analog", 0)) & bit:
        return "analog"
    if not int(cfg.get(prefix + "Direction", 0)) & bit:
        return "input"
    return "high" if int(cfg.get(prefix + "State", 0)) & bit else "low"


def u3_plan(device, declared: Dict[int, str]) -> Dict[str, Any]:
    """Read-only: the U3's power-up defaults against the declared levels."""
    from lager.io.labjack_ud_handle import dio_to_pin
    current = device.configU3()
    target = u3_target(current, declared)
    return {
        "model": "U3",
        "serial": str(current.get("SerialNumber", "")),
        "factory": None,
        "stored_known": True,
        "pins": {dio: {"dio_name": dio_to_pin(dio), "declared": level,
                       "stored": _u3_stored(current, dio)}
                 for dio, level in sorted(declared.items())},
        "undeclared_outputs": [],
        "in_sync": all(int(current[f]) == target[f] for f in U3_FIELDS),
        "_target": target,
    }


def u3_apply(device, declared: Dict[int, str], drive) -> Dict[str, Any]:
    """Drive the declared pins live, then write the U3's power-up defaults."""
    plan = u3_plan(device, declared)
    plan["written"] = False
    for dio, level in sorted(declared.items()):
        drive(dio, level)
    if plan["in_sync"]:
        plan.pop("_target", None)
        return plan
    target = plan.pop("_target")
    device.configU3(**target)
    readback = device.configU3()
    wrong = [f for f in U3_FIELDS if int(readback.get(f, -1)) != target[f]]
    if wrong:
        raise BootConfigError(
            "U3 power-up defaults did not take: %s read back differently"
            % ", ".join(wrong))
    after = u3_plan(device, declared)
    after.pop("_target", None)
    after["written"] = True
    return after


__all__ = [
    "BootConfigError", "LEVELS", "normalize_level", "t7_pin_to_dio",
    "t7_dio_name", "masks", "t7_plan", "t7_apply", "u3_target", "u3_plan",
    "u3_apply", "public", "iter_declared", "load_records", "save_record",
]

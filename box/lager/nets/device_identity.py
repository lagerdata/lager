# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Which physical device a saved net is wired to.

hardware_service serializes calls under one lock per physical device, so
every route that reaches a device -- the net command endpoint, the batch
read, and the Python ``Net`` API -- has to name the device the same way, or
two of them take different locks and interleave I/O on one instrument. This
module is that naming, kept free of imports so any of them can use it.
"""
import re

# Matches the T-series LabJack only.
_LABJACK_T7_RE = re.compile(r"labjack[_\-\s]*t7", re.IGNORECASE)


def address_from_rec(rec):
    """Resolve a device address from a saved-net record.

    Prefers a per-net mappings[].device_override, else the top-level address —
    the same precedence resolve_address() uses. Returns None if unset.
    """
    for mapping in rec.get("mappings") or []:
        if mapping.get("device_override"):
            return mapping["device_override"]
    return rec.get("address")


def physical_device_id(role, instrument, rec):
    """Stable identity for the physical device backing this net.

    hardware_service uses it as the shared lock key so that every net/role on
    one physical device serializes — critically the LabJack T7, whose single
    LJM handle is shared across GPIO/ADC/DAC/SPI/I2C, and a Joulescope/PPK2
    shared by a watt-meter net and an energy-analyzer net. Keyed on the device
    family + its serial/address (or a constant when a family has one shared
    handle), NOT on the net name or pin.
    """
    inst = (instrument or "").lower()
    addr = address_from_rec(rec) or ""
    serial = ""
    if "::" in addr:
        parts = addr.split("::")
        if len(parts) > 3 and parts[3]:
            serial = parts[3]
    if any(k in inst for k in ("usb-202", "usb202", "mcc")):
        return "usb202:" + (rec.get("unique_id") or addr or "ANY")
    if "ft232h" in inst or "ftdi" in inst:
        return "ft232h:" + (serial or addr or "ANY")
    if "aardvark" in inst or "totalphase" in inst:
        port = str((rec.get("params") or {}).get("port", 0))
        return "aardvark:" + (serial or addr or port)
    if "picoscope" in inst or "pico" in inst:
        # A PicoScope's USB handle admits one owner at a time, and the
        # oscilloscope daemon holds it. Every scope net on the same unit must
        # therefore take the same lock, so a `lager scope` command queues
        # behind a browser streaming it rather than failing to open.
        #
        # Keyed on the serial when the net carries one, since a bench can have
        # several PicoScopes and those must NOT serialize against each other.
        return "picoscope:" + (serial or rec.get("unique_id") or addr or "ANY")
    if role in ("scope", "scope-channel") or "rigol_mso" in inst or "mso5" in inst:
        # Every scope that is not a PicoScope, which the branch above already
        # keyed. In practice a Rigol, and it had been falling past all of
        # these to the LabJack default at the bottom. Its VISA address made
        # the key unique so it worked, but a Rigol saved without an address
        # collapsed onto "labjack:ANY" and took the lock a LabJack was using,
        # queueing scope commands behind GPIO traffic on unrelated hardware.
        #
        # The scope net and its channel nets land here together, which is
        # what they need: they address one instrument.
        return "scope:" + (serial or addr or rec.get("unique_id") or "ANY")
    if "joulescope" in inst or "js220" in inst:
        return "joulescope:" + (addr or "ANY")
    if "ppk" in inst or "nordic" in inst:
        return "ppk2:" + (addr or "ANY")
    if role == "thermocouple" or "phidget" in inst:
        return "phidget:" + (addr or "ANY")
    if role == "arm" or "dexarm" in inst or "rotrics" in inst:
        # One serial port per arm; the lock key is the arm's serial number
        # (saved under rec["serial"] or location.serial_number) or address.
        location = rec.get("location")
        loc_serial = location.get("serial_number") if isinstance(location, dict) else None
        return "dexarm:" + (rec.get("serial") or loc_serial or addr or "ANY")
    if role == "watt-meter" or "yocto" in inst:
        return "yocto:" + (addr or "ANY")
    if "labjack" in inst and not _LABJACK_T7_RE.search(inst):
        # A LabJack that is not a T7 -- a U3/U6. It reaches the hardware over
        # Exodriver, holds its own USB claim, and shares nothing with the T7's
        # LJM handle, so it must not share the T7's lock either.
        #
        # The address alone would nearly do it (it carries the PID: 0x0007 for
        # a T7, 0x0003 for a U3), but a LabJack net may be saved with no
        # address at all -- the ADC dispatcher resolves an empty one on the
        # grounds that LabJack auto-discovers. Both models would then collapse
        # onto "labjack:ANY". Folding the model in keeps them apart in that
        # case too.
        #
        # Deliberately below every other branch and additive: the T7 and every
        # instrument that falls through to the default below keep the exact key
        # they had, so nothing that works today changes lock identity.
        return "labjack:" + inst + ":" + (addr or "ANY")
    # Default: LabJack T7 — one shared LJM handle across all roles/pins.
    return "labjack:" + (addr or "ANY")

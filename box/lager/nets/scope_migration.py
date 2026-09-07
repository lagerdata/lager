# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Turning the old scope nets into a scope and its channels.

A scope net used to be a channel: `scope1` was channel A, `scope2` channel B,
both saved with ``role: "scope"``. But most of an oscilloscope's controls are
not per-channel. Of the settings Lager exposes, six belong to a channel --
enable, volts/div, offset, coupling, probe, measurements -- and fourteen
belong to the instrument: the timebase, the horizontal position, the whole
trigger, run/stop/single/force, and everything the unit reports about itself.
Those had nowhere to be addressed, so they were sent to whichever channel net
the caller happened to be holding, and the web UI picked one arbitrarily.

So ``role: "scope"`` now means the instrument and ``role: "scope-channel"``
means a channel of it. That reuses the same string for a different thing,
which is only safe because every scope net saved before this change is a
channel -- the instrument had no representation to be confused with. The
conversion is therefore total rather than a guess, and this module does it
once, in place.

The one record it cannot read is a scope net with no pin. Those exist: the
pin is optional and the driver quietly defaulted to channel 1. After this
change they are taken for instruments, because a record that names no channel
describes the unit better than it describes channel A, and a per-channel
command sent to one now says so instead of silently landing on the first
channel.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

SCOPE_ROLE = "scope"
SCOPE_CHANNEL_ROLE = "scope-channel"


def _pin_value(raw):
    """A pin, or None where the record is standing in for not having one.

    Scope channels are numbered from 1, A being 1, and zero is what
    ``Net.save_local_net`` writes into the mappings when a record has no pin
    at all. Reading that back as channel zero is what made this migration
    convert its own output on a second run, and it would have turned a scope
    net added through the TUI into a channel the moment it was saved.
    """
    if raw in (None, ""):
        return None
    try:
        return None if int(raw) == 0 else raw
    except (TypeError, ValueError):
        return raw


def pin_of(rec: Dict[str, Any]):
    """The channel a record is wired to, or None if it names none."""
    direct = _pin_value(rec.get("pin"))
    if direct is not None:
        return direct
    for mapping in rec.get("mappings") or []:
        for key in ("pin", "channel"):
            found = _pin_value(mapping.get(key))
            if found is not None:
                return found
    return None


def unit_of(rec: Dict[str, Any]) -> Tuple[str, str]:
    """Which physical scope a record belongs to.

    Instrument and address together, because a bench can hold two of the same
    model and their channels must not be gathered under one net.
    """
    return ((rec.get("instrument") or "").lower(),
            (rec.get("address") or "").lower())


def _base_name(instrument: str) -> str:
    """A readable stem for the instrument's own net.

    Taken from the model rather than from the channel nets, which are named
    after what they are probing: `picoscope1` says what it is, where a stem
    shared by `vbus` and `reset` would say nothing.
    """
    letters = re.split(r"[^A-Za-z]+", instrument or "")
    for word in letters:
        if word and not word.isdigit():
            return word.lower()
    return "scope"


def _unique_name(base: str, taken) -> str:
    for index in range(1, 1000):
        candidate = "%s%d" % (base, index)
        if candidate not in taken:
            return candidate
    return base


def _instrument_net(rec: Dict[str, Any], name: str) -> Dict[str, Any]:
    """A net standing for the scope itself, built from one of its channels.

    It carries no pin, which is what marks it as the instrument: the channels
    that came out of the same record keep theirs.
    """
    net: Dict[str, Any] = {
        "name": name,
        "role": SCOPE_ROLE,
        "instrument": rec.get("instrument") or "",
    }
    if rec.get("address"):
        net["address"] = rec["address"]
    if rec.get("unique_id"):
        net["unique_id"] = rec["unique_id"]
    # Shaped like every other saved record, which Net.save_local_net would
    # have added; nothing reads a scope net's mappings, but code that walks
    # them should not have to special-case this one.
    mapping = {"net": name, "pin": 0, "location": "0"}
    if rec.get("address"):
        mapping["device_override"] = rec["address"]
    net["mappings"] = [mapping]
    net["scope_points"] = [[0, "0"]]
    return net


def needs_migration(nets: List[Dict[str, Any]]) -> bool:
    """Whether any record still uses ``scope`` to mean a channel.

    A pin is what tells them apart. Before this change every channel had one
    or defaulted to channel 1; after it, the instrument's net is the pinless
    one, so a scope net carrying a pin can only be a channel that predates
    the split.
    """
    return any(rec.get("role") == SCOPE_ROLE and pin_of(rec) is not None
               for rec in nets or [])


def migrate(nets: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
    """Rewrite old scope nets as channels, and give each unit its own net.

    Returns the records and whether anything changed, so a caller can skip
    writing when there was nothing to do. Idempotent: run twice, the second
    pass finds no scope net with a pin and returns the input untouched.
    """
    nets = list(nets or [])
    if not needs_migration(nets):
        return nets, False

    taken = {rec.get("name") for rec in nets}
    # Units that already have an instrument net, so re-running after a
    # partial write does not add a second one.
    have_instrument = {unit_of(rec) for rec in nets
                       if rec.get("role") == SCOPE_ROLE
                       and pin_of(rec) is None}

    migrated: List[Dict[str, Any]] = []
    new_instruments: List[Dict[str, Any]] = []
    for rec in nets:
        if rec.get("role") != SCOPE_ROLE or pin_of(rec) is None:
            migrated.append(rec)
            continue

        channel = dict(rec)
        channel["role"] = SCOPE_CHANNEL_ROLE
        migrated.append(channel)

        unit = unit_of(rec)
        if unit in have_instrument:
            continue
        have_instrument.add(unit)
        name = _unique_name(_base_name(rec.get("instrument") or ""), taken)
        taken.add(name)
        new_instruments.append(_instrument_net(rec, name))

    return migrated + new_instruments, True

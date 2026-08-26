# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Whether a VISA address identifies exactly one physical device.

``nets add``, ``nets add-all``, the net TUI and ``lager instruments`` all need
the same question answered: with two devices of one model plugged in, can a
saved net still name the one it means?

That is a property of the ADDRESS, not of the model. Most instruments carry a
unique iSerialNumber, so two of them get different addresses and both stay
drivable -- an Acroname 8-port enumerates as
``USB0::0x24FF::0x0013::EBFB8D94::INSTR``, and a second one carries its own
serial. Hubs that report no serial are already handled by the scanner's
``_TOPOLOGY_ADDRESSED`` set (``box/lager/http_handlers/usb_scanner.py``), which
substitutes the sysfs topology path behind a ``port-`` prefix precisely so that
two of them remain distinguishable.

What is left is the case the scanner cannot fix from the descriptor: a
LabJack T7 reports no serial and is not topology-addressed, so it enumerates as
``USB0::0x0CD5::0x0007::::INSTR`` on every box measured. Two of those are
byte-identical, and a net naming that address cannot say which one it means.

These four call sites previously each carried their own copy of a hardcoded
``_MULTI_HUBS`` model set and each did something different with it -- one hard
errored, one silently skipped the whole family, one computed per-device keys and
then discarded them. Keying on the address instead is right for a model nobody
has thought about yet, and stops being wrong for a model the moment the scanner
learns how to address it.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping


def ambiguous_addresses(devices: Iterable[Mapping]) -> set[str]:
    """Return the addresses reported by more than one present device.

    *devices* is the scanner's instrument list -- dicts carrying at least
    ``address``. An address in the returned set cannot identify one physical
    device, so no net may be created against it.

    Empty on every single-device bench, which is the common case: the guard
    costs nothing when there is nothing to disambiguate.
    """
    counts = Counter(
        address
        for address in (dev.get("address") for dev in devices)
        if address
    )
    return {address for address, count in counts.items() if count > 1}


def describe_ambiguity(instrument: str, address: str) -> str:
    """One-line operator explanation for a refused address.

    Says what is wrong (two devices, one address) rather than prescribing
    "unplug extras", which was the old message and is not the only fix --
    teaching the scanner to address the model works too.
    """
    return (
        f"{instrument} at {address}: more than one device reports this "
        f"address, so a net could not say which one it means. This model "
        f"exposes no unique serial number. Use only one at a time, or drive "
        f"them from separate boxes."
    )

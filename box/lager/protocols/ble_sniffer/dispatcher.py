# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Instrument-name routing for ble-sniffer nets.

``DRIVERS`` is the only place a sniffer model is named. Adding a sniffer is a
``BleSnifferBase`` subclass plus one entry there; the HTTP handler and
``Net.get`` both route through ``driver_class``.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from lager.exceptions import BleSnifferBackendError
from lager.nets.device_identity import address_from_rec
from lager.protocols.ble_sniffer.ble_sniffer_net import BleSnifferNet

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lager.protocols.ble_sniffer.ble_sniffer_net import BleSnifferBase

#: Lowercase substring of a saved instrument name -> "module:Class" of its
#: driver. The first match wins, so list a more specific name before one it
#: contains.
DRIVERS: dict[str, str] = {
    "nrf_sniffer": "lager.protocols.ble_sniffer.nrf_sniffer:NrfSniffer",
}


def _driver_path(instrument: str) -> str | None:
    name = (instrument or "").lower()
    return next((path for match, path in DRIVERS.items() if match in name), None)


def is_supported(instrument: str) -> bool:
    """Whether a sniffer driver exists for the saved instrument name."""
    return _driver_path(instrument) is not None


def driver_class(instrument: str) -> type[BleSnifferBase]:
    """The driver class for a saved instrument name.

    Raises:
        BleSnifferBackendError: No driver for the instrument.
    """
    path = _driver_path(instrument)
    if path is None:
        raise BleSnifferBackendError(f"Unsupported ble-sniffer instrument '{instrument}'")
    module_name, class_name = path.split(":")
    return getattr(importlib.import_module(module_name), class_name)


def make_driver(name: str, rec: Mapping[str, Any]) -> BleSnifferBase:
    """The driver for a saved ble-sniffer net record.

    Raises:
        BleSnifferBackendError: No driver for the instrument, or the record
            has no address.
    """
    instrument = rec.get("instrument") or ""
    address = address_from_rec(rec)
    if not address:
        raise BleSnifferBackendError(f"ble-sniffer net '{name}' has no device address")
    return driver_class(instrument)({"name": name, "address": address, "instrument": instrument})


def make_net(name: str, rec: Mapping[str, Any]) -> BleSnifferNet:
    """The ``Net.get`` object for a saved ble-sniffer net record."""
    return BleSnifferNet(name, make_driver(name, rec))

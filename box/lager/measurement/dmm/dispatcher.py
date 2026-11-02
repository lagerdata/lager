# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Instrument-name routing for DMM nets.

A DMM is a VISA instrument, so it is driven through hardware_service like
the e-load: the box HTTP handler and ``Net.get`` build a ``Device`` proxy for
the ``dmm_hs`` factory, and hardware_service opens the driver chosen here,
caches it per VISA address and serializes calls on that address.

``DRIVERS`` is the only place a meter model is named. Adding a meter is a
``DMMBase`` subclass plus one entry there; the HTTP handler, ``Net.get`` and
``dmm_hs`` all route through ``driver_class``.
"""

from __future__ import annotations

import importlib
from functools import partial
from typing import TYPE_CHECKING, Any

from lager.exceptions import DMMBackendError
from lager.measurement.dmm.dmm_net import PROXY_TIMEOUT_S, DMMNet
from lager.nets.device import Device
from lager.nets.device_identity import address_from_rec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lager.measurement.dmm.dmm_net import DMMBase

#: hardware_service factory module for every DMM net.
HS_MODULE = "dmm_hs"

#: Lowercase substring of a saved instrument name -> "module:Class" of its
#: driver. The first match wins, so list a more specific name before one it
#: contains. Paths are strings because driver modules import pyvisa, which
#: is loaded only when a DMM is used.
DRIVERS: dict[str, str] = {
    "sdm3065": "lager.measurement.dmm.siglent_sdm3065x:SiglentSDM3065X",
}


def _driver_path(instrument: str) -> str | None:
    name = (instrument or "").lower()
    return next((path for match, path in DRIVERS.items() if match in name), None)


def is_supported(instrument: str) -> bool:
    """Whether a DMM driver exists for the saved instrument name."""
    return _driver_path(instrument) is not None


def module_for_instrument(instrument: str) -> str | None:
    """hardware_service module for ``instrument``, or None if no driver exists."""
    return HS_MODULE if is_supported(instrument) else None


def driver_class(instrument: str) -> type[DMMBase]:
    """The driver class for a saved instrument name.

    Raises:
        DMMBackendError: No driver for the instrument.
    """
    path = _driver_path(instrument)
    if path is None:
        raise DMMBackendError(f"Unsupported DMM instrument '{instrument}'")
    module_name, class_name = path.split(":")
    return getattr(importlib.import_module(module_name), class_name)


def validate(
    instrument: str,
    function: str,
    range: float | str | None = None,
    nplc: float | None = None,
    sensor: str | None = None,
) -> None:
    """Check a measure request against the instrument without touching it.

    Raises:
        DMMUnsupportedError: The instrument cannot do what was asked.
        DMMBackendError: No driver for the instrument.
    """
    driver_class(instrument).validate(function, range=range, nplc=nplc, sensor=sensor)


def make_driver(net_info: Mapping[str, Any]) -> DMMBase:
    """Open the driver for a net record (``name``, ``address``, ``instrument``).

    Raises:
        DMMBackendError: No driver for the instrument, or the open failed.
    """
    return driver_class(net_info.get("instrument") or "")(net_info)


def make_net(name: str, rec: Mapping[str, Any]) -> DMMNet:
    """The ``Net.get`` object for a saved DMM net record."""
    instrument = rec.get("instrument") or ""
    if not is_supported(instrument):
        raise DMMBackendError(f"Unsupported DMM instrument '{instrument}' for net '{name}'")
    address = address_from_rec(rec)
    if not address:
        raise DMMBackendError(f"DMM net '{name}' has no device address")
    net_info = {"name": name, "address": address, "instrument": instrument}
    return DMMNet(name, Device(HS_MODULE, net_info, timeout=PROXY_TIMEOUT_S),
                  validate=partial(validate, instrument))

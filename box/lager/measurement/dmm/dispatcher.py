# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Instrument-name routing for DMM nets.

A DMM is a VISA instrument, so it is driven through hardware_service like
the e-load: the box HTTP handler and ``Net.get`` build a ``Device`` proxy for
the ``dmm_hs`` factory, and hardware_service opens the driver chosen here,
caches it per VISA address and serializes calls on that address.
"""

from __future__ import annotations

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


def is_supported(instrument: str) -> bool:
    """Whether a DMM driver exists for the saved instrument name."""
    return "sdm3065" in (instrument or "").lower()


def module_for_instrument(instrument: str) -> str | None:
    """hardware_service module for ``instrument``, or None if no driver exists."""
    return HS_MODULE if is_supported(instrument) else None


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
    if not is_supported(instrument):
        raise DMMBackendError(f"Unsupported DMM instrument '{instrument}'")
    # Driver modules import pyvisa; load them only when a DMM is used.
    from lager.measurement.dmm.siglent_sdm3065x import build_commands
    build_commands(function, range=range, nplc=nplc, sensor=sensor)


def make_driver(net_info: Mapping[str, Any]) -> DMMBase:
    """Open the driver for a net record (``name``, ``address``, ``instrument``).

    Raises:
        DMMBackendError: No driver for the instrument, or the open failed.
    """
    instrument = net_info.get("instrument") or ""
    if not is_supported(instrument):
        raise DMMBackendError(f"Unsupported DMM instrument '{instrument}'")
    from lager.measurement.dmm.siglent_sdm3065x import SiglentSDM3065X
    return SiglentSDM3065X(net_info)


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

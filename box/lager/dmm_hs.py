# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for DMM nets (create_device factory).

Role-unique module name so hardware_service's import search finds it at
``lager.dmm_hs``. hardware_service caches the driver per VISA address and
serializes calls under that address's lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lager.measurement.dmm.dispatcher import make_driver

if TYPE_CHECKING:
    from collections.abc import Mapping

    from lager.measurement.dmm.dmm_net import DMMBase


def create_device(net_info: Mapping[str, Any], **_: Any) -> DMMBase:
    """Open the DMM driver for ``net_info`` (``name``, ``address``, ``instrument``)."""
    return make_driver(net_info or {})

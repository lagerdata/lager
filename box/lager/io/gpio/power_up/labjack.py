# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
LabJack T7 / U3 power-up backend: the level is stored in the LabJack's flash.

Each declared net is resolved through the GPIO dispatcher, the same path
``lager gpo`` takes, so pins are parsed and driven exactly as they are there.
The boot-config logic itself is in ``lager.io.gpio.labjack_boot_config``.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .base import PowerUpBackend


class LabJackBackend(PowerUpBackend):
    family = "labjack"
    label = "LabJack"
    storage = "flash"
    coverage = ("The LabJack applies these levels itself as it boots, before any "
                "host software runs; until then its pins are pulled-up inputs.")

    @staticmethod
    def _resolve(nets: List[Dict[str, Any]]):
        """``(kind, context, entries, drivers)`` for one LabJack's declared nets."""
        from lager.io.gpio import dispatcher as _disp
        from lager.io.gpio.labjack_t7 import LabJackGPIO
        from lager.io.gpio.labjack_ud import LabJackUDGPIO
        from lager.io.gpio import labjack_boot_config as bc

        kind = None
        context = None
        entries = []
        drivers = {}
        for net in nets:
            name = net["name"]
            level = bc.normalize_level(net.get("power_up"))
            if level is None:
                continue
            drv, pin = _disp._dispatcher._resolve_net_and_driver(name)
            if isinstance(drv, LabJackGPIO):
                this_kind, dio = "t7", bc.t7_pin_to_dio(pin)
                this_context = (drv._get_ljm(), drv._get_handle())
            elif isinstance(drv, LabJackUDGPIO):
                this_kind, dio = "u3", drv._get_dio()
                this_context = drv._get_device()
            else:
                raise bc.BootConfigError(
                    f"net '{name}' is on a {type(drv).__name__}, which has no "
                    f"power-up configuration; only LabJack T7 and U3 nets do")
            if kind is None:
                kind, context = this_kind, this_context
            elif kind != this_kind:
                raise bc.BootConfigError("nets of two LabJack models were grouped "
                                         "as one device")
            entries.append((dio, level, name))
            drivers[dio] = drv
        return kind, context, entries, drivers

    def run(self, mode, nets, allow_undeclared=False):
        """Check (``mode="plan"``) or write (``mode="apply"``) one LabJack."""
        from lager.io.gpio import labjack_boot_config as bc

        kind, context, entries, drivers = self._resolve(list(nets or []))
        if kind is None:
            return {"model": None, "pins": [], "in_sync": True, "written": False}
        declared, names = bc.iter_declared(entries)

        def drive(dio, level):
            drivers[dio].output(1 if level == "high" else 0)

        if kind == "t7":
            ljm, handle = context
            if mode == "plan":
                report = bc.t7_plan(ljm, handle, declared)
            else:
                report = bc.t7_apply(ljm, handle, declared, drive,
                                     allow_undeclared=bool(allow_undeclared),
                                     nets=names)
        else:
            if mode == "plan":
                report = bc.u3_plan(context, declared)
            else:
                report = bc.u3_apply(context, declared, drive)
        return bc.public(report, names)


BACKEND = LabJackBackend()

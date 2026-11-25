# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for declared gpio power-up levels.

One instance per physical device, under that device's ``device_id`` lock, so a
check or an apply never interleaves with a gpo/gpi/adc/dac call on the same
device. The instrument in ``net_info`` picks the family backend
(``lager.io.gpio.power_up``), which does the work.
"""
from __future__ import annotations


class PowerUpAdapter:
    def __init__(self, net_info) -> None:
        self._instrument = (net_info or {}).get("instrument") or ""

    def run(self, mode: str = "plan", nets=None, allow_undeclared: bool = False):
        """Check (``mode="plan"``) or apply (``mode="apply"``) one device.

        ``nets`` is ``[{"name", "power_up"}, ...]`` for the nets on this
        device. Returns the backend's report plus ``backend``, ``storage`` and
        ``coverage``.
        """
        from lager.io.gpio import power_up

        if mode not in ("plan", "apply"):
            raise ValueError(f"mode must be 'plan' or 'apply', got {mode!r}")
        backend = power_up.backend_for(self._instrument)
        if backend is None:
            raise power_up.PowerUpError(
                f"{self._instrument or 'this instrument'} has no power-up support; "
                f"power_up is supported on {power_up.supported_label()} gpio nets")
        report = backend.run(mode, list(nets or []),
                             allow_undeclared=bool(allow_undeclared))
        return {**backend.describe(), **report}


def create_device(net_info, **_):
    return PowerUpAdapter(net_info)

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for solar-simulator nets (create_device factory).

See ``adc_hs`` for why this is a role-unique top-level module. A solar net
drives PV-simulation mode on an EA PSB supply — the same physical VISA
instrument a power-supply net can point at — so the adapter must resolve
under hardware_service's *per-VISA-address* lock. The raw module name "ea"
would collide with ``lager.power.supply.ea`` in hardware_service's import
search (supply is tried before solar), hence this uniquely-named adapter.

hardware_service caches this adapter per (device_name, address), so the EA
driver's pyvisa session is opened once and reused; every call serializes
under the shared address lock with any supply/battery net on the same unit.
The EA driver's own file-based device lock stays in place as an inner guard
against out-of-process users.
"""
from __future__ import annotations

import time


class SolarHardwareAdapter:
    def __init__(self, address: str) -> None:
        self._address = address
        self._drv = None

    def _get(self):
        """Resolve (and cache) the EA solar driver for this address."""
        if self._drv is None:
            from lager.power.solar.ea import EA
            self._drv = EA(instr=self._address)
        return self._drv

    def set_mode(self):
        """Initialize and start PV simulation mode (with settle retries)."""
        drv = self._get()
        max_retries = 3
        for attempt in range(max_retries):
            try:
                drv.enable()
                return True
            except Exception as e:
                if attempt < max_retries - 1:
                    # EA devices need time to settle after a recent stop.
                    time.sleep(1.0 + attempt * 0.5)
                else:
                    raise RuntimeError(
                        "Failed to initialize solar simulator after %d attempts. "
                        "This may happen if the device was recently stopped. "
                        "Wait a few seconds and try again. Error: %s"
                        % (max_retries, e))

    def stop_mode(self):
        """Stop PV simulation and release the instrument's remote lock."""
        drv = self._get()
        try:
            drv.enable()  # Ensure we can communicate (may already be running)
        except Exception:
            pass  # Device might already be stopped — continue anyway
        try:
            drv.disable()
            # Give the device time to fully stop and release resources —
            # critical for rapid set/stop cycling.
            time.sleep(0.5)
            return True
        except Exception as e:
            # If the device reports it is already stopped, treat as success.
            try:
                status = drv.instr.query("FUNCtion:PHOTovoltaics:STATe?")
                if "STOP" in str(status) or "OFF" in str(status):
                    return True
            except Exception:
                pass
            raise RuntimeError("Failed to stop solar simulator: %s" % e)

    def irradiance(self, value=None):
        drv = self._get()
        drv.enable()
        return str(drv.irradiance(value=None if value is None else float(value)))

    def mpp_current(self):
        drv = self._get()
        drv.enable()
        return str(drv.mpp_current())

    def mpp_voltage(self):
        drv = self._get()
        drv.enable()
        return str(drv.mpp_voltage())

    def resistance(self, value=None):
        drv = self._get()
        drv.enable()
        if value is None:
            return str(drv.resistance())
        return str(drv.resistance(float(value)))

    def temperature(self):
        drv = self._get()
        drv.enable()
        return str(drv.temperature())

    def voc(self):
        drv = self._get()
        drv.enable()
        return str(drv.voc())

    def close(self):
        """Release the VISA session (called by hardware_service cache eviction)."""
        if self._drv is not None:
            try:
                self._drv.close()
            finally:
                self._drv = None


def create_device(net_info, **_):
    address = (net_info or {}).get("address")
    if not address:
        raise RuntimeError("Solar net has no VISA address")
    return SolarHardwareAdapter(address)

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
What to hand bleak when connecting to a device by address.

Shared by the box's BLE HTTP handlers (sessions, info, disconnect) and the
on-box script API (Central.connect/pair), so every connect path gets the
same fix. bleak is imported lazily so importing this module is cheap.
"""


async def ble_target(address):
    """What to hand BleakClient for `address`: BlueZ's own record, if it has one.

    Given a bare address, bleak's connect first scans and waits for BlueZ to
    announce the device. BlueZ only announces a device it already holds (for
    one this box connected to before, it keeps the record) when its RSSI moves
    by several dB. A device sitting still near the box then never shows up,
    and a second `info` or session to it fails with "not found". With BlueZ's
    record, bleak connects directly and skips the scan. Falls back to the
    address whenever the record is missing or cannot be read.
    """
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager
        from bleak.backends.device import BLEDevice

        manager = await get_global_bluez_manager()
        path = '%s/dev_%s' % (manager.get_default_adapter(),
                              address.upper().replace(':', '_').replace('-', '_'))
        props = manager._properties.get(path, {}).get('org.bluez.Device1')
    except Exception:  # noqa: BLE001 — a failed lookup just means "scan for it"
        return address
    if not props:
        return address
    return BLEDevice(props.get('Address', address), props.get('Alias'),
                     {'path': path, 'props': props}, props.get('RSSI', -127))

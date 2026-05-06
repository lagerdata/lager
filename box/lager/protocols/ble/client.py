# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    BLE async helpers
"""
import asyncio
import functools
from bleak import BleakScanner, BleakClient  # pylint: disable=import-error

def noop_handler(handle: int, data: bytearray):
    pass

def notify_handler(evt, messages, callback, max_messages, handle, data):
    if max_messages:
        if len(messages) >= max_messages:
            evt.set()
        else:
            callback(handle, data)
            messages.append(data)
    else:
        callback(handle, data)

async def waiter(event, timeout):
    await asyncio.wait_for(event.wait(), timeout=timeout)

class Client:
    """
        BLE Client class
    """
    def __init__(self, _client, *, loop):
        self.loop = loop
        self._client = _client

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def read_gatt_char(self, char_specifier):
        """read_gatt_char wrapper"""
        return self.loop.run_until_complete(self._client.read_gatt_char(char_specifier))

    def write_gatt_char(self, char_specifier, data):
        """write_gatt_char wrapper"""
        return self.loop.run_until_complete(self._client.write_gatt_char(char_specifier, data))

    def start_notify(self, char_specifier, callback=noop_handler, max_messages=None, timeout=None):
        """start_notify wrapper"""
        evt = asyncio.Event()
        messages = []
        handler = functools.partial(notify_handler, evt, messages, callback, max_messages)
        self.loop.run_until_complete(self._client.start_notify(char_specifier, handler))
        timed_out = False
        try:
            self.loop.run_until_complete(waiter(evt, timeout))
        except asyncio.TimeoutError:
            timed_out = True
        return timed_out, messages

    def stop_notify(self, char_specifier):
        """stop_notify wrapper"""
        return self.loop.run_until_complete(self._client.stop_notify(char_specifier))

    def connect(self, *args, **kwargs):
        """connect wrapper"""
        return self.loop.run_until_complete(self._client.connect(*args, **kwargs))

    def pair(self, *args, **kwargs):
        """pair wrapper"""
        return self.loop.run_until_complete(self._client.pair(*args, **kwargs))

    def disconnect(self, *args, **kwargs):
        """disconnect wrapper"""
        return self.loop.run_until_complete(self._client.disconnect(*args, **kwargs))

    def sleep(self, timeout):
        """sleep wrapper"""
        return self.loop.run_until_complete(asyncio.sleep(timeout))

    def get_services(self, *args, **kwargs):
        """get_services wrapper"""
        return self.loop.run_until_complete(self._client.get_services(*args, **kwargs))

    def has_characteristic(self, uuid):
        """ check if uuid exists as a characteristic"""
        for service in self._client.services:
            for characteristic in service.characteristics:
                if characteristic.uuid == uuid:
                    return True
        return False

class Central:
    """
        BLE Central object
    """
    def __init__(self, *, loop=None):
        if loop is None:
            loop = asyncio.get_event_loop()
        self.loop = loop
        self._client = None

    def scan(self, scan_time=5.0, name=None, address=None):
        """
            Scan for devices. If name or address are provided, remove
            devices that do not match.
        """
        devices = self.loop.run_until_complete(BleakScanner.discover(timeout=scan_time))
        if name is not None:
            devices = [device for device in devices if device.name == name]
        if address is not None:
            devices = [device for device in devices if device.address == address]
        return devices

    def connect(self, address, *args, **kwargs):
        """connect to `address`"""
        return Client(BleakClient(address), loop=self.loop).connect(*args, **kwargs)

    def pair(self, address, *args, **kwargs):
        """pair to `address`"""
        return Client(BleakClient(address), loop=self.loop).pair(*args, **kwargs)

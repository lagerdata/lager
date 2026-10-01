# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
A BLE test peripheral that runs on a Lager Box, for exercising BLE sessions.

Run it on a box whose Bluetooth adapter is in radio range of the box under
test, then drive it from that box with `lager ble session`:

    lager python test/assets/ble_peer/ble_peer.py --box <PEER-BOX> --detach
    lager python --kill-all --box <PEER-BOX>          # stop it

It uses only dbus-fast (a bleak dependency, so it is in every box image) and
the host's bluetoothd over the mounted system bus. Optional arguments:
`[duration_seconds] [adapter]` (defaults: 3600, hci0).

It re-advertises after each central disconnects. Once in testing, after the
peer itself dropped the link (CTRL 01), it logged "advertising again" but was
no longer seen by scans until it was restarted; if the box under test cannot
find the peer, kill it and start it again.

It advertises as `lager-ble-peer` with one service:

    ...def1  ECHO   notify           every write to RX comes back here,
                                     split into MTU-3 notifications
    ...def2  RX     write, write-without-response
    ...def3  CTRL   write            01          -> drop the link after 0.5 s
                                     02 HI LO    -> burst HI<<8|LO notifications
                                                    on TICK, back to back
    ...def4  TICK   notify, read     a 4-byte big-endian counter, sent every
                                     2 s by itself while subscribed
    ...def5  INFO   read             b"lager-ble-peer"

UUIDs are 12345678-1234-5678-1234-56789abcdefN.
"""
import asyncio
import sys
import time

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

BLUEZ = 'org.bluez'
APP_PATH = '/com/lager/blepeer'
ADV_PATH = '/com/lager/blepeer/adv0'
NAME = 'lager-ble-peer'


def uuid(n):
    return '12345678-1234-5678-1234-56789abcdef%d' % n


def log(*args):
    print('[%s]' % time.strftime('%H:%M:%S'), *args, flush=True)


class Service(ServiceInterface):
    def __init__(self, path, uuid_):
        super().__init__('org.bluez.GattService1')
        self.path = path
        self._uuid = uuid_

    @dbus_property(access=PropertyAccess.READ, name='UUID')
    def uuid_prop(self) -> 's':
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name='Primary')
    def primary(self) -> 'b':
        return True


class Characteristic(ServiceInterface):
    def __init__(self, peer, path, service_path, uuid_, flags, on_write=None, value=b''):
        super().__init__('org.bluez.GattCharacteristic1')
        self.peer = peer
        self.path = path
        self._service = service_path
        self._uuid = uuid_
        self._flags = flags
        self._on_write = on_write
        self.value = value
        self.notifying = False

    @dbus_property(access=PropertyAccess.READ, name='UUID')
    def uuid_prop(self) -> 's':
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name='Service')
    def service_prop(self) -> 'o':
        return self._service

    @dbus_property(access=PropertyAccess.READ, name='Flags')
    def flags_prop(self) -> 'as':
        return self._flags

    @dbus_property(access=PropertyAccess.READ, name='Value')
    def value_prop(self) -> 'ay':
        return self.value

    @method(name='ReadValue')
    def read_value(self, options: 'a{sv}') -> 'ay':
        return self.value

    @method(name='WriteValue')
    def write_value(self, value: 'ay', options: 'a{sv}'):
        if self._on_write is not None:
            self._on_write(bytes(value), options)

    @method(name='StartNotify')
    def start_notify(self):
        self.notifying = True
        log('subscribed:', self._uuid)

    @method(name='StopNotify')
    def stop_notify(self):
        self.notifying = False
        log('unsubscribed:', self._uuid)

    def notify(self, data):
        self.value = bytes(data)
        if self.notifying:
            self.emit_properties_changed({'Value': self.value})


class Advertisement(ServiceInterface):
    def __init__(self):
        super().__init__('org.bluez.LEAdvertisement1')

    @dbus_property(access=PropertyAccess.READ, name='Type')
    def type_prop(self) -> 's':
        return 'peripheral'

    @dbus_property(access=PropertyAccess.READ, name='LocalName')
    def local_name(self) -> 's':
        return NAME

    # Sets the LE General Discoverable flag, as ordinary peripherals do.
    # Without it a central's BlueZ reports the device while scanning but does
    # not keep it, so bleak's connect (scan, then look the device up) fails
    # with "device 'dev_...' not found".
    @dbus_property(access=PropertyAccess.READ, name='Discoverable')
    def discoverable(self) -> 'b':
        return True

    @method(name='Release')
    def release(self):
        log('advertisement released by BlueZ')


def _option(options, key, default):
    v = options.get(key)
    return v.value if isinstance(v, Variant) else default


class Peer:
    def __init__(self, bus, adapter):
        self.bus = bus
        self.adapter_path = '/org/bluez/' + adapter
        svc = APP_PATH + '/service0'
        self.service = Service(svc, uuid(0))
        self.echo = Characteristic(self, svc + '/char1', svc, uuid(1), ['notify'])
        self.rx = Characteristic(self, svc + '/char2', svc, uuid(2),
                                 ['write', 'write-without-response'], on_write=self.on_rx)
        self.ctrl = Characteristic(self, svc + '/char3', svc, uuid(3), ['write'],
                                   on_write=self.on_ctrl)
        self.tick = Characteristic(self, svc + '/char4', svc, uuid(4), ['notify', 'read'],
                                   value=(0).to_bytes(4, 'big'))
        self.info = Characteristic(self, svc + '/char5', svc, uuid(5), ['read'],
                                   value=NAME.encode())
        self.counter = 0

    def export(self):
        for obj in (self.service, self.echo, self.rx, self.ctrl, self.tick, self.info):
            self.bus.export(obj.path, obj)
        self.bus.export(ADV_PATH, Advertisement())

    async def call(self, path, interface, member, signature='', body=None):
        reply = await self.bus.call(Message(destination=BLUEZ, path=path, interface=interface,
                                            member=member, signature=signature,
                                            body=body or []))
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError('%s.%s: %s %s' % (interface, member, reply.error_name, reply.body))
        return reply

    async def register(self):
        await self.call(self.adapter_path, 'org.freedesktop.DBus.Properties', 'Set', 'ssv',
                        ['org.bluez.Adapter1', 'Powered', Variant('b', True)])
        await self.call(self.adapter_path, 'org.bluez.GattManager1', 'RegisterApplication',
                        'oa{sv}', [APP_PATH, {}])
        await self.call(self.adapter_path, 'org.bluez.LEAdvertisingManager1',
                        'RegisterAdvertisement', 'oa{sv}', [ADV_PATH, {}])
        # Watch centrals connect and disconnect (Device1.Connected).
        await self.bus.call(Message(
            destination='org.freedesktop.DBus', path='/org/freedesktop/DBus',
            interface='org.freedesktop.DBus', member='AddMatch', signature='s',
            body=["type='signal',sender='org.bluez',"
                  "interface='org.freedesktop.DBus.Properties',"
                  "member='PropertiesChanged',arg0='org.bluez.Device1'"]))
        self.bus.add_message_handler(self.on_signal)

    def on_signal(self, msg):
        if (msg.message_type != MessageType.SIGNAL or msg.member != 'PropertiesChanged'
                or not msg.body or msg.body[0] != 'org.bluez.Device1'):
            return
        connected = msg.body[1].get('Connected')
        if connected is None:
            return
        log('central %s %s' % (msg.path.rsplit('/', 1)[-1],
                               'connected' if connected.value else 'disconnected'))
        if not connected.value:
            asyncio.get_running_loop().create_task(self.readvertise())

    async def readvertise(self):
        """Advertise again after a central leaves.

        The controller stops advertising when a central connects, and BlueZ
        does not always restart a registered advertisement when it goes, so
        the peer would be connectable exactly once. Re-registering fixes it.
        """
        await asyncio.sleep(0.5)
        try:
            await self.call(self.adapter_path, 'org.bluez.LEAdvertisingManager1',
                            'UnregisterAdvertisement', 'o', [ADV_PATH])
        except RuntimeError:
            pass
        try:
            await self.call(self.adapter_path, 'org.bluez.LEAdvertisingManager1',
                            'RegisterAdvertisement', 'oa{sv}', [ADV_PATH, {}])
            log('advertising again')
        except RuntimeError as e:
            log('re-advertise failed:', e)

    # -- handlers (run on the event loop) --

    def on_rx(self, data, options):
        mtu = _option(options, 'mtu', 23)
        kind = _option(options, 'type', '?')
        log('RX %d byte(s) (%s, mtu %d): %s' % (len(data), kind, mtu, data.hex()))
        size = max(mtu - 3, 1)
        for i in range(0, len(data), size):
            self.echo.notify(data[i:i + size])

    def on_ctrl(self, data, options):
        log('CTRL', data.hex())
        loop = asyncio.get_running_loop()
        if data[:1] == b'\x01':
            device = _option(options, 'device', None)
            if device:
                loop.create_task(self.disconnect(device))
        elif data[:1] == b'\x02' and len(data) >= 3:
            loop.create_task(self.burst(int.from_bytes(data[1:3], 'big')))

    async def disconnect(self, device):
        await asyncio.sleep(0.5)
        log('dropping the link to', device)
        try:
            await self.call(device, 'org.bluez.Device1', 'Disconnect')
        except RuntimeError as e:
            log('disconnect failed:', e)

    async def burst(self, count):
        log('burst of %d on TICK' % count)
        for _ in range(count):
            self.counter += 1
            self.tick.notify(self.counter.to_bytes(4, 'big'))
            await asyncio.sleep(0)

    async def ticker(self):
        while True:
            await asyncio.sleep(2.0)
            if self.tick.notifying:
                self.counter += 1
                self.tick.notify(self.counter.to_bytes(4, 'big'))


async def main():
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 3600.0
    adapter = sys.argv[2] if len(sys.argv) > 2 else 'hci0'
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    peer = Peer(bus, adapter)
    peer.export()
    await peer.register()
    log('advertising as %r on %s for %.0fs; service %s' % (NAME, adapter, duration, uuid(0)))
    ticker = asyncio.ensure_future(peer.ticker())
    try:
        await asyncio.sleep(duration)
    finally:
        ticker.cancel()
        log('stopping')


if __name__ == '__main__':
    asyncio.run(main())

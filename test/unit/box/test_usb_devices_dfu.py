# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the sysfs USB enumeration (GET /usb/devices) and box-side
dfu-util (POST /usb/dfu) handlers in box/lager/http_handlers/usb.py.

The sysfs walk runs against a fixture directory built in tmp; dfu-util runs
are mocked at the subprocess boundary (``_run_dfu_util``), so no hardware
or dfu-util install is needed.
"""

import base64
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    parts = dotted.split('.')
    for i in range(1, len(parts) + 1):
        key = '.'.join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


_HARDWARE_STUBS = [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
    'flask_socketio',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from flask import Flask  # noqa: E402
from lager.http_handlers import usb as usb_handler  # noqa: E402


def _write_sysfs_device(root, name, attrs):
    dev_dir = os.path.join(root, name)
    os.makedirs(dev_dir, exist_ok=True)
    for attr, value in attrs.items():
        with open(os.path.join(dev_dir, attr), 'w') as fh:
            fh.write(value + '\n')


def _make_sysfs_fixture(root):
    """A root hub, a DUT with an iSerial, a device with no serial, and an
    interface node that must be skipped."""
    _write_sysfs_device(root, 'usb1', {
        'idVendor': '1d6b', 'idProduct': '0002', 'busnum': '1',
        'devnum': '1', 'devpath': '0', 'bDeviceClass': '09', 'speed': '480',
        'product': 'xHCI Host Controller', 'serial': '0000:00:14.0',
    })
    _write_sysfs_device(root, '1-1.4', {
        'idVendor': '0483', 'idProduct': 'df11', 'busnum': '1',
        'devnum': '42', 'devpath': '1.4', 'bDeviceClass': '00',
        'speed': '12', 'product': 'STM32 BOOTLOADER',
        'manufacturer': 'STMicroelectronics', 'serial': 'STM32-DUT-01',
    })
    _write_sysfs_device(root, '1-1.2', {
        'idVendor': '0403', 'idProduct': '6001', 'busnum': '1',
        'devnum': '7', 'devpath': '1.2', 'bDeviceClass': '00', 'speed': '12',
        'product': 'FT232R USB UART',
    })
    # Interface node: must never appear in results.
    _write_sysfs_device(root, '1-1.4:1.0', {'bInterfaceClass': 'fe'})
    # A non-device entry with no idVendor (e.g. a symlink target dir).
    os.makedirs(os.path.join(root, 'not-a-device'), exist_ok=True)


class EnumerateUsbDevicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _make_sysfs_fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enumerates_devices_and_skips_interfaces(self):
        devices = usb_handler.enumerate_usb_devices(sysfs_root=self.tmp.name)
        names = [d['sysfs_name'] for d in devices]
        self.assertEqual(names, ['1-1.2', '1-1.4', 'usb1'])
        dut = next(d for d in devices if d['sysfs_name'] == '1-1.4')
        self.assertEqual(dut['vid'], '0483')
        self.assertEqual(dut['pid'], 'df11')
        self.assertEqual(dut['serial'], 'STM32-DUT-01')
        self.assertEqual(dut['product'], 'STM32 BOOTLOADER')
        self.assertEqual(dut['speed'], '12')
        # Missing descriptors surface as None, not as errors.
        ftdi = next(d for d in devices if d['sysfs_name'] == '1-1.2')
        self.assertIsNone(ftdi['serial'])
        self.assertIsNone(ftdi['manufacturer'])

    def test_vid_pid_filters_accept_0x_and_case(self):
        devices = usb_handler.enumerate_usb_devices(
            sysfs_root=self.tmp.name, vid='0x0483', pid='DF11',
        )
        self.assertEqual([d['sysfs_name'] for d in devices], ['1-1.4'])

    def test_vid_pid_filters_accept_short_hex(self):
        # Query ``vid=483`` must still match sysfs ``0483``.
        devices = usb_handler.enumerate_usb_devices(
            sysfs_root=self.tmp.name, vid='483', pid='df11',
        )
        self.assertEqual([d['sysfs_name'] for d in devices], ['1-1.4'])

    def test_serial_filter_is_exact(self):
        devices = usb_handler.enumerate_usb_devices(
            sysfs_root=self.tmp.name, serial='STM32-DUT-01',
        )
        self.assertEqual(len(devices), 1)
        self.assertEqual(
            usb_handler.enumerate_usb_devices(
                sysfs_root=self.tmp.name, serial='STM32-DUT'),
            [],
        )

    def test_missing_sysfs_root_returns_empty(self):
        self.assertEqual(
            usb_handler.enumerate_usb_devices(sysfs_root='/nonexistent'), [],
        )


class UsbDevicesRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _make_sysfs_fixture(self.tmp.name)
        app = Flask(__name__)
        usb_handler.register_usb_routes(app)
        self.client = app.test_client()
        self._root_patch = patch.object(
            usb_handler, '_SYSFS_USB_ROOT', self.tmp.name)
        self._root_patch.start()

    def tearDown(self):
        self._root_patch.stop()
        self.tmp.cleanup()

    def test_get_devices(self):
        resp = self.client.get('/usb/devices')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(len(body['devices']), 3)

    def test_get_devices_with_filters(self):
        resp = self.client.get('/usb/devices?vid=0483&serial=STM32-DUT-01')
        body = resp.get_json()
        self.assertEqual(len(body['devices']), 1)
        self.assertEqual(body['devices'][0]['sysfs_name'], '1-1.4')


_DFU_LIST_STDOUT = '''dfu-util 0.11

Found DFU: [0483:df11] ver=2200, devnum=42, cfg=1, intf=0, path="1-1.4", \
alt=1, name="@Option Bytes  /0x1FFF7800/01*040 e", serial="STM32-DUT-01"
Found DFU: [0483:df11] ver=2200, devnum=42, cfg=1, intf=0, path="1-1.4", \
alt=0, name="@Internal Flash  /0x08000000/256*0002Kg", serial="STM32-DUT-01"
Found Runtime: [0483:374b] ver=0100, devnum=9, cfg=1, intf=3, path="1-1.2", \
alt=0, name="UNKNOWN", serial="066FFF383333"
'''


class DfuParsingAndArgsTests(unittest.TestCase):
    def test_parse_dfu_list(self):
        devices = usb_handler._parse_dfu_list(_DFU_LIST_STDOUT)
        self.assertEqual(len(devices), 3)
        flash = devices[1]
        self.assertEqual(flash['mode'], 'DFU')
        self.assertEqual(flash['vid'], '0483')
        self.assertEqual(flash['pid'], 'df11')
        self.assertEqual(flash['devnum'], 42)
        self.assertEqual(flash['alt'], 0)
        self.assertEqual(flash['path'], '1-1.4')
        self.assertEqual(flash['serial'], 'STM32-DUT-01')
        self.assertEqual(
            flash['name'], '@Internal Flash  /0x08000000/256*0002Kg')
        self.assertEqual(devices[2]['mode'], 'Runtime')

    def test_build_list_args(self):
        self.assertEqual(
            usb_handler._build_dfu_args('list', {}), ['dfu-util', '-l'])
        self.assertEqual(
            usb_handler._build_dfu_args(
                'list', {'vid_pid': '0483:df11', 'serial': 'S1'}),
            ['dfu-util', '-d', '0483:df11', '-S', 'S1', '-l'],
        )

    def test_build_download_args(self):
        args = usb_handler._build_dfu_args(
            'download',
            {'vid_pid': '0483:df11', 'alt': 0,
             'dfuse_address': '0x08000000:leave', 'reset': True},
            firmware_path='/tmp/fw.bin',
        )
        self.assertEqual(args, [
            'dfu-util', '-d', '0483:df11', '-a', '0',
            '-s', '0x08000000:leave', '-D', '/tmp/fw.bin', '-R',
        ])

    def test_build_detach_args(self):
        self.assertEqual(
            usb_handler._build_dfu_args('detach', {'alt': 1}),
            ['dfu-util', '-a', '1', '-e'],
        )


class DfuRouteTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        usb_handler.register_usb_routes(app)
        self.client = app.test_client()
        self._which = patch.object(
            usb_handler.shutil, 'which', return_value='/usr/bin/dfu-util')
        self._which.start()

    def tearDown(self):
        self._which.stop()

    def test_list_parses_devices(self):
        with patch.object(
            usb_handler, '_run_dfu_util',
            return_value=(0, _DFU_LIST_STDOUT, ''),
        ) as run:
            resp = self.client.post('/usb/dfu', json={'action': 'list'})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(len(body['value']['devices']), 3)
        run.assert_called_once()
        # argv[0] is the resolved which() path, not the bare 'dfu-util'.
        self.assertEqual(run.call_args[0][0], ['/usr/bin/dfu-util', '-l'])

    def test_download_writes_temp_firmware_and_builds_args(self):
        firmware = b'\x00\xff\x10firmware'
        captured = {}

        def fake_run(args, timeout):
            captured['args'] = args
            with open(args[args.index('-D') + 1], 'rb') as fh:
                captured['firmware'] = fh.read()
            return 0, 'Download done.', ''

        with patch.object(usb_handler, '_run_dfu_util', side_effect=fake_run):
            resp = self.client.post('/usb/dfu', json={
                'action': 'download',
                'params': {
                    'vid_pid': '0483:df11', 'alt': 0,
                    'dfuse_address': '0x08000000:leave',
                    'firmware': base64.b64encode(firmware).decode(),
                    'filename': 'app.bin',
                },
            })
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['value']['exit_code'], 0)
        self.assertEqual(captured['firmware'], firmware)
        self.assertEqual(captured['args'][:5],
                         ['/usr/bin/dfu-util', '-d', '0483:df11', '-a', '0'])
        # The temp firmware file is removed after the run.
        self.assertFalse(
            os.path.exists(captured['args'][captured['args'].index('-D') + 1]))

    def test_download_without_firmware_is_400(self):
        resp = self.client.post(
            '/usb/dfu', json={'action': 'download', 'params': {}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('firmware', resp.get_json()['error'])

    def test_invalid_action_is_400(self):
        resp = self.client.post('/usb/dfu', json={'action': 'upload'})
        self.assertEqual(resp.status_code, 400)

    def test_missing_dfu_util_is_500_with_install_hint(self):
        with patch.object(usb_handler.shutil, 'which', return_value=None):
            resp = self.client.post('/usb/dfu', json={'action': 'list'})
        self.assertEqual(resp.status_code, 500)
        body = resp.get_json()
        self.assertIn('dfu-util-missing', body['error'])
        self.assertIn('lager box-config apt add dfu-util', body['error'])

    def test_missing_dfu_util_on_download_leaves_no_temp_file(self):
        # Regression: firmware used to be written before the which() check,
        # leaking a lager-dfu-* file in /tmp on every missing-binary 500.
        before = {
            p for p in os.listdir(tempfile.gettempdir())
            if p.startswith('lager-dfu-')
        }
        with patch.object(usb_handler.shutil, 'which', return_value=None):
            resp = self.client.post('/usb/dfu', json={
                'action': 'download',
                'params': {
                    'firmware': base64.b64encode(b'firmware').decode(),
                    'filename': 'app.bin',
                },
            })
        self.assertEqual(resp.status_code, 500)
        after = {
            p for p in os.listdir(tempfile.gettempdir())
            if p.startswith('lager-dfu-')
        }
        self.assertEqual(after, before)

    def test_non_dict_params_is_400(self):
        resp = self.client.post(
            '/usb/dfu', json={'action': 'list', 'params': ['not', 'a', 'dict']})
        self.assertEqual(resp.status_code, 400)

    def test_nonzero_exit_is_502_with_stderr_tail(self):
        with patch.object(
            usb_handler, '_run_dfu_util',
            return_value=(74, '', 'dfu-util: No DFU capable USB device available'),
        ):
            resp = self.client.post('/usb/dfu', json={'action': 'detach'})
        self.assertEqual(resp.status_code, 502)
        body = resp.get_json()
        self.assertFalse(body['success'])
        self.assertIn('No DFU capable USB device', body['error'])
        self.assertEqual(body['value']['exit_code'], 74)


class SelfRestartGateTests(unittest.TestCase):
    """A service restart repairs exactly one thing: a USB handle THIS process
    orphaned across a re-enumeration. A driver that opens and closes inside
    every call has none, so restarting on its behalf cannot help — it only
    drops every other in-flight operation the service is holding.

    Measured on a two-hub bench: a hub that would not open triggered a restart,
    and the respawned process, with a brand-new libusb context, failed
    identically 37s later.
    """

    def _run(self, controller, netname='usb1'):
        """Call the gate with a fake controller; returns whether it restarted."""
        info = {netname: {'address': 'USB0::0x24FF::0x0011::E6BACCD5::INSTR'}}
        with patch.dict(sys.modules), \
                patch.object(usb_handler._self_restart, 'maybe_self_restart') as m:
            disp = types.ModuleType('lager.automation.usb_hub.dispatcher')
            disp._load_net_definitions = lambda: info
            disp._controller_for = lambda _info: controller
            sys.modules['lager.automation.usb_hub.dispatcher'] = disp
            usb_handler._self_restart_if_wedged(
                netname, 'state', RuntimeError('hub would not open'))
            return m.called

    def test_a_stateless_driver_does_not_trigger_a_restart(self):
        class _Stateless:
            holds_usb_context_between_ops = False

        self.assertFalse(self._run(_Stateless()))

    def test_a_stateful_driver_still_triggers_the_restart(self):
        class _Stateful:
            holds_usb_context_between_ops = True

        self.assertTrue(self._run(_Stateful()))

    def test_a_driver_that_declares_nothing_is_treated_as_stateful(self):
        """Fails safe: only a positive declaration exempts a driver, so an
        unmodified third-party driver keeps today's behaviour."""
        class _Silent:
            pass

        self.assertTrue(self._run(_Silent()))

    def test_an_unresolvable_controller_is_treated_as_stateful(self):
        """The gate runs on an error path and must never make it worse."""
        def _boom(_info):
            raise RuntimeError('no such driver')

        info = {'usb1': {'address': 'USB0::0x24FF::0x0011::E6BACCD5::INSTR'}}
        with patch.dict(sys.modules), \
                patch.object(usb_handler._self_restart,
                             'maybe_self_restart') as m:
            disp = types.ModuleType('lager.automation.usb_hub.dispatcher')
            disp._load_net_definitions = lambda: info
            disp._controller_for = _boom
            sys.modules['lager.automation.usb_hub.dispatcher'] = disp
            usb_handler._self_restart_if_wedged('usb1', 'state', RuntimeError('x'))
        self.assertTrue(m.called)

    def test_the_acroname_driver_declares_itself_stateful(self):
        """Ties the gate to the real driver, so moving the flag breaks a test
        rather than silently changing the restart policy. Since the bounded
        session hold (a connection parked for the idle window after a one-shot
        operation), a re-enumeration CAN briefly orphan a held Acroname
        handle, so the restart path must stay reachable for this driver."""
        from lager.automation.usb_hub.acroname import AcronameUSBNet
        self.assertTrue(AcronameUSBNet.holds_usb_context_between_ops)


if __name__ == '__main__':
    unittest.main()


class CycleMessageTests(unittest.TestCase):
    """What POST /usb/command {action: cycle} tells the operator.

    The CLI echoes `message` verbatim, so this mapping is the whole of what a
    person sees. `None` from the driver carries two different facts -- "nothing
    is attached to this port" and "this box could not read its own USB
    topology" -- and only the first justifies the words "no device on this
    port". Asserting the empty claim while a device was present is what sent
    #417's investigation at the device rather than at the tool.
    """

    def setUp(self):
        app = Flask(__name__)
        usb_handler.register_usb_routes(app)
        self.client = app.test_client()

    def _cycle(self, result, bus=('a-device',)):
        with patch.object(usb_handler.usb_hub, 'cycle', return_value=result), \
             patch.object(usb_handler, 'enumerate_usb_devices',
                          return_value=list(bus)):
            resp = self.client.post('/usb/command',
                                    json={'netname': 'usb1', 'action': 'cycle'})
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_a_confirmed_return_says_so(self):
        body = self._cycle(True)
        self.assertIn('device re-enumerated', body['message'])
        self.assertIs(body['reconnected'], True)

    def test_a_device_that_did_not_come_back_says_so(self):
        body = self._cycle(False)
        self.assertIn('did not come back before the timeout', body['message'])
        self.assertIs(body['reconnected'], False)

    def test_an_empty_port_is_only_claimed_when_the_bus_was_readable(self):
        body = self._cycle(None, bus=('a-device',))
        self.assertIn('no device on this port', body['message'])
        # None is not carried on the wire; an older client sees no new field.
        self.assertNotIn('reconnected', body)

    def test_an_unreadable_bus_does_not_claim_the_port_is_empty(self):
        """The regression, in the words a person actually reads."""
        body = self._cycle(None, bus=())
        self.assertNotIn('no device on this port', body['message'])
        self.assertIn('re-enumeration not verified', body['message'])
        self.assertIn('could not be read', body['message'])

    def test_the_port_is_reported_powered_whatever_the_verdict(self):
        """A cycle always ends powered; the verdict is about the device."""
        for result in (True, False, None):
            with self.subTest(result=result):
                self.assertEqual(self._cycle(result)['state'], 'enabled')

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the custom-driver routes and /net/command dispatch.

Actions run in real runner child processes (forked from the runner's
forkserver) against a real temp DRIVERS_DIR, so these cover the process
boundary: stray prints, driver exceptions, crashes and timeouts.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from flask import Flask

from lager.drivers import registry
from lager.http_handlers import drivers_handler, net_command
from lager.nets.net import Net

MANIFEST = {
    'type': 'stepper',
    'version': '1.0.0',
    'lager_api': 1,
    'params': {
        'uart_net': {'type': 'net', 'role': 'uart', 'required': True},
        'steps_per_mm': {'type': 'int', 'default': 80},
    },
    'actions': {
        'info': {},
        'jog': {'args': {'seconds': {'type': 'float', 'min': 0.1, 'max': 30,
                                     'required': True}}},
        'noisy': {},
        'fail': {},
        'hang': {'timeout_s': 1},
        'plain': {},
        'helper': {},
        'whoami': {},
        'crash': {},
    },
}

DRIVER = '''
import os
import time
from lager.drivers import CustomDriver, action

class Driver(CustomDriver):
    @action
    def info(self):
        return {"message": "ok", "params": self.params,
                "base": sorted(self._base_nets), "name": self.name}

    @action
    def jog(self, seconds):
        return {"message": "jogged %s s" % seconds, "seconds": seconds}

    @action
    def noisy(self):
        print("stray output that must not corrupt the result")
        return {"message": "quiet result"}

    @action
    def fail(self):
        raise RuntimeError("controller did not answer")

    @action
    def hang(self):
        time.sleep(30)

    @action
    def plain(self):
        return 42

    @action
    def whoami(self):
        return {"pid": os.getpid(), "ppid": os.getppid(), "cwd": os.getcwd(),
                "secret": os.environ.get("LAGER_SECRET_TOKEN")}

    @action
    def crash(self):
        os._exit(3)

    def helper(self):
        return "not an action"
'''

SAVED_NETS = [
    {'name': 'RIG_1', 'role': 'stepper', 'instrument': 'custom:stepper',
     'params': {'uart_net': 'UART_1'}},
    {'name': 'RIG_BAD', 'role': 'stepper', 'instrument': 'custom:stepper',
     'params': {'uart_net': 'SUPPLY_1'}},
    {'name': 'UART_1', 'role': 'uart', 'instrument': 'FTDI', 'pin': '0'},
    {'name': 'SUPPLY_1', 'role': 'power-supply', 'instrument': 'Rigol_DP821'},
]


def make_bundle(manifest=None, driver=DRIVER):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('lager_driver.json', json.dumps(manifest or MANIFEST))
        z.writestr('driver.py', driver)
    return buf.getvalue()


class HandlerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = registry.DRIVERS_DIR
        registry.DRIVERS_DIR = os.path.join(self.tmp, 'drivers')
        app = Flask(__name__)
        drivers_handler.register_drivers_routes(app)
        net_command.register_net_command_routes(app)
        self.client = app.test_client()
        self.nets = list(SAVED_NETS)
        patches = [
            mock.patch.object(Net, 'get_local_nets', side_effect=lambda: self.nets),
            mock.patch.object(drivers_handler, '_release_usb_claims'),
            mock.patch.object(drivers_handler, '_secret_env', return_value={}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        registry.DRIVERS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install(self, data=None, dtype='stepper'):
        return self.client.put('/drivers/%s' % dtype, data={
            'bundle': (io.BytesIO(data or make_bundle()), 'bundle.zip')},
            content_type='multipart/form-data')

    def command(self, action, params=None, netname='RIG_1'):
        return self.client.post('/net/command', json={
            'netname': netname, 'action': action, 'params': params or {}})


class TestRoutes(HandlerCase):
    def test_install_list_get_remove(self):
        resp = self.install()
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertEqual(resp.get_json()['type'], 'stepper')

        listed = self.client.get('/drivers').get_json()['drivers']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['nets'], ['RIG_1', 'RIG_BAD'])
        self.assertIn('jog', listed[0]['manifest']['actions'])

        one = self.client.get('/drivers/stepper')
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.get_json()['version'], '1.0.0')
        self.assertEqual(self.client.get('/drivers/nope').status_code, 404)

        refused = self.client.delete('/drivers/stepper')
        self.assertEqual(refused.status_code, 409)
        forced = self.client.delete('/drivers/stepper?force=1')
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(forced.get_json()['orphaned_nets'], ['RIG_1', 'RIG_BAD'])
        self.assertEqual(self.client.get('/drivers').get_json()['drivers'], [])

    def test_install_needs_bundle_field(self):
        resp = self.client.put('/drivers/stepper', data={},
                               content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 400)

    def test_install_bad_bundle(self):
        self.assertEqual(self.install(b'not a zip').status_code, 400)
        self.assertEqual(self.install(dtype='uart').status_code, 400)


class TestNetCommandDispatch(HandlerCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.install().status_code, 200)

    def test_action_runs_in_subprocess_with_typed_params(self):
        resp = self.command('info')
        body = resp.get_json()
        self.assertEqual(resp.status_code, 200, body)
        self.assertTrue(body['success'])
        self.assertEqual(body['message'], 'ok')
        self.assertEqual(body['value']['params'], {'uart_net': 'UART_1', 'steps_per_mm': 80})
        self.assertEqual(body['value']['base'], ['uart_net'])
        self.assertEqual(body['value']['name'], 'RIG_1')

    def test_args_are_coerced_from_cli_strings(self):
        body = self.command('jog', {'seconds': '2.5'}).get_json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['value']['seconds'], 2.5)
        self.assertEqual(body['message'], 'jogged 2.5 s')

    def test_bad_args_are_400_before_spawning(self):
        with mock.patch.object(drivers_handler, 'run_action') as run:
            for params in ({}, {'seconds': 99}, {'seconds': 1, 'speed': 2}):
                with self.subTest(params=params):
                    self.assertEqual(self.command('jog', params).status_code, 400)
            run.assert_not_called()

    def test_unknown_action_is_400(self):
        resp = self.command('explode')
        self.assertEqual(resp.status_code, 400)
        self.assertIn("Unknown action 'explode'", resp.get_json()['error'])

    def test_unmarked_method_cannot_be_called(self):
        resp = self.command('helper')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('no @action method', resp.get_json()['error'])

    def test_stray_print_does_not_corrupt_result(self):
        body = self.command('noisy').get_json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['message'], 'quiet result')

    def test_driver_exception_is_502_with_its_message(self):
        resp = self.command('fail')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('RuntimeError: controller did not answer', resp.get_json()['error'])

    def test_timeout_is_504(self):
        resp = self.command('hang')
        self.assertEqual(resp.status_code, 504)
        self.assertIn('did not finish within 1 s', resp.get_json()['error'])

    def test_scalar_return_becomes_message_and_value(self):
        body = self.command('plain').get_json()
        self.assertEqual((body['message'], body['value']), ('42', 42))

    def test_base_net_of_wrong_role_is_400(self):
        resp = self.command('info', netname='RIG_BAD')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('needs a uart net', resp.get_json()['error'])

    def test_missing_base_net_is_400(self):
        self.nets = [dict(SAVED_NETS[0])]
        resp = self.command('info')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('does not exist', resp.get_json()['error'])

    def test_busy_uart_session_is_409(self):
        from lager.http_handlers import uart as uart_handler
        sessions = {'sid1': {'netname': 'UART_1'}}
        with mock.patch.object(uart_handler, 'active_uart_sessions', sessions), \
                mock.patch.object(uart_handler, '_reclaim_if_stale', return_value=False), \
                mock.patch.object(drivers_handler, 'run_action') as run:
            resp = self.command('info')
        self.assertEqual(resp.status_code, 409)
        self.assertIn("UART net 'UART_1' is in use", resp.get_json()['error'])
        run.assert_not_called()

    def test_uninstalled_driver_role_is_still_501(self):
        self.nets = [{'name': 'X', 'role': 'mystery', 'instrument': 'custom:mystery'}]
        self.assertEqual(self.command('info', netname='X').status_code, 501)

    def test_broken_driver_is_503(self):
        with open(os.path.join(registry.driver_dir('stepper'), 'lager_driver.json'), 'w') as f:
            f.write('{oops')
        resp = self.command('info')
        self.assertEqual(resp.status_code, 503)
        self.assertIn('failed to load', resp.get_json()['error'])

    def test_import_error_in_driver_is_502(self):
        self.assertEqual(self.install(make_bundle(driver='import not_a_module\n')).status_code, 200)
        resp = self.command('info')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('failed to import', resp.get_json()['error'])

    def test_each_call_is_a_fresh_child_of_a_warm_forkserver(self):
        first = self.command('whoami').get_json()['value']
        second = self.command('whoami').get_json()['value']
        self.assertNotEqual(first['pid'], second['pid'])
        self.assertNotEqual(first['pid'], os.getpid())
        # Both forked from the same long-lived server, not spawned by us.
        self.assertEqual(first['ppid'], second['ppid'])
        self.assertNotEqual(first['ppid'], os.getpid())

    def test_child_gets_secrets_and_runs_in_the_bundle_dir(self):
        with mock.patch.object(drivers_handler, '_secret_env',
                               return_value={'LAGER_SECRET_TOKEN': 's3'}):
            value = self.command('whoami').get_json()['value']
        self.assertEqual(value['secret'], 's3')
        self.assertEqual(os.path.realpath(value['cwd']),
                         os.path.realpath(registry.driver_dir('stepper')))

    def test_driver_with_no_base_net(self):
        manifest = {'type': 'airpump', 'version': '0.1.0', 'lager_api': 1,
                    'params': {'serial': {'type': 'str', 'required': True}},
                    'actions': {'status': {}}}
        driver = '''
from lager.drivers import CustomDriver, action

class Driver(CustomDriver):
    @action
    def status(self):
        return {"message": "idle", "serial": self.params["serial"],
                "base": sorted(self._base_nets)}
'''
        self.assertEqual(self.install(make_bundle(manifest, driver), 'airpump').status_code, 200)
        self.nets.append({'name': 'PUMP', 'role': 'airpump', 'instrument': 'custom:airpump',
                          'params': {'serial': 'AP12345'}})
        body = self.command('status', netname='PUMP').get_json()
        self.assertTrue(body['success'], body)
        self.assertEqual(body['value'], {'message': 'idle', 'serial': 'AP12345', 'base': []})

    def test_child_that_dies_without_a_result_is_502(self):
        resp = self.command('crash')
        self.assertEqual(resp.status_code, 502)
        self.assertIn('exited (code 3) without a result', resp.get_json()['error'])

if __name__ == '__main__':
    unittest.main()

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""In-memory fake of the box's :9000 /drivers API for CLI tests.

Extends the nets fake so `lager nets add-custom` and `lager drivers` see one
box. Route semantics mirror box/lager/http_handlers/drivers_handler.py.
"""

from __future__ import annotations

import copy
from urllib.parse import unquote, urlparse

from test.unit.cli.nets_http_fake import FakeBoxHTTP, FakeResponse

STEPPER_MANIFEST = {
    'type': 'stepper',
    'version': '1.2.0',
    'lager_api': 1,
    'description': 'Stepper rig on a UART net',
    'timeout_s': 30,
    'params': {
        'uart_net': {'type': 'net', 'role': 'uart', 'required': True, 'help': 'UART net'},
        'baudrate': {'type': 'int', 'required': False, 'default': 115200, 'help': 'Baud'},
    },
    'actions': {
        'home': {'help': 'Return home', 'timeout_s': 30, 'args': {}},
        'jog': {
            'help': 'Move forward then home', 'timeout_s': 30,
            'args': {
                'seconds': {'type': 'float', 'required': True, 'min': 0.1, 'max': 30.0,
                            'help': 'Seconds forward'},
                'hold_time': {'type': 'float', 'required': False, 'default': 3.0,
                              'help': 'Hold'},
                'fast': {'type': 'bool', 'required': False, 'default': False, 'help': ''},
                'mode': {'type': 'choice', 'required': False, 'choices': ['a', 'b'],
                         'default': 'a', 'help': ''},
            },
        },
    },
}


def stepper_entry(**overrides):
    entry = {'type': 'stepper', 'version': '1.2.0', 'status': 'ok', 'sha256': 'ab' * 32,
             'description': STEPPER_MANIFEST['description'],
             'manifest': copy.deepcopy(STEPPER_MANIFEST), 'nets': []}
    entry.update(overrides)
    return entry


class FakeDriversBox(FakeBoxHTTP):
    def __init__(self, drivers=None, **kwargs):
        super().__init__(**kwargs)
        self.drivers = {d['type']: d for d in (drivers or [])}
        self.uploads = []
        self.timeouts = []
        # /status customDriverDeps, and the packages a PUT reports installing.
        self.deps_support = True
        self.packages = []

    def _entry(self, dtype):
        entry = dict(self.drivers[dtype])
        entry['nets'] = sorted(n['name'] for n in self.saved_nets if n.get('role') == dtype)
        return entry

    def request(self, method, url, json=None, params=None, timeout=None, files=None, **kwargs):
        path = unquote(urlparse(url).path)
        if path == '/status':
            # Same shape as box_http_server's /status: flags under "capabilities".
            return FakeResponse(200, {'healthy': True, 'capabilities': {
                'customDrivers': True, 'customDriverDeps': self.deps_support}})
        if not path.startswith('/drivers'):
            return super().request(method, url, json=json, params=params, timeout=timeout)
        method = method.upper()
        self.calls.append((method, path, files, params or {}))
        self.timeouts.append(timeout)
        dtype = path[len('/drivers/'):] if path.startswith('/drivers/') else None
        if method == 'POST' and dtype and dtype.endswith('/deps'):
            dtype = dtype[:-len('/deps')]
            if dtype not in self.drivers:
                return FakeResponse(404, {'error': "No custom driver '%s' is installed" % dtype})
            return FakeResponse(200, {'type': dtype, 'packages': list(self.packages)})
        if method == 'GET' and dtype is None:
            return FakeResponse(200, {'drivers': [self._entry(t) for t in sorted(self.drivers)]})
        if method == 'GET':
            if dtype not in self.drivers:
                return FakeResponse(404, {'error': "No custom driver '%s' is installed" % dtype})
            return FakeResponse(200, self._entry(dtype))
        if method == 'PUT':
            self.uploads.append((dtype, files))
            replaced = dtype in self.drivers
            self.drivers[dtype] = stepper_entry(type=dtype)
            return FakeResponse(200, {'type': dtype, 'version': '1.2.0',
                                      'sha256': 'cd' * 32, 'replaced': replaced,
                                      'packages': list(self.packages)})
        if method == 'DELETE':
            if dtype not in self.drivers:
                return FakeResponse(404, {'error': 'not installed'})
            nets = self._entry(dtype)['nets']
            if nets and not (params or {}).get('force'):
                return FakeResponse(409, {'error': 'Driver is used by %s' % ', '.join(nets)})
            del self.drivers[dtype]
            return FakeResponse(200, {'type': dtype, 'removed': True, 'orphaned_nets': nets})
        return FakeResponse(405, None)

    def get(self, url, headers=None, timeout=None, **kwargs):
        return self.request('GET', url, timeout=timeout)

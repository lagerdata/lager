# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the POST /exec handler (`lager rust`) in box/lager/python/service.py.

Like the other box service tests, the box package pulls in hardware-only deps via
lager/__init__.py, so we stub those in sys.modules before importing. We also purge
any pre-imported `lager.*` modules so the box package (not a stray install) wins.

What we test:
  - /exec with no `binary` field returns HTTP 400
  - /exec parses binary/args/env/timeout and runs the executor (detach=False)
  - /exec with detach=1 returns a JSON body with the lager_process_id (not a stream)
  - /exec stages --add-file companions through to the executor as (name, bytes)
  - /exec streams output when not detached
"""

import io
import os
import sys
import types
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
    'pigpio',
    'labjack', 'labjack.ljm',
    'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak',
    'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev',
    'smbus', 'smbus2',
    'RPi', 'RPi.GPIO',
    'gpiod',
]
for _dep in _HARDWARE_STUBS:
    _stub(_dep)

sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

# Purge any stray `lager` already imported so box/ wins regardless of test order.
for _m in [m for m in sys.modules if m == 'lager' or m.startswith('lager.')]:
    del sys.modules[_m]

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

import lager.python.service as svc_mod  # noqa: E402
from lager.python.service import PythonServiceHandler  # noqa: E402


def _make_handler(fields):
    """A handler whose parse_multipart returns `fields` and whose response
    methods record what was sent into handler.responses."""
    handler = PythonServiceHandler.__new__(PythonServiceHandler)
    handler.path = '/exec'
    handler.headers = {}
    handler.wfile = io.BytesIO()
    handler.rfile = io.BytesIO()
    handler.client_address = ('1.2.3.4', 5555)
    handler.responses = []
    handler.parse_multipart = lambda: fields
    handler.send_json_response = lambda status, data: handler.responses.append(('json', status, data))
    handler.send_error_response = lambda status, msg: handler.responses.append(('error', status, msg))
    handler.send_streaming_response = lambda gen: handler.responses.append(('stream', gen))
    return handler


def _patched_executor(execute_return):
    """Patch PythonExecutor in the service module; return the class mock so the
    test can inspect the execute() call."""
    cls = MagicMock()
    cls.return_value.execute.return_value = execute_return
    return patch.object(svc_mod, 'PythonExecutor', cls), cls


def test_exec_missing_binary_returns_400():
    handler = _make_handler({'timeout': b'5'})
    handler._handle_exec()
    assert handler.responses == [('error', 400, "Missing 'binary' field")]


def test_exec_parses_binary_args_env_timeout():
    fields = {
        'binary': io.BytesIO(b'\x7fELFdummy'),
        'args': [b'--flag', b'value'],
        'env': [b'FOO=bar'],
        'timeout': b'42',
    }
    handler = _make_handler(fields)
    ctx, cls = _patched_executor('GENERATOR')
    with ctx:
        handler._handle_exec()

    kwargs = cls.return_value.execute.call_args.kwargs
    assert kwargs['binary_file'] is fields['binary']
    assert kwargs['args'] == [b'--flag', b'value']
    assert kwargs['env_vars'] == ['FOO=bar']
    assert kwargs['timeout'] == 42
    assert kwargs['detach'] is False
    assert kwargs['add_files'] == []
    # Not detached → streamed
    assert handler.responses == [('stream', 'GENERATOR')]


def test_exec_detach_returns_json_with_process_id():
    fields = {
        'binary': io.BytesIO(b'\x7fELFdummy'),
        'detach': b'1',
    }
    handler = _make_handler(fields)
    detach_result = {'status': 'detached', 'pid': 99, 'lager_process_id': 'abc-123'}
    ctx, cls = _patched_executor(detach_result)
    with ctx:
        handler._handle_exec()

    assert cls.return_value.execute.call_args.kwargs['detach'] is True
    assert handler.responses == [('json', 200, detach_result)]


def test_exec_add_file_passed_to_executor():
    fields = {
        'binary': io.BytesIO(b'\x7fELFdummy'),
        'add_file': [('data.csv', io.BytesIO(b'1,2,3')), ('cfg.txt', io.BytesIO(b'hello'))],
    }
    handler = _make_handler(fields)
    ctx, cls = _patched_executor('GEN')
    with ctx:
        handler._handle_exec()

    assert cls.return_value.execute.call_args.kwargs['add_files'] == [
        ('data.csv', b'1,2,3'),
        ('cfg.txt', b'hello'),
    ]


def test_exec_streams_when_not_detached():
    fields = {'binary': io.BytesIO(b'\x7fELFdummy')}
    handler = _make_handler(fields)
    ctx, cls = _patched_executor('STREAMGEN')
    with ctx:
        handler._handle_exec()
    assert handler.responses == [('stream', 'STREAMGEN')]

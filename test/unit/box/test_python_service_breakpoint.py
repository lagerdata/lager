# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the breakpoint endpoints on box/lager/python/service.py.

  POST /python/breakpoint -> current breakpoint.json state, or {'paused': False}
  POST /python/continue   -> writes a resume marker, returns {'resumed': bool}

The box package pulls hardware-only deps via lager/__init__.py, so we stub them
in sys.modules before importing (same approach as test_python_service_nets_list).
"""

import io
import os
import sys
import json
import types
import shutil
import importlib.util

import pytest
from unittest.mock import MagicMock


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


for _dep in [
    'pyvisa', 'pyvisa.constants', 'pyvisa_py',
    'usb', 'usb.util', 'usb.core', 'pigpio',
    'labjack', 'labjack.ljm', 'nidaqmx',
    'phidget22', 'phidget22.Phidget', 'phidget22.Net',
    'bleak', 'picoscope',
    'serial', 'serial.tools', 'serial.tools.list_ports',
    'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]:
    _stub(_dep)
sys.modules['simplejson'] = sys.modules['json']

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python.service import PythonServiceHandler  # noqa: E402
from lager.python.exceptions import LagerPythonInvalidProcessIdError  # noqa: E402

PID = '22222222-2222-2222-2222-222222222222'
BASE = '/tmp/lager_processes'


@pytest.fixture
def proc_dir():
    d = os.path.join(BASE, PID)
    os.makedirs(d, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _make_handler(body):
    handler = PythonServiceHandler.__new__(PythonServiceHandler)
    raw = json.dumps(body).encode()
    handler.headers = {'Content-Length': str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler._responses = []
    handler.send_json_response = lambda status, data: handler._responses.append((status, data))
    handler.send_error_response = lambda status, msg: handler._responses.append((status, {'error': msg}))
    return handler


def test_breakpoint_status_not_paused(proc_dir):
    handler = _make_handler({'lager_process_id': PID})
    handler._handle_python_breakpoint()
    status, data = handler._responses[-1]
    assert status == 200
    assert data == {'paused': False}


def test_breakpoint_status_returns_state(proc_dir):
    state = {'paused': True, 'label': 'check DUT', 'line': 12, 'console_port': 8081}
    with open(os.path.join(proc_dir, 'breakpoint.json'), 'w') as f:
        json.dump(state, f)
    handler = _make_handler({'lager_process_id': PID})
    handler._handle_python_breakpoint()
    status, data = handler._responses[-1]
    assert status == 200
    assert data['paused'] is True
    assert data['label'] == 'check DUT'
    assert data['console_port'] == 8081


def test_breakpoint_status_corrupt_json(proc_dir):
    with open(os.path.join(proc_dir, 'breakpoint.json'), 'w') as f:
        f.write('{not json')
    handler = _make_handler({'lager_process_id': PID})
    handler._handle_python_breakpoint()
    status, data = handler._responses[-1]
    assert data == {'paused': False}


def test_continue_writes_resume_when_paused(proc_dir):
    with open(os.path.join(proc_dir, 'breakpoint.json'), 'w') as f:
        json.dump({'paused': True}, f)
    handler = _make_handler({'lager_process_id': PID})
    handler._handle_python_continue()
    status, data = handler._responses[-1]
    assert status == 200
    assert data == {'resumed': True}
    assert os.path.exists(os.path.join(proc_dir, 'resume'))


def test_continue_no_breakpoint(proc_dir):
    handler = _make_handler({'lager_process_id': PID})
    handler._handle_python_continue()
    status, data = handler._responses[-1]
    assert data == {'resumed': False}
    assert not os.path.exists(os.path.join(proc_dir, 'resume'))


def test_invalid_uuid_raises():
    for method in ('_handle_python_continue', '_handle_python_breakpoint'):
        handler = _make_handler({'lager_process_id': 'not-a-uuid'})
        with pytest.raises(LagerPythonInvalidProcessIdError):
            getattr(handler, method)()


def test_missing_id_returns_400():
    for method in ('_handle_python_continue', '_handle_python_breakpoint'):
        handler = _make_handler({})
        getattr(handler, method)()
        status, _data = handler._responses[-1]
        assert status == 400

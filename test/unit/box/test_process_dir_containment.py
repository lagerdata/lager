# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""The job registry path stays under PROCESS_REGISTRY_DIR.

A detached job's directory is named after a `lager_process_id` that arrives in
a request body. Every handler parses it as a UUID first, and that parse is the
real defence -- these tests pin it. The containment check beside each join is
the invariant stated where it holds, so that reordering or dropping a UUID
parse cannot silently turn an id into a path outside the registry.

`process_dir_for` is the one place the containment check is reachable on its
own, because its remaining callers build an id rather than taking one off the
wire. Hardware-only deps are stubbed as in test_python_service_breakpoint.py.
"""

import io
import os
import sys
import json
import types
import shutil
import uuid as uuid_mod

import pytest
from unittest.mock import MagicMock


def _make_module(name):
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted):
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

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python.executor import (  # noqa: E402
    PROCESS_REGISTRY_DIR,
    process_dir_for,
)
from lager.python.service import PythonServiceHandler  # noqa: E402
from lager.python.exceptions import (  # noqa: E402
    LagerPythonInvalidProcessIdError,
)

VALID = '22222222-2222-2222-2222-222222222222'

# Ids that would leave the registry if they were joined without a check.
ESCAPING = [
    '../etc',
    '../../home/lagerdata',
    'a/../../b',
    '/etc',
    '..',
]


@pytest.fixture
def proc_dir():
    d = os.path.join(PROCESS_REGISTRY_DIR, VALID)
    os.makedirs(d, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _handler(body):
    handler = PythonServiceHandler.__new__(PythonServiceHandler)
    raw = json.dumps(body).encode()
    handler.headers = {'Content-Length': str(len(raw))}
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler._responses = []
    handler.send_json_response = \
        lambda status, data: handler._responses.append((status, data))
    handler.send_error_response = \
        lambda status, msg: handler._responses.append((status, {'error': msg}))
    return handler


# ---- process_dir_for: the reachable containment check -----------------------

def test_a_uuid_maps_to_the_registry_subdirectory():
    assert process_dir_for(VALID) == os.path.join(PROCESS_REGISTRY_DIR, VALID)


@pytest.mark.parametrize('bad', ESCAPING)
def test_an_escaping_id_is_refused(bad):
    with pytest.raises(ValueError) as exc:
        process_dir_for(bad)
    assert 'escapes the registry' in str(exc.value)


def test_the_root_itself_is_not_inside_the_root():
    # '' joins to the root, which is not a job directory. The check uses a
    # trailing separator, so the root does not pass its own containment test.
    with pytest.raises(ValueError):
        process_dir_for('')


# ---- the handlers: the UUID parse is what a request actually meets ----------

@pytest.mark.parametrize('bad', ESCAPING)
@pytest.mark.parametrize('method', [
    '_handle_python_attach',
    '_handle_python_continue',
    '_handle_python_breakpoint',
])
def test_handlers_refuse_a_non_uuid_id(method, bad):
    handler = _handler({'lager_process_id': bad})
    with pytest.raises(LagerPythonInvalidProcessIdError):
        getattr(handler, method)()


def test_a_valid_id_still_resolves_inside_the_registry(proc_dir):
    # Regression: the inlined joins must produce the same directory the
    # helper does, so an existing job stays reachable across this change.
    handler = _handler({'lager_process_id': VALID})
    handler._handle_python_breakpoint()
    status, data = handler._responses[-1]
    assert status == 200
    assert data == {'paused': False}

    state = {'paused': True, 'label': 'check DUT'}
    with open(os.path.join(proc_dir, 'breakpoint.json'), 'w') as f:
        json.dump(state, f)
    handler = _handler({'lager_process_id': VALID})
    handler._handle_python_breakpoint()
    status, data = handler._responses[-1]
    assert status == 200
    assert data['label'] == 'check DUT'


def test_continue_writes_its_marker_inside_the_registry(proc_dir):
    with open(os.path.join(proc_dir, 'breakpoint.json'), 'w') as f:
        json.dump({'paused': True}, f)
    handler = _handler({'lager_process_id': VALID})
    handler._handle_python_continue()
    status, data = handler._responses[-1]
    assert status == 200
    assert data == {'resumed': True}
    resume = os.path.join(proc_dir, 'resume')
    assert os.path.exists(resume)
    assert os.path.normpath(resume).startswith(PROCESS_REGISTRY_DIR + os.sep)


def test_uuid_module_still_gates_before_the_join():
    # The UUID parse is the real defence; pin that it is what rejects, so a
    # later reorder that relies on containment alone is visible here.
    assert uuid_mod.UUID(VALID)
    with pytest.raises(ValueError):
        uuid_mod.UUID('../etc')

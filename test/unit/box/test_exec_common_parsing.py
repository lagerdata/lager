# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for PythonServiceHandler._parse_common_exec_fields — the field parser
shared by POST /python and POST /exec. Keeping the parsing in one place is what
guarantees `lager python` and `lager rust` decode args/env/timeout identically;
these tests pin that decoding.
"""

import io
import os
import sys
import types
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
    'pyvisa', 'pyvisa.constants', 'pyvisa_py', 'usb', 'usb.util', 'usb.core',
    'pigpio', 'labjack', 'labjack.ljm', 'nidaqmx', 'phidget22', 'phidget22.Phidget',
    'phidget22.Net', 'bleak', 'picoscope', 'serial', 'serial.tools',
    'serial.tools.list_ports', 'spidev', 'smbus', 'smbus2', 'RPi', 'RPi.GPIO', 'gpiod',
]:
    _stub(_dep)
sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]
for _m in [m for m in sys.modules if m == 'lager' or m.startswith('lager.')]:
    del sys.modules[_m]
_BOX_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box'))
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python.service import PythonServiceHandler  # noqa: E402

parse = PythonServiceHandler._parse_common_exec_fields


def test_defaults_when_fields_absent():
    stdout_is_stderr, detach, timeout, args, env = parse({})
    assert stdout_is_stderr is True   # default 'true'
    assert detach is False            # default 'false'
    assert timeout == 300             # default
    assert args == []
    assert env == []


def test_timeout_from_bytes():
    _, _, timeout, _, _ = parse({'timeout': b'90'})
    assert timeout == 90


def test_detach_and_stdout_truthy_strings():
    _, detach, _, _, _ = parse({'detach': b'1'})
    assert detach is True
    sis, _, _, _, _ = parse({'stdout_is_stderr': b'false'})
    assert sis is False


def test_single_arg_and_env_are_wrapped_into_lists():
    # parse_multipart returns a single (non-list) value when only one was sent.
    _, _, _, args, env = parse({'args': b'solo', 'env': b'A=1'})
    assert args == [b'solo']
    assert env == ['A=1']


def test_list_args_and_env_decoded():
    _, _, _, args, env = parse({'args': [b'a', b'b'], 'env': [b'X=1', b'Y=2']})
    assert args == [b'a', b'b']      # args kept as bytes (decoded later by executor)
    assert env == ['X=1', 'Y=2']     # env decoded to str


def test_bytesio_env_is_read_and_decoded():
    _, _, _, _, env = parse({'env': [io.BytesIO(b'Z=9')]})
    assert env == ['Z=9']

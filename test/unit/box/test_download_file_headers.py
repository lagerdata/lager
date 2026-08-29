# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Header safety for GET /download-file on the :5000 python service.

The :9000 twin (``http_handlers/binaries_handler.py``) hands the name to
Flask's ``send_file``, which escapes it. This service writes the header by
hand, so the reduction has to happen here.

The box package pulls hardware-only deps via lager/__init__.py, so we stub them
in sys.modules before importing (same approach as
test_python_service_breakpoint.py).
"""

import io
import os
import sys
import types
from urllib.parse import quote
from unittest.mock import MagicMock

import pytest


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

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python.service import PythonServiceHandler  # noqa: E402

# One of binaries_store.ALLOWED_DOWNLOAD_ROOTS -- anything outside is a 403
# before the header is ever built.
ROOT = '/tmp/lager-output'


@pytest.fixture
def root_dir():
    os.makedirs(ROOT, exist_ok=True)
    made = []
    yield made
    for p in made:
        try:
            os.remove(p)
        except OSError:
            pass


def _write(root_dir, name, content=b'payload'):
    path = os.path.join(ROOT, name)
    with open(path, 'wb') as f:
        f.write(content)
    root_dir.append(path)
    return path


def _download(path):
    """Drive _handle_download_file and return (status, headers, body)."""
    handler = PythonServiceHandler.__new__(PythonServiceHandler)
    handler.path = '/download-file?filename=' + quote(path, safe='')
    handler.wfile = io.BytesIO()
    handler._status = None
    handler._headers = []
    handler.send_response = lambda status: setattr(handler, '_status', status)
    handler.send_header = lambda k, v: handler._headers.append((k, v))
    handler.end_headers = lambda: None
    handler.send_error_response = lambda status, msg: (
        setattr(handler, '_status', status),
        handler._headers.append(('X-Error', msg)),
    )
    handler._handle_download_file()
    return handler._status, dict(handler._headers), handler.wfile.getvalue()


def test_ordinary_name_is_passed_through(root_dir):
    _write(root_dir, 'report.bin')
    status, headers, body = _download(os.path.join(ROOT, 'report.bin'))
    assert status == 200
    assert headers['Content-Disposition'] == 'attachment; filename="report.bin"'
    assert body == b'payload'


def test_spaces_survive(root_dir):
    # Only header syntax is reduced; a name a user would recognise is kept.
    _write(root_dir, 'my report (final).bin')
    status, headers, _ = _download(os.path.join(ROOT, 'my report (final).bin'))
    assert status == 200
    assert headers['Content-Disposition'] == \
        'attachment; filename="my report (final).bin"'


def test_line_breaks_in_a_real_filename_do_not_reach_the_header(root_dir):
    # A filename is allowed to contain CR and LF, so this file really can
    # exist on the box -- which is why the reduction happens at the header
    # rather than being assumed of the name on disk.
    name = 'report\r\nX-Extra: 1.bin'
    _write(root_dir, name)
    status, headers, _ = _download(os.path.join(ROOT, name))
    assert status == 200
    value = headers['Content-Disposition']
    assert '\r' not in value and '\n' not in value
    assert value == 'attachment; filename="report__X-Extra: 1.bin"'


def test_double_quote_cannot_close_the_quoted_string(root_dir):
    name = 'a"b.bin'
    _write(root_dir, name)
    status, headers, _ = _download(os.path.join(ROOT, name))
    assert status == 200
    assert headers['Content-Disposition'] == 'attachment; filename="a_b.bin"'


def test_path_outside_the_allowlist_is_refused(root_dir):
    status, headers, _ = _download('/etc/passwd')
    assert status == 403
    assert 'Content-Disposition' not in headers

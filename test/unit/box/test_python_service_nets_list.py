# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the GET /nets/list handler added to box/lager/python/service.py.

The box package has hardware-specific dependencies (simplejson, pyvisa, usb, …)
that are only available inside the Docker container.  We stub out the
problematic packages in sys.modules before importing so these tests can run
on any developer machine without installing box dependencies.

What we test:
  - /nets/list returns the full saved-net array (instrument, address, pin, role)
  - /nets/list returns [] when the file is missing
  - /nets/list returns [] when the file contains invalid JSON
  - /nets/list returns [] when the file contains a non-list value
  - unknown paths return HTTP 404
"""

import io
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, mock_open, patch

# ── Stub out hardware-only transitive dependencies ────────────────────────────
# These modules are imported by lager/__init__.py via the nets/instrument_wrappers
# chain and are only available inside the Docker container.
# We must stub them as real types.ModuleType objects (not MagicMock) so that
# the import system can treat them as packages with submodules.

def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    # Module-level __getattr__ takes only the attribute name (no self).
    mod.__getattr__ = lambda attr: MagicMock()  # type: ignore[method-assign]
    return mod


def _stub(dotted: str) -> None:
    """Register a fake module for `dotted` and all its parent packages."""
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

# Make `import simplejson as json` in visa_enum resolve to stdlib json.
sys.modules['simplejson'] = sys.modules['json']  # type: ignore[assignment]

# Add box/ to path so `import lager.python.service` resolves correctly.
_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager.python.service import PythonServiceHandler  # noqa: E402


# ── Helper ─────────────────────────────────────────────────────────────────────

SAMPLE_NETS = [
    {
        "name": "usb1",
        "role": "usb",
        "instrument": "Acroname_8Port",
        "address": "USB0::0x24FF::0x0013::EBFB8D94::INSTR",
        "pin": "0",
    },
    {
        "name": "webcam1",
        "role": "webcam",
        "instrument": "Logitech_BRIO_HD",
        "address": "USB0::0x046D::0x085E::B4EA562D::INSTR",
        "pin": "/dev/video0",
    },
]


def _make_handler(path: str) -> PythonServiceHandler:
    """Return a PythonServiceHandler wired to an in-memory wfile."""
    handler = PythonServiceHandler.__new__(PythonServiceHandler)
    handler.path = path
    handler.headers = {}
    handler.wfile = io.BytesIO()
    handler.rfile = io.BytesIO()

    # Capture raw HTTP response bytes so we can parse them back.
    def send_json_response(status, data):
        body = json.dumps(data).encode()
        handler.wfile.write(
            (
                f"HTTP/1.1 {status} OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
            ).encode()
            + body
        )

    def send_error_response(status, message):
        body = json.dumps({"error": message, "status": "error"}).encode()
        handler.wfile.write(
            (
                f"HTTP/1.1 {status} Error\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "\r\n"
            ).encode()
            + body
        )

    handler.send_json_response = send_json_response
    handler.send_error_response = send_error_response
    return handler


def _parse_response(handler) -> tuple[int, object]:
    """Return (status_code, parsed_body) from the handler's captured output."""
    raw = handler.wfile.getvalue().decode()
    status_line, _, rest = raw.partition("\r\n")
    _, _, body = rest.partition("\r\n\r\n")
    status_code = int(status_line.split()[1])
    return status_code, json.loads(body)


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestNetsListHandler(unittest.TestCase):

    def test_returns_full_nets_including_instrument_and_address(self):
        """GET /nets/list returns all fields: instrument, address, pin, role."""
        handler = _make_handler('/nets/list')
        with patch('builtins.open', mock_open(read_data=json.dumps(SAMPLE_NETS))):
            handler.do_GET()

        status, result = _parse_response(handler)
        self.assertEqual(status, 200)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

        usb1 = result[0]
        self.assertEqual(usb1['name'], 'usb1')
        self.assertEqual(usb1['instrument'], 'Acroname_8Port')
        self.assertEqual(usb1['address'], 'USB0::0x24FF::0x0013::EBFB8D94::INSTR')
        self.assertEqual(usb1['pin'], '0')
        self.assertEqual(usb1['role'], 'usb')

        webcam1 = result[1]
        self.assertEqual(webcam1['instrument'], 'Logitech_BRIO_HD')

    def test_returns_empty_list_when_file_missing(self):
        """GET /nets/list returns [] when saved_nets.json does not exist."""
        handler = _make_handler('/nets/list')
        with patch('builtins.open', side_effect=FileNotFoundError):
            handler.do_GET()

        status, result = _parse_response(handler)
        self.assertEqual(status, 200)
        self.assertEqual(result, [])

    def test_returns_empty_list_on_invalid_json(self):
        """GET /nets/list returns [] when saved_nets.json is malformed."""
        handler = _make_handler('/nets/list')
        with patch('builtins.open', mock_open(read_data='not valid json {{')):
            handler.do_GET()

        status, result = _parse_response(handler)
        self.assertEqual(status, 200)
        self.assertEqual(result, [])

    def test_returns_empty_list_when_json_is_not_array(self):
        """GET /nets/list returns [] when saved_nets.json contains a dict/non-list."""
        handler = _make_handler('/nets/list')
        with patch('builtins.open', mock_open(read_data=json.dumps({"unexpected": "object"}))):
            handler.do_GET()

        status, result = _parse_response(handler)
        self.assertEqual(status, 200)
        self.assertEqual(result, [])

    def test_unknown_path_returns_404(self):
        """Paths not known to the service return HTTP 404."""
        handler = _make_handler('/unknown/path/xyz')
        handler.do_GET()

        status, body = _parse_response(handler)
        self.assertEqual(status, 404)
        self.assertIn('error', body)


if __name__ == '__main__':
    unittest.main()

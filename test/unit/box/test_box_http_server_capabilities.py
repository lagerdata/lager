# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the box_http_server /status capabilities block.

Regression: the advertised `netCommand` capability must reflect whether the
POST /net/command route actually registered (`_has_net_command`), not be
hardcoded True. If the handler import fails the route is never registered, but
a hardcoded True still tells the control plane the box serves /net/command —
so it routes there and the box 404s ("The requested endpoint does not
exist").

The box package has hardware-only dependencies (pyvisa, usb, labjack, …) that
only exist inside the Docker container. We stub them in sys.modules before
import so these tests run on any developer machine.
"""

import os
import sys
import types
import unittest
import unittest.mock
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

_BOX_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', 'box')
)
if _BOX_ROOT not in sys.path:
    sys.path.insert(0, _BOX_ROOT)

from lager import box_http_server  # noqa: E402


class StatusCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.client = box_http_server.app.test_client()
        self._orig_has_net_command = box_http_server._has_net_command

    def tearDown(self):
        box_http_server._has_net_command = self._orig_has_net_command

    def _capabilities(self):
        resp = self.client.get('/status')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()['capabilities']

    def test_advertises_netcommand_when_route_registered(self):
        box_http_server._has_net_command = True
        self.assertIs(self._capabilities()['netCommand'], True)

    def test_does_not_advertise_netcommand_when_handler_unavailable(self):
        # Import failed → route never registered → must not claim the capability.
        box_http_server._has_net_command = False
        self.assertIs(self._capabilities()['netCommand'], False)

    def test_advertises_net_command_roles(self):
        # Clients detect arm/webcam/router support from the role list rather
        # than version sniffing; it must mirror the registered ROLE_ACTIONS.
        caps = self._capabilities()
        roles = caps['netCommandRoles']
        if box_http_server._has_net_command:
            for role in ('gpio', 'adc', 'arm', 'webcam', 'router'):
                self.assertIn(role, roles)
        else:
            self.assertEqual(roles, [])

    def test_box_level_capabilities_reflect_registration(self):
        # Same contract as netCommand: each flag mirrors whether its route
        # actually registered.
        orig = (box_http_server._has_ble, box_http_server._has_wifi,
                box_http_server._has_blufi)
        try:
            box_http_server._has_ble = True
            box_http_server._has_wifi = False
            box_http_server._has_blufi = True
            caps = self._capabilities()
            self.assertIs(caps['bleCommand'], True)
            self.assertIs(caps['wifiCommand'], False)
            self.assertIs(caps['blufiCommand'], True)
        finally:
            (box_http_server._has_ble, box_http_server._has_wifi,
             box_http_server._has_blufi) = orig

    def test_safety_limits_capability_reflects_nets_registration(self):
        # The control plane decides whether to report a configured ceiling as
        # enforced from this flag. Reporting it enforced on a box that cannot
        # store one is the failure the flag exists to prevent, so it must track
        # registration rather than the presence of the module on disk.
        orig = box_http_server._has_nets
        try:
            box_http_server._has_nets = True
            self.assertIs(self._capabilities()['safetyLimits'], True)
            box_http_server._has_nets = False
            self.assertIs(self._capabilities()['safetyLimits'], False)
        finally:
            box_http_server._has_nets = orig


if __name__ == '__main__':
    unittest.main()


class StatusDeployedRefTest(unittest.TestCase):
    """`/status` reports WHICH ref produced the box's code (issue #266).

    Version alone cannot: a branch not yet bumped past the last release
    declares the same `__version__` as the release tag, so a box on `main`
    and a box on the tag answered identically. The field must also be absent
    (null) rather than an error on a box predating /etc/lager/ref -- older
    boxes answer this endpoint too, and `lager hello` degrades to its previous
    output rather than breaking.
    """

    def setUp(self):
        self.client = box_http_server.app.test_client()

    def _status_with_ref_file(self, contents):
        """Run /status with /etc/lager/ref reading as `contents`, or absent
        when `contents` is None. Only the ref file is redirected; every other
        open() the handler makes keeps its real behaviour.
        """
        import builtins
        real_open = builtins.open

        def fake_open(path, *args, **kwargs):
            if str(path) == '/etc/lager/ref':
                if contents is None:
                    raise FileNotFoundError(path)
                import io
                return io.StringIO(contents)
            return real_open(path, *args, **kwargs)

        with unittest.mock.patch('builtins.open', fake_open):
            resp = self.client.get('/status')
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_reports_the_ref_when_present(self):
        self.assertEqual(self._status_with_ref_file('main@85c1b64\n')['ref'],
                         'main@85c1b64')

    def test_reports_a_release_tag_ref(self):
        self.assertEqual(self._status_with_ref_file('v0.41.0@d209f02\n')['ref'],
                         'v0.41.0@d209f02')

    def test_absent_ref_file_yields_null_not_an_error(self):
        # A box that has not been updated since /etc/lager/ref was introduced.
        body = self._status_with_ref_file(None)
        self.assertIsNone(body['ref'])
        self.assertTrue(body['healthy'])

    def test_empty_ref_file_yields_null(self):
        # A failed best-effort write can leave an empty file; '' must not be
        # reported as if it were a ref.
        self.assertIsNone(self._status_with_ref_file('\n')['ref'])

    def test_the_version_field_is_unaffected(self):
        # The ref is a sibling file precisely so the version field's parsing
        # is untouched.
        body = self._status_with_ref_file('main@85c1b64\n')
        self.assertIn('version', body)

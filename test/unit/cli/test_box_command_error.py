# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for ``cli.core.net_helpers.box_command_error``.

The :9000 command CLIs (usb / supply / battery) appended "This box image does
not expose <endpoint>; update the box." to every 404. But their handlers answer
404 themselves, for "the net or its instrument was not found" — so an unplugged
USB hub reported both halves at once:

    Error: device-not-found: No Acroname hub detected on USB with serial
    0xE6BACCD5. This box image does not expose /usb/command; update the box.

Both cannot be true, and the second sends the diagnosis somewhere the fault
never was — the box was current, the hub was unplugged. Observed on a box
running a build that very obviously did expose the endpoint, since the endpoint
is what produced the first half.

What distinguishes the two is the SHAPE of the body, not the status code: every
handler sets ``success`` in its JSON, and Flask's built-in 404 (the only one
that really means "this image predates the endpoint") does not.
"""

import unittest

from cli.core.net_helpers import box_command_error


class HandlerAnsweredTests(unittest.TestCase):
    """A body carrying ``success`` came from the handler — the endpoint exists,
    whatever else went wrong."""

    def test_device_not_found_gets_no_upgrade_hint(self):
        error = box_command_error(
            {'success': False,
             'error': 'device-not-found: No Acroname hub detected on USB '
                      'with serial 0xE6BACCD5'},
            404, '/usb/command', 'USB command failed (HTTP 404)')
        self.assertNotIn('update the box', error)
        self.assertIn('No Acroname hub detected', error)

    def test_unknown_net_gets_no_upgrade_hint(self):
        error = box_command_error(
            {'success': False, 'error': "USB net not found: 'usb99'"},
            404, '/usb/command', 'fallback')
        self.assertEqual(error, "USB net not found: 'usb99'")

    def test_non_404_handler_errors_are_passed_through(self):
        for status in (409, 500, 502, 503, 504):
            with self.subTest(status=status):
                error = box_command_error(
                    {'success': False, 'error': 'hub-busy: still running'},
                    status, '/usb/command', 'fallback')
                self.assertEqual(error, 'hub-busy: still running')


class EndpointMissingTests(unittest.TestCase):
    """Flask's own 404 has no ``success`` key — this is the one case the hint
    is for, and it must survive."""

    def test_flask_404_gets_the_upgrade_hint(self):
        error = box_command_error(
            {'error': 'Not found',
             'message': 'The requested endpoint does not exist'},
            404, '/usb/command', 'USB command failed (HTTP 404)')
        self.assertIn('does not expose /usb/command', error)
        self.assertIn('update the box', error)

    def test_hint_names_the_calling_endpoint(self):
        for endpoint in ('/usb/command', '/supply/command', '/battery/command'):
            with self.subTest(endpoint=endpoint):
                error = box_command_error(
                    {'error': 'Not found'}, 404, endpoint, 'fallback')
                self.assertIn(f'does not expose {endpoint}', error)

    def test_empty_404_body_still_gets_the_hint(self):
        error = box_command_error({}, 404, '/usb/command', 'HTTP 404')
        self.assertEqual(
            error,
            'HTTP 404. This box image does not expose /usb/command; '
            'update the box.')


class MalformedBodyTests(unittest.TestCase):
    """A box can answer with JSON that is not an object at all; the caller
    still needs a usable message rather than an AttributeError."""

    def test_non_dict_body_falls_back(self):
        for body in (None, [], 'nope', 7):
            with self.subTest(body=body):
                error = box_command_error(body, 500, '/usb/command', 'fallback')
                self.assertEqual(error, 'fallback')

    def test_non_dict_404_body_gets_the_hint(self):
        error = box_command_error(None, 404, '/usb/command', 'fallback')
        self.assertIn('update the box', error)

    def test_blank_error_field_falls_back(self):
        error = box_command_error(
            {'success': False, 'error': ''}, 502, '/usb/command', 'fallback')
        self.assertEqual(error, 'fallback')


if __name__ == '__main__':
    unittest.main()

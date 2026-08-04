# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
What `lager usb <net> <command>` tells you when the box says no.

The box answers 404 for two unrelated things: the route does not exist (an old
box image) and the device was not found (an unplugged or unreachable hub). The
CLI used to append "this box image does not expose /usb/command; update the
box" to both, so someone whose hub had lost its cable was sent to run
`lager update`. See issue #196.

Deliberately overlaps test_box_command_error.py, which covers the same rule at
the helper level. These drive the whole of `_invoke_remote`, so they also pin
the WIRING -- a caller that passes `box_command_error` the wrong arguments, or
stops calling it, produces exactly the old misdirection again and the helper's
own tests would still pass. Cheap insurance on a message that sent a real
diagnosis to the wrong place.
"""

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

import click

from cli.commands.communication.usb import _invoke_remote


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _run(status_code, payload):
    """Drive _invoke_remote against a canned box response; returns stderr."""
    ctx = MagicMock(spec=click.Context)
    ctx.exit.side_effect = SystemExit(1)
    err = io.StringIO()
    with patch("requests.post", return_value=_Resp(status_code, payload)), \
            patch("cli.box_storage._check_gateway", side_effect=lambda r, b: r), \
            patch("cli.gateway_auth.auth_headers_for_box", return_value={}):
        with redirect_stderr(err):
            try:
                _invoke_remote(ctx, "usb1", "10.0.0.1", "state")
            except SystemExit:
                pass
    return err.getvalue()


class UsbErrorMessageTests(unittest.TestCase):
    def test_a_device_404_does_not_tell_you_to_update_the_box(self):
        """THE regression. The hub is unreachable; updating the box is not
        the remedy and saying so costs someone a long detour."""
        err = _run(404, {
            "success": False,
            "error": ("device-not-found: No Acroname hub detected on USB with "
                      "serial 0xE6BACCD5: the hub is on the USB bus but is not "
                      "answering BrainStem; check hub power and the upstream "
                      "cable"),
            "reason_code": "hub-unreachable",
        })
        self.assertNotIn("update the box", err)
        self.assertIn("check hub power and the upstream cable", err)

    def test_a_routing_404_still_says_to_update_the_box(self):
        """An old box image with no /usb/command route sends no error body."""
        err = _run(404, {})
        self.assertIn("update the box", err)

    def test_the_detail_is_shown_when_the_box_sends_one(self):
        err = _run(404, {
            "success": False,
            "error": "device-not-found: no hub",
            "reason_detail": "discovery: findAllModules ok, 1 spec(s); sysfs: ...",
        })
        self.assertIn("findAllModules ok", err)

    def test_an_older_box_without_the_new_fields_still_reports_its_error(self):
        """The box may predate reason_code/reason_detail entirely."""
        err = _run(404, {"success": False, "error": "device-not-found: no hub"})
        self.assertIn("device-not-found: no hub", err)
        self.assertNotIn("update the box", err)

    def test_a_port_state_409_is_reported_verbatim(self):
        err = _run(409, {"success": False,
                         "error": "port-state: Acroname error code 12"})
        self.assertIn("Acroname error code 12", err)
        self.assertNotIn("update the box", err)


if __name__ == "__main__":
    unittest.main()

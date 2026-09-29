# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
A box host with no BlueZ (Ubuntu Server) fails every BLE and BluFi command
with a raw D-Bus ServiceUnknown error for org.bluez. The box rewrites it into
one remedy line; for a box whose image predates that, the CLI does the same.

Nothing here opens a socket: ``requests.post`` is patched.
"""

import pathlib
import re
import unittest
from unittest.mock import MagicMock, patch

import click
from click.testing import CliRunner

from cli.core import net_helpers

_NO_BLUEZ = ("BLE error: [org.freedesktop.DBus.Error.ServiceUnknown] The name "
             "org.bluez was not provided by any .service files")

_BOX_BLE = (pathlib.Path(__file__).resolve().parents[3]
            / "box" / "lager" / "http_handlers" / "ble.py")


def _run_post(status, body):
    resp = MagicMock(status_code=status)
    resp.json.return_value = body

    @click.command()
    @click.pass_context
    def cmd(ctx):
        net_helpers.post_box_command(ctx, "192.0.2.4", "/ble/command", "scan")

    with patch("requests.post", return_value=resp), \
            patch("cli.gateway_auth.auth_headers_for_box", return_value={}), \
            patch("cli.box_storage._check_gateway", side_effect=lambda r, ip: r):
        return CliRunner().invoke(cmd, [])


class OlderBoxImage(unittest.TestCase):
    def test_the_raw_dbus_error_becomes_the_remedy(self):
        result = _run_post(502, {"success": False, "error": _NO_BLUEZ})
        self.assertEqual(result.exit_code, 1)
        self.assertIn(net_helpers.BLUEZ_UNAVAILABLE_MESSAGE, result.output)
        self.assertNotIn("ServiceUnknown", result.output)

    def test_any_other_error_is_shown_as_sent(self):
        result = _run_post(502, {"success": False, "error": "BLE error: adapter off"})
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Error: BLE error: adapter off", result.output)
        self.assertNotIn("BlueZ", result.output)


class OneMessage(unittest.TestCase):
    def test_the_cli_and_the_box_say_the_same_thing(self):
        # The box image cannot import the CLI, so the text lives in both.
        source = _BOX_BLE.read_text(encoding="utf-8")
        block = re.search(r"BLUEZ_UNAVAILABLE_MESSAGE = \(\n(.*?)\n\)", source, re.S)
        self.assertIsNotNone(block)
        box_text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', block.group(1)))
        self.assertEqual(box_text, net_helpers.BLUEZ_UNAVAILABLE_MESSAGE)


if __name__ == "__main__":
    unittest.main()

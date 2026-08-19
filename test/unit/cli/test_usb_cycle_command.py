# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager usb <net> cycle` / `recover` -- the CLI half of the wiring.

A power-cycle is the operation people reach for when a DUT is wedged and the
bench is remote, so the two things pinned here are the two that would strand
someone: the off-time actually reaching the box (a dropped parameter silently
gives everyone the default), and the request not being cut short by the client
before the box can answer (a cycle holds the hub for its whole off time, so the
client budget has to cover it).
"""

import importlib
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import click

# Imported by module path, not `from ... import usb`: the package re-exports the
# click GROUP under that name, so the attribute form hands back the command.
usb_cmd = importlib.import_module("cli.commands.communication.usb")


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _invoke(action, off_time=None, payload=None):
    """Drive _invoke_remote and return (posted_json, stdout)."""
    ctx = MagicMock(spec=click.Context)
    ctx.exit.side_effect = SystemExit(1)
    body = payload or {"success": True, "action": action,
                       "message": f"USB port 'usb1' {action}d"}
    out = io.StringIO()
    with patch("requests.post", return_value=_Resp(body)) as post, \
            patch("cli.box_storage._check_gateway", side_effect=lambda r, b: r), \
            patch("cli.gateway_auth.auth_headers_for_box", return_value={}):
        with redirect_stdout(out):
            try:
                usb_cmd._invoke_remote(ctx, "usb1", "10.0.0.1", action, off_time)
            except SystemExit:
                pass
        return post.call_args.kwargs["json"], out.getvalue()


class UsbCycleWiringTests(unittest.TestCase):
    def test_off_time_reaches_the_box(self):
        sent, _ = _invoke("cycle", 2.5)
        self.assertEqual(sent["action"], "cycle")
        self.assertEqual(sent["off_time"], 2.5)

    def test_no_off_time_means_the_box_default_not_a_client_guess(self):
        # The range and the default live in one place, box-side. Sending a
        # client-side default here would let the two drift.
        sent, _ = _invoke("cycle", None)
        self.assertNotIn("off_time", sent)

    def test_recover_sends_no_off_time(self):
        sent, _ = _invoke("recover")
        self.assertEqual(sent["action"], "recover")
        self.assertNotIn("off_time", sent)

    def test_client_timeout_covers_the_longest_legal_cycle(self):
        # Box-side worst case is additive: up to 10s queueing on the process
        # hub lock, then up to HUB_OP_TIMEOUT_S (30s) in the driver deadline,
        # and a cycle spends its off time INSIDE that deadline. If the client
        # gives up first the box's structured error is undeliverable and the
        # user sees "box unreachable" for a hub that answered. The box-side
        # half of this invariant is pinned in
        # test/unit/box/test_plugable_driver.py::TestOffTimeContract.
        self.assertGreater(usb_cmd._USB_COMMAND_TIMEOUT_S, 10 + 30)

    def test_cycle_and_recover_are_registered_subcommands(self):
        self.assertIn("cycle", usb_cmd.usb.commands)
        self.assertIn("recover", usb_cmd.usb.commands)

    def test_cycle_exposes_an_off_time_option(self):
        opts = {o.name for o in usb_cmd.usb.commands["cycle"].params}
        self.assertIn("off_time", opts)


if __name__ == "__main__":
    unittest.main()

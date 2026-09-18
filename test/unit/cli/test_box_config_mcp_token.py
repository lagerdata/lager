# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager box-config mcp-token ...` -- the host side of the box MCP bearer token.

The box-side shim is mocked at `_run_box_config_py`, so each test is a pure
CLI exercise: no SSH, no HTTP, no box.

What is pinned here is where the secret goes. `enable` and `rotate` show it,
once each, and that is the only place it ever appears: not in `status`, not in
a refusal, and above all not in an error message. `_parse_response`, which
every other box-config command uses, prints the box's raw reply when it cannot
parse it -- and the reply to these two verbs carries the token.
"""
import json
import pathlib
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.box import _shim_verbs as verbs
from cli.commands.box import config as box_config_cli

BOX_IP = "1.2.3.4"
TOKEN = "s3cr3t-Value_for-the.test-0123456789abcdefghi"
REPO = pathlib.Path(__file__).resolve().parents[3]


class _Backend:
    """Stands in for `_run_box_config_py`. Replies are RAW strings, so a test
    can hand back something that is not JSON at all."""

    def __init__(self, reply):
        self.reply = reply if isinstance(reply, str) else json.dumps(reply)
        self.calls = []

    def __call__(self, ctx, box, *args, **kwargs):
        self.calls.append((box, args, kwargs))
        return self.reply


class _Case(unittest.TestCase):
    def _invoke(self, argv, reply, input=None):
        backend = _Backend(reply)
        with patch.object(box_config_cli, "_resolve_box", return_value=BOX_IP), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=backend):
            result = CliRunner().invoke(
                box_config_cli.box_config, ["mcp-token", *argv], input=input)
        return result, backend


class EnableAndRotateShowTheTokenOnce(_Case):
    def test_enable_shows_the_token_and_a_client_entry_that_carries_it(self):
        result, backend = self._invoke(
            ["enable"], {"ok": True, "state": "enabled", "previous": "disabled", "token": TOKEN})
        self.assertEqual(result.exit_code, 0, result.output)
        # Twice, both on purpose: once alone, once in the entry to paste.
        self.assertEqual(result.output.count(TOKEN), 2)
        entry = json.loads(result.output[result.output.index("{"):result.output.rindex("}") + 1])
        server = entry["mcpServers"]["lager"]
        self.assertEqual(server["url"], f"http://{BOX_IP}:8100/mcp")
        self.assertEqual(server["headers"], {"Authorization": f"Bearer {TOKEN}"})
        self.assertIn("only time", result.output)
        self.assertIn("unencrypted", result.output)

    def test_enable_sends_the_bare_verb_and_may_fall_back_to_ssh(self):
        _, backend = self._invoke(["enable"], {"ok": True, "token": TOKEN})
        self.assertEqual(backend.calls, [(BOX_IP, (verbs.MCP_TOKEN_ENABLE,), {"allow_ssh_fallback": True})])

    def test_rotate_with_yes_shows_the_new_token(self):
        result, backend = self._invoke(
            ["rotate", "--yes"], {"ok": True, "state": "enabled", "previous": "enabled", "token": TOKEN})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.count(TOKEN), 2)
        self.assertEqual(backend.calls[0][1], (verbs.MCP_TOKEN_ROTATE,))

    def test_rotate_asks_first_and_a_no_never_reaches_the_box(self):
        result, backend = self._invoke(["rotate"], {"ok": True, "token": TOKEN}, input="n\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(backend.calls, [])
        self.assertNotIn(TOKEN, result.output)


class TheTokenAppearsNowhereElse(_Case):
    def test_a_reply_that_does_not_parse_is_never_echoed(self):
        # Truncated JSON with a stray line in front: nothing in it parses.
        garbage = 'WARNING: something on stdout\n{"ok": true, "token": "' + TOKEN + '", "sta'
        for argv in (["enable"], ["rotate", "--yes"]):
            result, _ = self._invoke(argv, garbage)
            self.assertEqual(result.exit_code, 1, argv)
            self.assertNotIn(TOKEN, result.output, argv)
            self.assertIn("not shown", result.output)

    def test_the_shared_parser_would_have_echoed_it(self):
        # Why these commands have a parser of their own. If this ever stops
        # being true, the dedicated one can go.
        garbage = '{"ok": true, "token": "' + TOKEN + '", "sta'
        with patch.object(box_config_cli, "_resolve_box", return_value=BOX_IP), \
             patch.object(box_config_cli, "_run_box_config_py", side_effect=_Backend(garbage)):
            result = CliRunner().invoke(box_config_cli.box_config, ["network-mode", "show"])
        self.assertIn(TOKEN, result.output)

    def test_a_reply_that_is_json_but_not_an_object_is_never_echoed(self):
        result, _ = self._invoke(["enable"], json.dumps(TOKEN))
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn(TOKEN, result.output)

    def test_a_stray_line_before_a_good_reply_still_works(self):
        reply = "some banner line\n" + json.dumps({"ok": True, "token": TOKEN})
        result, _ = self._invoke(["enable"], reply)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.count(TOKEN), 2)

    def test_a_refused_enable_names_rotate_and_shows_no_token(self):
        result, _ = self._invoke(["enable"], {
            "ok": False, "code": "already-enabled",
            "errors": ["An MCP token already exists on this box, and its value "
                       "cannot be shown again. Use rotate to replace it."]})
        self.assertEqual(result.exit_code, 1)
        self.assertIn("rotate", result.output)
        self.assertNotIn(TOKEN, result.output)

    def test_an_empty_reply_is_a_connectivity_message(self):
        result, _ = self._invoke(["enable"], "")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("No response from box", result.output)


class Status(_Case):
    def test_each_state_is_reported_and_none_shows_a_value(self):
        expected = {"enabled": "enabled", "disabled": "disabled",
                    "unreadable": "UNREADABLE", "empty": "EMPTY"}
        for state, word in expected.items():
            # A box that (wrongly) sent a token with status must still not
            # have it shown: status prints the state and nothing else.
            result, backend = self._invoke(["status"], {"ok": True, "state": state, "token": TOKEN})
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn(f"{BOX_IP}: MCP token {word}", result.output)
            self.assertNotIn(TOKEN, result.output)
            self.assertEqual(backend.calls[0][1], (verbs.MCP_TOKEN_STATUS,))

    def test_the_two_states_that_refuse_everyone_say_how_to_get_out(self):
        for state in ("unreadable", "empty"):
            result, _ = self._invoke(["status"], {"ok": True, "state": state})
            self.assertIn("refuses every request", result.output)
            self.assertIn("rotate", result.output)
            self.assertIn("disable", result.output)

    def test_json_output_is_the_box_and_the_state(self):
        result, _ = self._invoke(["status", "--json"], {"ok": True, "state": "enabled", "token": TOKEN})
        self.assertEqual(json.loads(result.output), {"box": BOX_IP, "state": "enabled"})


class Disable(_Case):
    def test_disable_asks_first_and_a_no_never_reaches_the_box(self):
        result, backend = self._invoke(["disable"], {"ok": True}, input="n\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(backend.calls, [])

    def test_disable_with_yes_removes_it(self):
        result, backend = self._invoke(
            ["disable", "--yes"], {"ok": True, "state": "disabled", "previous": "enabled"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("removed", result.output)
        self.assertEqual(backend.calls[0][1], (verbs.MCP_TOKEN_DISABLE,))

    def test_disable_on_a_box_with_no_token_says_so(self):
        result, _ = self._invoke(
            ["disable", "--yes"], {"ok": True, "state": "disabled", "previous": "disabled"})
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("No MCP token was set", result.output)


class OldBoxAndWireNames(_Case):
    def test_a_box_that_predates_the_verbs_is_told_to_update(self):
        for argv in (["status"], ["enable"], ["rotate", "--yes"], ["disable", "--yes"]):
            verb = "mcp-token-" + argv[0]
            result, _ = self._invoke(argv, {"ok": False, "error": f"unknown command: {verb}"})
            self.assertEqual(result.exit_code, 1, argv)
            self.assertIn("lager update", result.output, argv)

    def test_every_verb_the_cli_sends_is_one_the_box_dispatches(self):
        shim = (REPO / "box" / "lager" / "box_config" / "box_config_cli.py").read_text()
        for verb in (verbs.MCP_TOKEN_STATUS, verbs.MCP_TOKEN_ENABLE,
                     verbs.MCP_TOKEN_ROTATE, verbs.MCP_TOKEN_DISABLE):
            self.assertIn(f'"{verb}":', shim, verb)

    def test_there_is_no_command_that_shows_the_token_again(self):
        self.assertEqual(sorted(box_config_cli.mcp_token_group.commands),
                         ["disable", "enable", "rotate", "status"])


if __name__ == "__main__":
    unittest.main()

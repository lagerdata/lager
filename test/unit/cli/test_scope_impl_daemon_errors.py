# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The scope impl script must read a daemon refusal as a failure.

The daemon answers a setting it refused as ``{"Response": {"response":
"Error", "message": ...}}``, with no top-level ``error``. Every caller of
``send_command_pico`` checked ``"error" in response``, so a trigger level
beyond the range printed "Trigger configured successfully" while the daemon
had refused it.
"""

import importlib.util
import json
import os
import sys
import types
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
IMPL = os.path.join(REPO_ROOT, "cli", "impl", "measurement", "scope.py")


def _load_impl():
    spec = importlib.util.spec_from_file_location("_scope_impl_under_test", IMPL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Socket:
    def __init__(self, reply):
        self._reply = reply
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        return json.dumps(self._reply)


def _send(reply, command=None):
    socket = _Socket(reply)
    websockets = types.ModuleType("websockets")
    websockets.connect = lambda uri, **kwargs: socket
    with mock.patch.dict(sys.modules, {"websockets": websockets}):
        result = _load_impl().send_command_pico(command or {"command": "SetTriggerLevel"})
    return result, socket


class SendCommandPicoTests(unittest.TestCase):

    def test_a_refusal_comes_back_as_an_error(self):
        result, _ = _send({"Response": {"response": "Error",
                                        "message": "Voltage out of range"}})
        self.assertEqual(result, {"error": "Voltage out of range"})

    def test_a_refusal_without_a_message_is_still_an_error(self):
        result, _ = _send({"Response": {"response": "Error"}})
        self.assertIn("error", result)

    def test_success_is_passed_through(self):
        reply = {"Response": {"response": "Ok"}}
        result, socket = _send(reply, {"command": "SetTriggerLevel", "trigger_level": 0.5})
        self.assertEqual(result, reply)
        self.assertEqual(socket.sent, [{"command": "SetTriggerLevel", "trigger_level": 0.5}])


if __name__ == "__main__":
    unittest.main()

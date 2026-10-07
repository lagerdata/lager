# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The scope impl scripts must read a daemon refusal as a failure.

The daemon answers a setting it refused as ``{"Response": {"response":
"Error", "message": ...}}``, with no top-level ``error``. Every caller of
``send_command_pico`` checked ``"error" in response``, so a trigger level
beyond the range printed "Trigger configured successfully" while the daemon
had refused it.

scope_stream.py read replies the same way, and worse: ``stream capture``
looked for ``is_ready`` and ``triggered_data`` at the top level, found
neither, wrote nothing and exited 0. Captures arrive as LSCP binary frames,
which it never read at all.

scope.py also matched nets on role "scope" alone, so for a scope-channel net
the PicoScope refusals never fired, and two of them exited 0.
"""

import asyncio
import csv
import importlib.util
import io
import json
import math
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
IMPL = os.path.join(REPO_ROOT, "cli", "impl", "measurement", "scope.py")
STREAM_IMPL = os.path.join(REPO_ROOT, "cli", "impl", "measurement", "scope_stream.py")


def _load_impl(path=IMPL, name="_scope_impl_under_test"):
    spec = importlib.util.spec_from_file_location(name, path)
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


# ---------------------------------------------------------------------------
# scope_stream.py
# ---------------------------------------------------------------------------

def _frame(channels, interval_ns=8.0, scale=0.5, offset=0.25, seq=1):
    """An LSCP/1 frame, built field by field from the daemon's layout."""
    per_channel = len(channels[0][1])
    header = struct.pack(
        "<IHHQQdIIIBBHI", 0x5043534C, 1, 0, seq, 0, interval_ns,
        0, per_channel, per_channel, len(channels), 8, 0, 0).ljust(64, b"\0")
    descriptors = b"".join(
        struct.pack("<BBBBffI", ord(name) - ord("A"), 0, 0, 0, scale, offset, 0)
        for name, _ in channels)
    payload = b"".join(struct.pack(f"<{len(counts)}h", *counts) for _, counts in channels)
    return header + descriptors + payload


class _Daemon:
    """A stand-in daemon: replies to each command by name, Ok by default.

    ``frames`` are pushed after a Subscribe. A socket with nothing left to
    send blocks, as an idle connection does, until the caller times out.
    """

    def __init__(self, replies=None, frames=(), refuse_connection=False):
        self.replies = replies or {}
        self.frames = list(frames)
        self.refuse_connection = refuse_connection
        self.sent = []

    def connect(self, uri, **kwargs):
        if self.refuse_connection:
            raise ConnectionRefusedError(111, "Connection refused")
        return _DaemonSocket(self)

    def names(self):
        return [command["command"] for command in self.sent]


class _DaemonSocket:
    def __init__(self, daemon):
        self.daemon = daemon
        self.outbox = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        command = json.loads(text)
        self.daemon.sent.append(command)
        reply = self.daemon.replies.get(command["command"], {"response": "Ok"})
        self.outbox.append(json.dumps({"Response": reply}))
        if command["command"] == "Subscribe":
            self.outbox.extend(self.daemon.frames)

    async def recv(self):
        if self.outbox:
            return self.outbox.pop(0)
        await asyncio.sleep(3600)


def _refusal(message):
    return {"response": "Error", "message": message}


ACQUIRING = {"GetState": {"response": "State", "state": {"acquiring": True, "rolling": False}}}
STOPPED = {"GetState": {"response": "State", "state": {"acquiring": False, "rolling": False}}}


def _run_stream(daemon, action, **params):
    """Run one scope_stream.py action against ``daemon``.

    Returns (exit code, stdout, stderr).
    """
    websockets = types.ModuleType("websockets")
    websockets.connect = daemon.connect
    with mock.patch.dict(sys.modules, {"websockets": websockets}):
        module = _load_impl(STREAM_IMPL, "_scope_stream_under_test")
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            getattr(module, action)(params)
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class StreamCommandRepliesTests(unittest.TestCase):

    def test_a_refused_setting_stops_the_start_before_acquisition(self):
        daemon = _Daemon({"SetVoltsPerDiv": _refusal("Voltage range out of bounds")})
        code, out, err = _run_stream(daemon, "stream_start", channel="A")
        self.assertEqual(code, 1)
        self.assertIn("Voltage range out of bounds", err)
        self.assertNotIn("Streaming started", out)
        self.assertNotIn("StartAcquisition", daemon.names())

    def test_a_refused_start_fails(self):
        daemon = _Daemon({"StartAcquisition": _refusal("no scope connected")})
        code, out, err = _run_stream(daemon, "stream_start", channel="A")
        self.assertEqual(code, 1)
        self.assertIn("no scope connected", err)
        self.assertNotIn("Streaming started", out)

    def test_a_clean_start_applies_everything_then_starts(self):
        daemon = _Daemon()
        code, out, _err = _run_stream(daemon, "stream_start", channel="B")
        self.assertEqual(code, 0)
        self.assertIn("Streaming started", out)
        self.assertEqual(daemon.names()[-1], "StartAcquisition")
        self.assertEqual(daemon.sent[0], {"command": "EnableChannel",
                                          "channel": {"Alphabetic": "B"}})

    def test_the_start_leaves_the_viewer_link_to_the_cli(self):
        """The link needs the user's sign-in token, which the box never sees."""
        code, out, _err = _run_stream(_Daemon(), "stream_start", channel="A")
        self.assertEqual(code, 0)
        self.assertNotIn("http", out)

    def test_a_refused_stop_fails(self):
        daemon = _Daemon({"StopAcquisition": _refusal("driver error")})
        code, out, err = _run_stream(daemon, "stream_stop")
        self.assertEqual(code, 1)
        self.assertIn("driver error", err)
        self.assertNotIn("Streaming stopped", out)

    def test_a_daemon_that_does_not_answer_fails_the_stop(self):
        code, _out, err = _run_stream(_Daemon(refuse_connection=True), "stream_stop")
        self.assertEqual(code, 1)
        self.assertIn("does not answer", err)

    def test_a_refused_config_setting_fails(self):
        daemon = _Daemon({"SetTriggerLevel": _refusal("level outside the range")})
        code, _out, err = _run_stream(daemon, "stream_config", trigger_level=99.0)
        self.assertEqual(code, 1)
        self.assertIn("level outside the range", err)

    def test_config_reports_each_setting_it_applied(self):
        daemon = _Daemon()
        code, out, _err = _run_stream(daemon, "stream_config", channel="B",
                                      volts_per_div=0.5)
        self.assertEqual(code, 0)
        self.assertIn("[OK] Set channel B to 0.5 V/div", out)
        self.assertNotIn("Response", out)
        self.assertEqual(daemon.sent, [{"command": "SetVoltsPerDiv",
                                        "channel": {"Alphabetic": "B"},
                                        "volts_per_div": 0.5}])

    def test_a_channel_setting_without_a_channel_is_refused(self):
        daemon = _Daemon()
        code, _out, err = _run_stream(daemon, "stream_config", volts_per_div=0.5)
        self.assertNotEqual(code, 0)
        self.assertIn("needs a channel", err)
        self.assertEqual(daemon.sent, [])

    def test_status_reads_the_replies_inside_the_envelope(self):
        daemon = _Daemon({
            "GetChannelCount": {"response": "GetChannelCount", "channel_count": 4},
            "IsReady": {"response": "IsReady", "is_ready": True},
        })
        code, out, _err = _run_stream(daemon, "stream_status")
        self.assertEqual(code, 0)
        self.assertIn("RUNNING", out)
        self.assertIn("Channels: 4", out)
        self.assertIn("Ready: True", out)

    def test_status_fails_when_the_daemon_does_not_answer(self):
        code, out, _err = _run_stream(_Daemon(refuse_connection=True), "stream_status")
        self.assertEqual(code, 1)
        self.assertIn("NOT RUNNING", out)

    def test_status_fails_when_the_daemon_cannot_reach_the_scope(self):
        daemon = _Daemon({"GetChannelCount": _refusal("no scope connected")})
        code, out, _err = _run_stream(daemon, "stream_status")
        self.assertEqual(code, 1)
        self.assertIn("no scope connected", out)


class StreamCaptureTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.output = os.path.join(self.dir, "capture.csv")

    def capture(self, daemon, **params):
        params = dict({"output": self.output, "duration": 0.05,
                       "netname": "pico1"}, **params)
        return _run_stream(daemon, "stream_capture", **params)

    def rows(self):
        with open(self.output, newline="") as handle:
            return list(csv.reader(handle))

    def test_subscribed_frames_become_csv_rows(self):
        frames = [_frame([("A", [0, 2, -4]), ("B", [10, 20, 30])], seq=1),
                  _frame([("A", [6, 8, 10]), ("B", [1, 1, 1])], seq=2)]
        daemon = _Daemon(ACQUIRING, frames)
        code, out, err = self.capture(daemon)
        self.assertEqual(code, 0, err)
        self.assertIn("Subscribe", daemon.names())
        rows = self.rows()
        self.assertEqual(rows[0], ["capture", "channel", "sample_index", "time_ns", "voltage"])
        self.assertEqual(len(rows), 1 + 2 * 2 * 3)
        # volts = count * scale + offset, with scale 0.5 and offset 0.25
        self.assertEqual(rows[1], ["0", "A", "0", "0.0", "0.25"])
        self.assertEqual(rows[3], ["0", "A", "2", "16.0", "-1.75"])
        self.assertEqual(rows[4], ["0", "B", "0", "0.0", "5.25"])
        self.assertEqual(rows[7][:3], ["1", "A", "0"])
        self.assertIn(self.output, out)

    def test_an_uncaptured_rolling_sample_is_written_as_nan(self):
        daemon = _Daemon(ACQUIRING, [_frame([("A", [4, -32768])])])
        code, _out, err = self.capture(daemon)
        self.assertEqual(code, 0, err)
        self.assertTrue(math.isnan(float(self.rows()[2][4])))

    def test_samples_caps_the_rows_per_channel(self):
        frames = [_frame([("A", [1, 2, 3])], seq=1), _frame([("A", [4, 5, 6])], seq=2)]
        code, _out, err = self.capture(_Daemon(ACQUIRING, frames), samples=4)
        self.assertEqual(code, 0, err)
        indexes = [(row[0], row[2]) for row in self.rows()[1:]]
        self.assertEqual(indexes, [("0", "0"), ("0", "1"), ("0", "2"), ("1", "0")])

    def test_no_capture_in_the_window_fails(self):
        code, out, err = self.capture(_Daemon(ACQUIRING, []))
        self.assertEqual(code, 1)
        self.assertIn("no capture arrived", err)
        self.assertNotIn("Capture complete", out)
        self.assertFalse(os.path.exists(self.output))

    def test_a_stopped_scope_fails_and_says_how_to_start_it(self):
        daemon = _Daemon(STOPPED, [_frame([("A", [1])])])
        code, _out, err = self.capture(daemon)
        self.assertEqual(code, 1)
        self.assertIn("stopped", err)
        self.assertIn("lager scope pico1 stream start", err)
        self.assertNotIn("Subscribe", daemon.names())

    def test_a_refusal_fails_the_capture(self):
        daemon = _Daemon({"GetState": _refusal("no scope connected")})
        code, _out, err = self.capture(daemon)
        self.assertEqual(code, 1)
        self.assertIn("no scope connected", err)

    def test_a_daemon_that_does_not_answer_fails_the_capture(self):
        code, _out, err = self.capture(_Daemon(refuse_connection=True))
        self.assertEqual(code, 1)
        self.assertIn("does not answer", err)

    def test_json_output_reports_a_failure_as_one(self):
        code, out, _err = self.capture(_Daemon(ACQUIRING, []), json_output=True)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "error")

    def test_a_dropped_capture_notice_is_passed_on(self):
        frames = [json.dumps({"Response": _refusal("dropped 3 captures: client is not keeping up")}),
                  _frame([("A", [1])])]
        code, _out, err = self.capture(_Daemon(ACQUIRING, frames))
        self.assertEqual(code, 0, err)
        self.assertIn("dropped 3 captures", err)

    def test_the_copy_command_names_where_the_file_is(self):
        daemon = _Daemon(ACQUIRING, [_frame([("A", [1])])])
        code, out, err = self.capture(daemon, scp_host="lagerdata@10.0.0.5")
        self.assertEqual(code, 0, err)
        self.assertIn(f"scp lagerdata@10.0.0.5:{self.output} .", out)


# ---------------------------------------------------------------------------
# scope.py: the PicoScope refusals
# ---------------------------------------------------------------------------

def _run_main(action, nets, daemon_reply=None, **params):
    """Run scope.py's main() for ``action`` with ``nets`` saved on the box.

    ``daemon_reply`` answers every daemon command, as send_command_pico
    returns it.
    """
    module = _load_impl()
    data = {"action": action, "params": dict({"netname": nets[0]["name"]}, **params)}
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(module, "load_saved_nets", return_value=nets), \
         mock.patch.object(module, "send_command_pico", return_value=daemon_reply), \
         mock.patch.dict(os.environ, {"LAGER_COMMAND_DATA": json.dumps(data)}), \
         redirect_stdout(out), redirect_stderr(err):
        try:
            module.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue() + err.getvalue()


PICO_CHANNEL = {"name": "scope2", "role": "scope-channel", "instrument": "Picoscope_2000",
                "pin": "2"}
PICO_INSTRUMENT = {"name": "picoscope1", "role": "scope", "instrument": "Picoscope_2000"}


class PicoScopeRefusalTests(unittest.TestCase):

    def test_display_is_refused_on_a_channel_net(self):
        code, output = _run_main("measure_vpp", [PICO_CHANNEL], display=True, cursor=False)
        self.assertEqual(code, 1)
        self.assertIn("no front panel", output)

    def test_display_is_refused_on_the_instrument_net(self):
        code, output = _run_main("measure_vpp", [PICO_INSTRUMENT], display=True, cursor=False)
        self.assertEqual(code, 1)
        self.assertIn("no front panel", output)

    def test_a_bus_trigger_is_refused_as_a_failure(self):
        for net in (PICO_INSTRUMENT, PICO_CHANNEL):
            with self.subTest(role=net["role"]):
                code, output = _run_main("trigger_uart", [net], role="scope")
                self.assertEqual(code, 1)
                self.assertIn("Only edge trigger", output)

    def test_a_refused_edge_setting_fails(self):
        code, output = _run_main("trigger_edge", [PICO_INSTRUMENT],
                                 daemon_reply={"error": "Voltage out of range"},
                                 mode=None, coupling=None, source=None, slope=None,
                                 level=9.0)
        self.assertEqual(code, 1)
        self.assertIn("Voltage out of range", output)
        self.assertNotIn("successfully", output)

    def test_an_accepted_edge_setting_succeeds(self):
        code, output = _run_main("trigger_edge", [PICO_INSTRUMENT],
                                 daemon_reply={"response": "Ok"},
                                 mode=None, coupling=None, source=None, slope=None,
                                 level=0.5)
        self.assertEqual(code, 0)
        self.assertIn("successfully", output)

    def test_front_panel_cursors_are_refused_as_a_failure(self):
        for net in (PICO_INSTRUMENT, PICO_CHANNEL):
            with self.subTest(role=net["role"]):
                code, output = _run_main("set_a", [net], x=0.1, y=0.2)
                self.assertEqual(code, 1)
                self.assertIn("no front panel", output)
                self.assertIn("cursor time", output)

    def test_a_logic_invocation_still_matches_its_role_only(self):
        module = _load_impl()
        with mock.patch.object(module, "load_saved_nets", return_value=[PICO_CHANNEL]), \
             mock.patch.object(module, "_NET_ROLE", "logic"):
            self.assertIsNone(module.get_net_info("scope2"))


if __name__ == "__main__":
    unittest.main()

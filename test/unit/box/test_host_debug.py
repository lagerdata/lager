# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the pieces that put the debug probe and the oscilloscope on the
host when the Box container cannot reach hardware (macOS):

* ``lager.hostlink.tcp_forward`` -- the container's plain TCP forward
* ``lager.hostlink.debug_proxy`` -- the container's stand-in for a debug net
* ``lager.host.debug_rpc``       -- the agent route that runs the real one
* ``lager.host.darwin_debug``    -- the macOS adjustments to the debug service
* ``lager.host.__main__``        -- the supervisor's children
* the ``LAGER_HOSTLINK`` switch in ``lager.nets.debug_net``
* ``lager.hostlink.usb_proxy`` / ``lager.host.usb_rpc`` -- the same hand-off
  for USB hub ports, and its switch in the hub dispatcher

No probe, GDB or J-Link software is involved: sockets are real loopback
sockets, everything else is a stand-in. That the adjustments match what a
real J-Link and a real GDB do was established on hardware and is recorded in
docs/macos-port-plan.md; these tests pin the logic so it stays that way.
"""

import base64
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

from flask import Flask

from lager.host import __main__ as host_main
from lager.automation.usb_hub import dispatcher, usb_net
from lager.host import darwin_debug, debug_rpc, usb_rpc
from lager.hostlink import config, debug_proxy, tcp_forward, usb_proxy

BOX_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "box")


def _echo_server(reply_prefix=b""):
    """A loopback server that echoes each connection until it closes."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)

    def serve():
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(reply_prefix + data)

    threading.Thread(target=serve, daemon=True).start()
    return listener


class TcpForwardTests(unittest.TestCase):
    def _forwarder(self, to_port, host="127.0.0.1"):
        server = tcp_forward.Forwarder(("127.0.0.1", 0), to_port, lambda: host)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_bytes_pass_both_ways_unchanged(self):
        upstream = _echo_server(b"host:")
        self.addCleanup(upstream.close)
        port = self._forwarder(upstream.getsockname()[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"\x00\xffping")
            self.assertEqual(client.recv(100), b"host:\x00\xffping")

    def test_client_half_close_still_delivers_the_reply(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)

        def serve():
            conn, _ = listener.accept()
            with conn:
                received = b""
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    received += data
                conn.sendall(b"got " + received)

        threading.Thread(target=serve, daemon=True).start()
        port = self._forwarder(listener.getsockname()[1])
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(b"all of it")
            client.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(100), b"got all of it")

    def test_unreachable_host_service_closes_the_client(self):
        port = self._forwarder(config.free_loopback_port())
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            self.assertEqual(client.recv(100), b"")

    def test_host_is_resolved_per_connection(self):
        upstream = _echo_server()
        self.addCleanup(upstream.close)
        hosts = ["203.0.113.1", "127.0.0.1"]
        seen = []

        def resolve():
            seen.append(1)
            return hosts[-1]

        server = tcp_forward.Forwarder(("127.0.0.1", 0), upstream.getsockname()[1], resolve)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for _ in range(2):
            with socket.create_connection(server.server_address, timeout=5) as client:
                client.sendall(b"x")
                self.assertEqual(client.recv(10), b"x")
        self.assertEqual(len(seen), 2)

    def test_target_host_comes_from_the_endpoint_record(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "host_agent.json")
            config.save_endpoint(config.Endpoint(token="t" * 40, port=1234,
                                                 connect_host="mac.example"), path)
            self.assertEqual(tcp_forward.target_host(path), "mac.example")

    def test_target_host_defaults_when_there_is_no_record(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = os.path.join(directory, "none.json")
            self.assertEqual(tcp_forward.target_host(missing), config.DEFAULT_CONNECT_HOST)

    def test_host_ports_are_fixed_and_distinct_from_the_box_ports(self):
        self.assertEqual(config.HOST_DEBUG_PORT, 48765)
        self.assertEqual(config.HOST_SCOPE_PORT, 48085)


class _FakeRtt:
    """Stands in for ``lager.debug.RTT``: queued up-channel data, recorded writes."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.written = []
        self.entered = False
        self.exited = threading.Event()

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited.set()
        return False

    def read_some(self, timeout=1.0):
        if self.chunks:
            return self.chunks.pop(0)
        time.sleep(0.01)
        return None

    def write(self, data):
        self.written.append(data)
        return len(data)


class _FakeDebug:
    serial = "000123"
    backend = "jlink"
    device = "NRF52840_XXAA"
    gdb_port = 2331
    rtt_telnet_port = 9090

    def __init__(self):
        self.calls = []
        self.rtt_session = _FakeRtt()
        self.flashed = None

    def reset(self, halt=False):
        self.calls.append(("reset", halt))
        return {"status": "ok"}

    def read_memory(self, address, length):
        return bytes(range(length))

    def status(self):
        raise ValueError("probe unplugged")

    def flash(self, path, flash_address=None):
        with open(path, "rb") as fh:
            self.flashed = (os.path.basename(path), fh.read(), flash_address)
        return "flashed"

    def rtt(self, **kwargs):
        self.calls.append(("rtt", kwargs))
        return self.rtt_session

    def secret(self):  # not on the allow-list
        raise AssertionError("must never be called")


class DebugRpcTests(unittest.TestCase):
    def setUp(self):
        self.dbg = _FakeDebug()
        self.made = []

        def make(name, net_info):
            self.made.append((name, net_info))
            return self.dbg

        patcher = patch.object(debug_rpc, "_make", make)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = debug_rpc.install(Flask(__name__)).test_client()

    def _call(self, **body):
        body.setdefault("name", "dbg")
        body.setdefault("net_info", {"instrument": "J-Link"})
        return self.client.post(debug_rpc.CALL_PATH, json=body)

    def test_call_reaches_the_real_object_with_its_net(self):
        resp = self._call(method="reset", kwargs={"halt": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"result": {"status": "ok"}})
        self.assertEqual(self.dbg.calls, [("reset", True)])
        self.assertEqual(self.made, [("dbg", {"instrument": "J-Link"})])

    def test_bytes_come_back_base64(self):
        resp = self._call(method="read_memory", args=[0x20000000, 4])
        self.assertEqual(base64.b64decode(resp.get_json()["result_b64"]), b"\x00\x01\x02\x03")

    def test_describe_reports_attributes_absent_ones_as_none(self):
        described = self._call(method="describe").get_json()["result"]
        self.assertEqual(described["serial"], "000123")
        self.assertEqual(described["gdb_port"], 2331)
        self.assertIsNone(described["openocd_tcl_port"])

    def test_a_method_off_the_allow_list_is_refused_not_looked_up(self):
        resp = self._call(method="secret")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("secret", resp.get_json()["error"])

    def test_dunder_and_private_names_are_refused(self):
        for name in ("__class__", "_net_info", "rtt"):
            self.assertEqual(self._call(method=name).status_code, 400, name)

    def test_body_without_a_method_is_a_bad_request(self):
        self.assertEqual(self.client.post(debug_rpc.CALL_PATH, json={"name": "dbg"}).status_code, 400)
        self.assertEqual(self.client.post(debug_rpc.CALL_PATH, data="nope").status_code, 400)

    def test_driver_error_is_returned_with_its_type(self):
        resp = self._call(method="status")
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {"error": "probe unplugged", "type": "ValueError"})

    def test_flash_writes_the_sent_bytes_to_a_file_that_is_then_removed(self):
        firmware = {"name": "app.hex", "data": base64.b64encode(b":00000001FF").decode()}
        resp = self._call(method="flash", args=["/container/app.hex", 0x1000], firmware=firmware)
        self.assertEqual(resp.get_json(), {"result": "flashed"})
        name, data, address = self.dbg.flashed
        self.assertTrue(name.endswith(".hex"))
        self.assertEqual(data, b":00000001FF")
        self.assertEqual(address, 0x1000)
        self.assertEqual([f for f in os.listdir(tempfile.gettempdir())
                          if f == name], [])

    def test_rtt_open_bridges_one_loopback_client_both_ways(self):
        self.dbg.rtt_session.chunks = [b"up-1", b"up-2"]
        resp = self._call(method="rtt_open", kwargs={"channel": 0})
        port = resp.get_json()["result"]["port"]
        self.assertTrue(self.dbg.rtt_session.entered)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.settimeout(5)
            received = b""
            while len(received) < 8:
                received += client.recv(100)
            self.assertEqual(received, b"up-1up-2")
            client.sendall(b"down")
            deadline = time.monotonic() + 5
            while not self.dbg.rtt_session.written and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.dbg.rtt_session.written, [b"down"])
        # Client gone: the real session must be closed, or the probe stays held.
        self.assertTrue(self.dbg.rtt_session.exited.wait(5))

    def test_rtt_session_is_closed_when_no_client_ever_connects(self):
        with patch.object(debug_rpc, "_ACCEPT_TIMEOUT_S", 0.1):
            self._call(method="rtt_open")
            self.assertTrue(self.dbg.rtt_session.exited.wait(5))

    def test_rtt_listener_is_loopback_only(self):
        bound = []
        real_socket = socket.socket

        class Recording(real_socket):
            def bind(self, address):
                bound.append(address)
                return super().bind(address)

        with patch.object(debug_rpc.socket, "socket", Recording), \
                patch.object(debug_rpc, "_ACCEPT_TIMEOUT_S", 0.1):
            self._call(method="rtt_open")
            self.dbg.rtt_session.exited.wait(5)
        self.assertEqual(bound[0][0], "127.0.0.1")


class _Response:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class DebugProxyTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.reply = _Response(200, {"result": None})

        def post(url, data=None, headers=None, timeout=None):
            self.posts.append((url, json.loads(data), timeout))
            return self.reply

        patcher = patch.object(debug_proxy.requests, "post", post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.net = debug_proxy.make_debug("dbg", {"instrument": "J-Link"})

    def test_call_goes_to_the_relay_with_the_net(self):
        self.reply = _Response(200, {"result": {"status": "ok"}})
        self.assertEqual(self.net.reset(halt=True), {"status": "ok"})
        url, body, timeout = self.posts[0]
        self.assertEqual(url, "http://localhost:8080/v1/debug/call")
        self.assertEqual(body["name"], "dbg")
        self.assertEqual(body["net_info"], {"instrument": "J-Link"})
        self.assertEqual((body["method"], body["kwargs"]), ("reset", {"halt": True}))
        self.assertGreaterEqual(timeout, 600)

    def test_bytes_result_is_decoded(self):
        self.reply = _Response(200, {"result_b64": base64.b64encode(b"\x01\x02").decode()})
        self.assertEqual(self.net.read_memory(0x20000000, 2), b"\x01\x02")
        self.assertEqual(self.posts[0][1]["args"], [0x20000000, 2])

    def test_agent_error_is_raised_with_its_message(self):
        self.reply = _Response(500, {"error": "probe unplugged", "type": "ValueError"})
        with self.assertRaisesRegex(RuntimeError, "probe unplugged"):
            self.net.status()

    def test_relay_error_message_is_raised(self):
        self.reply = _Response(503, {"error": "The host agent is not running.",
                                     "code": "host_agent_unreachable", "retryable": True})
        with self.assertRaisesRegex(RuntimeError, "not running"):
            self.net.halt()

    def test_non_json_error_is_raised_as_text(self):
        self.reply = _Response(502, None, text="bad gateway")
        with self.assertRaisesRegex(RuntimeError, "bad gateway"):
            self.net.erase()

    def test_flash_sends_the_file_contents(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(b"\xde\xad\xbe\xef")
        self.addCleanup(os.unlink, fh.name)
        self.net.flash(fh.name, 0x8000000)
        body = self.posts[0][1]
        self.assertEqual(base64.b64decode(body["firmware"]["data"]), b"\xde\xad\xbe\xef")
        self.assertEqual(body["firmware"]["name"], os.path.basename(fh.name))
        self.assertEqual(body["args"][1], 0x8000000)

    def test_attributes_are_described_once_then_cached(self):
        self.reply = _Response(200, {"result": {"serial": "000123", "gdb_port": 2331}})
        self.assertEqual(self.net.serial, "000123")
        self.assertEqual(self.net.gdb_port, 2331)
        self.assertEqual(len(self.posts), 1)
        with self.assertRaises(AttributeError):
            self.net.no_such_thing

    def test_private_attribute_lookup_makes_no_call(self):
        with self.assertRaises(AttributeError):
            self.net._missing
        self.assertEqual(self.posts, [])

    def test_session_connects_and_disconnects(self):
        with self.net.session(speed=4000) as inside:
            self.assertIs(inside, self.net)
        self.assertEqual([p[1]["method"] for p in self.posts], ["connect", "disconnect"])

    def test_session_teardown_does_not_mask_the_body_error(self):
        calls = []

        def post(url, data=None, headers=None, timeout=None):
            calls.append(json.loads(data)["method"])
            if calls[-1] == "disconnect":
                return _Response(500, {"error": "gone"})
            return _Response(200, {"result": None})

        with patch.object(debug_proxy.requests, "post", post):
            with self.assertRaisesRegex(KeyError, "body"):
                with self.net.session():
                    raise KeyError("body")
        self.assertEqual(calls, ["connect", "disconnect"])

    def test_rtt_reads_and_writes_over_the_bridged_port(self):
        upstream = _echo_server(b"rtt:")
        self.addCleanup(upstream.close)
        self.reply = _Response(200, {"result": {"port": upstream.getsockname()[1]}})
        with patch.object(debug_proxy.tcp_forward, "target_host", lambda: "127.0.0.1"):
            with self.net.rtt(channel=1) as rtt:
                self.assertEqual(self.posts[0][1]["method"], "rtt_open")
                self.assertEqual(self.posts[0][1]["kwargs"]["channel"], 1)
                self.assertIsNone(rtt.read_some(timeout=0.05))
                self.assertEqual(rtt.write("hi"), 2)
                self.assertEqual(rtt.read_some(timeout=5), b"rtt:hi")
            self.assertIsNone(rtt.read_some(timeout=0.05))
            with self.assertRaises(RuntimeError):
                rtt.write(b"x")

    def test_rtt_read_after_the_host_closes_returns_none(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        threading.Thread(target=lambda: listener.accept()[0].close(), daemon=True).start()
        self.reply = _Response(200, {"result": {"port": listener.getsockname()[1]}})
        with patch.object(debug_proxy.tcp_forward, "target_host", lambda: "127.0.0.1"):
            with self.net.rtt() as rtt:
                self.assertIsNone(rtt.read_some(timeout=5))
                self.assertIsNone(rtt.read_some(timeout=0.05))


class DebugNetSwitchTests(unittest.TestCase):
    """The one Linux file this touches: default off, on only with the variable."""

    def _make_debug_module(self, env_value):
        env = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
        if env_value is not None:
            env["LAGER_HOSTLINK"] = env_value
        env["PYTHONPATH"] = os.path.abspath(BOX_DIR)
        out = subprocess.run(
            [sys.executable, "-c",
             "from lager.nets import debug_net; print(debug_net.make_debug.__module__)"],
            env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip().splitlines()[-1]

    def test_unset_keeps_the_box_implementation(self):
        self.assertEqual(self._make_debug_module(None), "lager.nets.debug_net")

    def test_any_other_value_keeps_the_box_implementation(self):
        for value in ("0", "", "true", "yes"):
            self.assertEqual(self._make_debug_module(value), "lager.nets.debug_net", value)

    def test_one_selects_the_host_proxy(self):
        self.assertEqual(self._make_debug_module("1"), "lager.hostlink.debug_proxy")


class ProcShimTests(unittest.TestCase):
    def test_cmdline_is_nul_separated(self):
        with patch.object(darwin_debug, "_ps", return_value="/x/JLinkGDBServerCLExe -port 2331"):
            self.assertEqual(darwin_debug.proc_open("/proc/42/cmdline", "rb").read(),
                             b"/x/JLinkGDBServerCLExe\0-port\x002331\0")
            self.assertEqual(darwin_debug.proc_open("/proc/42/cmdline").read(),
                             "/x/JLinkGDBServerCLExe\0-port\x002331\0")

    def test_stat_has_state_third_and_parent_fourth(self):
        with patch.object(darwin_debug, "_ps", return_value="S+   1"):
            fields = darwin_debug.proc_open("/proc/42/stat").read().split()
        self.assertEqual(fields[0], "42")
        self.assertEqual(fields[2], "S")
        self.assertEqual(fields[3], "1")

    def test_missing_process_raises_what_proc_would(self):
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with patch.object(darwin_debug.subprocess, "run", return_value=failed):
            with self.assertRaises(FileNotFoundError):
                darwin_debug.proc_open("/proc/999999/cmdline", "rb")

    def test_other_paths_are_opened_normally(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("plain")
        self.addCleanup(os.unlink, fh.name)
        with patch.object(darwin_debug, "_ps", side_effect=AssertionError("not for this")):
            with darwin_debug.proc_open(fh.name) as opened:
                self.assertEqual(opened.read(), "plain")
            # Only the two files the service reads are answered.
            with self.assertRaises(OSError):
                darwin_debug.proc_open("/proc/42/environ")
            with self.assertRaises(OSError):
                darwin_debug.proc_open(12345678)


class CommanderWrapperTests(unittest.TestCase):
    def test_wrapper_adds_nogui_and_is_private(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = os.path.join(parent, "wrap")
            path = darwin_debug.commander_wrapper("/Applications/SEGGER/JLink/JLinkExe", directory)
            self.assertEqual(os.path.basename(path), "JLinkExe")
            with open(path) as fh:
                script = fh.read()
            self.assertIn('exec "/Applications/SEGGER/JLink/JLinkExe" -NoGui 1 "$@"', script)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)

    @unittest.skipUnless(os.name == "posix", "runs a shell script")
    def test_wrapper_passes_arguments_through(self):
        with tempfile.TemporaryDirectory() as directory:
            path = darwin_debug.commander_wrapper("/bin/echo", directory)
            out = subprocess.run([path, "-device", "A B"], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "-NoGui 1 -device A B")


class _FakeGdb:
    """Stands in for pygdbmi's controller."""

    def __init__(self, command=None, replies=None, late=()):
        self.command = command
        self.replies = replies or {}
        self.late = list(late)
        self.written = []
        self.gdb_process = types.SimpleNamespace(poll=lambda: None)

    def write(self, command, **kwargs):
        self.written.append(command)
        return list(self.replies.get(command, []))

    def get_gdb_response(self, **kwargs):
        return [self.late.pop(0)] if self.late else []


class GdbControllerTests(unittest.TestCase):
    def test_gdb_multiarch_is_replaced_and_other_commands_are_not(self):
        factory = darwin_debug._gdb_controller(_FakeGdb, "/opt/homebrew/bin/arm-none-eabi-gdb")
        self.assertEqual(factory(["gdb-multiarch", "--interpreter=mi3"]).command,
                         ["/opt/homebrew/bin/arm-none-eabi-gdb", "--interpreter=mi3"])
        self.assertEqual(factory(["lldb"]).command, ["lldb"])

    def test_connect_first_turns_off_the_query_jlink_never_answers(self):
        gdbmi = _FakeGdb(replies={"tar ext 127.0.0.1:2331": [{"type": "result", "message": "done"}]})
        darwin_debug._wait_for_connect_result(gdbmi)
        gdbmi.write("tar ext 127.0.0.1:2331", timeout_sec=5)
        self.assertEqual(gdbmi.written, [darwin_debug.QUIET_CONNECT_COMMAND,
                                         "tar ext 127.0.0.1:2331"])
        self.assertIn("fetch-exec-and-args-packet off", darwin_debug.QUIET_CONNECT_COMMAND)

    def test_connect_waits_for_a_late_result(self):
        refusal = {"type": "result", "message": "error",
                   "payload": {"msg": "Non-stop mode requested, but remote does not support non-stop"}}
        gdbmi = _FakeGdb(replies={"tar ext h:1": [{"type": "console"}]},
                         late=[{"type": "log"}, refusal])
        darwin_debug._wait_for_connect_result(gdbmi)
        self.assertIn(refusal, gdbmi.write("tar ext h:1"))

    def test_connect_wait_is_bounded(self):
        gdbmi = _FakeGdb(replies={"tar ext h:1": [{"type": "console"}]})
        darwin_debug._wait_for_connect_result(gdbmi)
        with patch.object(darwin_debug, "CONNECT_RESULT_WAIT_S", 0.05):
            self.assertEqual(gdbmi.write("tar ext h:1"), [{"type": "console"}])

    def test_other_commands_are_passed_straight_through(self):
        gdbmi = _FakeGdb(replies={"monitor go": [{"type": "console"}]})
        darwin_debug._wait_for_connect_result(gdbmi)
        self.assertEqual(gdbmi.write("monitor go"), [{"type": "console"}])
        self.assertEqual(gdbmi.written, ["monitor go"])


def _threads(*threads):
    return [{"type": "result", "message": "done", "payload": {"threads": list(threads)}}]


class StaleControllerTests(unittest.TestCase):
    def test_remote_alive_needs_a_thread(self):
        alive = _FakeGdb(replies={"-thread-info": _threads({"id": "1"})})
        gone = _FakeGdb(replies={"-thread-info": _threads()})
        errored = _FakeGdb(replies={"-thread-info": [
            {"type": "result", "message": "error",
             "payload": {"msg": "Remote communication error."}}]})
        silent = _FakeGdb()
        self.assertTrue(darwin_debug.remote_alive(alive))
        self.assertFalse(darwin_debug.remote_alive(gone))
        self.assertFalse(darwin_debug.remote_alive(errored))
        self.assertFalse(darwin_debug.remote_alive(silent))

    def test_remote_alive_is_false_when_gdb_itself_fails(self):
        broken = _FakeGdb()
        broken.write = lambda *a, **k: (_ for _ in ()).throw(OSError("pipe closed"))
        self.assertFalse(darwin_debug.remote_alive(broken))

    def _module(self, cache):
        module = types.SimpleNamespace(_gdb_controller_cache=cache, connects=0, cleaned=[])

        def get_controller(device=None, **kwargs):
            key = (device, "127.0.0.1", 2331)
            if key not in cache:
                module.connects += 1
                cache[key] = _FakeGdb(replies={"-thread-info": _threads({"id": "1"})})
            return cache[key]

        def cleanup_controller(key):
            module.cleaned.append(key)
            del cache[key]

        module.get_controller = get_controller
        module.cleanup_controller = cleanup_controller
        return module

    def test_new_controller_is_not_probed(self):
        module = self._module({})
        gdbmi = darwin_debug._fresh_controller(module)(device="NRF52840_XXAA")
        self.assertEqual(gdbmi.written, [])
        self.assertEqual(module.connects, 1)

    def test_live_cached_controller_is_reused(self):
        key = ("NRF52840_XXAA", "127.0.0.1", 2331)
        live = _FakeGdb(replies={"-thread-info": _threads({"id": "1"})})
        module = self._module({key: live})
        self.assertIs(darwin_debug._fresh_controller(module)(device="NRF52840_XXAA"), live)
        self.assertEqual(module.cleaned, [])
        self.assertEqual(module.connects, 0)

    def test_cached_controller_whose_server_is_gone_is_replaced(self):
        key = ("NRF52840_XXAA", "127.0.0.1", 2331)
        stale = _FakeGdb(replies={"-thread-info": _threads()})
        module = self._module({key: stale})
        gdbmi = darwin_debug._fresh_controller(module)(device="NRF52840_XXAA")
        self.assertIsNot(gdbmi, stale)
        self.assertEqual(module.cleaned, [key])
        self.assertEqual(module.connects, 1)


class FindGdbTests(unittest.TestCase):
    def test_path_is_searched_before_the_fixed_directories(self):
        with patch.object(darwin_debug.shutil, "which",
                          lambda name: "/usr/bin/" + name if name == "arm-none-eabi-gdb" else None), \
                patch.object(darwin_debug.os.path, "exists", lambda path: False):
            self.assertEqual(darwin_debug.find_gdb(), "/usr/bin/arm-none-eabi-gdb")

    def test_homebrew_is_found_without_it_on_path(self):
        with patch.object(darwin_debug.shutil, "which", lambda name: None), \
                patch.object(darwin_debug.os.path, "exists",
                             lambda path: path == "/opt/homebrew/bin/arm-none-eabi-gdb"):
            self.assertEqual(darwin_debug.find_gdb(), "/opt/homebrew/bin/arm-none-eabi-gdb")

    def test_none_when_there_is_no_arm_gdb(self):
        with patch.object(darwin_debug.shutil, "which", lambda name: None), \
                patch.object(darwin_debug.os.path, "exists", lambda path: False):
            self.assertIsNone(darwin_debug.find_gdb())


class SupervisorTests(unittest.TestCase):
    def _args(self, **overrides):
        values = {"config_dir": "/etc/lager", "scope_daemon": None}
        values.update(overrides)
        return types.SimpleNamespace(**values)

    def test_worker_and_debug_service_are_always_supervised(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(host_main.SCOPE_DAEMON_ENV, None)
            children = host_main._children(self._args())
        self.assertEqual([c.name for c in children], ["hardware worker", "debug service"])
        self.assertEqual(children[0].command[-3:], ["worker", "--config-dir", "/etc/lager"])
        self.assertEqual(children[1].command[-1], "debug")

    def test_scope_daemon_is_bound_to_loopback_on_the_host_port(self):
        for args, env in ((self._args(scope_daemon="/opt/daemon"), {}),
                          (self._args(), {host_main.SCOPE_DAEMON_ENV: "/opt/daemon"})):
            with patch.dict(os.environ, env):
                if not env:
                    os.environ.pop(host_main.SCOPE_DAEMON_ENV, None)
                scope = host_main._children(args)[-1]
            self.assertEqual(scope.command, ["/opt/daemon"])
            self.assertEqual(scope.env["LAGER_SCOPE_DATA_BIND"], "127.0.0.1")
            self.assertEqual(scope.env["LAGER_SCOPE_DATA_PORT"], "48085")

    @unittest.skipUnless(os.name == "posix", "uses POSIX signals")
    def test_child_is_restarted_after_it_exits_and_stops_on_request(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = os.path.join(directory, "runs")
            child = host_main._Child("counter", [
                sys.executable, "-c",
                f"open({marker!r}, 'a').write('x')"])
            stopping = threading.Event()
            with patch.object(host_main, "RESTART_DELAY_S", 0.05):
                thread = threading.Thread(target=child.run, args=(stopping,))
                thread.start()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if os.path.exists(marker) and os.path.getsize(marker) >= 2:
                        break
                    time.sleep(0.05)
                stopping.set()
                child.stop()
                thread.join(20)
            self.assertFalse(thread.is_alive())
            self.assertGreaterEqual(os.path.getsize(marker), 2)

    @unittest.skipUnless(os.name == "posix", "uses POSIX signals")
    def test_child_that_ignores_the_stop_request_is_killed(self):
        child = host_main._Child("stubborn", [
            sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(60)"])
        stopping = threading.Event()
        real_popen = subprocess.Popen
        with patch.object(host_main.subprocess, "Popen",
                          lambda command, env=None: real_popen(command, env=env,
                                                               stdout=subprocess.PIPE)):
            thread = threading.Thread(target=child.run, args=(stopping,))
            thread.start()
            deadline = time.monotonic() + 20
            while child.process is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(child.process.stdout.readline().strip(), b"ready")
            stopping.set()
            child.stop()
            time.sleep(0.3)
            self.assertIsNone(child.process.poll())  # still there: it ignored SIGTERM
            child.kill()
            thread.join(20)
        child.process.stdout.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(child.process.returncode, -signal.SIGKILL)

    def test_missing_binary_does_not_spin(self):
        child = host_main._Child("absent", ["/nonexistent/lager-scope-daemon"])
        thread = threading.Thread(target=child.run, args=(threading.Event(),))
        thread.start()
        thread.join(10)
        self.assertFalse(thread.is_alive())


class _FakeHub:
    def __init__(self):
        self.calls = []

    def _lock_key(self):
        return "USB0::0x24FF::0x0013::EC077668::INSTR"

    def enable(self, net_name, port):
        self.calls.append(("enable", net_name, port))

    def disable(self, net_name, port):
        self.calls.append(("disable", net_name, port))
        return True

    def toggle(self, net_name, port):
        self.calls.append(("toggle", net_name, port))
        return port == 1

    def state(self, net_name, port):
        raise usb_net.DeviceNotFoundError(
            "hub EC077668 did not open", classification=usb_net.HUB_UNREACHABLE,
            usb_context_healthy=True, detail="scan found 0 hubs")

    def cycle(self, net_name, port, off_time=None):
        self.calls.append(("cycle", net_name, port, off_time))
        return True

    def recover(self, net_name, port):
        self.calls.append(("recover", net_name, port))
        return True

    def states(self, ports, timeout=None):
        self.calls.append(("states", ports, timeout))
        return {port: port % 2 == 0 for port in ports}

    def firmware_update(self):  # not on the allow-list
        raise AssertionError("must never be called")


class UsbRpcTests(unittest.TestCase):
    def setUp(self):
        self.hub = _FakeHub()
        self.net_infos = []

        def controller(net_info):
            self.net_infos.append(net_info)
            return self.hub

        self.powered = []
        for target, name, value in (
                (usb_rpc, "_controller", controller),
                (usb_net, "note_powered_on", lambda c, port: self.powered.append(port))):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = usb_rpc.install(Flask(__name__)).test_client()

    def _call(self, method, *args, **kwargs):
        return self.client.post(usb_rpc.CALL_PATH, json={
            "net_info": {"instrument": "Acroname_8Port", "port": 0},
            "method": method, "args": list(args), "kwargs": kwargs})

    def test_call_reaches_the_driver_for_that_net(self):
        resp = self._call("cycle", "usbjl", 0, 2.0)
        self.assertEqual(resp.get_json(), {"result": True})
        self.assertEqual(self.hub.calls, [("cycle", "usbjl", 0, 2.0)])
        self.assertEqual(self.net_infos, [{"instrument": "Acroname_8Port", "port": 0}])

    def test_states_and_lock_key_are_served(self):
        self.assertEqual(self._call("states", [0, 1], timeout=3.0).get_json(),
                         {"result": {"0": True, "1": False}})
        self.assertEqual(self._call("_lock_key").get_json()["result"],
                         "USB0::0x24FF::0x0013::EC077668::INSTR")

    def test_powering_a_port_on_is_recorded_as_the_dispatcher_does(self):
        self._call("enable", "a", 3)
        self._call("recover", "a", 4)
        self._call("toggle", "a", 1)   # ends enabled
        self._call("toggle", "a", 2)   # ends disabled
        self._call("disable", "a", 5)
        self._call("cycle", "a", 6)
        self.assertEqual(self.powered, [3, 4, 1])

    def test_a_method_off_the_allow_list_is_refused_not_looked_up(self):
        for name in ("firmware_update", "__class__", "calls"):
            self.assertEqual(self._call(name).status_code, 400, name)
        self.assertEqual(self.net_infos, [])

    def test_body_without_a_method_is_a_bad_request(self):
        self.assertEqual(self.client.post(usb_rpc.CALL_PATH, json={}).status_code, 400)

    def test_driver_error_carries_its_class_and_fields(self):
        resp = self._call("state", "usbjl", 0)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.get_json(), {
            "error": "hub EC077668 did not open", "type": "DeviceNotFoundError",
            "classification": "hub-unreachable", "usb_context_healthy": True,
            "detail": "scan found 0 hubs"})


class UsbProxyTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.reply = _Response(200, {"result": None})

        def post(url, data=None, headers=None, timeout=None):
            self.posts.append((url, json.loads(data), timeout))
            return self.reply

        patcher = patch.object(usb_proxy.requests, "post", post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hub = usb_proxy.controller_for({"instrument": "Acroname_8Port", "port": 0})

    def test_each_operation_is_sent_with_the_net(self):
        self.reply = _Response(200, {"result": True})
        self.assertTrue(self.hub.cycle("usbjl", 0, 2.0))
        for name in ("enable", "disable", "toggle", "state", "recover"):
            getattr(self.hub, name)("usbjl", 0)
        url, body, timeout = self.posts[0]
        self.assertEqual(url, "http://localhost:8080/v1/usb/call")
        self.assertEqual(body, {"net_info": {"instrument": "Acroname_8Port", "port": 0},
                                "method": "cycle", "args": ["usbjl", 0, 2.0], "kwargs": {}})
        self.assertEqual([p[1]["method"] for p in self.posts[1:]],
                         ["enable", "disable", "toggle", "state", "recover"])
        # Longer than the longest cycle the drivers allow.
        self.assertGreater(timeout, 10 + usb_net.USB_REENUM_WAIT_S)

    def test_states_gives_back_the_callers_port_keys(self):
        self.reply = _Response(200, {"result": {"0": True, "3": False}})
        self.assertEqual(self.hub.states([0, 3, 5], timeout=2.0), {0: True, 3: False})
        self.assertEqual(self.posts[0][1]["kwargs"], {"timeout": 2.0})

    def test_lock_key_is_the_real_drivers(self):
        self.reply = _Response(200, {"result": "USB0::0x24FF::0x0013::EC077668::INSTR"})
        self.assertEqual(self.hub._lock_key(), "USB0::0x24FF::0x0013::EC077668::INSTR")

    def test_driver_error_is_raised_as_the_same_class_with_its_fields(self):
        self.reply = _Response(500, {
            "error": "hub EC077668 did not open", "type": "DeviceNotFoundError",
            "classification": "hub-unreachable", "usb_context_healthy": True,
            "detail": "scan found 0 hubs"})
        with self.assertRaises(usb_net.DeviceNotFoundError) as caught:
            self.hub.state("usbjl", 0)
        self.assertEqual(str(caught.exception), "hub EC077668 did not open")
        self.assertEqual(caught.exception.classification, usb_net.HUB_UNREACHABLE)
        self.assertTrue(caught.exception.usb_context_healthy)
        self.assertEqual(caught.exception.detail, "scan found 0 hubs")

    def test_each_handler_distinguished_class_survives_the_link(self):
        for name in ("HubOperationTimeout", "DeviceLockError", "LibraryMissingError",
                     "PortStateError", "USBBackendError"):
            self.reply = _Response(500, {"error": "x", "type": name})
            with self.assertRaises(getattr(usb_net, name)):
                self.hub.enable("usbjl", 0)

    def test_unknown_error_type_is_a_runtime_error(self):
        for name in ("OSError", "NoSuchClass", "logging", None):
            self.reply = _Response(500, {"error": "boom", "type": name})
            with self.assertRaises(RuntimeError) as caught:
                self.hub.enable("usbjl", 0)
            self.assertIs(type(caught.exception), RuntimeError, name)

    def test_link_failure_reads_as_the_relays_sentence(self):
        self.reply = _Response(503, {"error": "The host agent is not running.",
                                     "code": "host_agent_unreachable", "retryable": True})
        with self.assertRaisesRegex(RuntimeError, "not running"):
            self.hub.state("usbjl", 0)


class UsbDispatcherSwitchTests(unittest.TestCase):
    """The one Linux file the USB hand-off touches: default off."""

    NET = {"instrument": "Acroname_8Port", "port": 0,
           "address": "USB0::0x24FF::0x0013::EC077668::INSTR", "params": {}}

    def _controller(self, value):
        env = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
        if value is not None:
            env["LAGER_HOSTLINK"] = value
        with patch.dict(os.environ, env, clear=True):
            return dispatcher._controller_for(dict(self.NET))

    def test_unset_or_any_other_value_selects_the_box_driver(self):
        for value in (None, "0", "", "true"):
            self.assertEqual(type(self._controller(value)).__name__, "AcronameUSBNet", value)

    def test_one_selects_the_host_stand_in(self):
        self.assertIsInstance(self._controller("1"), usb_proxy.HostUSBNet)

    def test_stand_in_has_every_operation_the_dispatcher_uses(self):
        for name in ("enable", "disable", "toggle", "state", "cycle", "recover",
                     "states", "_lock_key"):
            self.assertTrue(callable(getattr(usb_proxy.HostUSBNet, name)), name)
            self.assertIn(name, usb_rpc._METHODS)


if __name__ == "__main__":
    unittest.main()

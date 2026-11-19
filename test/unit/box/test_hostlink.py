# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for box/lager/hostlink and box/lager/host: the link between a Box
container and a native host agent.

On a host where the container cannot reach hardware (Docker Desktop or
OrbStack on macOS), the hardware service runs natively and the container
forwards to it. These tests pin what that link adds around the Box's
existing ``/invoke`` interface: version negotiation, the token, the endpoint
record, and the relay's behaviour when the agent is missing, restarted,
re-provisioned, or the wrong version.

Everything here runs over real loopback sockets with a stand-in application
behind the agent's guard. No driver and no instrument is involved, so a pass
says the link is sound and nothing about hardware.
"""

import json
import os
import stat
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

import requests
from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from lager.host import worker
from lager.hostlink import auth, config, protocol, relay


class NegotiateTests(unittest.TestCase):
    def test_picks_the_highest_version_both_sides_support(self):
        self.assertEqual(protocol.negotiate(1, 3, own_min=2, own_max=5), 3)
        self.assertEqual(protocol.negotiate(2, 9, own_min=1, own_max=4), 4)

    def test_current_build_agrees_with_itself(self):
        self.assertEqual(
            protocol.negotiate(protocol.MIN_PROTOCOL_VERSION, protocol.PROTOCOL_VERSION),
            protocol.PROTOCOL_VERSION,
        )

    def test_disjoint_ranges_are_a_mismatch_in_both_directions(self):
        with self.assertRaises(protocol.ProtocolMismatch):
            protocol.negotiate(4, 5, own_min=1, own_max=3)
        with self.assertRaises(protocol.ProtocolMismatch):
            protocol.negotiate(1, 1, own_min=2, own_max=3)

    def test_malformed_ranges_are_rejected_not_negotiated(self):
        for bad in ((None, 1), (1, None), ("1", 1), (0, 1), (-1, 1), (True, 1), (1.0, 1), (3, 2)):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    protocol.negotiate(*bad)

    def test_error_body_reads_through_the_field_device_error_uses(self):
        body = protocol.error_body("some-code", "Plain sentence.", retryable=True, details="d")
        self.assertEqual(body["error"], "Plain sentence.")
        self.assertEqual(body["code"], "some-code")
        self.assertTrue(body["retryable"])
        self.assertEqual(body["details"], "d")
        self.assertNotIn("details", protocol.error_body("c", "m"))


class AuthTests(unittest.TestCase):
    TOKEN = "t" * 43

    def test_exact_token_is_accepted(self):
        self.assertTrue(auth.is_authorized(
            {"Authorization": auth.bearer_header(self.TOKEN)}, self.TOKEN))

    def test_scheme_is_case_insensitive(self):
        self.assertTrue(auth.is_authorized({"Authorization": f"bearer {self.TOKEN}"}, self.TOKEN))

    def test_everything_else_is_refused(self):
        cases = {
            "no header": {},
            "empty": {"Authorization": ""},
            "wrong token": {"Authorization": auth.bearer_header("x" * 43)},
            "prefix of token": {"Authorization": auth.bearer_header(self.TOKEN[:-1])},
            "token plus extra": {"Authorization": auth.bearer_header(self.TOKEN + "x")},
            "other scheme": {"Authorization": f"Basic {self.TOKEN}"},
            "bare token": {"Authorization": self.TOKEN},
            "scheme only": {"Authorization": "Bearer "},
        }
        for name, headers in cases.items():
            with self.subTest(name):
                self.assertFalse(auth.is_authorized(headers, self.TOKEN))

    def test_an_unset_expected_token_fails_closed(self):
        self.assertFalse(auth.is_authorized({"Authorization": "Bearer "}, ""))
        self.assertFalse(auth.is_authorized({"Authorization": auth.bearer_header("x")}, ""))


class EndpointRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "etc", config.ENDPOINT_FILENAME)

    def test_created_record_round_trips(self):
        created = config.ensure_endpoint(self.path)
        self.assertEqual(config.load_endpoint(self.path), created)
        self.assertEqual(created.bind, "127.0.0.1")
        self.assertGreaterEqual(len(created.token), 43)

    def test_the_agent_never_listens_beyond_loopback_by_default(self):
        self.assertEqual(config.DEFAULT_BIND, "127.0.0.1")

    def test_ensure_does_not_rotate_an_existing_token(self):
        first = config.ensure_endpoint(self.path)
        again = config.ensure_endpoint(self.path, port=1234)
        self.assertEqual(again, first)

    def test_tokens_are_not_reused(self):
        self.assertNotEqual(config.new_token(), config.new_token())

    def test_record_is_world_readable_but_not_writable(self):
        # Read by the container as another user; see save_endpoint.
        config.ensure_endpoint(self.path)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o644)

    def test_no_temporary_file_is_left_behind(self):
        config.ensure_endpoint(self.path)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), [config.ENDPOINT_FILENAME])

    def test_missing_record_is_an_endpoint_error(self):
        with self.assertRaises(config.EndpointError):
            config.load_endpoint(self.path)

    def test_unusable_records_are_refused(self):
        good = {"token": "t" * 43, "port": 5000}
        bad_records = [
            "not json",
            json.dumps([]),
            json.dumps({**good, "token": "short"}),
            json.dumps({"port": 5000}),
            json.dumps({**good, "port": 0}),
            json.dumps({**good, "port": 70000}),
            json.dumps({**good, "port": "5000"}),
            json.dumps({**good, "port": True}),
            json.dumps({**good, "bind": ""}),
            json.dumps({**good, "version": 99}),
        ]
        os.makedirs(os.path.dirname(self.path))
        for text in bad_records:
            with self.subTest(text=text[:40]):
                Path(self.path).write_text(text)
                with self.assertRaises(config.EndpointError):
                    config.load_endpoint(self.path)

    def test_an_unusable_existing_record_is_not_silently_replaced(self):
        os.makedirs(os.path.dirname(self.path))
        Path(self.path).write_text("{}")
        with self.assertRaises(config.EndpointError):
            config.ensure_endpoint(self.path)
        self.assertEqual(Path(self.path).read_text(), "{}")

    def test_ipv6_connect_host_is_bracketed(self):
        endpoint = config.Endpoint(token="t" * 43, port=8, connect_host="fd00::1")
        self.assertEqual(endpoint.base_url(), "http://[fd00::1]:8")

    def test_environment_overrides_the_record_location(self):
        os.environ[config.ENDPOINT_ENV] = "/somewhere/else.json"
        self.addCleanup(os.environ.pop, config.ENDPOINT_ENV, None)
        self.assertEqual(config.endpoint_path("/ignored"), "/somewhere/else.json")


def _stand_in_app():
    """A stand-in for the hardware service: enough routes to observe."""
    app = Flask("stand_in_hardware_service")
    app.seen_headers = []

    @app.route("/invoke", methods=["POST"])
    def invoke():
        app.seen_headers.append(dict(request.headers))
        data = request.get_json()
        if data.get("function") == "fail":
            return jsonify({"error": "Instrument said no", "details": "trace"}), 500
        return jsonify({"echo": data})

    @app.route("/health")
    def health():
        return jsonify({"status": "healthy"})

    return app


class _Served:
    """Run a WSGI app or relay server on a loopback port in a thread."""

    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1] if hasattr(server, "server_address") else server.port
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.server.shutdown()
        if hasattr(self.server, "server_close"):
            self.server.server_close()
        self._thread.join(timeout=5)


class AgentGuardTests(unittest.TestCase):
    """The agent's own front door, without the relay."""

    def setUp(self):
        self.endpoint = config.Endpoint(token=config.new_token(), port=1, connect_host="127.0.0.1")
        self.app = worker.install(_stand_in_app(), self.endpoint)
        self.client = self.app.test_client()
        self.auth = {"Authorization": auth.bearer_header(self.endpoint.token)}
        self.version = {protocol.PROTOCOL_HEADER: str(protocol.PROTOCOL_VERSION)}

    def test_every_route_requires_the_token(self):
        for method, path in (("get", "/health"), ("post", "/invoke"),
                             ("post", protocol.HANDSHAKE_PATH), ("get", "/no-such-route")):
            with self.subTest(path=path):
                resp = getattr(self.client, method)(path, headers=self.version)
                self.assertEqual(resp.status_code, 401)
                self.assertEqual(resp.get_json()["code"], protocol.ERR_UNAUTHORIZED)

    def test_a_wrong_token_never_reaches_the_service(self):
        resp = self.client.post("/invoke", json={"function": "x"},
                                headers={"Authorization": "Bearer nope", **self.version})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(self.app.seen_headers, [])

    def test_handshake_agrees_a_version(self):
        resp = self.client.post(protocol.HANDSHAKE_PATH, headers=self.auth,
                                json=protocol.handshake_request("v9.9.9"))
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["protocol"], protocol.PROTOCOL_VERSION)
        self.assertIn("agent_version", body)
        self.assertIn("platform", body)

    def test_handshake_refuses_an_incompatible_box(self):
        resp = self.client.post(protocol.HANDSHAKE_PATH, headers=self.auth,
                                json={"protocol_min": 98, "protocol_max": 99})
        self.assertEqual(resp.status_code, 426)
        self.assertEqual(resp.get_json()["code"], protocol.ERR_PROTOCOL_MISMATCH)

    def test_handshake_rejects_a_malformed_body(self):
        for body in (None, [], {"protocol_min": "1", "protocol_max": 1}, {}):
            with self.subTest(body=body):
                resp = self.client.post(protocol.HANDSHAKE_PATH, headers=self.auth, json=body)
                self.assertEqual(resp.status_code, 400)

    def test_requests_must_state_a_supported_protocol(self):
        resp = self.client.get("/health", headers=self.auth)
        self.assertEqual(resp.status_code, 426)
        resp = self.client.get("/health", headers={**self.auth, protocol.PROTOCOL_HEADER: "99"})
        self.assertEqual(resp.status_code, 426)
        resp = self.client.get("/health", headers={**self.auth, protocol.PROTOCOL_HEADER: "x"})
        self.assertEqual(resp.status_code, 426)
        resp = self.client.get("/health", headers={**self.auth, **self.version})
        self.assertEqual(resp.status_code, 200)

    def test_installing_again_swaps_the_token_without_stacking_hooks(self):
        rotated = config.Endpoint(token=config.new_token(), port=1)
        worker.install(self.app, rotated)
        self.assertEqual(len(self.app.before_request_funcs[None]), 1)
        old = self.client.get("/health", headers={**self.auth, **self.version})
        self.assertEqual(old.status_code, 401)
        new = self.client.get("/health", headers={
            "Authorization": auth.bearer_header(rotated.token), **self.version})
        self.assertEqual(new.status_code, 200)


class RelayTests(unittest.TestCase):
    """The relay against a live agent guard, over loopback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.endpoint_file = os.path.join(self._tmp.name, config.ENDPOINT_FILENAME)
        self.agent = None
        self.agent_app = None
        self._start_agent()
        self.link = relay.Link(self.endpoint_file)
        self.relay = _Served(relay.make_server(self.link, "127.0.0.1", 0))
        self.addCleanup(self.relay.stop)
        self.url = f"http://127.0.0.1:{self.relay.port}"

    def tearDown(self):
        self._stop_agent()

    def _start_agent(self, token=None):
        self.agent_app = _stand_in_app()
        server = make_server("127.0.0.1", 0, self.agent_app, threaded=True)
        endpoint = config.Endpoint(token=token or config.new_token(), port=server.port,
                                   connect_host="127.0.0.1")
        worker.install(self.agent_app, endpoint)
        config.save_endpoint(endpoint, self.endpoint_file)
        # mtime granularity can hide a rewrite made within the same tick.
        os.utime(self.endpoint_file, (server.port, server.port))
        self.agent = _Served(server)
        self.endpoint = endpoint

    def _stop_agent(self):
        if self.agent is not None:
            self.agent.stop()
            self.agent = None

    def test_a_call_is_forwarded_and_its_reply_returned(self):
        resp = requests.post(self.url + "/invoke", json={"function": "f", "args": [1]}, timeout=10)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"echo": {"function": "f", "args": [1]}})

    def test_the_agent_sees_the_link_token_and_version(self):
        requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        seen = self.agent_app.seen_headers[-1]
        self.assertEqual(seen["Authorization"], auth.bearer_header(self.endpoint.token))
        self.assertEqual(seen[protocol.PROTOCOL_HEADER], str(protocol.PROTOCOL_VERSION))

    def test_a_callers_own_authorization_header_is_not_passed_on(self):
        requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10,
                      headers={"authorization": "Bearer callers-own-secret"})
        seen = self.agent_app.seen_headers[-1]
        self.assertEqual(seen["Authorization"], auth.bearer_header(self.endpoint.token))
        self.assertNotIn("callers-own-secret", json.dumps(seen))

    def test_a_driver_error_passes_through_unchanged(self):
        resp = requests.post(self.url + "/invoke", json={"function": "fail"}, timeout=10)
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json(), {"error": "Instrument said no", "details": "trace"})

    def test_an_unknown_route_keeps_the_agents_status(self):
        self.assertEqual(requests.get(self.url + "/no-such-route", timeout=10).status_code, 404)

    def test_agent_down_is_one_clear_retryable_error(self):
        self._stop_agent()
        resp = requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["code"], protocol.ERR_AGENT_UNREACHABLE)
        self.assertTrue(body["retryable"])
        self.assertIn("host agent is not responding", body["error"])

    def test_the_relay_recovers_when_the_agent_comes_back_elsewhere(self):
        requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        self._stop_agent()
        self.assertEqual(
            requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10).status_code, 503)
        self._start_agent()  # new port and new token, as after a re-provision
        resp = requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        self.assertEqual(resp.status_code, 200)

    def test_a_rotated_token_is_picked_up_without_a_failed_call(self):
        requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        rotated = config.Endpoint(token=config.new_token(), port=self.endpoint.port,
                                  connect_host="127.0.0.1")
        worker.install(self.agent_app, rotated)
        config.save_endpoint(rotated, self.endpoint_file)
        os.utime(self.endpoint_file, (1, 1))
        resp = requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        self.assertEqual(resp.status_code, 200)

    def test_a_stale_token_is_reported_as_a_repairable_install(self):
        stale = config.Endpoint(token=config.new_token(), port=self.endpoint.port,
                                connect_host="127.0.0.1")
        config.save_endpoint(stale, self.endpoint_file)
        os.utime(self.endpoint_file, (2, 2))
        resp = requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["code"], protocol.ERR_UNAUTHORIZED)
        self.assertEqual(self.agent_app.seen_headers, [])

    def test_no_endpoint_record_says_the_agent_is_not_set_up(self):
        os.unlink(self.endpoint_file)
        resp = requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["code"], protocol.ERR_NOT_CONFIGURED)

    def test_a_version_mismatch_tells_the_user_to_update(self):
        original = protocol.negotiate

        def agent_from_the_future(peer_min, peer_max, **kw):
            return original(peer_min, peer_max, own_min=98, own_max=99)

        worker.protocol.negotiate = agent_from_the_future
        self.addCleanup(setattr, worker.protocol, "negotiate", original)
        self.link.mark_unreachable()
        resp = requests.post(self.url + "/invoke", json={"function": "f"}, timeout=10)
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertEqual(body["code"], protocol.ERR_PROTOCOL_MISMATCH)
        self.assertIn("lager update", body["error"])

    def test_status_reports_the_link_and_never_the_token(self):
        resp = requests.get(self.url + protocol.RELAY_PREFIX + "status", timeout=10)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["connected"])
        self.assertEqual(body["protocol"], protocol.PROTOCOL_VERSION)
        self.assertNotIn(self.endpoint.token, resp.text)

    def test_status_reports_a_down_agent_instead_of_failing(self):
        self._stop_agent()
        self.link.mark_unreachable()
        body = requests.get(self.url + protocol.RELAY_PREFIX + "status", timeout=10).json()
        self.assertFalse(body["connected"])
        self.assertEqual(body["last_error"]["code"], protocol.ERR_AGENT_UNREACHABLE)

    def test_relay_only_paths_are_never_forwarded(self):
        resp = requests.get(self.url + protocol.RELAY_PREFIX + "anything-else", timeout=10)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.agent_app.seen_headers, [])

    def test_one_connection_carries_several_calls(self):
        with requests.Session() as session:
            for n in range(5):
                resp = session.post(self.url + "/invoke", json={"function": "f", "n": n}, timeout=10)
                self.assertEqual(resp.json()["echo"]["n"], n)


class WorkerHidapiTests(unittest.TestCase):
    """hidapi must first be called from the worker's main thread (macOS keeps
    its HID manager on the run loop of the thread that first calls it)."""

    def test_the_first_call_is_made_on_the_calling_thread(self):
        threads = []
        fake = unittest.mock.Mock()
        fake.enumerate.side_effect = lambda *a: threads.append(threading.current_thread())
        with unittest.mock.patch.dict("sys.modules", {"hid": fake}):
            worker._start_hidapi_on_main_thread()
        fake.enumerate.assert_called_once_with(0, 0)
        self.assertEqual(threads, [threading.current_thread()])

    def test_without_hidapi_or_with_a_failing_one_the_worker_still_starts(self):
        with unittest.mock.patch.dict("sys.modules", {"hid": None}):
            worker._start_hidapi_on_main_thread()  # ImportError: nothing to do
        broken = unittest.mock.Mock()
        broken.enumerate.side_effect = OSError("no HID access")
        with unittest.mock.patch.dict("sys.modules", {"hid": broken}), \
                self.assertLogs(worker.logger, "ERROR"):
            worker._start_hidapi_on_main_thread()


if __name__ == "__main__":
    unittest.main()

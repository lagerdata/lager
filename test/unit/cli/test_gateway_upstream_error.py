# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
The CLI side of a gateway in front of lager that cannot reach it, and of a
gateway left without its configuration.

* A gateway whose box service does not accept its connection answers 502
  with `X-Gateway-Error: upstream_unavailable` (gateway-auth contract §7).
  Before, it dropped the connection, and the CLI reported "connection
  failed" with nothing to say the gateway was fine and lager was not.
* `lager install` / `lager update` warn when a container still runs on
  lagernet but /etc/lager/control_plane.json is gone: that gateway refuses
  every connection until the box is re-linked, and lager cannot fix it.
* `lager install` names a port conflict (start_box.sh's exit 5) instead of a
  bare "Deployment failed!".
"""
import importlib
import subprocess
import unittest
from unittest import mock

import requests
from click.testing import CliRunner

from cli.commands.box import _gateway, _ssh
from cli.errors import LagerError
from cli import gateway_auth

install_mod = importlib.import_module("cli.commands.utility.install")
bs = importlib.import_module("cli.box_storage")


def _response(status, headers=None):
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp.request = requests.Request("GET", "http://10.0.0.1:5000/python").prepare()
    return resp


class UpstreamUnavailable(unittest.TestCase):
    HEADERS = {"X-Gateway-Error": "upstream_unavailable",
               "X-Gateway-Upstream": "lager:5000"}

    def test_names_the_service_behind_the_gateway(self):
        with self.assertRaises(LagerError) as ctx:
            gateway_auth.handle_gateway_upstream_error(_response(502, self.HEADERS), "10.0.0.1")
        err = ctx.exception
        self.assertIn("behind box 10.0.0.1's gateway does not answer", err.problem)
        self.assertIn("lager:5000", err.cause)
        self.assertTrue(any("lager update --box 10.0.0.1" in f for f in err.fixes))

    def test_callers_that_tolerate_an_unreachable_box_still_do(self):
        # `lager install` onto a box whose lager was uninstalled checks and
        # takes the box lock on :9000 first. The gateway answers 502 there,
        # and the lock code skips an unreachable box by catching
        # RequestException, as it did when the connection simply dropped.
        # Raising a plain LagerError aborted the install before it deployed.
        with self.assertRaises(requests.exceptions.RequestException):
            gateway_auth.handle_gateway_upstream_error(_response(502, self.HEADERS), "10.0.0.1")

    def test_the_lock_acquire_treats_it_as_unreachable(self):
        resp = _response(502, self.HEADERS)
        # box_storage imports requests inside each function.
        with mock.patch("requests.get", return_value=resp), \
                mock.patch("requests.post", return_value=resp), \
                mock.patch.object(bs, "_resolve_gateway", return_value=(resp, False)):
            state, _data = bs.acquire_box_lock("10.0.0.1", "PRD-X", "test-holder", quiet=True)
        self.assertEqual(state, "unreachable")

    def test_the_response_hook_raises_it(self):
        hook = gateway_auth.gateway_response_hook("10.0.0.1")
        with self.assertRaises(LagerError):
            hook(_response(502, self.HEADERS))

    def test_a_plain_502_is_left_alone(self):
        # A 502 from the box's own service, or a proxy that is not a lager
        # gateway, carries no X-Gateway-Error: the caller's handling stands.
        hook = gateway_auth.gateway_response_hook("10.0.0.1")
        resp = _response(502)
        self.assertIs(hook(resp), resp)

    def test_other_gateway_errors_are_left_alone(self):
        resp = _response(502, {"X-Gateway-Error": "something_else"})
        gateway_auth.handle_gateway_upstream_error(resp, "10.0.0.1")

    def test_a_response_without_headers_is_left_alone(self):
        # Some call sites hand over a minimal stand-in, not a requests.Response.
        class Bare:
            status_code = 502
        gateway_auth.handle_gateway_upstream_error(Bare(), "10.0.0.1")

    def test_success_is_untouched(self):
        resp = _response(200, self.HEADERS)
        self.assertIs(gateway_auth.gateway_response_hook("10.0.0.1")(resp), resp)

    def test_direct_box_requests_report_it_too(self):
        # box_storage._check_gateway serves the commands that call requests
        # without the session hook.
        resp = _response(502, self.HEADERS)
        with mock.patch.object(bs, "_resolve_gateway", return_value=(resp, False)):
            with self.assertRaises(LagerError):
                bs._check_gateway(resp, "10.0.0.1")


class GatewayWithoutConfig(unittest.TestCase):
    def test_a_container_left_on_lagernet_is_named(self):
        out = "lagernet:\nlager\npigpio\ngateway\n"
        self.assertEqual(_gateway.parse_gateway_query(out), ["gateway"])

    def test_a_present_config_means_no_warning(self):
        self.assertEqual(_gateway.parse_gateway_query("config\n"), [])

    def test_only_lagers_own_containers_means_no_warning(self):
        self.assertEqual(_gateway.parse_gateway_query("lagernet:\nlager\npigpio\n"), [])

    def test_an_unrelated_reply_is_not_a_container_list(self):
        for out in ("", "ok\n", "0.52.0\n", None):
            self.assertEqual(_gateway.parse_gateway_query(out), [], out)

    def test_the_query_runs_without_a_prompt(self):
        argv = _gateway.gateway_query_argv("lagerdata@10.0.0.1", ["-i", "k"])
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-1], _gateway.GATEWAY_QUERY)
        self.assertIn(_ssh.CONTROL_PLANE_CONFIG, _gateway.GATEWAY_QUERY)

    def test_the_query_parses_on_a_real_shell(self):
        # No docker and no config here: the header still comes first.
        out = subprocess.run(["bash", "-c", _gateway.GATEWAY_QUERY],
                             capture_output=True, text=True).stdout
        self.assertTrue(out.startswith("lagernet:") or out.startswith("config"), out)

    def test_the_warning_says_what_to_do(self):
        lines = []
        _gateway.warn_gateway_without_config(["gateway"], lambda m, **_k: lines.append(m))
        text = " ".join(lines)
        self.assertIn("'gateway' runs on lagernet", text)
        self.assertIn("re-link", text)
        _gateway.warn_gateway_without_config([], lambda m, **_k: lines.append("x"))
        self.assertNotIn("x", lines)

    def test_names_the_role_not_a_product(self):
        # Lager is the open standard; the same rule test_box_ssh_identity
        # pins for the key-setup modules.
        import inspect
        for module in (_gateway, gateway_auth):
            self.assertNotIn("stout", inspect.getsource(module).lower())


def _proc(rc, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


class InstallReportsTheConflict(unittest.TestCase):
    def _install(self, *, deploy_rc, gateway_reply="config\n"):
        def fake_run(cmd, **_kw):
            cmd = list(cmd) if isinstance(cmd, (list, tuple)) else [cmd]
            if cmd and cmd[0] != "ssh":
                return _proc(deploy_rc)  # the deploy script
            if cmd[-1] == _gateway.GATEWAY_QUERY:
                return _proc(0, gateway_reply)
            return _proc(0, "0.36.2\n")

        patches = [
            mock.patch.object(bs, "acquire_box_lock", return_value=("acquired", {})),
            mock.patch.object(bs, "release_box_lock", return_value=True),
            mock.patch.object(bs, "get_lock_holder", return_value="test-holder"),
            mock.patch.object(bs, "HeartbeatThread", return_value=mock.Mock()),
            mock.patch.object(install_mod.subprocess, "run", fake_run),
            mock.patch.object(_ssh, "lager_box_key_if_present", lambda *a, **k: None),
            mock.patch.object(install_mod, "lager_box_key_if_present", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return CliRunner().invoke(install_mod.install, ["--ip", "10.0.0.1", "--yes"])

    def test_a_port_conflict_is_named(self):
        result = self._install(deploy_rc=_gateway.START_BOX_PORT_CONFLICT)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("held by another container", result.output)
        self.assertIn("Nothing was stopped", result.output)
        self.assertNotIn("Deployment failed!", result.output)

    def test_any_other_failure_is_unchanged(self):
        result = self._install(deploy_rc=1)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("Deployment failed!", result.output)

    def test_a_gateway_without_config_is_warned_about_before_deploying(self):
        result = self._install(deploy_rc=1, gateway_reply="lagernet:\nlager\ngateway\n")
        self.assertIn("'gateway' runs on lagernet", result.output)
        self.assertLess(result.output.index("runs on lagernet"),
                        result.output.index("Deployment failed!"))


if __name__ == "__main__":
    unittest.main()

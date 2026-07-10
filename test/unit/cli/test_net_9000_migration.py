#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Tier-1 net CLI migration to the box HTTP API (:9000).

Tier-1 instrument/IO net commands (adc, dac, gpi, gpo, thermocouple, eload,
spi, i2c, watt, energy) now drive the box exclusively over
``POST :9000/net/command`` via ``cli.core.net_helpers.post_net_command`` — the
legacy :5000 ``lager python`` script-upload path was removed. These tests:

  * exercise the shared ``post_net_command`` / ``fetch_nets`` HTTP helpers with
    ``requests`` mocked (success, hardware error, unreachable, 501);
  * assert each simple Tier-1 command dispatches the right action/role; and
  * guard against regressions by asserting the Tier-1 command modules no longer
    import the :5000 exec helpers.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest
import requests
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

net_helpers = importlib.import_module("cli.core.net_helpers")


class _Obj:
    """Settable stand-in for the LagerContext."""


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


# --------------------------------------------------------------------------- #
# post_net_command                                                            #
# --------------------------------------------------------------------------- #

class TestPostNetCommand:

    def test_success_posts_to_9000_net_command(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return _Resp(200, {"success": True, "message": "HIGH (1)", "value": 1})

        with patch("requests.post", fake_post):
            result = net_helpers.post_net_command(
                None, "1.2.3.4", "gpi1", "input", role="gpio", quiet=True)

        assert captured["url"] == "http://1.2.3.4:9000/net/command"
        assert captured["json"] == {
            "netname": "gpi1", "action": "input", "params": {}, "role": "gpio"}
        assert result["value"] == 1

    def test_params_forwarded_under_params_key(self):
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["json"] = json
            return _Resp(200, {"success": True, "message": "ok"})

        with patch("requests.post", fake_post):
            net_helpers.post_net_command(
                None, "1.2.3.4", "dac1", "set", role="dac", quiet=True, value=1.5)

        assert captured["json"]["params"] == {"value": 1.5}

    def test_hardware_error_exits_nonzero(self):
        def fake_post(url, json=None, timeout=None):
            return _Resp(502, {"success": False, "error": "Hardware error: busy"})

        with patch("requests.post", fake_post):
            with pytest.raises(SystemExit):
                net_helpers.post_net_command(None, "1.2.3.4", "adc1", "read", role="adc")

    def test_unreachable_box_exits_nonzero(self):
        def fake_post(url, json=None, timeout=None):
            raise requests.ConnectionError()

        with patch("requests.post", fake_post):
            with pytest.raises(SystemExit):
                net_helpers.post_net_command(None, "1.2.3.4", "adc1", "read", role="adc")

    def test_501_unsupported_role_exits_nonzero(self):
        def fake_post(url, json=None, timeout=None):
            return _Resp(501, {"success": False, "error": "Role 'x' not supported"})

        with patch("requests.post", fake_post):
            with pytest.raises(SystemExit):
                net_helpers.post_net_command(None, "1.2.3.4", "scope1", "read")


# --------------------------------------------------------------------------- #
# fetch_nets                                                                  #
# --------------------------------------------------------------------------- #

class TestFetchNets:

    def _run(self, get_impl):
        with patch("requests.get", get_impl):
            return net_helpers.fetch_nets("1.2.3.4")

    def test_bare_array_from_nets_list(self):
        nets = [{"name": "a", "role": "adc"}]

        def get(url, timeout=None):
            assert url.endswith(":9000/nets/list")
            return _Resp(200, nets)

        assert self._run(get) == nets

    def test_dict_shape_from_nets_list(self):
        def get(url, timeout=None):
            return _Resp(200, {"nets": [{"name": "b", "role": "dac"}]})

        assert self._run(get) == [{"name": "b", "role": "dac"}]

    def test_falls_back_to_uart_nets_list(self):
        def get(url, timeout=None):
            if url.endswith("/uart/nets/list"):
                return _Resp(200, {"nets": [{"name": "c", "role": "gpio"}]})
            return _Resp(404, {})

        assert self._run(get) == [{"name": "c", "role": "gpio"}]

    def test_unreachable_returns_empty(self):
        def get(url, timeout=None):
            raise requests.RequestException()

        assert self._run(get) == []


# --------------------------------------------------------------------------- #
# Simple Tier-1 commands dispatch via post_net_command                        #
# --------------------------------------------------------------------------- #

def _invoke_simple(module_name, command_attr, argv, patch_extra=None):
    """Invoke a standalone Tier-1 command with the box boundary mocked.

    Returns the list of post_net_command calls (each a dict of kwargs).
    """
    mod = importlib.import_module(module_name)
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False, **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "params": params})
        return {"success": True, "value": 1, "message": "ok"}

    patchers = {
        "post_net_command": fake_post,
        "resolve_box": lambda ctx, box: "1.2.3.4",
        "validate_net_exists": lambda ctx, ip, name, role: {"name": name},
        "get_default_net": lambda ctx, t: None,
    }
    if patch_extra:
        patchers.update(patch_extra)

    applied = [patch.object(mod, k, v) for k, v in patchers.items()
               if hasattr(mod, k)]
    for p in applied:
        p.start()
    try:
        result = CliRunner().invoke(
            getattr(mod, command_attr), argv, obj=_Obj(),
            catch_exceptions=False)
    finally:
        for p in applied:
            p.stop()
    assert result.exit_code == 0, result.output
    return calls


class TestSimpleCommandDispatch:

    def test_adc_read(self):
        calls = _invoke_simple("cli.commands.measurement.adc", "adc",
                               ["NET1", "--box", "b"])
        assert calls == [{"netname": "NET1", "action": "read", "role": "adc",
                          "params": {}}]

    def test_dac_read(self):
        calls = _invoke_simple("cli.commands.measurement.dac", "dac",
                               ["NET1", "--box", "b"])
        assert calls[0]["action"] == "read"
        assert calls[0]["role"] == "dac"

    def test_dac_set(self):
        calls = _invoke_simple("cli.commands.measurement.dac", "dac",
                               ["NET1", "3.3", "--box", "b"])
        assert calls[0]["action"] == "set"
        assert calls[0]["params"]["value"] == 3.3

    def test_gpi_input(self):
        calls = _invoke_simple("cli.commands.measurement.gpi", "gpi",
                               ["NET1", "--box", "b"])
        assert calls[0]["action"] == "input"
        assert calls[0]["role"] == "gpio"

    def test_gpi_wait_for(self):
        calls = _invoke_simple("cli.commands.measurement.gpi", "gpi",
                               ["NET1", "--wait-for", "high", "--box", "b"])
        assert calls[0]["action"] == "wait_for_level"
        assert calls[0]["params"]["level"] == "high"

    def test_gpo_output(self):
        calls = _invoke_simple("cli.commands.measurement.gpo", "gpo",
                               ["NET1", "high", "--box", "b"])
        assert calls[0]["action"] == "output"
        assert calls[0]["params"]["level"] == "high"

    def test_thermocouple_read(self):
        calls = _invoke_simple("cli.commands.measurement.thermocouple",
                               "thermocouple", ["NET1", "--box", "b"])
        assert calls[0]["action"] == "read"
        assert calls[0]["role"] == "thermocouple"


# --------------------------------------------------------------------------- #
# Regression guard: no Tier-1 command still imports the :5000 exec helpers     #
# --------------------------------------------------------------------------- #

class TestNo5000Fallback:

    TIER1_MODULES = [
        "cli.commands.measurement.adc",
        "cli.commands.measurement.dac",
        "cli.commands.measurement.gpi",
        "cli.commands.measurement.gpo",
        "cli.commands.measurement.thermocouple",
        "cli.commands.measurement.watt",
        "cli.commands.measurement.energy",
        "cli.commands.power.eload",
        "cli.commands.power.supply",
        "cli.commands.power.battery",
        "cli.commands.communication.spi",
        "cli.commands.communication.i2c",
        "cli.commands.communication.usb",
        "cli.commands.communication.uart",
    ]

    @pytest.mark.parametrize("module_name", TIER1_MODULES)
    def test_no_python_exec_helpers_imported(self, module_name):
        mod = importlib.import_module(module_name)
        for banned in ("run_python_internal", "run_impl_script", "run_backend"):
            assert not hasattr(mod, banned), (
                f"{module_name} still references {banned}; Tier-1 commands must "
                f"use post_net_command / the :9000 endpoints only.")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

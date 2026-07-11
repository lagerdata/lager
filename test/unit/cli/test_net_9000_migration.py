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
import json
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
            captured["timeout"] = timeout
            return _Resp(200, {"success": True, "message": "HIGH (1)", "value": 1})

        with patch("requests.post", fake_post):
            result = net_helpers.post_net_command(
                None, "1.2.3.4", "gpi1", "input", role="gpio", quiet=True)

        assert captured["url"] == "http://1.2.3.4:9000/net/command"
        assert captured["json"] == {
            "netname": "gpi1", "action": "input", "params": {}, "role": "gpio"}
        assert captured["timeout"] == net_helpers._NET_HTTP_TIMEOUT
        assert result["value"] == 1

    def test_http_timeout_forwarded_to_requests(self):
        # Regression: long-running actions (energy/watt windows, gpi wait) widen
        # the client timeout; post_net_command must actually pass it through.
        captured = {}

        def fake_post(url, json=None, timeout=None):
            captured["timeout"] = timeout
            return _Resp(200, {"success": True, "message": "ok"})

        with patch("requests.post", fake_post):
            net_helpers.post_net_command(
                None, "1.2.3.4", "energy1", "read_energy",
                role="energy-analyzer", quiet=True, http_timeout=130.0,
                duration=100.0)
        assert captured["timeout"] == 130.0

    def test_http_timeout_none_disables_client_timeout(self):
        # gpi --wait-for with no --timeout waits indefinitely (old behavior).
        captured = {"timeout": "unset"}

        def fake_post(url, json=None, timeout=None):
            captured["timeout"] = timeout
            return _Resp(200, {"success": True, "message": "ok"})

        with patch("requests.post", fake_post):
            net_helpers.post_net_command(
                None, "1.2.3.4", "gpi1", "wait_for_level", role="gpio",
                quiet=True, http_timeout=None, level="high")
        assert captured["timeout"] is None

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

def _invoke_simple(module_name, command_attr, argv, patch_extra=None,
                   mock_value=1):
    """Invoke a standalone Tier-1 command with the box boundary mocked.

    Returns the list of post_net_command calls (each a dict of kwargs).
    """
    mod = importlib.import_module(module_name)
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout="default", **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "quiet": quiet, "http_timeout": http_timeout,
                      "params": params})
        return {"success": True, "value": mock_value, "message": "ok"}

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
                          "quiet": True, "http_timeout": "default",
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
        # No --timeout -> wait indefinitely; the client timeout must be off.
        assert calls[0]["http_timeout"] is None

    def test_gpi_wait_for_with_timeout_widens_client_budget(self):
        # Regression: the HTTP budget must exceed the device-side wait, else a
        # healthy wait longer than ~10s dies with ReadTimeout on the client.
        calls = _invoke_simple(
            "cli.commands.measurement.gpi", "gpi",
            ["NET1", "--wait-for", "high", "--timeout", "60", "--box", "b"])
        assert calls[0]["params"]["timeout"] == 60.0
        assert calls[0]["http_timeout"] == 80.0

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
        assert calls[0]["quiet"] is True


# --------------------------------------------------------------------------- #
# Role-specific CLI output (quiet=True + local formatting)                    #
# --------------------------------------------------------------------------- #

def _invoke_output(module_name, command_attr, argv, *, value, patch_extra=None):
    """Invoke a Tier-1 command and return (CliResult, calls) with a canned value."""
    mod = importlib.import_module(module_name)
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout="default", **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "quiet": quiet, "http_timeout": http_timeout,
                      "params": params})
        return {"success": True, "value": value, "message": "ok"}

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
    return result, calls


_ELOAD_STATE = {
    "mode": "CC",
    "input_enabled": True,
    "measured_voltage": 3.300,
    "measured_current": 0.500,
    "measured_power": 1.650,
    "current_setting": 0.500,
}


def _invoke_eload(argv, *, value):
    mod = importlib.import_module("cli.commands.power.eload")
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout="default", **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "quiet": quiet, "params": params})
        return {"success": True, "value": value, "message": "ok"}

    with patch.object(mod, "post_net_command", fake_post), \
            patch.object(mod, "resolve_box", lambda ctx, box: "1.2.3.4"), \
            patch.object(mod, "require_netname", lambda ctx, role: "NET1"), \
            patch.object(mod, "display_nets", lambda *a, **k: None), \
            patch.object(mod, "get_default_net", lambda ctx, t: None):
        result = CliRunner().invoke(mod.eload, argv, obj=_Obj(),
                                    catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result, calls


class TestRoleSpecificOutput:

    def test_adc_prints_role_label(self):
        result, calls = _invoke_output(
            "cli.commands.measurement.adc", "adc",
            ["NET1", "--box", "b"], value=1.234567)
        assert calls[0]["quiet"] is True
        assert "ADC 'NET1': 1.234567 V" in result.output

    def test_adc_json(self):
        result, _ = _invoke_output(
            "cli.commands.measurement.adc", "adc",
            ["NET1", "--json", "--box", "b"], value=1.234567)
        assert json.loads(result.output) == {
            "netname": "NET1", "voltage": 1.234567}

    def test_dac_read_prints_role_label(self):
        result, _ = _invoke_output(
            "cli.commands.measurement.dac", "dac",
            ["NET1", "--box", "b"], value=3.3)
        assert "DAC 'NET1': 3.300000 V" in result.output

    def test_dac_set_prints_role_label(self):
        result, _ = _invoke_output(
            "cli.commands.measurement.dac", "dac",
            ["NET1", "3.3", "--box", "b"], value=3.3)
        assert "DAC 'NET1' set to 3.300000 V" in result.output

    def test_gpi_input_prints_level(self):
        result, _ = _invoke_output(
            "cli.commands.measurement.gpi", "gpi",
            ["NET1", "--box", "b"], value=1)
        assert "GPIO 'NET1': HIGH (1)" in result.output

    def test_gpi_wait_for_prints_elapsed(self):
        result, calls = _invoke_output(
            "cli.commands.measurement.gpi", "gpi",
            ["NET1", "--wait-for", "high", "--box", "b"], value=0.25)
        assert calls[0]["quiet"] is True
        assert "GPIO 'NET1' reached level high in 0.2500s" in result.output

    def test_gpo_set_prints_level(self):
        result, _ = _invoke_output(
            "cli.commands.measurement.gpo", "gpo",
            ["NET1", "high", "--box", "b"], value=1)
        assert "GPIO 'NET1' set to HIGH" in result.output

    def test_gpo_toggle_prints_toggled(self):
        result, _ = _invoke_output(
            "cli.commands.measurement.gpo", "gpo",
            ["NET1", "toggle", "--box", "b"], value=0)
        assert "GPIO 'NET1' toggled to LOW" in result.output

    def test_thermocouple_prints_temperature(self):
        result, _ = _invoke_output(
            "cli.commands.measurement.thermocouple", "thermocouple",
            ["NET1", "--box", "b"], value=25.3)
        assert "Temperature: 25.3˚C" in result.output

    def test_eload_cc_set_prints_mode_and_value(self):
        result, calls = _invoke_eload(
            ["NET1", "cc", "0.5", "--box", "b"], value=0.5)
        assert calls[0]["quiet"] is True
        assert "Mode: CC" in result.output
        assert "Current: 0.5 A" in result.output

    def test_eload_state_prints_multiline_block(self):
        result, _ = _invoke_eload(
            ["NET1", "state", "--box", "b"], value=_ELOAD_STATE)
        for token in ("Electronic Load State:", "Mode: CC", "Input: Enabled",
                      "Measured Voltage:", "Measured Current:",
                      "Current Setting:"):
            assert token in result.output

    def test_eload_state_json(self):
        result, _ = _invoke_eload(
            ["NET1", "state", "--json", "--box", "b"], value=_ELOAD_STATE)
        data = json.loads(result.output)
        assert data["netname"] == "NET1"
        assert data["mode"] == "CC"
        assert data["current_setting"] == 0.5


# --------------------------------------------------------------------------- #
# Energy command: budget, output detail, --json                               #
# --------------------------------------------------------------------------- #

_ENERGY_VALUE = {
    "duration_s": 10.0, "energy_j": 1.234, "energy_wh": 0.000342778,
    "charge_c": 0.5678, "charge_ah": 0.000157722,
}
_STATS_VALUE = {
    "duration_s": 1.0,
    "current": {"mean": 0.001234, "min": 0.001, "max": 0.0015, "std": 0.0001},
    "voltage": {"mean": 3.3, "min": 3.29, "max": 3.31, "std": 0.005},
    "power": {"mean": 0.004072, "min": 0.0033, "max": 0.005, "std": 0.0003},
}


def _invoke_energy(argv):
    mod = importlib.import_module("cli.commands.measurement.energy")
    calls: list[dict] = []

    def fake_post(ctx, box_ip, netname, action, role=None, quiet=False,
                  http_timeout="default", **params):
        calls.append({"netname": netname, "action": action, "role": role,
                      "http_timeout": http_timeout, "params": params})
        value = _ENERGY_VALUE if action == "read_energy" else _STATS_VALUE
        return {"success": True, "value": value, "message": "ok"}

    with patch.object(mod, "post_net_command", fake_post), \
            patch.object(mod, "resolve_box", lambda ctx, box: "1.2.3.4"), \
            patch.object(mod, "validate_net_exists",
                         lambda ctx, ip, name, role: {"name": name}), \
            patch.object(mod, "display_nets", lambda *a, **k: None), \
            patch.object(mod, "get_default_net", lambda ctx, t: None):
        result = CliRunner().invoke(mod.energy, argv, obj=_Obj(),
                                    catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return result, calls


class TestEnergyCommand:

    def test_read_widens_client_budget_past_duration(self):
        # Regression: `lager energy read` defaults to a 10s integration; the
        # old fixed 10s client timeout aborted the healthy default request.
        _, calls = _invoke_energy(["NET1", "read", "--box", "b"])
        assert calls[0]["action"] == "read_energy"
        assert calls[0]["params"]["duration"] == 10.0
        assert calls[0]["http_timeout"] == 40.0

    def test_read_long_duration_budget(self):
        # The old :5000 path allowed up to 120s integrations; the client budget
        # must scale with the requested duration.
        _, calls = _invoke_energy(
            ["NET1", "read", "--duration", "100", "--box", "b"])
        assert calls[0]["http_timeout"] == 130.0

    def test_read_prints_full_energy_breakdown(self):
        # Regression: the box's structured value (J/Wh, C/Ah) must be shown,
        # not discarded in favor of the lossy one-line message.
        result, _ = _invoke_energy(["NET1", "read", "--box", "b"])
        assert "Energy 'NET1'" in result.output
        assert "J" in result.output and "Wh" in result.output
        assert "Charge" in result.output and "Ah" in result.output

    def test_stats_prints_mean_min_max_std(self):
        result, calls = _invoke_energy(["NET1", "stats", "--box", "b"])
        assert calls[0]["action"] == "read_stats"
        for token in ("Current", "Voltage", "Power",
                      "mean=", "min=", "max=", "std="):
            assert token in result.output

    def test_read_json_output(self):
        result, _ = _invoke_energy(["NET1", "read", "--json", "--box", "b"])
        data = json.loads(result.output)
        assert data["netname"] == "NET1"
        assert data["energy_j"] == 1.234
        assert data["charge_ah"] == 0.000157722


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
        "cli.commands.communication.ble",
        "cli.commands.communication.wifi",
        "cli.commands.communication.router",
        "cli.commands.communication.blufi",
        "cli.commands.development.arm",
        "cli.commands.utility.webcam",
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

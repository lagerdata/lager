# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""`lager drivers` and `lager nets add-custom` against a fake box.

The driver code never reaches the CLI: install zips a local bundle and PUTs
it, and every other command reads the manifest the box serves.
"""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from unittest.mock import patch

import pytest
from click.testing import CliRunner

drivers_mod = importlib.import_module("cli.commands.box.drivers")
nets_mod = importlib.import_module("cli.commands.box.nets")
custom_drivers = importlib.import_module("cli.core.custom_drivers")
from test.unit.cli.drivers_http_fake import FakeDriversBox, STEPPER_MANIFEST, stepper_entry  # noqa: E402
from test.unit.cli.nets_http_fake import FakeResponse  # noqa: E402

BOX_IP = "10.0.0.5"


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(custom_drivers, "CACHE_FILE", tmp_path / "drivers_manifest.json")


@pytest.fixture
def box():
    fake = FakeDriversBox(drivers=[stepper_entry()])
    fake.saved_nets = [{"name": "UART_1", "role": "uart", "instrument": "FTDI"},
                       {"name": "SUPPLY_1", "role": "power-supply", "instrument": "Rigol"}]
    with patch("requests.request", fake.request), \
            patch.object(drivers_mod, "resolve_and_validate_box",
                         lambda ctx, name, read_only=False: BOX_IP), \
            patch.object(nets_mod, "_resolve_box", lambda ctx, name: BOX_IP):
        yield fake


def _run(group, args, input=None):
    result = CliRunner().invoke(group, args, input=input, catch_exceptions=False)
    return result, result.output


def _bundle(tmp_path, manifest=None, driver=True):
    d = tmp_path / "stepper"
    d.mkdir()
    (d / "lager_driver.json").write_text(json.dumps(manifest or STEPPER_MANIFEST))
    if driver:
        (d / "driver.py").write_text("from lager.drivers import CustomDriver\n")
    (d / "helper.py").write_text("X = 1\n")
    return d


class TestDriversList:
    def test_lists_installed_drivers_with_their_nets(self, box):
        box.saved_nets.append({"name": "RIG_1", "role": "stepper", "instrument": "custom:stepper"})
        result, out = _run(drivers_mod.drivers, ["list", "--box", "B"])
        assert result.exit_code == 0, out
        assert "stepper" in out and "1.2.0" in out and "RIG_1" in out
        assert "abababababab" in out

    def test_lists_load_errors(self, box):
        box.drivers["broken"] = {"type": "broken", "status": "error", "error": "bad JSON",
                                 "version": None, "sha256": None, "manifest": None}
        _, out = _run(drivers_mod.drivers, ["list", "--box", "B"])
        assert "error: bad JSON" in out

    def test_empty_box(self, box):
        box.drivers = {}
        _, out = _run(drivers_mod.drivers, ["list", "--box", "B"])
        assert "No custom drivers" in out

    def test_json(self, box):
        _, out = _run(drivers_mod.drivers, ["list", "--box", "B", "--json"])
        assert json.loads(out)[0]["type"] == "stepper"

    @pytest.mark.parametrize("body", [
        None,
        # box_http_server's generic 404 handler
        {"error": "Not found", "message": "The requested endpoint does not exist"},
    ])
    def test_old_box_says_update(self, box, body):
        with patch("requests.request", lambda *a, **k: FakeResponse(404, body)):
            result = CliRunner().invoke(drivers_mod.drivers, ["list", "--box", "B"])
        assert result.exit_code == 1
        assert "does not support custom drivers" in result.output


class TestDriversShow:
    def test_shows_params_actions_and_args(self, box):
        _, out = _run(drivers_mod.drivers, ["show", "stepper", "--box", "B"])
        assert "stepper 1.2.0" in out
        assert "uart_net" in out and "uart net, required" in out
        assert "jog" in out and "--seconds <float>" in out and "--hold-time" in out
        assert "a|b" in out

    def test_missing_driver(self, box):
        result = CliRunner().invoke(drivers_mod.drivers, ["show", "nope", "--box", "B"])
        assert result.exit_code == 1
        assert "No custom driver 'nope'" in result.output


class TestDriversInstall:
    def test_install_zips_bundle_and_puts_it(self, box, tmp_path):
        box.drivers = {}
        result, out = _run(drivers_mod.drivers, ["install", str(_bundle(tmp_path)), "--box", "B"])
        assert result.exit_code == 0, out
        assert "Installed driver 'stepper'" in out
        dtype, files = box.uploads[0]
        assert dtype == "stepper"
        name, data, ctype = files["bundle"]
        names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        assert names == {"lager_driver.json", "driver.py", "helper.py"}
        assert "nets add-custom" in out

    def test_replace_asks_first(self, box, tmp_path):
        bundle = str(_bundle(tmp_path))
        _, out = _run(drivers_mod.drivers, ["install", bundle, "--box", "B"], input="n\n")
        assert "Cancelled" in out and box.uploads == []
        _, out = _run(drivers_mod.drivers, ["install", bundle, "--box", "B", "--yes"])
        assert "Replaced driver 'stepper'" in out

    def test_local_checks(self, box, tmp_path):
        result = CliRunner().invoke(drivers_mod.drivers,
                                    ["install", str(_bundle(tmp_path, driver=False)), "--box", "B"])
        assert result.exit_code == 2 and "no driver.py" in result.output
        empty = tmp_path / "empty"
        empty.mkdir()
        result = CliRunner().invoke(drivers_mod.drivers, ["install", str(empty), "--box", "B"])
        assert result.exit_code == 2 and "no lager_driver.json" in result.output

    def test_install_clears_the_manifest_cache(self, box, tmp_path):
        custom_drivers.remember_drivers(BOX_IP, [])
        _run(drivers_mod.drivers, ["install", str(_bundle(tmp_path)), "--box", "B", "--yes"])
        assert custom_drivers.cached_drivers(BOX_IP) is None


class TestDriversRemove:
    def test_refused_while_nets_use_it_then_forced(self, box):
        box.saved_nets.append({"name": "RIG_1", "role": "stepper", "instrument": "custom:stepper"})
        result = CliRunner().invoke(drivers_mod.drivers, ["remove", "stepper", "--box", "B", "--yes"])
        assert result.exit_code == 1 and "RIG_1" in result.output
        result, out = _run(drivers_mod.drivers,
                           ["remove", "stepper", "--box", "B", "--yes", "--force"])
        assert result.exit_code == 0
        assert "now have no driver: RIG_1" in out
        assert box.calls[-1][3] == {"force": "1"}


class TestNetsAddCustom:
    def test_saves_record_with_typed_params(self, box):
        result, out = _run(nets_mod.nets, ["add-custom", "RIG_1", "stepper",
                                           "--param", "uart_net=UART_1",
                                           "--param", "baudrate=9600"])
        assert result.exit_code == 0, out
        rec = next(n for n in box.saved_nets if n["name"] == "RIG_1")
        assert rec == {"name": "RIG_1", "role": "stepper", "instrument": "custom:stepper",
                       "params": {"uart_net": "UART_1", "baudrate": 9600}}

    @pytest.mark.parametrize("params,expect", [
        ([], "needs --param for: uart_net"),
        (["uart_net=UART_1", "speed=3"], "has no setting speed"),
        (["uart_net"], "NAME=VALUE"),
        (["uart_net=UART_1", "baudrate=fast"], "must be int"),
    ])
    def test_usage_errors(self, box, params, expect):
        args = ["add-custom", "RIG_1", "stepper"]
        for p in params:
            args += ["--param", p]
        result = CliRunner().invoke(nets_mod.nets, args)
        assert result.exit_code == 2
        assert expect in result.output
        assert not any(n["name"] == "RIG_1" for n in box.saved_nets)

    @pytest.mark.parametrize("target,expect", [
        ("NOPE", "which does not exist"),
        ("SUPPLY_1", "needs a uart net, but 'SUPPLY_1' is a power-supply net"),
    ])
    def test_base_net_must_exist_with_right_role(self, box, target, expect):
        result = CliRunner().invoke(nets_mod.nets, ["add-custom", "RIG_1", "stepper",
                                                    "--param", f"uart_net={target}"])
        assert result.exit_code == 1
        assert expect in result.output

    def test_refuses_existing_name(self, box):
        result = CliRunner().invoke(nets_mod.nets, ["add-custom", "UART_1", "stepper",
                                                    "--param", "uart_net=UART_1"])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_unknown_driver(self, box):
        result = CliRunner().invoke(nets_mod.nets, ["add-custom", "RIG_1", "nope",
                                                    "--param", "uart_net=UART_1"])
        assert result.exit_code == 1
        assert "No custom driver 'nope'" in result.output

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the uniform net command handler (``/net/command``).

The handler is a generic bridge over ``Net.get(name, type).<action>(**params)``.
These tests inject a fake ``lager.nets.net`` module (so the heavy real one with
its driver imports is never loaded) and stub the nets cache, then assert the
handler's role inference, action allow-listing, param passing, return
normalization, and error envelope — without touching hardware.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

import pytest

# Ensure box/ is on the path so `import lager...` resolves to the box package.
BOX_DIR = Path(__file__).resolve().parents[3] / "box"
sys.path.insert(0, str(BOX_DIR))


def _make_module(name):
    m = types.ModuleType(name)
    m.__getattr__ = lambda attr: mock.MagicMock()  # type: ignore[method-assign]
    return m


def _stub(dotted):
    """Register a fake module for `dotted` and all its parent packages."""
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        key = ".".join(parts[:i])
        if key not in sys.modules:
            sys.modules[key] = _make_module(key)


# lager/__init__.py eagerly imports the full driver chain (net.py -> arm/rotrics
# -> serial, labjack, phidget, …), none of which exist off-box. Stub them before
# importing the box package so collection succeeds on a developer machine.
# Mirrors test_python_service_nets_list.py.
for _hw in (
    "pyvisa", "pyvisa.constants", "pyvisa_py",
    "usb", "usb.util", "usb.core",
    "pigpio",
    "labjack", "labjack.ljm",
    "nidaqmx",
    "phidget22", "phidget22.Phidget", "phidget22.Net",
    "phidget22.devices", "phidget22.devices.VoltageInput",
    "serial", "serial.tools", "serial.tools.list_ports",
    "serial.tools.list_ports_common",
    "usbinfo",
    "ftdi", "pyftdi", "pyftdi.spi", "pyftdi.i2c", "pyftdi.gpio",
    "aardvark_py",
    "joulescope",
    "bleak", "bleak.backends", "bleak.backends.scanner",
    "usbtmc", "smbus2", "pylibftdi", "telnetlib3", "pyudev",
    "flask_socketio",
):
    _stub(_hw)

# visa_enum does `import simplejson as json`; point it at stdlib json.
sys.modules.setdefault("simplejson", sys.modules["json"])

from flask import Flask

from lager.http_handlers.net_command import register_net_command_routes


@pytest.fixture
def client():
    app = Flask(__name__)
    register_net_command_routes(app)
    return app.test_client()


@pytest.fixture
def fake_net(monkeypatch):
    """
    Replace ``lager.nets.net`` with a stub exposing ``Net`` + the two net errors,
    so the handler's lazy import resolves to it instead of the heavy real module.
    Returns a holder dict; set ``holder['obj']`` to the object ``Net.get`` returns.
    """
    mod = types.ModuleType("lager.nets.net")

    class InvalidNetError(Exception):
        pass

    class SetupFunctionRequiredError(Exception):
        pass

    holder = {"obj": mock.MagicMock(), "raise_on_get": None}

    class Net:
        @classmethod
        def get(cls, name, nettype):
            if holder["raise_on_get"] is not None:
                raise holder["raise_on_get"]
            holder["last_get"] = (name, nettype)
            return holder["obj"]

    mod.Net = Net
    mod.InvalidNetError = InvalidNetError
    mod.SetupFunctionRequiredError = SetupFunctionRequiredError
    monkeypatch.setitem(sys.modules, "lager.nets.net", mod)
    return holder


@pytest.fixture
def set_role(monkeypatch):
    """Return a setter that makes the nets cache resolve a netname to a role."""
    import lager.http_handlers.net_command as nc

    def _set(role, *, name="net1"):
        cache = mock.MagicMock()
        cache.find_by_name.side_effect = (
            lambda n: {"name": n, "role": role} if n == name else None
        )
        monkeypatch.setattr(nc, "get_nets_cache", lambda: cache)

    return _set


# --------------------------- validation / routing ---------------------------

def test_missing_netname_returns_400(client):
    resp = client.post("/net/command", json={"action": "input"})
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_missing_action_returns_400(client):
    resp = client.post("/net/command", json={"netname": "net1"})
    assert resp.status_code == 400


def test_unknown_net_returns_404(client, fake_net, set_role):
    set_role("adc", name="other")  # cache only knows "other"
    resp = client.post("/net/command", json={"netname": "net1", "action": "input"})
    assert resp.status_code == 404
    assert "not found" in resp.get_json()["error"].lower()


def test_unsupported_role_returns_400(client, fake_net, set_role):
    set_role("webcam")  # valid NetType role, but not in ALLOWED_ACTIONS
    resp = client.post("/net/command", json={"netname": "net1", "action": "snap"})
    assert resp.status_code == 400


def test_disallowed_action_returns_400(client, fake_net, set_role):
    set_role("adc")
    resp = client.post("/net/command", json={"netname": "net1", "action": "output"})
    assert resp.status_code == 400
    assert "not valid" in resp.get_json()["error"].lower()


# ------------------------------ happy paths --------------------------------

def test_adc_input_returns_scalar_data(client, fake_net, set_role):
    set_role("adc")
    fake_net["obj"].input.return_value = 3.31
    resp = client.post("/net/command", json={"netname": "net1", "action": "input"})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "success": True, "netname": "net1", "role": "adc",
        "action": "input", "data": 3.31,
    }
    fake_net["obj"].input.assert_called_once_with()


def test_supply_set_voltage_passes_named_param(client, fake_net, set_role):
    set_role("power-supply")
    fake_net["obj"].set_voltage.return_value = None
    resp = client.post("/net/command", json={
        "netname": "net1", "action": "set_voltage", "params": {"voltage": 3.3},
    })
    assert resp.status_code == 200
    assert resp.get_json()["data"] is None
    fake_net["obj"].set_voltage.assert_called_once_with(voltage=3.3)


def test_i2c_scan_returns_list(client, fake_net, set_role):
    set_role("i2c")
    fake_net["obj"].scan.return_value = [0x48, 0x50]
    resp = client.post("/net/command", json={"netname": "net1", "action": "scan"})
    assert resp.status_code == 200
    assert resp.get_json()["data"] == [0x48, 0x50]


def test_tuple_return_is_normalized_to_list(client, fake_net, set_role):
    set_role("power-supply")
    fake_net["obj"].voltage.return_value = (1.0, 2.0)
    resp = client.post("/net/command", json={"netname": "net1", "action": "voltage"})
    assert resp.get_json()["data"] == [1.0, 2.0]


# ------------------------------- error paths -------------------------------

def test_bad_params_returns_400(client, fake_net, set_role):
    set_role("dac")
    fake_net["obj"].output.side_effect = TypeError(
        "output() got an unexpected keyword argument 'volts'"
    )
    resp = client.post("/net/command", json={
        "netname": "net1", "action": "output", "params": {"volts": 1.0},
    })
    assert resp.status_code == 400
    assert "invalid params" in resp.get_json()["error"].lower()


def test_backend_error_returns_400(client, fake_net, set_role):
    from lager.exceptions import ADCBackendError
    set_role("adc")
    fake_net["obj"].input.side_effect = ADCBackendError("no device")
    resp = client.post("/net/command", json={"netname": "net1", "action": "input"})
    assert resp.status_code == 400
    assert "no device" in resp.get_json()["error"]


def test_hardware_service_error_returns_502(client, fake_net, set_role):
    from lager.nets.device import DeviceError
    set_role("power-supply")
    fake_net["obj"].enable.side_effect = DeviceError("proxy down")
    resp = client.post("/net/command", json={"netname": "net1", "action": "enable"})
    assert resp.status_code == 502
    assert "hardware service error" in resp.get_json()["error"].lower()

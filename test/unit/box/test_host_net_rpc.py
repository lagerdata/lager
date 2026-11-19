# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""A script's net object whose driver runs in the host agent.

The stand-in (`hostlink.net_proxy`) is wired to the agent's routes
(`host.net_rpc`) through a Flask test client, with a fake driver in place of
`Net.get`. Covers the whole interface being forwarded (methods, properties,
attributes), values keeping their Python types, errors keeping their class,
objects with no JSON form coming back as handles (sessions, generators),
object lifetime (close, release before the next script), what is refused,
and `Net.get` choosing the stand-in only under LAGER_HOSTLINK.
"""
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
from flask import Flask

from lager.host import net_rpc
from lager.hostlink import net_proxy, wire
from lager.nets.constants import NetType


class DriverError(Exception):
    pass


class Session:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        self.log.append("enter")
        return self

    def __exit__(self, *exc):
        self.log.append("exit")
        return False

    def sign(self, data):
        return data[::-1]


class FakeDriver:
    """Stands in for a driver that needs hardware."""

    channel = 3

    def __init__(self, name):
        self.name = name
        self.log = []
        self.closed = False
        self._secret = "hidden"

    @property
    def temperature(self):
        self.log.append("temperature")
        return 21.5

    def read(self, duration=0.1):
        return {"power": 1.25, "duration": duration}

    def echo(self, data):
        return data

    def shapes(self):
        return (1, b"\x00\xff", {2: "two"}, {"a", "b"}, [1.5, None])

    def fail(self):
        raise DriverError("probe unplugged")

    def missing(self):
        raise KeyError("no such channel")

    def session(self):
        return Session(self.log)

    def samples(self, n):
        return (i * i for i in range(n))

    def close(self):
        self.closed = True


@pytest.fixture
def agent(monkeypatch):
    net_rpc.release_all()
    made = []

    def make(name, role):
        if name == "absent":
            raise DriverError(f"net {name} not found")
        made.append(FakeDriver(name))
        return made[-1]

    monkeypatch.setattr(net_rpc, "_make", make)
    app = net_rpc.install(Flask(__name__))

    @app.route(net_rpc.RELEASE_PATH, methods=["POST"])
    def _release():  # the hardware service's own route, stood in for
        return "{}"

    client = app.test_client()

    def post(url, data=None, headers=None, timeout=None):
        got = client.post(url.replace(net_proxy.BASE_URL, ""), data=data, headers=headers)
        text = got.get_data(as_text=True)
        post.timeouts.append((url, timeout))
        return SimpleNamespace(ok=got.status_code < 400, text=text,
                               json=lambda: json.loads(text))

    post.timeouts = []
    monkeypatch.setattr(net_proxy.requests, "post", post)
    yield type("Agent", (), {"made": made, "client": client, "post": post})
    net_rpc.release_all()


def test_methods_properties_and_attributes_are_all_forwarded(agent):
    net = net_proxy.open_net("watt1", "watt-meter")
    assert net.read() == {"power": 1.25, "duration": 0.1}
    assert net.read(duration=2) == {"power": 1.25, "duration": 2}
    assert net.name == "watt1" and net.channel == 3
    assert agent.made[0].log == []          # a property is not read until asked for
    assert net.temperature == 21.5
    assert agent.made[0].log == ["temperature"]
    assert "read" in dir(net) and "temperature" in dir(net)
    assert "FakeDriver" in repr(net)


def test_values_keep_their_python_types(agent):
    net = net_proxy.open_net("n", "hsm")
    assert net.echo(b"\x01\x02") == b"\x01\x02"
    assert net.shapes() == (1, b"\x00\xff", {2: "two"}, {"a", "b"}, [1.5, None])


def test_wire_round_trips_and_refuses_what_it_cannot_carry():
    import numpy
    value = {"t": (1, 2), "b": b"x", 3: [complex(1, 2)], "s": frozenset({1})}
    assert wire.decode(json.loads(json.dumps(wire.encode(value)))) == \
        {"t": (1, 2), "b": b"x", 3: [complex(1, 2)], "s": {1}}
    assert wire.encode(numpy.float64(1.5)) == 1.5
    assert wire.encode(numpy.arange(3)) == [0, 1, 2]
    assert wire.decode(wire.encode({"__bytes__": "user data"})) == {"__bytes__": "user data"}
    with pytest.raises(TypeError, match="cannot be sent"):
        wire.encode(object())
    with pytest.raises(TypeError, match="cannot be sent"):
        wire.encode([lambda: None])


def test_a_driver_error_is_raised_as_its_own_class(agent):
    net = net_proxy.open_net("n", "watt-meter")
    with pytest.raises(DriverError, match="probe unplugged"):
        net.fail()
    with pytest.raises(KeyError):
        net.missing()
    with pytest.raises(DriverError, match="net absent not found"):
        net_proxy.open_net("absent", "watt-meter")


def test_an_error_class_that_cannot_be_imported_becomes_runtime_error():
    assert net_proxy._error_class({"type": "Gone", "module": "no.such.module"}) is RuntimeError
    assert net_proxy._error_class({"type": "ValueError", "module": "builtins"}) is ValueError
    assert net_proxy._error_class({"type": "open", "module": "builtins"}) is RuntimeError
    assert net_proxy._error_class({"type": "X", "stop": True}) is StopIteration


def test_private_names_are_refused_on_both_sides(agent):
    net = net_proxy.open_net("n", "hsm")
    with pytest.raises(AttributeError):
        net._secret
    with pytest.raises(AttributeError):
        net.no_such_method()
    with pytest.raises(AttributeError, match="runs on the host agent"):
        net.channel = 4
    for attr in ("_secret", "__class__", "__dict__", None):
        response = agent.client.post(net_rpc.CALL_PATH, json={"handle": net._handle,
                                                              "attr": attr, "get": True})
        assert response.status_code == 400


def test_a_session_stays_on_the_agent_and_works_as_a_context_manager(agent):
    net = net_proxy.open_net("n", "hsm")
    with net.session() as session:
        assert isinstance(session, net_proxy.HostObject)
        assert session.sign(b"abc") == b"cba"
        assert agent.made[0].log == ["enter"]
    assert agent.made[0].log == ["enter", "exit"]


def test_a_generator_is_iterated_across_the_link(agent):
    net = net_proxy.open_net("n", "watt-meter")
    assert list(net.samples(4)) == [0, 1, 4, 9]


def test_close_closes_the_driver_once_and_later_calls_say_so(agent):
    net = net_proxy.open_net("n", "thermocouple")
    net.close()
    net.close()
    assert agent.made[0].closed is True
    with pytest.raises(RuntimeError, match="no longer open"):
        net.read()


def test_every_object_is_released_before_the_next_script(agent):
    first = net_proxy.open_net("a", "watt-meter")
    second = net_proxy.open_net("b", "hsm")
    session = second.session()
    assert agent.client.post(net_rpc.RELEASE_PATH).status_code == 200
    assert [d.closed for d in agent.made] == [True, True]
    for stale in (first, session):
        with pytest.raises(RuntimeError, match="no longer open"):
            stale.read() if stale is first else stale.sign(b"x")
    assert net_proxy.open_net("a", "watt-meter").read()["power"] == 1.25


def test_a_long_measurement_gets_a_matching_http_timeout(agent):
    net = net_proxy.open_net("n", "energy-analyzer")
    net.read(duration=600)
    net.read(0.5)
    # Only the calls: a stand-in left by an earlier test may close in between.
    calls = [t for url, t in agent.post.timeouts if url == net_proxy.CALL_URL]
    assert calls[-2:] == [720.0, 120.5]
    assert net_proxy._budget((True, 10 ** 9), {"timeout": -1}) == 120.0


def test_a_bad_open_request_is_refused(agent):
    assert agent.client.post(net_rpc.OPEN_PATH, json={"name": "n"}).status_code == 400


# ------------------------------------------------------------------ Net.get

@pytest.fixture
def saved_nets(monkeypatch):
    from lager.nets import net as net_module
    nets = [{"name": name, "role": role, "instrument": "x", "pin": 0, "address": "a"}
            for name, role in (("tc", "thermocouple"), ("w", "watt-meter"),
                               ("e", "energy-analyzer"), ("h", "hsm"), ("r", "rotation"),
                               ("act", "actuate"), ("arm1", "arm"))]
    monkeypatch.setattr(net_module.Net, "get_local_nets", classmethod(lambda cls: nets),
                        raising=False)
    return net_module


ROLES = [("tc", NetType.Thermocouple, "thermocouple"), ("w", NetType.WattMeter, "watt-meter"),
         ("e", NetType.EnergyAnalyzer, "energy-analyzer"), ("h", NetType.Hsm, "hsm"),
         ("r", NetType.Rotation, "rotation"), ("act", NetType.Actuate, "actuate"),
         ("arm1", NetType.Arm, "arm")]


@pytest.mark.parametrize("name, net_type, role", ROLES)
def test_net_get_returns_the_stand_in_under_hostlink(saved_nets, name, net_type, role):
    with mock.patch.dict(os.environ, {"LAGER_HOSTLINK": "1"}), \
            mock.patch.object(net_proxy, "open_net", return_value="stand-in") as opened:
        assert saved_nets.Net.get(name, type=net_type) == "stand-in"
    opened.assert_called_once_with(name, role)


def test_net_get_never_uses_the_stand_in_without_hostlink(saved_nets):
    environ = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
    with mock.patch.dict(os.environ, environ, clear=True), \
            mock.patch.object(net_proxy, "open_net") as opened, \
            mock.patch.object(saved_nets, "PhidgetThermocouple", return_value="real") as real:
        assert saved_nets.Net.get("tc", type=NetType.Thermocouple) == "real"
    opened.assert_not_called()
    real.assert_called_once()


# --------------------------------------------------- module-level helpers

from lager.dispatchers.base import BaseDispatcher  # noqa: E402


class FakeDispatcher(BaseDispatcher):
    ROLE = "watt-meter"
    made = []

    def _choose_driver(self, instrument_name):
        return FakeDriver

    def _make_error(self, message):
        return DriverError(message)

    def _make_driver(self, rec, netname, channel):
        if netname == "broken":
            raise DriverError("no instrument at " + rec["address"])
        FakeDispatcher.made.append((rec, netname, channel))
        return FakeDriver(netname)


def test_a_dispatcher_keeps_its_own_make_driver_without_hostlink():
    environ = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
    with mock.patch.dict(os.environ, environ, clear=True):
        dispatcher = FakeDispatcher()
    assert "_make_driver" not in vars(dispatcher)
    assert isinstance(dispatcher._make_driver({"address": "a"}, "n", 1), FakeDriver)


def test_under_hostlink_the_agent_runs_that_dispatchers_make_driver(agent, monkeypatch):
    # The agent only builds dispatchers from the Box's own package.
    import sys
    import types
    fake_module = types.ModuleType("lager._fake_dispatcher")
    fake_module.FakeDispatcher = FakeDispatcher
    monkeypatch.setitem(sys.modules, "lager._fake_dispatcher", fake_module)
    monkeypatch.setattr(FakeDispatcher, "__module__", "lager._fake_dispatcher")
    FakeDispatcher.made.clear()
    with mock.patch.dict(os.environ, {"LAGER_HOSTLINK": "1"}):
        dispatcher = FakeDispatcher()
    # The agent builds the dispatcher without the switch, so it makes the real one.
    environ = {k: v for k, v in os.environ.items() if k != "LAGER_HOSTLINK"}
    with mock.patch.dict(os.environ, environ, clear=True):
        driver = dispatcher._make_driver({"address": "USB0::1", "pin": 2}, "w1", 2)
        assert isinstance(driver, net_proxy.HostObject)
        assert FakeDispatcher.made == [({"address": "USB0::1", "pin": 2}, "w1", 2)]
        assert driver.read(duration=1) == {"power": 1.25, "duration": 1}
        with pytest.raises(DriverError, match="no instrument at USB0::9"):
            dispatcher._make_driver({"address": "USB0::9"}, "broken", 0)


@pytest.mark.parametrize("module, name", [
    ("os", "system"), ("lager.nets.net", "Net"), ("lager.nope", "X"),
    ("lager.dispatchers.base", "no such"), (None, "X"), ("subprocess", "Popen"),
])
def test_only_a_box_dispatcher_class_can_be_asked_to_make_a_driver(agent, module, name):
    response = agent.client.post(net_rpc.OPEN_DRIVER_PATH,
                                 json={"module": module, "class": name, "args": []})
    assert response.status_code == 400


@pytest.mark.parametrize("path", [
    "lager.power.supply.dispatcher", "lager.power.battery.dispatcher",
    "lager.power.eload.dispatcher", "lager.power.solar.dispatcher",
    "lager.io.adc.dispatcher", "lager.io.gpio.dispatcher", "lager.io.dac.dispatcher",
    "lager.measurement.watt.dispatcher", "lager.measurement.thermocouple.dispatcher",
    "lager.measurement.energy_analyzer.dispatcher", "lager.security.hsm.dispatcher",
])
def test_every_box_dispatcher_goes_through_the_base_constructor(path):
    """The hand-off is installed there; a dispatcher with its own __init__ would miss it."""
    import importlib
    import inspect
    module = importlib.import_module(path)
    classes = [c for _n, c in inspect.getmembers(module, inspect.isclass)
               if issubclass(c, BaseDispatcher) and c is not BaseDispatcher
               and c.__module__ == path]
    assert classes
    for cls in classes:
        assert cls.__init__ is BaseDispatcher.__init__

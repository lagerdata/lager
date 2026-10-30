# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for the Siglent SDM3065X DMM driver and the ``dmm`` net role.

The driver is exercised against a fake SCPI transport that records writes
and answers queries from a script, so command forms, overload handling and
error-queue handling are checked without hardware. The /net/command
handler is exercised with ``Net`` and ``Device`` mocked, as in
test_hsm_net_command.py.
"""

from __future__ import annotations

from collections import defaultdict, deque
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask
from lager import dmm_hs
from lager.exceptions import DeviceNotFoundError, DMMBackendError
from lager.http_handlers import net_command
from lager.http_handlers.usb_scanner import CHANNEL_MAPS, SUPPORTED_USB
from lager.measurement.dmm import (
    FUNCTIONS,
    UNITS,
    DMMNet,
    DMMUnsupportedError,
    dispatcher,
)
from lager.measurement.dmm import siglent_sdm3065x as sdm
from lager.nets.constants import NetType

_IDN = "Siglent Technologies,SDM3065X,SDM36HCX7R0827,3.01.01.10"
_ADDRESS = "USB0::0xF4EC::0x1203::SDM36HCX7R0827::INSTR"


def _visa_timeout() -> Exception:
    # VI_ERROR_TMO; built from the module the driver catches it from.
    return sdm.pyvisa.errors.VisaIOError(-1073807339)


class FakeSCPI:
    """A pyvisa resource stand-in that logs every command in order.

    ``replies`` maps a query to a queue of answers; an answer that is an
    exception is raised instead. A query with an empty queue answers with
    its default: the IDN, "+0,\"No error\"" for SYST:ERR?, else "0".
    ``timeouts`` records the timeout each query ran under; ``clears`` counts
    device clears.
    """

    def __init__(self, replies: dict[str, list] | None = None) -> None:
        self.log: list[str] = []
        self.replies: defaultdict[str, deque] = defaultdict(deque)
        self.load(replies or {})
        self.closed = False
        self.timeout = sdm._VISA_TIMEOUT_MS
        self.timeouts: list[tuple[str, int]] = []
        self.clears = 0

    def load(self, replies: dict[str, list]) -> None:
        for query, answers in replies.items():
            self.replies[query].extend(answers)

    def clear(self) -> None:
        self.clears += 1

    def write(self, command: str) -> None:
        self.log.append(command)

    def query(self, command: str) -> str:
        self.log.append(command)
        self.timeouts.append((command, self.timeout))
        if self.replies[command]:
            answer = self.replies[command].popleft()
            if isinstance(answer, Exception):
                raise answer
            return answer
        return {"*IDN?": _IDN, "SYST:ERR?": '+0,"No error"'}.get(command, "0")

    def close(self) -> None:
        self.closed = True


def _driver(replies: dict[str, list] | None = None) -> tuple[sdm.SiglentSDM3065X, FakeSCPI]:
    """A driver past its open; ``replies`` apply to what it is asked afterwards."""
    fake = FakeSCPI()
    driver = sdm.SiglentSDM3065X({"name": "dmm1", "address": _ADDRESS}, resource=fake)
    fake.log.clear()
    fake.load(replies or {})
    return driver, fake


class TestBuildCommands:

    @pytest.mark.parametrize("function, expected", [
        ("dcv", "CONF:VOLT:DC"), ("acv", "CONF:VOLT:AC"),
        ("dci", "CONF:CURR:DC"), ("aci", "CONF:CURR:AC"),
        ("res", "CONF:RES"), ("fres", "CONF:FRES"),
        ("cont", "CONF:CONT"), ("diode", "CONF:DIOD"),
        ("freq", "CONF:FREQ"), ("period", "CONF:PER"),
        ("cap", "CONF:CAP"),
    ])
    def test_every_function_configures_with_auto_range(self, function, expected):
        assert sdm.build_commands(function) == [expected]

    def test_every_interface_function_has_a_command(self):
        for function in FUNCTIONS:
            assert sdm.build_commands(function)[0].startswith("CONF:")

    def test_temperature_forces_celsius(self):
        assert sdm.build_commands("temp") == ["CONF:TEMP", "UNIT:TEMP C"]

    @pytest.mark.parametrize("sensor, probe", [
        ("rtd:pt100", "RTD,PT100"), ("RTD:PT1000", "RTD,PT1000"),
        ("ther:k", "THER,KITS90"), ("ther:t", "THER,TITS90"),
    ])
    def test_temperature_sensor(self, sensor, probe):
        assert sdm.build_commands("temp", sensor=sensor)[0] == f"CONF:TEMP {probe}"

    @pytest.mark.parametrize("function, full_scale, expected", [
        ("dcv", 20, "CONF:VOLT:DC 20"),
        ("dcv", 1000, "CONF:VOLT:DC 1000"),
        ("acv", 0.2, "CONF:VOLT:AC 0.2"),
        ("dci", 200e-6, "CONF:CURR:DC 0.0002"),
        ("aci", 10, "CONF:CURR:AC 10"),
        ("res", 1e6, "CONF:RES 1e+06"),
        ("fres", 200, "CONF:FRES 200"),
        ("cap", 2e-6, "CONF:CAP 2e-06"),
        ("cap", 0.1, "CONF:CAP 0.1"),
        ("dcv", "20", "CONF:VOLT:DC 20"),
    ])
    def test_fixed_range(self, function, full_scale, expected):
        assert sdm.build_commands(function, range=full_scale) == [expected]

    @pytest.mark.parametrize("auto", [None, "auto", "AUTO"])
    def test_auto_range_sends_no_range(self, auto):
        assert sdm.build_commands("dcv", range=auto) == ["CONF:VOLT:DC"]

    @pytest.mark.parametrize("function, nplc, expected", [
        ("dcv", 10, "VOLT:DC:NPLC 10"),
        ("dci", 100, "CURR:DC:NPLC 100"),
        ("res", 0.005, "RES:NPLC 0.005"),
        ("fres", 0.5, "FRES:NPLC 0.5"),
    ])
    def test_nplc(self, function, nplc, expected):
        assert sdm.build_commands(function, nplc=nplc)[-1] == expected

    @pytest.mark.parametrize("kwargs, fragment", [
        ({"function": "ohms"}, "Unknown DMM function"),
        ({"function": "dcv", "range": 10}, "no 10 range"),
        ({"function": "acv", "range": 1000}, "no 1000 range"),
        ({"function": "dcv", "range": "big"}, "number or 'auto'"),
        ({"function": "cont", "range": 2000}, "no selectable range"),
        ({"function": "diode", "range": 2}, "no selectable range"),
        ({"function": "freq", "range": 20}, "no selectable range"),
        ({"function": "temp", "range": 100}, "no selectable range"),
        ({"function": "acv", "nplc": 10}, "no NPLC setting"),
        ({"function": "cap", "nplc": 1}, "no NPLC setting"),
        ({"function": "dcv", "nplc": 2}, "does not support NPLC 2"),
        ({"function": "dcv", "nplc": "x"}, "NPLC must be a number"),
        ({"function": "dcv", "sensor": "rtd:pt100"}, "sensor applies to temperature"),
        ({"function": "temp", "sensor": "ther:x"}, "Unknown temperature sensor"),
    ])
    def test_unsupported_requests_raise(self, kwargs, fragment):
        with pytest.raises(DMMUnsupportedError, match=fragment):
            sdm.build_commands(**kwargs)

    def test_unsupported_is_a_value_error(self):
        # net_command maps ValueError to 400, so a bad request is not reported
        # as a hardware fault.
        assert issubclass(DMMUnsupportedError, ValueError)


class TestParsers:

    @pytest.mark.parametrize("reply, value", [
        ("-5.21391630E-04\n", -5.21391630e-04),
        ("+1.49999321E+00", 1.49999321),
        ("+7.26141264E-10", 7.26141264e-10),
    ])
    def test_reading(self, reply, value):
        assert sdm.parse_reading(reply) == pytest.approx(value)

    @pytest.mark.parametrize("reply", ["+9.90000000E+37", "9.9E37", "-9.90000000E+37", "inf"])
    def test_overload_is_none(self, reply):
        assert sdm.parse_reading(reply) is None

    def test_garbage_reading_raises(self):
        with pytest.raises(DMMBackendError, match="not a number"):
            sdm.parse_reading("ERROR")

    @pytest.mark.parametrize("reply, parsed", [
        ('+0,"No error"', (0, "No error")),
        ("0  No Error", (0, "No Error")),
        ('-222,"Data out of range"', (-222, "Data out of range")),
        ("-113, Undefined header", (-113, "Undefined header")),
    ])
    def test_error_reply(self, reply, parsed):
        assert sdm.parse_error(reply) == parsed

    def test_unparseable_error_reply_is_reported(self):
        assert sdm.parse_error("garbled") == (-1, "garbled")


class TestDriver:

    def test_open_checks_idn(self):
        fake = FakeSCPI({"*IDN?": ["RIGOL TECHNOLOGIES,DL3021,DL3A1,00.01"]})
        with pytest.raises(DeviceNotFoundError, match="not a Siglent SDM3065X"):
            sdm.SiglentSDM3065X({"address": _ADDRESS}, resource=fake)

    def test_no_address_and_no_resource(self):
        with pytest.raises(DMMBackendError, match="No VISA address"):
            sdm.SiglentSDM3065X({"name": "dmm1"})

    def test_measure_sequence(self):
        driver, fake = _driver({"READ?": ["+3.30012345E+00"]})
        value = driver.measure("dcv", range=20, nplc=10)
        assert value == pytest.approx(3.30012345)
        assert fake.log == ["*CLS", "CONF:VOLT:DC 20", "VOLT:DC:NPLC 10", "SYST:ERR?", "READ?"]

    def test_overload_returns_none(self):
        driver, _ = _driver({"READ?": ["+9.90000000E+37"]})
        assert driver.measure("cont") is None

    def test_rejected_configuration_raises_before_reading(self):
        driver, fake = _driver({"SYST:ERR?": ['-222,"Data out of range"', '+0,"No error"']})
        with pytest.raises(DMMBackendError, match="Data out of range"):
            driver.measure("dcv", range=20)
        assert "READ?" not in fake.log

    def test_unsupported_request_never_touches_the_meter(self):
        driver, fake = _driver()
        with pytest.raises(DMMUnsupportedError):
            driver.measure("acv", nplc=10)
        assert fake.log == []

    def test_error_queue_is_drained(self):
        driver, _ = _driver({"SYST:ERR?": ['-113,"Undefined header"', "-222, Data out of range",
                                           "0  No Error"]})
        assert driver.errors() == ["-113, Undefined header", "-222, Data out of range"]

    def test_error_drain_is_bounded(self):
        driver, fake = _driver({"SYST:ERR?": ['-100,"Command error"'] * 100})
        assert len(driver.errors()) == sdm._MAX_ERROR_READS
        assert fake.log.count("SYST:ERR?") == sdm._MAX_ERROR_READS

    def test_open_probes_the_error_queue_with_a_short_timeout(self):
        fake = FakeSCPI()
        sdm.SiglentSDM3065X({"address": _ADDRESS}, resource=fake)
        assert ("SYST:ERR?", sdm._PROBE_TIMEOUT_MS) in fake.timeouts
        assert fake.timeout == sdm._VISA_TIMEOUT_MS
        assert fake.clears == 0

    def test_unanswered_probe_turns_checking_off_before_the_first_read(self):
        fake = FakeSCPI({"SYST:ERR?": [_visa_timeout()], "READ?": ["1.0"]})
        driver = sdm.SiglentSDM3065X({"address": _ADDRESS}, resource=fake)
        # The probe waited the short timeout, cleared the session and put the
        # full timeout back.
        assert fake.timeouts[-1] == ("SYST:ERR?", sdm._PROBE_TIMEOUT_MS)
        assert fake.clears == 1
        assert fake.timeout == sdm._VISA_TIMEOUT_MS
        fake.log.clear()
        assert driver.measure("dcv") == 1.0
        assert "SYST:ERR?" not in fake.log
        assert driver.errors() == []

    def test_error_query_timeout_later_turns_checking_off(self):
        driver, fake = _driver({"SYST:ERR?": [_visa_timeout()], "READ?": ["1.0", "2.0"]})
        assert driver.measure("dcv") == 1.0
        assert driver.measure("dcv") == 2.0
        # Asked once; after the timeout the session reads without asking.
        assert fake.log.count("SYST:ERR?") == 1
        assert fake.clears == 1

    def test_read_timeout_is_a_backend_error_and_clears(self):
        driver, fake = _driver({"READ?": [_visa_timeout()]})
        with pytest.raises(DMMBackendError, match="READ"):
            driver.measure("cap")
        assert fake.clears == 1

    def test_close(self):
        driver, fake = _driver()
        driver.close()
        assert fake.closed


class TestRouting:

    def test_role(self):
        assert NetType.from_role("dmm") is NetType.DMM

    @pytest.mark.parametrize("instrument, module", [
        ("Siglent_SDM3065X", "dmm_hs"),
        ("siglent_sdm3065x-sc", "dmm_hs"),
        ("Keysight_34465A", None),
        ("", None),
    ])
    def test_module_for_instrument(self, instrument, module):
        assert dispatcher.module_for_instrument(instrument) == module

    def test_make_driver_refuses_unknown_instrument(self):
        with pytest.raises(DMMBackendError, match="Unsupported DMM instrument"):
            dispatcher.make_driver({"instrument": "Fluke_8846A", "address": _ADDRESS})

    def test_make_net_builds_a_hardware_service_proxy(self):
        rec = {"name": "dmm1", "role": "dmm", "instrument": "Siglent_SDM3065X",
               "address": _ADDRESS}
        with patch.object(dispatcher, "Device") as device_cls:
            net = dispatcher.make_net("dmm1", rec)
        assert isinstance(net, DMMNet)
        args, kwargs = device_cls.call_args
        assert args == ("dmm_hs", {"name": "dmm1", "address": _ADDRESS,
                                   "instrument": "Siglent_SDM3065X"})
        assert kwargs["timeout"] > sdm._VISA_TIMEOUT_MS / 1000

    def test_make_net_refuses_unsupported_requests_before_the_proxy(self):
        # A driver exception comes back through the proxy as DeviceError, so
        # the net has to raise DMMUnsupportedError before calling it.
        rec = {"name": "dmm1", "role": "dmm", "instrument": "Siglent_SDM3065X",
               "address": _ADDRESS}
        with patch.object(dispatcher, "Device") as device_cls:
            net = dispatcher.make_net("dmm1", rec)
        with pytest.raises(DMMUnsupportedError, match="no NPLC setting for 'acv'"):
            net.measure("acv", nplc=1)
        with pytest.raises(DMMUnsupportedError, match="no 5000 range"):
            net.measure("dcv", range=5000)
        device_cls.return_value.measure.assert_not_called()
        device_cls.return_value.measure.return_value = 0.5
        assert net.measure("dcv", range=20) == 0.5

    def test_dmm_net_forwards_and_rejects_unknown_functions(self):
        device = MagicMock()
        device.measure.return_value = 1.5
        net = DMMNet("dmm1", device)
        assert net.measure("res", range=2000) == 1.5
        device.measure.assert_called_once_with("res", range=2000, nplc=None, sensor=None)
        with pytest.raises(DMMUnsupportedError):
            net.measure("ohms")

    def test_dmm_hs_factory(self):
        with patch.object(dmm_hs, "make_driver", return_value="driver") as make:
            assert dmm_hs.create_device({"instrument": "Siglent_SDM3065X"}) == "driver"
        make.assert_called_once()

    def test_usb_catalog(self):
        entry = SUPPORTED_USB["Siglent_SDM3065X"]
        assert (entry["vid"], entry["net_type"]) == ("f4ec", ["dmm"])
        assert CHANNEL_MAPS["Siglent_SDM3065X"] == {"dmm": ["1"]}

    def test_units_cover_every_function(self):
        assert set(UNITS) == set(FUNCTIONS)


_SAVED = [{"name": "dmm1", "role": "dmm", "instrument": "Siglent_SDM3065X",
           "address": _ADDRESS}]


@pytest.fixture
def post():
    app = Flask(__name__)
    net_command.register_net_command_routes(app)
    client = app.test_client()

    def _post(action, params=None, dev=None):
        dev = MagicMock() if dev is None else dev
        body = {"netname": "dmm1", "action": action, "params": params or {}}
        with patch("lager.http_handlers.net_command.Net") as net_cls, \
                patch("lager.http_handlers.net_command.Device", return_value=dev) as device_cls:
            net_cls.get_local_nets.return_value = _SAVED
            resp = client.post("/net/command", json=body)
        return resp, dev, device_cls

    return _post


class TestNetCommand:

    def test_measure(self, post):
        dev = MagicMock()
        dev.measure.return_value = 3.3
        resp, _, device_cls = post("measure", {"function": "dcv", "range": 20}, dev)
        assert resp.status_code == 200
        body = resp.get_json()
        assert (body["value"], body["unit"], body["overload"]) == (3.3, "V", False)
        assert body["message"] == "3.3 V"
        dev.measure.assert_called_once_with("dcv", range=20, nplc=None, sensor=None)
        args, kwargs = device_cls.call_args
        assert args[0] == "dmm_hs" and args[1]["address"] == _ADDRESS
        assert kwargs["timeout"] == pytest.approx(60.0)

    def test_default_function_is_dc_volts(self, post):
        resp, dev, _ = post("measure", {})
        assert resp.status_code == 200
        assert dev.measure.call_args.args == ("dcv",)

    @pytest.mark.parametrize("function, message", [("dcv", "OVERLOAD"), ("cont", "OPEN")])
    def test_overload(self, post, function, message):
        dev = MagicMock()
        dev.measure.return_value = None
        resp, _, _ = post("measure", {"function": function}, dev)
        body = resp.get_json()
        assert body["overload"] is True
        assert body["message"] == message
        assert "value" not in body

    def test_unsupported_is_400_and_never_reaches_the_meter(self, post):
        resp, dev, device_cls = post("measure", {"function": "acv", "nplc": 10})
        assert resp.status_code == 400
        assert "no NPLC setting" in resp.get_json()["error"]
        device_cls.assert_not_called()
        dev.measure.assert_not_called()

    def test_errors(self, post):
        dev = MagicMock()
        dev.errors.return_value = ["-113, Undefined header"]
        body = post("errors", dev=dev)[0].get_json()
        assert body["value"] == ["-113, Undefined header"]

    def test_identify(self, post):
        dev = MagicMock()
        dev.identify.return_value = _IDN
        assert post("identify", dev=dev)[0].get_json()["value"] == _IDN

    def test_unknown_action(self, post):
        resp, _, _ = post("read")
        assert resp.status_code == 400
        assert "Unknown action" in resp.get_json()["error"]

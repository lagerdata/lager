# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Net.get gaps: Rigol sample cursors (#849), solar and Waveform nets (#844).

- A Rigol MSO5000 analog net's ``set_cursors``, ``get_cursors``,
  ``clear_cursors``, ``measure_cursors``, ``get_sample_rate`` and
  ``capabilities`` fell through to the SCPI driver, which has none of them,
  and the hardware service answered "Function not found". They now go to the
  ``scope_hs`` driver the `lager scope` route uses, so there is one set of
  cursors for a script, both CLIs and the web UI.
- A solar net (role ``solar``) had no NetType, so ``Net.get`` could not return
  it. ``NetType.Solar`` now returns a wrapper over the ``solar_hs`` adapter.
- ``NetType.Waveform`` has no driver; ``Net.get`` now says so instead of
  failing with "Invalid mapper type rigol_mso5000".

The hardware service is replaced by a recording fake ``Device``: what is under
test is which adapter a call reaches and with what, not the instrument.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")
if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)

from lager.exceptions import SolarBackendError  # noqa: E402
from lager.nets import device as device_mod  # noqa: E402
from lager.nets.constants import NetType  # noqa: E402
from lager.nets.mappers.rigol_mso5000 import RigolMSO5000AnalogMapper  # noqa: E402
from lager.nets.net import Net  # noqa: E402
from lager.power.solar.solar_net_wrapper import SolarNetWrapper  # noqa: E402


class _FakeDevice:
    """Stands in for the hardware-service proxy; records every call."""

    instances = []
    answers = {}

    def __init__(self, device_name, net_info=None, timeout=None):
        self.device_name = device_name
        self.net_info = net_info
        self.timeout = timeout
        self.calls = []
        _FakeDevice.instances.append(self)

    def __getattr__(self, func):
        def call(*args, **kwargs):
            self.calls.append((func, args, kwargs))
            return _FakeDevice.answers.get(func)
        return call


@pytest.fixture
def fake_device(monkeypatch):
    _FakeDevice.instances = []
    _FakeDevice.answers = {}
    monkeypatch.setattr(device_mod, "Device", _FakeDevice)
    return _FakeDevice


# --------------------------------------------------------------------------
# #849: Rigol sample cursors, sample rate, capabilities
# --------------------------------------------------------------------------

RIGOL_RECORD = {
    "name": "scope_ch1", "role": "scope-channel", "instrument": "Rigol_MSO5204",
    "address": "USB0::0x1AB1::0x0515::MS5A000001::INSTR", "pin": 1,
}


class _FakeNet:
    name = "scope_ch1"
    channel = 1


def _rigol_mapper():
    scpi = MagicMock()
    scpi.net_info = dict(RIGOL_RECORD)
    return RigolMSO5000AnalogMapper(_FakeNet(), scpi), scpi


@pytest.mark.parametrize("call, expected", [
    (lambda m: m.set_cursors(time=(0.0, 1e-3)),
     ("set_cursors", (), {"time": (0.0, 1e-3), "volts": None, "channel": None})),
    (lambda m: m.get_cursors(), ("get_cursors", (), {})),
    (lambda m: m.clear_cursors(), ("clear_cursors", (), {})),
    (lambda m: m.measure_cursors(), ("measure_cursors", (), {"channel": None, "timeout": None})),
    (lambda m: m.get_sample_rate(), ("get_sample_rate", (), {})),
    (lambda m: m.capabilities(), ("capabilities", (), {"refresh": False})),
])
def test_rigol_daemon_calls_reach_scope_hs_not_the_scpi_driver(fake_device, call, expected):
    mapper, scpi = _rigol_mapper()
    call(mapper)

    [dev] = fake_device.instances
    assert dev.device_name == "scope_hs"
    assert dev.calls == [expected]
    # Not the SCPI driver, which has none of these and answered
    # "Function not found".
    assert not getattr(scpi, expected[0]).called


def test_rigol_scope_hs_is_told_the_net_and_the_scope_lock(fake_device):
    mapper, _ = _rigol_mapper()
    mapper.get_sample_rate()
    [dev] = fake_device.instances
    # The same three fields the `lager scope` route sends, so the call takes
    # the scope's shared lock and the adapter reads the net's own channel.
    assert dev.net_info == {
        "name": "scope_ch1",
        "instrument": "Rigol_MSO5204",
        "device_id": "scope:MS5A000001",
    }
    assert dev.timeout == 30.0


def test_rigol_cursors_set_and_read_through_one_driver(fake_device):
    """Set and measure must land on the same driver, or a script's cursors
    would not be the ones it reads back."""
    mapper, _ = _rigol_mapper()
    fake_device.answers["measure_cursors"] = {"cursors": {}, "readings": {"dt": 1e-3}}
    mapper.set_cursors(time=(0.0, 1e-3))
    result = mapper.measure_cursors(channel="A")
    assert len(fake_device.instances) == 1
    assert result["readings"] == {"dt": 1e-3}
    assert fake_device.instances[0].calls[-1] == (
        "measure_cursors", (), {"channel": "A", "timeout": None})


def test_rigol_capabilities_forwards_refresh(fake_device):
    mapper, _ = _rigol_mapper()
    fake_device.answers["capabilities"] = {"model": "MSO5204", "analog_channels": 4}
    assert mapper.capabilities(refresh=True) == {"model": "MSO5204", "analog_channels": 4}
    assert fake_device.instances[0].calls == [("capabilities", (), {"refresh": True})]


# --------------------------------------------------------------------------
# #844: Waveform
# --------------------------------------------------------------------------

def test_waveform_refuses_naming_the_type_to_use(monkeypatch):
    # Refused before any lookup: no saved nets are read.
    monkeypatch.setattr(Net, "get_local_nets", classmethod(lambda cls: pytest.fail("read nets")))
    with pytest.raises(NotImplementedError, match="NetType.Analog"):
        Net.get("wave1", NetType.Waveform)


def test_waveform_stays_in_the_enum():
    """Kept so a script that names it still imports."""
    assert NetType.from_role("waveform") is NetType.Waveform


# --------------------------------------------------------------------------
# #844: Solar
# --------------------------------------------------------------------------

SOLAR_RECORD = {
    "name": "solar1", "role": "solar", "instrument": "EA_PSB_10080_60",
    "address": "USB0::0x232E::0x0053::2232220001::INSTR", "pin": 1,
}


def test_solar_role_has_a_net_type():
    assert NetType.from_role("solar") is NetType.Solar


def test_solar_is_appended_so_no_member_value_moves():
    members = list(NetType)
    assert members[-1] is NetType.Solar
    assert NetType.BleSniffer.value == len(members) - 1


def test_net_get_returns_the_solar_wrapper(monkeypatch):
    monkeypatch.setattr(Net, "get_local_nets",
                        classmethod(lambda cls: [dict(SOLAR_RECORD)]))
    net = Net.get("solar1", NetType.Solar)
    assert isinstance(net, SolarNetWrapper)
    assert net.name == "solar1"
    assert net.address == SOLAR_RECORD["address"]


def test_solar_prefers_the_mapping_device_override():
    record = dict(SOLAR_RECORD, mappings=[{"net": "solar1", "device_override": "TCPIP::10.0.0.9::INSTR"}])
    assert SolarNetWrapper("solar1", record).address == "TCPIP::10.0.0.9::INSTR"


def _solar():
    return SolarNetWrapper("solar1", dict(SOLAR_RECORD))


def test_solar_enable_and_disable_run_the_mode_through_solar_hs(fake_device):
    pv = _solar()
    pv.enable()
    pv.disable()
    names = [d.device_name for d in fake_device.instances]
    assert names == ["solar_hs", "solar_hs"]
    assert [d.calls[0][0] for d in fake_device.instances] == ["set_mode", "stop_mode"]
    # The same net_info the `lager solar` route sends, so the call takes the
    # instrument's VISA-address lock in the hardware service.
    assert fake_device.instances[0].net_info == {
        "name": "solar1", "address": SOLAR_RECORD["address"],
        "instrument": "EA_PSB_10080_60",
    }
    assert fake_device.instances[0].timeout == 90.0


def test_solar_module_function_names_are_aliases(fake_device):
    pv = _solar()
    pv.set_to_solar_mode()
    pv.stop_solar_mode()
    assert [d.calls[0][0] for d in fake_device.instances] == ["set_mode", "stop_mode"]


@pytest.mark.parametrize("method, answer, expected", [
    ("mpp_current", "1.234 A", 1.234),
    ("mpp_voltage", "41.5 V", 41.5),
    ("temperature", "25.0°C", 25.0),
    ("voc", "48", 48.0),
    ("voc", "n/a", None),
])
def test_solar_reads_return_the_number(fake_device, method, answer, expected):
    fake_device.answers[method] = answer
    assert getattr(_solar(), method)() == expected


def test_solar_irradiance_sets_then_returns_the_reading(fake_device):
    fake_device.answers["irradiance"] = "800.0"
    assert _solar().irradiance(800) == 800.0
    assert fake_device.instances[0].calls == [("irradiance", (800.0,), {})]


def test_solar_irradiance_read_passes_none(fake_device):
    fake_device.answers["irradiance"] = "1000"
    assert _solar().irradiance() == 1000.0
    assert fake_device.instances[0].calls == [("irradiance", (None,), {})]


@pytest.mark.parametrize("method, value, match", [
    ("irradiance", -1, "cannot be negative"),
    ("irradiance", 1501, "max 1500"),
    ("resistance", 0, "must be positive"),
])
def test_solar_refuses_out_of_range_values_before_the_instrument(fake_device, method, value, match):
    with pytest.raises(ValueError, match=match):
        getattr(_solar(), method)(value)
    assert fake_device.instances == []


def test_solar_without_an_address_says_so():
    with pytest.raises(SolarBackendError, match="no device address"):
        SolarNetWrapper("solar1", {"name": "solar1", "role": "solar",
                                   "instrument": "EA_PSB_10080_60"})


def test_solar_unsupported_instrument_says_so():
    with pytest.raises(SolarBackendError, match="Unsupported solar instrument"):
        SolarNetWrapper("solar1", dict(SOLAR_RECORD, instrument="Rigol_DP832"))

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager nets add` and `add-batch` accept what the box can drive, and refuse
what it cannot (#513, #516, and the CLI half of #515).

* Five instruments the box scanner detects, and has drivers for, were missing
  from `INSTRUMENT_NET_MAP`, so `nets add` refused every net on them while
  `lager instruments` listed their channels. (`add-all` and the TUI skip that
  table, which is why the bench's MCC USB-202 nets exist.)
* `add-batch` kept five keys of each record and dropped the rest without a
  word, `params` included, so a batch could not create a LabJack net on custom
  pins or an FTDI net on channel B. It also skipped the role and ambiguity
  checks `nets add` makes.
* A LabJack U3's FIO0-FIO3 were accepted as custom pins and refused only by
  the box, at the first transfer.
"""

from __future__ import annotations

import importlib
import json

import pytest
from click.testing import CliRunner
from unittest.mock import patch

nets_mod = importlib.import_module("cli.commands.box.nets")
from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402


def _device(name, address, channels, serial: str | None = "S1"):
    return {"name": name, "vid": "0000", "pid": "0000", "serial": serial,
            "address": address, "net_type": list(channels), "channels": channels}


DP832 = _device("Rigol_DP832", "USB0::0x1AB1::0x0E11::DP8C1::INSTR",
                {"power-supply": ["1", "2", "3"]})
E36312A = _device("KEYSIGHT_E36312A", "USB0::0x2A8D::0x1102::MY1::INSTR",
                  {"power-supply": ["1", "2", "3"]})
USB202 = _device("MCC_USB-202", "USB0::0x09DB::0x012B::01::INSTR",
                 {"adc": [f"CH{n}" for n in range(8)], "dac": ["DAC0", "DAC1"],
                  "gpio": [f"DIO{n}" for n in range(8)]})
PHIDGET = _device("Phidget", "USB0::0x06C2::0x0046::P1::INSTR",
                  {"thermocouple": ["0", "1", "2", "3"]})
JLINK_BASE = _device("J-Link_Base_Compact", "USB0::0x1366::0x1020::JB1::INSTR",
                     {"debug": ["DEVICE_TYPE"]})
T7_ADDR = "USB0::0x0CD5::0x0007::470012345::INSTR"
T7 = _device("LabJack_T7", T7_ADDR,
             {"gpio": ["FIO4"], "spi": ["FIO0-FIO3"], "i2c": ["FIO4-FIO5"]})
U3_ADDR = "USB0::0x0CD5::0x0003::::INSTR"
U3 = _device("LabJack_U3", U3_ADDR,
             {"gpio": ["FIO4"], "spi": ["FIO4-FIO7"], "i2c": ["FIO6-FIO7"]}, serial=None)
FT4232H_ADDR = "USB0::0x0403::0x6011::FT1::INSTR"
FT4232H = _device("FTDI_FT4232H", FT4232H_ADDR,
                  {"gpio": ["0", "1"], "spi": ["SPI"], "i2c": ["I2C"]})

DEVICES = [DP832, E36312A, USB202, PHIDGET, JLINK_BASE, T7, U3, FT4232H]


@pytest.fixture
def fake_box():
    box = FakeBoxHTTP(list(DEVICES))
    with patch("requests.request", box.request), \
            patch("cli.box_storage.resolve_and_validate_box", lambda ctx, name: name or "testbox"), \
            patch.object(nets_mod, "_resolve_box", lambda ctx, name: name or "testbox"):
        yield box


def _invoke(args):
    result = CliRunner().invoke(nets_mod.nets, args, catch_exceptions=False)
    try:
        stderr = result.stderr
    except ValueError:
        stderr = ""
    return result, result.output + (stderr if stderr not in result.output else "")


# ---------------------------------------------------------------------------
# #513: the instruments the scanner detects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("device, role, channel", [
    (DP832, "power-supply", "3"),
    (E36312A, "power-supply", "2"),
    (USB202, "dac", "DAC1"),
    (USB202, "adc", "CH0"),
    (USB202, "gpio", "DIO3"),
    (PHIDGET, "thermocouple", "0"),
    (JLINK_BASE, "debug", "DEVICE_TYPE"),
], ids=lambda v: v["name"] if isinstance(v, dict) else v)
def test_nets_add_accepts_every_detected_instrument(fake_box, device, role, channel):
    result, output = _invoke(["add", "net1", role, channel, device["address"], "--box", "b"])
    assert result.exit_code == 0, output
    assert fake_box.saved_nets[0]["instrument"] == device["name"]


# ---------------------------------------------------------------------------
# #515: a U3's FIO0-FIO3 are refused as custom pins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pin", ["FIO0", "FIO3", "2"])
def test_nets_add_refuses_a_u3_analog_pin(fake_box, pin):
    result, output = _invoke(["add", "bus", "i2c", "custom", U3_ADDR, "--box", "b",
                              "--sda", pin, "--scl", "FIO7"])
    assert result.exit_code != 0
    assert "Invalid LabJack pin" in output
    assert "FIO4-FIO7" in output
    assert fake_box.saved_nets == []


def test_nets_add_accepts_a_u3_digital_pin(fake_box):
    result, output = _invoke(["add", "bus", "i2c", "custom", U3_ADDR, "--box", "b",
                              "--sda", "EIO0", "--scl", "FIO4"])
    assert result.exit_code == 0, output
    assert fake_box.saved_nets[0]["params"] == {"sda_pin": 8, "scl_pin": 4}


def test_a_t7_keeps_fio0_through_fio3():
    assert nets_mod._parse_labjack_pin("FIO0", "SDA", "LabJack_T7") == 0


# ---------------------------------------------------------------------------
# #516: add-batch carries params and refuses what it does not read
# ---------------------------------------------------------------------------

def _batch(tmp_path, records):
    path = tmp_path / "nets.json"
    path.write_text(json.dumps(records))
    return _invoke(["add-batch", str(path), "--box", "b"])


def test_batch_saves_custom_labjack_pins(fake_box, tmp_path):
    result, output = _batch(tmp_path, [
        {"name": "bus", "role": "i2c", "channel": "custom", "address": T7_ADDR,
         "instrument": "LabJack_T7", "params": {"sda_pin": "EIO0", "scl_pin": 9}},
        {"name": "flash", "role": "spi", "channel": "custom", "address": T7_ADDR,
         "instrument": "LabJack_T7",
         "params": {"clk_pin": "FIO1", "mosi_pin": "FIO2", "miso_pin": "FIO3"}},
    ])
    assert result.exit_code == 0, output
    saved = {n["name"]: n for n in fake_box.saved_nets}
    assert saved["bus"]["params"] == {"sda_pin": 8, "scl_pin": 9}
    assert saved["bus"]["pin"] == "SDA:EIO0 SCL:EIO1"
    assert saved["flash"]["params"] == {"clk_pin": 1, "mosi_pin": 2, "miso_pin": 3}


def test_batch_saves_an_ftdi_channel(fake_box, tmp_path):
    result, output = _batch(tmp_path, [
        {"name": "ctrl", "role": "gpio", "channel": "1", "address": FT4232H_ADDR,
         "instrument": "FTDI_FT4232H", "params": {"interface": "@c"}},
    ])
    assert result.exit_code == 0, output
    assert fake_box.saved_nets[0]["params"] == {"interface": "C"}


def test_a_batch_without_params_saves_what_it_did_before(fake_box, tmp_path):
    result, output = _batch(tmp_path, [
        {"name": "psu", "role": "power-supply", "channel": "1",
         "address": DP832["address"], "instrument": "Rigol_DP832"},
    ])
    assert result.exit_code == 0, output
    assert fake_box.saved_nets == [{
        "name": "psu", "role": "power-supply", "address": DP832["address"],
        "pin": "1", "instrument": "Rigol_DP832"}]


def test_a_uart_device_path_is_kept(fake_box, tmp_path):
    result, output = _batch(tmp_path, [
        {"name": "console", "role": "uart", "channel": "/dev/ttyUSB3",
         "address": "/dev/ttyUSB3", "instrument": "Unknown_UART_Device"},
    ])
    assert result.exit_code == 0, output
    assert fake_box.saved_nets[0]["device_path"] == "/dev/ttyUSB3"


@pytest.mark.parametrize("record, message", [
    ({"jlink_script": "abc"}, "unknown key(s) jlink_script"),
    ({"params": {"sda": "EIO0"}}, "unknown params key(s) sda"),
    ({"params": "EIO0"}, "'params' must be an object"),
    ({"params": {"sda_pin": "FIO0", "scl_pin": "FIO7"}, "address": U3_ADDR,
      "instrument": "LabJack_U3"}, "Invalid LabJack pin 'FIO0'"),
    ({"params": {"sda_pin": "EIO0"}}, "Missing pin option(s) for i2c"),
    ({"params": {"sda_pin": "EIO0", "scl_pin": "EIO1"}, "address": DP832["address"],
      "instrument": "Rigol_DP832"}, "only supported for LabJack nets"),
    ({"role": "thermocouple"}, "does not support net type 'thermocouple'"),
    ({"instrument": "LabJack"}, "No net types are defined"),
])
def test_a_bad_record_saves_nothing(fake_box, tmp_path, record, message):
    bad = {"name": "bad", "role": "i2c", "channel": "custom", "address": T7_ADDR,
           "instrument": "LabJack_T7", **record}
    good = {"name": "psu", "role": "power-supply", "channel": "1",
            "address": DP832["address"], "instrument": "Rigol_DP832"}
    result, output = _batch(tmp_path, [good, bad])
    assert result.exit_code == 1, output
    assert message in output
    assert "No nets were saved." in output
    assert fake_box.saved_nets == []


def test_every_bad_record_is_reported_at_once(fake_box, tmp_path):
    result, output = _batch(tmp_path, [
        {"name": "a", "role": "dac", "channel": "1", "address": DP832["address"],
         "instrument": "Rigol_DP832"},
        {"name": "b", "role": "power-supply", "channel": "1",
         "address": DP832["address"], "instrument": "Rigol_DP832", "extra": 1},
    ])
    assert result.exit_code == 1
    assert "Net 1 ('a')" in output and "Net 2 ('b')" in output


def test_an_ambiguous_address_is_refused(tmp_path):
    twin_a = dict(U3, serial=None)
    twin_b = dict(U3, serial=None)
    box = FakeBoxHTTP([twin_a, twin_b])
    with patch("requests.request", box.request), \
            patch.object(nets_mod, "_resolve_box", lambda ctx, name: name or "testbox"):
        result, output = _batch(tmp_path, [
            {"name": "led", "role": "gpio", "channel": "FIO4", "address": U3_ADDR,
             "instrument": "LabJack_U3"},
        ])
    assert result.exit_code == 1, output
    assert "No nets were saved." in output
    assert box.saved_nets == []

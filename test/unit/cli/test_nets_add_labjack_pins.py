#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for custom LabJack i2c/spi pin selection in ``lager nets add``
(cli/commands/box/nets.py).

The LabJack T7 can run its built-in I2C/SPI masters on any DIO pin, but the
scanner only ever advertised the hardcoded FIO4-FIO5 / FIO0-FIO3 channels.
``nets add`` now accepts ``--sda/--scl`` (i2c) and ``--cs/--sck/--mosi/--miso``
(spi) to pick arbitrary pins; the chosen pins are written to the net record's
``params`` dict (the path the box dispatchers already consume) plus a labeled
``pin`` summary for display.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

nets_mod = importlib.import_module('cli.commands.box.nets')
from cli.commands.box.nets import nets as nets_group  # noqa: E402


LABJACK_ADDR = "USB0::0x0CD5::0x0007::470012345::INSTR"
DP811_ADDR = "USB0::0x1AB1::0x0E11::DP8H123456::INSTR"

LABJACK = {
    "name": "LabJack_T7",
    "vid": "0cd5", "pid": "0007", "serial": "470012345",
    "address": LABJACK_ADDR,
    "net_type": ["gpio", "adc", "dac", "spi", "i2c"],
    "channels": {
        "gpio": ["FIO0", "FIO1", "FIO2", "FIO3", "FIO4", "FIO5", "EIO0", "EIO1"],
        "adc": ["AIN0", "AIN1"],
        "dac": ["DAC0", "DAC1"],
        "spi": ["FIO0-FIO3"],
        "i2c": ["FIO4-FIO5"],
    },
}
DP811 = {
    "name": "Rigol_DP811",
    "vid": "1ab1", "pid": "0e11", "serial": "DP8H123456",
    "address": DP811_ADDR,
    "net_type": ["power-supply"],
    "channels": {"power-supply": ["1"]},
}


from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402


@pytest.fixture
def fake_box():
    # nets.py talks to the box over :9000 HTTP (requests.request); replace
    # the box with the in-memory API fake.
    box = FakeBoxHTTP([LABJACK, DP811])
    with patch("requests.request", box.request), \
         patch("cli.box_storage.resolve_and_validate_box",
               lambda ctx, name: name or "testbox"), \
         patch.object(nets_mod, "_resolve_box", lambda ctx, name: name or "testbox"):
        yield box


def _invoke(args, input=None):
    result = CliRunner().invoke(nets_group, args, input=input, catch_exceptions=False)
    # Click >= 8.2 separates stderr; fold it into .output so assertions can
    # check user-facing text (errors, warnings) in one place.
    try:
        stderr = result.stderr
    except ValueError:
        stderr = ""
    if stderr and stderr not in result.output:
        result.output_bytes = (result.output + stderr).encode()
    return result


# --------------------------------------------------------------------------- #
# pin parsing helpers                                                          #
# --------------------------------------------------------------------------- #

class TestPinParsing:
    def test_pin_names_map_to_dio_numbers(self):
        assert nets_mod._parse_labjack_pin("FIO0", "SDA") == 0
        assert nets_mod._parse_labjack_pin("fio7", "SDA") == 7
        assert nets_mod._parse_labjack_pin("EIO0", "SDA") == 8
        assert nets_mod._parse_labjack_pin("EIO7", "SDA") == 15
        assert nets_mod._parse_labjack_pin("CIO0", "SDA") == 16
        assert nets_mod._parse_labjack_pin("CIO3", "SDA") == 19
        assert nets_mod._parse_labjack_pin("MIO0", "SDA") == 20
        assert nets_mod._parse_labjack_pin("MIO2", "SDA") == 22

    def test_raw_dio_numbers_accepted(self):
        assert nets_mod._parse_labjack_pin("0", "SDA") == 0
        assert nets_mod._parse_labjack_pin("22", "SDA") == 22

    def test_out_of_range_rejected(self):
        from cli.errors import LagerError
        for bad in ("FIO8", "EIO9", "CIO4", "MIO3", "23", "-1", "garbage"):
            with pytest.raises(LagerError):
                nets_mod._parse_labjack_pin(bad, "SDA")

    def test_roundtrip_names(self):
        for dio in range(23):
            name = nets_mod._labjack_pin_name(dio)
            assert nets_mod._parse_labjack_pin(name, "X") == dio


# --------------------------------------------------------------------------- #
# i2c custom pins                                                              #
# --------------------------------------------------------------------------- #

class TestI2CCustomPins:
    def test_default_channel_still_works(self, fake_box):
        result = _invoke(["add", "i2c1", "i2c", "FIO4-FIO5", LABJACK_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        rec = fake_box.saved_nets[0]
        assert rec["pin"] == "FIO4-FIO5"
        assert "params" not in rec

    def test_custom_pins_saved_as_params(self, fake_box):
        result = _invoke([
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "EIO0", "--scl", "EIO1",
        ])
        assert result.exit_code == 0, result.output
        rec = fake_box.saved_nets[0]
        assert rec["pin"] == "SDA:EIO0 SCL:EIO1"
        assert rec["params"] == {"sda_pin": 8, "scl_pin": 9}

    def test_numeric_pins_normalized_to_names(self, fake_box):
        result = _invoke([
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "8", "--scl", "9",
        ])
        assert result.exit_code == 0, result.output
        assert fake_box.saved_nets[0]["pin"] == "SDA:EIO0 SCL:EIO1"

    def test_missing_scl_rejected(self, fake_box):
        result = _invoke([
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "EIO0",
        ])
        assert result.exit_code != 0
        assert "--scl" in result.output
        assert fake_box.saved_nets == []

    def test_duplicate_pin_rejected(self, fake_box):
        result = _invoke([
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "EIO0", "--scl", "8",
        ])
        assert result.exit_code != 0
        assert "EIO0" in result.output
        assert fake_box.saved_nets == []

    def test_spi_options_rejected_on_i2c_net(self, fake_box):
        result = _invoke([
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "EIO0", "--scl", "EIO1", "--mosi", "EIO2",
        ])
        assert result.exit_code != 0
        assert "--mosi" in result.output
        assert fake_box.saved_nets == []


# --------------------------------------------------------------------------- #
# spi custom pins                                                              #
# --------------------------------------------------------------------------- #

class TestSPICustomPins:
    def test_default_channel_still_works(self, fake_box):
        result = _invoke(["add", "spi1", "spi", "FIO0-FIO3", LABJACK_ADDR, "--box", "b"])
        assert result.exit_code == 0, result.output
        rec = fake_box.saved_nets[0]
        assert rec["pin"] == "FIO0-FIO3"
        assert "params" not in rec

    def test_custom_pins_saved_as_params(self, fake_box):
        result = _invoke([
            "add", "flash", "spi", "custom", LABJACK_ADDR, "--box", "b",
            "--cs", "FIO6", "--sck", "FIO7", "--mosi", "EIO0", "--miso", "EIO1",
        ])
        assert result.exit_code == 0, result.output
        rec = fake_box.saved_nets[0]
        assert rec["pin"] == "CS:FIO6 SCK:FIO7 MOSI:EIO0 MISO:EIO1"
        # --sck maps to clk_pin: that's the key the box SPI dispatcher reads.
        assert rec["params"] == {
            "cs_pin": 6, "clk_pin": 7, "mosi_pin": 8, "miso_pin": 9,
        }

    def test_cs_optional_for_manual_cs_mode(self, fake_box):
        result = _invoke([
            "add", "flash", "spi", "custom", LABJACK_ADDR, "--box", "b",
            "--sck", "FIO7", "--mosi", "EIO0", "--miso", "EIO1",
        ])
        assert result.exit_code == 0, result.output
        rec = fake_box.saved_nets[0]
        assert rec["pin"] == "SCK:FIO7 MOSI:EIO0 MISO:EIO1"
        assert "cs_pin" not in rec["params"]
        assert rec["params"] == {"clk_pin": 7, "mosi_pin": 8, "miso_pin": 9}

    def test_missing_required_pin_rejected(self, fake_box):
        result = _invoke([
            "add", "flash", "spi", "custom", LABJACK_ADDR, "--box", "b",
            "--cs", "FIO6", "--sck", "FIO7",
        ])
        assert result.exit_code != 0
        assert "--miso" in result.output and "--mosi" in result.output
        assert fake_box.saved_nets == []


# --------------------------------------------------------------------------- #
# guard rails                                                                  #
# --------------------------------------------------------------------------- #

class TestGuards:
    def test_pin_options_rejected_for_non_labjack(self, fake_box):
        result = _invoke([
            "add", "psu", "power-supply", "1", DP811_ADDR, "--box", "b",
            "--sda", "FIO4",
        ])
        assert result.exit_code != 0
        assert "LabJack" in result.output
        assert fake_box.saved_nets == []

    def test_pin_options_rejected_for_gpio_role(self, fake_box):
        result = _invoke([
            "add", "g1", "gpio", "FIO0", LABJACK_ADDR, "--box", "b",
            "--sda", "FIO4", "--scl", "FIO5",
        ])
        assert result.exit_code != 0
        assert "i2c or spi" in result.output
        assert fake_box.saved_nets == []

    def test_overlap_with_saved_net_warns_but_saves(self, fake_box):
        fake_box.saved_nets.append({
            "name": "gpio_led", "role": "gpio", "instrument": "LabJack_T7",
            "pin": "EIO0", "address": LABJACK_ADDR,
        })
        result = _invoke([
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "EIO0", "--scl", "EIO1",
        ])
        assert result.exit_code == 0, result.output
        assert "EIO0" in result.output and "gpio_led" in result.output
        assert len(fake_box.saved_nets) == 2

    def test_overlap_with_default_spi_net_warns(self, fake_box):
        fake_box.saved_nets.append({
            "name": "spi1", "role": "spi", "instrument": "LabJack_T7",
            "pin": "FIO0-FIO3", "address": LABJACK_ADDR,
        })
        result = _invoke([
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "FIO0", "--scl", "FIO4",
        ])
        assert result.exit_code == 0, result.output
        assert "spi1" in result.output
        assert len(fake_box.saved_nets) == 2

    def test_duplicate_custom_net_blocked(self, fake_box):
        args = [
            "add", "mybus", "i2c", "custom", LABJACK_ADDR, "--box", "b",
            "--sda", "EIO0", "--scl", "EIO1",
        ]
        result = _invoke(args)
        assert result.exit_code == 0, result.output
        args[1] = "mybus2"
        result = _invoke(args)
        assert result.exit_code != 0
        assert "already exists" in result.output
        assert len(fake_box.saved_nets) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

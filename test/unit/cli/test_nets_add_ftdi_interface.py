#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""``lager nets add --interface``: the FTDI channel of a gpio, i2c or spi net.

The box drivers take the channel from ``params.interface``, but no CLI command
wrote it, so every FTDI net landed on channel A. ``--interface`` writes it and
refuses a channel the part does not have for that net type, at add time rather
than when a command first opens the net.

Two identity rules change with it, and both are pinned here:

* the channel is part of a gpio/i2c/spi net's identity, so pin 5 on channel A
  and pin 5 on channel B are two nets, not a duplicate;
* a second ``debug`` net on another channel suffix (``@B``) is allowed, the
  rule that ``add-all`` and the TUI already applied.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, REPO)

nets_mod = importlib.import_module('cli.commands.box.nets')
from cli.commands.box.nets import nets as nets_group  # noqa: E402
from test.unit.cli.nets_http_fake import FakeBoxHTTP  # noqa: E402


FT232H_ADDR = "USB0::0x0403::0x6014::FT1ABCD::INSTR"
FT2232H_ADDR = "USB0::0x0403::0x6010::FT2ABCD::INSTR"
FT4232H_ADDR = "USB0::0x0403::0x6011::FT4ABCD::INSTR"
DP811_ADDR = "USB0::0x1AB1::0x0E11::DP8H123456::INSTR"

# Channel lists as box/lager/http_handlers/usb_scanner.py advertises them.
FT232H = {
    "name": "FTDI_FT232H", "vid": "0403", "pid": "6014", "serial": "FT1ABCD",
    "address": FT232H_ADDR,
    "net_type": ["spi", "i2c", "gpio", "debug", "uart"],
    "channels": {"spi": ["SPI0"], "i2c": ["I2C0"],
                 "gpio": [str(n) for n in range(4, 16)],
                 "debug": ["DEVICE_TYPE"], "uart": []},
}
FT2232H = {
    "name": "FTDI_FT2232H", "vid": "0403", "pid": "6010", "serial": "FT2ABCD",
    "address": FT2232H_ADDR,
    "net_type": ["spi", "i2c", "gpio", "debug", "uart"],
    "channels": {"spi": ["SPI0"], "i2c": ["I2C0"],
                 "gpio": [str(n) for n in range(4, 16)],
                 "debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"], "uart": []},
}
FT4232H = {
    "name": "FTDI_FT4232H", "vid": "0403", "pid": "6011", "serial": "FT4ABCD",
    "address": FT4232H_ADDR,
    "net_type": ["debug", "uart", "spi", "i2c", "gpio"],
    "channels": {"debug": ["DEVICE_TYPE@A", "DEVICE_TYPE@B"], "uart": [],
                 "spi": ["SPI0"], "i2c": ["I2C0"],
                 "gpio": [str(n) for n in range(8)]},
}
DP811 = {
    "name": "Rigol_DP811", "vid": "1ab1", "pid": "0e11", "serial": "DP8H123456",
    "address": DP811_ADDR,
    "net_type": ["power-supply"],
    "channels": {"power-supply": ["1"]},
}


@pytest.fixture
def fake_box():
    box = FakeBoxHTTP([FT232H, FT2232H, FT4232H, DP811])
    with patch("requests.request", box.request), \
         patch("cli.box_storage.resolve_and_validate_box",
               lambda ctx, name: name or "testbox"), \
         patch.object(nets_mod, "_resolve_box", lambda ctx, name: name or "testbox"):
        yield box


def _invoke(args):
    return CliRunner().invoke(nets_group, args, catch_exceptions=False)


def _text(result) -> str:
    """stdout and stderr together, on every click version the CLI allows."""
    try:
        err = result.stderr
    except ValueError:      # click < 8.2 folds stderr into output already
        err = ""
    return result.output + err


def _add(*args):
    return _invoke(["add", *args, "--box", "b"])


class TestInterfaceIsSaved:
    def test_i2c_on_channel_b(self, fake_box):
        result = _add("sensors", "i2c", "I2C0", FT4232H_ADDR, "--interface", "b")
        assert result.exit_code == 0, _text(result)
        assert fake_box.saved_nets[0]["params"] == {"interface": "B"}

    def test_gpio_on_channel_c_of_an_ft4232h(self, fake_box):
        result = _add("reset_line", "gpio", "5", FT4232H_ADDR, "--interface", "C")
        assert result.exit_code == 0, _text(result)
        assert fake_box.saved_nets[0]["params"] == {"interface": "C"}

    def test_spi_on_channel_b_of_an_ft2232h(self, fake_box):
        result = _add("flash", "spi", "SPI0", FT2232H_ADDR, "--interface", "B")
        assert result.exit_code == 0, _text(result)
        assert fake_box.saved_nets[0]["params"] == {"interface": "B"}

    def test_without_interface_no_params_are_saved(self, fake_box):
        result = _add("sensors", "i2c", "I2C0", FT4232H_ADDR)
        assert result.exit_code == 0, _text(result)
        assert "params" not in fake_box.saved_nets[0]


class TestInterfaceIsRefused:
    def test_i2c_on_a_channel_without_mpsse(self, fake_box):
        result = _add("sensors", "i2c", "I2C0", FT4232H_ADDR, "--interface", "C")
        assert result.exit_code != 0
        assert "MPSSE" in _text(result)
        assert "Channels for i2c: A, B" in _text(result)
        assert fake_box.saved_nets == []

    def test_a_channel_a_single_channel_part_does_not_have(self, fake_box):
        result = _add("g", "gpio", "4", FT232H_ADDR, "--interface", "B")
        assert result.exit_code != 0
        assert "Channels for gpio: A" in _text(result)
        assert fake_box.saved_nets == []

    def test_channel_c_on_an_ft2232h(self, fake_box):
        result = _add("g", "gpio", "4", FT2232H_ADDR, "--interface", "C")
        assert result.exit_code != 0
        assert "Channels for gpio: A, B" in _text(result)
        assert fake_box.saved_nets == []

    def test_an_instrument_that_is_not_ftdi(self, fake_box):
        result = _add("psu", "power-supply", "1", DP811_ADDR, "--interface", "A")
        assert result.exit_code != 0
        assert "applies only to FTDI" in _text(result)
        assert fake_box.saved_nets == []

    def test_a_debug_net_is_pointed_at_the_device_suffix(self, fake_box):
        result = _add("dbg", "debug", "STM32F4x", FT4232H_ADDR, "--interface", "B")
        assert result.exit_code != 0
        assert "STM32F4x@B" in _text(result)
        assert fake_box.saved_nets == []

    def test_an_unknown_letter_is_a_usage_error(self, fake_box):
        result = _add("g", "gpio", "4", FT4232H_ADDR, "--interface", "E")
        assert result.exit_code == 2
        assert fake_box.saved_nets == []


class TestChannelIsPartOfTheIdentity:
    def test_the_same_pin_on_two_channels_is_two_nets(self, fake_box):
        assert _add("line_a", "gpio", "5", FT4232H_ADDR, "--interface", "A").exit_code == 0
        result = _add("line_b", "gpio", "5", FT4232H_ADDR, "--interface", "B")
        assert result.exit_code == 0, _text(result)
        assert len(fake_box.saved_nets) == 2

    def test_the_same_pin_on_the_same_channel_is_a_duplicate(self, fake_box):
        assert _add("line_1", "gpio", "5", FT4232H_ADDR, "--interface", "B").exit_code == 0
        result = _add("line_2", "gpio", "5", FT4232H_ADDR, "--interface", "b")
        assert result.exit_code != 0
        assert "already exists" in _text(result)
        assert len(fake_box.saved_nets) == 1

    def test_no_interface_is_channel_a(self, fake_box):
        assert _add("line_1", "gpio", "5", FT4232H_ADDR).exit_code == 0
        result = _add("line_2", "gpio", "5", FT4232H_ADDR, "--interface", "A")
        assert result.exit_code != 0
        assert "already exists" in _text(result)

    def test_a_numeric_interface_saved_by_hand_matches_its_letter(self, fake_box):
        fake_box.saved_nets.append({
            "name": "old_line", "role": "gpio", "instrument": "FTDI_FT4232H",
            "pin": "5", "address": FT4232H_ADDR, "params": {"interface": 1},
        })
        result = _add("new_line", "gpio", "5", FT4232H_ADDR, "--interface", "B")
        assert result.exit_code != 0
        assert "already exists" in _text(result)

    def test_other_instruments_keep_the_old_duplicate_rule(self, fake_box):
        assert _add("psu", "power-supply", "1", DP811_ADDR).exit_code == 0
        result = _add("psu2", "power-supply", "1", DP811_ADDR)
        assert result.exit_code != 0
        assert "already exists" in _text(result)


class TestDebugNetPerChannel:
    def test_a_second_debug_net_on_another_channel(self, fake_box):
        assert _add("dbg_a", "debug", "STM32F4x@A", FT2232H_ADDR).exit_code == 0
        result = _add("dbg_b", "debug", "NRF52840_XXAA@B", FT2232H_ADDR)
        assert result.exit_code == 0, _text(result)
        assert len(fake_box.saved_nets) == 2

    def test_a_second_debug_net_on_the_same_channel(self, fake_box):
        assert _add("dbg_a", "debug", "STM32F4x@A", FT2232H_ADDR).exit_code == 0
        result = _add("dbg_a2", "debug", "STM32F1x@A", FT2232H_ADDR)
        assert result.exit_code != 0
        assert "A debug net already exists" in _text(result)
        assert len(fake_box.saved_nets) == 1

    def test_a_probe_without_channels_keeps_one_debug_net(self, fake_box):
        assert _add("dbg", "debug", "STM32F4x", FT232H_ADDR).exit_code == 0
        result = _add("dbg2", "debug", "STM32F1x", FT232H_ADDR)
        assert result.exit_code != 0
        assert "A debug net already exists" in _text(result)


class TestTablesAgreeWithTheBox:
    """The CLI's channel tables must say what the box drivers enforce."""

    @staticmethod
    def _ftdi_url():
        path = os.path.join(REPO, "box", "lager", "util", "ftdi_url.py")
        spec = importlib.util.spec_from_file_location("_ftdi_url_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_channel_counts_match(self):
        box = self._ftdi_url()
        products = {"FTDI_FT232H": "232h", "FTDI_FT2232H": "2232h",
                    "FTDI_FT4232H": "4232h"}
        assert set(nets_mod._FTDI_CHANNELS) == set(products)
        for part, product in products.items():
            assert len(nets_mod._FTDI_CHANNELS[part]) == box.channel_count(product)
            assert (len(nets_mod._FTDI_MPSSE_CHANNELS[part])
                    == box.mpsse_channel_count(product))
            assert box.is_ftdi_instrument(part)

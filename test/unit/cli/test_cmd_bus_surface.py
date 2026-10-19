# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
`lager i2c` and `lager spi`: argument parsing and the request each sends.

COVERAGE.md listed `spi.py` (~770 lines) and `i2c.py` (~560) as smoke-only:
the gate checked that each posts to :9000 and nothing about what it posts.
The parsing here is where a user's bytes become the bytes on the bus -- an
address, a hex string, a frequency suffix -- so a regression is a wrong
transaction on real hardware, not an error message.
"""

import pytest


@pytest.fixture
def bus(fake_box):
    fake_box.net("i2c1", "i2c").net("spi1", "spi")
    return fake_box


def _net_command(box):
    url, body = box.posts[-1]
    assert url == f"http://{box.IP}:9000/net/command"
    return body


# ---------------------------------------------------------------------------
# i2c
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("freq, hz", [("100k", 100_000), ("400k", 400_000),
                                      ("1M", 1_000_000), ("100000", 100_000)])
def test_i2c_config_parses_the_frequency_suffix(lager, bus, freq, hz):
    result = lager("i2c", "i2c1", "config", "--frequency", freq)
    assert result.exit_code == 0, result.output
    assert _net_command(bus) == {"netname": "i2c1", "action": "config",
                                 "params": {"frequency_hz": hz}, "role": "i2c"}


def test_i2c_config_rejects_a_garbled_frequency(lager, bus):
    result = lager("i2c", "i2c1", "config", "--frequency", "9x")
    assert result.exit_code == 2
    assert "Invalid frequency '9x'" in result.output
    assert bus.posts == []


def test_i2c_scan_defaults_to_the_non_reserved_7_bit_range(lager, bus):
    result = lager("i2c", "i2c1", "scan")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"] == {"start_addr": 0x08, "end_addr": 0x77}


def test_i2c_scan_accepts_a_hex_range(lager, bus):
    result = lager("i2c", "i2c1", "scan", "--start", "0x10", "--end", "0x20")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"] == {"start_addr": 0x10, "end_addr": 0x20}


def test_i2c_read_sends_address_and_length(lager, bus):
    result = lager("i2c", "i2c1", "read", "4", "--address", "0x48")
    assert result.exit_code == 0, result.output
    assert _net_command(bus) == {"netname": "i2c1", "action": "read",
                                 "params": {"address": 0x48, "num_bytes": 4, "overrides": None},
                                 "role": "i2c"}


def test_i2c_read_frequency_override_is_per_call(lager, bus):
    result = lager("i2c", "i2c1", "read", "2", "--address", "0x48", "--frequency", "400k")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"]["overrides"] == {"frequency_hz": 400_000}


def test_i2c_read_requires_an_address(lager, bus):
    result = lager("i2c", "i2c1", "read", "4")
    assert result.exit_code == 2
    assert "--address" in result.output
    assert bus.posts == []


def test_i2c_write_splits_a_hex_string_into_bytes(lager, bus):
    result = lager("i2c", "i2c1", "write", "0x0A03", "--address", "0x48")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"] == {"address": 0x48, "data": [0x0A, 0x03],
                                           "overrides": None}


@pytest.mark.parametrize("data", ["0a 03", "0a,03", "0x0a 0x03"])
def test_i2c_write_accepts_separated_bytes(lager, bus, data):
    result = lager("i2c", "i2c1", "write", data, "--address", "0x48")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"]["data"] == [0x0A, 0x03]


@pytest.mark.parametrize("address", ["0x80", "0xFF", "zz"])
def test_i2c_write_refuses_an_address_outside_7_bits(lager, bus, address):
    result = lager("i2c", "i2c1", "write", "0x01", "--address", address)
    assert result.exit_code == 2
    assert bus.posts == []


def test_i2c_write_refuses_bad_hex(lager, bus):
    result = lager("i2c", "i2c1", "write", "zz", "--address", "0x48")
    assert result.exit_code == 2
    assert "Invalid hex value 'zz'" in result.output
    assert bus.posts == []


def test_i2c_transfer_sends_write_then_read_length(lager, bus):
    result = lager("i2c", "i2c1", "transfer", "2", "--address", "0x48", "--data", "0x01")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"] == {"address": 0x48, "num_bytes": 2,
                                           "data": [0x01], "overrides": None}


def test_i2c_read_prints_the_bytes_the_box_returned(lager, bus):
    bus.answer = lambda url, body: {"success": True, "value": [0x12, 0xAB]}
    result = lager("i2c", "i2c1", "read", "2", "--address", "0x48")
    assert result.exit_code == 0, result.output
    assert "12" in result.output and "ab" in result.output.lower()


def test_i2c_box_error_is_a_non_zero_exit(lager, bus):
    bus.status = 500
    bus.answer = lambda url, body: {"success": False, "error": "NACK at 0x48"}
    result = lager("i2c", "i2c1", "read", "1", "--address", "0x48")
    assert result.exit_code != 0
    assert "NACK at 0x48" in result.output


# ---------------------------------------------------------------------------
# spi
# ---------------------------------------------------------------------------

def test_spi_config_payload(lager, bus):
    result = lager("spi", "spi1", "config", "--mode", "1", "--frequency", "1M")
    assert result.exit_code == 0, result.output
    assert _net_command(bus) == {"netname": "spi1", "action": "config",
                                 "params": {"mode": 1, "frequency_hz": 1_000_000},
                                 "role": "spi"}


def test_spi_config_rejects_a_garbled_frequency(lager, bus):
    result = lager("spi", "spi1", "config", "--frequency", "abc")
    assert result.exit_code == 2
    assert bus.posts == []


def test_spi_read_clocks_fill_bytes(lager, bus):
    result = lager("spi", "spi1", "read", "4")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"] == {"n_words": 4, "fill": 0xFF,
                                           "keep_cs": False, "overrides": None}


def test_spi_write_is_a_full_duplex_read_write(lager, bus):
    result = lager("spi", "spi1", "write", "0x9f 0x01")
    assert result.exit_code == 0, result.output
    body = _net_command(bus)
    assert body["action"] == "read_write"
    assert body["params"]["data"] == [0x9F, 0x01]


def test_spi_write_without_a_word_size_keeps_a_run_of_hex_as_one_word(lager, bus):
    # Documented in _parse_hex_data: byte splitting happens only for an
    # explicit 8-bit word size; otherwise the box validates the width.
    result = lager("spi", "spi1", "write", "0x9f01")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"]["data"] == [0x9F01]


def test_spi_write_with_8_bit_words_splits_bytes(lager, bus):
    result = lager("spi", "spi1", "write", "0x9f01", "--word-size", "8")
    assert result.exit_code == 0, result.output
    params = _net_command(bus)["params"]
    assert params["data"] == [0x9F, 0x01]
    assert params["overrides"] == {"word_size": 8}


def test_spi_write_refuses_a_word_wider_than_the_word_size(lager, bus):
    result = lager("spi", "spi1", "write", "0x1ff 0x01", "--word-size", "8")
    assert result.exit_code == 2
    assert "exceeds 8-bit range" in result.output
    assert bus.posts == []


def test_spi_write_refuses_bad_hex(lager, bus):
    result = lager("spi", "spi1", "write", "xyz")
    assert result.exit_code == 2
    assert bus.posts == []


def test_spi_transfer_payload(lager, bus):
    result = lager("spi", "spi1", "transfer", "4", "--data", "0x9f")
    assert result.exit_code == 0, result.output
    assert _net_command(bus)["params"] == {"n_words": 4, "data": [0x9F], "fill": 0xFF,
                                           "keep_cs": False, "overrides": None}


def test_spi_per_call_overrides_reach_the_box(lager, bus):
    result = lager("spi", "spi1", "write", "0x01", "--mode", "3", "--bit-order", "lsb",
                   "--cs-active", "high", "--keep-cs")
    assert result.exit_code == 0, result.output
    params = _net_command(bus)["params"]
    assert params["keep_cs"] is True
    assert params["overrides"] == {"mode": 3, "bit_order": "lsb", "cs_active": "high"}

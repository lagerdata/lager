# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""cli.simple_hdlc: the CRC-16/CCITT-FALSE checksum and the HDLC framing.

The expected CRC values were computed with the PyCRC ``CRCCCITT("FFFF")``
implementation that this module used before it switched to
``binascii.crc_hqx``, so these tests pin the existing wire format.
"""

import pytest

from cli.simple_hdlc import HDLC, calcCRC


@pytest.mark.parametrize("data, expected", [
    (b"", 0xFFFF),
    (b"123456789", 0x29B1),  # the published CRC-16/CCITT-FALSE check value
    (b"\x7e\x7d\x00\xff", 0xE046),
    (b"lager", 0x186C),
    (bytes(range(32)), 0x23B3),
])
def test_calc_crc_matches_previous_implementation(data, expected):
    assert calcCRC(data) == bytearray(expected.to_bytes(2, "big"))


def test_calc_crc_accepts_bytearray_and_int_list():
    assert calcCRC(bytearray(b"lager")) == calcCRC(b"lager")
    assert calcCRC(list(b"lager")) == calcCRC(b"lager")


def _decode(encoded):
    frames, errors = [], []
    hdlc = HDLC()
    hdlc.frame_callback = frames.append
    hdlc.error_callback = errors.append
    for byte in encoded:
        hdlc._readByte(byte)
    return frames, errors


@pytest.mark.parametrize("payload", [b"lager", b"\x7e\x7d\x7e", bytes(range(256))])
def test_encode_then_decode_round_trips(payload):
    frames, errors = _decode(HDLC._encode(payload))
    assert frames == [payload]
    assert errors == []


def test_encode_escapes_flag_and_escape_bytes():
    encoded = HDLC._encode(b"\x7e\x7d")
    assert encoded[0] == 0x7E and encoded[-1] == 0x7E
    assert 0x7E not in encoded[1:-1]
    assert encoded[1:5] == b"\x7d\x5e\x7d\x5d"


def test_corrupted_crc_reports_an_error_frame():
    encoded = bytearray(HDLC._encode(b"lager"))
    encoded[-2] ^= 0x01  # flip one bit of the last CRC byte (0x6C, not escaped)
    frames, errors = _decode(bytes(encoded))
    assert frames == []
    assert errors == [b"lager"]

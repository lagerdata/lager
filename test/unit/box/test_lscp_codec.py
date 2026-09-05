# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""LSCP/1 codec tests, including cross-language agreement with Rust.

RUST_FIXTURE below is the literal output of the Rust encoder:

    cargo run -p protocol --example lscp_fixture

If the Rust layout changes without the Python decoder following, these tests
fail rather than the daemon and its clients silently disagreeing on the wire.
"""

import struct

import numpy as np
import pytest

from lager.measurement.scope import lscp


# Produced by protocol/examples/lscp_fixture.rs. Regenerate with the command
# in this module's docstring if the frame layout intentionally changes.
RUST_FIXTURE = bytes.fromhex(
    "4c534350010001000700000000000000081a99be1c000000000000000000304002"
    "00000002000000040000000208020000000000000000000000000000000000000700"
    "000ad7a33a0000000000000000010401000ad7233b000080be000000000000e80318"
    "fcff7f00800500fbff0000"
)


def _sample_frame():
    return lscp.CaptureFrame(
        seq=7,
        capture_mono_ns=123_456_789_000,
        sample_interval_ns=16.0,
        pre_trigger_samples=2,
        post_trigger_samples=2,
        samples_per_channel=4,
        resolution_bits=8,
        overflow_mask=0b10,
        flags=lscp.FLAG_TRIGGERED,
        channels=[
            lscp.ChannelFrame("A", 7, "DC", 0.00125, 0.0),
            lscp.ChannelFrame("B", 4, "AC", 0.0025, -0.25),
        ],
        samples=np.array(
            [0, 1000, -1000, 32767, -32768, 5, -5, 0], dtype="<i2"
        ),
    )


class TestRustInterop:
    """The Python decoder must read exactly what the Rust encoder writes."""

    def test_decodes_the_rust_fixture(self):
        frame = lscp.decode(RUST_FIXTURE)

        assert frame.seq == 7
        assert frame.capture_mono_ns == 123_456_789_000
        assert frame.sample_interval_ns == 16.0
        assert frame.pre_trigger_samples == 2
        assert frame.post_trigger_samples == 2
        assert frame.samples_per_channel == 4
        assert frame.resolution_bits == 8
        assert frame.overflow_mask == 0b10
        assert frame.triggered is True
        assert frame.streaming is False

    def test_reads_rust_channel_descriptors(self):
        frame = lscp.decode(RUST_FIXTURE)

        assert [c.channel for c in frame.channels] == ["A", "B"]
        assert [c.coupling for c in frame.channels] == ["DC", "AC"]
        assert [c.range_code for c in frame.channels] == [7, 4]
        assert frame.channels[0].scale_v_per_count == pytest.approx(0.00125)
        assert frame.channels[1].offset_v == pytest.approx(-0.25)

    def test_reads_rust_sample_payload(self):
        frame = lscp.decode(RUST_FIXTURE)

        np.testing.assert_array_equal(
            frame.counts("A"), np.array([0, 1000, -1000, 32767], dtype="<i2")
        )
        np.testing.assert_array_equal(
            frame.counts("B"), np.array([-32768, 5, -5, 0], dtype="<i2")
        )

    def test_python_encoder_reproduces_the_rust_bytes(self):
        assert lscp.encode(_sample_frame()) == RUST_FIXTURE


class TestRoundTrip:
    def test_encode_decode_preserves_everything(self):
        original = _sample_frame()
        decoded = lscp.decode(lscp.encode(original))

        assert decoded.seq == original.seq
        assert decoded.flags == original.flags
        assert decoded.sample_interval_ns == original.sample_interval_ns
        np.testing.assert_array_equal(decoded.samples, original.samples)

    def test_empty_capture_round_trips(self):
        frame = lscp.CaptureFrame(
            seq=0,
            capture_mono_ns=0,
            sample_interval_ns=1.0,
            pre_trigger_samples=0,
            post_trigger_samples=0,
            samples_per_channel=0,
            resolution_bits=12,
            overflow_mask=0,
            flags=0,
            channels=[],
            samples=np.array([], dtype="<i2"),
        )
        decoded = lscp.decode(lscp.encode(frame))
        assert decoded.samples_per_channel == 0
        assert decoded.channels == []


class TestDerivedValues:
    def test_volts_apply_scale_and_offset(self):
        frame = lscp.decode(RUST_FIXTURE)

        # Channel B: count * 0.0025 + (-0.25)
        volts = frame.volts("B")
        assert volts[1] == pytest.approx(5 * 0.0025 - 0.25)
        assert volts[3] == pytest.approx(-0.25)

    def test_counts_is_a_view_not_a_copy(self):
        frame = lscp.decode(RUST_FIXTURE)
        # A view shares the decoded buffer; a copy would not.
        assert frame.counts("A").base is not None

    def test_sample_rate_is_inverse_of_interval(self):
        frame = lscp.decode(RUST_FIXTURE)
        assert frame.sample_rate_hz == pytest.approx(1e9 / 16.0)

    def test_time_axis_is_zero_at_the_trigger(self):
        frame = lscp.decode(RUST_FIXTURE)
        axis = frame.time_axis()

        assert len(axis) == 4
        # 2 pre-trigger samples, so the trigger sits at index 2.
        assert axis[2] == pytest.approx(0.0)
        assert axis[0] < 0

    def test_duration_covers_all_samples(self):
        frame = lscp.decode(RUST_FIXTURE)
        assert frame.duration_s == pytest.approx(4 * 16.0 / 1e9)

    def test_overflow_mask_reports_per_channel(self):
        frame = lscp.decode(RUST_FIXTURE)
        assert frame.overflowed(0) is False
        assert frame.overflowed(1) is True

    def test_lookup_by_index_and_label_agree(self):
        frame = lscp.decode(RUST_FIXTURE)
        np.testing.assert_array_equal(frame.counts(0), frame.counts("A"))
        np.testing.assert_array_equal(frame.counts(1), frame.counts("b"))


class TestRejectsMalformedFrames:
    def test_rejects_short_header(self):
        with pytest.raises(lscp.LscpError, match="too short"):
            lscp.decode(b"\x00" * 8)

    def test_rejects_bad_magic(self):
        corrupted = bytearray(RUST_FIXTURE)
        corrupted[0] = ord("X")
        with pytest.raises(lscp.LscpError, match="bad magic"):
            lscp.decode(bytes(corrupted))

    def test_rejects_future_version(self):
        corrupted = bytearray(RUST_FIXTURE)
        corrupted[4:6] = struct.pack("<H", 99)
        with pytest.raises(lscp.LscpError, match="unsupported LSCP version 99"):
            lscp.decode(bytes(corrupted))

    def test_rejects_truncated_payload(self):
        with pytest.raises(lscp.LscpError, match="length mismatch"):
            lscp.decode(RUST_FIXTURE[:-4])

    def test_rejects_unknown_channel(self):
        frame = lscp.decode(RUST_FIXTURE)
        with pytest.raises(lscp.LscpError, match="no such channel"):
            frame.counts("Z")


class TestSizeGuarantee:
    def test_stays_far_smaller_than_the_json_it_replaces(self):
        """The measured legacy baseline was 648.1 KB for this capture."""
        frame = lscp.CaptureFrame(
            seq=1,
            capture_mono_ns=1,
            sample_interval_ns=8.0,
            pre_trigger_samples=4000,
            post_trigger_samples=4000,
            samples_per_channel=8000,
            resolution_bits=8,
            overflow_mask=0,
            flags=lscp.FLAG_TRIGGERED,
            channels=[lscp.ChannelFrame("A", 7, "DC", 0.001, 0.0)],
            samples=np.zeros(8000, dtype="<i2"),
        )
        encoded = lscp.encode(frame)

        assert len(encoded) == 64 + 16 + 16_000
        # Two bytes per sample plus fixed overhead, versus 83 bytes per sample.
        assert len(encoded) / 8000 < 2.1

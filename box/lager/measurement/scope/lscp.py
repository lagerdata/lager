# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Decoder for LSCP/1 oscilloscope capture frames.

Mirrors ``protocol/src/lscp.rs``. The two are covered by a round-trip test
(``test/unit/box/test_lscp_codec.py``) that decodes fixtures the Rust encoder
produced, so the formats cannot drift apart silently.

Sample data stays as raw ADC counts in a numpy view over the original buffer.
Nothing is copied and nothing is converted to volts unless a caller asks, which
is what keeps a capture cheap to receive: the format this replaced cost 83
bytes and ~37 ms of parsing per capture.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy ships in the box image
    np = None


MAGIC = 0x5043534C  # "LSCP" little-endian
VERSION = 1
HEADER_SIZE = 64
CHANNEL_DESC_SIZE = 16

FLAG_TRIGGERED = 1 << 0
FLAG_STREAMING = 1 << 1

_HEADER = struct.Struct("<IHHQQdIIIBBH")
_CHANNEL = struct.Struct("<BBBBffI")

_COUPLING = {0: "DC", 1: "AC", 2: "GND"}


class LscpError(ValueError):
    """Frame could not be decoded."""


@dataclass
class ChannelFrame:
    channel: str
    range_code: int
    coupling: str
    scale_v_per_count: float
    offset_v: float


@dataclass
class CaptureFrame:
    seq: int
    capture_mono_ns: int
    sample_interval_ns: float
    pre_trigger_samples: int
    post_trigger_samples: int
    samples_per_channel: int
    resolution_bits: int
    overflow_mask: int
    flags: int
    channels: List[ChannelFrame]
    # int16 counts, channel-major. A view over the source buffer, not a copy.
    samples: "np.ndarray"

    @property
    def triggered(self) -> bool:
        return bool(self.flags & FLAG_TRIGGERED)

    @property
    def streaming(self) -> bool:
        return bool(self.flags & FLAG_STREAMING)

    @property
    def sample_rate_hz(self) -> float:
        if self.sample_interval_ns <= 0:
            return 0.0
        return 1e9 / self.sample_interval_ns

    @property
    def duration_s(self) -> float:
        return self.samples_per_channel * self.sample_interval_ns / 1e9

    def channel_index(self, label: str) -> Optional[int]:
        for i, channel in enumerate(self.channels):
            if channel.channel == label.upper():
                return i
        return None

    def counts(self, channel) -> "np.ndarray":
        """Raw ADC counts for a channel, by label or index. No copy."""
        index = channel if isinstance(channel, int) else self.channel_index(channel)
        if index is None or index >= len(self.channels):
            raise LscpError(f"no such channel: {channel!r}")
        n = self.samples_per_channel
        return self.samples[index * n:(index + 1) * n]

    def volts(self, channel) -> "np.ndarray":
        """Channel converted to volts. Allocates, unlike :meth:`counts`."""
        index = channel if isinstance(channel, int) else self.channel_index(channel)
        if index is None or index >= len(self.channels):
            raise LscpError(f"no such channel: {channel!r}")
        descriptor = self.channels[index]
        raw = self.counts(index)
        return raw.astype(np.float64) * descriptor.scale_v_per_count + descriptor.offset_v

    def time_axis(self) -> "np.ndarray":
        """Seconds relative to the trigger, so t=0 is the trigger point."""
        interval_s = self.sample_interval_ns / 1e9
        start = -self.pre_trigger_samples * interval_s
        return start + np.arange(self.samples_per_channel, dtype=np.float64) * interval_s

    def overflowed(self, index: int) -> bool:
        """Whether a channel clipped during this capture."""
        return bool(self.overflow_mask & (1 << index))


def peek_seq(buf: bytes) -> int:
    """Read a frame's sequence number without decoding its samples.

    Lets a caller decide whether a frame is the one it asked for before
    paying to build arrays over it, which matters on a subscribed connection
    where most arriving frames are broadcasts the caller will discard.
    """
    if len(buf) < HEADER_SIZE:
        raise LscpError(f"frame too short: need {HEADER_SIZE} bytes, got {len(buf)}")
    magic, version, _flags, seq = _HEADER.unpack_from(buf, 0)[:4]
    if magic != MAGIC:
        raise LscpError(f"bad magic 0x{magic:08x}, expected 0x{MAGIC:08x}")
    if version != VERSION:
        raise LscpError(f"unsupported LSCP version {version}, expected {VERSION}")
    return seq


def decode(buf: bytes) -> CaptureFrame:
    """Decode one LSCP frame. Raises :class:`LscpError` on malformed input."""
    if np is None:  # pragma: no cover
        raise LscpError("numpy is required to decode LSCP frames")

    if len(buf) < HEADER_SIZE:
        raise LscpError(f"frame too short: need {HEADER_SIZE} bytes, got {len(buf)}")

    (
        magic,
        version,
        flags,
        seq,
        capture_mono_ns,
        sample_interval_ns,
        pre_trigger,
        post_trigger,
        samples_per_channel,
        channel_count,
        resolution_bits,
        overflow_mask,
    ) = _HEADER.unpack_from(buf, 0)

    if magic != MAGIC:
        raise LscpError(f"bad magic 0x{magic:08x}, expected 0x{MAGIC:08x}")
    if version != VERSION:
        raise LscpError(
            f"unsupported LSCP version {version}, this build speaks {VERSION}"
        )

    descriptors_end = HEADER_SIZE + channel_count * CHANNEL_DESC_SIZE
    if len(buf) < descriptors_end:
        raise LscpError(
            f"frame too short: need {descriptors_end} bytes, got {len(buf)}"
        )

    channels = []
    for i in range(channel_count):
        (
            channel_id,
            range_code,
            coupling,
            _reserved,
            scale,
            offset,
            _pad,
        ) = _CHANNEL.unpack_from(buf, HEADER_SIZE + i * CHANNEL_DESC_SIZE)
        channels.append(
            ChannelFrame(
                channel=chr(ord("A") + channel_id),
                range_code=range_code,
                coupling=_COUPLING.get(coupling, "DC"),
                scale_v_per_count=scale,
                offset_v=offset,
            )
        )

    expected = samples_per_channel * channel_count * 2
    payload = buf[descriptors_end:]
    if len(payload) != expected:
        raise LscpError(
            f"payload length mismatch: expected {expected} bytes, got {len(payload)}"
        )

    samples = np.frombuffer(payload, dtype="<i2")

    return CaptureFrame(
        seq=seq,
        capture_mono_ns=capture_mono_ns,
        sample_interval_ns=sample_interval_ns,
        pre_trigger_samples=pre_trigger,
        post_trigger_samples=post_trigger,
        samples_per_channel=samples_per_channel,
        resolution_bits=resolution_bits,
        overflow_mask=overflow_mask,
        flags=flags,
        channels=channels,
        samples=samples,
    )


def encode(frame: CaptureFrame) -> bytes:
    """Re-encode a frame. Exists so tests can round-trip without Rust."""
    if np is None:  # pragma: no cover
        raise LscpError("numpy is required to encode LSCP frames")

    out = bytearray(HEADER_SIZE)
    _HEADER.pack_into(
        out,
        0,
        MAGIC,
        VERSION,
        frame.flags,
        frame.seq,
        frame.capture_mono_ns,
        frame.sample_interval_ns,
        frame.pre_trigger_samples,
        frame.post_trigger_samples,
        frame.samples_per_channel,
        len(frame.channels),
        frame.resolution_bits,
        frame.overflow_mask,
    )

    inverse_coupling = {v: k for k, v in _COUPLING.items()}
    for channel in frame.channels:
        out.extend(
            _CHANNEL.pack(
                ord(channel.channel) - ord("A"),
                channel.range_code,
                inverse_coupling.get(channel.coupling, 0),
                0,
                channel.scale_v_per_count,
                channel.offset_v,
                0,
            )
        )

    out.extend(np.asarray(frame.samples, dtype="<i2").tobytes())
    return bytes(out)

// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! LSCP/1 — the binary capture frame format.
//!
//! Captures are carried as packed little-endian frames rather than JSON. The
//! payload is raw ADC counts in channel-major order, with per-channel scaling
//! factors in the header, so neither side converts to volts until something
//! actually needs volts. A renderer that only needs pixel positions never
//! converts at all.
//!
//! The measured cost of the format this replaces was 83 bytes per sample, and
//! a client that spent 36.9 ms of every 38.0 ms interval parsing. See
//! `tests/bench/BASELINE.md`.
//!
//! ```text
//! header             64 bytes
//! channel descriptor 16 bytes each, channel_count of them
//! payload            samples_per_channel * channel_count * 2 bytes
//! ```
//!
//! Nothing here is transport-specific: the same bytes travel over a WebSocket
//! binary frame today and could travel over a WebTransport stream unchanged.

use crate::{ChannelId, Coupling};

/// `"LSCP"` read as a little-endian u32.
pub const MAGIC: u32 = 0x5043_534C;
pub const VERSION: u16 = 1;
pub const HEADER_SIZE: usize = 64;
pub const CHANNEL_DESC_SIZE: usize = 16;

/// Frame carried a hardware trigger. Clear means auto/untriggered.
pub const FLAG_TRIGGERED: u16 = 1 << 0;
/// Frame came from continuous streaming mode rather than block mode.
pub const FLAG_STREAMING: u16 = 1 << 1;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ChannelFrame {
    pub channel: ChannelId,
    pub range_code: u8,
    pub coupling: Coupling,
    /// Volts represented by one ADC count.
    pub scale_v_per_count: f32,
    /// Volts to add after scaling, for analogue offset.
    pub offset_v: f32,
}

/// One capture, ready to encode or freshly decoded.
///
/// `samples` is channel-major: all of channel 0, then all of channel 1. Every
/// channel contributes exactly `samples_per_channel` values.
#[derive(Debug, Clone, PartialEq)]
pub struct CaptureFrame {
    pub seq: u64,
    /// Monotonic nanoseconds at the moment the capture left the hardware.
    /// Lets a client measure true capture-to-client latency.
    pub capture_mono_ns: u64,
    pub sample_interval_ns: f64,
    pub pre_trigger_samples: u32,
    pub post_trigger_samples: u32,
    pub samples_per_channel: u32,
    pub resolution_bits: u8,
    pub overflow_mask: u16,
    pub flags: u16,
    pub channels: Vec<ChannelFrame>,
    pub samples: Vec<i16>,
}

#[derive(Debug, PartialEq)]
pub enum DecodeError {
    TooShort { need: usize, got: usize },
    BadMagic(u32),
    UnsupportedVersion(u16),
    LengthMismatch { expected: usize, got: usize },
}

impl std::fmt::Display for DecodeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            DecodeError::TooShort { need, got } => {
                write!(f, "frame too short: need {need} bytes, got {got}")
            }
            DecodeError::BadMagic(m) => {
                write!(f, "bad magic 0x{m:08x}, expected 0x{MAGIC:08x}")
            }
            DecodeError::UnsupportedVersion(v) => {
                write!(f, "unsupported LSCP version {v}, this build speaks {VERSION}")
            }
            DecodeError::LengthMismatch { expected, got } => {
                write!(f, "payload length mismatch: expected {expected} bytes, got {got}")
            }
        }
    }
}

impl std::error::Error for DecodeError {}

fn channel_to_u8(channel: ChannelId) -> u8 {
    match channel {
        // 'A' becomes 0 so the wire value doubles as a channel index.
        ChannelId::Alphabetic(c) => (c as u8).wrapping_sub(b'A'),
        ChannelId::Numeric(n) => n,
    }
}

fn channel_from_u8(value: u8) -> ChannelId {
    ChannelId::Alphabetic((b'A' + value) as char)
}

fn coupling_to_u8(coupling: Coupling) -> u8 {
    match coupling {
        Coupling::DC => 0,
        Coupling::AC => 1,
        Coupling::GND => 2,
    }
}

fn coupling_from_u8(value: u8) -> Coupling {
    match value {
        1 => Coupling::AC,
        2 => Coupling::GND,
        _ => Coupling::DC,
    }
}

impl CaptureFrame {
    pub fn encoded_len(&self) -> usize {
        HEADER_SIZE
            + self.channels.len() * CHANNEL_DESC_SIZE
            + self.samples.len() * 2
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.encoded_len());

        out.extend_from_slice(&MAGIC.to_le_bytes());
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.extend_from_slice(&self.flags.to_le_bytes());
        out.extend_from_slice(&self.seq.to_le_bytes());
        out.extend_from_slice(&self.capture_mono_ns.to_le_bytes());
        out.extend_from_slice(&self.sample_interval_ns.to_le_bytes());
        out.extend_from_slice(&self.pre_trigger_samples.to_le_bytes());
        out.extend_from_slice(&self.post_trigger_samples.to_le_bytes());
        out.extend_from_slice(&self.samples_per_channel.to_le_bytes());
        out.push(self.channels.len() as u8);
        out.push(self.resolution_bits);
        out.extend_from_slice(&self.overflow_mask.to_le_bytes());
        out.resize(HEADER_SIZE, 0); // reserved tail

        for channel in &self.channels {
            out.push(channel_to_u8(channel.channel));
            out.push(channel.range_code);
            out.push(coupling_to_u8(channel.coupling));
            out.push(0); // reserved
            out.extend_from_slice(&channel.scale_v_per_count.to_le_bytes());
            out.extend_from_slice(&channel.offset_v.to_le_bytes());
            out.extend_from_slice(&0u32.to_le_bytes()); // reserved
        }

        // The hot path. Native little-endian lets this be one bulk copy;
        // anywhere else it degrades to a per-sample swap, which is still
        // far cheaper than formatting a JSON object per sample.
        #[cfg(target_endian = "little")]
        {
            let bytes = unsafe {
                std::slice::from_raw_parts(
                    self.samples.as_ptr() as *const u8,
                    std::mem::size_of_val(&self.samples[..]),
                )
            };
            out.extend_from_slice(bytes);
        }
        #[cfg(not(target_endian = "little"))]
        {
            for sample in &self.samples {
                out.extend_from_slice(&sample.to_le_bytes());
            }
        }

        out
    }

    pub fn decode(buf: &[u8]) -> Result<Self, DecodeError> {
        if buf.len() < HEADER_SIZE {
            return Err(DecodeError::TooShort {
                need: HEADER_SIZE,
                got: buf.len(),
            });
        }

        let magic = u32::from_le_bytes(buf[0..4].try_into().unwrap());
        if magic != MAGIC {
            return Err(DecodeError::BadMagic(magic));
        }
        let version = u16::from_le_bytes(buf[4..6].try_into().unwrap());
        if version != VERSION {
            return Err(DecodeError::UnsupportedVersion(version));
        }

        let flags = u16::from_le_bytes(buf[6..8].try_into().unwrap());
        let seq = u64::from_le_bytes(buf[8..16].try_into().unwrap());
        let capture_mono_ns = u64::from_le_bytes(buf[16..24].try_into().unwrap());
        let sample_interval_ns = f64::from_le_bytes(buf[24..32].try_into().unwrap());
        let pre_trigger_samples = u32::from_le_bytes(buf[32..36].try_into().unwrap());
        let post_trigger_samples = u32::from_le_bytes(buf[36..40].try_into().unwrap());
        let samples_per_channel = u32::from_le_bytes(buf[40..44].try_into().unwrap());
        let channel_count = buf[44] as usize;
        let resolution_bits = buf[45];
        let overflow_mask = u16::from_le_bytes(buf[46..48].try_into().unwrap());

        let descriptors_end = HEADER_SIZE + channel_count * CHANNEL_DESC_SIZE;
        if buf.len() < descriptors_end {
            return Err(DecodeError::TooShort {
                need: descriptors_end,
                got: buf.len(),
            });
        }

        let mut channels = Vec::with_capacity(channel_count);
        for i in 0..channel_count {
            let at = HEADER_SIZE + i * CHANNEL_DESC_SIZE;
            channels.push(ChannelFrame {
                channel: channel_from_u8(buf[at]),
                range_code: buf[at + 1],
                coupling: coupling_from_u8(buf[at + 2]),
                scale_v_per_count: f32::from_le_bytes(
                    buf[at + 4..at + 8].try_into().unwrap(),
                ),
                offset_v: f32::from_le_bytes(buf[at + 8..at + 12].try_into().unwrap()),
            });
        }

        let sample_count = samples_per_channel as usize * channel_count;
        let payload = &buf[descriptors_end..];
        if payload.len() != sample_count * 2 {
            return Err(DecodeError::LengthMismatch {
                expected: sample_count * 2,
                got: payload.len(),
            });
        }

        let mut samples = Vec::with_capacity(sample_count);
        for chunk in payload.chunks_exact(2) {
            samples.push(i16::from_le_bytes([chunk[0], chunk[1]]));
        }

        Ok(CaptureFrame {
            seq,
            capture_mono_ns,
            sample_interval_ns,
            pre_trigger_samples,
            post_trigger_samples,
            samples_per_channel,
            resolution_bits,
            overflow_mask,
            flags,
            channels,
            samples,
        })
    }

    /// Raw counts for one channel, by position in `channels`.
    pub fn channel_samples(&self, index: usize) -> Option<&[i16]> {
        if index >= self.channels.len() {
            return None;
        }
        let n = self.samples_per_channel as usize;
        Some(&self.samples[index * n..(index + 1) * n])
    }

    /// One channel converted to volts. Allocates, so it is for callers that
    /// genuinely need volts rather than for the streaming path.
    pub fn channel_volts(&self, index: usize) -> Option<Vec<f64>> {
        let descriptor = self.channels.get(index)?;
        let samples = self.channel_samples(index)?;
        Some(
            samples
                .iter()
                .map(|&count| {
                    count as f64 * descriptor.scale_v_per_count as f64
                        + descriptor.offset_v as f64
                })
                .collect(),
        )
    }

    pub fn is_triggered(&self) -> bool {
        self.flags & FLAG_TRIGGERED != 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_frame() -> CaptureFrame {
        CaptureFrame {
            seq: 42,
            capture_mono_ns: 1_234_567_890,
            sample_interval_ns: 8.0,
            pre_trigger_samples: 4,
            post_trigger_samples: 4,
            samples_per_channel: 8,
            resolution_bits: 8,
            overflow_mask: 0b10,
            flags: FLAG_TRIGGERED,
            channels: vec![
                ChannelFrame {
                    channel: ChannelId::Alphabetic('A'),
                    range_code: 7,
                    coupling: Coupling::DC,
                    scale_v_per_count: 0.001,
                    offset_v: 0.0,
                },
                ChannelFrame {
                    channel: ChannelId::Alphabetic('B'),
                    range_code: 5,
                    coupling: Coupling::AC,
                    scale_v_per_count: 0.002,
                    offset_v: -0.5,
                },
            ],
            samples: vec![
                0, 100, 200, 300, -300, -200, -100, 0, // A
                1, 2, 3, 4, -4, -3, -2, -1, // B
            ],
        }
    }

    #[test]
    fn round_trips() {
        let original = sample_frame();
        let decoded = CaptureFrame::decode(&original.encode()).unwrap();
        assert_eq!(original, decoded);
    }

    #[test]
    fn encoded_len_matches_actual() {
        let frame = sample_frame();
        assert_eq!(frame.encode().len(), frame.encoded_len());
        // 64 header + 2*16 descriptors + 16 samples * 2 bytes
        assert_eq!(frame.encode().len(), 64 + 32 + 32);
    }

    #[test]
    fn header_is_the_documented_size() {
        let frame = sample_frame();
        let bytes = frame.encode();
        assert_eq!(&bytes[0..4], b"LSCP");
        assert_eq!(HEADER_SIZE, 64);
        assert_eq!(CHANNEL_DESC_SIZE, 16);
    }

    #[test]
    fn splits_channels_correctly() {
        let frame = sample_frame();
        assert_eq!(
            frame.channel_samples(0).unwrap(),
            &[0, 100, 200, 300, -300, -200, -100, 0]
        );
        assert_eq!(
            frame.channel_samples(1).unwrap(),
            &[1, 2, 3, 4, -4, -3, -2, -1]
        );
        assert!(frame.channel_samples(2).is_none());
    }

    #[test]
    fn converts_counts_to_volts_with_offset() {
        let frame = sample_frame();
        let volts = frame.channel_volts(1).unwrap();
        // count 1 * 0.002 + (-0.5)
        assert!((volts[0] - (-0.498)).abs() < 1e-9);
    }

    #[test]
    fn rejects_bad_magic() {
        let mut bytes = sample_frame().encode();
        bytes[0] = b'X';
        assert!(matches!(
            CaptureFrame::decode(&bytes),
            Err(DecodeError::BadMagic(_))
        ));
    }

    #[test]
    fn rejects_future_version() {
        let mut bytes = sample_frame().encode();
        bytes[4..6].copy_from_slice(&99u16.to_le_bytes());
        assert_eq!(
            CaptureFrame::decode(&bytes),
            Err(DecodeError::UnsupportedVersion(99))
        );
    }

    #[test]
    fn rejects_truncated_payload() {
        let bytes = sample_frame().encode();
        let truncated = &bytes[..bytes.len() - 4];
        assert!(matches!(
            CaptureFrame::decode(truncated),
            Err(DecodeError::LengthMismatch { .. })
        ));
    }

    #[test]
    fn rejects_short_header() {
        assert!(matches!(
            CaptureFrame::decode(&[0u8; 8]),
            Err(DecodeError::TooShort { .. })
        ));
    }

    #[test]
    fn empty_capture_round_trips() {
        let frame = CaptureFrame {
            seq: 0,
            capture_mono_ns: 0,
            sample_interval_ns: 1.0,
            pre_trigger_samples: 0,
            post_trigger_samples: 0,
            samples_per_channel: 0,
            resolution_bits: 12,
            overflow_mask: 0,
            flags: 0,
            channels: vec![],
            samples: vec![],
        };
        assert_eq!(CaptureFrame::decode(&frame.encode()).unwrap(), frame);
    }

    #[test]
    fn stays_far_smaller_than_the_json_it_replaces() {
        // 8000 samples on one channel, the real STG-2 capture size.
        let frame = CaptureFrame {
            seq: 1,
            capture_mono_ns: 1,
            sample_interval_ns: 8.0,
            pre_trigger_samples: 4000,
            post_trigger_samples: 4000,
            samples_per_channel: 8000,
            resolution_bits: 8,
            overflow_mask: 0,
            flags: FLAG_TRIGGERED,
            channels: vec![ChannelFrame {
                channel: ChannelId::Alphabetic('A'),
                range_code: 7,
                coupling: Coupling::DC,
                scale_v_per_count: 0.001,
                offset_v: 0.0,
            }],
            samples: vec![0i16; 8000],
        };
        let encoded = frame.encode().len();
        assert_eq!(encoded, 64 + 16 + 16_000);
        // The measured legacy baseline was 648.1 KB for this same capture.
        assert!(encoded < 20_000, "encoded {encoded} bytes");
    }
}

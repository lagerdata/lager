// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Emits the canonical LSCP frame as hex.
//!
//! The Python decoder's cross-language test embeds this output, so if the
//! Rust encoder's layout ever changes the Python test fails rather than the
//! two implementations quietly disagreeing on the wire.
//!
//!     cargo run -p protocol --example lscp_fixture

use protocol::lscp::{CaptureFrame, ChannelFrame, FLAG_TRIGGERED};
use protocol::{ChannelId, Coupling};

fn main() {
    let frame = CaptureFrame {
        seq: 7,
        capture_mono_ns: 123_456_789_000,
        sample_interval_ns: 16.0,
        pre_trigger_samples: 2,
        post_trigger_samples: 2,
        samples_per_channel: 4,
        resolution_bits: 8,
        overflow_mask: 0b10,
        flags: FLAG_TRIGGERED,
        channels: vec![
            ChannelFrame {
                channel: ChannelId::Alphabetic('A'),
                range_code: 7,
                coupling: Coupling::DC,
                scale_v_per_count: 0.00125,
                offset_v: 0.0,
            },
            ChannelFrame {
                channel: ChannelId::Alphabetic('B'),
                range_code: 4,
                coupling: Coupling::AC,
                scale_v_per_count: 0.0025,
                offset_v: -0.25,
            },
        ],
        samples: vec![0, 1000, -1000, 32767, -32768, 5, -5, 0],
    };

    let encoded = frame.encode();
    for byte in &encoded {
        print!("{byte:02x}");
    }
    println!();
}

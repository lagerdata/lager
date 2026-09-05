// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Encode benchmarks for the LSCP frame path.
//!
//! The point of comparison is the format this replaced: serializing one JSON
//! object per sample, which cost 83 bytes and, on the client side, 36.9 ms of
//! parsing per 8000-sample capture. Both encoders run here on identical data
//! so the difference is measured rather than asserted.
//!
//!     cargo bench -p daemon

use criterion::{BenchmarkId, Criterion, Throughput, criterion_group, criterion_main};
use protocol::lscp::{CaptureFrame, ChannelFrame, FLAG_TRIGGERED};
use protocol::{ChannelId, Coupling};
use serde::Serialize;

/// The shape the old wire format used, kept here only as a baseline to
/// measure against.
#[derive(Serialize)]
struct LegacySample {
    channel: ChannelId,
    voltage: f64,
    sample_index: u32,
}

fn frame(samples_per_channel: u32, channel_count: usize) -> CaptureFrame {
    let channels: Vec<ChannelFrame> = (0..channel_count)
        .map(|i| ChannelFrame {
            channel: ChannelId::Alphabetic((b'A' + i as u8) as char),
            range_code: 7,
            coupling: Coupling::DC,
            scale_v_per_count: 0.001,
            offset_v: 0.0,
        })
        .collect();

    // A ramp rather than zeros, so nothing can be optimized away as constant.
    let total = samples_per_channel as usize * channel_count;
    let samples: Vec<i16> = (0..total).map(|i| (i % 32768) as i16).collect();

    CaptureFrame {
        seq: 1,
        capture_mono_ns: 123_456_789,
        sample_interval_ns: 8.0,
        pre_trigger_samples: samples_per_channel / 2,
        post_trigger_samples: samples_per_channel / 2,
        samples_per_channel,
        resolution_bits: 8,
        overflow_mask: 0,
        flags: FLAG_TRIGGERED,
        channels,
        samples,
    }
}

fn bench_encode(c: &mut Criterion) {
    let mut group = c.benchmark_group("lscp_encode");

    // 8000 x 1ch is the real STG-2 capture; the rest bracket it.
    for (samples, channels) in [(8_000u32, 1usize), (8_000, 2), (32_000, 4), (128_000, 2)] {
        let frame = frame(samples, channels);
        let total = samples as u64 * channels as u64;
        group.throughput(Throughput::Elements(total));
        group.bench_with_input(
            BenchmarkId::from_parameter(format!("{samples}x{channels}ch")),
            &frame,
            |b, frame| b.iter(|| std::hint::black_box(frame.encode())),
        );
    }

    group.finish();
}

fn bench_decode(c: &mut Criterion) {
    let mut group = c.benchmark_group("lscp_decode");

    for (samples, channels) in [(8_000u32, 1usize), (8_000, 2), (32_000, 4)] {
        let encoded = frame(samples, channels).encode();
        let total = samples as u64 * channels as u64;
        group.throughput(Throughput::Elements(total));
        group.bench_with_input(
            BenchmarkId::from_parameter(format!("{samples}x{channels}ch")),
            &encoded,
            |b, bytes| b.iter(|| std::hint::black_box(CaptureFrame::decode(bytes).unwrap())),
        );
    }

    group.finish();
}

/// What the old format cost to produce, for the same capture.
fn bench_legacy_json(c: &mut Criterion) {
    let mut group = c.benchmark_group("legacy_json_encode");
    group.sample_size(20); // Slow enough that the default 100 is tedious.

    for (samples, channels) in [(8_000u32, 1usize), (8_000, 2)] {
        let source = frame(samples, channels);
        let legacy: Vec<LegacySample> = source
            .channels
            .iter()
            .enumerate()
            .flat_map(|(index, descriptor)| {
                let counts = source.channel_samples(index).unwrap().to_vec();
                counts.into_iter().enumerate().map(move |(i, count)| {
                    LegacySample {
                        channel: descriptor.channel,
                        voltage: count as f64 * descriptor.scale_v_per_count as f64,
                        sample_index: i as u32,
                    }
                })
            })
            .collect();

        let total = samples as u64 * channels as u64;
        group.throughput(Throughput::Elements(total));
        group.bench_with_input(
            BenchmarkId::from_parameter(format!("{samples}x{channels}ch")),
            &legacy,
            |b, samples| {
                b.iter(|| std::hint::black_box(serde_json::to_string(samples).unwrap()))
            },
        );
    }

    group.finish();
}

criterion_group!(benches, bench_encode, bench_decode, bench_legacy_json);
criterion_main!(benches);

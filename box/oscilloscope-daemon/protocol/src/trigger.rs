// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Whether a capture was triggered, read from its own samples.
//!
//! In auto mode a block completes either on a trigger or when the auto
//! timeout runs out, and neither driver family says which. A block that
//! triggered holds the trigger condition at its trigger point -- the source
//! channel crosses the level in the slope's direction there -- and one that
//! timed out almost never does, since its trigger point landed wherever the
//! timeout expired. That is what a scope's "Trig'd" and "Auto" indicators
//! report.

use crate::lscp::{CaptureFrame, NO_SAMPLE};
use crate::{ChannelId, TriggerSlope};

/// Samples either side of the trigger point that a crossing may fall in.
///
/// A PicoScope places the trigger at the sample the crossing completes on,
/// measured at +1 on a 2204A, so one is enough in principle. The rest is
/// margin for quantisation near the level, where an 8-bit ADC can put two
/// consecutive samples on the same code.
pub const TRIGGER_WINDOW: usize = 4;

/// Whether `samples` cross `level` in the direction of `slope` within
/// `window` samples of `index`.
///
/// A crossing is a pair of consecutive samples that straddle the level: for
/// a rising edge the first is below it and the second at or above. `Neither`
/// never triggers, and `Either` takes a crossing in either direction.
pub fn crosses_near(
    samples: &[i16],
    index: usize,
    level: f64,
    slope: TriggerSlope,
    window: usize,
) -> bool {
    if samples.len() < 2 || slope == TriggerSlope::Neither {
        return false;
    }
    let first = index.saturating_sub(window).max(1);
    let last = (index + window).min(samples.len() - 1);
    (first..=last).any(|i| {
        let (before, after) = (samples[i - 1], samples[i]);
        if before == NO_SAMPLE || after == NO_SAMPLE {
            return false;
        }
        let (before, after) = (f64::from(before), f64::from(after));
        let rising = before < level && after >= level;
        let falling = before > level && after <= level;
        match slope {
            TriggerSlope::Rising => rising,
            TriggerSlope::Falling => falling,
            TriggerSlope::Either => rising || falling,
            TriggerSlope::Neither => false,
        }
    })
}

/// Whether `frame` holds the trigger condition at its trigger point.
///
/// `level_volts` is at the probe tip, the same units the frame's scale
/// converts counts into. A frame without the source channel -- triggering on
/// a channel that is switched off -- cannot show it, and reads as untriggered.
pub fn frame_is_triggered(
    frame: &CaptureFrame,
    source: ChannelId,
    level_volts: f64,
    slope: TriggerSlope,
) -> bool {
    let Some(index) = frame.channels.iter().position(|c| c.channel == source) else {
        return false;
    };
    let descriptor = &frame.channels[index];
    let scale = f64::from(descriptor.scale_v_per_count);
    if scale == 0.0 || !scale.is_finite() {
        return false;
    }
    let level_counts = (level_volts - f64::from(descriptor.offset_v)) / scale;
    let Some(samples) = frame.channel_samples(index) else {
        return false;
    };
    crosses_near(
        samples,
        frame.pre_trigger_samples as usize,
        level_counts,
        slope,
        TRIGGER_WINDOW,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lscp::ChannelFrame;
    use crate::Coupling;

    fn ramp_crossing_at(n: usize, at: usize, rising: bool) -> Vec<i16> {
        (0..n)
            .map(|i| {
                let up = if i >= at { 1000 } else { -1000 };
                if rising { up } else { -up }
            })
            .collect()
    }

    #[test]
    fn a_rising_edge_at_the_trigger_point_is_found() {
        let samples = ramp_crossing_at(100, 50, true);
        assert!(crosses_near(&samples, 50, 0.0, TriggerSlope::Rising, 4));
        assert!(crosses_near(&samples, 49, 0.0, TriggerSlope::Rising, 4));
    }

    #[test]
    fn the_wrong_direction_does_not_count() {
        let samples = ramp_crossing_at(100, 50, false);
        assert!(!crosses_near(&samples, 50, 0.0, TriggerSlope::Rising, 4));
        assert!(crosses_near(&samples, 50, 0.0, TriggerSlope::Falling, 4));
        assert!(crosses_near(&samples, 50, 0.0, TriggerSlope::Either, 4));
    }

    #[test]
    fn a_crossing_far_from_the_trigger_point_does_not_count() {
        // What an auto-triggered block looks like: an edge somewhere, but
        // not where the trigger point landed.
        let samples = ramp_crossing_at(1000, 300, true);
        assert!(!crosses_near(&samples, 500, 0.0, TriggerSlope::Rising, 4));
    }

    #[test]
    fn a_level_the_signal_never_reaches_does_not_count() {
        let samples = ramp_crossing_at(100, 50, true);
        assert!(!crosses_near(&samples, 50, 2000.0, TriggerSlope::Rising, 4));
    }

    #[test]
    fn neither_never_triggers() {
        let samples = ramp_crossing_at(100, 50, true);
        assert!(!crosses_near(&samples, 50, 0.0, TriggerSlope::Neither, 4));
    }

    #[test]
    fn the_window_is_clamped_to_the_record() {
        let samples = ramp_crossing_at(10, 1, true);
        assert!(crosses_near(&samples, 0, 0.0, TriggerSlope::Rising, 4));
        assert!(!crosses_near(&[5], 0, 0.0, TriggerSlope::Rising, 4));
        assert!(!crosses_near(&[], 0, 0.0, TriggerSlope::Rising, 4));
    }

    #[test]
    fn a_gap_in_a_rolling_record_is_not_a_crossing() {
        let samples = [NO_SAMPLE, 1000, 1000];
        assert!(!crosses_near(&samples, 1, 0.0, TriggerSlope::Rising, 4));
    }

    fn frame(samples: Vec<i16>, pre: u32, scale: f32) -> CaptureFrame {
        let n = samples.len() as u32;
        CaptureFrame {
            seq: 1,
            capture_mono_ns: 0,
            sample_interval_ns: 1.0,
            pre_trigger_samples: pre,
            post_trigger_samples: n - pre,
            samples_per_channel: n,
            resolution_bits: 8,
            overflow_mask: 0,
            flags: 0,
            channels: vec![ChannelFrame {
                channel: ChannelId::Alphabetic('A'),
                range_code: 0,
                coupling: Coupling::DC,
                scale_v_per_count: scale,
                offset_v: 0.0,
            }],
            samples,
        }
    }

    #[test]
    fn the_level_is_converted_through_the_channel_scale() {
        // 1 mV a count, an edge from -1 V to +1 V: a 0.5 V level sits between.
        let f = frame(ramp_crossing_at(100, 50, true), 50, 0.001);
        assert!(frame_is_triggered(&f, ChannelId::Alphabetic('A'), 0.5, TriggerSlope::Rising));
        assert!(!frame_is_triggered(&f, ChannelId::Alphabetic('A'), 1.5, TriggerSlope::Rising));
    }

    #[test]
    fn a_source_channel_missing_from_the_frame_is_untriggered() {
        let f = frame(ramp_crossing_at(100, 50, true), 50, 0.001);
        assert!(!frame_is_triggered(&f, ChannelId::Alphabetic('B'), 0.0, TriggerSlope::Rising));
    }
}

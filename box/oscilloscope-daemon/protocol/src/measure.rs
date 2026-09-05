// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Waveform measurements computed from a captured block.
//!
//! No PicoTech API exposes hardware measurements, unlike the Rigol MSO5000
//! which answers `MEAS:VPP?` directly. To reach parity these are computed
//! from the samples, on the daemon side so that every client -- web UI,
//! CLI, Python, MCP -- gets the same number from the same code.
//!
//! This lives in the protocol crate rather than the daemon because
//! [`MeasurementSet`] is part of the wire response, and because everything
//! here is a pure function of a [`CaptureFrame`], which the protocol also
//! owns. A client holding a frame can compute the same numbers offline.
//!
//! Timing measurements use mid-level crossings with hysteresis set from the
//! measured amplitude. Hysteresis matters: a plain mid-level comparison
//! double-counts every noisy edge, which reports a harmonic of the true
//! frequency rather than the frequency.

use crate::CaptureFrame;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Measurement {
    Vpp,
    Vmax,
    Vmin,
    Vrms,
    Vavg,
    Period,
    Frequency,
    DutyCyclePositive,
    DutyCycleNegative,
    PulseWidthPositive,
    PulseWidthNegative,
    RiseTime,
    FallTime,
    Overshoot,
}

impl Measurement {
    pub fn parse(name: &str) -> Option<Self> {
        let normalized = name.to_lowercase().replace(['-', ' '], "_");
        Some(match normalized.as_str() {
            "vpp" | "peak_to_peak" => Measurement::Vpp,
            "vmax" | "max" => Measurement::Vmax,
            "vmin" | "min" => Measurement::Vmin,
            "vrms" | "rms" => Measurement::Vrms,
            "vavg" | "avg" | "average" | "mean" => Measurement::Vavg,
            "period" => Measurement::Period,
            "freq" | "frequency" => Measurement::Frequency,
            "duty_cycle_pos" | "duty_cycle_positive" | "dc_pos" => {
                Measurement::DutyCyclePositive
            }
            "duty_cycle_neg" | "duty_cycle_negative" | "dc_neg" => {
                Measurement::DutyCycleNegative
            }
            "pulse_width_pos" | "pulse_width_positive" | "pw_pos" => {
                Measurement::PulseWidthPositive
            }
            "pulse_width_neg" | "pulse_width_negative" | "pw_neg" => {
                Measurement::PulseWidthNegative
            }
            "rise_time" | "risetime" => Measurement::RiseTime,
            "fall_time" | "falltime" => Measurement::FallTime,
            "overshoot" => Measurement::Overshoot,
            _ => return None,
        })
    }

    /// Unit for display. Empty for dimensionless ratios.
    pub fn unit(&self) -> &'static str {
        match self {
            Measurement::Vpp
            | Measurement::Vmax
            | Measurement::Vmin
            | Measurement::Vrms
            | Measurement::Vavg => "V",
            Measurement::Period
            | Measurement::PulseWidthPositive
            | Measurement::PulseWidthNegative
            | Measurement::RiseTime
            | Measurement::FallTime => "s",
            Measurement::Frequency => "Hz",
            Measurement::DutyCyclePositive
            | Measurement::DutyCycleNegative
            | Measurement::Overshoot => "%",
        }
    }
}

/// Everything computable from one channel of one capture.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MeasurementSet {
    pub vmax: f64,
    pub vmin: f64,
    pub vpp: f64,
    pub vavg: f64,
    pub vrms: f64,
    /// None when the capture holds fewer than two full cycles, in which case
    /// a period cannot be established rather than merely being imprecise.
    pub period: Option<f64>,
    pub frequency: Option<f64>,
    pub duty_cycle_positive: Option<f64>,
    pub duty_cycle_negative: Option<f64>,
    pub pulse_width_positive: Option<f64>,
    pub pulse_width_negative: Option<f64>,
    pub rise_time: Option<f64>,
    pub fall_time: Option<f64>,
    pub overshoot: Option<f64>,
}

impl MeasurementSet {
    pub fn get(&self, which: Measurement) -> Option<f64> {
        match which {
            Measurement::Vpp => Some(self.vpp),
            Measurement::Vmax => Some(self.vmax),
            Measurement::Vmin => Some(self.vmin),
            Measurement::Vrms => Some(self.vrms),
            Measurement::Vavg => Some(self.vavg),
            Measurement::Period => self.period,
            Measurement::Frequency => self.frequency,
            Measurement::DutyCyclePositive => self.duty_cycle_positive,
            Measurement::DutyCycleNegative => self.duty_cycle_negative,
            Measurement::PulseWidthPositive => self.pulse_width_positive,
            Measurement::PulseWidthNegative => self.pulse_width_negative,
            Measurement::RiseTime => self.rise_time,
            Measurement::FallTime => self.fall_time,
            Measurement::Overshoot => self.overshoot,
        }
    }
}

/// Index and interpolated sub-sample position of a level crossing.
struct Crossing {
    /// Fractional sample index, interpolated between the two straddling
    /// samples so timing resolution is not limited to the sample interval.
    position: f64,
    rising: bool,
}

fn interpolate(before: f64, after: f64, level: f64, index: usize) -> f64 {
    let span = after - before;
    if span.abs() < f64::EPSILON {
        return index as f64;
    }
    index as f64 + (level - before) / span
}

/// Mid-level crossings with hysteresis, which is what stops noise around the
/// threshold from registering as extra edges.
fn find_crossings(samples: &[f64], low: f64, high: f64, mid: f64) -> Vec<Crossing> {
    let mut crossings = Vec::new();
    if samples.len() < 2 {
        return crossings;
    }

    // 10% of amplitude on each side. Wide enough to reject typical noise,
    // narrow enough not to miss a genuine edge on a low-amplitude signal.
    let hysteresis = (high - low) * 0.1;
    let upper = mid + hysteresis;
    let lower = mid - hysteresis;

    // Start in whichever state the first sample is unambiguously in, so a
    // capture beginning mid-transition does not fabricate an edge.
    let mut state_high = samples[0] > mid;

    for i in 1..samples.len() {
        let value = samples[i];
        if !state_high && value > upper {
            crossings.push(Crossing {
                position: interpolate(samples[i - 1], value, mid, i - 1),
                rising: true,
            });
            state_high = true;
        } else if state_high && value < lower {
            crossings.push(Crossing {
                position: interpolate(samples[i - 1], value, mid, i - 1),
                rising: false,
            });
            state_high = false;
        }
    }

    crossings
}

/// Time from the first sample crossing `from_level` to the first crossing of
/// `to_level` after it, used for rise and fall times.
fn transition_time(
    samples: &[f64],
    interval_s: f64,
    from_level: f64,
    to_level: f64,
    rising: bool,
) -> Option<f64> {
    let mut start = None;
    for i in 1..samples.len() {
        let (previous, current) = (samples[i - 1], samples[i]);
        let crossed_from = if rising {
            previous < from_level && current >= from_level
        } else {
            previous > from_level && current <= from_level
        };
        if start.is_none() && crossed_from {
            start = Some(interpolate(previous, current, from_level, i - 1));
            continue;
        }
        if let Some(start_position) = start {
            let crossed_to = if rising {
                previous < to_level && current >= to_level
            } else {
                previous > to_level && current <= to_level
            };
            if crossed_to {
                let end = interpolate(previous, current, to_level, i - 1);
                return Some((end - start_position) * interval_s);
            }
        }
    }
    None
}

/// Compute every measurement for one channel of a capture.
pub fn measure_channel(frame: &CaptureFrame, channel_index: usize) -> Option<MeasurementSet> {
    let descriptor = frame.channels.get(channel_index)?;
    let counts = frame.channel_samples(channel_index)?;
    if counts.is_empty() {
        return None;
    }

    let scale = descriptor.scale_v_per_count as f64;
    let offset = descriptor.offset_v as f64;
    let samples: Vec<f64> = counts.iter().map(|&c| c as f64 * scale + offset).collect();

    let mut vmax = f64::NEG_INFINITY;
    let mut vmin = f64::INFINITY;
    let mut sum = 0.0;
    let mut sum_squares = 0.0;
    for &v in &samples {
        vmax = vmax.max(v);
        vmin = vmin.min(v);
        sum += v;
        sum_squares += v * v;
    }
    let n = samples.len() as f64;
    let vavg = sum / n;
    let vrms = (sum_squares / n).sqrt();
    let vpp = vmax - vmin;

    let interval_s = frame.sample_interval_ns / 1e9;
    let mid = (vmax + vmin) / 2.0;
    let crossings = find_crossings(&samples, vmin, vmax, mid);

    // A period needs one rising edge, the following falling edge, and the
    // next rising edge. Fewer than three crossings cannot establish one.
    let mut period = None;
    let mut pulse_width_positive = None;
    let mut pulse_width_negative = None;

    if crossings.len() >= 3 {
        let rising: Vec<f64> = crossings
            .iter()
            .filter(|c| c.rising)
            .map(|c| c.position)
            .collect();

        if rising.len() >= 2 {
            // Average across all cycles present rather than timing a single
            // one, which divides random jitter by the cycle count.
            let span = rising[rising.len() - 1] - rising[0];
            period = Some(span / (rising.len() - 1) as f64 * interval_s);
        }

        // First complete high and low interval.
        for pair in crossings.windows(2) {
            let width = (pair[1].position - pair[0].position) * interval_s;
            if pair[0].rising && pulse_width_positive.is_none() {
                pulse_width_positive = Some(width);
            } else if !pair[0].rising && pulse_width_negative.is_none() {
                pulse_width_negative = Some(width);
            }
        }
    }

    let frequency = period.filter(|p| *p > 0.0).map(|p| 1.0 / p);
    let duty_cycle_positive = match (pulse_width_positive, period) {
        (Some(width), Some(p)) if p > 0.0 => Some(width / p * 100.0),
        _ => None,
    };
    let duty_cycle_negative = duty_cycle_positive.map(|d| 100.0 - d);

    // 10% / 90% of amplitude, the conventional definition.
    let low_threshold = vmin + vpp * 0.1;
    let high_threshold = vmin + vpp * 0.9;
    let rise_time = if vpp > 0.0 {
        transition_time(&samples, interval_s, low_threshold, high_threshold, true)
    } else {
        None
    };
    let fall_time = if vpp > 0.0 {
        transition_time(&samples, interval_s, high_threshold, low_threshold, false)
    } else {
        None
    };

    // Overshoot is measured against the settled top, approximated here by the
    // mean of the samples above mid.
    let above: Vec<f64> = samples.iter().copied().filter(|&v| v > mid).collect();
    let overshoot = if !above.is_empty() && vpp > 0.0 {
        let top = above.iter().sum::<f64>() / above.len() as f64;
        let excess = vmax - top;
        if excess > 0.0 {
            Some(excess / vpp * 100.0)
        } else {
            Some(0.0)
        }
    } else {
        None
    };

    Some(MeasurementSet {
        vmax,
        vmin,
        vpp,
        vavg,
        vrms,
        period,
        frequency,
        duty_cycle_positive,
        duty_cycle_negative,
        pulse_width_positive,
        pulse_width_negative,
        rise_time,
        fall_time,
        overshoot,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lscp::ChannelFrame;
    use crate::{ChannelId, Coupling};

    /// Build a frame from volts, using a 1 mV/count scale so the numbers
    /// coming back out are easy to reason about.
    fn frame_from_volts(volts: &[f64], sample_rate_hz: f64) -> CaptureFrame {
        let scale = 0.001f32;
        let counts: Vec<i16> = volts
            .iter()
            .map(|v| (v / scale as f64).round() as i16)
            .collect();
        CaptureFrame {
            seq: 0,
            capture_mono_ns: 0,
            sample_interval_ns: 1e9 / sample_rate_hz,
            pre_trigger_samples: 0,
            post_trigger_samples: counts.len() as u32,
            samples_per_channel: counts.len() as u32,
            resolution_bits: 16,
            overflow_mask: 0,
            flags: 0,
            channels: vec![ChannelFrame {
                channel: ChannelId::Alphabetic('A'),
                range_code: 0,
                coupling: Coupling::DC,
                scale_v_per_count: scale,
                offset_v: 0.0,
            }],
            samples: counts,
        }
    }

    fn sine(cycles: f64, samples: usize, amplitude: f64, offset: f64) -> Vec<f64> {
        (0..samples)
            .map(|i| {
                let phase = 2.0 * std::f64::consts::PI * cycles * i as f64 / samples as f64;
                offset + amplitude * phase.sin()
            })
            .collect()
    }

    fn square(cycles: usize, samples_per_cycle: usize, duty: f64) -> Vec<f64> {
        let mut out = Vec::new();
        let high_samples = (samples_per_cycle as f64 * duty).round() as usize;
        for _ in 0..cycles {
            for i in 0..samples_per_cycle {
                out.push(if i < high_samples { 1.0 } else { -1.0 });
            }
        }
        out
    }

    #[test]
    fn amplitude_of_a_known_sine() {
        // 1 Vpk sine, so 2 Vpp and Vrms = 1/sqrt(2).
        let frame = frame_from_volts(&sine(10.0, 10_000, 1.0, 0.0), 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        assert!((m.vpp - 2.0).abs() < 0.01, "vpp {}", m.vpp);
        assert!((m.vmax - 1.0).abs() < 0.01, "vmax {}", m.vmax);
        assert!((m.vmin + 1.0).abs() < 0.01, "vmin {}", m.vmin);
        // A unit sine's RMS is 1/sqrt(2).
        let expected_vrms = std::f64::consts::FRAC_1_SQRT_2;
        assert!((m.vrms - expected_vrms).abs() < 0.01, "vrms {}", m.vrms);
        assert!(m.vavg.abs() < 0.01, "vavg {}", m.vavg);
    }

    #[test]
    fn dc_offset_shows_up_in_average_not_amplitude() {
        let frame = frame_from_volts(&sine(10.0, 10_000, 1.0, 2.5), 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        assert!((m.vavg - 2.5).abs() < 0.01, "vavg {}", m.vavg);
        assert!((m.vpp - 2.0).abs() < 0.01, "vpp {}", m.vpp);
    }

    #[test]
    fn frequency_of_a_known_sine() {
        // 10 cycles over 10k samples at 1 MS/s is 1 kHz.
        let frame = frame_from_volts(&sine(10.0, 10_000, 1.0, 0.0), 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        let frequency = m.frequency.expect("frequency");
        assert!((frequency - 1000.0).abs() < 5.0, "frequency {frequency}");
        let period = m.period.expect("period");
        assert!((period - 0.001).abs() < 1e-5, "period {period}");
    }

    #[test]
    fn duty_cycle_of_an_asymmetric_square() {
        // 30% duty, 100 samples per cycle at 1 MS/s.
        let frame = frame_from_volts(&square(10, 100, 0.3), 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        let duty = m.duty_cycle_positive.expect("duty");
        assert!((duty - 30.0).abs() < 2.0, "duty {duty}");
        let negative = m.duty_cycle_negative.expect("negative duty");
        assert!((negative - 70.0).abs() < 2.0, "negative duty {negative}");
    }

    #[test]
    fn pulse_widths_of_a_square() {
        let frame = frame_from_volts(&square(10, 100, 0.3), 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        // 30 samples at 1 us each.
        let positive = m.pulse_width_positive.expect("positive width");
        assert!((positive - 30e-6).abs() < 2e-6, "positive {positive}");
        let negative = m.pulse_width_negative.expect("negative width");
        assert!((negative - 70e-6).abs() < 2e-6, "negative {negative}");
    }

    #[test]
    fn hysteresis_rejects_noise_at_the_threshold() {
        // A square wave with noise straddling mid-level. Without hysteresis
        // each noisy sample reads as an edge and the frequency comes back a
        // large multiple of the true one.
        let mut volts = Vec::new();
        for cycle in 0..10 {
            for i in 0..100 {
                let base = if i < 50 { 1.0 } else { -1.0 };
                // Deterministic dither so the test does not depend on a PRNG.
                let noise = if (cycle + i) % 7 == 0 { 0.05 } else { -0.05 };
                volts.push(base + noise);
            }
        }
        let frame = frame_from_volts(&volts, 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        let frequency = m.frequency.expect("frequency");
        // True frequency is 10 kHz.
        assert!(
            (frequency - 10_000.0).abs() < 500.0,
            "noise was counted as edges: {frequency}"
        );
    }

    #[test]
    fn flat_signal_reports_amplitude_but_no_timing() {
        let frame = frame_from_volts(&vec![1.0; 1000], 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        assert!((m.vmax - 1.0).abs() < 0.01);
        assert!(m.vpp.abs() < 0.01);
        // A DC level has no period, and reporting one would be a lie.
        assert!(m.period.is_none());
        assert!(m.frequency.is_none());
    }

    #[test]
    fn single_edge_is_not_enough_for_a_period() {
        let mut volts = vec![-1.0; 500];
        volts.extend(vec![1.0; 500]);
        let frame = frame_from_volts(&volts, 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        assert!(m.period.is_none());
    }

    #[test]
    fn rise_time_of_a_linear_ramp() {
        // Ramp -1 to 1 over 100 samples at 1 MS/s. The 10-90% band is 80% of
        // the ramp, so 80 samples, 80 us.
        let mut volts: Vec<f64> = vec![-1.0; 50];
        volts.extend((0..100).map(|i| -1.0 + 2.0 * i as f64 / 99.0));
        volts.extend(vec![1.0; 50]);
        let frame = frame_from_volts(&volts, 1e6);
        let m = measure_channel(&frame, 0).unwrap();

        let rise = m.rise_time.expect("rise time");
        assert!((rise - 80e-6).abs() < 5e-6, "rise {rise}");
    }

    #[test]
    fn empty_capture_measures_nothing() {
        let frame = frame_from_volts(&[], 1e6);
        assert!(measure_channel(&frame, 0).is_none());
    }

    #[test]
    fn out_of_range_channel_measures_nothing() {
        let frame = frame_from_volts(&sine(10.0, 1000, 1.0, 0.0), 1e6);
        assert!(measure_channel(&frame, 5).is_none());
    }

    #[test]
    fn measurement_names_accept_cli_spellings() {
        assert_eq!(Measurement::parse("vpp"), Some(Measurement::Vpp));
        assert_eq!(Measurement::parse("VPP"), Some(Measurement::Vpp));
        assert_eq!(Measurement::parse("freq"), Some(Measurement::Frequency));
        assert_eq!(
            Measurement::parse("duty-cycle-pos"),
            Some(Measurement::DutyCyclePositive)
        );
        assert_eq!(
            Measurement::parse("pulse_width_neg"),
            Some(Measurement::PulseWidthNegative)
        );
        assert_eq!(Measurement::parse("nonsense"), None);
    }

    #[test]
    fn units_match_the_quantity() {
        assert_eq!(Measurement::Vpp.unit(), "V");
        assert_eq!(Measurement::Frequency.unit(), "Hz");
        assert_eq!(Measurement::Period.unit(), "s");
        assert_eq!(Measurement::DutyCyclePositive.unit(), "%");
    }
}

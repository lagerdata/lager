// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Everything a client needs to draw the scope's controls, in one message.
//!
//! The web UI used to learn each setting with its own round trip and then
//! never again, so a timebase changed from the terminal CLI left the page
//! showing the old one until it was reloaded. The daemon now builds this
//! snapshot after every change and pushes it to subscribers that ask for it,
//! and `GetState` answers the same thing on demand.

use serde::{Deserialize, Serialize};

use crate::{CaptureMode, ChannelId, Coupling, TriggerSlope};

/// How consecutive captures are combined into the one that is published.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AcquisitionMode {
    /// Each capture as it came off the hardware.
    #[default]
    Normal,
    /// The mean of the last `average_count` captures, sample by sample.
    /// Random noise falls by the square root of the count; anything locked to
    /// the trigger stays.
    Average,
    /// The minimum and maximum of every sample interval, so a glitch narrower
    /// than the interval still shows. Needs hardware aggregation, which
    /// `ScopeCapabilities::peak_detect` reports.
    Peak,
}

impl AcquisitionMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            AcquisitionMode::Normal => "normal",
            AcquisitionMode::Average => "average",
            AcquisitionMode::Peak => "peak",
        }
    }
}

/// Whether slow timebases stream continuously instead of capturing blocks.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum RollMode {
    /// Roll at `ROLL_THRESHOLD_S_PER_DIV` and slower while the trigger is in
    /// auto, as a bench scope does. Normal and single keep capturing blocks,
    /// because they wait for a trigger that roll mode would ignore.
    #[default]
    Auto,
    /// Roll at any timebase the scope can stream at, whatever the trigger.
    On,
    /// Never roll.
    Off,
}

impl RollMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            RollMode::Auto => "auto",
            RollMode::On => "on",
            RollMode::Off => "off",
        }
    }
}

/// Time/div at which `RollMode::Auto` starts rolling.
///
/// Half a second a screen. A block capture at that timebase takes half a
/// second to fill and another to show, so the trace updates about once a
/// second; rolling draws it as it arrives.
pub const ROLL_THRESHOLD_S_PER_DIV: f64 = 0.05;

/// Largest `average_count` accepted. Past this the trace takes minutes to
/// settle after any change, which reads as the scope ignoring it.
pub const MAX_AVERAGE_COUNT: u32 = 1024;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TriggerState {
    pub source: ChannelId,
    /// Volts at the probe tip.
    pub level: f64,
    pub slope: TriggerSlope,
    /// Seconds after a trigger during which the next is ignored.
    pub holdoff_s: f64,
    /// Where the trigger sits in the block, 0 to 100 percent from its start.
    pub position_percent: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TimebaseState {
    /// The time/div in effect, which is the request rounded to a sample
    /// interval the hardware has.
    pub time_per_div: f64,
    pub time_offset: f64,
    pub sample_interval_ns: f64,
    pub memory_depth: usize,
    pub roll: RollMode,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AcquisitionState {
    pub mode: AcquisitionMode,
    pub average_count: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChannelState {
    pub channel: ChannelId,
    pub enabled: bool,
    pub volts_per_div: f64,
    pub volts_offset: f64,
    pub coupling: Coupling,
    pub attenuation: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScopeState {
    /// Increments with every change, so a client can tell a new state from a
    /// repeat of one it has already applied.
    pub version: u64,
    pub acquiring: bool,
    /// Streaming in roll mode rather than capturing blocks.
    pub rolling: bool,
    pub capture_mode: CaptureMode,
    pub trigger: TriggerState,
    pub timebase: TimebaseState,
    pub acquisition: AcquisitionState,
    pub channels: Vec<ChannelState>,
    /// Display settings -- persistence, zoom, XY, math, cursors -- that the
    /// daemon keeps for its clients and does not interpret. Kept here so the
    /// terminal CLI can set one and an open web UI follows it.
    pub display: serde_json::Value,
}

/// Merge `patch` into `display`, one level deep.
///
/// A key set to null is removed, so a client can clear one setting without
/// knowing the rest. Anything but an object replaces the whole value, which
/// is what a caller sending a non-object means.
pub fn merge_display(display: &mut serde_json::Value, patch: serde_json::Value) {
    match (display.as_object_mut(), patch) {
        (Some(current), serde_json::Value::Object(changes)) => {
            for (key, value) in changes {
                if value.is_null() {
                    current.remove(&key);
                } else {
                    current.insert(key, value);
                }
            }
        }
        (_, replacement) => *display = replacement,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_patch_sets_only_the_keys_it_names() {
        let mut display = json!({"persistence": 0.5, "xy": false});
        merge_display(&mut display, json!({"xy": true}));
        assert_eq!(display, json!({"persistence": 0.5, "xy": true}));
    }

    #[test]
    fn null_clears_a_key() {
        let mut display = json!({"zoom": {"factor": 4}, "xy": false});
        merge_display(&mut display, json!({"zoom": null}));
        assert_eq!(display, json!({"xy": false}));
    }

    #[test]
    fn a_non_object_replaces_the_whole_value() {
        let mut display = json!({"xy": true});
        merge_display(&mut display, json!({}));
        assert_eq!(display, json!({"xy": true}), "an empty patch changes nothing");
        merge_display(&mut display, json!(null));
        assert_eq!(display, json!(null));
    }

    #[test]
    fn modes_spell_themselves_the_way_the_wire_does() {
        for mode in [AcquisitionMode::Normal, AcquisitionMode::Average, AcquisitionMode::Peak] {
            assert_eq!(
                serde_json::to_value(mode).unwrap(),
                json!(mode.as_str()),
                "{mode:?}"
            );
        }
        for roll in [RollMode::Auto, RollMode::On, RollMode::Off] {
            assert_eq!(serde_json::to_value(roll).unwrap(), json!(roll.as_str()));
        }
    }
}

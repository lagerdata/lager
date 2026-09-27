// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use protocol::{CaptureFrame, CaptureMode, ChannelId, Coupling, ScopeCapabilities, TriggerSlope};

pub mod pico;

pub use pico::PicoScope2000;

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, PartialOrd)]
pub enum CursorType {
    Horizontal,
    #[default]
    Vertical,
}

#[derive(Debug, Clone, PartialEq, PartialOrd)]
pub struct Cursor {
    cursor_type: CursorType,
    position: f64,
    name: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ChannelSettings {
    pub channel_id: ChannelId,
    pub volts_per_div: f64,
    pub volts_offset: f64,
    pub coupling: Coupling,
    pub attenuation: f64,
    pub enabled: bool,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct TriggerSettings {
    pub trigger_level: f64,
    pub trigger_source: ChannelId,
    pub trigger_slope: TriggerSlope,
    pub capture_mode: CaptureMode,
    pub delay: f64,
    pub trigger_position: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct OscilloscopeSettings {
    pub channels: Vec<ChannelSettings>,
    pub trigger: TriggerSettings,
    pub cursors: Vec<Cursor>,
    pub time_per_div: f64,
    pub time_offset: f64,
    pub sample_rate: Option<f64>,
    pub memory_depth: Option<usize>,
    pub bandwidth: Option<f64>,
}

pub trait Oscilloscope: Send + Sync {
    fn enable_channel(&mut self, channel: ChannelId) -> anyhow::Result<()>;
    fn disable_channel(&mut self, channel: ChannelId) -> anyhow::Result<()>;
    fn is_channel_enabled(&self, channel: ChannelId) -> anyhow::Result<bool>;

    fn set_volts_per_div(&mut self, channel: ChannelId, volts_per_div: f64) -> anyhow::Result<()>;
    fn get_volts_per_div(&self, channel: ChannelId) -> anyhow::Result<f64>;

    fn set_volts_offset(&mut self, channel: ChannelId, volts_offset: f64) -> anyhow::Result<()>;
    fn get_volts_offset(&self, channel: ChannelId) -> anyhow::Result<f64>;

    fn set_coupling(&mut self, channel: ChannelId, coupling: Coupling) -> anyhow::Result<()>;
    fn get_coupling(&self, channel: ChannelId) -> anyhow::Result<Coupling>;

    fn set_attenuation(&mut self, channel: ChannelId, attenuation: f64) -> anyhow::Result<()>;
    fn get_attenuation(&self, channel: ChannelId) -> anyhow::Result<f64>;

    //global
    fn set_trigger_level(&mut self, trigger_level: f64) -> anyhow::Result<()>;
    fn get_trigger_level(&self) -> anyhow::Result<f64>;

    fn set_time_per_div(&mut self, time_per_div: f64) -> anyhow::Result<()>;
    fn get_time_per_div(&self) -> anyhow::Result<f64>;
    fn set_time_offset(&mut self, time_offset: f64) -> anyhow::Result<()>;
    fn get_time_offset(&self) -> anyhow::Result<f64>;

    fn set_trigger_source(&mut self, trigger_source: ChannelId) -> anyhow::Result<()>;
    fn get_trigger_source(&self) -> anyhow::Result<ChannelId>;

    fn set_trigger_slope(&mut self, trigger_slope: TriggerSlope) -> anyhow::Result<()>;
    fn get_trigger_slope(&self) -> anyhow::Result<TriggerSlope>;

    fn set_capture_mode(&mut self, capture_mode: CaptureMode) -> anyhow::Result<()>;
    fn get_capture_mode(&self) -> anyhow::Result<CaptureMode>;

    fn set_cursor_position(&mut self, cursor: Cursor) -> anyhow::Result<()>;
    fn get_cursor_position(&self, cursor: Cursor) -> anyhow::Result<f64>;

    //measure
    fn measure_horizontal_cursor_delta(&self) -> anyhow::Result<f64>;
    fn measure_vertical_cursor_delta(&self) -> anyhow::Result<f64>;
    fn measure_duty_cycle(&self, channel: ChannelId) -> anyhow::Result<f64>;
    fn measure_frequency(&self, channel: ChannelId) -> anyhow::Result<f64>;
    fn measure_period(&self, channel: ChannelId) -> anyhow::Result<f64>;
    fn measure_rms(&self, channel: ChannelId) -> anyhow::Result<f64>;
    fn measure_peak_to_peak(&self, channel: ChannelId) -> anyhow::Result<f64>;
    fn measure_average(&self, channel: ChannelId) -> anyhow::Result<f64>;
    fn measure_min(&self, channel: ChannelId) -> anyhow::Result<f64>;

    //data
    fn get_data(&self, channel: ChannelId) -> anyhow::Result<Vec<f64>>;

    //settings
    fn get_sample_rate(&self) -> anyhow::Result<f64>;
    fn get_memory_depth(&self) -> anyhow::Result<usize>;
    fn get_bandwidth(&self) -> anyhow::Result<f64>;
    fn get_channel_count(&self) -> anyhow::Result<usize>;
    fn get_trigger_position(&self) -> anyhow::Result<f64>;

    //streaming
    fn start_triggered_capture(&mut self, trigger_position_percent: f64) -> anyhow::Result<()>;
    fn stop_triggered_capture(&mut self) -> anyhow::Result<()>;
    fn is_ready(&self) -> anyhow::Result<bool>;
    fn get_triggered_data(&self) -> anyhow::Result<CaptureFrame>;

    /// Trigger immediately rather than waiting for the configured condition.
    fn force_trigger(&mut self) -> anyhow::Result<()>;

    /// What this specific unit supports, detected at open time. Drives which
    /// controls the UI shows and which commands the CLI accepts.
    fn capabilities(&self) -> anyhow::Result<ScopeCapabilities>;

    /// How long to wait before re-checking readiness when the driver offers
    /// no completion callback. Derived from the timebase so a slow capture
    /// does not spin and a fast one is not throttled by a fixed interval.
    fn suggested_poll_interval(&self) -> std::time::Duration {
        std::time::Duration::from_millis(2)
    }

    /// Arm the next block after one has been read, nothing having changed.
    ///
    /// The acquisition loop calls this once per capture, so it is the hot
    /// path: a driver that reprograms channels and trigger on every arm spends
    /// the gap between blocks on setup the hardware already has. The default
    /// arms the long way, for drivers with nothing faster.
    fn rearm(&mut self) -> anyhow::Result<()> {
        let position = self.get_trigger_position()?;
        self.start_triggered_capture(position)
    }

    /// Samples in a block at the current settings, answered from what the
    /// driver already knows. `get_memory_depth` may ask the hardware.
    fn current_memory_depth(&self) -> anyhow::Result<usize> {
        self.get_memory_depth()
    }

    /// The time/div last asked for, before rounding to a sample interval.
    /// Roll mode has no such rounding, so it works from this.
    fn requested_time_per_div(&self) -> anyhow::Result<f64> {
        self.get_time_per_div()
    }

    /// Whether this driver can stream continuously, for roll mode.
    fn supports_roll(&self) -> bool {
        false
    }

    /// Start streaming minimum/maximum pairs, one per `plan.bucket_ns`.
    fn start_roll(&mut self, _plan: &RollPlan) -> anyhow::Result<RollInfo> {
        anyhow::bail!("this scope cannot stream, so it has no roll mode")
    }

    /// Append whatever pairs have arrived since the last call to `sink`,
    /// returning how many per channel.
    fn poll_roll(&mut self, _sink: &mut RollSink) -> anyhow::Result<usize> {
        anyhow::bail!("this scope cannot stream, so it has no roll mode")
    }

    fn stop_roll(&mut self) -> anyhow::Result<()> {
        Ok(())
    }
}

/// What roll mode asks the driver for.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RollPlan {
    /// Time each minimum/maximum pair should cover.
    pub bucket_ns: f64,
}

/// What the driver actually set up, which can differ from the plan: the
/// bucket is a whole number of the hardware's sample intervals.
#[derive(Debug, Clone, PartialEq)]
pub struct RollInfo {
    pub bucket_ns: f64,
    /// The channels streamed, in the order `RollSink` fills them.
    pub channels: Vec<protocol::ChannelFrame>,
    pub resolution_bits: u8,
}

/// Pairs delivered by `poll_roll`, one list per streamed channel.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct RollSink {
    pub pairs: Vec<Vec<(i16, i16)>>,
}

impl RollSink {
    pub fn clear(&mut self) {
        for pairs in &mut self.pairs {
            pairs.clear();
        }
    }
}

/// How long auto mode waits for a trigger before capturing anyway.
///
/// Scaled to the block, where it was a fixed 500 ms on one family and 100 ms
/// on the other. Fixed at 500 ms, a scope in auto with nothing crossing the
/// level refreshed twice a second, which reads as a frozen screen. Twice the
/// block plus a margin still gives any signal that repeats within the
/// window time to trigger, and the floor keeps a fast timebase from
/// auto-triggering between the edges of an ordinary signal.
pub fn auto_trigger_timeout_ms(block_seconds: f64) -> u32 {
    let block_ms = (block_seconds * 1000.0).max(0.0);
    ((2.0 * block_ms + 20.0).ceil() as u32).clamp(40, 500)
}

#[cfg(test)]
mod tests {
    use super::auto_trigger_timeout_ms;

    #[test]
    fn a_fast_block_waits_the_floor() {
        assert_eq!(auto_trigger_timeout_ms(80e-6), 40);
        assert_eq!(auto_trigger_timeout_ms(0.0), 40);
    }

    #[test]
    fn a_slower_block_waits_about_two_windows() {
        // 1 ms/div: a 10 ms block.
        assert_eq!(auto_trigger_timeout_ms(0.010), 40);
        // 10 ms/div: a 100 ms block.
        assert_eq!(auto_trigger_timeout_ms(0.100), 220);
    }

    #[test]
    fn the_wait_is_capped_where_roll_mode_takes_over() {
        assert_eq!(auto_trigger_timeout_ms(0.5), 500);
        assert_eq!(auto_trigger_timeout_ms(10.0), 500);
    }
}

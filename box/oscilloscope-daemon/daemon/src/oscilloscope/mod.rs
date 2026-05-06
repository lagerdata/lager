// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use anyhow::Result;
use protocol::{CaptureMode, ChannelId, Coupling, TriggerSlope, TriggeredCapture};
// use std::sync::{Arc, Mutex}; // Not used in this file

#[cfg(feature = "ps2000")]
pub mod pico;

#[cfg(feature = "ps2000a")]
pub mod pico;

#[cfg(feature = "mso5000")]
pub mod rigol;

#[cfg(feature = "generic")]
pub mod generic;

// Only re-export the oscilloscope that is enabled
#[cfg(feature = "ps2000")]
pub use pico::PicoScope2000;

// Only re-export the oscilloscope that is enabled
#[cfg(feature = "ps2000a")]
pub use pico::PicoScope2000A;

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
    fn get_triggered_data(&self) -> anyhow::Result<TriggeredCapture>;
}

pub fn create_oscilloscope() -> Result<Box<dyn Oscilloscope>> {
    #[cfg(feature = "ps2000")]
    {
        Ok(Box::new(PicoScope2000::new()?))
    }
    #[cfg(all(not(feature = "ps2000"), feature = "ps2000a"))]
    {
        Ok(Box::new(PicoScope2000A::new()?))
    }
    #[cfg(not(any(feature = "ps2000", feature = "ps2000a")))]
    {
        Err(anyhow::anyhow!("No oscilloscope found"))
    }
}

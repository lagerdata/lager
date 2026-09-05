// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Block-capture driver for the modern PicoScope families.
//!
//! This is the layer that turns [`PicoModernApi`] -- a thin, mechanical
//! wrapper over four near-identical C APIs -- into an [`Oscilloscope`] the
//! rest of the daemon can drive. Everything family-specific was already
//! absorbed by the vtable, so nothing here branches on the series; a 2206B
//! and a 5444D differ only in the capabilities they report.
//!
//! # How a capture happens
//!
//! 1. `set_channel` for each channel, translating volts/div into the
//!    tightest range that will not clip.
//! 2. `set_simple_trigger`, with the level converted from volts to ADC
//!    counts against that channel's range.
//! 3. `set_data_buffer` per enabled channel, pointing at buffers this struct
//!    owns, then `run_block`.
//! 4. Poll `is_ready`, then `get_values` and assemble a [`CaptureFrame`].
//!
//! # Why the timebase is searched rather than computed
//!
//! Each family documents its own timebase formula, and each formula changes
//! with the ADC resolution and the number of enabled channels. Encoding four
//! of those (and their exceptions) is how the legacy driver ended up with a
//! sample-interval bug that went unnoticed for a long time. `GetTimebase2`
//! is the device's own answer for the exact configuration currently applied,
//! so this asks it instead -- via binary search, since the interval rises
//! monotonically with the timebase index.

use std::time::Duration;

use anyhow::{anyhow, bail, Context, Result};
use protocol::capabilities::ScopeCapabilities;
use protocol::lscp::FLAG_TRIGGERED;
use protocol::{
    CaptureFrame, CaptureMode, ChannelFrame, ChannelId, Coupling as WireCoupling, TriggerSlope,
};

use super::modern::PicoModernApi;
use super::types::{Coupling, Range, RatioMode, ThresholdDirection};
use crate::oscilloscope::{
    ChannelSettings, Cursor, Oscilloscope, OscilloscopeSettings, TriggerSettings,
};

/// Horizontal divisions the UI draws. The timebase is chosen so one capture
/// fills exactly this many.
const HORIZONTAL_DIVISIONS: f64 = 10.0;
/// Vertical divisions, used to turn volts/div into a full-scale range.
const VERTICAL_DIVISIONS: f64 = 8.0;

/// Samples per capture.
///
/// Sized to fill a typical plot width several times over without making
/// each frame expensive: at 100 captures/second this is 200k samples/second
/// per channel on the wire, which the LSCP encoder handles comfortably.
const DEFAULT_CAPTURE_SAMPLES: u32 = 2000;

/// Largest timebase index to consider during the search.
///
/// The APIs take a `uint32_t`, but the slowest timebase any of these
/// families offers is far below this, and searching the whole space would
/// add pointless round trips. This is ~1.4 hours per sample at the 4000a's
/// slowest, so nothing usable lies beyond it.
const MAX_TIMEBASE: u32 = 1 << 24;

/// How long `auto` mode waits for a trigger before capturing anyway.
const AUTO_TRIGGER_MS: i16 = 100;

pub struct PicoScopeModern {
    api: Box<dyn PicoModernApi>,
    handle: i16,
    capabilities: ScopeCapabilities,
    settings: OscilloscopeSettings,

    /// One buffer per enabled channel, in the same order as `enabled()`.
    ///
    /// These are owned here rather than allocated per capture because the
    /// driver keeps the pointers it was handed in `set_data_buffer` until
    /// the matching `get_values`. A per-capture `Vec` would be freed while
    /// the driver still held a pointer to it.
    buffers: Vec<Vec<i16>>,

    timebase: u32,
    interval_seconds: f64,
    samples_per_capture: u32,
    pre_trigger_samples: u32,
    /// Full-scale ADC count, read from the device: it varies with
    /// resolution, so it cannot be a constant.
    max_adc_count: i16,
    resolution_bits: u8,
    is_capturing: bool,
    /// The driver's own estimate for the last armed capture, which sets the
    /// readiness poll interval.
    expected_capture: Duration,
    /// Set when the hardware configuration changed and must be re-applied
    /// before the next capture.
    dirty: bool,
}

impl PicoScopeModern {
    /// Adopt an already-open unit, as returned by [`super::detect::detect`].
    ///
    /// Takes over closing the handle, including when this fails: the struct
    /// is assembled before anything fallible runs, so every error path goes
    /// through `Drop`. That matters because a leaked PicoScope handle needs
    /// a physical replug before the next open succeeds, which on a remote
    /// box means a site visit.
    pub fn adopt(
        api: Box<dyn PicoModernApi>,
        handle: i16,
        capabilities: ScopeCapabilities,
    ) -> Result<Self> {
        let resolution_bits = capabilities.resolution.current_bits;

        let channels = (0..capabilities.analog_channels)
            .map(|i| ChannelSettings {
                channel_id: ChannelId::Alphabetic((b'A' + i) as char),
                volts_per_div: 1.0,
                volts_offset: 0.0,
                coupling: WireCoupling::DC,
                attenuation: 1.0,
                // Only channel A starts on. Enabling every channel of an
                // 8-channel 4824 by default would restrict the timebase
                // range before the user has asked for anything.
                enabled: i == 0,
            })
            .collect();

        let settings = OscilloscopeSettings {
            channels,
            trigger: TriggerSettings {
                trigger_level: 0.0,
                trigger_source: ChannelId::Alphabetic('A'),
                trigger_slope: TriggerSlope::Rising,
                capture_mode: CaptureMode::Auto,
                delay: 0.0,
                trigger_position: 0.5,
            },
            cursors: Vec::new(),
            time_per_div: 1e-3,
            time_offset: 0.0,
            sample_rate: None,
            memory_depth: Some(DEFAULT_CAPTURE_SAMPLES as usize),
            bandwidth: None,
        };

        let mut scope = Self {
            api,
            handle,
            capabilities,
            settings,
            buffers: Vec::new(),
            timebase: 0,
            interval_seconds: 0.0,
            samples_per_capture: DEFAULT_CAPTURE_SAMPLES,
            pre_trigger_samples: DEFAULT_CAPTURE_SAMPLES / 2,
            // Replaced immediately below. Reading it from the device is
            // fallible, and doing that after construction is what puts the
            // handle under `Drop` for the rest of this function.
            max_adc_count: 0,
            resolution_bits,
            is_capturing: false,
            expected_capture: Duration::from_millis(0),
            dirty: true,
        };

        scope.max_adc_count = scope
            .api
            .maximum_value(handle)
            .context("reading the device's full-scale ADC count")?;
        scope.apply_configuration()?;
        Ok(scope)
    }

    /// Open whichever modern PicoScope is attached.
    pub fn open(serial: Option<&str>) -> Result<Self> {
        let found = super::detect::detect(serial)?;
        Self::adopt(found.api, found.handle, found.capabilities)
    }

    fn enabled_indices(&self) -> Vec<usize> {
        self.settings
            .channels
            .iter()
            .enumerate()
            .filter(|(_, c)| c.enabled)
            .map(|(i, _)| i)
            .collect()
    }

    /// Hardware channel index for a channel id: A is 0, B is 1, and so on.
    fn channel_index(&self, id: ChannelId) -> Result<u8> {
        match id {
            ChannelId::Alphabetic(c) => {
                let index = (c.to_ascii_uppercase() as u8).wrapping_sub(b'A');
                if index < self.capabilities.analog_channels {
                    Ok(index)
                } else {
                    bail!(
                        "this {} has channels A-{}, so there is no channel {c}",
                        self.capabilities.model,
                        (b'A' + self.capabilities.analog_channels - 1) as char
                    )
                }
            }
            // 1-based numeric ids, as the CLI and saved nets use.
            ChannelId::Numeric(n) if n >= 1 && n <= self.capabilities.analog_channels => {
                Ok(n - 1)
            }
            ChannelId::Numeric(n) => bail!(
                "this {} has {} channels, so there is no channel {n}",
                self.capabilities.model,
                self.capabilities.analog_channels
            ),
        }
    }

    fn settings_for(&self, id: ChannelId) -> Result<&ChannelSettings> {
        self.settings
            .channels
            .iter()
            .find(|c| c.channel_id == id)
            .ok_or_else(|| anyhow!("channel {id} is not on this scope"))
    }

    fn settings_for_mut(&mut self, id: ChannelId) -> Result<&mut ChannelSettings> {
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == id)
            .ok_or_else(|| anyhow!("channel {id} is not on this scope"))
    }

    /// The range a channel's volts/div and probe attenuation imply.
    ///
    /// Attenuation divides the signal before it reaches the input, so a 10x
    /// probe showing 1 V/div only needs a 0.1 V/div range at the connector.
    fn range_for(channel: &ChannelSettings) -> Range {
        let at_input = channel.volts_per_div / channel.attenuation.max(f64::MIN_POSITIVE);
        Range::smallest_containing(at_input * VERTICAL_DIVISIONS / 2.0)
    }

    /// Push channel and trigger settings to the device, then pick a timebase.
    ///
    /// Ordering matters: the legal timebase range depends on how many
    /// channels are enabled and at what resolution, so channels are applied
    /// first and the timebase searched afterwards.
    fn apply_configuration(&mut self) -> Result<()> {
        for channel in &self.settings.channels {
            let index = match channel.channel_id {
                ChannelId::Alphabetic(c) => (c.to_ascii_uppercase() as u8).wrapping_sub(b'A'),
                ChannelId::Numeric(n) => n.saturating_sub(1),
            };
            if index >= self.capabilities.analog_channels {
                continue;
            }
            self.api.set_channel(
                self.handle,
                index,
                channel.enabled,
                Coupling::from(channel.coupling),
                Self::range_for(channel),
                // The driver takes the offset at the input, so it is scaled
                // the same way the range is.
                (channel.volts_offset / channel.attenuation.max(f64::MIN_POSITIVE)) as f32,
            )?;
        }

        self.select_timebase()?;
        self.apply_trigger()?;

        // Sized after the timebase search, because a configuration change
        // can alter how many channels are enabled.
        let count = self.enabled_indices().len();
        self.buffers = vec![vec![0i16; self.samples_per_capture as usize]; count];

        self.dirty = false;
        Ok(())
    }

    fn apply_trigger(&mut self) -> Result<()> {
        let trigger = &self.settings.trigger;
        let source = self.channel_index(trigger.trigger_source)?;
        let source_settings = self.settings_for(trigger.trigger_source)?;

        let threshold = self.volts_to_counts(trigger.trigger_level, source_settings);

        // Auto mode captures anyway after a timeout; normal waits forever.
        // Single is armed the same as normal -- what makes it single is that
        // the acquisition loop does not re-arm.
        let auto_trigger_ms = match trigger.capture_mode {
            CaptureMode::Auto => AUTO_TRIGGER_MS,
            CaptureMode::Normal | CaptureMode::Single => 0,
        };

        self.api.set_simple_trigger(
            self.handle,
            trigger.trigger_slope != TriggerSlope::Neither,
            source,
            threshold,
            ThresholdDirection::from(trigger.trigger_slope),
            0,
            auto_trigger_ms,
        )
    }

    /// Convert a voltage to ADC counts on a channel's current range.
    ///
    /// A level beyond full scale saturates rather than wrapping, which is
    /// what the user meant: the hardware can never reach it, so the nearest
    /// reachable threshold is the right answer. Rust's float-to-int `as`
    /// saturates for us, and the test below pins that, since a wrapping
    /// cast would arm the trigger at the opposite end of the range.
    fn volts_to_counts(&self, volts: f64, channel: &ChannelSettings) -> i16 {
        let full_scale = Self::range_for(channel).full_scale_volts()
            * channel.attenuation.max(f64::MIN_POSITIVE);
        if full_scale <= 0.0 {
            return 0;
        }
        (volts / full_scale * f64::from(self.max_adc_count)) as i16
    }

    /// Sample interval that fits the requested window into one capture.
    pub fn target_interval_seconds(time_per_div: f64, samples: u32) -> f64 {
        (time_per_div * HORIZONTAL_DIVISIONS) / f64::from(samples.max(1))
    }

    /// Find the timebase whose sample interval best matches the requested
    /// time/div, by asking the device rather than computing it.
    ///
    /// The interval increases monotonically with the timebase index, so this
    /// binary-searches for the smallest index that is at least the target --
    /// erring towards a window slightly wider than requested, since a
    /// narrower one would cut off the end of the waveform the user asked to
    /// see.
    fn select_timebase(&mut self) -> Result<()> {
        let target = Self::target_interval_seconds(
            self.settings.time_per_div,
            self.samples_per_capture,
        );

        let mut low = 0u32;
        let mut high = MAX_TIMEBASE;
        let mut best: Option<(u32, f64, u32)> = None;

        while low <= high {
            let mid = low + (high - low) / 2;
            match self.api.get_timebase(self.handle, mid, self.samples_per_capture) {
                Ok(info) if info.interval_seconds >= target => {
                    best = Some((mid, info.interval_seconds, info.max_samples));
                    if mid == 0 {
                        break;
                    }
                    high = mid - 1;
                }
                Ok(_) => low = mid + 1,
                // An invalid timebase at this configuration means "too
                // fast for the channels that are on", so the usable range
                // starts above it.
                Err(_) => low = mid + 1,
            }
            if low > MAX_TIMEBASE {
                break;
            }
        }

        let (timebase, interval, max_samples) = best.ok_or_else(|| {
            anyhow!(
                "no timebase on this {} can produce {} s/div with {} channels \
                 enabled; try a faster time/div or turn a channel off",
                self.capabilities.model,
                self.settings.time_per_div,
                self.enabled_indices().len()
            )
        })?;

        // The device may not have memory for the requested block at this
        // timebase. Shrinking beats failing: a shorter capture still shows
        // the waveform, and the frame reports its own length.
        if max_samples > 0 && max_samples < self.samples_per_capture {
            tracing::debug!(
                requested = self.samples_per_capture,
                available = max_samples,
                "capture shortened to fit device memory"
            );
            self.samples_per_capture = max_samples;
        }

        self.timebase = timebase;
        self.interval_seconds = interval;
        self.pre_trigger_samples =
            (f64::from(self.samples_per_capture) * self.settings.trigger.trigger_position) as u32;
        self.settings.sample_rate = Some(1.0 / interval);

        Ok(())
    }

    fn ensure_applied(&mut self) -> Result<()> {
        if self.dirty {
            self.apply_configuration()?;
        }
        Ok(())
    }

    /// Arm one block capture.
    fn arm(&mut self) -> Result<()> {
        self.ensure_applied()?;

        // Re-register buffers on every capture. The driver forgets them on
        // stop, and re-registering is cheap next to the capture itself.
        let enabled = self.enabled_indices();
        for (slot, &channel_index) in enabled.iter().enumerate() {
            let index = match self.settings.channels[channel_index].channel_id {
                ChannelId::Alphabetic(c) => (c.to_ascii_uppercase() as u8).wrapping_sub(b'A'),
                ChannelId::Numeric(n) => n.saturating_sub(1),
            };
            // Split the borrow: the API call needs &self while the buffer
            // needs &mut, and they are different fields.
            let buffer = &mut self.buffers[slot];
            self.api
                .set_data_buffer(self.handle, index, buffer)
                .with_context(|| format!("registering the capture buffer for channel {index}"))?;
        }

        let post = self.samples_per_capture - self.pre_trigger_samples;
        self.expected_capture =
            self.api
                .run_block(self.handle, self.pre_trigger_samples, post, self.timebase)?;
        self.is_capturing = true;
        Ok(())
    }
}

impl Oscilloscope for PicoScopeModern {
    fn enable_channel(&mut self, channel: ChannelId) -> Result<()> {
        self.channel_index(channel)?;
        self.settings_for_mut(channel)?.enabled = true;
        self.dirty = true;
        self.apply_configuration()
    }

    fn disable_channel(&mut self, channel: ChannelId) -> Result<()> {
        self.channel_index(channel)?;
        self.settings_for_mut(channel)?.enabled = false;
        self.dirty = true;
        self.apply_configuration()
    }

    fn is_channel_enabled(&self, channel: ChannelId) -> Result<bool> {
        Ok(self.settings_for(channel)?.enabled)
    }

    fn set_volts_per_div(&mut self, channel: ChannelId, volts_per_div: f64) -> Result<()> {
        if volts_per_div <= 0.0 {
            bail!("volts per division must be positive, got {volts_per_div}");
        }
        self.settings_for_mut(channel)?.volts_per_div = volts_per_div;
        self.dirty = true;
        self.apply_configuration()
    }

    fn get_volts_per_div(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings_for(channel)?.volts_per_div)
    }

    fn set_volts_offset(&mut self, channel: ChannelId, volts_offset: f64) -> Result<()> {
        self.settings_for_mut(channel)?.volts_offset = volts_offset;
        self.dirty = true;
        self.apply_configuration()
    }

    fn get_volts_offset(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings_for(channel)?.volts_offset)
    }

    fn set_coupling(&mut self, channel: ChannelId, coupling: WireCoupling) -> Result<()> {
        self.settings_for_mut(channel)?.coupling = coupling;
        self.dirty = true;
        self.apply_configuration()
    }

    fn get_coupling(&self, channel: ChannelId) -> Result<WireCoupling> {
        Ok(self.settings_for(channel)?.coupling)
    }

    fn set_attenuation(&mut self, channel: ChannelId, attenuation: f64) -> Result<()> {
        if attenuation <= 0.0 {
            bail!("probe attenuation must be positive, got {attenuation}");
        }
        self.settings_for_mut(channel)?.attenuation = attenuation;
        self.dirty = true;
        self.apply_configuration()
    }

    fn get_attenuation(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings_for(channel)?.attenuation)
    }

    fn set_trigger_level(&mut self, trigger_level: f64) -> Result<()> {
        self.settings.trigger.trigger_level = trigger_level;
        self.apply_trigger()
    }

    fn get_trigger_level(&self) -> Result<f64> {
        Ok(self.settings.trigger.trigger_level)
    }

    fn set_time_per_div(&mut self, time_per_div: f64) -> Result<()> {
        if time_per_div <= 0.0 {
            bail!("time per division must be positive, got {time_per_div}");
        }
        self.settings.time_per_div = time_per_div;
        self.select_timebase()
    }

    fn get_time_per_div(&self) -> Result<f64> {
        Ok(self.settings.time_per_div)
    }

    fn set_time_offset(&mut self, time_offset: f64) -> Result<()> {
        self.settings.time_offset = time_offset;
        Ok(())
    }

    fn get_time_offset(&self) -> Result<f64> {
        Ok(self.settings.time_offset)
    }

    fn set_trigger_source(&mut self, trigger_source: ChannelId) -> Result<()> {
        self.channel_index(trigger_source)?;
        self.settings.trigger.trigger_source = trigger_source;
        self.apply_trigger()
    }

    fn get_trigger_source(&self) -> Result<ChannelId> {
        Ok(self.settings.trigger.trigger_source)
    }

    fn set_trigger_slope(&mut self, trigger_slope: TriggerSlope) -> Result<()> {
        self.settings.trigger.trigger_slope = trigger_slope;
        self.apply_trigger()
    }

    fn get_trigger_slope(&self) -> Result<TriggerSlope> {
        Ok(self.settings.trigger.trigger_slope)
    }

    fn set_capture_mode(&mut self, capture_mode: CaptureMode) -> Result<()> {
        self.settings.trigger.capture_mode = capture_mode;
        self.apply_trigger()
    }

    fn get_capture_mode(&self) -> Result<CaptureMode> {
        Ok(self.settings.trigger.capture_mode)
    }

    fn set_cursor_position(&mut self, _cursor: Cursor) -> Result<()> {
        // Cursors are a front-panel concept. A PicoScope has no display, so
        // the UI draws its own and the daemon is not involved.
        bail!("a PicoScope has no on-screen cursors; measure from the capture instead")
    }

    fn get_cursor_position(&self, _cursor: Cursor) -> Result<f64> {
        bail!("a PicoScope has no on-screen cursors")
    }

    fn measure_horizontal_cursor_delta(&self) -> Result<f64> {
        bail!("a PicoScope has no on-screen cursors")
    }

    fn measure_vertical_cursor_delta(&self) -> Result<f64> {
        bail!("a PicoScope has no on-screen cursors")
    }

    fn measure_duty_cycle(&self, channel: ChannelId) -> Result<f64> {
        self.measure(channel, protocol::Measurement::DutyCyclePositive)
    }

    fn measure_frequency(&self, channel: ChannelId) -> Result<f64> {
        self.measure(channel, protocol::Measurement::Frequency)
    }

    fn measure_period(&self, channel: ChannelId) -> Result<f64> {
        self.measure(channel, protocol::Measurement::Period)
    }

    fn measure_rms(&self, channel: ChannelId) -> Result<f64> {
        self.measure(channel, protocol::Measurement::Vrms)
    }

    fn measure_peak_to_peak(&self, channel: ChannelId) -> Result<f64> {
        self.measure(channel, protocol::Measurement::Vpp)
    }

    fn measure_average(&self, channel: ChannelId) -> Result<f64> {
        self.measure(channel, protocol::Measurement::Vavg)
    }

    fn measure_min(&self, channel: ChannelId) -> Result<f64> {
        self.measure(channel, protocol::Measurement::Vmin)
    }

    fn get_data(&self, channel: ChannelId) -> Result<Vec<f64>> {
        let frame = self.get_triggered_data()?;
        let index = frame
            .channels
            .iter()
            .position(|c| c.channel == channel)
            .ok_or_else(|| anyhow!("channel {channel} is not enabled"))?;
        let per_channel = frame.samples_per_channel as usize;
        let start = index * per_channel;
        Ok(frame.samples[start..start + per_channel]
            .iter()
            .map(|&count| {
                f64::from(count) * f64::from(frame.channels[index].scale_v_per_count)
                    + f64::from(frame.channels[index].offset_v)
            })
            .collect())
    }

    fn get_sample_rate(&self) -> Result<f64> {
        if self.interval_seconds <= 0.0 {
            bail!("no timebase has been selected yet");
        }
        Ok(1.0 / self.interval_seconds)
    }

    fn get_memory_depth(&self) -> Result<usize> {
        Ok(self.samples_per_capture as usize)
    }

    fn get_bandwidth(&self) -> Result<f64> {
        self.capabilities
            .bandwidth_hz
            .ok_or_else(|| anyhow!("this model does not report its bandwidth"))
    }

    fn get_channel_count(&self) -> Result<usize> {
        Ok(self.capabilities.analog_channels as usize)
    }

    fn get_trigger_position(&self) -> Result<f64> {
        Ok(self.settings.trigger.trigger_position)
    }

    fn start_triggered_capture(&mut self, trigger_position_percent: f64) -> Result<()> {
        self.settings.trigger.trigger_position = trigger_position_percent.clamp(0.0, 1.0);
        self.pre_trigger_samples = (f64::from(self.samples_per_capture)
            * self.settings.trigger.trigger_position) as u32;
        self.arm()
    }

    fn stop_triggered_capture(&mut self) -> Result<()> {
        self.is_capturing = false;
        self.api.stop(self.handle)
    }

    fn is_ready(&self) -> Result<bool> {
        if !self.is_capturing {
            return Ok(false);
        }
        self.api.is_ready(self.handle)
    }

    fn get_triggered_data(&self) -> Result<CaptureFrame> {
        let enabled = self.enabled_indices();
        if enabled.is_empty() {
            bail!("no channel is enabled, so there is nothing to capture");
        }

        let result = self.api.get_values(
            self.handle,
            0,
            self.samples_per_capture,
            RatioMode::None,
        )?;

        // Trust the driver's count over the request: returning fewer is
        // legal, and a frame whose declared length disagrees with its
        // payload cannot be decoded.
        let returned = result.samples.min(self.samples_per_capture) as usize;

        let mut channels = Vec::with_capacity(enabled.len());
        let mut samples = Vec::with_capacity(returned * enabled.len());
        let mut overflow_mask = 0u16;

        for (slot, &channel_index) in enabled.iter().enumerate() {
            let channel = &self.settings.channels[channel_index];
            let range = Self::range_for(channel);

            // Counts stay raw on the wire. This factor is how a client turns
            // one back into volts, with the probe attenuation folded in so
            // the client does not need to know about it.
            let scale_v_per_count = (range.full_scale_volts()
                * channel.attenuation.max(f64::MIN_POSITIVE)
                / f64::from(self.max_adc_count)) as f32;

            channels.push(ChannelFrame {
                channel: channel.channel_id,
                range_code: range.code() as u8,
                coupling: channel.coupling,
                scale_v_per_count,
                offset_v: channel.volts_offset as f32,
            });

            // Channel-major, which is what lets the decoder hand out a
            // zero-copy view per channel.
            samples.extend_from_slice(&self.buffers[slot][..returned]);

            // The driver's overflow word is indexed by hardware channel; the
            // frame's mask is indexed by position in `channels`, so a client
            // can pair it with the descriptors it was sent.
            let hardware_index = match channel.channel_id {
                ChannelId::Alphabetic(c) => (c.to_ascii_uppercase() as u8).wrapping_sub(b'A'),
                ChannelId::Numeric(n) => n.saturating_sub(1),
            };
            if result.channel_overflowed(hardware_index) {
                overflow_mask |= 1 << slot;
            }
        }

        let pre = self.pre_trigger_samples.min(returned as u32);
        Ok(CaptureFrame {
            // Numbered by the acquisition loop, the only thing that can do
            // it monotonically across clients.
            seq: 0,
            capture_mono_ns: super::ps2000::monotonic_ns(),
            sample_interval_ns: self.interval_seconds * 1e9,
            pre_trigger_samples: pre,
            post_trigger_samples: returned as u32 - pre,
            samples_per_channel: returned as u32,
            resolution_bits: self.resolution_bits,
            overflow_mask,
            flags: FLAG_TRIGGERED,
            channels,
            samples,
        })
    }

    fn force_trigger(&mut self) -> Result<()> {
        // None of these families has a ForceTrigger entry point. The
        // equivalent is to re-arm in auto mode, which captures after the
        // timeout whether or not the condition is met.
        let previous = self.settings.trigger.capture_mode;
        self.settings.trigger.capture_mode = CaptureMode::Auto;
        let result = self.apply_trigger().and_then(|()| self.arm());
        self.settings.trigger.capture_mode = previous;
        result
    }

    fn capabilities(&self) -> Result<ScopeCapabilities> {
        Ok(self.capabilities.clone())
    }

    fn suggested_poll_interval(&self) -> Duration {
        // The driver told us how long the capture will take when it was
        // armed. Polling faster cannot produce data; a tenth of the expected
        // duration keeps the added latency under 10% of the capture time,
        // clamped so a sub-millisecond capture does not spin a core and a
        // very slow one still feels responsive to a stop request.
        let tenth = self.expected_capture.as_millis() as u64 / 10;
        Duration::from_millis(tenth.clamp(1, 20))
    }
}

impl PicoScopeModern {
    /// Compute one measurement from the most recent capture.
    fn measure(&self, channel: ChannelId, which: protocol::Measurement) -> Result<f64> {
        let frame = self.get_triggered_data()?;
        let index = frame
            .channels
            .iter()
            .position(|c| c.channel == channel)
            .ok_or_else(|| {
                anyhow!("channel {channel} is not enabled, so there is nothing to measure")
            })?;
        let set = protocol::measure::measure_channel(&frame, index)
            .ok_or_else(|| anyhow!("capture held no samples for channel {channel}"))?;
        set.get(which).ok_or_else(|| {
            anyhow!(
                "{which:?} needs at least one full cycle in the capture window; \
                 try a slower time/div"
            )
        })
    }
}

impl Drop for PicoScopeModern {
    fn drop(&mut self) {
        // Without this the USB handle leaks and the next open fails until
        // the device is physically replugged, which on a remote box means a
        // site visit.
        if self.is_capturing {
            let _ = self.api.stop(self.handle);
        }
        if let Err(e) = self.api.close(self.handle) {
            tracing::warn!(error = %e, "could not close the PicoScope cleanly");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::oscilloscope::pico::modern::{CaptureResult, TimebaseInfo};
    use crate::oscilloscope::pico::types::{DeviceResolution, UnitInfo};
    use protocol::capabilities::ResolutionSupport;
    use protocol::DriverFamily;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::{Arc, Mutex};

    /// A stand-in for a real device.
    ///
    /// The point of the vtable is that the driver above never touches the
    /// FFI directly, so the whole capture path -- range selection, timebase
    /// search, trigger conversion, frame assembly -- can be exercised here
    /// with no PicoScope attached. `interval_for` models the one behaviour
    /// the timebase search depends on: interval rising with the index.
    struct MockScope {
        calls: Arc<Mutex<Vec<String>>>,
        /// Sample data handed back, per hardware channel.
        channel_data: Mutex<Vec<Vec<i16>>>,
        /// Registered buffers, so get_values can fill them as the real
        /// driver does.
        registered: Mutex<Vec<(u8, *mut i16, usize)>>,
        max_adc: i16,
        overflow: Mutex<i16>,
        /// Timebases below this fail, as they do when several channels are
        /// enabled on real hardware.
        min_valid_timebase: u32,
        /// Makes `maximum_value` fail, to exercise the open error path.
        fail_maximum_value: bool,
        /// Counted so the tests can prove the handle was released.
        closes: Arc<AtomicUsize>,
    }

    // `registered` holds pointers into buffers the driver under test owns,
    // which are only ever written during a `get_values` call on the same
    // thread. Nothing hands a MockScope to another thread.
    unsafe impl Send for MockScope {}
    unsafe impl Sync for MockScope {}

    impl MockScope {
        fn new() -> Self {
            Self {
                calls: Arc::new(Mutex::new(Vec::new())),
                channel_data: Mutex::new(vec![Vec::new(); 8]),
                registered: Mutex::new(Vec::new()),
                max_adc: 32767,
                overflow: Mutex::new(0),
                min_valid_timebase: 0,
                fail_maximum_value: false,
                closes: Arc::new(AtomicUsize::new(0)),
            }
        }

        /// 1 ns at timebase 0, doubling every index. Monotonic, which is all
        /// the binary search relies on.
        fn interval_for(timebase: u32) -> f64 {
            1e-9 * 2f64.powi(timebase.min(60) as i32)
        }

        fn log(&self, entry: String) {
            self.calls.lock().unwrap().push(entry);
        }

        /// A handle on the call log that outlives the mock, so a test can
        /// assert on what happened during `Drop`.
        fn log_handle(&self) -> Arc<Mutex<Vec<String>>> {
            Arc::clone(&self.calls)
        }
    }

    /// How many logged calls start with `needle`.
    fn count(log: &Arc<Mutex<Vec<String>>>, needle: &str) -> usize {
        log.lock()
            .unwrap()
            .iter()
            .filter(|c| c.starts_with(needle))
            .count()
    }

    /// Every logged call starting with `needle`.
    fn matching(log: &Arc<Mutex<Vec<String>>>, needle: &str) -> Vec<String> {
        log.lock()
            .unwrap()
            .iter()
            .filter(|c| c.starts_with(needle))
            .cloned()
            .collect()
    }

    impl PicoModernApi for MockScope {
        fn family(&self) -> DriverFamily {
            DriverFamily::Ps5000a
        }

        fn enumerate(&self) -> Result<Vec<String>> {
            Ok(vec!["MOCK/0001".to_string()])
        }

        fn open(&self, _serial: Option<&str>, _resolution: DeviceResolution) -> Result<i16> {
            Ok(1)
        }

        fn close(&self, _handle: i16) -> Result<()> {
            self.closes.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        fn unit_info(&self, _handle: i16, info: UnitInfo) -> Result<String> {
            Ok(match info {
                UnitInfo::VariantInfo => "5442D".to_string(),
                UnitInfo::BatchAndSerial => "MOCK/0001".to_string(),
                _ => String::new(),
            })
        }

        fn set_channel(
            &self,
            _handle: i16,
            channel: u8,
            enabled: bool,
            coupling: Coupling,
            range: Range,
            offset: f32,
        ) -> Result<()> {
            self.log(format!(
                "set_channel ch={channel} on={enabled} coupling={coupling:?} \
                 range={range:?} offset={offset}"
            ));
            Ok(())
        }

        fn get_timebase(&self, _h: i16, timebase: u32, samples: u32) -> Result<TimebaseInfo> {
            if timebase < self.min_valid_timebase {
                bail!("PICO_INVALID_TIMEBASE");
            }
            Ok(TimebaseInfo {
                interval_seconds: Self::interval_for(timebase),
                max_samples: samples.max(1000),
            })
        }

        fn run_block(&self, _h: i16, pre: u32, post: u32, timebase: u32) -> Result<Duration> {
            self.log(format!("run_block pre={pre} post={post} timebase={timebase}"));
            Ok(Duration::from_millis(20))
        }

        fn is_ready(&self, _handle: i16) -> Result<bool> {
            Ok(true)
        }

        fn set_data_buffer(&self, _h: i16, channel: u8, buffer: &mut [i16]) -> Result<()> {
            self.log(format!("set_data_buffer ch={channel} len={}", buffer.len()));
            self.registered
                .lock()
                .unwrap()
                .push((channel, buffer.as_mut_ptr(), buffer.len()));
            Ok(())
        }

        fn get_values(
            &self,
            _h: i16,
            _start: u32,
            samples: u32,
            _mode: RatioMode,
        ) -> Result<CaptureResult> {
            // Fill the registered buffers the way the real driver does.
            let data = self.channel_data.lock().unwrap();
            for &(channel, ptr, len) in self.registered.lock().unwrap().iter() {
                let source = &data[channel as usize];
                let count = len.min(samples as usize);
                for i in 0..count {
                    // Safety: these buffers belong to the driver under test
                    // and outlive this call, which is the same contract the
                    // real driver relies on.
                    unsafe {
                        *ptr.add(i) = source.get(i).copied().unwrap_or(0);
                    }
                }
            }
            Ok(CaptureResult {
                samples,
                overflow: *self.overflow.lock().unwrap(),
            })
        }

        fn stop(&self, _handle: i16) -> Result<()> {
            self.log("stop".to_string());
            Ok(())
        }

        fn set_simple_trigger(
            &self,
            _h: i16,
            enabled: bool,
            channel: u8,
            threshold: i16,
            direction: ThresholdDirection,
            _delay: u32,
            auto_trigger_ms: i16,
        ) -> Result<()> {
            self.log(format!(
                "set_simple_trigger on={enabled} ch={channel} threshold={threshold} \
                 dir={direction:?} auto_ms={auto_trigger_ms}"
            ));
            Ok(())
        }

        fn maximum_value(&self, _handle: i16) -> Result<i16> {
            if self.fail_maximum_value {
                bail!("PICO_NOT_RESPONDING");
            }
            Ok(self.max_adc)
        }
    }

    fn capabilities(channels: u8) -> ScopeCapabilities {
        ScopeCapabilities {
            family: DriverFamily::Ps5000a,
            model: "5442D".to_string(),
            serial: "MOCK/0001".to_string(),
            analog_channels: channels,
            channel_labels: ScopeCapabilities::default_labels(channels),
            resolution: ResolutionSupport::fixed(8),
            voltage_ranges: Vec::new(),
            max_sample_rate_hz: 1e9,
            max_memory_samples: 512_000_000,
            bandwidth_hz: None,
            analog_offset: true,
            bandwidth_limiter: true,
            digital_ports: 0,
            rapid_block: true,
            streaming_mode: true,
            smart_probes: true,
            signal_generator: None,
            advanced_triggers: Vec::new(),
        }
    }

    fn scope() -> PicoScopeModern {
        PicoScopeModern::adopt(Box::new(MockScope::new()), 1, capabilities(4))
            .expect("mock scope should open")
    }

    // ---------- range selection ----------

    #[test]
    fn volts_per_div_picks_a_range_that_does_not_clip() {
        let mut s = scope();
        let ch = ChannelId::Alphabetic('A');

        // 1 V/div over 8 divisions is +/- 4 V, so the 5 V range.
        s.set_volts_per_div(ch, 1.0).unwrap();
        let channel = s.settings_for(ch).unwrap();
        assert_eq!(PicoScopeModern::range_for(channel), Range::R5V);
    }

    #[test]
    fn a_ten_x_probe_selects_a_tenth_of_the_range() {
        // The probe divides before the input, so the scope needs a range
        // ten times smaller than the displayed volts/div.
        let mut s = scope();
        let ch = ChannelId::Alphabetic('A');
        s.set_attenuation(ch, 10.0).unwrap();
        s.set_volts_per_div(ch, 1.0).unwrap();

        let channel = s.settings_for(ch).unwrap();
        assert_eq!(PicoScopeModern::range_for(channel), Range::R500mV);
    }

    // ---------- timebase ----------

    #[test]
    fn the_capture_window_covers_ten_divisions() {
        // 1 ms/div should mean a 10 ms window across the capture.
        let interval = PicoScopeModern::target_interval_seconds(1e-3, 2000);
        assert!((interval * 2000.0 - 10e-3).abs() < 1e-12);
    }

    #[test]
    fn the_timebase_search_finds_the_smallest_index_that_is_slow_enough() {
        let mut s = scope();
        s.set_time_per_div(1e-3).unwrap();

        // Target interval is 10ms/2000 = 5 us. With the mock's doubling
        // model, 2^12 ns = 4.096 us is too fast and 2^13 ns = 8.192 us is
        // the first that is slow enough.
        assert_eq!(s.timebase, 13);
        assert!(s.interval_seconds >= 5e-6);
    }

    #[test]
    fn the_window_errs_wider_rather_than_cutting_the_waveform_off() {
        let mut s = scope();
        s.set_time_per_div(1e-3).unwrap();

        let window = s.interval_seconds * f64::from(s.samples_per_capture);
        assert!(
            window >= 10e-3,
            "window {window} is narrower than the requested 10 ms"
        );
    }

    #[test]
    fn a_faster_time_per_div_selects_a_faster_timebase() {
        let mut s = scope();
        s.set_time_per_div(1e-3).unwrap();
        let slow = s.timebase;
        s.set_time_per_div(1e-6).unwrap();
        assert!(s.timebase < slow, "faster time/div should lower the index");
    }

    #[test]
    fn timebases_the_device_rejects_are_skipped() {
        // With several channels on, the fastest timebases are unavailable.
        // The search must step over them rather than give up.
        let mut mock = MockScope::new();
        mock.min_valid_timebase = 20;
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();

        s.set_time_per_div(1e-6).unwrap();
        assert!(
            s.timebase >= 20,
            "search settled on rejected timebase {}",
            s.timebase
        );
    }

    #[test]
    fn sample_rate_is_the_inverse_of_the_chosen_interval() {
        let mut s = scope();
        s.set_time_per_div(1e-3).unwrap();
        let rate = s.get_sample_rate().unwrap();
        assert!((rate - 1.0 / s.interval_seconds).abs() < 1e-6);
    }

    // ---------- trigger ----------

    #[test]
    fn a_trigger_level_converts_to_counts_against_the_channel_range() {
        let mut s = scope();
        let ch = ChannelId::Alphabetic('A');
        s.set_volts_per_div(ch, 1.0).unwrap(); // the 5 V range

        // Half of full scale should be half of the ADC count.
        let channel = s.settings_for(ch).unwrap();
        let counts = s.volts_to_counts(2.5, channel);
        assert!(
            (counts as i32 - 16383).abs() < 10,
            "expected about half scale, got {counts}"
        );
    }

    #[test]
    fn a_trigger_level_beyond_full_scale_saturates_instead_of_wrapping() {
        // Relies on Rust's float-to-int cast saturating. Pinned because a
        // wrapping conversion would turn a level above full scale into a
        // negative threshold, arming the trigger at the opposite end.
        let s = scope();
        let channel = s.settings_for(ChannelId::Alphabetic('A')).unwrap();
        assert_eq!(s.volts_to_counts(1e9, channel), i16::MAX);
        assert_eq!(s.volts_to_counts(-1e9, channel), i16::MIN);
    }

    #[test]
    fn auto_mode_arms_a_timeout_and_normal_mode_does_not() {
        let mock = MockScope::new();
        let log = mock.log_handle();
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();

        s.set_capture_mode(CaptureMode::Normal).unwrap();
        s.set_capture_mode(CaptureMode::Auto).unwrap();

        let triggers = matching(&log, "set_simple_trigger");
        assert!(triggers.iter().any(|c| c.contains("auto_ms=0")), "{triggers:?}");
        assert!(
            triggers
                .iter()
                .any(|c| c.contains(&format!("auto_ms={AUTO_TRIGGER_MS}"))),
            "{triggers:?}"
        );
    }

    #[test]
    fn the_trigger_slope_reaches_the_driver() {
        let mock = MockScope::new();
        let log = mock.log_handle();
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();

        s.set_trigger_slope(TriggerSlope::Falling).unwrap();

        let triggers = matching(&log, "set_simple_trigger");
        assert!(
            triggers.iter().any(|c| c.contains("dir=Falling")),
            "slope never reached the driver: {triggers:?}"
        );
    }

    // ---------- channels ----------

    #[test]
    fn a_channel_the_model_does_not_have_is_rejected_by_name() {
        let mut s = PicoScopeModern::adopt(Box::new(MockScope::new()), 1, capabilities(2))
            .unwrap();
        let err = s
            .enable_channel(ChannelId::Alphabetic('C'))
            .unwrap_err()
            .to_string();
        assert!(err.contains("A-B"), "unhelpful message: {err}");
        assert!(err.contains("5442D"), "does not name the model: {err}");
    }

    #[test]
    fn only_channel_a_is_enabled_to_begin_with() {
        // Turning every channel on by default would restrict the timebase
        // range before the user has asked for anything.
        let s = scope();
        assert!(s.is_channel_enabled(ChannelId::Alphabetic('A')).unwrap());
        assert!(!s.is_channel_enabled(ChannelId::Alphabetic('B')).unwrap());
    }

    #[test]
    fn enabling_a_channel_reapplies_it_to_the_hardware() {
        let mock = MockScope::new();
        let log = mock.log_handle();
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();
        let before = count(&log, "set_channel");

        s.enable_channel(ChannelId::Alphabetic('B')).unwrap();

        assert!(count(&log, "set_channel") > before);
        assert!(s.is_channel_enabled(ChannelId::Alphabetic('B')).unwrap());
    }

    #[test]
    fn numeric_channel_ids_are_one_based() {
        // Saved nets and the CLI use 1..n, not 0..n-1.
        let s = scope();
        assert_eq!(s.channel_index(ChannelId::Numeric(1)).unwrap(), 0);
        assert_eq!(s.channel_index(ChannelId::Numeric(4)).unwrap(), 3);
        assert!(s.channel_index(ChannelId::Numeric(5)).is_err());
        assert!(s.channel_index(ChannelId::Numeric(0)).is_err());
    }

    // ---------- capture ----------

    #[test]
    fn a_capture_produces_a_frame_matching_the_enabled_channels() {
        let mut s = scope();
        s.enable_channel(ChannelId::Alphabetic('B')).unwrap();
        s.start_triggered_capture(0.5).unwrap();

        let frame = s.get_triggered_data().unwrap();
        assert_eq!(frame.channels.len(), 2);
        assert_eq!(frame.channels[0].channel, ChannelId::Alphabetic('A'));
        assert_eq!(frame.channels[1].channel, ChannelId::Alphabetic('B'));
        // Channel-major: every channel contributes exactly this many.
        assert_eq!(
            frame.samples.len(),
            frame.samples_per_channel as usize * 2
        );
    }

    #[test]
    fn the_frame_declares_a_length_matching_its_payload() {
        // A mismatch here is undecodable at the other end.
        let mut s = scope();
        s.start_triggered_capture(0.5).unwrap();
        let frame = s.get_triggered_data().unwrap();

        assert_eq!(
            frame.samples.len(),
            frame.samples_per_channel as usize * frame.channels.len()
        );
        assert_eq!(
            frame.pre_trigger_samples + frame.post_trigger_samples,
            frame.samples_per_channel
        );
    }

    #[test]
    fn the_trigger_position_splits_the_capture() {
        let mut s = scope();
        s.start_triggered_capture(0.25).unwrap();
        let frame = s.get_triggered_data().unwrap();

        let ratio = f64::from(frame.pre_trigger_samples)
            / f64::from(frame.samples_per_channel);
        assert!((ratio - 0.25).abs() < 0.01, "pre-trigger ratio was {ratio}");
    }

    #[test]
    fn capture_data_reaches_the_frame() {
        let mock = MockScope::new();
        mock.channel_data.lock().unwrap()[0] = vec![100, 200, 300, 400];
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();

        s.start_triggered_capture(0.5).unwrap();
        let frame = s.get_triggered_data().unwrap();

        assert_eq!(&frame.samples[..4], &[100, 200, 300, 400]);
    }

    #[test]
    fn counts_scale_back_to_the_volts_that_produced_them() {
        // Full scale in counts must come back as full scale in volts, or
        // every waveform is drawn at the wrong amplitude.
        let mock = MockScope::new();
        mock.channel_data.lock().unwrap()[0] = vec![32767];
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();
        s.set_volts_per_div(ChannelId::Alphabetic('A'), 1.0).unwrap();

        s.start_triggered_capture(0.5).unwrap();
        let volts = s.get_data(ChannelId::Alphabetic('A')).unwrap();

        // The 5 V range, at full-scale count.
        assert!((volts[0] - 5.0).abs() < 0.01, "got {} V", volts[0]);
    }

    #[test]
    fn a_ten_x_probe_scales_the_reported_volts_up() {
        let mock = MockScope::new();
        mock.channel_data.lock().unwrap()[0] = vec![32767];
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();
        let ch = ChannelId::Alphabetic('A');
        s.set_attenuation(ch, 10.0).unwrap();
        s.set_volts_per_div(ch, 1.0).unwrap();

        s.start_triggered_capture(0.5).unwrap();
        let volts = s.get_data(ch).unwrap();

        // 500 mV range at the input, times the 10x probe.
        assert!((volts[0] - 5.0).abs() < 0.01, "got {} V", volts[0]);
    }

    #[test]
    fn overflow_is_reported_by_frame_position_not_hardware_index() {
        // The client pairs the mask with the channel descriptors it was
        // sent, so bit N must mean "the Nth descriptor", not "hardware
        // channel N".
        let mock = MockScope::new();
        *mock.overflow.lock().unwrap() = 0b0010; // hardware channel B
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();

        s.disable_channel(ChannelId::Alphabetic('A')).unwrap();
        s.enable_channel(ChannelId::Alphabetic('B')).unwrap();
        s.start_triggered_capture(0.5).unwrap();

        let frame = s.get_triggered_data().unwrap();
        assert_eq!(frame.channels.len(), 1);
        // B is the only descriptor, so its overflow is bit 0.
        assert_eq!(frame.overflow_mask, 0b0001);
    }

    #[test]
    fn capturing_with_no_channel_enabled_says_so() {
        let mut s = scope();
        s.disable_channel(ChannelId::Alphabetic('A')).unwrap();
        let err = s.get_triggered_data().unwrap_err().to_string();
        assert!(err.contains("no channel is enabled"), "got: {err}");
    }

    #[test]
    fn is_ready_is_false_before_a_capture_is_armed() {
        // Otherwise the acquisition loop would read a buffer that holds
        // nothing but the previous capture.
        let s = scope();
        assert!(!s.is_ready().unwrap());
    }

    #[test]
    fn stopping_clears_the_capturing_state() {
        let mut s = scope();
        s.start_triggered_capture(0.5).unwrap();
        assert!(s.is_ready().unwrap());
        s.stop_triggered_capture().unwrap();
        assert!(!s.is_ready().unwrap());
    }

    #[test]
    fn buffers_are_registered_for_every_enabled_channel() {
        let mock = MockScope::new();
        let log = mock.log_handle();
        let mut s = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();

        s.enable_channel(ChannelId::Alphabetic('B')).unwrap();
        s.enable_channel(ChannelId::Alphabetic('C')).unwrap();
        s.start_triggered_capture(0.5).unwrap();

        assert_eq!(count(&log, "set_data_buffer"), 3);
    }

    // ---------- polling ----------

    #[test]
    fn the_poll_interval_follows_the_drivers_own_estimate() {
        let mut s = scope();
        s.start_triggered_capture(0.5).unwrap();
        // The mock reports 20 ms, so a tenth is 2 ms.
        assert_eq!(s.suggested_poll_interval(), Duration::from_millis(2));
    }

    #[test]
    fn the_poll_interval_is_clamped_at_both_ends() {
        let mut s = scope();
        // Never zero, or the loop spins a core on a fast capture.
        s.expected_capture = Duration::from_micros(1);
        assert_eq!(s.suggested_poll_interval(), Duration::from_millis(1));
        // Never so long that a stop request feels unresponsive.
        s.expected_capture = Duration::from_secs(60);
        assert_eq!(s.suggested_poll_interval(), Duration::from_millis(20));
    }

    // ---------- rejections ----------

    #[test]
    fn nonsensical_settings_are_rejected_rather_than_applied() {
        let mut s = scope();
        let ch = ChannelId::Alphabetic('A');
        assert!(s.set_volts_per_div(ch, 0.0).is_err());
        assert!(s.set_volts_per_div(ch, -1.0).is_err());
        assert!(s.set_attenuation(ch, 0.0).is_err());
        assert!(s.set_time_per_div(0.0).is_err());
    }

    #[test]
    fn cursor_calls_explain_that_the_hardware_has_no_screen() {
        let s = scope();
        let err = s.measure_horizontal_cursor_delta().unwrap_err().to_string();
        assert!(err.contains("no on-screen cursors"), "got: {err}");
    }

    // ---------- handle lifecycle ----------

    #[test]
    fn a_failed_open_still_releases_the_device_handle() {
        // A leaked PicoScope handle makes every later open fail until the
        // device is physically replugged, so a failure here has to close.
        let mut mock = MockScope::new();
        mock.fail_maximum_value = true;
        let closes = Arc::clone(&mock.closes);

        let result = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4));

        assert!(result.is_err(), "the mock was set up to fail");
        assert_eq!(
            closes.load(Ordering::SeqCst),
            1,
            "the handle was leaked instead of closed"
        );
    }

    #[test]
    fn dropping_the_scope_closes_the_handle() {
        let mock = MockScope::new();
        let closes = Arc::clone(&mock.closes);

        {
            let _scope = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();
            assert_eq!(closes.load(Ordering::SeqCst), 0, "closed while still in use");
        }

        assert_eq!(closes.load(Ordering::SeqCst), 1, "the handle was not closed");
    }

    #[test]
    fn dropping_mid_capture_stops_before_closing() {
        // Closing a unit that is still armed is what leaves the USB
        // endpoint needing a replug.
        let mock = MockScope::new();
        let log = mock.log_handle();
        let mut scope = PicoScopeModern::adopt(Box::new(mock), 1, capabilities(4)).unwrap();
        scope.start_triggered_capture(0.5).unwrap();
        drop(scope);

        assert_eq!(count(&log, "stop"), 1, "dropped without stopping");
    }

    #[test]
    fn capabilities_are_reported_as_detected() {
        let s = scope();
        let caps = s.capabilities().unwrap();
        assert_eq!(caps.model, "5442D");
        assert_eq!(caps.analog_channels, 4);
        assert_eq!(s.get_channel_count().unwrap(), 4);
    }
}

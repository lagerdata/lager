// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Rigol DS1000Z series: DS1054Z, DS1074Z, DS1104Z (and their Plus
//! versions), the MSO1000Z, and the two-channel DS1202Z-E. Only the DS1054Z
//! has been run. The USB catalog lists the `1ab1:04ce` parts; the DS1202Z-E
//! is recognized by its `*IDN?` but may use another product ID, which the
//! catalog does not list.
//!
//! Everything goes over SCPI. These scopes have no video output, so where
//! the MSO5000 backend takes its live view from a capture card, this one
//! reads the scope's on-screen record (`:WAVeform:MODE NORMal`, 1200 points
//! a channel) at most every `LIVE_VIEW_INTERVAL`. That record is exact rather
//! than an envelope of the screen, which also makes it the published capture
//! and the record a client asks for.
//!
//! Most of the command set is the MSO5000's, so settings are read with the
//! same `read_settings`, and the screen record with the same
//! `screen_record`. The differences: twelve horizontal divisions, not ten;
//! 1 mV/div to 10 V/div; and `:MEASure:ITEM? <item>,<source>` takes its
//! source as an argument, so measuring does not move the front panel's
//! measurement source.

use std::sync::Mutex;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use protocol::lscp::FLAG_TRIGGERED;
use protocol::{
    CaptureFrame, CaptureMode, ChannelId, Coupling, DriverFamily, Measurement, MeasurementSet,
    ScopeCapabilities, TriggerSlope,
};

use super::scpi::{DEFAULT_TIMEOUT, DeviceLock, Scpi};
use super::{
    MEASUREMENT_ITEMS, Messages, Settings, changes_cached_settings, channel_index, check_errors,
    format_volts, letter, measurement_item, parse_f64, read_settings, scope_measurement,
    screen_record,
};
use crate::oscilloscope::{Cursor, Oscilloscope};

/// The DS1000Z grid: twelve divisions across, eight up.
pub const HORIZONTAL_DIVISIONS: f64 = 12.0;
/// Volts per division the DS1000Z takes at a 1x probe, 1-2-5 from 1 mV.
const VOLTS_PER_DIV: [f64; 13] = [
    1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0,
];
/// 1 GSa/s with one channel on; it halves with two and quarters with three
/// or four.
const MAX_SAMPLE_RATE_HZ: f64 = 1e9;
/// 12 Mpts is the standard memory; 24 Mpts is an option a unit may carry.
const MAX_MEMORY_SAMPLES: u64 = 12_000_000;
/// The least time between two live-view captures. A screen record is 1200
/// bytes a channel, so the link is free most of the time for commands.
const LIVE_VIEW_INTERVAL: Duration = Duration::from_millis(100);
/// Reply bytes to ask for in one USB-TMC transfer. The scope sends at most
/// 500 a transfer whatever is asked for, and asked for more than about 512,
/// corrupts the transfers after the first: a byte of `:WAVeform:DATA?` comes
/// back as 0x0A, and an ASCII record loses separators (firmware 00.04.05).
const MAX_TRANSFER_BYTES: u32 = 500;
/// `:AUToscale` takes about 5 s on a DS1054Z, where a reply is otherwise
/// waited for `DEFAULT_TIMEOUT`, also 5 s.
const AUTOSCALE_TIMEOUT: Duration = Duration::from_secs(15);

/// What `*IDN?` says, for a DS1000Z-series model.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub model: String,
    pub serial: String,
    pub firmware: String,
    channels: u8,
    bandwidth_tens_mhz: u32,
}

impl Identity {
    /// Parse `RIGOL TECHNOLOGIES,DS1054Z,DS1ZA000000000,00.04.04.SP4`.
    ///
    /// The model is `DS1` or `MSO1`, two digits of bandwidth in tens of MHz,
    /// one digit of channel count, then `Z` and anything after it (` Plus`,
    /// `-E`).
    ///
    /// # Errors
    ///
    /// When the instrument is not a Rigol, or not a DS1000Z-series model.
    pub fn parse(idn: &str) -> Result<Identity> {
        let fields: Vec<&str> = idn.trim().split(',').map(str::trim).collect();
        if fields.len() < 4 || !fields[0].to_ascii_uppercase().contains("RIGOL") {
            bail!("the instrument identifies as {idn:?}, not a Rigol");
        }
        let model = fields[1].to_ascii_uppercase();
        let digits = model
            .strip_prefix("DS1")
            .or_else(|| model.strip_prefix("MSO1"))
            .with_context(|| format!("the instrument is a {model}, not a DS1000Z-series scope"))?;
        let (number, rest) = digits.split_at(
            digits
                .find(|c: char| !c.is_ascii_digit())
                .unwrap_or(digits.len()),
        );
        if number.len() != 3 || !rest.starts_with('Z') {
            bail!("the instrument is a {model}, not a DS1000Z-series scope");
        }
        let (bandwidth, channels) = number.split_at(2);
        let channels: u8 = channels.parse()?;
        if !matches!(channels, 2 | 4) {
            bail!("cannot tell the channel count of a {model}");
        }
        Ok(Identity {
            serial: fields[2].to_string(),
            firmware: fields[3].to_string(),
            channels,
            bandwidth_tens_mhz: bandwidth.parse()?,
            model,
        })
    }

    /// Analog channels: the digit before `Z`.
    pub fn channels(&self) -> u8 {
        self.channels
    }

    /// Analog bandwidth in Hz, as the model number states it. A DS1054Z
    /// says 50 MHz whatever an upgrade code may have unlocked.
    pub fn bandwidth_hz(&self) -> f64 {
        f64::from(self.bandwidth_tens_mhz) * 10e6
    }
}

fn capabilities_for(identity: &Identity) -> ScopeCapabilities {
    let channels = identity.channels();
    let voltage_ranges = VOLTS_PER_DIV
        .iter()
        .enumerate()
        .map(
            |(code, &volts_per_div)| protocol::capabilities::VoltageRange {
                code: u8::try_from(code).expect("13 ranges fit in a u8"),
                // Full scale is four divisions either side of centre.
                full_scale_volts: volts_per_div * 4.0,
                label: format_volts(volts_per_div * 4.0),
            },
        )
        .collect();
    ScopeCapabilities {
        family: DriverFamily::RigolDs1000z,
        model: identity.model.clone(),
        serial: identity.serial.clone(),
        analog_channels: channels,
        channel_labels: ScopeCapabilities::default_labels(channels),
        resolution: protocol::capabilities::ResolutionSupport::fixed(8),
        voltage_ranges,
        max_sample_rate_hz: MAX_SAMPLE_RATE_HZ,
        max_memory_samples: MAX_MEMORY_SAMPLES,
        bandwidth_hz: Some(identity.bandwidth_hz()),
        analog_offset: true,
        bandwidth_limiter: false,
        digital_ports: 0,
        rapid_block: false,
        streaming_mode: false,
        smart_probes: false,
        signal_generator: None,
        advanced_triggers: Vec::new(),
        roll_mode: false,
        peak_detect: true,
        timebase_steps: protocol::capabilities::TimebaseSteps::OneTwoFive,
    }
}

/// What the capture path changes from `&self`.
#[derive(Debug, Default)]
struct Work {
    last_capture: Option<Instant>,
    /// A single capture already published, so the frozen screen is not
    /// published again until the next arm.
    single_done: bool,
}

/// A DS1000Z-series scope over SCPI.
pub struct RigolDs1000z {
    scpi: Mutex<Box<dyn Scpi>>,
    _lock: Option<DeviceLock>,
    capabilities: ScopeCapabilities,
    firmware: String,
    settings: Settings,
    work: Mutex<Work>,
}

impl RigolDs1000z {
    /// Open over an established connection, the one `rigol::open` made to
    /// ask the scope what it is.
    ///
    /// # Errors
    ///
    /// When the scope does not identify as a DS1000Z-series model, or does
    /// not answer the settings queries.
    pub fn with(mut scpi: Box<dyn Scpi>, lock: Option<DeviceLock>) -> Result<RigolDs1000z> {
        scpi.set_max_transfer(MAX_TRANSFER_BYTES);
        // Anything a previous process left in the error queue would be
        // reported against the first command here.
        scpi.write("*CLS")?;
        let identity = Identity::parse(&scpi.query("*IDN?")?)?;
        let capabilities = capabilities_for(&identity);
        let settings = read_settings(scpi.as_mut(), usize::from(identity.channels()))?;
        tracing::info!(
            model = %identity.model,
            serial = %identity.serial,
            firmware = %identity.firmware,
            "opened Rigol DS1000Z scope"
        );
        Ok(RigolDs1000z {
            scpi: Mutex::new(scpi),
            _lock: lock,
            capabilities,
            firmware: identity.firmware,
            settings,
            work: Mutex::new(Work::default()),
        })
    }

    /// The firmware version the scope reported.
    pub fn firmware(&self) -> &str {
        &self.firmware
    }

    fn link(&self) -> std::sync::MutexGuard<'_, Box<dyn Scpi>> {
        self.scpi
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn work(&self) -> std::sync::MutexGuard<'_, Work> {
        self.work
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn index(&self, channel: ChannelId) -> Result<usize> {
        channel_index(channel, self.settings.channels.len())
    }

    /// Send a setting and check the scope took it.
    fn apply(&self, command: &str) -> Result<()> {
        let mut scpi = self.link();
        scpi.write(command)?;
        check_errors(scpi.as_mut(), command)
    }

    fn query_f64(&self, query: &str) -> Result<f64> {
        parse_f64(&self.link().query(query)?, query)
    }

    fn not_offered(&self, what: &str) -> anyhow::Error {
        anyhow::anyhow!(
            "the {} does not offer {what} through this driver",
            self.capabilities.model
        )
    }

    /// One `:MEASure:ITEM?` of zero-based channel `i`: `None` when the scope
    /// has no such value for the signal shown (it answers 9.9E37).
    fn measure_item(&self, i: usize, which: Measurement) -> Result<Option<f64>> {
        if !self.settings.channels[i].enabled {
            bail!(
                "channel {} is not enabled, so there is nothing to measure",
                letter(i)
            );
        }
        let (item, percent) = measurement_item(which);
        let query = format!(
            ":MEASure:ITEM? {},CHANnel{}",
            MEASUREMENT_ITEMS[item],
            i + 1
        );
        let reply = self.link().query(&query)?;
        Ok(scope_measurement(&reply).map(|v| if percent { v * 100.0 } else { v }))
    }

    fn measured(&self, channel: ChannelId, which: Measurement) -> Result<f64> {
        let i = self.index(channel)?;
        self.measure_item(i, which)?
            .with_context(|| format!("the scope has no {which:?} for channel {channel}"))
    }

    fn enabled(&self) -> Vec<usize> {
        (0..self.settings.channels.len())
            .filter(|&i| self.settings.channels[i].enabled)
            .collect()
    }

    fn record(&self, channels: &[usize]) -> Result<CaptureFrame> {
        let wanted: Vec<(usize, Coupling)> = channels
            .iter()
            .map(|&i| (i, self.settings.channels[i].coupling))
            .collect();
        screen_record(self.link().as_mut(), &wanted, Messages::OneEach)
    }

    /// `record`, marked triggered when the scope captured it on a trigger.
    fn flagged_record(&self, channels: &[usize]) -> Result<CaptureFrame> {
        let status = self.trigger_status()?;
        let triggered = match self.settings.capture_mode {
            // Stopped in single: on the trigger it was armed for. Still
            // WAIT, the record is from before it was armed.
            CaptureMode::Single => status == "STOP",
            // TD: triggered. AUTO is a capture the scope made without one.
            _ => status == "TD",
        };
        let mut frame = self.record(channels)?;
        if triggered {
            frame.flags |= FLAG_TRIGGERED;
        }
        Ok(frame)
    }

    fn trigger_status(&self) -> Result<String> {
        Ok(self
            .link()
            .query(":TRIGger:STATus?")?
            .trim()
            .to_ascii_uppercase())
    }

    /// Re-read the channel setting a write may have rounded.
    fn reread_channel(&mut self, i: usize) -> Result<()> {
        let n = i + 1;
        self.settings.channels[i].volts_per_div = self.query_f64(&format!(":CHANnel{n}:SCALe?"))?;
        self.settings.channels[i].offset = self.query_f64(&format!(":CHANnel{n}:OFFSet?"))?;
        Ok(())
    }
}

impl Oscilloscope for RigolDs1000z {
    fn enable_channel(&mut self, channel: ChannelId) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:DISPlay ON", i + 1))?;
        self.settings.channels[i].enabled = true;
        Ok(())
    }

    fn disable_channel(&mut self, channel: ChannelId) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:DISPlay OFF", i + 1))?;
        self.settings.channels[i].enabled = false;
        Ok(())
    }

    fn is_channel_enabled(&self, channel: ChannelId) -> Result<bool> {
        Ok(self.settings.channels[self.index(channel)?].enabled)
    }

    fn set_volts_per_div(&mut self, channel: ChannelId, volts_per_div: f64) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:SCALe {volts_per_div}", i + 1))?;
        self.reread_channel(i)
    }

    fn get_volts_per_div(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings.channels[self.index(channel)?].volts_per_div)
    }

    fn set_volts_offset(&mut self, channel: ChannelId, volts_offset: f64) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:OFFSet {volts_offset}", i + 1))?;
        self.reread_channel(i)
    }

    fn get_volts_offset(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings.channels[self.index(channel)?].offset)
    }

    fn set_coupling(&mut self, channel: ChannelId, coupling: Coupling) -> Result<()> {
        let i = self.index(channel)?;
        let token = match coupling {
            Coupling::AC => "AC",
            Coupling::DC => "DC",
            Coupling::GND => "GND",
        };
        self.apply(&format!(":CHANnel{}:COUPling {token}", i + 1))?;
        self.settings.channels[i].coupling = coupling;
        Ok(())
    }

    fn get_coupling(&self, channel: ChannelId) -> Result<Coupling> {
        Ok(self.settings.channels[self.index(channel)?].coupling)
    }

    fn set_attenuation(&mut self, channel: ChannelId, attenuation: f64) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:PROBe {attenuation}", i + 1))?;
        self.settings.channels[i].probe = self.query_f64(&format!(":CHANnel{}:PROBe?", i + 1))?;
        // The probe ratio rescales the channel's volts per division.
        self.reread_channel(i)
    }

    fn get_attenuation(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings.channels[self.index(channel)?].probe)
    }

    fn set_trigger_level(&mut self, trigger_level: f64) -> Result<()> {
        self.apply(&format!(":TRIGger:EDGE:LEVel {trigger_level}"))?;
        self.settings.trigger_level = self.query_f64(":TRIGger:EDGE:LEVel?")?;
        Ok(())
    }

    fn get_trigger_level(&self) -> Result<f64> {
        Ok(self.settings.trigger_level)
    }

    fn set_time_per_div(&mut self, time_per_div: f64) -> Result<()> {
        self.apply(&format!(":TIMebase:MAIN:SCALe {time_per_div}"))?;
        self.settings.time_per_div = self.query_f64(":TIMebase:MAIN:SCALe?")?;
        self.settings.time_offset = self.query_f64(":TIMebase:MAIN:OFFSet?")?;
        self.settings.sample_rate = self.query_f64(":ACQuire:SRATe?").unwrap_or(0.0);
        Ok(())
    }

    fn get_time_per_div(&self) -> Result<f64> {
        Ok(self.settings.time_per_div)
    }

    fn set_time_offset(&mut self, time_offset: f64) -> Result<()> {
        self.apply(&format!(":TIMebase:MAIN:OFFSet {time_offset}"))?;
        self.settings.time_offset = self.query_f64(":TIMebase:MAIN:OFFSet?")?;
        Ok(())
    }

    fn get_time_offset(&self) -> Result<f64> {
        Ok(self.settings.time_offset)
    }

    fn set_trigger_source(&mut self, trigger_source: ChannelId) -> Result<()> {
        let i = self.index(trigger_source)?;
        self.apply(":TRIGger:MODE EDGE")?;
        self.apply(&format!(":TRIGger:EDGE:SOURce CHANnel{}", i + 1))?;
        self.settings.trigger_source = i;
        self.settings.trigger_level = self.query_f64(":TRIGger:EDGE:LEVel?")?;
        Ok(())
    }

    fn get_trigger_source(&self) -> Result<ChannelId> {
        Ok(letter(self.settings.trigger_source))
    }

    fn set_trigger_slope(&mut self, trigger_slope: TriggerSlope) -> Result<()> {
        let token = match trigger_slope {
            TriggerSlope::Rising => "POSitive",
            TriggerSlope::Falling => "NEGative",
            TriggerSlope::Either => "RFALl",
            TriggerSlope::Neither => bail!("the scope's edge trigger has no 'neither' slope"),
        };
        self.apply(&format!(":TRIGger:EDGE:SLOPe {token}"))?;
        self.settings.trigger_slope = trigger_slope;
        Ok(())
    }

    fn get_trigger_slope(&self) -> Result<TriggerSlope> {
        Ok(self.settings.trigger_slope)
    }

    fn set_capture_mode(&mut self, capture_mode: CaptureMode) -> Result<()> {
        let token = match capture_mode {
            CaptureMode::Auto => "AUTO",
            CaptureMode::Normal => "NORMal",
            CaptureMode::Single => "SINGle",
        };
        self.apply(&format!(":TRIGger:SWEep {token}"))?;
        self.settings.capture_mode = capture_mode;
        Ok(())
    }

    fn get_capture_mode(&self) -> Result<CaptureMode> {
        Ok(self.settings.capture_mode)
    }

    fn set_cursor_position(&mut self, _cursor: Cursor) -> Result<()> {
        Err(self.not_offered("cursors"))
    }

    fn get_cursor_position(&self, _cursor: Cursor) -> Result<f64> {
        Err(self.not_offered("cursors"))
    }

    fn measure_horizontal_cursor_delta(&self) -> Result<f64> {
        Err(self.not_offered("cursors"))
    }

    fn measure_vertical_cursor_delta(&self) -> Result<f64> {
        Err(self.not_offered("cursors"))
    }

    fn measure_duty_cycle(&self, channel: ChannelId) -> Result<f64> {
        self.measured(channel, Measurement::DutyCyclePositive)
    }

    fn measure_frequency(&self, channel: ChannelId) -> Result<f64> {
        self.measured(channel, Measurement::Frequency)
    }

    fn measure_period(&self, channel: ChannelId) -> Result<f64> {
        self.measured(channel, Measurement::Period)
    }

    fn measure_rms(&self, channel: ChannelId) -> Result<f64> {
        self.measured(channel, Measurement::Vrms)
    }

    fn measure_peak_to_peak(&self, channel: ChannelId) -> Result<f64> {
        self.measured(channel, Measurement::Vpp)
    }

    fn measure_average(&self, channel: ChannelId) -> Result<f64> {
        self.measured(channel, Measurement::Vavg)
    }

    fn measure_min(&self, channel: ChannelId) -> Result<f64> {
        self.measured(channel, Measurement::Vmin)
    }

    /// The channel's screen record in volts.
    fn get_data(&self, channel: ChannelId) -> Result<Vec<f64>> {
        let i = self.index(channel)?;
        let frame = self.record(&[i])?;
        let ch = &frame.channels[0];
        let scale = f64::from(ch.scale_v_per_count);
        let offset = f64::from(ch.offset_v);
        Ok(frame
            .samples
            .iter()
            .map(|&s| f64::from(s) * scale + offset)
            .collect())
    }

    fn get_sample_rate(&self) -> Result<f64> {
        self.query_f64(":ACQuire:SRATe?")
    }

    /// The memory depth in use. In AUTO the scope does not say, so it is the
    /// samples the timebase spans at the current rate.
    fn get_memory_depth(&self) -> Result<usize> {
        let reply = self.link().query(":ACQuire:MDEPth?")?;
        if let Ok(points) = reply.trim().parse::<f64>() {
            return Ok(points as usize);
        }
        let rate = self.get_sample_rate()?;
        Ok((rate * self.settings.time_per_div * HORIZONTAL_DIVISIONS).round() as usize)
    }

    fn get_bandwidth(&self) -> Result<f64> {
        self.capabilities
            .bandwidth_hz
            .context("the model states no bandwidth")
    }

    fn get_channel_count(&self) -> Result<usize> {
        Ok(self.settings.channels.len())
    }

    fn get_trigger_position(&self) -> Result<f64> {
        let s = &self.settings;
        if s.time_per_div <= 0.0 {
            return Ok(50.0);
        }
        Ok(50.0 - s.time_offset / (s.time_per_div * HORIZONTAL_DIVISIONS) * 100.0)
    }

    /// Run, or arm one capture in single. The position is the scope's own
    /// horizontal offset, set with `set_time_offset`.
    fn start_triggered_capture(&mut self, _trigger_position_percent: f64) -> Result<()> {
        self.work().single_done = false;
        let command = if self.settings.capture_mode == CaptureMode::Single {
            ":SINGle"
        } else {
            ":RUN"
        };
        self.apply(command)
    }

    fn stop_triggered_capture(&mut self) -> Result<()> {
        let mut scpi = self.link();
        scpi.write(":STOP")?;
        scpi.query("*OPC?")?;
        Ok(())
    }

    /// The scope has its own screen and front panel: leave it as the
    /// operator had it.
    fn release_capture(&mut self) -> Result<()> {
        Ok(())
    }

    fn acquiring_after_open(&mut self) -> Result<bool> {
        Ok(self.trigger_status()? != "STOP")
    }

    /// In single, once the scope has stopped with its capture; otherwise at
    /// most every `LIVE_VIEW_INTERVAL`.
    fn is_ready(&self) -> Result<bool> {
        if self.settings.capture_mode == CaptureMode::Single {
            if self.work().single_done {
                return Ok(false);
            }
            return Ok(self.trigger_status()? == "STOP");
        }
        let work = self.work();
        Ok(work
            .last_capture
            .is_none_or(|t| t.elapsed() >= LIVE_VIEW_INTERVAL))
    }

    fn get_triggered_data(&self) -> Result<CaptureFrame> {
        let enabled = self.enabled();
        if enabled.is_empty() {
            bail!("no channel is enabled, so there is nothing to capture");
        }
        let frame = self.flagged_record(&enabled)?;
        let mut work = self.work();
        work.last_capture = Some(Instant::now());
        if self.settings.capture_mode == CaptureMode::Single {
            work.single_done = true;
        }
        Ok(frame)
    }

    fn force_trigger(&mut self) -> Result<()> {
        let mut scpi = self.link();
        scpi.write(":TFORce")?;
        scpi.query("*OPC?")?;
        Ok(())
    }

    fn capabilities(&self) -> Result<ScopeCapabilities> {
        Ok(self.capabilities.clone())
    }

    fn suggested_poll_interval(&self) -> Duration {
        Duration::from_millis(20)
    }

    fn reports_trigger(&self) -> bool {
        true
    }

    fn answers_from_instrument(&self) -> bool {
        true
    }

    fn measure_one(
        &mut self,
        channel: ChannelId,
        which: Measurement,
    ) -> Option<Result<Option<f64>>> {
        Some(
            self.index(channel)
                .and_then(|i| self.measure_item(i, which)),
        )
    }

    /// Computed from the channel's screen record, as the MSO5000's are: one
    /// read, where asking the scope item by item would be fourteen queries.
    fn measure_all(&mut self, channel: ChannelId) -> Option<Result<MeasurementSet>> {
        Some((|| {
            let i = self.index(channel)?;
            if !self.settings.channels[i].enabled {
                bail!("channel {channel} is not enabled, so there is nothing to measure");
            }
            let record = self.record(&[i])?;
            let mut set = protocol::measure_channel(&record, 0).with_context(|| {
                format!("the scope has no samples for channel {channel}; is there a signal on it?")
            })?;
            // 1200 points across the screen are too coarse to time an edge;
            // `measure_one` asks the scope for rise and fall.
            set.rise_time = None;
            set.fall_time = None;
            Ok(set)
        })())
    }

    fn read_record(&mut self) -> Option<Result<CaptureFrame>> {
        Some((|| {
            let enabled = self.enabled();
            if enabled.is_empty() {
                bail!("no channel is enabled, so there is no record to read");
            }
            self.flagged_record(&enabled)
        })())
    }

    fn set_holdoff(&mut self, seconds: f64) -> Option<Result<()>> {
        // 16 ns is the scope's least holdoff.
        Some(self.apply(&format!(":TRIGger:HOLDoff {}", seconds.max(1.6e-8))))
    }

    /// One SCPI command; a query answers with the reply. A command that can
    /// change a cached setting, `:AUToscale` among them, has the settings
    /// read again after it.
    fn scpi(&mut self, command: &str) -> Option<Result<Option<String>>> {
        Some((|| {
            let reply = if command.contains('?') {
                let reply = self.link().query(command)?;
                check_errors(self.link().as_mut(), command)?;
                Some(reply)
            } else if is_autoscale(command) {
                // The scope answers nothing, the error check included,
                // until the autoscale is done.
                self.link().set_timeout(AUTOSCALE_TIMEOUT);
                let applied = self.apply(command);
                self.link().set_timeout(DEFAULT_TIMEOUT);
                applied?;
                None
            } else {
                self.apply(command)?;
                None
            };
            if changes_cached_settings(command) {
                let channels = self.settings.channels.len();
                let settings = read_settings(self.link().as_mut(), channels)?;
                self.settings = settings;
            }
            Ok(reply)
        })())
    }
}

/// Whether `command` is `:AUToscale`, in any of its spellings.
fn is_autoscale(command: &str) -> bool {
    let header = command.trim().trim_start_matches(':');
    header
        .get(..3)
        .is_some_and(|short| short.eq_ignore_ascii_case("AUT"))
}

#[cfg(test)]
mod tests;

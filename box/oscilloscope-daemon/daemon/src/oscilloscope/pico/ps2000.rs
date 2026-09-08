// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use crate::oscilloscope::CaptureMode;
use crate::oscilloscope::Coupling;
use crate::oscilloscope::Oscilloscope;
use crate::oscilloscope::{
    ChannelId, ChannelSettings, Cursor, CursorType, OscilloscopeSettings, TriggerSettings,
    TriggerSlope,
};
use anyhow::Result;
use once_cell::sync::Lazy;
use std::collections::HashMap;
use std::fmt::Debug;

use protocol::ScopeCapabilities;
use protocol::capabilities::{DriverFamily, ResolutionSupport, VoltageRange};
use protocol::lscp::{CaptureFrame, ChannelFrame, FLAG_TRIGGERED};

use protocol::{measure_channel, Measurement};

// Types and constants come from the generated bindings; the functions are
// reached through the runtime-loaded driver rather than by linking.
use super::loader::ps2000_sys::*;
use super::loader::ps2000;

const NANOSECONDS_PER_SECOND: f64 = 1_000_000_000.0;
const MIN_MEMORY_DEPTH: usize = 8000;
/// Horizontal divisions a capture is spread across.
///
/// Ten, which is what a scope screen is: eight is the vertical count, and it
/// had been used for both. Time/div therefore meant one eighth of the capture
/// here while every display of it -- the web UI's graticule included -- drew
/// one tenth, so a window asked for at 1 ms/div was captured 8 ms wide and
/// drawn as though it were 10, and each division on screen was 0.8 ms.
const TOTAL_NUM_TIME_DIVISIONS: usize = 10;
const MAX_NUM_TIMEBASES: i16 = PS2000_MAX_TIMEBASE as i16;
const DEFAULT_ATTENUATION: f64 = 10.0;
const DEFAULT_TRIGGER_POSITION: f64 = 50.0;

#[derive(Debug, Clone)]
struct DeviceSpecs {
    channels: u8,
    sample_rate: f64,
    memory_depth: usize,
    bandwidth: f64,
}

/// Nanoseconds from `CLOCK_MONOTONIC`, used to stamp captures so a client can
/// measure true capture-to-client latency.
///
/// Deliberately not `Instant`, whose epoch is opaque and process-relative:
/// the readers are separate processes, and Python's `time.monotonic_ns()` and
/// JavaScript's `performance.now()` both derive from `CLOCK_MONOTONIC`, so
/// using it directly is what makes the subtraction meaningful. Monotonic
/// rather than wall-clock so an NTP step cannot produce a negative latency.
pub(crate) fn monotonic_ns() -> u64 {
    let mut ts = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // Cannot fail for CLOCK_MONOTONIC on any supported platform.
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut ts) };
    ts.tv_sec as u64 * 1_000_000_000 + ts.tv_nsec as u64
}

static DEVICE_SPECS: Lazy<HashMap<&'static str, DeviceSpecs>> = Lazy::new(|| {
    let mut specs = HashMap::new();
    specs.insert(
        "2204A",
        DeviceSpecs {
            channels: 2,
            sample_rate: 100_000_000.0,
            memory_depth: 8_000,
            bandwidth: 10_000_000.0,
        },
    );
    specs.insert(
        "2205A",
        DeviceSpecs {
            channels: 2,
            sample_rate: 200_000_000.0,
            memory_depth: 16_000,
            bandwidth: 25_000_000.0,
        },
    );

    specs.insert(
        "2205A MSO",
        DeviceSpecs {
            channels: 2,
            sample_rate: 500_000_000.0,
            memory_depth: 48_000,
            bandwidth: 25_000_000.0,
        },
    );

    specs.insert(
        "2405A",
        DeviceSpecs {
            channels: 4,
            sample_rate: 500_000_000.0,
            memory_depth: 48_000,
            bandwidth: 25_000_000.0,
        },
    );

    specs.insert(
        "2206B",
        DeviceSpecs {
            channels: 2,
            sample_rate: 500_000_000.0,
            memory_depth: 32_000_000,
            bandwidth: 50_000_000.0,
        },
    );

    specs.insert(
        "2206B MSO",
        DeviceSpecs {
            channels: 2,
            sample_rate: 1_000_000_000.0,
            memory_depth: 32_000_000,
            bandwidth: 50_000_000.0,
        },
    );

    specs.insert(
        "2406B",
        DeviceSpecs {
            channels: 4,
            sample_rate: 1_000_000_000.0,
            memory_depth: 32_000_000,
            bandwidth: 50_000_000.0,
        },
    );

    specs.insert(
        "2207B",
        DeviceSpecs {
            channels: 2,
            sample_rate: 1_000_000_000.0,
            memory_depth: 64_000_000,
            bandwidth: 70_000_000.0,
        },
    );

    specs.insert(
        "2207B MSO",
        DeviceSpecs {
            channels: 2,
            sample_rate: 1_000_000_000.0,
            memory_depth: 64_000_000,
            bandwidth: 70_000_000.0,
        },
    );

    specs.insert(
        "2407B",
        DeviceSpecs {
            channels: 4,
            sample_rate: 1_000_000_000.0,
            memory_depth: 64_000_000,
            bandwidth: 70_000_000.0,
        },
    );

    specs.insert(
        "2208B",
        DeviceSpecs {
            channels: 2,
            sample_rate: 1_000_000_000.0,
            memory_depth: 128_000_000,
            bandwidth: 100_000_000.0,
        },
    );

    specs.insert(
        "2208B MSO",
        DeviceSpecs {
            channels: 2,
            sample_rate: 1_000_000_000.0,
            memory_depth: 128_000_000,
            bandwidth: 100_000_000.0,
        },
    );

    specs.insert(
        "2408B",
        DeviceSpecs {
            channels: 4,
            sample_rate: 1_000_000_000.0,
            memory_depth: 128_000_000,
            bandwidth: 100_000_000.0,
        },
    );
    specs
});

#[derive(Debug, Clone)]
#[allow(dead_code)]
struct ScopeInfo {
    driver_version: String,
    usb_version: String,
    hardware_version: u8,
    variant_info: String,
    batch_serial: String,
    calibration_date: String,
    error_code: u32,
    kernerl_driver_version: String,
}

pub struct PicoScope2000 {
    handle: i16,
    settings: OscilloscopeSettings,
    is_capturing: bool,
    #[allow(dead_code)]
    capture_buffer: Vec<u16>,
    pre_trigger_samples: u32,
    post_trigger_samples: u32,
    current_timebase: i16,
    current_time_interval_ns: f64,
    memory_depth: u32,
    is_new_channel_enabled_disabled: bool,
    memory_depth_not_update: bool,
    /// `time_indisposed_ms` from the last `ps2000_run_block`. Sets the floor
    /// for the readiness poll, since polling faster than the capture takes
    /// cannot return data.
    expected_capture_ms: u64,
    /// Filled in once at open time from the variant string.
    capabilities: ScopeCapabilities,
}

impl PicoScope2000 {
    const MAX_NUM_DEVICES: u32 = 64;
    pub fn new() -> Result<Self> {
        let api = ps2000()?;
        for _ in 0..Self::MAX_NUM_DEVICES {
            let handle = unsafe { api.ps2000_open_unit() };
            if handle > 0 {
                let info = Self::get_scope_info(handle)?;
                let settings = Self::initial_settings(handle)?;
                // Before the timebase, which needs the unit's fastest
                // interval to know which of the driver's candidates it can
                // actually sample at.
                let capabilities = Self::detect_capabilities(&info, &settings);
                let (current_timebase, current_time_interval_ns) =
                    Self::get_timebase_for_sample_rate(
                        handle,
                        settings.memory_depth.unwrap_or(MIN_MEMORY_DEPTH) as f64,
                        settings.time_per_div,
                    )?;

                tracing::info!(
                    model = %capabilities.model,
                    serial = %capabilities.serial,
                    channels = capabilities.analog_channels,
                    timebase = current_timebase,
                    interval_ns = current_time_interval_ns,
                    "opened PicoScope"
                );

                return Ok(Self {
                    handle,
                    settings,
                    is_capturing: false,
                    capture_buffer: Vec::new(),
                    pre_trigger_samples: MIN_MEMORY_DEPTH as u32 / 2,
                    post_trigger_samples: MIN_MEMORY_DEPTH as u32 / 2,
                    current_timebase,
                    current_time_interval_ns,
                    memory_depth: MIN_MEMORY_DEPTH as u32,
                    is_new_channel_enabled_disabled: false,
                    memory_depth_not_update: false,
                    expected_capture_ms: 0,
                    capabilities,
                });
            }
        }
        Err(anyhow::anyhow!(
            "no PicoScope 2000-series device found; \
             check the USB connection and that the lager group owns the device node"
        ))
    }

    /// Build the capability set from the variant string.
    ///
    /// The ps2000 API predates `SetChannelWithOffset`, `SetBandwidthFilter`
    /// and `SetNoOfCaptures`, so those are false for every device on this
    /// driver regardless of model. What does vary by model is channel count,
    /// memory, and bandwidth, which come from the specs table.
    fn detect_capabilities(info: &ScopeInfo, settings: &OscilloscopeSettings) -> ScopeCapabilities {
        let model = info.variant_info.trim().to_string();
        let channels = Self::read_channel_count(&model) as u8;

        // MSO variants carry a digital port alongside the analog channels.
        let digital_ports = if model.to_uppercase().contains("MSO") {
            1
        } else {
            0
        };

        let voltage_ranges = (enPS2000Range_PS2000_50MV as i16..=enPS2000Range_PS2000_20V as i16)
            .map(|code| {
                let full_scale = Self::raw_range_to_volts(code);
                VoltageRange {
                    code: code as u8,
                    full_scale_volts: full_scale,
                    label: if full_scale < 1.0 {
                        format!("{:.0} mV", full_scale * 1000.0)
                    } else {
                        format!("{full_scale:.0} V")
                    },
                }
            })
            .collect();

        ScopeCapabilities {
            family: DriverFamily::Ps2000,
            model,
            serial: info.batch_serial.trim().to_string(),
            analog_channels: channels,
            channel_labels: ScopeCapabilities::default_labels(channels),
            // Every part on this driver is fixed 8-bit.
            resolution: ResolutionSupport::fixed(8),
            voltage_ranges,
            max_sample_rate_hz: settings.sample_rate.unwrap_or(0.0),
            max_memory_samples: settings.memory_depth.unwrap_or(0) as u64,
            bandwidth_hz: settings.bandwidth,
            analog_offset: false,
            bandwidth_limiter: false,
            digital_ports,
            rapid_block: false,
            // ps2000 has streaming, but via the legacy overview-buffer API
            // rather than RunStreaming. Not wired up, so not advertised.
            streaming_mode: false,
            smart_probes: false,
            signal_generator: None,
            advanced_triggers: vec!["edge".to_string()],
        }
    }

    fn initial_settings(handle: i16) -> Result<OscilloscopeSettings> {
        let scope_info = Self::get_scope_info(handle)?;
        let channel_count = Self::read_channel_count(&scope_info.variant_info);
        let sample_rate = Self::read_sample_rate(&scope_info.variant_info);
        let memory_depth = Self::read_memory_depth(&scope_info.variant_info);
        let bandwidth = Self::read_bandwidth(&scope_info.variant_info);

        let mut channels: Vec<ChannelSettings> = Vec::new();
        for i in 0..channel_count {
            let channel_id = match i {
                0 => ChannelId::Alphabetic('A'),
                1 => ChannelId::Alphabetic('B'),
                2 => ChannelId::Alphabetic('C'),
                3 => ChannelId::Alphabetic('D'),
                _ => continue, // Shouldn't happen for PicoScope 2000
            };
            channels.push(ChannelSettings {
                channel_id,
                volts_per_div: 1.0,
                volts_offset: 0.0,
                coupling: Coupling::DC,
                attenuation: DEFAULT_ATTENUATION,
                enabled: false,
            });
            Self::set_channel_to_default(handle, channel_id)?;
        }

        Self::set_trigger_to_default(handle)?;

        Ok(OscilloscopeSettings {
            channels,
            cursors: vec![
                Cursor {
                    cursor_type: CursorType::Vertical,
                    position: 0.0,
                    name: "Time1".to_string(),
                }, // Vertical cursor 1
                Cursor {
                    cursor_type: CursorType::Vertical,
                    position: 0.0,
                    name: "Time2".to_string(),
                }, // Vertical cursor 2
                Cursor {
                    cursor_type: CursorType::Horizontal,
                    position: 0.0,
                    name: "Voltage1".to_string(),
                }, // Horizontal cursor 1
                Cursor {
                    cursor_type: CursorType::Horizontal,
                    position: 0.0,
                    name: "Voltage2".to_string(),
                }, // Horizontal cursor 2
            ],
            time_per_div: 0.001,
            time_offset: 0.0,
            sample_rate: Some(sample_rate),
            memory_depth: Some(memory_depth),
            bandwidth: Some(bandwidth),
            trigger: TriggerSettings {
                trigger_level: 0.0,
                trigger_source: ChannelId::Alphabetic('A'),
                trigger_slope: TriggerSlope::Either,
                capture_mode: CaptureMode::Normal,
                delay: 0.0,
                trigger_position: DEFAULT_TRIGGER_POSITION,
            },
        })
    }

    fn disable_scope_channel(&mut self, channel: ChannelId) -> anyhow::Result<()> {
        tracing::debug!("Disabling channel {}", channel.as_str());
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .enabled = false;
        self.is_new_channel_enabled_disabled = true;
        tracing::debug!("is_capturing={}", self.is_capturing);
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        tracing::debug!("Channel {} disabled successfully", channel.as_str());
        Ok(())
    }

    fn enable_scope_channel(&mut self, channel: ChannelId) -> anyhow::Result<()> {
        tracing::debug!("Enabling channel {}", channel.as_str());
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .enabled = true;
        self.is_new_channel_enabled_disabled = true;
        tracing::debug!("is_capturing={}, calling do_update_channel if capturing",
            self.is_capturing);
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        tracing::debug!("Channel {} enabled successfully", channel.as_str());
        Ok(())
    }

    fn set_channel_to_default(handle: i16, channel: ChannelId) -> Result<()> {
        let api = ps2000()?;
        let channel_id_raw_value = Self::channel_id_to_raw_value(channel);
        let is_enabled_raw_value = Self::is_enabled_to_raw_value(false);
        let coupling_raw_value = Self::coupling_to_raw_value(Coupling::DC);
        let range = Self::volts_per_div_to_range(1.0, DEFAULT_ATTENUATION);
        let result = unsafe {
            api.ps2000_set_channel(
                handle,
                channel_id_raw_value,
                is_enabled_raw_value,
                coupling_raw_value,
                range,
            )
        };
        if result == 0 {
            return Err(anyhow::anyhow!(
                "ps2000_set_channel failed while defaulting channel {channel}"
            ));
        }
        Ok(())
    }

    fn set_trigger_to_default(handle: i16) -> Result<()> {
        let api = ps2000()?;
        let mut trigger_conditions = PS2000_TRIGGER_CONDITIONS {
            channelA: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            channelB: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            channelC: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            channelD: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            external: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            pulseWidthQualifier: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
        };

        let _ = unsafe {
            api.ps2000SetAdvTriggerChannelConditions(handle, &mut trigger_conditions, 0_i16)
        };

        let _ = unsafe {
            api.ps2000SetAdvTriggerChannelDirections(
                handle,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING,
            )
        };

        let mut trigger_channel_properties = PS2000_TRIGGER_CHANNEL_PROPERTIES {
            thresholdMajor: 0_i16,
            thresholdMinor: 0_i16,
            hysteresis: 0_u16,
            channel: 0_i16,
            thresholdMode: enPS2000ThresholdMode_PS2000_WINDOW,
        };
        let _ = unsafe {
            api.ps2000SetAdvTriggerChannelProperties(
                handle,
                &mut trigger_channel_properties,
                0_i16,
                0_i32,
            )
        };

        let _ =
            unsafe { api.ps2000SetAdvTriggerDelay(handle, 0_u32, -DEFAULT_TRIGGER_POSITION as f32) };

        // The advanced-trigger calls above are best-effort: the 2204A and
        // 2205A accept them, but older units in the family return 0 for the
        // advanced-trigger family while still working with simple edge
        // triggers. Failing startup here would refuse a working scope.
        Ok(())
    }

    fn get_scope_info(handle: i16) -> Result<ScopeInfo> {
        let _info_string = vec![0i8; 256];

        // Get all the different info types
        let driver_version = Self::get_unit_info_string(handle, 0)?; // PS2000_DRIVER_VERSION
        let usb_version = Self::get_unit_info_string(handle, 1)?; // PS2000_USB_VERSION
        let hardware_version = Self::get_unit_info_string(handle, 2)?
            .parse::<u8>()
            .unwrap_or(0);
        let variant_info = Self::get_unit_info_string(handle, 3)?;
        let batch_serial = Self::get_unit_info_string(handle, 4)?; // PS2000_BATCH_AND_SERIAL
        let calibration_date = Self::get_unit_info_string(handle, 5)?; // PS2000_CAL_DATE
        let error_code = Self::get_unit_info_string(handle, 6)?
            .parse::<u32>()
            .unwrap_or(0);
        let kernel_driver_version = Self::get_unit_info_string(handle, 7)?; // PS2000_KERNEL_DRIVER_VERSION

        Ok(ScopeInfo {
            driver_version,
            usb_version,
            hardware_version,
            variant_info,
            batch_serial,
            calibration_date,
            error_code,
            kernerl_driver_version: kernel_driver_version,
        })
    }

    fn get_unit_info_string(handle: i16, line: i16) -> Result<String> {
        let api = ps2000()?;
        let mut info_string = vec![0i8; 256];

        let result = unsafe {
            api.ps2000_get_unit_info(
                handle,
                info_string.as_mut_ptr(),
                info_string.len() as i16,
                line,
            )
        };

        if result <= 0 {
            return Err(anyhow::anyhow!("Failed to get unit info for line {}", line));
        }

        let info = unsafe {
            std::ffi::CStr::from_ptr(info_string.as_ptr())
                .to_string_lossy()
                .to_string()
        };
        tracing::debug!("Info: {}", info);
        Ok(info)
    }

    pub fn close(&self) -> anyhow::Result<()> {
        let api = ps2000()?;
        unsafe { api.ps2000_close_unit(self.handle) };
        Ok(())
    }

    pub fn set_volts_per_div_range(
        &mut self,
        channel: ChannelId,
        volts_per_div: f64,
    ) -> anyhow::Result<()> {
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .volts_per_div = volts_per_div;
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;         
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    pub fn set_ac_dc_coupling(
        &mut self,
        channel: ChannelId,
        coupling: Coupling,
    ) -> anyhow::Result<()> {
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .coupling = coupling;

        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;         
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    fn read_channel_count(base_model: &str) -> usize {
        let device_specs = DEVICE_SPECS.get(&base_model).unwrap();
        device_specs.channels as usize
    }

    fn read_sample_rate(base_model: &str) -> f64 {
        let device_specs = DEVICE_SPECS.get(&base_model).unwrap();
        device_specs.sample_rate
    }

    fn read_memory_depth(base_model: &str) -> usize {
        let device_specs = DEVICE_SPECS.get(&base_model).unwrap();
        device_specs.memory_depth
    }

    fn read_bandwidth(base_model: &str) -> f64 {
        let device_specs = DEVICE_SPECS.get(&base_model).unwrap();
        device_specs.bandwidth
    }

    fn get_sample_rate_from_device(&self) -> Result<f64> {
        let scope_info = Self::get_scope_info(self.handle)?;
        Ok(Self::read_sample_rate(&scope_info.variant_info))
    }

    fn get_memory_depth_from_device(&self) -> Result<usize> {
        let scope_info = Self::get_scope_info(self.handle)?;
        Ok(Self::read_memory_depth(&scope_info.variant_info))
    }

    fn get_bandwidth_from_device(&self) -> Result<f64> {
        let scope_info = Self::get_scope_info(self.handle)?;
        Ok(Self::read_bandwidth(&scope_info.variant_info))
    }

    fn get_channel_count_from_device(&self) -> Result<usize> {
        let scope_info = Self::get_scope_info(self.handle)?;
        Ok(Self::read_channel_count(&scope_info.variant_info))
    }

    fn raw_range_to_volts(range: i16) -> f64 {
        #[allow(non_upper_case_globals)]
        match range as u32 {
            enPS2000Range_PS2000_10MV => 0.01,
            enPS2000Range_PS2000_20MV => 0.02,
            enPS2000Range_PS2000_50MV => 0.05,
            enPS2000Range_PS2000_100MV => 0.1,
            enPS2000Range_PS2000_200MV => 0.2,
            enPS2000Range_PS2000_500MV => 0.5,
            enPS2000Range_PS2000_1V => 1.0,
            enPS2000Range_PS2000_2V => 2.0,
            enPS2000Range_PS2000_5V => 5.0,
            enPS2000Range_PS2000_10V => 10.0,
            enPS2000Range_PS2000_20V => 20.0,
            enPS2000Range_PS2000_50V => 50.0,
            enPS2000Range_PS2000_MAX_RANGES => 50.0,
            _ => 1.0,
        }
    }

    fn volts_per_div_to_range(volts_per_div: f64, attenuation: f64) -> i16 {
        let total_range = volts_per_div * 8.0 / attenuation;

        match total_range {
            t if t <= 0.02 => enPS2000Range_PS2000_10MV as i16, // ±10 mV
            t if t <= 0.04 => enPS2000Range_PS2000_20MV as i16, // ±20 mV
            t if t <= 0.1 => enPS2000Range_PS2000_50MV as i16,  // ±50 mV
            t if t <= 0.2 => enPS2000Range_PS2000_100MV as i16, // ±100 mV
            t if t <= 0.4 => enPS2000Range_PS2000_200MV as i16, // ±200 mV
            t if t <= 1.0 => enPS2000Range_PS2000_500MV as i16, // ±500 mV
            t if t <= 2.0 => enPS2000Range_PS2000_1V as i16,    // ±1 V
            t if t <= 4.0 => enPS2000Range_PS2000_2V as i16,    // ±2 V
            t if t <= 10.0 => enPS2000Range_PS2000_5V as i16,   // ±5 V
            t if t <= 20.0 => enPS2000Range_PS2000_10V as i16,  // ±10 V
            t if t <= 40.0 => enPS2000Range_PS2000_20V as i16,  // ±20 V
            t if t <= 100.0 => enPS2000Range_PS2000_50V as i16, // ±50 V
            _ => enPS2000Range_PS2000_MAX_RANGES as i16,
        }
    }

    fn coupling_to_raw_value(coupling: Coupling) -> i16 {
        if coupling == Coupling::DC { 1 } else { 0 }
    }

    fn is_enabled_to_raw_value(is_enabled: bool) -> i16 {
        if is_enabled { 1 } else { 0 }
    }

    fn channel_id_to_raw_value(channel_id: ChannelId) -> i16 {
        match channel_id {
            ChannelId::Alphabetic('A') => 0,
            ChannelId::Alphabetic('B') => 1,
            ChannelId::Alphabetic('C') => 2,
            ChannelId::Alphabetic('D') => 3,
            _ => 0,
        }
    }

    fn set_scope_trigger_source(&mut self, trigger_source: ChannelId) -> anyhow::Result<()> {
        self.settings.trigger.trigger_source = trigger_source;
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;           
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    fn get_scope_trigger_source(&self) -> anyhow::Result<ChannelId> {
        Ok(self.settings.trigger.trigger_source)
    }

    fn set_trigger_direction(&mut self, trigger_slope: TriggerSlope) -> anyhow::Result<()> {
        self.settings.trigger.trigger_slope = trigger_slope;
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    fn get_trigger_direction_value(trigger_slope: TriggerSlope) -> i16 {
        match trigger_slope {
            TriggerSlope::Rising => enPS2000ThresholdDirection_PS2000_ADV_RISING as i16,
            TriggerSlope::Falling => enPS2000ThresholdDirection_PS2000_ADV_FALLING as i16,
            TriggerSlope::Either => enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING as i16,
            TriggerSlope::Neither => enPS2000ThresholdDirection_PS2000_ADV_NONE as i16,
        }
    }

    fn create_trigger_channel_properties(
        &self,
        mode: enPS2000ThresholdMode,
    ) -> PS2000_TRIGGER_CHANNEL_PROPERTIES {
        let trigger_source_raw_value =
            Self::channel_id_to_raw_value(self.settings.trigger.trigger_source);
        let trigger_level_adc_count = self
            .voltage_to_adc_counts(
                self.settings.trigger.trigger_level,
                self.settings.trigger.trigger_source,
            )
            .unwrap_or(0);
        PS2000_TRIGGER_CHANNEL_PROPERTIES {
            thresholdMajor: ((trigger_level_adc_count as f64) * 0.90) as i16,
            thresholdMinor: ((trigger_level_adc_count as f64) * 1.10) as i16,
            hysteresis: ((trigger_level_adc_count as f64) * 0.20) as u16, // Increased hysteresis for stability
            channel: trigger_source_raw_value,
            thresholdMode: mode,
        }
    }
    fn get_auto_trigger_ms(capture_mode: CaptureMode, desired_trigger_ms: i32) -> i32 {
        match capture_mode {
            CaptureMode::Auto => desired_trigger_ms,
            CaptureMode::Single => 0,
            CaptureMode::Normal => 0,
        }
    }

    fn set_scope_trigger_level(&mut self, trigger_level: f64) -> anyhow::Result<()> {
        // Stored at the input, since that is what converts to ADC counts.
        // `get_scope_trigger_level` converts back; both go through the
        // helpers below so the pair cannot drift.
        self.settings.trigger.trigger_level =
            to_input_volts(trigger_level, self.trigger_source_attenuation());
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;           
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    fn do_update_channel(&mut self) -> anyhow::Result<()> {
        let api = ps2000()?;
        let enabled_count = self.settings.channels.iter().filter(|c| c.enabled).count();
        tracing::debug!("Starting update, {} channel(s) enabled", enabled_count);

        if enabled_count == 0 {
            tracing::debug!("WARNING: No channels are enabled!");
        }

        for channel in &self.settings.channels {
            tracing::debug!("Setting channel: {} enabled={}",
                channel.channel_id.as_str(), channel.enabled);
            let channel_id_raw_value = Self::channel_id_to_raw_value(channel.channel_id);
            let is_enabled_raw_value = Self::is_enabled_to_raw_value(channel.enabled);
            let coupling_raw_value = Self::coupling_to_raw_value(channel.coupling);
            let range =
                Self::volts_per_div_to_range(channel.volts_per_div, channel.attenuation);
            tracing::debug!(
                "[DEBUG do_update_channel] Calling ps2000_set_channel: handle={}, id={}, enabled={}, coupling={}, range={}",
                self.handle, channel_id_raw_value, is_enabled_raw_value, coupling_raw_value, range
            );
            let result = unsafe {
                api.ps2000_set_channel(
                    self.handle,
                    channel_id_raw_value,
                    is_enabled_raw_value,
                    coupling_raw_value,
                    range,
                )
            };
            tracing::debug!("ps2000_set_channel returned: {}", result);
            if result == 0 {
                tracing::debug!("FAILED - ps2000_set_channel returned 0 for channel {}",
                    channel.channel_id.as_str());
                return Err(anyhow::anyhow!(
                    "Failed to set volts per div for channel {}",
                    channel.channel_id.as_str()
                ));
            } else {
                tracing::debug!("Channel {} configured successfully",
                    channel.channel_id.as_str());
            }
        }
        tracing::debug!("All channels updated successfully");
        Ok(())
    }


    /// Translate the stored trigger position into a pre/post-trigger split.
    ///
    /// Where the trigger sits inside the block is what moves the capture
    /// window in time, so this has to run whenever the position changes as
    /// well as whenever the depth does. It used to run only on a depth
    /// change, which is why the horizontal position looked like it worked:
    /// arming stored the percent and read it back, while the split -- and so
    /// the window -- stayed wherever it was, centred.
    fn apply_trigger_position(&mut self) {
        let (pre, post) =
            trigger_position_split(self.memory_depth, self.settings.trigger.trigger_position);
        self.pre_trigger_samples = pre;
        self.post_trigger_samples = post;
    }

    fn update_memory_depth(&mut self) -> anyhow::Result<()> {
        let num_channels = self.settings.channels.iter().filter(|c| c.enabled).count().max(1);
        let memory_depth = if num_channels == 1 {
            self.get_memory_depth_cached()? as u32
        }else{
            self.get_memory_depth_cached()? as u32 / num_channels as u32 - 1_000
        };

        match self.set_scope_time_per_div(self.settings.time_per_div, memory_depth) {
            Ok(_) => {
                tracing::debug!("Updated memory depth: {}", memory_depth);
                self.memory_depth = memory_depth;
                self.apply_trigger_position();
                Ok(())
            },
            Err(e) => {
                Err(e)
            }
        }
    }

    fn do_update_trigger(&mut self) -> anyhow::Result<()> {
        let api = ps2000()?;
        let trigger_source_raw_value =
            Self::channel_id_to_raw_value(self.settings.trigger.trigger_source);
        let trigger_level_adc_count = self.voltage_to_adc_counts(
            self.settings.trigger.trigger_level,
            self.settings.trigger.trigger_source,
        )?;
        let direction_raw_value =
            Self::get_trigger_direction_value(self.settings.trigger.trigger_slope);
        let delay_raw_value = Self::safe_f64_to_i16(self.settings.trigger.delay).unwrap_or(0);
        tracing::debug!("raw source: {}", trigger_source_raw_value);
        tracing::debug!("raw level: {}", trigger_level_adc_count);
        tracing::debug!("raw direction: {}", direction_raw_value);
        tracing::debug!("raw delay: {}", delay_raw_value);
        let auto_trigger_ms = Self::get_auto_trigger_ms(self.settings.trigger.capture_mode, 500);

        let mut trigger_conditions = match self.settings.trigger.trigger_source {
            ChannelId::Alphabetic('A') => {
                tracing::debug!("Trigger conditions set successfully for channel A");
                PS2000_TRIGGER_CONDITIONS {
                    channelA: enPS2000TriggerState_PS2000_CONDITION_TRUE,
                    channelB: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelC: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelD: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    external: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    pulseWidthQualifier: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                }
            }
            ChannelId::Alphabetic('B') => {
                tracing::debug!("Trigger conditions set successfully for channel B");
                PS2000_TRIGGER_CONDITIONS {
                    channelA: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelB: enPS2000TriggerState_PS2000_CONDITION_TRUE,
                    channelC: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelD: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    external: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    pulseWidthQualifier: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                }
            }
            ChannelId::Alphabetic('C') => {
                tracing::debug!("Trigger conditions set successfully for channel C");
                PS2000_TRIGGER_CONDITIONS {
                    channelA: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelB: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelC: enPS2000TriggerState_PS2000_CONDITION_TRUE,
                    channelD: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    external: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    pulseWidthQualifier: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                }
            }
            ChannelId::Alphabetic('D') => {
                tracing::debug!("Trigger conditions set successfully for channel D");
                PS2000_TRIGGER_CONDITIONS {
                    channelA: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelB: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelC: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    channelD: enPS2000TriggerState_PS2000_CONDITION_TRUE,
                    external: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                    pulseWidthQualifier: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
                }
            }
            _ => {
                return Err(anyhow::anyhow!("Failed to set trigger conditions"));
            }
        };

        let result = unsafe {
            api.ps2000SetAdvTriggerChannelConditions(self.handle, &mut trigger_conditions, 1i16)
        };
        if result == 0 {
            return Err(anyhow::anyhow!("Failed to set trigger conditions"));
        } else {
            tracing::debug!("Trigger conditions set successfully",);
        }
        // Set trigger direction only for the active trigger source channel
        // Set unused channels to RISING_OR_FALLING to avoid conflicts
        let trigger_source_channel = Self::channel_id_to_raw_value(self.settings.trigger.trigger_source);
        let permissive_direction = enPS2000ThresholdDirection_PS2000_ADV_NONE;
        let active_direction = direction_raw_value as u32;
        
        let result = unsafe {
            api.ps2000SetAdvTriggerChannelDirections(
                self.handle,
                if trigger_source_channel == 0 { active_direction } else { permissive_direction }, // Channel A
                if trigger_source_channel == 1 { active_direction } else { permissive_direction }, // Channel B
                if trigger_source_channel == 2 { active_direction } else { permissive_direction }, // Channel C
                if trigger_source_channel == 3 { active_direction } else { permissive_direction }, // Channel D
                permissive_direction, // External trigger
            )
        };
        if result == 0 {
            return Err(anyhow::anyhow!("Failed to set trigger directions"));
        } else {
            tracing::debug!("Trigger directions set successfully");
        }

        let mut trigger_channel_properties =
            self.create_trigger_channel_properties(enPS2000ThresholdMode_PS2000_LEVEL);
        let result = unsafe {
            api.ps2000SetAdvTriggerChannelProperties(
                self.handle,
                &mut trigger_channel_properties,
                1i16,
                auto_trigger_ms,
            )
        };
        if result == 0 {
            return Err(anyhow::anyhow!("Failed to set trigger channel properties"));
        } else {
            tracing::debug!("Trigger channel properties set successfully");
        }
        let pre_trigger_delay = -100.0
            * (self.pre_trigger_samples as f64
                / ((self.pre_trigger_samples + self.post_trigger_samples) as f64));
        tracing::debug!("Pre trigger delay: {}", pre_trigger_delay);
        let result = unsafe {
            api.ps2000SetAdvTriggerDelay(
                self.handle,
                delay_raw_value as u32,
                pre_trigger_delay as f32,
            )
        };
        if result == 0 {
            return Err(anyhow::anyhow!("Failed to set trigger delay"));
        } else {
            tracing::debug!("Trigger delay set successfully {}", pre_trigger_delay);
        }
        Ok(())
    }

    fn get_scope_trigger_level(&self) -> anyhow::Result<f64> {
        // Undoes the division done on the way in. Without this, setting
        // 1.0 V through a 10x probe read back as 0.1 V.
        Ok(to_probe_volts(
            self.settings.trigger.trigger_level,
            self.trigger_source_attenuation(),
        ))
    }

    /// Probe attenuation of whichever channel the trigger watches.
    ///
    /// Falls back to 1.0 rather than failing: a getter that errors because
    /// the trigger points at a disabled channel is less useful than one that
    /// reports the level unscaled.
    fn trigger_source_attenuation(&self) -> f64 {
        self.settings
            .channels
            .iter()
            .find(|c| c.channel_id == self.settings.trigger.trigger_source)
            .map(|c| c.attenuation)
            .filter(|a| *a > 0.0)
            .unwrap_or(1.0)
    }

    #[allow(dead_code)]
    fn set_trigger_delay(&mut self, delay: f64) -> anyhow::Result<()> {
        self.settings.trigger.delay = delay;
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    #[allow(dead_code)]
    fn get_trigger_delay(&self) -> anyhow::Result<f64> {
        Ok(self.settings.trigger.delay)
    }

    fn set_scope_capture_mode(&mut self, capture_mode: CaptureMode) -> anyhow::Result<()> {
        self.settings.trigger.capture_mode = capture_mode;
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
        }
        self.do_update_trigger()?;
        if self.is_capturing {
            self.do_memory_depth_update()?;
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    fn get_scope_capture_mode(&self) -> anyhow::Result<CaptureMode> {
        Ok(self.settings.trigger.capture_mode)
    }

    fn voltage_to_adc_counts(&self, voltage: f64, channel: ChannelId) -> Result<i16> {
        // Find the channel's current voltage range
        let channel_settings = self
            .settings
            .channels
            .iter()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?;

        let range_voltage = Self::raw_range_to_volts(Self::volts_per_div_to_range(
            channel_settings.volts_per_div,
            channel_settings.attenuation,
        ));
        if voltage > range_voltage {
            return Err(anyhow::anyhow!(
                "Voltage out of range {} > {}",
                voltage,
                range_voltage
            ));
        }
        // Convert to ADC counts (assuming bipolar range)
        let adc_counts = (voltage / range_voltage) * i16::MAX as f64;
        Ok(adc_counts as i16)
    }
    fn safe_f64_to_i16(value: f64) -> Option<i16> {
        if value >= i16::MIN as f64 && value <= i16::MAX as f64 {
            Some(value.round() as i16)
        } else {
            None
        }
    }

    /// Pick the timebase whose interval comes closest to filling the screen.
    ///
    /// Every timebase the driver accepts is a real one: they step by a factor
    /// of two from the unit's fastest, and `ps2000_get_timebase` refuses the
    /// ones that cannot hold the sample count asked of them -- which is how
    /// enabling a second channel, halving the buffer, takes the fast end of
    /// the range away.
    fn get_timebase_for_sample_rate(
        handle: i16,
        memory_depth: f64,
        time_per_div: f64,
    ) -> anyhow::Result<(i16, f64)> {
        let api = ps2000()?;
        let mut timebase_found = false;
        let total_divisions: f64 = TOTAL_NUM_TIME_DIVISIONS as f64;
        let total_time_ns: f64 = (time_per_div * total_divisions) * NANOSECONDS_PER_SECOND;
        let desired_timer_interval_ns: f64 = total_time_ns / memory_depth;

        let mut best_timebase = 0;
        let mut best_error = f64::INFINITY;
        let mut best_interval = 0.001;

        for timebase in 0..MAX_NUM_TIMEBASES {
            let mut time_interval = 0i32;
            let mut time_units = 0i16;
            let mut max_samples = 0i32;
            let result = unsafe {
                api.ps2000_get_timebase(
                    handle,
                    timebase,
                    memory_depth as i32,
                    &mut time_interval,
                    &mut time_units,
                    1,
                    &mut max_samples,
                )
            };
            if result != 0 {
                // Nanoseconds, always. `time_units` is not the unit of this
                // value -- it is the unit the driver suggests for the time
                // AXIS, to be handed to `ps2000_get_times_and_values`, and it
                // varies with the sample count for that reason. Converting
                // the interval through it scaled every fast timebase into
                // nonsense: a 2204A's fastest, a real 10 ns, came back
                // labelled picoseconds and became 0.01 ns, so 100 us/div
                // chose 0.16 ns a sample, captured 1.3 us instead of 1 ms and
                // reported 6.25 GS/s from a 100 MS/s unit. The slow
                // timebases, where the suggested axis unit happens to be
                // nanoseconds, came through untouched -- which is why only
                // the fast end of the dial was wrong.
                let actual_interval = time_interval as f64;
                timebase_found = true;
                let error = (actual_interval - desired_timer_interval_ns).abs();
                if error < best_error {
                    best_error = error;
                    best_timebase = timebase;
                    best_interval = actual_interval;
                }
            }
        }
        if !timebase_found {
            return Err(anyhow::anyhow!("No timebase found"));
        }
        Ok((best_timebase, best_interval))
    }

    fn run_block(&mut self, trigger_position_percent: f64) -> anyhow::Result<()> {
        let api = ps2000()?;
        self.settings.trigger.trigger_position = trigger_position_percent;
        // Before do_update_trigger, which derives the hardware trigger delay
        // from the split.
        self.apply_trigger_position();

        // Stop unconditionally: a previous client that disconnected without
        // stopping leaves is_capturing set with no capture actually armed,
        // and ps2000_run_block on an already-running unit fails.
        if self.is_capturing {
            let _ = self.stop_triggering();
        }

        self.do_update_channel()?;
        self.do_update_trigger()?;
        self.do_memory_depth_update()?;
        if self.memory_depth_not_update {
            self.stop_triggering()?;
            if self.update_memory_depth().is_ok() {
                self.memory_depth_not_update = false;
            }
        }

        let mut time_indisposed_ms = 0i32;
        let result = unsafe {
            api.ps2000_run_block(
                self.handle,
                self.memory_depth as i32,
                self.current_timebase,
                1, //oversample,
                &mut time_indisposed_ms,
            )
        };

        tracing::trace!(
            result,
            time_indisposed_ms,
            memory_depth = self.memory_depth,
            timebase = self.current_timebase,
            "ps2000_run_block"
        );

        if result == 0 {
            Err(anyhow::anyhow!(
                "ps2000_run_block failed for {} samples at timebase {}",
                self.memory_depth,
                self.current_timebase
            ))
        } else {
            self.is_capturing = true;
            // How long the driver says this capture will take. Polling any
            // faster than this cannot produce data, so it sets the floor for
            // the readiness poll.
            self.expected_capture_ms = time_indisposed_ms.max(0) as u64;
            Ok(())
        }
    }

    fn stop_triggering(&mut self) -> anyhow::Result<()> {
        let api = ps2000()?;
        let result = unsafe { api.ps2000_stop(self.handle) };
        tracing::trace!(result, "ps2000_stop");
        if result == 0 {
            Err(anyhow::anyhow!("ps2000_stop failed"))
        } else {
            self.is_capturing = false;
            if matches!(self.settings.trigger.capture_mode, CaptureMode::Single) {
                // Single-shot disarms after one capture, so the mode returns
                // to Normal rather than silently re-arming.
                self.settings.trigger.capture_mode = CaptureMode::Normal;
            }
            Ok(())
        }
    }
    fn is_scope_ready(&self) -> anyhow::Result<bool> {
        let api = ps2000()?;
        let result = unsafe { api.ps2000_ready(self.handle) };
        // This is the hottest call in the daemon: it runs on every poll of
        // every acquisition. Printing here unconditionally was measured at
        // ~990 MB/day of log on STG-2, so it sits behind `trace`.
        tracing::trace!(result, is_capturing = self.is_capturing, "ps2000_ready");

        match result {
            0 => Ok(false),
            n if n > 0 && self.is_capturing => Ok(true),
            n if n < 0 => Err(anyhow::anyhow!("ps2000_ready reported error {n}")),
            n => {
                // The scope reports data ready while we believe nothing is
                // running: a capture left over from a previous client that
                // exited without stopping. Not fatal, but the data belongs
                // to a configuration we no longer have, so it is not served.
                tracing::debug!(
                    result = n,
                    "scope holds data from a capture this session did not start"
                );
                Ok(false)
            }
        }
    }

    fn get_triggered_scope_data(&self) -> anyhow::Result<CaptureFrame> {
        let api = ps2000()?;
        let total_samples = (self.pre_trigger_samples + self.post_trigger_samples) as usize;

        // ps2000_get_values writes into four fixed buffers rather than taking
        // per-channel registrations, so all four must exist even when only
        // one channel is enabled.
        let mut buffer_a = vec![0i16; total_samples];
        let mut buffer_b = vec![0i16; total_samples];
        let mut buffer_c = vec![0i16; total_samples];
        let mut buffer_d = vec![0i16; total_samples];

        let mut overflow = 0i16;
        let result = unsafe {
            api.ps2000_get_values(
                self.handle,
                buffer_a.as_mut_ptr(),
                buffer_b.as_mut_ptr(),
                buffer_c.as_mut_ptr(),
                buffer_d.as_mut_ptr(),
                &mut overflow,
                total_samples as i32,
            )
        };
        if result == 0 {
            return Err(anyhow::anyhow!(
                "ps2000_get_values failed while reading {total_samples} samples"
            ));
        }

        // The driver may return fewer samples than requested. Trust its
        // count rather than the request, or the frame's declared length
        // will not match its payload.
        let returned = (result as usize).min(total_samples);

        let enabled: Vec<&ChannelSettings> = self
            .settings
            .channels
            .iter()
            .filter(|channel| channel.enabled)
            .collect();

        let mut channels = Vec::with_capacity(enabled.len());
        let mut samples = Vec::with_capacity(returned * enabled.len());

        for (index, channel) in enabled.iter().enumerate() {
            let buffer = match channel.channel_id {
                ChannelId::Alphabetic('A') => &buffer_a,
                ChannelId::Alphabetic('B') => &buffer_b,
                ChannelId::Alphabetic('C') => &buffer_c,
                ChannelId::Alphabetic('D') => &buffer_d,
                other => {
                    return Err(anyhow::anyhow!(
                        "channel {other} has no ps2000 buffer"
                    ));
                }
            };

            let range_code =
                Self::volts_per_div_to_range(channel.volts_per_div, channel.attenuation);
            let full_scale = Self::raw_range_to_volts(range_code);

            let scale_v_per_count =
                volts_per_count(full_scale, channel.attenuation) as f32;

            channels.push(ChannelFrame {
                channel: channel.channel_id,
                range_code: range_code as u8,
                coupling: channel.coupling,
                scale_v_per_count,
                offset_v: channel.volts_offset as f32,
            });

            // Channel-major: the whole channel appended contiguously, which
            // is what lets the decoder hand out a zero-copy view per channel.
            samples.extend_from_slice(&buffer[..returned]);

            if overflow & (1 << Self::channel_id_to_raw_value(channel.channel_id)) != 0 {
                tracing::debug!(channel = %channel.channel_id, "channel overflowed");
            }
            debug_assert_eq!(samples.len(), (index + 1) * returned);
        }

        // The driver's overflow word is indexed by hardware channel; the
        // frame's mask is indexed by position in `channels`, so that a client
        // can pair it with the descriptors it received.
        let mut overflow_mask = 0u16;
        for (index, channel) in enabled.iter().enumerate() {
            let hardware_bit = 1 << Self::channel_id_to_raw_value(channel.channel_id);
            if overflow & hardware_bit != 0 {
                overflow_mask |= 1 << index;
            }
        }

        Ok(CaptureFrame {
            // Assigned by the acquisition loop, which is the only thing that
            // can number captures monotonically across clients.
            seq: 0,
            capture_mono_ns: monotonic_ns(),
            sample_interval_ns: self.current_time_interval_ns,
            pre_trigger_samples: self.pre_trigger_samples.min(returned as u32),
            post_trigger_samples: returned as u32
                - self.pre_trigger_samples.min(returned as u32),
            samples_per_channel: returned as u32,
            // Every ps2000-family part is 8-bit.
            resolution_bits: 8,
            overflow_mask,
            flags: FLAG_TRIGGERED,
            channels,
            samples,
        })
    }

    fn set_scope_attenuation(
        &mut self,
        channel: ChannelId,
        attenuation: f64,
    ) -> anyhow::Result<()> {
        if attenuation <= 0.0 {
            anyhow::bail!("probe attenuation must be positive, got {attenuation}");
        }

        // Changing the probe means the same signal now arrives at the input
        // divided differently, so a stored level that is correct at the input
        // stops being correct at the tip. Swapping a 1x probe for a 10x one
        // without this turns a 1 V trigger into a 10 V one, which on a 5 V
        // range simply never fires.
        let previous = self.trigger_source_attenuation();

        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .attenuation = attenuation;

        if channel == self.settings.trigger.trigger_source {
            let at_probe = to_probe_volts(self.settings.trigger.trigger_level, previous);
            self.settings.trigger.trigger_level = to_input_volts(at_probe, attenuation);
        }
        Ok(())
    }

    fn get_scope_attenuation(&self, channel: ChannelId) -> anyhow::Result<f64> {
        Ok(self
            .settings
            .channels
            .iter()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .attenuation)
    }
    fn get_memory_depth_cached(&self) -> anyhow::Result<usize> {
        Ok(self.settings.memory_depth.unwrap_or(MIN_MEMORY_DEPTH))
    }

    fn set_scope_time_per_div(&mut self, time_per_div: f64, memory_depth: u32) -> anyhow::Result<()> {
        self.settings.time_per_div = time_per_div;
        let memory_depth = memory_depth as f64;
        tracing::debug!("Memory depth: {}", memory_depth);
        if let Ok((timebase, interval)) = Self::get_timebase_for_sample_rate(
            self.handle,
            memory_depth,
            time_per_div,
        ) {
            self.current_timebase = timebase;
            self.current_time_interval_ns = interval;
        } else {
            return Err(anyhow::anyhow!("Failed to get timebase for sample rate"));
        }
        tracing::debug!("Current timebase: {}", self.current_timebase);
        tracing::debug!("Current time interval: {}", self.current_time_interval_ns);
        Ok(())
    }

    fn do_memory_depth_update(&mut self) -> anyhow::Result<()> {
        if self.is_new_channel_enabled_disabled {
            if self.update_memory_depth().is_err() {
                self.memory_depth_not_update = true;
            }
            self.is_new_channel_enabled_disabled = false;
        }
        Ok(())
    }        
}


impl Oscilloscope for PicoScope2000 {
    fn enable_channel(&mut self, channel: ChannelId) -> anyhow::Result<()> {
        self.enable_scope_channel(channel)
    }

    fn disable_channel(&mut self, channel: ChannelId) -> anyhow::Result<()> {
        self.disable_scope_channel(channel)
    }

    fn is_channel_enabled(&self, channel: ChannelId) -> anyhow::Result<bool> {
        Ok(self
            .settings
            .channels
            .iter()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .enabled)
    }

    fn set_volts_per_div(&mut self, channel: ChannelId, volts_per_div: f64) -> anyhow::Result<()> {
        if !matches!(
            channel,
            ChannelId::Alphabetic('A')
                | ChannelId::Alphabetic('B')
                | ChannelId::Alphabetic('C')
                | ChannelId::Alphabetic('D')
        ) {
            return Err(anyhow::anyhow!("Invalid channel: {}", channel.as_str()));
        }
        self.set_volts_per_div_range(channel, volts_per_div)
    }

    fn get_volts_per_div(&self, channel: ChannelId) -> anyhow::Result<f64> {
        Ok(self
            .settings
            .channels
            .iter()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .volts_per_div)
    }

    /// Refuses any offset but zero: this series has no analog offset.
    ///
    /// `analog_offset: false` in the capabilities is the whole story -- there
    /// is no hardware here to apply an offset to. It was stored anyway and put
    /// in the frame's `offset_v`, which is added back when counts become
    /// volts, so the trace stayed exactly where it was and every measurement
    /// taken from the capture moved instead: Vmax, Vmin and Vavg all reported
    /// a signal that was not there. Better to fail with a reason than to hand
    /// back readings that are quietly wrong.
    ///
    /// Zero still succeeds, so clearing an offset and the usual "set it to the
    /// default" startup both work. Moving a trace on screen is a separate
    /// thing -- the web UI's per-channel position does it without touching
    /// what the capture says.
    fn set_volts_offset(&mut self, channel: ChannelId, volts_offset: f64) -> anyhow::Result<()> {
        reject_unsupported_offset(volts_offset)?;
        // Still resolved, so a bad channel is reported as one.
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .volts_offset = 0.0;
        Ok(())
    }

    fn get_volts_offset(&self, channel: ChannelId) -> anyhow::Result<f64> {
        Ok(self.settings.channels.iter().find(|c| c.channel_id == channel).ok_or(anyhow::anyhow!("Channel not found"))?.volts_offset)
    }

    fn set_coupling(&mut self, channel: ChannelId, coupling: Coupling) -> anyhow::Result<()> {
        reject_unsupported_coupling(coupling)?;
        self.set_ac_dc_coupling(channel, coupling)
    }

    fn get_coupling(&self, channel: ChannelId) -> anyhow::Result<Coupling> {
        Ok(self
            .settings
            .channels
            .iter()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .coupling)
    }

    fn set_trigger_level(&mut self, trigger_level: f64) -> anyhow::Result<()> {
        self.set_scope_trigger_level(trigger_level)
    }

    fn get_trigger_level(&self) -> anyhow::Result<f64> {
        self.get_scope_trigger_level()
    }

    fn set_trigger_source(&mut self, trigger_source: ChannelId) -> anyhow::Result<()> {
        self.set_scope_trigger_source(trigger_source)
    }

    fn get_trigger_source(&self) -> anyhow::Result<ChannelId> {
        self.get_scope_trigger_source()
    }

    fn set_trigger_slope(&mut self, trigger_slope: TriggerSlope) -> anyhow::Result<()> {
        self.set_trigger_direction(trigger_slope)
    }

    fn get_trigger_slope(&self) -> anyhow::Result<TriggerSlope> {
        Ok(self.settings.trigger.trigger_slope)
    }

    fn set_capture_mode(&mut self, capture_mode: CaptureMode) -> anyhow::Result<()> {
        self.set_scope_capture_mode(capture_mode)
    }

    fn get_capture_mode(&self) -> anyhow::Result<CaptureMode> {
        self.get_scope_capture_mode()
    }

    /// Choose a time/div, and re-arm so it takes effect.
    ///
    /// Re-arming here rather than in `set_scope_time_per_div`, which is where
    /// every other setter on this driver does its own: that one is also called
    /// by `update_memory_depth`, which `do_memory_depth_update` calls, which a
    /// re-arm calls in turn -- so re-arming down there recurses until the
    /// hardware thread overflows its stack. Enabling a channel was enough to
    /// do it, that being what makes `do_memory_depth_update` recompute.
    ///
    /// Without a re-arm somewhere, though, a new time/div did not take effect
    /// until something else happened to re-arm, and the frame it eventually
    /// produced was stamped with the interval in force when it was read rather
    /// than the one it was captured at -- so the samples and the time axis came
    /// from different settings.
    fn set_time_per_div(&mut self, time_per_div: f64) -> anyhow::Result<()> {
        self.set_scope_time_per_div(time_per_div, self.memory_depth)?;
        if self.is_capturing {
            self.stop_triggering()?;
            self.do_update_channel()?;
            self.is_capturing = true;
            self.do_update_trigger()?;
            self.start_triggered_capture(self.settings.trigger.trigger_position)?;
        }
        Ok(())
    }

    /// The time/div the capture is actually running at, not the one asked for.
    ///
    /// A scope has a fixed set of sample intervals, so an arbitrary time/div
    /// cannot be honoured exactly: asking a 2204A for 1 ms/div lands on the
    /// 1280 ns interval and captures 1.024 ms per division. Returning the
    /// request hid that, and hid it worst where it matters -- anything
    /// converting divisions to seconds, like the horizontal position, was
    /// working from a number the hardware had already rounded away from.
    fn get_time_per_div(&self) -> anyhow::Result<f64> {
        if self.current_time_interval_ns <= 0.0 || self.memory_depth == 0 {
            // Nothing captured yet, so there is no achieved value to report
            // and the request is the best answer available.
            return Ok(self.settings.time_per_div);
        }
        let span_seconds =
            self.memory_depth as f64 * self.current_time_interval_ns / NANOSECONDS_PER_SECOND;
        Ok(span_seconds / TOTAL_NUM_TIME_DIVISIONS as f64)
    }

    fn set_time_offset(&mut self, time_offset: f64) -> anyhow::Result<()> {
        self.settings.time_offset = time_offset;
        Ok(())
    }

    fn get_time_offset(&self) -> anyhow::Result<f64> {
        Ok(self.settings.time_offset)
    }

    fn set_cursor_position(&mut self, _cursor: Cursor) -> anyhow::Result<()> {
        Ok(())
    }

    fn get_cursor_position(&self, _cursor: Cursor) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_horizontal_cursor_delta(&self) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_vertical_cursor_delta(&self) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    // The measurements below were previously hardcoded to Ok(0.0), which
    // reported a plausible-looking zero instead of an error. They are now
    // computed from a capture by the shared measure module, so the CLI, the
    // web UI and the Python API all get the same number.
    fn measure_duty_cycle(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.measure(channel, Measurement::DutyCyclePositive)
    }

    fn measure_frequency(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.measure(channel, Measurement::Frequency)
    }

    fn measure_period(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.measure(channel, Measurement::Period)
    }

    fn measure_rms(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.measure(channel, Measurement::Vrms)
    }

    fn measure_peak_to_peak(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.measure(channel, Measurement::Vpp)
    }

    fn measure_average(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.measure(channel, Measurement::Vavg)
    }

    fn measure_min(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.measure(channel, Measurement::Vmin)
    }

    fn get_data(&self, channel: ChannelId) -> anyhow::Result<Vec<f64>> {
        let frame = self.get_triggered_scope_data()?;
        let index = frame
            .channels
            .iter()
            .position(|c| c.channel == channel)
            .ok_or_else(|| anyhow::anyhow!("channel {channel} is not enabled"))?;
        frame
            .channel_volts(index)
            .ok_or_else(|| anyhow::anyhow!("capture held no samples for channel {channel}"))
    }

    /// The rate captures are actually running at, not the unit's maximum.
    ///
    /// These differ by a lot: a 2204A tops out at 100 MS/s, but at 1 ms/div
    /// it samples at about 780 kS/s. Reporting the maximum made every caller
    /// that divides the depth by the rate to get the length of the capture
    /// window wrong by that ratio -- including the horizontal position, which
    /// converts a time offset into a fraction of the window. The maximum is
    /// still available, as `max_sample_rate_hz` in the capabilities.
    fn get_sample_rate(&self) -> anyhow::Result<f64> {
        if self.current_time_interval_ns <= 0.0 {
            return self.get_sample_rate_from_device();
        }
        Ok(1e9 / self.current_time_interval_ns)
    }

    fn get_memory_depth(&self) -> anyhow::Result<usize> {
        self.get_memory_depth_from_device()
    }

    fn get_bandwidth(&self) -> anyhow::Result<f64> {
        self.get_bandwidth_from_device()
    }

    fn get_channel_count(&self) -> anyhow::Result<usize> {
        self.get_channel_count_from_device()
    }

    fn start_triggered_capture(&mut self, trigger_position_percent: f64) -> anyhow::Result<()> {
        self.run_block(trigger_position_percent)
    }

    fn stop_triggered_capture(&mut self) -> anyhow::Result<()> {
        self.stop_triggering()
    }

    fn is_ready(&self) -> anyhow::Result<bool> {
        self.is_scope_ready()
    }

    fn get_triggered_data(&self) -> anyhow::Result<CaptureFrame> {
        self.get_triggered_scope_data()
    }

    fn set_attenuation(&mut self, channel: ChannelId, attenuation: f64) -> anyhow::Result<()> {
        self.set_scope_attenuation(channel, attenuation)
    }

    fn get_attenuation(&self, channel: ChannelId) -> anyhow::Result<f64> {
        self.get_scope_attenuation(channel)
    }

    fn get_trigger_position(&self) -> anyhow::Result<f64> {
        Ok(self.settings.trigger.trigger_position)
    }

    fn force_trigger(&mut self) -> anyhow::Result<()> {
        // ps2000 has no ForceTrigger entry point, unlike the *a APIs. The
        // equivalent is to re-arm with auto-trigger, which fires after the
        // timeout whether or not the condition is met.
        let previous = self.settings.trigger.capture_mode;
        self.set_scope_capture_mode(CaptureMode::Auto)?;
        let position = self.settings.trigger.trigger_position;
        let result = self.run_block(position);
        self.settings.trigger.capture_mode = previous;
        result
    }

    fn capabilities(&self) -> anyhow::Result<ScopeCapabilities> {
        Ok(self.capabilities.clone())
    }

    fn suggested_poll_interval(&self) -> std::time::Duration {
        // The driver tells us how long the capture will take. Polling faster
        // cannot produce data, and polling on a fixed 10 ms interval (as this
        // did before) both wastes cycles on slow captures and caps the rate
        // on fast ones. A tenth of the expected duration keeps the added
        // latency under 10% of the capture time, floored so a sub-millisecond
        // capture does not spin a core.
        let tenth = self.expected_capture_ms / 10;
        std::time::Duration::from_millis(tenth.clamp(1, 20))
    }
}

impl PicoScope2000 {
    /// Compute one measurement from a fresh capture.
    fn measure(&self, channel: ChannelId, which: Measurement) -> anyhow::Result<f64> {
        let frame = self.get_triggered_scope_data()?;
        let index = frame
            .channels
            .iter()
            .position(|c| c.channel == channel)
            .ok_or_else(|| {
                anyhow::anyhow!("channel {channel} is not enabled, so there is nothing to measure")
            })?;
        let set = measure_channel(&frame, index)
            .ok_or_else(|| anyhow::anyhow!("capture held no samples for channel {channel}"))?;
        set.get(which).ok_or_else(|| {
            anyhow::anyhow!(
                "{:?} needs at least one full cycle in the capture window; \
                 try a slower time/div",
                which
            )
        })
    }
}

impl Drop for PicoScope2000 {
    fn drop(&mut self) {
        // Without this the USB handle leaks and the next open fails until
        // the device is physically replugged, which on a remote box means a
        // site visit.
        if self.is_capturing {
            let _ = self.stop_triggering();
        }
        // Drop cannot propagate, and the driver must already be loaded for
        // this instance to exist, so a failure here is only worth logging.
        match ps2000() {
            Ok(api) => {
                let result = unsafe { api.ps2000_close_unit(self.handle) };
                if result == 0 {
                    tracing::warn!(handle = self.handle, "ps2000_close_unit failed");
                } else {
                    tracing::info!(handle = self.handle, "closed PicoScope");
                }
            }
            Err(e) => tracing::warn!(error = %e, "cannot close unit: driver unavailable"),
        }
    }
}

/// A probe divides the signal before it reaches the input, so the two ends of
/// the cable disagree about what "1 volt" means.
///
/// Every voltage crossing the driver boundary is at the probe tip, because
/// that is where the user's signal is; everything converting to ADC counts is
/// at the input. These two functions are the only places that conversion
/// happens, so a setter and its getter cannot disagree about the direction --
/// which they did, leaving a trigger level set through a 10x probe reading
/// back ten times too small.
fn to_input_volts(probe_volts: f64, attenuation: f64) -> f64 {
    if attenuation <= 0.0 {
        return probe_volts;
    }
    probe_volts / attenuation
}

fn to_probe_volts(input_volts: f64, attenuation: f64) -> f64 {
    if attenuation <= 0.0 {
        return input_volts;
    }
    input_volts * attenuation
}

/// Split a block into pre- and post-trigger samples for a trigger position.
///
/// The percent is where the trigger sits in the block, so 50 centres it and
/// 0 puts the whole window after it. Out-of-range values are clamped rather
/// than refused: the callers that produce them are converting a time offset
/// the user asked for, and clamping is what "as far as the block reaches"
/// means.
///
/// A free function so it can be tested: building a `PicoScope2000` needs a
/// device handle, and this arithmetic is where the window comes from.
fn trigger_position_split(memory_depth: u32, percent: f64) -> (u32, u32) {
    let clamped_percent = percent.clamp(0.0, 100.0);
    let mut pre = (memory_depth as f64 * clamped_percent / 100.0) as u32;
    let mut post = memory_depth.saturating_sub(pre);

    // One sample either side at the extremes: a block that is entirely pre-
    // or post-trigger leaves ps2000 nothing to align the trigger to.
    if pre == 0 {
        pre = 1;
        post = memory_depth.saturating_sub(1);
    }
    if post == 0 {
        post = 1;
        pre = memory_depth.saturating_sub(1);
    }
    (pre, post)
}

/// Rejects ground coupling, which this series has no input switch for.
///
/// `ps2000_set_channel` takes one flag for coupling, DC or AC, so GND had
/// nowhere to go: it fell to the same raw value as AC and the input stayed
/// live, while `get_coupling` returned the GND it had stored. Both halves
/// were wrong, and together they are worse than either -- the readback
/// agrees with a setting the hardware never applied, so the trace looks like
/// a grounded input that is drifting rather than a live one.
fn reject_unsupported_coupling(coupling: Coupling) -> anyhow::Result<()> {
    if coupling == Coupling::GND {
        anyhow::bail!(
            "this scope has no ground coupling, so GND cannot be applied; \
             the input would stay live while the setting read back as GND. \
             Use AC to block the DC component, or unplug the probe to see \
             where zero sits"
        );
    }
    Ok(())
}

/// Rejects any volts offset but zero, since this series has no analog offset.
///
/// A free function so it can be tested: building a `PicoScope2000` needs a
/// device handle, and the refusal is the whole point here.
///
/// `analog_offset: false` in the capabilities says there is no hardware to
/// apply an offset to. The value was stored anyway and put in the frame's
/// `offset_v`, which is added back when counts become volts, so the trace
/// stayed put and the measurements moved instead -- Vmax, Vmin and Vavg all
/// describing a signal that was not there. Failing with a reason beats
/// handing back readings that are quietly wrong.
fn reject_unsupported_offset(volts_offset: f64) -> anyhow::Result<()> {
    // Zero still succeeds: clearing an offset, and the "set everything to its
    // default" a client does on startup, both have to work. `-0.0 != 0.0` is
    // false in IEEE 754, so a negative zero passes here too.
    if volts_offset != 0.0 {
        anyhow::bail!(
            "this scope has no analog offset, so {volts_offset} V cannot be \
             applied; it would move the measurements rather than the trace. \
             Use the vertical position control to move a trace on screen"
        );
    }
    Ok(())
}

/// Volts at the probe tip per ADC count, the factor a client multiplies a
/// raw count by.
///
/// `full_scale_input_volts` is the selected range's full-scale deflection at
/// the input, so the probe factor has to be applied here: a frame carries no
/// attenuation field, and a client that had to apply it would need a setting
/// it cannot see. It also keeps a capture in the same units as volts/div and
/// the trigger level, which are at the tip -- these disagreed, so with the
/// default 10x probe a trace was drawn a tenth of its height and every
/// measurement read a tenth of its value. modern_scope already did this.
fn volts_per_count(full_scale_input_volts: f64, attenuation: f64) -> f64 {
    // A non-positive attenuation is meaningless and would zero or invert
    // every sample, so it is treated as the 1x it most likely meant --
    // matching to_input_volts, which passes such a level through untouched.
    let factor = if attenuation > 0.0 { attenuation } else { 1.0 };
    full_scale_input_volts * factor / PS2000_MAX_VALUE as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_level_survives_the_round_trip_through_any_probe() {
        for attenuation in [1.0, 10.0, 100.0, 0.5] {
            for level in [0.0, 0.25, 1.0, -0.5, 12.5] {
                let stored = to_input_volts(level, attenuation);
                let read_back = to_probe_volts(stored, attenuation);
                assert!(
                    (read_back - level).abs() < 1e-12,
                    "{level} V through a {attenuation}x probe read back as {read_back}"
                );
            }
        }
    }

    #[test]
    fn a_ten_x_probe_puts_a_tenth_of_the_level_at_the_input() {
        // The hardware sees a tenth of what the user asked for, which is
        // what has to reach voltage_to_adc_counts.
        assert!((to_input_volts(1.0, 10.0) - 0.1).abs() < 1e-12);
        assert!((to_probe_volts(0.1, 10.0) - 1.0).abs() < 1e-12);
    }

    #[test]
    fn swapping_probes_keeps_the_level_where_the_user_put_it() {
        // What set_scope_attenuation does: read the level back out at the old
        // probe, then store it against the new one. The tip-referred level is
        // the one the user chose, so it is the one that has to survive.
        let stored_at_1x = to_input_volts(1.0, 1.0);

        let at_probe = to_probe_volts(stored_at_1x, 1.0);
        let stored_at_10x = to_input_volts(at_probe, 10.0);

        assert!((to_probe_volts(stored_at_10x, 10.0) - 1.0).abs() < 1e-12);
        // And the input really does see a tenth, which is the point.
        assert!((stored_at_10x - 0.1).abs() < 1e-12);
    }

    #[test]
    fn a_nonsensical_attenuation_passes_the_level_through_untouched() {
        // Dividing by zero would poison the level with an infinity that then
        // reaches the driver as a garbage ADC count.
        assert_eq!(to_input_volts(1.5, 0.0), 1.5);
        assert_eq!(to_probe_volts(1.5, -1.0), 1.5);
    }

    #[test]
    fn a_full_scale_count_converts_to_the_range_at_the_probe_tip() {
        // The ±1 V range through a 1x probe: full deflection is 1 V.
        let scale = volts_per_count(1.0, 1.0);
        let full = scale * PS2000_MAX_VALUE as f64;
        assert!((full - 1.0).abs() < 1e-12, "full scale came back as {full}");
    }

    #[test]
    fn a_ten_x_probe_makes_a_count_worth_ten_times_as_much() {
        // The regression: a capture in volts at the input while volts/div and
        // the trigger level were in volts at the tip, so a 10x probe drew
        // every trace a tenth of its height.
        let at_1x = volts_per_count(1.0, 1.0);
        let at_10x = volts_per_count(1.0, 10.0);
        assert!((at_10x - at_1x * 10.0).abs() < 1e-18);

        // Which is to say: the ±1 V range spans 10 V at the tip.
        let full = at_10x * PS2000_MAX_VALUE as f64;
        assert!((full - 10.0).abs() < 1e-12, "full scale came back as {full}");
    }

    #[test]
    fn a_count_agrees_with_the_trigger_level_conversion() {
        // Both directions must land on the same tip-referred volt, or a
        // trigger set to the top of the screen would not sit there.
        let attenuation = 10.0;
        let full_scale_input = 1.0;

        // A level at the top of the ±1 V range, expressed at the tip.
        let at_tip = to_probe_volts(full_scale_input, attenuation);
        let counts = at_tip / volts_per_count(full_scale_input, attenuation);

        assert!(
            (counts - PS2000_MAX_VALUE as f64).abs() < 1e-9,
            "the top of the range came to {counts} counts"
        );
    }

    #[test]
    fn a_nonsensical_attenuation_leaves_the_scale_at_1x() {
        // Zero would flatten every sample to 0 V and a negative would invert
        // the trace, both of which look like a hardware fault.
        assert_eq!(volts_per_count(1.0, 0.0), volts_per_count(1.0, 1.0));
        assert_eq!(volts_per_count(1.0, -10.0), volts_per_count(1.0, 1.0));
    }

    #[test]
    fn an_offset_this_scope_cannot_apply_is_refused() {
        // It used to be accepted and quietly folded into the frame, where it
        // was added back on the way to volts: the trace did not move and
        // every measurement did.
        let refused = reject_unsupported_offset(2.0).expect_err("2 V should be refused");
        let message = format!("{refused}");
        assert!(
            message.contains("no analog offset"),
            "should say why, got {message:?}"
        );
        assert!(
            message.contains("measurements"),
            "and what it would have done instead, got {message:?}"
        );
        assert!(
            message.contains("position"),
            "and point at the control that does work, got {message:?}"
        );
    }

    #[test]
    fn no_offset_at_all_is_still_allowed() {
        // Clearing an offset, and a client pushing defaults on startup, both
        // send zero; refusing that would break setup rather than protect it.
        assert!(reject_unsupported_offset(0.0).is_ok());
        assert!(reject_unsupported_offset(-0.0).is_ok());
    }

    #[test]
    fn ground_coupling_is_refused_rather_than_silently_ac() {
        let message = match reject_unsupported_coupling(Coupling::GND) {
            Ok(()) => panic!("GND was accepted on a scope with no ground switch"),
            Err(e) => e.to_string(),
        };
        assert!(
            message.contains("ground coupling"),
            "say which setting was refused, got {message:?}"
        );
        assert!(
            message.contains("AC"),
            "point at what to use instead, got {message:?}"
        );
    }

    #[test]
    fn the_couplings_this_scope_has_are_still_accepted() {
        assert!(reject_unsupported_coupling(Coupling::DC).is_ok());
        assert!(reject_unsupported_coupling(Coupling::AC).is_ok());
    }

    #[test]
    fn a_centred_trigger_splits_the_block_in_half() {
        assert_eq!(trigger_position_split(8000, 50.0), (4000, 4000));
    }

    #[test]
    fn moving_the_trigger_moves_the_split() {
        // The whole point of the position: a quarter of the block before the
        // trigger means three quarters of the window is signal after it.
        assert_eq!(trigger_position_split(8000, 25.0), (2000, 6000));
        assert_eq!(trigger_position_split(8000, 75.0), (6000, 2000));
    }

    #[test]
    fn every_position_accounts_for_the_whole_block() {
        // A split that loses or invents samples would shorten or overrun the
        // capture rather than move it.
        for percent in [0.0, 1.0, 25.0, 33.3, 50.0, 66.7, 99.0, 100.0] {
            let (pre, post) = trigger_position_split(8000, percent);
            assert_eq!(pre + post, 8000, "at {percent}% the block is not whole");
        }
    }

    #[test]
    fn the_extremes_keep_a_sample_either_side() {
        // ps2000 has nothing to align to in a block that is entirely one
        // side of the trigger, so the ends of the travel stop one short.
        assert_eq!(trigger_position_split(8000, 0.0), (1, 7999));
        assert_eq!(trigger_position_split(8000, 100.0), (7999, 1));
    }

    #[test]
    fn a_position_past_the_end_of_the_block_is_clamped() {
        // Callers convert a time offset the user asked for, which can be
        // further than one window; that means "as far as the block reaches".
        assert_eq!(
            trigger_position_split(8000, 150.0),
            trigger_position_split(8000, 100.0)
        );
        assert_eq!(
            trigger_position_split(8000, -50.0),
            trigger_position_split(8000, 0.0)
        );
    }

    /// The recursion that overflowed the hardware thread's stack.
    ///
    /// `update_memory_depth` calls `set_scope_time_per_div`, and a re-arm
    /// calls `do_memory_depth_update`, which calls `update_memory_depth`. So
    /// anything that re-arms, put inside `set_scope_time_per_div`, closes a
    /// loop with no base case. Enabling a channel was enough to enter it,
    /// that being what makes `do_memory_depth_update` recompute, and the
    /// daemon aborted with "thread 'scope-hw' has overflowed its stack" every
    /// time a channel was switched on. The re-arm belongs in the trait's
    /// `set_time_per_div`, which nothing downstream calls.
    ///
    /// Checked by reading the source, there being no scope to drive here.
    #[test]
    fn the_internal_timebase_setter_does_not_re_arm() {
        let source = include_str!("ps2000.rs");
        let body = source
            .split("fn set_scope_time_per_div(")
            .nth(1)
            .expect("set_scope_time_per_div exists")
            .split("\n    fn ")
            .next()
            .expect("the function ends");

        for reentrant in [
            "do_memory_depth_update",
            "start_triggered_capture",
            "stop_triggering",
        ] {
            assert!(
                !body.contains(reentrant),
                "set_scope_time_per_div calls {reentrant}, which reaches it \
                 again through update_memory_depth: the hardware thread will \
                 overflow its stack the next time a channel is enabled"
            );
        }
    }

    /// And the re-arm is where it can be reached without recursing.
    #[test]
    fn the_public_timebase_setter_does_re_arm() {
        let source = include_str!("ps2000.rs");
        let body = source
            .split("fn set_time_per_div(")
            .nth(1)
            .expect("set_time_per_div exists")
            .split("\n    fn ")
            .next()
            .expect("the function ends");

        assert!(
            body.contains("start_triggered_capture"),
            "a new time/div takes effect only on a re-arm, so without one the \
             timebase does not change until something else arms the scope"
        );
    }
}

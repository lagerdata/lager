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

use async_trait::async_trait;
use protocol::StreamingSample;
use protocol::TriggeredCapture;

// Suppress warnings from generated bindings
#[allow(
    non_camel_case_types,
    non_upper_case_globals,
    non_snake_case,
    dead_code,
    unused_imports
)]
mod bindings {
    include!(concat!(env!("OUT_DIR"), "/ps2000_bindings.rs"));
}

// Re-export everything from bindings
use bindings::*;

const NANOSECONDS_PER_SECOND: f64 = 1_000_000_000.0;
const MIN_MEMORY_DEPTH: usize = 8000;
const TOTAL_NUM_TIME_DIVISIONS: usize = 8;
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
}

impl PicoScope2000 {
    const MAX_NUM_DEVICES: u32 = 64;
    pub fn new() -> Result<Self> {
        for _ in 0..Self::MAX_NUM_DEVICES {
            let handle = unsafe { ps2000_open_unit() };
            if handle > 0 {
                let settings = Self::initial_settings(handle)?;
                let (current_timebase, current_time_interval_ns) =
                    Self::get_timebase_for_sample_rate(
                        handle,
                        settings.memory_depth.unwrap_or(MIN_MEMORY_DEPTH) as f64,
                        settings.time_per_div,
                    )?;

                println!("Current timebase: {}", current_timebase);
                println!("Current time interval: {}", current_time_interval_ns);
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
                });
            }
        }
        Err(anyhow::anyhow!("No PicoScope 2000 found"))
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
            Self::set_channel_to_default(handle, channel_id);
        }

        Self::set_trigger_to_default(handle);

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
        println!("[DEBUG disable_scope_channel] Disabling channel {}", channel.as_str());
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .enabled = false;
        self.is_new_channel_enabled_disabled = true;
        println!("[DEBUG disable_scope_channel] is_capturing={}", self.is_capturing);
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
        println!("[DEBUG disable_scope_channel] Channel {} disabled successfully", channel.as_str());
        Ok(())
    }

    fn enable_scope_channel(&mut self, channel: ChannelId) -> anyhow::Result<()> {
        println!("[DEBUG enable_scope_channel] Enabling channel {}", channel.as_str());
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .enabled = true;
        self.is_new_channel_enabled_disabled = true;
        println!("[DEBUG enable_scope_channel] is_capturing={}, calling do_update_channel if capturing",
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
        println!("[DEBUG enable_scope_channel] Channel {} enabled successfully", channel.as_str());
        Ok(())
    }

    fn set_channel_to_default(handle: i16, channel: ChannelId) {
        let channel_id_raw_value = Self::channel_id_to_raw_value(channel);
        let is_enabled_raw_value = Self::is_enabled_to_raw_value(false);
        let coupling_raw_value = Self::coupling_to_raw_value(Coupling::DC);
        let range = Self::volts_per_div_to_range(1.0, DEFAULT_ATTENUATION);
        let _ = unsafe {
            ps2000_set_channel(
                handle,
                channel_id_raw_value,
                is_enabled_raw_value,
                coupling_raw_value,
                range,
            )
        };
    }

    fn set_trigger_to_default(handle: i16) {
        let mut trigger_conditions = PS2000_TRIGGER_CONDITIONS {
            channelA: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            channelB: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            channelC: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            channelD: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            external: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
            pulseWidthQualifier: enPS2000TriggerState_PS2000_CONDITION_DONT_CARE,
        };

        let _ = unsafe {
            ps2000SetAdvTriggerChannelConditions(handle, &mut trigger_conditions, 0 as i16)
        };

        let _ = unsafe {
            ps2000SetAdvTriggerChannelDirections(
                handle,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING as u32,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING as u32,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING as u32,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING as u32,
                enPS2000ThresholdDirection_PS2000_RISING_OR_FALLING as u32,
            )
        };

        let mut trigger_channel_properties = PS2000_TRIGGER_CHANNEL_PROPERTIES {
            thresholdMajor: 0 as i16,
            thresholdMinor: 0 as i16,
            hysteresis: 0 as u16,
            channel: 0 as i16,
            thresholdMode: enPS2000ThresholdMode_PS2000_WINDOW,
        };
        let _ = unsafe {
            ps2000SetAdvTriggerChannelProperties(
                handle,
                &mut trigger_channel_properties,
                0 as i16,
                0 as i32,
            )
        };

        let _ =
            unsafe { ps2000SetAdvTriggerDelay(handle, 0 as u32, -DEFAULT_TRIGGER_POSITION as f32) };
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
        let mut info_string = vec![0i8; 256];

        let result = unsafe {
            ps2000_get_unit_info(
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
        println!("Info: {}", info);
        Ok(info)
    }

    pub fn close(&self) -> anyhow::Result<()> {
        unsafe { ps2000_close_unit(self.handle) };
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

    fn raw_adc_counts_to_voltage(adc_counts: i16, range: f64) -> f64 {
        (adc_counts as f64) / (i16::MAX as f64 / range)
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
        let attenuation = self
            .settings
            .channels
            .iter()
            .find(|c| c.channel_id == self.settings.trigger.trigger_source)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .attenuation;
        self.settings.trigger.trigger_level = trigger_level / attenuation;
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
        let enabled_count = self.settings.channels.iter().filter(|c| c.enabled).count();
        println!("[DEBUG do_update_channel] Starting update, {} channel(s) enabled", enabled_count);

        if enabled_count == 0 {
            println!("[DEBUG do_update_channel] WARNING: No channels are enabled!");
        }

        for channel in &self.settings.channels {
            println!("[DEBUG do_update_channel] Setting channel: {} enabled={}",
                channel.channel_id.as_str(), channel.enabled);
            let channel_id_raw_value = Self::channel_id_to_raw_value(channel.channel_id);
            let is_enabled_raw_value = Self::is_enabled_to_raw_value(channel.enabled);
            let coupling_raw_value = Self::coupling_to_raw_value(channel.coupling);
            let range =
                Self::volts_per_div_to_range(channel.volts_per_div, channel.attenuation);
            println!(
                "[DEBUG do_update_channel] Calling ps2000_set_channel: handle={}, id={}, enabled={}, coupling={}, range={}",
                self.handle, channel_id_raw_value, is_enabled_raw_value, coupling_raw_value, range
            );
            let result = unsafe {
                ps2000_set_channel(
                    self.handle,
                    channel_id_raw_value,
                    is_enabled_raw_value,
                    coupling_raw_value,
                    range,
                )
            };
            println!("[DEBUG do_update_channel] ps2000_set_channel returned: {}", result);
            if result == 0 {
                println!("[DEBUG do_update_channel] FAILED - ps2000_set_channel returned 0 for channel {}",
                    channel.channel_id.as_str());
                return Err(anyhow::anyhow!(
                    "Failed to set volts per div for channel {}",
                    channel.channel_id.as_str()
                ));
            } else {
                println!("[DEBUG do_update_channel] Channel {} configured successfully",
                    channel.channel_id.as_str());
            }
        }
        println!("[DEBUG do_update_channel] All channels updated successfully");
        Ok(())
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
                println!("Updated memory depth: {}", memory_depth);
                self.memory_depth = memory_depth;
                // Clamp trigger position to valid range (0-100%)
                let clamped_percent = self.settings.trigger.trigger_position.clamp(0.0, 100.0);
                self.pre_trigger_samples = (self.memory_depth as f64 * clamped_percent / 100.0) as u32;
                self.post_trigger_samples = self.memory_depth.saturating_sub(self.pre_trigger_samples);

                // Ensure we have at least some samples for both pre and post trigger
                if self.pre_trigger_samples == 0 {
                    self.pre_trigger_samples = 1;
                    self.post_trigger_samples = self.memory_depth.saturating_sub(1);
                }
                if self.post_trigger_samples == 0 {
                    self.post_trigger_samples = 1;
                    self.pre_trigger_samples = self.memory_depth.saturating_sub(1);
                }                
                Ok(())
            },
            Err(e) => {
                Err(e)
            }
        }
    }

    fn do_update_trigger(&mut self) -> anyhow::Result<()> {
        let trigger_source_raw_value =
            Self::channel_id_to_raw_value(self.settings.trigger.trigger_source);
        let trigger_level_adc_count = self.voltage_to_adc_counts(
            self.settings.trigger.trigger_level,
            self.settings.trigger.trigger_source,
        )?;
        let direction_raw_value =
            Self::get_trigger_direction_value(self.settings.trigger.trigger_slope);
        let delay_raw_value = Self::safe_f64_to_i16(self.settings.trigger.delay).unwrap_or(0);
        println!("raw source: {}", trigger_source_raw_value);
        println!("raw level: {}", trigger_level_adc_count);
        println!("raw direction: {}", direction_raw_value);
        println!("raw delay: {}", delay_raw_value);
        let auto_trigger_ms = Self::get_auto_trigger_ms(self.settings.trigger.capture_mode, 500);

        let mut trigger_conditions = match self.settings.trigger.trigger_source {
            ChannelId::Alphabetic('A') => {
                println!("Trigger conditions set successfully for channel A");
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
                println!("Trigger conditions set successfully for channel B");
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
                println!("Trigger conditions set successfully for channel C");
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
                println!("Trigger conditions set successfully for channel D");
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
            ps2000SetAdvTriggerChannelConditions(self.handle, &mut trigger_conditions, 1i16)
        };
        if result == 0 {
            return Err(anyhow::anyhow!("Failed to set trigger conditions"));
        } else {
            println!("Trigger conditions set successfully",);
        }
        // Set trigger direction only for the active trigger source channel
        // Set unused channels to RISING_OR_FALLING to avoid conflicts
        let trigger_source_channel = Self::channel_id_to_raw_value(self.settings.trigger.trigger_source);
        let permissive_direction = enPS2000ThresholdDirection_PS2000_ADV_NONE as u32;
        let active_direction = direction_raw_value as u32;
        
        let result = unsafe {
            ps2000SetAdvTriggerChannelDirections(
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
            println!("Trigger directions set successfully");
        }

        let mut trigger_channel_properties =
            self.create_trigger_channel_properties(enPS2000ThresholdMode_PS2000_LEVEL);
        let result = unsafe {
            ps2000SetAdvTriggerChannelProperties(
                self.handle,
                &mut trigger_channel_properties,
                1i16,
                auto_trigger_ms as i32,
            )
        };
        if result == 0 {
            return Err(anyhow::anyhow!("Failed to set trigger channel properties"));
        } else {
            println!("Trigger channel properties set successfully");
        }
        let pre_trigger_delay = -100.0
            * (self.pre_trigger_samples as f64
                / ((self.pre_trigger_samples + self.post_trigger_samples) as f64));
        println!("Pre trigger delay: {}", pre_trigger_delay);
        let result = unsafe {
            ps2000SetAdvTriggerDelay(
                self.handle,
                delay_raw_value as u32,
                pre_trigger_delay as f32,
            )
        };
        if result == 0 {
            return Err(anyhow::anyhow!("Failed to set trigger delay"));
        } else {
            println!("Trigger delay set successfully {}", pre_trigger_delay);
        }
        Ok(())
    }

    fn get_scope_trigger_level(&self) -> anyhow::Result<f64> {
        Ok(self.settings.trigger.trigger_level)
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

    fn get_timebase_for_sample_rate(
        handle: i16,
        memory_depth: f64,
        time_per_div: f64,
    ) -> anyhow::Result<(i16, f64)> {
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
                ps2000_get_timebase(
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
                timebase_found = true;
                let actual_interval = time_interval as f64;
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
        println!("[DEBUG run_block] Starting with trigger_position_percent={}", trigger_position_percent);
        println!("[DEBUG run_block] is_capturing={}, memory_depth={}, timebase={}",
            self.is_capturing, self.memory_depth, self.current_timebase);

        self.settings.trigger.trigger_position = trigger_position_percent;

        // Always stop any existing capture to ensure clean state
        // This fixes the stuck state issue where is_capturing=true but no active capture
        if self.is_capturing {
            println!("[DEBUG run_block] Stopping existing capture to reset state");
            let _ = self.stop_triggering(); // Ignore error, just reset state
        }

        // Now update channel/trigger/memory_depth (always, since we just stopped)
        println!("[DEBUG run_block] Updating channel/trigger/memory_depth");
        self.do_update_channel()?;
        self.do_update_trigger()?;
        self.do_memory_depth_update()?;
        if self.memory_depth_not_update {
            println!("[DEBUG run_block] memory_depth_not_update=true, stopping and updating");
            self.stop_triggering()?;
            if self.update_memory_depth().is_ok() {
                self.memory_depth_not_update = false;
            }
        }

        let mut time_indisposed_ms = 0i32;
        println!("[DEBUG run_block] Calling ps2000_run_block with: handle={}, memory_depth={}, timebase={}, oversample=1",
            self.handle, self.memory_depth, self.current_timebase);

        let result = unsafe {
            ps2000_run_block(
                self.handle,
                self.memory_depth as i32,
                self.current_timebase,
                1, //oversample,
                &mut time_indisposed_ms,
            )
        };

        println!("[DEBUG run_block] ps2000_run_block returned: result={}, time_indisposed_ms={}",
            result, time_indisposed_ms);

        if result == 0 {
            println!("[DEBUG run_block] FAILED - ps2000_run_block returned 0");
            Err(anyhow::anyhow!("Failed to run block"))
        } else {
            println!("[DEBUG run_block] SUCCESS - setting is_capturing=true");
            self.is_capturing = true;
            Ok(())
        }
    }

    fn stop_triggering(&mut self) -> anyhow::Result<()> {
        println!("[DEBUG stop_triggering] Calling ps2000_stop, current is_capturing={}", self.is_capturing);
        let result = unsafe { ps2000_stop(self.handle) };
        println!("[DEBUG stop_triggering] ps2000_stop returned: {}", result);
        if result == 0 {
            println!("[DEBUG stop_triggering] FAILED - ps2000_stop returned 0");
            Err(anyhow::anyhow!("Failed to stop triggering"))
        } else {
            println!("[DEBUG stop_triggering] SUCCESS - setting is_capturing=false");
            self.is_capturing = false;
            if matches!(self.settings.trigger.capture_mode, CaptureMode::Single) {
                println!("[DEBUG stop_triggering] Changing capture_mode from Single to Normal");
                self.settings.trigger.capture_mode = CaptureMode::Normal;
            }
            Ok(())
        }
    }
    fn is_scope_ready(&self) -> anyhow::Result<bool> {
        let result = unsafe { ps2000_ready(self.handle) };
        println!("[DEBUG is_scope_ready] ps2000_ready returned: result={}, is_capturing={}",
            result, self.is_capturing);

        match result {
            0 => {
                println!("[DEBUG is_scope_ready] Returning false (result=0, not ready)");
                Ok(false)
            }
            n if n > 0 && self.is_capturing => {
                println!("[DEBUG is_scope_ready] Returning true (result={}, is_capturing=true)", n);
                Ok(true)
            }
            n if n < 0 => {
                println!("[DEBUG is_scope_ready] ERROR - negative result: {}", n);
                Err(anyhow::anyhow!("PicoScope error: {}", n))
            }
            n => {
                // NOTE: This branch is hit when n > 0 but is_capturing is false
                // This may be a bug - if the scope says it's ready, we should return true
                println!("[DEBUG is_scope_ready] WARNING: result={} but is_capturing={}, returning false",
                    n, self.is_capturing);
                Ok(false)
            }
        }
    }

    fn get_triggered_scope_data(&self) -> anyhow::Result<TriggeredCapture> {
        let total_samples = self.pre_trigger_samples + self.post_trigger_samples;

        // Allocate buffers for each channel (A, B, C, D)
        let mut buffer_a = vec![0i16; total_samples as usize];
        let mut buffer_b = vec![0i16; total_samples as usize];
        let mut buffer_c = vec![0i16; total_samples as usize];
        let mut buffer_d = vec![0i16; total_samples as usize];

        // Call ps2000_get_values to retrieve the data
        let mut overflow = 0i16;
        let result = unsafe {
            ps2000_get_values(
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
            return Err(anyhow::anyhow!("Failed to get triggered data"));
        }

        let mut samples = Vec::new();

        for channel in &self.settings.channels {
            if channel.enabled {
                let buffer = match channel.channel_id {
                    ChannelId::Alphabetic('A') => &buffer_a,
                    ChannelId::Alphabetic('B') => &buffer_b,
                    ChannelId::Alphabetic('C') => &buffer_c,
                    ChannelId::Alphabetic('D') => &buffer_d,
                    _ => continue,
                };
                let voltage_range = Self::raw_range_to_volts(Self::volts_per_div_to_range(
                    channel.volts_per_div,
                    channel.attenuation,
                ));
                for (index, &adc_value) in buffer.iter().enumerate() {
                    let voltage = Self::raw_adc_counts_to_voltage(adc_value, voltage_range);
                    samples.push(StreamingSample {
                        channel: channel.channel_id,
                        voltage,
                        sample_index: index as u32,
                    });
                }
            }
        }
        let sample_interval_ns = self.current_time_interval_ns;

        Ok(TriggeredCapture {
            samples,
            trigger_position: self.pre_trigger_samples,
            pre_trigger_samples: self.pre_trigger_samples,
            post_trigger_samples: self.post_trigger_samples,
            sample_interval_ns,
            overflow,
        })
    }

    fn set_scope_attenuation(
        &mut self,
        channel: ChannelId,
        attenuation: f64,
    ) -> anyhow::Result<()> {
        self.settings
            .channels
            .iter_mut()
            .find(|c| c.channel_id == channel)
            .ok_or(anyhow::anyhow!("Channel not found"))?
            .attenuation = attenuation;
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
        Ok(self.settings.memory_depth.unwrap_or(MIN_MEMORY_DEPTH) as usize)
    }

    fn set_scope_time_per_div(&mut self, time_per_div: f64, memory_depth: u32) -> anyhow::Result<()> {
        self.settings.time_per_div = time_per_div;
        let memory_depth = memory_depth as f64;
        println!("Memory depth: {}", memory_depth);
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
        println!("Current timebase: {}", self.current_timebase);
        println!("Current time interval: {}", self.current_time_interval_ns);
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

#[async_trait]
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

    fn set_volts_offset(&mut self, channel: ChannelId, volts_offset: f64) -> anyhow::Result<()> {
        self.settings.channels.iter_mut().find(|c| c.channel_id == channel).ok_or(anyhow::anyhow!("Channel not found"))?.volts_offset = volts_offset;
        Ok(())
    }

    fn get_volts_offset(&self, channel: ChannelId) -> anyhow::Result<f64> {
        Ok(self.settings.channels.iter().find(|c| c.channel_id == channel).ok_or(anyhow::anyhow!("Channel not found"))?.volts_offset)
    }

    fn set_coupling(&mut self, channel: ChannelId, coupling: Coupling) -> anyhow::Result<()> {
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

    fn set_time_per_div(&mut self, time_per_div: f64) -> anyhow::Result<()> {
        self.set_scope_time_per_div(time_per_div, self.memory_depth)
    }

    fn get_time_per_div(&self) -> anyhow::Result<f64> {
        Ok(self.settings.time_per_div)
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

    fn measure_duty_cycle(&self, _channel: ChannelId) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_frequency(&self, _channel: ChannelId) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_period(&self, _channel: ChannelId) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_rms(&self, _channel: ChannelId) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_peak_to_peak(&self, _channel: ChannelId) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_average(&self, _channel: ChannelId) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn measure_min(&self, _channel: ChannelId) -> anyhow::Result<f64> {
        Ok(0.0)
    }

    fn get_data(&self, _channel: ChannelId) -> anyhow::Result<Vec<f64>> {
        Ok(vec![])
    }

    fn get_sample_rate(&self) -> anyhow::Result<f64> {
        self.get_sample_rate_from_device()
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

    fn get_triggered_data(&self) -> anyhow::Result<TriggeredCapture> {
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
}

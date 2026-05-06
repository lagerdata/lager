// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use crate::oscilloscope::Oscilloscope;
use anyhow::Result;
use protocol::{Command, Response};
use serde_json;
use std::sync::{Arc, Mutex};
use tungstenite::protocol::Message;

pub fn handle_command_internal(
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
    command: Command,
) -> Result<Message> {
    let response = match command {
        Command::EnableChannel { channel } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.enable_channel(channel) {
                Ok(()) => Response::ConfigureChannelEnabled,
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::DisableChannel { channel } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.disable_channel(channel) {
                Ok(()) => Response::ConfigureChannelDisabled,
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::IsChannelEnabled { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.is_channel_enabled(channel) {
                Ok(is_enabled) => Response::IsChannelEnabled {
                    channel,
                    is_enabled,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetChannelCount => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_channel_count() {
                Ok(count) => Response::GetChannelCount {
                    channel_count: count,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetSampleRate => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_sample_rate() {
                Ok(rate) => Response::GetSampleRate { sample_rate: rate },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetVoltsPerDiv {
            channel,
            volts_per_div,
        } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_volts_per_div(channel, volts_per_div) {
                Ok(()) => Response::ConfigureChannelVoltsPerDiv {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetVoltsPerDiv { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_volts_per_div(channel) {
                Ok(volts_per_div) => Response::GetVoltsPerDiv {
                    channel,
                    volts_per_div,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTimePerDiv { time_per_div } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_time_per_div(time_per_div) {
                Ok(()) => Response::ConfigureTimePerDiv {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTimeOffset { time_offset } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_time_offset(time_offset) {
                Ok(()) => Response::ConfigureTimeOffset {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetVoltsOffset { channel, volts_offset } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_volts_offset(channel, volts_offset) {
                Ok(()) => Response::ConfigureChannelVoltsOffset {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTimePerDiv => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_time_per_div() {
                Ok(time_per_div) => Response::GetTimePerDiv { time_per_div },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTimeOffset => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_time_offset() {
                Ok(time_offset) => Response::GetTimeOffset { time_offset },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetVoltsOffset { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_volts_offset(channel) {
                Ok(volts_offset) => Response::GetVoltsOffset { channel, volts_offset },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetCoupling { channel, coupling } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_coupling(channel, coupling) {
                Ok(()) => Response::ConfigureChannelCoupling {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetCoupling { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_coupling(channel) {
                Ok(coupling) => Response::GetCoupling { channel, coupling },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTriggerLevel { trigger_level } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_trigger_level(trigger_level) {
                Ok(()) => Response::ConfigureTriggerLevel {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTriggerLevel => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_trigger_level() {
                Ok(trigger_level) => Response::GetTriggerLevel { trigger_level },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTriggerSource { trigger_source } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_trigger_source(trigger_source) {
                Ok(()) => Response::ConfigureTriggerSource {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTriggerSource => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_trigger_source() {
                Ok(trigger_source) => Response::GetTriggerSource { trigger_source },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTriggerSlope { trigger_slope } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_trigger_slope(trigger_slope) {
                Ok(()) => Response::ConfigureTriggerSlope {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTriggerSlope => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_trigger_slope() {
                Ok(trigger_slope) => Response::GetTriggerSlope { trigger_slope },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetCaptureMode { capture_mode } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_capture_mode(capture_mode) {
                Ok(()) => Response::ConfigureCaptureMode {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetCaptureMode => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_capture_mode() {
                Ok(capture_mode) => Response::GetCaptureMode { capture_mode },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }

        Command::StartAcquisition {
            trigger_position_percent,
        } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.start_triggered_capture(trigger_position_percent) {
                Ok(()) => Response::StartAcquisition,
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::StopAcquisition => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.stop_triggered_capture() {
                Ok(()) => {
                    println!("Stopped triggering");
                    Response::StopAcquisition
                }
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::IsReady => {
            let scope = oscilloscope.lock().unwrap();
            match scope.is_ready() {
                Ok(is_ready) => Response::IsReady { is_ready },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetAttenuation {
            channel,
            attenuation,
        } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_attenuation(channel, attenuation) {
                Ok(()) => Response::ConfigureChannelAttenuation {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetAttenuation { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_attenuation(channel) {
                Ok(attenuation) => Response::GetAttenuation {
                    channel,
                    attenuation,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }

        _ => Response::Error {
            message: "Unsupported command".to_string(),
        },
    };

    Ok(Message::Text(serde_json::to_string(&response)?))
}

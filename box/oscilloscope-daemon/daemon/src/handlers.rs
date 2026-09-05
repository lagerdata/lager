// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Command dispatch: one [`Command`] in, one [`Response`] out.
//!
//! Every branch returns a response, including failures. The handler this
//! replaces logged driver errors and returned nothing for several commands,
//! so a client waited on a reply that was never coming; and it round-tripped
//! each response through `to_string` then `from_str` then `to_string` again
//! before sending.

use std::sync::Arc;

use protocol::{CaptureFrame, Command, Response};

use crate::scope_thread::{ScopeHandle, ScopeReply, ScopeRequest};

/// A response, plus a capture to send after it when the command produced one.
pub struct Outcome {
    pub response: Response,
    pub frame: Option<Arc<CaptureFrame>>,
}

impl From<Response> for Outcome {
    fn from(response: Response) -> Self {
        Outcome {
            response,
            frame: None,
        }
    }
}

pub async fn handle(command: Command, scope: &ScopeHandle) -> Outcome {
    // The only command whose payload is binary. The acknowledgement names
    // the sequence number so the client can match it to the frame that
    // follows on the same socket.
    if matches!(command, Command::GetTriggeredData) {
        return match scope.request(ScopeRequest::GetTriggeredData).await {
            ScopeReply::Capture(frame) => Outcome {
                response: Response::TriggeredDataFollows { seq: frame.seq },
                frame: Some(frame),
            },
            other => other.into_error().into(),
        };
    }

    control(command, scope).await.into()
}

async fn control(command: Command, scope: &ScopeHandle) -> Response {
    match command {
        Command::EnableChannel { channel } => scope
            .request(ScopeRequest::EnableChannel(channel))
            .await
            .into_ack(Response::ConfigureChannelEnabled),

        Command::DisableChannel { channel } => scope
            .request(ScopeRequest::DisableChannel(channel))
            .await
            .into_ack(Response::ConfigureChannelDisabled),

        Command::IsChannelEnabled { channel } => {
            match scope.request(ScopeRequest::IsChannelEnabled(channel)).await {
                ScopeReply::Bool(is_enabled) => Response::IsChannelEnabled {
                    channel,
                    is_enabled,
                },
                other => other.into_error(),
            }
        }

        Command::SetVoltsPerDiv {
            channel,
            volts_per_div,
        } => scope
            .request(ScopeRequest::SetVoltsPerDiv(channel, volts_per_div))
            .await
            .into_ack(Response::ConfigureChannelVoltsPerDiv),

        Command::GetVoltsPerDiv { channel } => {
            match scope.request(ScopeRequest::GetVoltsPerDiv(channel)).await {
                ScopeReply::Float(volts_per_div) => Response::GetVoltsPerDiv {
                    channel,
                    volts_per_div,
                },
                other => other.into_error(),
            }
        }

        Command::SetVoltsOffset {
            channel,
            volts_offset,
        } => scope
            .request(ScopeRequest::SetVoltsOffset(channel, volts_offset))
            .await
            .into_ack(Response::ConfigureChannelVoltsOffset),

        Command::GetVoltsOffset { channel } => {
            match scope.request(ScopeRequest::GetVoltsOffset(channel)).await {
                ScopeReply::Float(volts_offset) => Response::GetVoltsOffset {
                    channel,
                    volts_offset,
                },
                other => other.into_error(),
            }
        }

        Command::SetCoupling { channel, coupling } => scope
            .request(ScopeRequest::SetCoupling(channel, coupling))
            .await
            .into_ack(Response::ConfigureChannelCoupling),

        Command::GetCoupling { channel } => {
            match scope.request(ScopeRequest::GetCoupling(channel)).await {
                ScopeReply::Coupling(coupling) => Response::GetCoupling { channel, coupling },
                other => other.into_error(),
            }
        }

        Command::SetAttenuation {
            channel,
            attenuation,
        } => scope
            .request(ScopeRequest::SetAttenuation(channel, attenuation))
            .await
            .into_ack(Response::ConfigureChannelAttenuation),

        Command::GetAttenuation { channel } => {
            match scope.request(ScopeRequest::GetAttenuation(channel)).await {
                ScopeReply::Float(attenuation) => Response::GetAttenuation {
                    channel,
                    attenuation,
                },
                other => other.into_error(),
            }
        }

        Command::SetTimePerDiv { time_per_div } => scope
            .request(ScopeRequest::SetTimePerDiv(time_per_div))
            .await
            .into_ack(Response::ConfigureTimePerDiv),

        Command::GetTimePerDiv => match scope.request(ScopeRequest::GetTimePerDiv).await {
            ScopeReply::Float(time_per_div) => Response::GetTimePerDiv { time_per_div },
            other => other.into_error(),
        },

        Command::SetTimeOffset { time_offset } => scope
            .request(ScopeRequest::SetTimeOffset(time_offset))
            .await
            .into_ack(Response::ConfigureTimeOffset),

        Command::GetTimeOffset => match scope.request(ScopeRequest::GetTimeOffset).await {
            ScopeReply::Float(time_offset) => Response::GetTimeOffset { time_offset },
            other => other.into_error(),
        },

        Command::SetTriggerLevel { trigger_level } => scope
            .request(ScopeRequest::SetTriggerLevel(trigger_level))
            .await
            .into_ack(Response::ConfigureTriggerLevel),

        Command::GetTriggerLevel => match scope.request(ScopeRequest::GetTriggerLevel).await {
            ScopeReply::Float(trigger_level) => Response::GetTriggerLevel { trigger_level },
            other => other.into_error(),
        },

        Command::SetTriggerSource { trigger_source } => scope
            .request(ScopeRequest::SetTriggerSource(trigger_source))
            .await
            .into_ack(Response::ConfigureTriggerSource),

        Command::GetTriggerSource => match scope.request(ScopeRequest::GetTriggerSource).await {
            ScopeReply::Channel(trigger_source) => Response::GetTriggerSource { trigger_source },
            other => other.into_error(),
        },

        Command::SetTriggerSlope { trigger_slope } => scope
            .request(ScopeRequest::SetTriggerSlope(trigger_slope))
            .await
            .into_ack(Response::ConfigureTriggerSlope),

        Command::GetTriggerSlope => match scope.request(ScopeRequest::GetTriggerSlope).await {
            ScopeReply::Slope(trigger_slope) => Response::GetTriggerSlope { trigger_slope },
            other => other.into_error(),
        },

        Command::SetCaptureMode { capture_mode } => scope
            .request(ScopeRequest::SetCaptureMode(capture_mode))
            .await
            .into_ack(Response::ConfigureCaptureMode),

        Command::GetCaptureMode => match scope.request(ScopeRequest::GetCaptureMode).await {
            ScopeReply::Mode(capture_mode) => Response::GetCaptureMode { capture_mode },
            other => other.into_error(),
        },

        Command::GetSampleRate => match scope.request(ScopeRequest::GetSampleRate).await {
            ScopeReply::Float(sample_rate) => Response::GetSampleRate { sample_rate },
            other => other.into_error(),
        },

        Command::GetMemoryDepth => match scope.request(ScopeRequest::GetMemoryDepth).await {
            ScopeReply::Usize(memory_depth) => Response::GetMemoryDepth { memory_depth },
            other => other.into_error(),
        },

        Command::GetBandwidth => match scope.request(ScopeRequest::GetBandwidth).await {
            ScopeReply::Float(bandwidth) => Response::GetBandwidth { bandwidth },
            other => other.into_error(),
        },

        Command::GetChannelCount => match scope.request(ScopeRequest::GetChannelCount).await {
            ScopeReply::Usize(channel_count) => Response::GetChannelCount { channel_count },
            other => other.into_error(),
        },

        Command::GetCapabilities => match scope.request(ScopeRequest::GetCapabilities).await {
            ScopeReply::Capabilities(capabilities) => Response::Capabilities { capabilities },
            other => other.into_error(),
        },

        Command::StartAcquisition {
            trigger_position_percent,
        } => scope
            .request(ScopeRequest::StartAcquisition(trigger_position_percent))
            .await
            .into_ack(Response::StartAcquisition),

        Command::StopAcquisition => scope
            .request(ScopeRequest::StopAcquisition)
            .await
            .into_ack(Response::StopAcquisition),

        // Previously accepted and then ignored, leaving the caller waiting.
        Command::ForceTrigger => scope
            .request(ScopeRequest::ForceTrigger)
            .await
            .into_ack(Response::ForceTrigger),

        Command::IsReady => match scope.request(ScopeRequest::IsReady).await {
            ScopeReply::Bool(is_ready) => Response::IsReady { is_ready },
            other => other.into_error(),
        },

        Command::Measure {
            channel,
            measurement,
        } => measure(channel, measurement, scope).await,

        // Intercepted by `handle`, which needs to send a binary frame too.
        Command::GetTriggeredData => unreachable!("handled before control dispatch"),

        // Connection-scoped, so the server answers these itself; the hardware
        // thread has no notion of who is listening.
        Command::Subscribe | Command::Unsubscribe => {
            unreachable!("handled by the connection loop")
        }
    }
}

/// Capture a block and measure one channel of it.
///
/// Always computes the whole set, even when one quantity was named: it all
/// comes from the same capture, so returning only the named value would cost
/// the caller another acquisition to learn anything else.
async fn measure(
    channel: protocol::ChannelId,
    measurement: Option<String>,
    scope: &ScopeHandle,
) -> Response {
    // Resolve the name before touching hardware: a typo should not cost a
    // capture, and the error should say what would have been accepted.
    let requested = match measurement.as_deref() {
        None => None,
        Some(name) => match protocol::Measurement::parse(name) {
            Some(parsed) => Some(parsed),
            None => {
                return Response::Error {
                    message: format!(
                        "unknown measurement '{name}'; expected one of vpp, vmax, vmin, vrms, \
                         vavg, period, frequency, duty_cycle_pos, duty_cycle_neg, \
                         pulse_width_pos, pulse_width_neg, rise_time, fall_time, overshoot"
                    ),
                }
            }
        },
    };

    let measurements = match scope.request(ScopeRequest::MeasureAll { channel }).await {
        ScopeReply::Measurements(set) => set,
        other => return other.into_error(),
    };

    let (value, unit) = match requested {
        Some(which) => (measurements.get(which), Some(which.unit().to_string())),
        None => (None, None),
    };

    Response::Measurement {
        channel,
        measurements,
        value,
        unit,
    }
}

impl ScopeReply {
    /// Turn a bare acknowledgement into its response, preserving errors.
    pub(crate) fn into_ack(self, ok: Response) -> Response {
        match self {
            ScopeReply::Ok => ok,
            other => other.into_error(),
        }
    }

    /// Any reply that was not the expected shape becomes an error the client
    /// can read, rather than a dropped message.
    pub(crate) fn into_error(self) -> Response {
        match self {
            ScopeReply::Error(message) => Response::Error { message },
            unexpected => Response::Error {
                message: format!("oscilloscope returned an unexpected reply: {unexpected:?}"),
            },
        }
    }
}

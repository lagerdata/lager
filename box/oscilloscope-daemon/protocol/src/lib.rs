// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use clap::Subcommand;
use serde::{Deserialize, Serialize};
use std::fmt;

pub mod capabilities;
pub mod lscp;
pub mod measure;

pub use capabilities::{DriverFamily, ScopeCapabilities};
pub use lscp::{CaptureFrame, ChannelFrame};
pub use measure::{measure_channel, Measurement, MeasurementSet};

#[derive(Debug, Serialize, Deserialize, Subcommand)]
#[serde(tag = "command")]
pub enum Command {
    //     // Channel configuration
    EnableChannel {
        channel: ChannelId,
    },
    DisableChannel {
        channel: ChannelId,
    },
    IsChannelEnabled {
        channel: ChannelId,
    },

    SetVoltsOffset {
        channel: ChannelId,
        volts_offset: f64,
    },

    SetTimeOffset {
        time_offset: f64,
    },

    SetTriggerLevel {
        trigger_level: f64,
    },

    SetTriggerSource {
        trigger_source: ChannelId,
    },

    SetTriggerSlope {
        trigger_slope: TriggerSlope,
    },

    SetCaptureMode {
        capture_mode: CaptureMode,
    },

    SetCoupling {
        channel: ChannelId,
        coupling: Coupling,
    },

    SetTimePerDiv {
        time_per_div: f64,
    },

    SetVoltsPerDiv {
        channel: ChannelId,
        volts_per_div: f64,
    },
    SetAttenuation {
        channel: ChannelId,
        attenuation: f64,
    },

    StartAcquisition {
        trigger_position_percent: f64,
    },
    StopAcquisition,
    IsReady,
    GetTriggeredData,
    ForceTrigger,
    GetCapabilities,

    GetVoltsPerDiv {
        channel: ChannelId,
    },

    GetVoltsOffset {
        channel: ChannelId,
    },

    GetAttenuation {
        channel: ChannelId,
    },

    GetTriggerLevel,

    GetTriggerSource,

    GetTriggerSlope,

    GetCaptureMode,

    GetCoupling {
        channel: ChannelId,
    },

    GetTimePerDiv,

    GetTimeOffset,

    GetSampleRate,

    GetMemoryDepth,

    GetBandwidth,

    GetChannelCount,

    /// Start receiving captures on this connection as LSCP binary frames.
    ///
    /// Off by default, and deliberately per-connection rather than global.
    /// The daemon used to push captures to every client the moment
    /// acquisition started, which meant a control-only client -- the CLI, the
    /// Python driver -- received megabytes per second it never asked for, and
    /// worse, read a capture frame where it expected its command's reply.
    Subscribe,

    /// Stop receiving captures on this connection.
    Unsubscribe,

    /// Capture a block and report waveform measurements for one channel.
    ///
    /// Answers the Rigol's `MEAS:VPP?` family, which no PicoTech API
    /// provides. `measurement` names a single quantity (see
    /// [`Measurement::parse`] for accepted spellings); omitting it returns the
    /// whole set, which costs no more since all of it comes from one capture.
    Measure {
        channel: ChannelId,
        #[serde(default)]
        measurement: Option<String>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "response")]
pub enum Response {
    ConfigureTimePerDiv,
    ConfigureTimeOffset,
    ConfigureChannelVoltsPerDiv,
    ConfigureChannelVoltsOffset,
    ConfigureChannelCoupling,
    ConfigureTriggerLevel,
    ConfigureTriggerSource,
    ConfigureTriggerSlope,
    ConfigureCaptureMode,
    ConfigureTrigger,
    ConfigureChannelEnabled,
    ConfigureChannelDisabled,
    ConfigureChannelAttenuation,
    StartAcquisition,
    StopAcquisition,
    IsReady {
        is_ready: bool,
    },
    IsChannelEnabled {
        channel: ChannelId,
        is_enabled: bool,
    },
    /// Acknowledges `GetTriggeredData`. The capture itself follows as an
    /// LSCP binary frame carrying this sequence number, so the sample data
    /// never passes through JSON.
    TriggeredDataFollows {
        seq: u64,
    },
    ForceTrigger,
    GetVoltsPerDiv {
        channel: ChannelId,
        volts_per_div: f64,
    },
    GetVoltsOffset {
        channel: ChannelId,
        volts_offset: f64,
    },
    GetTimeConfig {
        time_per_div: f64,
    },
    GetCoupling {
        channel: ChannelId,
        coupling: Coupling,
    },
    GetChannelCount {
        channel_count: usize,
    },
    GetSampleRate {
        sample_rate: f64,
    },
    GetBandwidth {
        bandwidth: f64,
    },
    GetMemoryDepth {
        memory_depth: usize,
    },
    GetTriggerLevel {
        trigger_level: f64,
    },
    GetTriggerSource {
        trigger_source: ChannelId,
    },
    GetTriggerSlope {
        trigger_slope: TriggerSlope,
    },
    GetCaptureMode {
        capture_mode: CaptureMode,
    },
    GetTimePerDiv {
        time_per_div: f64,
    },
    GetTimeOffset {
        time_offset: f64,
    },
    GetAttenuation {
        channel: ChannelId,
        attenuation: f64,
    },
    Capabilities {
        capabilities: Box<ScopeCapabilities>,
    },
    Subscribed,
    Unsubscribed,
    /// Result of `Measure`. `measurements` always carries the full set;
    /// `value`/`unit` are populated only when a single measurement was named,
    /// so a CLI can print one number without knowing which field to read.
    ///
    /// A named measurement that the capture cannot support -- a period on a
    /// DC level, say -- comes back with `value: null` rather than a zero or
    /// an error, because "not present in this capture" is a real answer and
    /// zero would be a wrong one.
    Measurement {
        channel: ChannelId,
        measurements: Box<MeasurementSet>,
        #[serde(skip_serializing_if = "Option::is_none")]
        value: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        unit: Option<String>,
    },
    Error {
        message: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ChannelId {
    Alphabetic(char),
    Numeric(u8),
}

impl Default for ChannelId {
    fn default() -> Self {
        ChannelId::Alphabetic('A')
    }
}

impl clap::ValueEnum for ChannelId {
    fn value_variants<'a>() -> &'a [Self] {
        &[
            ChannelId::Alphabetic('A'),
            ChannelId::Alphabetic('B'),
            ChannelId::Alphabetic('C'),
            ChannelId::Alphabetic('D'),
            ChannelId::Numeric(1),
            ChannelId::Numeric(2),
            ChannelId::Numeric(3),
            ChannelId::Numeric(4),
        ]
    }

    fn to_possible_value(&self) -> Option<clap::builder::PossibleValue> {
        match self {
            ChannelId::Alphabetic(c) => Some(clap::builder::PossibleValue::new(match c {
                'A' => "A",
                'B' => "B",
                'C' => "C",
                'D' => "D",
                _ => return None,
            })),
            ChannelId::Numeric(n) => Some(clap::builder::PossibleValue::new(match n {
                1 => "1",
                2 => "2",
                3 => "3",
                4 => "4",
                _ => return None,
            })),
        }
    }
}

impl ChannelId {
    pub fn converted_numeric_to_alphabetic(n: u8) -> Self {
        match n {
            0 => ChannelId::Alphabetic('A'),
            1 => ChannelId::Alphabetic('B'),
            2 => ChannelId::Alphabetic('C'),
            3 => ChannelId::Alphabetic('D'),
            _ => ChannelId::Alphabetic('A'),
        }
    }

    pub fn converted_alphabetic_to_numeric(c: char) -> Self {
        match c {
            'A' => ChannelId::Numeric(0),
            'B' => ChannelId::Numeric(1),
            'C' => ChannelId::Numeric(2),
            'D' => ChannelId::Numeric(3),
            _ => ChannelId::Numeric(0),
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            ChannelId::Alphabetic('A') => "A",
            ChannelId::Alphabetic('B') => "B",
            ChannelId::Alphabetic('C') => "C",
            ChannelId::Alphabetic('D') => "D",
            ChannelId::Numeric(0) => "0",
            ChannelId::Numeric(1) => "1",
            ChannelId::Numeric(2) => "2",
            ChannelId::Numeric(3) => "3",
            _ => "Unknown",
        }
    }
}

impl From<char> for ChannelId {
    fn from(c: char) -> Self {
        ChannelId::Alphabetic(c)
    }
}

impl From<u8> for ChannelId {
    fn from(n: u8) -> Self {
        ChannelId::Numeric(n)
    }
}

impl fmt::Display for ChannelId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.as_str())
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TriggerSlope {
    #[default]
    Rising,
    Falling,
    Either,
    Neither,
}

impl fmt::Display for TriggerSlope {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TriggerSlope::Rising => write!(f, "Rising"),
            TriggerSlope::Falling => write!(f, "Falling"),
            TriggerSlope::Either => write!(f, "Either"),    
            TriggerSlope::Neither => write!(f, "Neither"),
        }
    }
}

impl clap::ValueEnum for TriggerSlope {
    fn value_variants<'a>() -> &'a [Self] {
        &[
            TriggerSlope::Rising,
            TriggerSlope::Falling,
            TriggerSlope::Either,
            TriggerSlope::Neither,
        ]
    }

    fn to_possible_value(&self) -> Option<clap::builder::PossibleValue> {
        Some(clap::builder::PossibleValue::new(match self {
            TriggerSlope::Rising => "rising",
            TriggerSlope::Falling => "falling",
            TriggerSlope::Either => "either",
            TriggerSlope::Neither => "neither",
        }))
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum CaptureMode {
    Normal,
    Single,
    #[default]
    Auto,
}

impl fmt::Display for CaptureMode {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            CaptureMode::Normal => write!(f, "Normal"),
            CaptureMode::Single => write!(f, "Single"),
            CaptureMode::Auto => write!(f, "Auto"),
        }
    }
}

impl clap::ValueEnum for CaptureMode {
    fn value_variants<'a>() -> &'a [Self] {
        &[CaptureMode::Normal, CaptureMode::Single, CaptureMode::Auto]
    }

    fn to_possible_value(&self) -> Option<clap::builder::PossibleValue> {
        Some(clap::builder::PossibleValue::new(match self {
            CaptureMode::Normal => "normal",
            CaptureMode::Single => "single",
            CaptureMode::Auto => "auto",
        }))
    }
}

#[derive(Debug, Default, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Coupling {
    #[default]
    #[serde(alias = "dc")]
    DC,
    #[serde(alias = "ac")]
    AC,
    #[serde(alias = "gnd")]
    GND,
}

impl fmt::Display for Coupling {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Coupling::DC => write!(f, "DC"),
            Coupling::AC => write!(f, "AC"),
            Coupling::GND => write!(f, "Ground"),
        }
    }
}

impl clap::ValueEnum for Coupling {
    fn value_variants<'a>() -> &'a [Self] {
        &[Coupling::AC, Coupling::DC, Coupling::GND]
    }

    fn to_possible_value(&self) -> Option<clap::builder::PossibleValue> {
        Some(clap::builder::PossibleValue::new(match self {
            Coupling::AC => "AC",
            Coupling::DC => "DC",
            Coupling::GND => "GND",
        }))
    }
}

/// Captures no longer travel as JSON. They are LSCP binary frames on the
/// same socket; see the [`lscp`] module. Commands and responses stay JSON
/// text frames, which keeps the control plane readable in `websocat`.
#[derive(Debug, Serialize, Deserialize)]
pub enum WebSocketMessage {
    Command(Command),
    Response(Response),
}

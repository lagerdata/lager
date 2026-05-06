// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use clap::Subcommand;
use serde::{Deserialize, Serialize};
use std::fmt;

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
}

#[derive(Debug, Serialize, Deserialize)]
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
    GetTriggeredData {
        triggered_data: TriggeredCapture,
    },
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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StreamingSample {
    pub channel: ChannelId,
    pub voltage: f64,
    pub sample_index: u32,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TriggeredCapture {
    pub samples: Vec<StreamingSample>,
    pub trigger_position: u32,
    pub pre_trigger_samples: u32,
    pub post_trigger_samples: u32,
    pub sample_interval_ns: f64,
    pub overflow: i16,
}

#[derive(Debug, Serialize, Deserialize)]
pub enum WebSocketMessage {
    Command(Command),
    Response(Response),
    TriggeredData(TriggeredCapture),
}

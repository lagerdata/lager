// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! What the attached scope can actually do.
//!
//! PicoScope models differ enough that a fixed feature set would either lie
//! about a 2204A or hide most of a 5444D. This struct is filled in at open
//! time from the driver family plus the variant string, then handed to every
//! surface -- web UI, terminal CLI, Python API, MCP -- so all four agree on
//! which controls exist.

use serde::{Deserialize, Serialize};

/// ADC resolutions a device supports, in bits. Single-entry for fixed-
/// resolution parts, multi-entry where `SetDeviceResolution` exists.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ResolutionSupport {
    pub available_bits: Vec<u8>,
    pub current_bits: u8,
    /// False when resolution is a property of the model rather than a setting.
    pub switchable: bool,
}

impl ResolutionSupport {
    pub fn fixed(bits: u8) -> Self {
        Self {
            available_bits: vec![bits],
            current_bits: bits,
            switchable: false,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct VoltageRange {
    /// Index the driver expects for this range.
    pub code: u8,
    /// Full-scale deflection in volts, e.g. 0.05 for the +/-50 mV range.
    pub full_scale_volts: f64,
    pub label: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SignalGeneratorSupport {
    pub built_in: bool,
    pub arbitrary: bool,
    pub min_frequency_hz: f64,
    pub max_frequency_hz: f64,
}

/// Which PicoTech driver family is behind this device. Determines the FFI
/// vtable, and by extension which of the flags below can ever be true.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum DriverFamily {
    Ps2000,
    Ps2000a,
    Ps3000a,
    Ps4000a,
    Ps5000a,
}

impl DriverFamily {
    /// Shared-object name to `dlopen`.
    pub fn library_name(&self) -> &'static str {
        match self {
            DriverFamily::Ps2000 => "libps2000.so",
            DriverFamily::Ps2000a => "libps2000a.so",
            DriverFamily::Ps3000a => "libps3000a.so",
            DriverFamily::Ps4000a => "libps4000a.so",
            DriverFamily::Ps5000a => "libps5000a.so",
        }
    }

    /// Symbol prefix for this family's functions.
    pub fn symbol_prefix(&self) -> &'static str {
        match self {
            DriverFamily::Ps2000 => "ps2000_",
            DriverFamily::Ps2000a => "ps2000a",
            DriverFamily::Ps3000a => "ps3000a",
            DriverFamily::Ps4000a => "ps4000a",
            DriverFamily::Ps5000a => "ps5000a",
        }
    }

    /// The pre-2010 snake_case APIs behave differently enough to need their
    /// own adapter: no serial on open, a separate time-units enum, and four
    /// fixed buffer pointers instead of `SetDataBuffer`.
    pub fn is_legacy(&self) -> bool {
        matches!(self, DriverFamily::Ps2000)
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            DriverFamily::Ps2000 => "ps2000",
            DriverFamily::Ps2000a => "ps2000a",
            DriverFamily::Ps3000a => "ps3000a",
            DriverFamily::Ps4000a => "ps4000a",
            DriverFamily::Ps5000a => "ps5000a",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScopeCapabilities {
    pub family: DriverFamily,
    /// Variant string from `PICO_VARIANT_INFO`, e.g. "2205A" or "5444D".
    pub model: String,
    pub serial: String,
    pub analog_channels: u8,
    pub channel_labels: Vec<String>,
    pub resolution: ResolutionSupport,
    pub voltage_ranges: Vec<VoltageRange>,
    pub max_sample_rate_hz: f64,
    pub max_memory_samples: u64,
    pub bandwidth_hz: Option<f64>,

    /// `SetChannel` accepts an analogue offset. Absent on legacy ps2000.
    pub analog_offset: bool,
    /// `SetBandwidthFilter` exists (ps3000a, ps4000a, ps5000a).
    pub bandwidth_limiter: bool,
    /// Digital port count for MSO parts; zero on analog-only models.
    pub digital_ports: u8,
    /// `SetNoOfCaptures` / `GetValuesBulk`.
    pub rapid_block: bool,
    /// Gap-free `RunStreaming`, as opposed to retriggered block mode.
    pub streaming_mode: bool,
    /// PicoConnect smart probes (ps4000a, ps5000a).
    pub smart_probes: bool,
    pub signal_generator: Option<SignalGeneratorSupport>,
    /// Hardware trigger types this model accepts beyond a simple edge.
    pub advanced_triggers: Vec<String>,
}

impl ScopeCapabilities {
    /// Channel labels A..=n for a given analog channel count.
    pub fn default_labels(count: u8) -> Vec<String> {
        (0..count)
            .map(|i| ((b'A' + i) as char).to_string())
            .collect()
    }

    pub fn has_channel(&self, label: &str) -> bool {
        self.channel_labels.iter().any(|c| c == label)
    }

    pub fn is_mso(&self) -> bool {
        self.digital_ports > 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_family_is_flagged() {
        assert!(DriverFamily::Ps2000.is_legacy());
        assert!(!DriverFamily::Ps2000a.is_legacy());
        assert!(!DriverFamily::Ps5000a.is_legacy());
    }

    #[test]
    fn library_names_match_the_packages_installed_on_a_box() {
        // These are the sonames dpkg installs into /opt/picoscope/lib.
        assert_eq!(DriverFamily::Ps2000.library_name(), "libps2000.so");
        assert_eq!(DriverFamily::Ps5000a.library_name(), "libps5000a.so");
    }

    #[test]
    fn legacy_symbol_prefix_carries_its_underscore() {
        // ps2000_open_unit versus ps2000aOpenUnit.
        assert_eq!(DriverFamily::Ps2000.symbol_prefix(), "ps2000_");
        assert_eq!(DriverFamily::Ps2000a.symbol_prefix(), "ps2000a");
    }

    #[test]
    fn default_labels_count_up_from_a() {
        assert_eq!(ScopeCapabilities::default_labels(2), vec!["A", "B"]);
        assert_eq!(
            ScopeCapabilities::default_labels(4),
            vec!["A", "B", "C", "D"]
        );
    }

    #[test]
    fn fixed_resolution_is_not_switchable() {
        let r = ResolutionSupport::fixed(8);
        assert_eq!(r.current_bits, 8);
        assert!(!r.switchable);
        assert_eq!(r.available_bits, vec![8]);
    }
}

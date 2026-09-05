// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Normalized parameter types shared by the modern PicoScope drivers.
//!
//! Every "a" family declares its own C enum for the same concept --
//! `PS2000A_RANGE`, `PS5000A_RANGE`, `PICO_CONNECT_PROBE_RANGE` -- and
//! bindgen turns each into a distinct Rust type alias. Without a normalized
//! layer, every call site would need a family match, which is exactly the
//! per-family branching the vtable in `modern.rs` exists to remove.
//!
//! These types are the single spelling the driver code uses; `code()` lowers
//! one to the integer the FFI wants. The tests at the bottom pin the
//! ordinals against the vendored headers, because the whole design rests on
//! the four families agreeing on them, and a silent divergence in a future
//! SDK would otherwise mean a scope quietly using the wrong voltage range.

use protocol::{Coupling as WireCoupling, TriggerSlope};

/// An input voltage range, as a full-scale deflection.
///
/// Ordinals match `enPS2000ARange` / `enPS5000ARange`, and the first twelve
/// `PICO_X1_PROBE_*` values ps4000a uses. 100 V and 200 V exist only in the
/// PicoConnect enum, so they are deliberately absent: offering a range the
/// 2000a cannot select would turn a UI choice into a driver error.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Range {
    R10mV,
    R20mV,
    R50mV,
    R100mV,
    R200mV,
    R500mV,
    R1V,
    R2V,
    R5V,
    R10V,
    R20V,
    R50V,
}

impl Range {
    pub const ALL: [Range; 12] = [
        Range::R10mV,
        Range::R20mV,
        Range::R50mV,
        Range::R100mV,
        Range::R200mV,
        Range::R500mV,
        Range::R1V,
        Range::R2V,
        Range::R5V,
        Range::R10V,
        Range::R20V,
        Range::R50V,
    ];

    /// The integer the driver expects.
    pub fn code(self) -> i32 {
        self as i32
    }

    pub fn from_code(code: i32) -> Option<Range> {
        Range::ALL.get(usize::try_from(code).ok()?).copied()
    }

    /// Full-scale deflection in volts. A capture at this range spans
    /// +/- this value.
    pub fn full_scale_volts(self) -> f64 {
        match self {
            Range::R10mV => 0.01,
            Range::R20mV => 0.02,
            Range::R50mV => 0.05,
            Range::R100mV => 0.1,
            Range::R200mV => 0.2,
            Range::R500mV => 0.5,
            Range::R1V => 1.0,
            Range::R2V => 2.0,
            Range::R5V => 5.0,
            Range::R10V => 10.0,
            Range::R20V => 20.0,
            Range::R50V => 50.0,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Range::R10mV => "10 mV",
            Range::R20mV => "20 mV",
            Range::R50mV => "50 mV",
            Range::R100mV => "100 mV",
            Range::R200mV => "200 mV",
            Range::R500mV => "500 mV",
            Range::R1V => "1 V",
            Range::R2V => "2 V",
            Range::R5V => "5 V",
            Range::R10V => "10 V",
            Range::R20V => "20 V",
            Range::R50V => "50 V",
        }
    }

    /// The tightest range that still fits a signal of `volts` amplitude.
    ///
    /// Picking too tight a range clips the waveform, so this rounds up, and
    /// saturates at the coarsest range rather than failing: a signal larger
    /// than 50 V is out of spec for these scopes either way, and the capture
    /// will flag the overflow.
    pub fn smallest_containing(volts: f64) -> Range {
        let magnitude = volts.abs();
        Range::ALL
            .into_iter()
            .find(|r| r.full_scale_volts() >= magnitude)
            .unwrap_or(Range::R50V)
    }

    /// The range a `volts_per_div` setting implies, for the eight vertical
    /// divisions these scopes draw.
    pub fn for_volts_per_div(volts_per_div: f64) -> Range {
        Range::smallest_containing(volts_per_div * 4.0)
    }
}

/// Input coupling. `enPS2000ACoupling` and friends are all AC = 0, DC = 1.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Coupling {
    Ac,
    #[default]
    Dc,
}

impl Coupling {
    pub fn code(self) -> i32 {
        match self {
            Coupling::Ac => 0,
            Coupling::Dc => 1,
        }
    }
}

impl From<WireCoupling> for Coupling {
    /// PicoScopes have no ground-coupling relay, unlike a Rigol. Ground is
    /// mapped to DC rather than rejected so a shared net configuration still
    /// applies; a caller that needs a true ground reference has to disable
    /// the channel.
    fn from(wire: WireCoupling) -> Self {
        match wire {
            WireCoupling::AC => Coupling::Ac,
            WireCoupling::DC | WireCoupling::GND => Coupling::Dc,
        }
    }
}

/// Edge/threshold condition for the trigger.
///
/// Ordinals match `enPS2000AThresholdDirection` and its siblings. Only the
/// five single-threshold conditions are modelled; the window and
/// `*_LOWER` variants need the full `SetTriggerChannelConditions` path
/// rather than `SetSimpleTrigger`, so exposing them here would suggest a
/// capability the simple path does not have.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ThresholdDirection {
    Above,
    Below,
    #[default]
    Rising,
    Falling,
    RisingOrFalling,
}

impl ThresholdDirection {
    pub fn code(self) -> i32 {
        self as i32
    }
}

impl From<TriggerSlope> for ThresholdDirection {
    /// `Neither` has no direction to express: the driver models "do not
    /// trigger on an edge" through `SetSimpleTrigger`'s enable flag, not
    /// through this enum. It maps to `Rising` so the direction argument is
    /// always well-formed; callers pass `enabled: false` alongside it.
    fn from(slope: TriggerSlope) -> Self {
        match slope {
            TriggerSlope::Rising | TriggerSlope::Neither => ThresholdDirection::Rising,
            TriggerSlope::Falling => ThresholdDirection::Falling,
            TriggerSlope::Either => ThresholdDirection::RisingOrFalling,
        }
    }
}

/// ADC resolution, for the parts that can switch it.
///
/// Only ps5000a has `SetDeviceResolution`; the other families are fixed at
/// 8 bits (2000a, 3000a) or 12/14 bits by model (4000a). Ordinals match
/// `enPS5000ADeviceResolution`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, PartialOrd, Ord)]
pub enum DeviceResolution {
    #[default]
    Bits8,
    Bits12,
    Bits14,
    Bits15,
    Bits16,
}

impl DeviceResolution {
    pub fn code(self) -> i32 {
        self as i32
    }

    pub fn bits(self) -> u8 {
        match self {
            DeviceResolution::Bits8 => 8,
            DeviceResolution::Bits12 => 12,
            DeviceResolution::Bits14 => 14,
            DeviceResolution::Bits15 => 15,
            DeviceResolution::Bits16 => 16,
        }
    }

    pub fn from_bits(bits: u8) -> Option<DeviceResolution> {
        match bits {
            8 => Some(DeviceResolution::Bits8),
            12 => Some(DeviceResolution::Bits12),
            14 => Some(DeviceResolution::Bits14),
            15 => Some(DeviceResolution::Bits15),
            16 => Some(DeviceResolution::Bits16),
            _ => None,
        }
    }
}

/// Downsampling mode for `GetValues`. `None` returns every sample.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum RatioMode {
    #[default]
    None,
    Aggregate,
    Decimate,
    Average,
}

impl RatioMode {
    pub fn code(self) -> i32 {
        match self {
            RatioMode::None => 0,
            RatioMode::Aggregate => 1,
            RatioMode::Decimate => 2,
            RatioMode::Average => 4,
        }
    }
}

/// `PICO_INFO` selectors used to identify a unit after it opens.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnitInfo {
    DriverVersion,
    HardwareVersion,
    VariantInfo,
    BatchAndSerial,
}

impl UnitInfo {
    pub fn code(self) -> u32 {
        match self {
            UnitInfo::DriverVersion => 0,
            UnitInfo::HardwareVersion => 1,
            UnitInfo::VariantInfo => 3,
            UnitInfo::BatchAndSerial => 4,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // These assertions are the contract with the vendored headers. If a
    // future SDK renumbers one of these enums, the driver would otherwise
    // keep compiling and silently select the wrong setting.

    #[test]
    fn range_ordinals_match_the_vendored_headers() {
        // enPS2000ARange / enPS5000ARange, and PICO_X1_PROBE_* for ps4000a.
        assert_eq!(Range::R10mV.code(), 0);
        assert_eq!(Range::R20mV.code(), 1);
        assert_eq!(Range::R50mV.code(), 2);
        assert_eq!(Range::R100mV.code(), 3);
        assert_eq!(Range::R200mV.code(), 4);
        assert_eq!(Range::R500mV.code(), 5);
        assert_eq!(Range::R1V.code(), 6);
        assert_eq!(Range::R2V.code(), 7);
        assert_eq!(Range::R5V.code(), 8);
        assert_eq!(Range::R10V.code(), 9);
        assert_eq!(Range::R20V.code(), 10);
        assert_eq!(Range::R50V.code(), 11);
    }

    #[test]
    fn coupling_ordinals_match_the_vendored_headers() {
        // enPS5000ACoupling: PS5000A_AC = 0, PS5000A_DC = 1.
        assert_eq!(Coupling::Ac.code(), 0);
        assert_eq!(Coupling::Dc.code(), 1);
    }

    #[test]
    fn threshold_direction_ordinals_match_the_vendored_headers() {
        // enPS5000AThresholdDirection.
        assert_eq!(ThresholdDirection::Above.code(), 0);
        assert_eq!(ThresholdDirection::Below.code(), 1);
        assert_eq!(ThresholdDirection::Rising.code(), 2);
        assert_eq!(ThresholdDirection::Falling.code(), 3);
        assert_eq!(ThresholdDirection::RisingOrFalling.code(), 4);
    }

    #[test]
    fn resolution_ordinals_match_the_vendored_headers() {
        // enPS5000ADeviceResolution.
        assert_eq!(DeviceResolution::Bits8.code(), 0);
        assert_eq!(DeviceResolution::Bits12.code(), 1);
        assert_eq!(DeviceResolution::Bits14.code(), 2);
        assert_eq!(DeviceResolution::Bits15.code(), 3);
        assert_eq!(DeviceResolution::Bits16.code(), 4);
    }

    #[test]
    fn variant_info_is_the_selector_capability_detection_uses() {
        // PICO_VARIANT_INFO = 3 in PicoStatus.h. Getting this wrong would
        // make every model detect as something else.
        assert_eq!(UnitInfo::VariantInfo.code(), 3);
        assert_eq!(UnitInfo::BatchAndSerial.code(), 4);
    }

    #[test]
    fn range_round_trips_through_its_code() {
        for range in Range::ALL {
            assert_eq!(Range::from_code(range.code()), Some(range));
        }
    }

    #[test]
    fn out_of_bounds_range_codes_are_rejected() {
        assert_eq!(Range::from_code(12), None);
        assert_eq!(Range::from_code(-1), None);
    }

    #[test]
    fn range_selection_rounds_up_so_the_signal_is_not_clipped() {
        // A 3 V signal does not fit the 2 V range.
        assert_eq!(Range::smallest_containing(3.0), Range::R5V);
        // An exact match uses that range rather than the next one up.
        assert_eq!(Range::smallest_containing(1.0), Range::R1V);
        assert_eq!(Range::smallest_containing(0.0), Range::R10mV);
        // Negative amplitudes are the same range as positive.
        assert_eq!(Range::smallest_containing(-3.0), Range::R5V);
    }

    #[test]
    fn oversized_signals_saturate_rather_than_failing() {
        // Out of spec for the hardware, but the capture flags the overflow;
        // refusing to set a range would just fail earlier and less clearly.
        assert_eq!(Range::smallest_containing(1000.0), Range::R50V);
    }

    #[test]
    fn volts_per_div_covers_all_eight_divisions() {
        // 1 V/div over 8 divisions is +/- 4 V, so the 5 V range.
        assert_eq!(Range::for_volts_per_div(1.0), Range::R5V);
        // 0.5 V/div is +/- 2 V exactly.
        assert_eq!(Range::for_volts_per_div(0.5), Range::R2V);
    }

    #[test]
    fn ground_coupling_falls_back_to_dc() {
        // PicoScopes have no ground relay; see the From impl.
        assert_eq!(Coupling::from(WireCoupling::GND), Coupling::Dc);
        assert_eq!(Coupling::from(WireCoupling::AC), Coupling::Ac);
        assert_eq!(Coupling::from(WireCoupling::DC), Coupling::Dc);
    }

    #[test]
    fn trigger_slope_maps_onto_a_threshold_direction() {
        assert_eq!(
            ThresholdDirection::from(TriggerSlope::Rising),
            ThresholdDirection::Rising
        );
        assert_eq!(
            ThresholdDirection::from(TriggerSlope::Falling),
            ThresholdDirection::Falling
        );
        assert_eq!(
            ThresholdDirection::from(TriggerSlope::Either),
            ThresholdDirection::RisingOrFalling
        );
    }

    #[test]
    fn neither_slope_still_yields_a_valid_direction() {
        // Trigger-off is the enable flag's job, not this enum's, so the
        // direction must still be something the driver accepts.
        assert_eq!(
            ThresholdDirection::from(TriggerSlope::Neither),
            ThresholdDirection::Rising
        );
    }

    #[test]
    fn resolution_round_trips_through_its_bit_count() {
        for res in [
            DeviceResolution::Bits8,
            DeviceResolution::Bits12,
            DeviceResolution::Bits14,
            DeviceResolution::Bits15,
            DeviceResolution::Bits16,
        ] {
            assert_eq!(DeviceResolution::from_bits(res.bits()), Some(res));
        }
        // 10-bit is a resolution some 6000-series parts have, but not one
        // any family covered here accepts.
        assert_eq!(DeviceResolution::from_bits(10), None);
    }

    #[test]
    fn no_downsampling_is_mode_zero() {
        // PS2000A_RATIO_MODE_NONE. Aggregate is 1, and average is 4 rather
        // than 3 -- the enum is a bitfield, not a sequence.
        assert_eq!(RatioMode::None.code(), 0);
        assert_eq!(RatioMode::Average.code(), 4);
    }
}

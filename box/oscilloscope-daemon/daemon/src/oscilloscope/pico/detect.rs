// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Work out which PicoScope is attached, and what it can do.
//!
//! The daemon used to construct a `PicoScope2000` unconditionally, which
//! meant a box with any other PicoScope either failed to open or -- worse --
//! opened and reported a 2204A's channel count and ranges for a different
//! instrument. Nothing in the system could tell the difference.
//!
//! Detection runs in two steps, for a reason. Asking "which drivers are
//! installed" is cheap and needs no hardware, so it happens first and
//! narrows the families worth trying. Only then does it try to open a unit
//! per family, because opening is slow (hundreds of milliseconds while the
//! driver uploads firmware) and exclusive.
//!
//! The model then comes from `PICO_VARIANT_INFO`, which is the device's own
//! answer rather than anything inferred from a USB product id. Capabilities
//! are derived from that string plus the family; see
//! [`capabilities_from_variant`] for exactly which fields are read from the
//! device and which follow a documented series-level rule.

use anyhow::{bail, Context, Result};
use protocol::capabilities::{
    ResolutionSupport, ScopeCapabilities, SignalGeneratorSupport, VoltageRange,
};
use protocol::DriverFamily;

use super::modern::{api_for, PicoModernApi};
use super::status::{self, PicoStatusError};
use super::types::{DeviceResolution, Range, UnitInfo};

/// Detection found no unit at all, as opposed to one that would not open.
///
/// The difference decides which error the daemon reports: with no modern
/// unit attached the legacy ps2000 driver's answer is the useful one, but a
/// modern unit that was there and failed has to be reported as itself.
#[derive(Debug)]
pub struct NoUnitFound(pub String);

impl std::fmt::Display for NoUnitFound {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for NoUnitFound {}

/// A unit that was found, opened and identified.
pub struct DetectedScope {
    pub family: DriverFamily,
    /// Open device handle. The caller owns closing it.
    pub handle: i16,
    pub capabilities: ScopeCapabilities,
    pub api: Box<dyn PicoModernApi>,
}

/// Families to try, in the order they are tried.
///
/// Legacy `ps2000` is deliberately last. Its `open_unit` takes no serial and
/// grabs the first unit it can, so trying it first on a box with both a
/// 2204A and a 3000-series scope could claim the wrong one. Every modern
/// driver refuses units that are not its own, making them safe to probe.
const PROBE_ORDER: &[DriverFamily] = &[
    DriverFamily::Ps5000a,
    DriverFamily::Ps4000a,
    DriverFamily::Ps3000a,
    DriverFamily::Ps2000a,
];

/// Open whichever supported PicoScope is attached.
///
/// `serial` restricts the search to one unit, which is what a box with two
/// scopes needs. Without it, the first unit found wins.
///
/// Fails with [`NoUnitFound`] when no modern unit is attached, and with the
/// unit's own error when one was found but could not be opened.
pub fn detect(serial: Option<&str>) -> Result<DetectedScope> {
    detect_with(serial, &super::loader::installed_families(), api_for)
}

fn detect_with(
    serial: Option<&str>,
    installed: &[DriverFamily],
    api_for: impl Fn(DriverFamily) -> Result<Box<dyn PicoModernApi>>,
) -> Result<DetectedScope> {
    if installed.is_empty() {
        return Err(NoUnitFound(
            "no PicoScope driver libraries are installed. Run `lager install` \
             on the box, or set LD_LIBRARY_PATH if the SDK is somewhere \
             unusual."
                .to_string(),
        )
        .into());
    }

    let mut attempts: Vec<String> = Vec::new();
    // Whether any family had a unit there, listed or answering, that then
    // failed: that failure is the one to report.
    let mut found_a_unit = false;

    for &family in PROBE_ORDER {
        if !installed.contains(&family) {
            // No driver for this series on this box; not an error, just not
            // a candidate. Recorded so the failure message can say so.
            attempts.push(format!("{}: driver not installed", family.as_str()));
            continue;
        }

        let api = match api_for(family) {
            Ok(api) => api,
            Err(e) => {
                attempts.push(format!("{}: {e}", family.as_str()));
                continue;
            }
        };

        // Enumerate before opening. Opening is slow and exclusive, so on a
        // box with four drivers installed and one scope attached this turns
        // three firmware uploads into three descriptor reads. A driver that
        // cannot enumerate is still tried, since failing to list units is
        // not proof that none are there.
        let listed = match api.enumerate() {
            Ok(serials) if serials.is_empty() => {
                attempts.push(format!("{}: no units attached", family.as_str()));
                continue;
            }
            Ok(serials) => {
                if let Some(wanted) = serial {
                    if !serials.iter().any(|s| s == wanted) {
                        attempts.push(format!(
                            "{}: has units {:?}, none matching {wanted}",
                            family.as_str(),
                            serials
                        ));
                        continue;
                    }
                }
                tracing::debug!(
                    family = family.as_str(),
                    ?serials,
                    "family reports attached units"
                );
                true
            }
            Err(e) => {
                tracing::debug!(
                    family = family.as_str(),
                    error = %e,
                    "could not enumerate; trying to open anyway"
                );
                false
            }
        };

        // Open at 8 bits. It is the one resolution every flexible part
        // supports with all channels enabled, so opening cannot fail on a
        // channel-count constraint before we know what the unit even is.
        match api.open(serial, DeviceResolution::Bits8) {
            Ok(handle) => {
                let capabilities = match identify(api.as_ref(), handle) {
                    Ok(capabilities) => capabilities,
                    Err(e) => {
                        // Identified badly is worse than not found: close up
                        // rather than leave a half-known unit open.
                        let _ = api.close(handle);
                        found_a_unit = true;
                        attempts.push(format!("{}: opened but {e}", family.as_str()));
                        continue;
                    }
                };

                tracing::info!(
                    family = family.as_str(),
                    model = %capabilities.model,
                    serial = %capabilities.serial,
                    channels = capabilities.analog_channels,
                    "detected PicoScope"
                );

                return Ok(DetectedScope {
                    family,
                    handle,
                    capabilities,
                    api,
                });
            }
            Err(e) => {
                if listed || open_failure_means_a_unit(&e) {
                    found_a_unit = true;
                }
                attempts.push(format!("{}: {e:#}", family.as_str()));
            }
        }
    }

    // Legacy ps2000 has its own driver and does not go through the modern
    // vtable, so it is reported as a possibility rather than probed here.
    if installed.contains(&DriverFamily::Ps2000) {
        attempts.push(
            "ps2000: installed, and handled by the legacy driver rather than \
             this probe"
                .to_string(),
        );
    }

    let with_serial = serial
        .map(|s| format!(" with serial {s}"))
        .unwrap_or_default();
    if found_a_unit {
        bail!(
            "a PicoScope was found{with_serial} but could not be opened. Tried:\n  {}",
            attempts.join("\n  ")
        );
    }
    Err(NoUnitFound(format!(
        "no supported PicoScope was found{with_serial}. Tried:\n  {}",
        attempts.join("\n  ")
    ))
    .into())
}

/// Whether an `OpenUnit` failure came from a unit rather than from there
/// being none: anything the driver answered other than `PICO_NOT_FOUND`.
fn open_failure_means_a_unit(error: &anyhow::Error) -> bool {
    error
        .chain()
        .filter_map(|cause| cause.downcast_ref::<PicoStatusError>())
        .any(|failure| failure.status != status::NOT_FOUND)
}

/// Read a unit's identity and turn it into capabilities.
fn identify(api: &dyn PicoModernApi, handle: i16) -> Result<ScopeCapabilities> {
    let model = api
        .unit_info(handle, UnitInfo::VariantInfo)
        .context("could not read PICO_VARIANT_INFO")?;
    if model.is_empty() {
        bail!("the unit reported an empty model string");
    }

    let serial = api
        .unit_info(handle, UnitInfo::BatchAndSerial)
        .unwrap_or_default();

    let resolution = resolution_support(api.family(), &model, || api.resolution(handle));

    Ok(capabilities_from_variant(
        api.family(),
        &model,
        &serial,
        resolution,
    ))
}

/// A unit's ADC resolution, and whether it can be changed.
///
/// Read from the device where it is a setting, so a 5000-series part opened
/// at 8 bits reports 8 rather than its maximum. Of the 4000a parts only the
/// 4444 has a second resolution and `GetDeviceResolution` to ask; the rest
/// are 12-bit, which the 8-bit fallback used to report them as.
fn resolution_support(
    family: DriverFamily,
    model: &str,
    current: impl FnOnce() -> Result<DeviceResolution>,
) -> ResolutionSupport {
    let switchable = |fallback: DeviceResolution| ResolutionSupport {
        available_bits: available_resolutions(family)
            .iter()
            .map(|r| r.bits())
            .collect(),
        current_bits: current().unwrap_or(fallback).bits(),
        switchable: true,
    };
    match family {
        DriverFamily::Ps5000a => switchable(DeviceResolution::Bits8),
        DriverFamily::Ps4000a if model_number(model) == "4444" => {
            switchable(DeviceResolution::Bits12)
        }
        DriverFamily::Ps4000a => ResolutionSupport::fixed(12),
        _ => ResolutionSupport::fixed(8),
    }
}

/// Resolutions a family's `SetDeviceResolution` accepts.
fn available_resolutions(family: DriverFamily) -> &'static [DeviceResolution] {
    match family {
        // The flexible-resolution family: 8 through 16 bits, though the
        // higher settings restrict how many channels can be on at once.
        DriverFamily::Ps5000a => &[
            DeviceResolution::Bits8,
            DeviceResolution::Bits12,
            DeviceResolution::Bits14,
            DeviceResolution::Bits15,
            DeviceResolution::Bits16,
        ],
        // The 4444, the one 4000a part with a 14-bit mode.
        DriverFamily::Ps4000a => &[DeviceResolution::Bits12, DeviceResolution::Bits14],
        _ => &[DeviceResolution::Bits8],
    }
}

/// The model number in a variant string: "4444" from "PicoScope 4444".
fn model_number(model: &str) -> &str {
    let start = model.find(|c: char| c.is_ascii_digit()).unwrap_or(model.len());
    let digits = &model[start..];
    let end = digits.find(|c: char| !c.is_ascii_digit()).unwrap_or(digits.len());
    &digits[..end]
}

/// Analog channel count encoded in a PicoScope model number.
///
/// Pico puts the channel count in the second digit: 2204A and 3204D and
/// 5242D are 2-channel; 2405A and 3403D and 5442D and 4424 are 4-channel.
/// That holds across every series this daemon drives, which is why it is
/// read from the model rather than kept as a table of every model Pico has
/// ever sold. `box/lager/http_handlers/usb_scanner.py` does the same thing
/// for discovery, from the USB product string.
///
/// Returns `None` when the string does not look like a model number, so the
/// caller can decide rather than being handed a guess.
pub fn channels_from_variant(model: &str) -> Option<u8> {
    let digits: Vec<u8> = model
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(|c| c.is_ascii_digit())
        .map(|c| c as u8 - b'0')
        .collect();

    // Model numbers are four digits: series, channels, then two more.
    if digits.len() < 2 {
        return None;
    }
    match digits[1] {
        2 => Some(2),
        4 => Some(4),
        // 8 appears in the 4824, an 8-channel part.
        8 => Some(8),
        _ => None,
    }
}

/// Build capabilities from what the device reported.
///
/// Read from the device: model, serial, and the current resolution.
///
/// Derived from the model string: analog channel count (see
/// [`channels_from_variant`]) and whether it is an MSO, which Pico spells
/// out in the variant string itself ("2205AMSO").
///
/// The rest are series-level facts from the programmer's guides in
/// `picoscope/`: which ranges exist, whether there is a signal generator,
/// and which trigger types the hardware implements. They are properties of
/// the driver family rather than the individual model, which is why they do
/// not need a per-model table.
pub fn capabilities_from_variant(
    family: DriverFamily,
    model: &str,
    serial: &str,
    resolution: ResolutionSupport,
) -> ScopeCapabilities {
    // An unrecognised model number is treated as 2-channel: under-reporting
    // hides a channel, while over-reporting offers one that errors when
    // driven.
    let analog_channels = channels_from_variant(model).unwrap_or(2);

    // Pico marks mixed-signal parts in the variant string itself.
    let digital_ports = if model.to_uppercase().contains("MSO") { 1 } else { 0 };

    let voltage_ranges: Vec<VoltageRange> = Range::supported(family)
        .map(|r| VoltageRange {
            code: r.code() as u8,
            full_scale_volts: r.full_scale_volts(),
            label: r.label().to_string(),
        })
        .collect();

    ScopeCapabilities {
        family,
        model: model.to_string(),
        serial: serial.to_string(),
        analog_channels,
        channel_labels: ScopeCapabilities::default_labels(analog_channels),
        resolution,
        voltage_ranges,
        max_sample_rate_hz: max_sample_rate(family),
        max_memory_samples: max_memory(family),
        // Bandwidth is a per-model figure the driver does not report, and
        // guessing it would put a wrong number in front of the user. Left
        // unset rather than approximated.
        bandwidth_hz: None,
        // Every modern "a" API takes an analogue offset on SetChannel; only
        // the legacy ps2000 lacks it.
        analog_offset: true,
        // SetBandwidthFilter exists on 3000a, 4000a and 5000a.
        bandwidth_limiter: matches!(
            family,
            DriverFamily::Ps3000a | DriverFamily::Ps4000a | DriverFamily::Ps5000a
        ),
        digital_ports,
        // SetNoOfCaptures / GetValuesBulk are in all four modern APIs.
        rapid_block: true,
        // RunStreaming likewise.
        streaming_mode: true,
        // PicoConnect intelligent probes are a 4000a/5000a feature.
        smart_probes: matches!(family, DriverFamily::Ps4000a | DriverFamily::Ps5000a),
        signal_generator: signal_generator(family),
        advanced_triggers: advanced_triggers(family),
        // `RunStreaming` and the aggregating `GetValues` modes exist on these
        // families but are not wired up, so neither is advertised.
        roll_mode: false,
        peak_detect: false,
    }
}

/// Peak sample rate for a family, in samples/second.
///
/// These are the series maxima from the programmer's guides, reached with a
/// single channel enabled. The per-model figure is lower on the smaller
/// parts, so this is an upper bound rather than a promise; the authoritative
/// answer for a given configuration comes from `GetTimebase2`, which the
/// driver calls before every capture.
fn max_sample_rate(family: DriverFamily) -> f64 {
    match family {
        DriverFamily::Ps2000 => 200e6,
        DriverFamily::Ps2000a => 1e9,
        DriverFamily::Ps3000a => 1e9,
        DriverFamily::Ps4000a => 80e6,
        DriverFamily::Ps5000a => 1e9,
    }
}

/// Capture memory for a family, in samples.
fn max_memory(family: DriverFamily) -> u64 {
    match family {
        DriverFamily::Ps2000 => 32_000,
        DriverFamily::Ps2000a => 128_000_000,
        DriverFamily::Ps3000a => 512_000_000,
        DriverFamily::Ps4000a => 256_000_000,
        DriverFamily::Ps5000a => 512_000_000,
    }
}

fn signal_generator(family: DriverFamily) -> Option<SignalGeneratorSupport> {
    match family {
        // The 4000a parts (4224, 4424, 4444, 4824) ship no signal generator.
        DriverFamily::Ps4000a => None,
        // The others carry one across the series. Arbitrary-waveform output
        // is a per-model extra on top of the function generator, so it is
        // reported as present only where the whole series has it.
        DriverFamily::Ps2000a | DriverFamily::Ps3000a | DriverFamily::Ps5000a => {
            Some(SignalGeneratorSupport {
                built_in: true,
                arbitrary: true,
                min_frequency_hz: 0.03,
                max_frequency_hz: 20e6,
            })
        }
        DriverFamily::Ps2000 => Some(SignalGeneratorSupport {
            built_in: true,
            arbitrary: false,
            min_frequency_hz: 0.1,
            max_frequency_hz: 100e3,
        }),
    }
}

/// Hardware trigger types beyond a simple edge.
///
/// All four modern APIs implement these through
/// `SetTriggerChannelConditions` and `SetPulseWidthQualifier`. The legacy
/// ps2000 has edge triggers only, which is why its list is empty.
fn advanced_triggers(family: DriverFamily) -> Vec<String> {
    if family.is_legacy() {
        return Vec::new();
    }
    ["window", "pulse-width", "level-dropout", "runt", "interval"]
        .iter()
        .map(|s| s.to_string())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    // Recorded PICO_VARIANT_INFO strings. These are what the devices
    // actually return, so the detection logic can be tested without the
    // hardware present.
    const VARIANTS: &[(&str, u8)] = &[
        ("2204A", 2),
        ("2205A", 2),
        ("2205AMSO", 2),
        ("2206B", 2),
        ("2405A", 4),
        ("2406B", 4),
        ("3204D", 2),
        ("3205DMSO", 2),
        ("3403D", 4),
        ("3404D", 4),
        ("4224", 2),
        ("4424", 4),
        ("4444", 4),
        ("4824", 8),
        ("5242D", 2),
        ("5244D", 2),
        ("5442D", 4),
        ("5444D", 4),
    ];

    #[test]
    fn channel_count_comes_out_of_the_model_number() {
        for (model, expected) in VARIANTS {
            assert_eq!(
                channels_from_variant(model),
                Some(*expected),
                "wrong channel count for {model}"
            );
        }
    }

    #[test]
    fn an_unrecognisable_model_yields_no_channel_count() {
        // The caller decides what to do; it is not handed a guess.
        assert_eq!(channels_from_variant(""), None);
        assert_eq!(channels_from_variant("PicoScope"), None);
        // Second digit 9 is not a channel count Pico ships.
        assert_eq!(channels_from_variant("2904A"), None);
    }

    #[test]
    fn a_model_with_a_prefix_still_parses() {
        // Some units answer "PicoScope 5444D" rather than a bare number.
        assert_eq!(channels_from_variant("PicoScope 5444D"), Some(4));
    }

    #[test]
    fn unknown_models_fall_back_to_two_channels_not_a_panic() {
        let caps = capabilities_from_variant(
            DriverFamily::Ps5000a,
            "something-new",
            "AB/123",
            ResolutionSupport::fixed(8),
        );
        assert_eq!(caps.analog_channels, 2);
        assert_eq!(caps.channel_labels, vec!["A", "B"]);
    }

    #[test]
    fn channel_labels_match_the_channel_count() {
        let caps = capabilities_from_variant(
            DriverFamily::Ps4000a,
            "4824",
            "S",
            ResolutionSupport::fixed(12),
        );
        assert_eq!(caps.analog_channels, 8);
        assert_eq!(
            caps.channel_labels,
            vec!["A", "B", "C", "D", "E", "F", "G", "H"]
        );
        assert!(caps.has_channel("H"));
        assert!(!caps.has_channel("I"));
    }

    #[test]
    fn mso_models_report_a_digital_port() {
        let mso = capabilities_from_variant(
            DriverFamily::Ps2000a,
            "2205AMSO",
            "S",
            ResolutionSupport::fixed(8),
        );
        assert_eq!(mso.digital_ports, 1);
        assert!(mso.is_mso());

        let analog_only = capabilities_from_variant(
            DriverFamily::Ps2000a,
            "2205A",
            "S",
            ResolutionSupport::fixed(8),
        );
        assert_eq!(analog_only.digital_ports, 0);
        assert!(!analog_only.is_mso());
    }

    #[test]
    fn the_model_and_serial_are_carried_through_verbatim() {
        // The UI shows these; a normalised or truncated form would not
        // match what is printed on the case.
        let caps = capabilities_from_variant(
            DriverFamily::Ps5000a,
            "5444D",
            "GQ94/0135",
            ResolutionSupport::fixed(8),
        );
        assert_eq!(caps.model, "5444D");
        assert_eq!(caps.serial, "GQ94/0135");
    }

    #[test]
    fn each_modern_family_offers_only_the_ranges_it_has() {
        // (family, first label, first code, last label, last code). The code
        // must be the driver's own index, or a range change would select
        // the wrong one.
        for (family, first, first_code, last, last_code) in [
            (DriverFamily::Ps2000a, "20 mV", 1, "20 V", 10),
            (DriverFamily::Ps3000a, "50 mV", 2, "20 V", 10),
            (DriverFamily::Ps4000a, "10 mV", 0, "50 V", 11),
            (DriverFamily::Ps5000a, "10 mV", 0, "20 V", 10),
        ] {
            let caps =
                capabilities_from_variant(family, "3204D", "S", ResolutionSupport::fixed(8));
            let ranges = &caps.voltage_ranges;
            assert_eq!(ranges[0].label, first, "{family:?}");
            assert_eq!(ranges[0].code, first_code, "{family:?}");
            assert_eq!(ranges[ranges.len() - 1].label, last, "{family:?}");
            assert_eq!(ranges[ranges.len() - 1].code, last_code, "{family:?}");
        }
    }

    #[test]
    fn the_four_thousand_series_reports_no_signal_generator() {
        // It has none, and claiming otherwise would put a control in the UI
        // that always errors.
        let caps = capabilities_from_variant(
            DriverFamily::Ps4000a,
            "4424",
            "S",
            ResolutionSupport::fixed(12),
        );
        assert!(caps.signal_generator.is_none());
    }

    #[test]
    fn other_series_report_their_signal_generator() {
        for family in [
            DriverFamily::Ps2000a,
            DriverFamily::Ps3000a,
            DriverFamily::Ps5000a,
        ] {
            let caps =
                capabilities_from_variant(family, "3204D", "S", ResolutionSupport::fixed(8));
            let siggen = caps
                .signal_generator
                .unwrap_or_else(|| panic!("{family:?} should report a signal generator"));
            assert!(siggen.built_in);
            assert!(siggen.max_frequency_hz > 0.0);
        }
    }

    #[test]
    fn smart_probes_are_only_claimed_where_picoconnect_exists() {
        let with = capabilities_from_variant(
            DriverFamily::Ps4000a,
            "4444",
            "S",
            ResolutionSupport::fixed(14),
        );
        assert!(with.smart_probes);

        let without = capabilities_from_variant(
            DriverFamily::Ps2000a,
            "2205A",
            "S",
            ResolutionSupport::fixed(8),
        );
        assert!(!without.smart_probes);
    }

    #[test]
    fn bandwidth_is_left_unset_rather_than_guessed() {
        // A wrong bandwidth figure shown to the user is worse than none.
        let caps = capabilities_from_variant(
            DriverFamily::Ps5000a,
            "5444D",
            "S",
            ResolutionSupport::fixed(8),
        );
        assert!(caps.bandwidth_hz.is_none());
    }

    #[test]
    fn resolution_support_is_carried_from_the_device_reading() {
        // identify() reads the live value; this checks it is not overwritten.
        let switchable = ResolutionSupport {
            available_bits: vec![8, 12, 14, 15, 16],
            current_bits: 12,
            switchable: true,
        };
        let caps = capabilities_from_variant(
            DriverFamily::Ps5000a,
            "5444D",
            "S",
            switchable.clone(),
        );
        assert_eq!(caps.resolution, switchable);
        assert_eq!(caps.resolution.current_bits, 12);
    }

    #[test]
    fn flexible_families_list_every_resolution_they_accept() {
        assert_eq!(available_resolutions(DriverFamily::Ps5000a).len(), 5);
        assert_eq!(available_resolutions(DriverFamily::Ps4000a).len(), 2);
        // 8-bit only.
        assert_eq!(available_resolutions(DriverFamily::Ps2000a).len(), 1);
        assert_eq!(available_resolutions(DriverFamily::Ps3000a).len(), 1);
    }

    #[test]
    fn only_the_4444_is_a_switchable_4000a_and_the_rest_are_twelve_bit() {
        let unasked = || -> Result<DeviceResolution> {
            panic!("only the 4444 has GetDeviceResolution to ask")
        };
        for model in ["4224A", "4424A", "4824", "4824A"] {
            let support = resolution_support(DriverFamily::Ps4000a, model, unasked);
            assert_eq!(support, ResolutionSupport::fixed(12), "{model}");
        }

        let support =
            resolution_support(DriverFamily::Ps4000a, "4444", || Ok(DeviceResolution::Bits14));
        assert!(support.switchable);
        assert_eq!(support.available_bits, vec![12, 14]);
        assert_eq!(support.current_bits, 14);

        let unreadable = resolution_support(DriverFamily::Ps4000a, "PicoScope 4444", || {
            bail!("no answer")
        });
        assert_eq!(unreadable.current_bits, 12);
    }

    #[test]
    fn the_flexible_and_fixed_families_report_their_resolution() {
        let five = resolution_support(DriverFamily::Ps5000a, "5444D", || {
            Ok(DeviceResolution::Bits12)
        });
        assert!(five.switchable);
        assert_eq!(five.current_bits, 12);
        assert_eq!(
            resolution_support(DriverFamily::Ps3000a, "3204D", || bail!("unasked")),
            ResolutionSupport::fixed(8)
        );
    }

    #[test]
    fn the_model_number_is_the_first_run_of_digits() {
        assert_eq!(model_number("4444"), "4444");
        assert_eq!(model_number("PicoScope 4444"), "4444");
        assert_eq!(model_number("2205AMSO"), "2205");
        assert_eq!(model_number("no digits"), "");
    }

    use super::super::modern::{CaptureResult, TimebaseInfo};
    use super::super::types::{Coupling, RatioMode, ThresholdDirection};
    use std::sync::{Arc, Mutex};

    /// A driver family with a scripted enumerate, open and identity. No
    /// `listed` makes enumerate fail.
    struct FakeApi {
        family: DriverFamily,
        listed: Option<Vec<String>>,
        open: std::result::Result<i16, u32>,
        model: Option<&'static str>,
        closed: Arc<Mutex<Vec<i16>>>,
    }

    impl PicoModernApi for FakeApi {
        fn family(&self) -> DriverFamily {
            self.family
        }
        fn enumerate(&self) -> Result<Vec<String>> {
            match &self.listed {
                Some(listed) => Ok(listed.clone()),
                None => bail!("ps5000aEnumerateUnits failed"),
            }
        }
        fn open(&self, _: Option<&str>, _: DeviceResolution) -> Result<i16> {
            self.open
                .map_err(|status| PicoStatusError::new("ps5000aOpenUnit", status).into())
        }
        fn close(&self, handle: i16) -> Result<()> {
            self.closed.lock().unwrap().push(handle);
            Ok(())
        }
        fn unit_info(&self, _: i16, info: UnitInfo) -> Result<String> {
            match (info, self.model) {
                (UnitInfo::VariantInfo, Some(model)) => Ok(model.to_string()),
                (UnitInfo::VariantInfo, None) => bail!("PICO_INFO_UNAVAILABLE"),
                _ => Ok("AB123/0001".to_string()),
            }
        }
        fn set_channel(&self, _: i16, _: u8, _: bool, _: Coupling, _: Range, _: f32) -> Result<()> {
            unimplemented!()
        }
        fn get_timebase(&self, _: i16, _: u32, _: u32) -> Result<TimebaseInfo> {
            unimplemented!()
        }
        fn run_block(&self, _: i16, _: u32, _: u32, _: u32) -> Result<std::time::Duration> {
            unimplemented!()
        }
        fn is_ready(&self, _: i16) -> Result<bool> {
            unimplemented!()
        }
        fn set_data_buffer(&self, _: i16, _: u8, _: &mut [i16]) -> Result<()> {
            unimplemented!()
        }
        fn get_values(&self, _: i16, _: u32, _: u32, _: RatioMode) -> Result<CaptureResult> {
            unimplemented!()
        }
        fn stop(&self, _: i16) -> Result<()> {
            unimplemented!()
        }
        fn set_simple_trigger(
            &self,
            _: i16,
            _: bool,
            _: u8,
            _: i16,
            _: ThresholdDirection,
            _: u32,
            _: i16,
        ) -> Result<()> {
            unimplemented!()
        }
        fn maximum_value(&self, _: i16) -> Result<i16> {
            unimplemented!()
        }
    }

    /// Detect with every modern family installed: the 5000a behaving as
    /// described, the others with nothing attached.
    fn detect_on(
        listed: Option<&[&str]>,
        open: std::result::Result<i16, u32>,
        model: Option<&'static str>,
    ) -> (Result<DetectedScope>, Vec<i16>) {
        let closed = Arc::new(Mutex::new(Vec::new()));
        let listed: Option<Vec<String>> =
            listed.map(|serials| serials.iter().map(|s| s.to_string()).collect());
        let result = detect_with(None, PROBE_ORDER, |family| {
            let attached = family == DriverFamily::Ps5000a;
            Ok(Box::new(FakeApi {
                family,
                listed: if attached { listed.clone() } else { Some(Vec::new()) },
                open: if attached { open } else { Err(status::NOT_FOUND) },
                model,
                closed: closed.clone(),
            }) as Box<dyn PicoModernApi>)
        });
        let closed = closed.lock().unwrap().clone();
        (result, closed)
    }

    fn error_of(result: Result<DetectedScope>) -> anyhow::Error {
        match result {
            Ok(_) => panic!("detection was expected to fail"),
            Err(e) => e,
        }
    }

    const ATTACHED: Option<&[&str]> = Some(&["GQ94/0135"]);

    #[test]
    fn nothing_attached_is_reported_as_no_unit_found() {
        let err = error_of(detect_on(Some(&[]), Err(status::NOT_FOUND), Some("5444D")).0);
        assert!(err.downcast_ref::<NoUnitFound>().is_some(), "{err:#}");
    }

    #[test]
    fn an_open_answering_not_found_after_enumerate_failed_is_still_no_unit() {
        // Enumerate failing is not proof of absence, so open is tried; its
        // PICO_NOT_FOUND is.
        let err = error_of(detect_on(None, Err(status::NOT_FOUND), Some("5444D")).0);
        assert!(err.downcast_ref::<NoUnitFound>().is_some(), "{err:#}");
    }

    #[test]
    fn a_listed_unit_that_will_not_open_is_reported_as_itself() {
        // Held by another process: listed, then PICO_NOT_FOUND from open.
        let err = error_of(detect_on(ATTACHED, Err(status::NOT_FOUND), Some("5444D")).0);
        assert!(err.downcast_ref::<NoUnitFound>().is_none(), "{err:#}");
        let text = format!("{err:#}");
        assert!(text.contains("could not be opened"), "{text}");
        assert!(text.contains("PICO_NOT_FOUND"), "lost the unit's own error: {text}");
    }

    #[test]
    fn a_unit_that_fails_to_open_for_any_other_reason_is_reported_as_itself() {
        for listed in [ATTACHED, None] {
            let err = error_of(detect_on(listed, Err(0x04), Some("5444D")).0);
            assert!(err.downcast_ref::<NoUnitFound>().is_none(), "{err:#}");
            assert!(format!("{err:#}").contains("PICO_FW_FAIL"), "{err:#}");
        }
    }

    #[test]
    fn a_unit_that_opens_but_cannot_be_identified_is_closed_and_reported() {
        let (result, closed) = detect_on(ATTACHED, Ok(9), None);
        let err = error_of(result);
        assert!(err.downcast_ref::<NoUnitFound>().is_none(), "{err:#}");
        assert_eq!(closed, vec![9]);
    }

    #[test]
    fn an_identified_unit_is_handed_over_open() {
        let (result, closed) = detect_on(ATTACHED, Ok(9), Some("5444D"));
        let detected = result.unwrap_or_else(|e| panic!("{e:#}"));
        assert_eq!(detected.handle, 9);
        assert_eq!(detected.capabilities.model, "5444D");
        assert!(closed.is_empty(), "the caller owns closing it");
    }

    #[test]
    fn modern_families_advertise_advanced_triggers_and_legacy_does_not() {
        assert!(!advanced_triggers(DriverFamily::Ps5000a).is_empty());
        assert!(advanced_triggers(DriverFamily::Ps2000).is_empty());
    }

    #[test]
    fn the_probe_order_puts_no_legacy_family_in_the_modern_loop() {
        // ps2000's open_unit claims the first unit it sees regardless of
        // series, so probing it here could grab the wrong scope.
        assert!(!PROBE_ORDER.contains(&DriverFamily::Ps2000));
        assert_eq!(PROBE_ORDER.len(), 4);
    }

    #[test]
    fn detection_without_any_driver_installed_says_what_to_do() {
        // On a developer machine no PicoTech library is present, which is
        // exactly the case this message is for.
        if !super::super::loader::installed_families().is_empty() {
            return; // A machine with the SDK; nothing to assert here.
        }
        // DetectedScope holds a boxed trait object, so it is not Debug and
        // unwrap_err is unavailable.
        let err = match detect(None) {
            Ok(_) => panic!("no driver is installed, so detection cannot succeed"),
            Err(e) => e.to_string(),
        };
        assert!(err.contains("lager install"), "unhelpful message: {err}");
    }
}

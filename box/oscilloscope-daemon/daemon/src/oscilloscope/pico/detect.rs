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
use super::types::{DeviceResolution, Range, UnitInfo};

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
pub fn detect(serial: Option<&str>) -> Result<DetectedScope> {
    let installed = super::loader::installed_families();
    if installed.is_empty() {
        bail!(
            "no PicoScope driver libraries are installed. Run `lager install` \
             on the box, or set LD_LIBRARY_PATH if the SDK is somewhere \
             unusual."
        );
    }

    let mut attempts: Vec<String> = Vec::new();

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
        match api.enumerate() {
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
            }
            Err(e) => {
                tracing::debug!(
                    family = family.as_str(),
                    error = %e,
                    "could not enumerate; trying to open anyway"
                );
            }
        }

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
            Err(e) => attempts.push(format!("{}: {e}", family.as_str())),
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

    bail!(
        "no supported PicoScope was found{}. Tried:\n  {}",
        serial
            .map(|s| format!(" with serial {s}"))
            .unwrap_or_default(),
        attempts.join("\n  ")
    )
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

    // Read the resolution from the device where it is a setting, so a
    // 5000-series part opened at 8 bits reports 8 rather than its maximum.
    let resolution = if api.supports_resolution_switching() {
        let current = api.resolution(handle).unwrap_or(DeviceResolution::Bits8);
        ResolutionSupport {
            available_bits: available_resolutions(api.family())
                .iter()
                .map(|r| r.bits())
                .collect(),
            current_bits: current.bits(),
            switchable: true,
        }
    } else {
        ResolutionSupport::fixed(8)
    };

    Ok(capabilities_from_variant(
        api.family(),
        &model,
        &serial,
        resolution,
    ))
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
        // 4000a parts are 12-bit, with a 14-bit mode on the 4444 and 4824.
        DriverFamily::Ps4000a => &[DeviceResolution::Bits12, DeviceResolution::Bits14],
        _ => &[DeviceResolution::Bits8],
    }
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

    let voltage_ranges: Vec<VoltageRange> = Range::ALL
        .iter()
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
    fn every_modern_family_offers_the_full_range_set() {
        for family in [
            DriverFamily::Ps2000a,
            DriverFamily::Ps3000a,
            DriverFamily::Ps4000a,
            DriverFamily::Ps5000a,
        ] {
            let caps =
                capabilities_from_variant(family, "3204D", "S", ResolutionSupport::fixed(8));
            assert_eq!(caps.voltage_ranges.len(), 12, "{family:?}");
            assert_eq!(caps.voltage_ranges[0].label, "10 mV");
            assert_eq!(caps.voltage_ranges[11].label, "50 V");
            // The code must be the driver's own index, or a range change
            // would select the wrong one.
            assert_eq!(caps.voltage_ranges[0].code, 0);
            assert_eq!(caps.voltage_ranges[11].code, 11);
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

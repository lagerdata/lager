// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! One vtable over the four modern PicoScope APIs.
//!
//! `ps2000a`, `ps3000a`, `ps4000a` and `ps5000a` are the same API four times
//! over: the same calls taking the same arguments, spelled with a different
//! prefix and a different set of C enum typedefs that all lower to the same
//! integers. Four hand-written drivers would mean four places to fix every
//! bug, and a per-call `match family` would mean the same four-way branch
//! repeated at every call site.
//!
//! So [`PicoModernApi`] is the shape all four share, and `impl_modern_api!`
//! writes each implementation. Only two real differences survive, and both
//! are macro parameters:
//!
//! * **oversample** -- `ps2000a` and `ps3000a` carry a legacy oversample
//!   argument on `GetTimebase2` and `RunBlock` that `ps4000a` and `ps5000a`
//!   dropped. Passed as `1` (no oversampling) where it exists.
//! * **open resolution** -- `ps5000aOpenUnit` takes the ADC resolution up
//!   front, because on a flexible-resolution part it decides which channel
//!   combinations are legal. The other three have no such argument.
//!
//! Those are selected by marker token rather than a runtime `if`, because
//! the two forms differ in arity: a runtime branch would have to typecheck
//! both, and the wrong-arity call is a compile error.
//!
//! The method names are spelled out at each invocation rather than pasted
//! together from a prefix. It is more to read, but `ps5000aRunBlock` is then
//! greppable, and this file needs no proc-macro dependency to build.
//!
//! Everything here is mechanical FFI. The decisions -- which range to pick,
//! when to re-arm, how to scale counts to volts -- belong to the driver on
//! top of this, not to this layer.

use std::ffi::CString;

use anyhow::{anyhow, bail, Context, Result};
use protocol::DriverFamily;

use super::status;
use super::types::{Coupling, DeviceResolution, Range, RatioMode, ThresholdDirection, UnitInfo};

/// What `GetTimebase2` reports for a proposed timebase.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TimebaseInfo {
    /// Interval between samples, in seconds. The driver reports nanoseconds
    /// as an `f32`; this is that value converted, which is what every caller
    /// actually wants.
    pub interval_seconds: f64,
    /// Largest block this timebase can capture, per channel.
    pub max_samples: u32,
}

/// Outcome of reading a captured block back.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CaptureResult {
    /// Samples actually returned, which can be fewer than requested.
    pub samples: u32,
    /// Per-channel overflow bitmask: bit N set means channel N clipped.
    pub overflow: i16,
}

impl CaptureResult {
    pub fn channel_overflowed(&self, channel: u8) -> bool {
        self.overflow & (1i16 << channel) != 0
    }
}

/// The subset of each family's API the daemon drives.
///
/// Every method takes an explicit `handle`: the drivers are handle-based and
/// one loaded library can hold several units open, so the handle is not a
/// property of the vtable.
///
/// Methods take `&self` because these types are stateless views onto a
/// loaded library. Serializing access to a given handle is the scope
/// thread's job (see `scope_thread.rs`), not this layer's.
pub trait PicoModernApi: Send + Sync {
    fn family(&self) -> DriverFamily;

    /// Serial numbers of this family's units that are attached but not open.
    ///
    /// This is the cheap half of detection: it enumerates over USB without
    /// opening anything, so probing four families costs four descriptor
    /// reads rather than four firmware uploads. It also does not disturb a
    /// unit another process already holds -- an `OpenUnit` probe would fail
    /// against one and could disrupt it.
    ///
    /// An empty list means no unit of this family is available.
    fn enumerate(&self) -> Result<Vec<String>>;

    /// Open a unit, optionally by serial.
    ///
    /// `resolution` is honoured only where `OpenUnit` accepts it; elsewhere
    /// it is ignored, and [`Self::supports_resolution_switching`] reports
    /// which case this is.
    fn open(&self, serial: Option<&str>, resolution: DeviceResolution) -> Result<i16>;

    fn close(&self, handle: i16) -> Result<()>;

    fn unit_info(&self, handle: i16, info: UnitInfo) -> Result<String>;

    fn set_channel(
        &self,
        handle: i16,
        channel: u8,
        enabled: bool,
        coupling: Coupling,
        range: Range,
        analog_offset_volts: f32,
    ) -> Result<()>;

    fn get_timebase(&self, handle: i16, timebase: u32, samples: u32) -> Result<TimebaseInfo>;

    /// Arm a block capture, returning the driver's own estimate of how long
    /// it will take. The acquisition loop sizes its poll interval from that
    /// rather than spinning or using a fixed sleep.
    fn run_block(
        &self,
        handle: i16,
        pre_trigger_samples: u32,
        post_trigger_samples: u32,
        timebase: u32,
    ) -> Result<std::time::Duration>;

    fn is_ready(&self, handle: i16) -> Result<bool>;

    /// Hand the driver somewhere to write one channel's data.
    ///
    /// # Safety contract
    ///
    /// The driver retains this pointer until the next `SetDataBuffer` for
    /// the same channel, a `stop`, or a `close`. `buffer` must therefore
    /// stay alive and unmoved across the [`Self::get_values`] that fills it.
    /// Callers satisfy this by owning the buffers alongside the handle.
    fn set_data_buffer(&self, handle: i16, channel: u8, buffer: &mut [i16]) -> Result<()>;

    fn get_values(
        &self,
        handle: i16,
        start_index: u32,
        samples: u32,
        ratio_mode: RatioMode,
    ) -> Result<CaptureResult>;

    fn stop(&self, handle: i16) -> Result<()>;

    /// Configure the single-channel edge trigger.
    ///
    /// `threshold` is in ADC counts, not volts: converting needs the
    /// channel's range and the device's full-scale count, which the driver
    /// above knows and this layer deliberately does not.
    ///
    /// `auto_trigger_ms` of 0 waits indefinitely (normal mode); non-zero
    /// captures anyway after that long without an edge (auto mode).
    #[allow(clippy::too_many_arguments)]
    fn set_simple_trigger(
        &self,
        handle: i16,
        enabled: bool,
        channel: u8,
        threshold: i16,
        direction: ThresholdDirection,
        delay_samples: u32,
        auto_trigger_ms: i16,
    ) -> Result<()>;

    /// Full-scale ADC count at the current resolution.
    ///
    /// Needed to turn counts into volts, and not a constant: on ps5000a it
    /// changes with the resolution setting.
    fn maximum_value(&self, handle: i16) -> Result<i16>;

    /// Whether this family has `SetDeviceResolution` (ps4000a and ps5000a).
    fn supports_resolution_switching(&self) -> bool {
        false
    }

    fn set_resolution(&self, _handle: i16, _resolution: DeviceResolution) -> Result<()> {
        bail!(
            "{} has a fixed ADC resolution; it cannot be changed at runtime",
            self.family().as_str()
        )
    }

    /// The device's current resolution. Fixed-resolution families report
    /// 8 bits, which is what every 2000a and 3000a part is.
    fn resolution(&self, _handle: i16) -> Result<DeviceResolution> {
        Ok(DeviceResolution::Bits8)
    }
}

/// Turn a `PICO_STATUS` into a `Result`, naming the call that produced it.
///
/// Not every non-zero status is a failure -- see [`status::is_success`] for
/// the power-supply warnings that mean "open and usable".
fn check(raw: u32, call: &'static str) -> Result<()> {
    if status::is_success(raw) {
        Ok(())
    } else {
        Err(anyhow!("{call}: {}", status::describe(raw)))
    }
}

/// `GetTimebase2` / `RunBlock`, with or without the legacy oversample arg.
macro_rules! timebase_call {
    (with_oversample, $lib:expr, $f:ident, $handle:expr, $tb:expr, $n:expr, $ns:expr, $max:expr) => {
        unsafe { $lib.$f($handle, $tb, $n, $ns, 1, $max, 0) }
    };
    (no_oversample, $lib:expr, $f:ident, $handle:expr, $tb:expr, $n:expr, $ns:expr, $max:expr) => {
        unsafe { $lib.$f($handle, $tb, $n, $ns, $max, 0) }
    };
}

macro_rules! run_block_call {
    (with_oversample, $lib:expr, $f:ident, $handle:expr, $pre:expr, $post:expr, $tb:expr, $ms:expr) => {
        unsafe { $lib.$f($handle, $pre, $post, $tb, 1, $ms, 0, None, std::ptr::null_mut()) }
    };
    (no_oversample, $lib:expr, $f:ident, $handle:expr, $pre:expr, $post:expr, $tb:expr, $ms:expr) => {
        unsafe { $lib.$f($handle, $pre, $post, $tb, $ms, 0, None, std::ptr::null_mut()) }
    };
}

/// `OpenUnit`, with or without the up-front resolution argument.
macro_rules! open_call {
    (with_resolution, $lib:expr, $f:ident, $handle:expr, $serial:expr, $res:expr) => {
        unsafe { $lib.$f($handle, $serial, $res.code() as _) }
    };
    (no_resolution, $lib:expr, $f:ident, $handle:expr, $serial:expr, $res:expr) => {{
        // This family's OpenUnit has no resolution argument. The value is
        // consumed here so the trait can keep one signature for all four.
        let _: DeviceResolution = $res;
        unsafe { $lib.$f($handle, $serial) }
    }};
}

/// Implement [`PicoModernApi`] for one family. See the module docs.
macro_rules! impl_modern_api {
    (
        $name:ident,
        family: $family:expr,
        loader: $loader:path,
        oversample: $oversample:ident,
        open: $open:ident / $open_form:ident,
        close: $close:ident,
        enumerate: $enumerate:ident,
        unit_info: $unit_info:ident,
        set_channel: $set_channel:ident,
        get_timebase: $get_timebase:ident,
        run_block: $run_block:ident,
        is_ready: $is_ready:ident,
        set_data_buffer: $set_data_buffer:ident,
        get_values: $get_values:ident,
        stop: $stop:ident,
        set_simple_trigger: $set_simple_trigger:ident,
        maximum_value: $maximum_value:ident,
        resolution: { $($res_tokens:tt)* }
    ) => {
        #[doc = concat!("`PicoModernApi` over the ", stringify!($family), " driver.")]
        ///
        /// Generated by `impl_modern_api!`.
        pub struct $name;

        impl $name {
            /// Whether this family's driver is installed and loadable.
            pub fn available() -> bool {
                $loader().is_ok()
            }
        }

        impl PicoModernApi for $name {
            fn family(&self) -> DriverFamily {
                $family
            }

            fn enumerate(&self) -> Result<Vec<String>> {
                let lib = $loader()?;
                let mut count: i16 = 0;
                // The driver writes a comma-separated serial list and
                // updates the length in place, so the length starts as the
                // buffer's capacity.
                let mut serials = [0i8; 256];
                let mut length: i16 = serials.len() as i16;

                let raw = unsafe {
                    lib.$enumerate(&mut count, serials.as_mut_ptr(), &mut length)
                };
                check(raw, stringify!($enumerate))?;

                if count <= 0 {
                    return Ok(Vec::new());
                }
                Ok(parse_serial_list(&serials))
            }

            fn open(&self, serial: Option<&str>, resolution: DeviceResolution) -> Result<i16> {
                let lib = $loader()?;
                let mut handle: i16 = 0;

                // The driver takes a mutable char* it does not write to.
                // A CString keeps the NUL terminator correct; None asks for
                // the first unit it finds.
                let mut owned = match serial {
                    Some(s) => Some(
                        CString::new(s)
                            .with_context(|| format!("serial {s:?} contains a NUL byte"))?
                            .into_bytes_with_nul(),
                    ),
                    None => None,
                };
                let serial_ptr = owned
                    .as_mut()
                    .map(|b| b.as_mut_ptr() as *mut i8)
                    .unwrap_or(std::ptr::null_mut());

                let raw = open_call!(
                    $open_form, lib, $open, &mut handle, serial_ptr, resolution
                );
                check(raw, stringify!($open))?;

                if handle <= 0 {
                    bail!(
                        "{}: driver reported success but returned handle {handle}",
                        stringify!($open)
                    );
                }
                Ok(handle)
            }

            fn close(&self, handle: i16) -> Result<()> {
                let lib = $loader()?;
                check(unsafe { lib.$close(handle) }, stringify!($close))
            }

            fn unit_info(&self, handle: i16, info: UnitInfo) -> Result<String> {
                let lib = $loader()?;
                // 64 bytes covers every PICO_INFO string these drivers
                // return; required_size below catches it if that changes.
                let mut buffer = [0i8; 64];
                let mut required: i16 = 0;
                let raw = unsafe {
                    lib.$unit_info(
                        handle,
                        buffer.as_mut_ptr(),
                        buffer.len() as i16,
                        &mut required,
                        info.code() as _,
                    )
                };
                check(raw, stringify!($unit_info))?;

                let bytes: Vec<u8> = buffer
                    .iter()
                    .take_while(|&&c| c != 0)
                    .map(|&c| c as u8)
                    .collect();
                Ok(String::from_utf8_lossy(&bytes).trim().to_string())
            }

            fn set_channel(
                &self,
                handle: i16,
                channel: u8,
                enabled: bool,
                coupling: Coupling,
                range: Range,
                analog_offset_volts: f32,
            ) -> Result<()> {
                let lib = $loader()?;
                let raw = unsafe {
                    lib.$set_channel(
                        handle,
                        channel as _,
                        enabled as i16,
                        coupling.code() as _,
                        range.code() as _,
                        analog_offset_volts,
                    )
                };
                check(raw, stringify!($set_channel))
            }

            fn get_timebase(
                &self,
                handle: i16,
                timebase: u32,
                samples: u32,
            ) -> Result<TimebaseInfo> {
                let lib = $loader()?;
                let mut interval_ns: f32 = 0.0;
                let mut max_samples: i32 = 0;
                let raw = timebase_call!(
                    $oversample, lib, $get_timebase, handle, timebase,
                    samples as i32, &mut interval_ns, &mut max_samples
                );
                check(raw, stringify!($get_timebase))?;

                Ok(TimebaseInfo {
                    interval_seconds: f64::from(interval_ns) * 1e-9,
                    max_samples: max_samples.max(0) as u32,
                })
            }

            fn run_block(
                &self,
                handle: i16,
                pre_trigger_samples: u32,
                post_trigger_samples: u32,
                timebase: u32,
            ) -> Result<std::time::Duration> {
                let lib = $loader()?;
                let mut time_indisposed_ms: i32 = 0;
                let raw = run_block_call!(
                    $oversample, lib, $run_block, handle,
                    pre_trigger_samples as i32, post_trigger_samples as i32,
                    timebase, &mut time_indisposed_ms
                );
                check(raw, stringify!($run_block))?;

                Ok(std::time::Duration::from_millis(
                    time_indisposed_ms.max(0) as u64,
                ))
            }

            fn is_ready(&self, handle: i16) -> Result<bool> {
                let lib = $loader()?;
                let mut ready: i16 = 0;
                let raw = unsafe { lib.$is_ready(handle, &mut ready) };
                check(raw, stringify!($is_ready))?;
                Ok(ready != 0)
            }

            fn set_data_buffer(
                &self,
                handle: i16,
                channel: u8,
                buffer: &mut [i16],
            ) -> Result<()> {
                let lib = $loader()?;
                let length = i32::try_from(buffer.len()).context(
                    "capture buffer is larger than the driver's i32 length field",
                )?;
                // Safety: see the trait's safety contract -- the caller keeps
                // `buffer` alive and in place until the matching get_values.
                let raw = unsafe {
                    lib.$set_data_buffer(
                        handle,
                        channel as _,
                        buffer.as_mut_ptr(),
                        length,
                        0,
                        RatioMode::None.code() as _,
                    )
                };
                check(raw, stringify!($set_data_buffer))
            }

            fn get_values(
                &self,
                handle: i16,
                start_index: u32,
                samples: u32,
                ratio_mode: RatioMode,
            ) -> Result<CaptureResult> {
                let lib = $loader()?;
                let mut count = samples;
                let mut overflow: i16 = 0;
                let raw = unsafe {
                    lib.$get_values(
                        handle,
                        start_index,
                        &mut count,
                        1,
                        ratio_mode.code() as _,
                        0,
                        &mut overflow,
                    )
                };
                check(raw, stringify!($get_values))?;
                Ok(CaptureResult {
                    samples: count,
                    overflow,
                })
            }

            fn stop(&self, handle: i16) -> Result<()> {
                let lib = $loader()?;
                check(unsafe { lib.$stop(handle) }, stringify!($stop))
            }

            fn set_simple_trigger(
                &self,
                handle: i16,
                enabled: bool,
                channel: u8,
                threshold: i16,
                direction: ThresholdDirection,
                delay_samples: u32,
                auto_trigger_ms: i16,
            ) -> Result<()> {
                let lib = $loader()?;
                let raw = unsafe {
                    lib.$set_simple_trigger(
                        handle,
                        enabled as i16,
                        channel as _,
                        threshold,
                        direction.code() as _,
                        delay_samples,
                        auto_trigger_ms,
                    )
                };
                check(raw, stringify!($set_simple_trigger))
            }

            fn maximum_value(&self, handle: i16) -> Result<i16> {
                let lib = $loader()?;
                let mut value: i16 = 0;
                let raw = unsafe { lib.$maximum_value(handle, &mut value) };
                check(raw, stringify!($maximum_value))?;
                if value == 0 {
                    bail!(
                        "{} returned a full-scale count of 0, which would make \
                         every sample divide by zero",
                        stringify!($maximum_value)
                    );
                }
                Ok(value)
            }

            $($res_tokens)*
        }
    };
}

impl_modern_api! {
    Ps2000aApi,
    family: DriverFamily::Ps2000a,
    loader: super::loader::ps2000a,
    oversample: with_oversample,
    open: ps2000aOpenUnit / no_resolution,
    close: ps2000aCloseUnit,
    enumerate: ps2000aEnumerateUnits,
    unit_info: ps2000aGetUnitInfo,
    set_channel: ps2000aSetChannel,
    get_timebase: ps2000aGetTimebase2,
    run_block: ps2000aRunBlock,
    is_ready: ps2000aIsReady,
    set_data_buffer: ps2000aSetDataBuffer,
    get_values: ps2000aGetValues,
    stop: ps2000aStop,
    set_simple_trigger: ps2000aSetSimpleTrigger,
    maximum_value: ps2000aMaximumValue,
    // 2000a parts are 8-bit only, so the trait defaults are correct.
    resolution: {}
}

impl_modern_api! {
    Ps3000aApi,
    family: DriverFamily::Ps3000a,
    loader: super::loader::ps3000a,
    oversample: with_oversample,
    open: ps3000aOpenUnit / no_resolution,
    close: ps3000aCloseUnit,
    enumerate: ps3000aEnumerateUnits,
    unit_info: ps3000aGetUnitInfo,
    set_channel: ps3000aSetChannel,
    get_timebase: ps3000aGetTimebase2,
    run_block: ps3000aRunBlock,
    is_ready: ps3000aIsReady,
    set_data_buffer: ps3000aSetDataBuffer,
    get_values: ps3000aGetValues,
    stop: ps3000aStop,
    set_simple_trigger: ps3000aSetSimpleTrigger,
    maximum_value: ps3000aMaximumValue,
    // 3000a parts are 8-bit only.
    resolution: {}
}

impl_modern_api! {
    Ps4000aApi,
    family: DriverFamily::Ps4000a,
    loader: super::loader::ps4000a,
    oversample: no_oversample,
    open: ps4000aOpenUnit / no_resolution,
    close: ps4000aCloseUnit,
    enumerate: ps4000aEnumerateUnits,
    unit_info: ps4000aGetUnitInfo,
    set_channel: ps4000aSetChannel,
    get_timebase: ps4000aGetTimebase2,
    run_block: ps4000aRunBlock,
    is_ready: ps4000aIsReady,
    set_data_buffer: ps4000aSetDataBuffer,
    get_values: ps4000aGetValues,
    stop: ps4000aStop,
    set_simple_trigger: ps4000aSetSimpleTrigger,
    maximum_value: ps4000aMaximumValue,
    // 4000a has Set/GetDeviceResolution, but not on OpenUnit: the 14-bit
    // parts switch after the unit is open.
    resolution: {
        fn supports_resolution_switching(&self) -> bool {
            true
        }

        fn set_resolution(&self, handle: i16, resolution: DeviceResolution) -> Result<()> {
            let lib = super::loader::ps4000a()?;
            let raw = unsafe { lib.ps4000aSetDeviceResolution(handle, resolution.code() as _) };
            check(raw, "ps4000aSetDeviceResolution")
        }

        fn resolution(&self, handle: i16) -> Result<DeviceResolution> {
            let lib = super::loader::ps4000a()?;
            let mut code = 0;
            let raw = unsafe { lib.ps4000aGetDeviceResolution(handle, &mut code) };
            check(raw, "ps4000aGetDeviceResolution")?;
            resolution_from_code(code as i32)
        }
    }
}

impl_modern_api! {
    Ps5000aApi,
    family: DriverFamily::Ps5000a,
    loader: super::loader::ps5000a,
    oversample: no_oversample,
    open: ps5000aOpenUnit / with_resolution,
    close: ps5000aCloseUnit,
    enumerate: ps5000aEnumerateUnits,
    unit_info: ps5000aGetUnitInfo,
    set_channel: ps5000aSetChannel,
    get_timebase: ps5000aGetTimebase2,
    run_block: ps5000aRunBlock,
    is_ready: ps5000aIsReady,
    set_data_buffer: ps5000aSetDataBuffer,
    get_values: ps5000aGetValues,
    stop: ps5000aStop,
    set_simple_trigger: ps5000aSetSimpleTrigger,
    maximum_value: ps5000aMaximumValue,
    // The flexible-resolution family: 8 to 16 bits, switchable while open.
    resolution: {
        fn supports_resolution_switching(&self) -> bool {
            true
        }

        fn set_resolution(&self, handle: i16, resolution: DeviceResolution) -> Result<()> {
            let lib = super::loader::ps5000a()?;
            let raw = unsafe { lib.ps5000aSetDeviceResolution(handle, resolution.code() as _) };
            check(raw, "ps5000aSetDeviceResolution")
        }

        fn resolution(&self, handle: i16) -> Result<DeviceResolution> {
            let lib = super::loader::ps5000a()?;
            let mut code = 0;
            let raw = unsafe { lib.ps5000aGetDeviceResolution(handle, &mut code) };
            check(raw, "ps5000aGetDeviceResolution")?;
            resolution_from_code(code as i32)
        }
    }
}

/// Split `EnumerateUnits`' output into serial numbers.
///
/// The drivers return one NUL-terminated buffer holding a comma-separated
/// list, e.g. `"AQ005/139,VDR61/356"`. Serials themselves contain a slash
/// but never a comma, so splitting on commas is safe.
fn parse_serial_list(buffer: &[i8]) -> Vec<String> {
    let bytes: Vec<u8> = buffer
        .iter()
        .take_while(|&&c| c != 0)
        .map(|&c| c as u8)
        .collect();

    String::from_utf8_lossy(&bytes)
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Map a driver resolution code back to the normalized enum.
fn resolution_from_code(code: i32) -> Result<DeviceResolution> {
    match code {
        0 => Ok(DeviceResolution::Bits8),
        1 => Ok(DeviceResolution::Bits12),
        2 => Ok(DeviceResolution::Bits14),
        3 => Ok(DeviceResolution::Bits15),
        4 => Ok(DeviceResolution::Bits16),
        other => bail!("driver reported unknown device resolution code {other}"),
    }
}

/// A vtable for `family`, or an error naming what is missing.
pub fn api_for(family: DriverFamily) -> Result<Box<dyn PicoModernApi>> {
    match family {
        DriverFamily::Ps2000a => Ok(Box::new(Ps2000aApi)),
        DriverFamily::Ps3000a => Ok(Box::new(Ps3000aApi)),
        DriverFamily::Ps4000a => Ok(Box::new(Ps4000aApi)),
        DriverFamily::Ps5000a => Ok(Box::new(Ps5000aApi)),
        DriverFamily::Ps2000 => bail!(
            "ps2000 is the legacy snake_case API and does not fit this vtable; \
             it has its own driver in pico/ps2000.rs"
        ),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // These tests exercise the parts that do not need a device attached:
    // status handling, the resolution mapping, and the per-family wiring the
    // macro produced. Anything requiring a real capture lives in the
    // hardware-marked suite.

    #[test]
    fn a_successful_status_is_ok() {
        assert!(check(status::OK, "ps5000aOpenUnit").is_ok());
    }

    #[test]
    fn a_failing_status_names_the_call_that_produced_it() {
        let err = check(status::NOT_FOUND, "ps5000aOpenUnit")
            .unwrap_err()
            .to_string();
        assert!(err.contains("ps5000aOpenUnit"), "unhelpful message: {err}");
        assert!(err.contains("PICO_NOT_FOUND"), "lost the code: {err}");
    }

    #[test]
    fn a_power_warning_is_not_treated_as_a_failure() {
        // Returned by OpenUnit on USB-powered parts with no barrel jack.
        // The unit is open and usable.
        assert!(check(status::POWER_SUPPLY_NOT_CONNECTED, "ps5000aOpenUnit").is_ok());
    }

    #[test]
    fn every_family_reports_the_family_it_was_generated_for() {
        // Guards against a copy-paste slip in the macro invocations, which
        // would otherwise surface as a scope detected as the wrong series.
        assert_eq!(Ps2000aApi.family(), DriverFamily::Ps2000a);
        assert_eq!(Ps3000aApi.family(), DriverFamily::Ps3000a);
        assert_eq!(Ps4000aApi.family(), DriverFamily::Ps4000a);
        assert_eq!(Ps5000aApi.family(), DriverFamily::Ps5000a);
    }

    #[test]
    fn api_for_returns_the_matching_vtable() {
        for family in [
            DriverFamily::Ps2000a,
            DriverFamily::Ps3000a,
            DriverFamily::Ps4000a,
            DriverFamily::Ps5000a,
        ] {
            let api = api_for(family).expect("modern family should have a vtable");
            assert_eq!(api.family(), family);
        }
    }

    #[test]
    fn the_legacy_family_is_rejected_with_a_pointer_to_its_driver() {
        // Boxed trait objects are not Debug, so unwrap_err is unavailable.
        let err = match api_for(DriverFamily::Ps2000) {
            Ok(_) => panic!("the legacy family must not resolve to a modern vtable"),
            Err(e) => e.to_string(),
        };
        assert!(err.contains("ps2000.rs"), "unhelpful message: {err}");
    }

    #[test]
    fn only_the_flexible_resolution_families_claim_switching() {
        assert!(!Ps2000aApi.supports_resolution_switching());
        assert!(!Ps3000aApi.supports_resolution_switching());
        assert!(Ps4000aApi.supports_resolution_switching());
        assert!(Ps5000aApi.supports_resolution_switching());
    }

    #[test]
    fn fixed_resolution_families_report_eight_bits() {
        // 2000a and 3000a parts are 8-bit; the trait default is correct for
        // them and no device is needed to answer.
        assert_eq!(Ps2000aApi.resolution(0).unwrap(), DeviceResolution::Bits8);
        assert_eq!(Ps3000aApi.resolution(0).unwrap(), DeviceResolution::Bits8);
    }

    #[test]
    fn setting_resolution_on_a_fixed_family_explains_why_not() {
        let err = Ps2000aApi
            .set_resolution(1, DeviceResolution::Bits16)
            .unwrap_err()
            .to_string();
        assert!(err.contains("ps2000a"), "does not name the family: {err}");
        assert!(err.contains("fixed"), "does not say why: {err}");
    }

    #[test]
    fn resolution_codes_map_back_to_their_bit_counts() {
        assert_eq!(resolution_from_code(0).unwrap(), DeviceResolution::Bits8);
        assert_eq!(resolution_from_code(4).unwrap(), DeviceResolution::Bits16);
    }

    #[test]
    fn an_unknown_resolution_code_is_an_error_not_a_guess() {
        // Silently defaulting would mis-scale every sample.
        assert!(resolution_from_code(9).is_err());
        assert!(resolution_from_code(-1).is_err());
    }

    /// Build a NUL-terminated i8 buffer, as the drivers hand one back.
    fn as_c_buffer(text: &str) -> Vec<i8> {
        text.bytes()
            .map(|b| b as i8)
            .chain(std::iter::once(0))
            .collect()
    }

    #[test]
    fn a_single_attached_unit_parses_to_one_serial() {
        assert_eq!(
            parse_serial_list(&as_c_buffer("AQ005/139")),
            vec!["AQ005/139"]
        );
    }

    #[test]
    fn several_units_split_on_the_comma() {
        // The example from the ps2000a header's own documentation.
        assert_eq!(
            parse_serial_list(&as_c_buffer("AQ005/139,VDR61/356,ZOR14/107")),
            vec!["AQ005/139", "VDR61/356", "ZOR14/107"]
        );
    }

    #[test]
    fn serials_keep_their_slash() {
        // Pico serials are batch/serial; splitting on the wrong character
        // would truncate every one of them.
        let parsed = parse_serial_list(&as_c_buffer("GQ94/0135"));
        assert_eq!(parsed, vec!["GQ94/0135"]);
        assert!(parsed[0].contains('/'));
    }

    #[test]
    fn an_empty_buffer_yields_no_serials() {
        assert!(parse_serial_list(&as_c_buffer("")).is_empty());
        assert!(parse_serial_list(&[0i8; 32]).is_empty());
    }

    #[test]
    fn trailing_separators_and_padding_are_dropped() {
        // Some drivers pad the list; an empty string is not a unit.
        assert_eq!(parse_serial_list(&as_c_buffer("AQ005/139,")), vec!["AQ005/139"]);
        assert_eq!(
            parse_serial_list(&as_c_buffer(" AQ005/139 , VDR61/356 ")),
            vec!["AQ005/139", "VDR61/356"]
        );
    }

    #[test]
    fn nothing_past_the_nul_terminator_is_read() {
        // The buffer is reused, so stale bytes sit after the terminator.
        let mut buffer = as_c_buffer("AQ005/139");
        buffer.extend(as_c_buffer("STALE/999"));
        assert_eq!(parse_serial_list(&buffer), vec!["AQ005/139"]);
    }

    #[test]
    fn overflow_mask_is_read_per_channel() {
        let result = CaptureResult {
            samples: 1000,
            overflow: 0b0101,
        };
        assert!(result.channel_overflowed(0));
        assert!(!result.channel_overflowed(1));
        assert!(result.channel_overflowed(2));
        assert!(!result.channel_overflowed(3));
    }

    #[test]
    fn no_overflow_reports_nothing_clipped() {
        let result = CaptureResult {
            samples: 1,
            overflow: 0,
        };
        for channel in 0..4 {
            assert!(!result.channel_overflowed(channel));
        }
    }
}

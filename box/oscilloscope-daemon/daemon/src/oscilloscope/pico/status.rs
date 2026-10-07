// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Human-readable text for `PICO_STATUS` codes.
//!
//! The drivers return a bare `uint32_t`, so without this every failure
//! surfaces to the user as a hex number they have to look up in a PDF. The
//! codes are shared across every family (PicoStatus.h is duplicated
//! verbatim in each family's include directory), so one table serves all of
//! them.
//!
//! Only the codes the daemon can actually provoke are named. Anything else
//! falls back to the hex value plus a pointer to the header, which is more
//! useful than a wrong guess.

/// `PICO_OK`.
pub const OK: u32 = 0x00000000;
/// `PICO_BUSY` -- the device is still working on the last request.
pub const BUSY: u32 = 0x00000027;
/// `PICO_NOT_FOUND` -- no unit matched, or none is attached.
pub const NOT_FOUND: u32 = 0x00000003;
/// `PICO_NOT_RESPONDING`.
pub const NOT_RESPONDING: u32 = 0x00000007;
/// `PICO_INVALID_HANDLE` -- the unit was closed or unplugged.
pub const INVALID_HANDLE: u32 = 0x0000000C;
/// `PICO_POWER_SUPPLY_CONNECTED`.
pub const POWER_SUPPLY_CONNECTED: u32 = 0x00000119;
/// `PICO_POWER_SUPPLY_NOT_CONNECTED` -- USB-powered part needs the DC input.
pub const POWER_SUPPLY_NOT_CONNECTED: u32 = 0x0000011A;
/// `PICO_POWER_SUPPLY_REQUEST_INVALID`.
pub const POWER_SUPPLY_REQUEST_INVALID: u32 = 0x0000011B;
/// `PICO_POWER_SUPPLY_UNDERVOLTAGE`.
pub const POWER_SUPPLY_UNDERVOLTAGE: u32 = 0x0000011C;
/// `PICO_CAPTURING_DATA`.
pub const CAPTURING_DATA: u32 = 0x0000011D;
/// `PICO_USB3_0_DEVICE_NON_USB3_0_PORT`.
pub const USB3_DEVICE_NON_USB3_PORT: u32 = 0x0000011E;
/// `PICO_NOT_SUPPORTED_BY_THIS_DEVICE`.
pub const NOT_SUPPORTED_BY_THIS_DEVICE: u32 = 0x0000011F;

/// A call that returned something other than `PICO_OK`.
///
/// Kept as the code rather than flattened to text, so that a caller can
/// tell "nothing is attached" or "the unit has gone" from any other
/// failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PicoStatusError {
    pub call: &'static str,
    pub status: u32,
}

impl PicoStatusError {
    pub fn new(call: &'static str, status: u32) -> Self {
        Self { call, status }
    }

    /// Whether the unit itself is gone -- unplugged, or no longer
    /// answering -- rather than this one request having been refused.
    pub fn unit_is_gone(&self) -> bool {
        matches!(self.status, NOT_RESPONDING | INVALID_HANDLE)
    }
}

impl std::fmt::Display for PicoStatusError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.call, describe(self.status))
    }
}

impl std::error::Error for PicoStatusError {}

/// Whether `error`, or anything that caused it, says the unit is gone.
pub fn unit_is_gone(error: &anyhow::Error) -> bool {
    error.chain().any(|cause| {
        cause
            .downcast_ref::<PicoStatusError>()
            .is_some_and(PicoStatusError::unit_is_gone)
    })
}

/// Describe a status code for a log line or an error message.
pub fn describe(status: u32) -> String {
    let text = match status {
        OK => "PICO_OK",
        0x00000001 => "PICO_MAX_UNITS_OPENED: the driver already has as many \
                       scopes open as it supports",
        0x00000002 => "PICO_MEMORY_FAIL: the driver could not allocate memory",
        NOT_FOUND => "PICO_NOT_FOUND: no matching scope is attached. Check the \
                      USB cable and that no other process holds the device",
        0x00000004 => "PICO_FW_FAIL: the unit's firmware failed to load",
        0x00000005 => "PICO_OPEN_OPERATION_IN_PROGRESS",
        0x00000006 => "PICO_OPERATION_FAILED",
        NOT_RESPONDING => "PICO_NOT_RESPONDING: the scope stopped answering. \
                           It usually needs a physical replug",
        0x00000008 => "PICO_CONFIG_FAIL: the unit's configuration is corrupt",
        0x00000009 => "PICO_KERNEL_DRIVER_TOO_OLD",
        0x0000000A => "PICO_EEPROM_CORRUPT",
        0x0000000B => "PICO_OS_NOT_SUPPORTED",
        INVALID_HANDLE => "PICO_INVALID_HANDLE: the device handle is stale, which \
                           means the unit was closed or unplugged",
        0x0000000D => "PICO_INVALID_PARAMETER",
        0x0000000E => "PICO_INVALID_TIMEBASE: this timebase is not available, \
                       often because too many channels are enabled for it",
        0x0000000F => "PICO_INVALID_VOLTAGE_RANGE: this model does not have \
                       the requested range",
        0x00000010 => "PICO_INVALID_CHANNEL: this model does not have that \
                       channel",
        0x00000011 => "PICO_INVALID_TRIGGER_CHANNEL",
        0x00000012 => "PICO_INVALID_CONDITION_CHANNEL",
        0x00000013 => "PICO_NO_SIGNAL_GENERATOR: this model has no built-in \
                       signal generator",
        0x00000014 => "PICO_STREAMING_FAILED",
        0x00000015 => "PICO_BLOCK_MODE_FAILED",
        0x00000016 => "PICO_NULL_PARAMETER",
        0x00000018 => "PICO_DATA_NOT_AVAILABLE: no capture has completed yet",
        0x00000019 => "PICO_STRING_BUFFER_TO_SMALL",
        0x0000001B => "PICO_AUTO_TRIGGER_TIME_TO_SHORT",
        0x0000001C => "PICO_BUFFER_STALL",
        0x0000001D => "PICO_TOO_MANY_SAMPLES: the request exceeds this \
                       model's capture memory",
        0x00000024 => "PICO_DEVICE_SAMPLING: the device is mid-capture; stop \
                       it before changing this setting",
        0x00000025 => "PICO_NO_SAMPLES_AVAILABLE",
        0x00000026 => "PICO_SEGMENT_OUT_OF_RANGE",
        BUSY => "PICO_BUSY: the device is still working on the previous request",
        0x00000028 => "PICO_STARTINDEX_INVALID",
        0x00000029 => "PICO_INVALID_INFO",
        0x0000002A => "PICO_INFO_UNAVAILABLE",
        0x0000002B => "PICO_INVALID_SAMPLE_INTERVAL",
        0x0000002C => "PICO_TRIGGER_ERROR",
        0x0000002D => "PICO_MEMORY",
        POWER_SUPPLY_CONNECTED => {
            "PICO_POWER_SUPPLY_CONNECTED: the scope's DC supply was connected \
             and the driver has to be told before the scope will run again"
        }
        POWER_SUPPLY_NOT_CONNECTED => {
            "PICO_POWER_SUPPLY_NOT_CONNECTED: this model needs its DC supply \
             or a second USB lead for full performance"
        }
        POWER_SUPPLY_REQUEST_INVALID => "PICO_POWER_SUPPLY_REQUEST_INVALID",
        POWER_SUPPLY_UNDERVOLTAGE => {
            "PICO_POWER_SUPPLY_UNDERVOLTAGE: the scope's DC supply voltage is \
             too low"
        }
        CAPTURING_DATA => {
            "PICO_CAPTURING_DATA: the scope is mid-capture; stop it first"
        }
        USB3_DEVICE_NON_USB3_PORT => {
            "PICO_USB3_0_DEVICE_NON_USB3_0_PORT: a USB 3.0 scope is plugged \
             into a slower port, which limits its sample rate"
        }
        NOT_SUPPORTED_BY_THIS_DEVICE => {
            "PICO_NOT_SUPPORTED_BY_THIS_DEVICE: this model does not have that \
             feature"
        }
        _ => {
            return format!(
                "PICO_STATUS 0x{status:08X} (see PicoStatus.h in \
                 picoscope/include/ for this code)"
            );
        }
    };
    text.to_string()
}

/// Whether a status is one the caller should treat as success: `PICO_OK`,
/// and nothing else.
///
/// The power-source codes are the awkward ones. `OpenUnit` returns them for
/// a unit that is open but will not run until `ChangePowerSource` confirms
/// which supply it is on, which `open` deals with (see
/// [`is_power_source_warning`]). From any later call they mean the supply
/// changed under the scope and that call did not run, so passing them as
/// success would leave the daemon waiting on a capture never armed.
pub fn is_success(status: u32) -> bool {
    status == OK
}

/// Whether `OpenUnit`'s status says the unit is open but waiting to be told
/// which power source it is on.
pub fn is_power_source_warning(status: u32) -> bool {
    matches!(
        status,
        POWER_SUPPLY_CONNECTED | POWER_SUPPLY_NOT_CONNECTED | USB3_DEVICE_NON_USB3_PORT
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ok_is_zero() {
        assert_eq!(OK, 0);
        assert!(is_success(OK));
    }

    #[test]
    fn a_lost_unit_is_recognised_under_any_context() {
        let gone: anyhow::Error = PicoStatusError::new("ps5000aIsReady", NOT_RESPONDING).into();
        assert!(unit_is_gone(&gone.context("reading the capture")));
        let closed: anyhow::Error = PicoStatusError::new("ps2000_ready", INVALID_HANDLE).into();
        assert!(unit_is_gone(&closed));

        let refused: anyhow::Error = PicoStatusError::new("ps5000aRunBlock", BUSY).into();
        assert!(!unit_is_gone(&refused), "one refused request is not a lost unit");
        assert!(!unit_is_gone(&anyhow::anyhow!("PICO_NOT_RESPONDING, in words")));
    }

    #[test]
    fn the_codes_match_the_headers() {
        // Every family's PicoStatus.h is the same file, so one family's
        // bindings pin them all. A wrong value here once made the daemon
        // accept PICO_CAPTURING_DATA and PICO_NOT_SUPPORTED_BY_THIS_DEVICE
        // as power warnings.
        use super::super::loader::ps5000a_sys as sys;
        assert_eq!(OK, sys::PICO_OK);
        assert_eq!(NOT_FOUND, sys::PICO_NOT_FOUND);
        assert_eq!(NOT_RESPONDING, sys::PICO_NOT_RESPONDING);
        assert_eq!(INVALID_HANDLE, sys::PICO_INVALID_HANDLE);
        assert_eq!(BUSY, sys::PICO_BUSY);
        assert_eq!(POWER_SUPPLY_CONNECTED, sys::PICO_POWER_SUPPLY_CONNECTED);
        assert_eq!(POWER_SUPPLY_NOT_CONNECTED, sys::PICO_POWER_SUPPLY_NOT_CONNECTED);
        assert_eq!(POWER_SUPPLY_REQUEST_INVALID, sys::PICO_POWER_SUPPLY_REQUEST_INVALID);
        assert_eq!(POWER_SUPPLY_UNDERVOLTAGE, sys::PICO_POWER_SUPPLY_UNDERVOLTAGE);
        assert_eq!(CAPTURING_DATA, sys::PICO_CAPTURING_DATA);
        assert_eq!(USB3_DEVICE_NON_USB3_PORT, sys::PICO_USB3_0_DEVICE_NON_USB3_0_PORT);
        assert_eq!(NOT_SUPPORTED_BY_THIS_DEVICE, sys::PICO_NOT_SUPPORTED_BY_THIS_DEVICE);

        assert_eq!(POWER_SUPPLY_CONNECTED, 0x119);
        assert_eq!(POWER_SUPPLY_NOT_CONNECTED, 0x11A);
        assert_eq!(POWER_SUPPLY_REQUEST_INVALID, 0x11B);
        assert_eq!(POWER_SUPPLY_UNDERVOLTAGE, 0x11C);
        assert_eq!(CAPTURING_DATA, 0x11D);
        assert_eq!(USB3_DEVICE_NON_USB3_PORT, 0x11E);
        assert_eq!(NOT_SUPPORTED_BY_THIS_DEVICE, 0x11F);
    }

    #[test]
    fn power_source_codes_are_open_warnings_but_not_success() {
        for code in [POWER_SUPPLY_CONNECTED, POWER_SUPPLY_NOT_CONNECTED, USB3_DEVICE_NON_USB3_PORT] {
            assert!(is_power_source_warning(code), "0x{code:X}");
            assert!(!is_success(code), "0x{code:X} means the call did not run");
        }
        for code in [CAPTURING_DATA, NOT_SUPPORTED_BY_THIS_DEVICE, POWER_SUPPLY_UNDERVOLTAGE] {
            assert!(!is_power_source_warning(code), "0x{code:X}");
            assert!(!is_success(code), "0x{code:X}");
        }
    }

    #[test]
    fn real_failures_are_not_success() {
        assert!(!is_success(NOT_FOUND));
        assert!(!is_success(BUSY));
        assert!(!is_success(INVALID_HANDLE));
    }

    #[test]
    fn a_status_error_keeps_its_code_and_says_what_it_was() {
        let error = PicoStatusError::new("ps5000aRunBlock", CAPTURING_DATA);
        assert_eq!(error.status, CAPTURING_DATA);
        let text = error.to_string();
        assert!(text.starts_with("ps5000aRunBlock: PICO_CAPTURING_DATA"), "{text}");
    }

    #[test]
    fn only_a_stale_handle_or_a_silent_unit_means_the_unit_is_gone() {
        assert!(PicoStatusError::new("x", NOT_RESPONDING).unit_is_gone());
        assert!(PicoStatusError::new("x", INVALID_HANDLE).unit_is_gone());
        assert!(!PicoStatusError::new("x", BUSY).unit_is_gone());
        assert!(!PicoStatusError::new("x", 0x0E).unit_is_gone());
    }

    #[test]
    fn known_codes_get_their_name() {
        assert!(describe(NOT_FOUND).contains("PICO_NOT_FOUND"));
        assert!(describe(0x0000000E).contains("PICO_INVALID_TIMEBASE"));
    }

    #[test]
    fn common_failures_say_what_to_do_about_it() {
        // These are the ones a user actually hits, so a bare code name is
        // not enough.
        assert!(describe(NOT_FOUND).contains("USB cable"));
        assert!(describe(0x0000000E).contains("channels are enabled"));
        assert!(describe(0x00000024).contains("stop it"));
    }

    #[test]
    fn unknown_codes_report_the_value_and_where_to_look() {
        let text = describe(0xDEADBEEF);
        assert!(text.contains("0xDEADBEEF"), "lost the code: {text}");
        assert!(text.contains("PicoStatus.h"), "no pointer to the header: {text}");
    }

    #[test]
    fn describe_never_panics_across_the_low_code_space() {
        // Cheap guard: the table is a match on literals and a missing arm
        // must fall through to the generic branch, not panic.
        for status in 0..0x200u32 {
            assert!(!describe(status).is_empty());
        }
    }
}

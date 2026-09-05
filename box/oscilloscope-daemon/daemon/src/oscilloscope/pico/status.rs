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
/// `PICO_POWER_SUPPLY_NOT_CONNECTED` -- USB-powered part needs the DC input.
pub const POWER_SUPPLY_NOT_CONNECTED: u32 = 0x0000011E;
/// `PICO_POWER_SUPPLY_CONNECTED`.
pub const POWER_SUPPLY_CONNECTED: u32 = 0x0000011D;
/// `PICO_USB3_0_DEVICE_NON_USB3_0_PORT`.
pub const USB3_DEVICE_NON_USB3_PORT: u32 = 0x0000011F;

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
        0x0000000C => "PICO_INVALID_HANDLE: the device handle is stale, which \
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
        POWER_SUPPLY_CONNECTED => "PICO_POWER_SUPPLY_CONNECTED",
        POWER_SUPPLY_NOT_CONNECTED => {
            "PICO_POWER_SUPPLY_NOT_CONNECTED: this model needs its DC supply \
             or a second USB lead for full performance"
        }
        USB3_DEVICE_NON_USB3_PORT => {
            "PICO_USB3_0_DEVICE_NON_USB3_0_PORT: a USB 3.0 scope is plugged \
             into a slower port, which limits its sample rate"
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

/// Whether a status is one the caller should treat as success.
///
/// `PICO_POWER_SUPPLY_NOT_CONNECTED` is the awkward one: `OpenUnit` returns
/// it as a *warning* on models that can run from USB alone, and the unit is
/// open and usable. Treating it as an error would refuse to talk to a
/// perfectly working scope that simply has no barrel jack attached.
pub fn is_success(status: u32) -> bool {
    matches!(
        status,
        OK | POWER_SUPPLY_NOT_CONNECTED | POWER_SUPPLY_CONNECTED | USB3_DEVICE_NON_USB3_PORT
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
    fn power_warnings_count_as_success() {
        // The unit is open and usable; see is_success.
        assert!(is_success(POWER_SUPPLY_NOT_CONNECTED));
        assert!(is_success(POWER_SUPPLY_CONNECTED));
        assert!(is_success(USB3_DEVICE_NON_USB3_PORT));
    }

    #[test]
    fn real_failures_are_not_success() {
        assert!(!is_success(NOT_FOUND));
        assert!(!is_success(BUSY));
        assert!(!is_success(0x0000000C)); // PICO_INVALID_HANDLE
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

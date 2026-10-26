// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Whether any PicoScope is on the USB bus, read from sysfs.
//!
//! The daemon retries its open every couple of seconds for as long as no
//! scope answers, and on most boxes none ever will. Every retry used to go
//! through the PicoTech drivers -- `ps2000_open_unit` plus an enumerate per
//! modern family -- and those calls keep memory on each failure: about 0.3
//! to 1.1 MB per attempt, measured on two boxes with no scope attached. That
//! grew the daemon to 15 GB in a day or two, until the kernel's OOM killer
//! took it, and the starved host lost its network on the way. Reading
//! `idVendor` files costs nothing and keeps the drivers out of it until a
//! unit is actually plugged in.

use std::path::Path;

/// PicoTech's USB vendor id, as sysfs writes it.
pub const VENDOR_ID: &str = "0ce9";

/// Where the kernel lists USB devices.
const SYSFS_USB_DEVICES: &str = "/sys/bus/usb/devices";

/// The reason given when the bus has no PicoScope on it.
pub const NONE_ON_THE_BUS: &str =
    "no PicoScope is plugged in (no USB device with vendor id 0ce9); check the USB connection";

/// `Some(true)` when a PicoScope is on the bus, `Some(false)` when none is,
/// and `None` when sysfs cannot be read, so the caller asks the driver
/// instead of concluding there is nothing there.
pub fn attached() -> Option<bool> {
    attached_in(Path::new(SYSFS_USB_DEVICES))
}

fn attached_in(devices: &Path) -> Option<bool> {
    let entries = std::fs::read_dir(devices).ok()?;
    Some(entries.flatten().any(|entry| {
        std::fs::read_to_string(entry.path().join("idVendor"))
            .is_ok_and(|vendor| vendor.trim().eq_ignore_ascii_case(VENDOR_ID))
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    /// A fake `/sys/bus/usb/devices` holding one device per vendor id, plus
    /// an interface entry with no `idVendor`, as the real directory has.
    fn bus(name: &str, vendors: &[&str]) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("pico-usb-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("1-0:1.0")).unwrap();
        for (i, vendor) in vendors.iter().enumerate() {
            let device = dir.join(format!("1-{}", i + 1));
            std::fs::create_dir_all(&device).unwrap();
            std::fs::write(device.join("idVendor"), format!("{vendor}\n")).unwrap();
        }
        dir
    }

    #[test]
    fn a_pico_among_other_devices_is_found() {
        let dir = bus("present", &["1d6b", "24ff", "0ce9"]);
        assert_eq!(attached_in(&dir), Some(true));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_bus_without_a_pico_says_so() {
        let dir = bus("absent", &["1d6b", "24ff", "0cd5"]);
        assert_eq!(attached_in(&dir), Some(false));
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn an_unreadable_bus_leaves_the_answer_to_the_driver() {
        let missing = std::env::temp_dir().join("pico-usb-no-such-bus");
        assert_eq!(attached_in(&missing), None);
    }
}

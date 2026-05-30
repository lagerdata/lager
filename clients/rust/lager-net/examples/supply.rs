// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Set a supply, enable it, and read back the measured output.
//!
//! Build + run on the box:
//!   cargo zigbuild --release --target x86_64-unknown-linux-musl --example supply
//!   lager rust target/x86_64-unknown-linux-musl/release/examples/supply --box PRD-1

use lager_net::Supply;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let supply = Supply::get("supply2");

    supply.set_voltage(3.3)?;
    supply.set_current(0.5)?;
    supply.enable()?;

    let m = supply.measure()?;
    println!("supply2: {:.3} V, {:.3} A, {:.3} W", m.voltage, m.current, m.power);

    supply.disable()?;
    Ok(())
}

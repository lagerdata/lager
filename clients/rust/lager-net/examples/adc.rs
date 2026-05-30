// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Read a voltage from an ADC net.
//!
//!   cargo zigbuild --release --target x86_64-unknown-linux-musl --example adc
//!   lager rust target/x86_64-unknown-linux-musl/release/examples/adc --box PRD-1

use lager_net::Adc;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let volts = Adc::get("adc1").read()?;
    println!("adc1: {volts:.4} V");
    Ok(())
}

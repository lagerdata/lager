// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Toggle a GPIO output and read an input back.
//!
//!   cargo zigbuild --release --target x86_64-unknown-linux-musl --example gpio
//!   lager rust target/x86_64-unknown-linux-musl/release/examples/gpio --box PRD-1

use lager_net::Gpio;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let gpio = Gpio::get("gpio1");

    gpio.set_high()?;
    println!("gpio1 driven high; reads back: {}", gpio.read()?);

    gpio.set_low()?;
    println!("gpio1 driven low; reads back: {}", gpio.read()?);

    Ok(())
}

// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! End-to-end smoke test of the lager-net crate against a live box.
//!
//! Exercises the LabJack-backed nets present on PRD-1 (adc/dac/gpio/i2c) using
//! the same net-name-based API as the Python SDK. No VISA instruments required.
//!
//! Build + run:
//!   cargo zigbuild --release --target x86_64-unknown-linux-musl --example demo
//!   lager rust target/x86_64-unknown-linux-musl/release/examples/demo --box PRD-1
//!
//! (Use `python -m cli.main rust ...` from the repo if your installed `lager`
//! predates the `rust` command.)

use lager_net::{Adc, Dac, Gpio, I2c, Net};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // --- Discover nets (GET /nets/list) ---------------------------------
    let nets = lager_net::list_nets()?;
    println!("Box has {} nets configured.\n", nets.len());

    // --- ADC: read a voltage (adc.input()) ------------------------------
    let v = Adc::get("adc1").read()?; // read() is the alias for input()
    println!("adc1.input()        = {v:.4} V");

    // --- DAC: write then read back --------------------------------------
    let dac = Dac::get("dac1");
    dac.output(1.25)?;
    let back = dac.get_voltage()?;
    println!("dac1.output(1.25)   -> get_voltage() = {back:.4} V");

    // --- GPIO: drive and read both ways ---------------------------------
    let gpio = Gpio::get("gpio1");
    gpio.set_high()?;
    println!("gpio1.set_high()    -> input() = {}", gpio.input()?);
    gpio.set_low()?;
    println!("gpio1.set_low()     -> input() = {}", gpio.input()?);

    // --- I2C: scan the bus ----------------------------------------------
    let found = I2c::get("i2c1").scan()?;
    println!("i2c1.scan()         = {found:?} ({} device(s))", found.len());

    // --- Generic escape hatch: same call, untyped -----------------------
    let raw = Net::get("adc1").command("input", serde_json::json!({}))?;
    println!("Net::command(input) = {raw}");

    println!("\nAll calls succeeded.");
    Ok(())
}

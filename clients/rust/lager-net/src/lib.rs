// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! `lager-net` — an instrument-agnostic, net-name-based client for the Lager box.
//!
//! It is the Rust analog of the Python `lager` SDK's net API, for binaries run
//! on the box via `lager rust`. A binary cannot `import lager`, so this crate
//! talks to the box over localhost HTTP (`POST /net/command`). You name a *net*,
//! not a driver or a SCPI command — the box resolves the instrument:
//!
//! ```no_run
//! use lager_net::{Supply, Adc, Gpio};
//!
//! # fn main() -> Result<(), Box<dyn std::error::Error>> {
//! Supply::get("supply2").set_voltage(3.3)?;
//! Supply::get("supply2").enable()?;
//! let v = Adc::get("adc1").read()?;          // ergonomic alias for input()
//! Gpio::get("gpio1").set_high()?;
//! # Ok(())
//! # }
//! ```
//!
//! Method names mirror the Python SDK (`adc.input()`, `gpio.output("high")`,
//! `supply.voltage()`), with ergonomic aliases (`read()`, `set_high()`) on top.
//!
//! ## Configuration
//! - `LAGER_BOX_HTTP` — box HTTP base (default `http://localhost:9000`).
//! - `LAGER_HARDWARE_HTTP` — low-level proxy base for [`client::invoke`]
//!   (default `http://localhost:8080`).
//!
//! ## Building for the box
//! Cross-compile a static musl binary, then run it via `lager rust`:
//! ```text
//! cargo zigbuild --release --target x86_64-unknown-linux-musl --example supply
//! lager rust target/x86_64-unknown-linux-musl/release/examples/supply --box PRD-1
//! ```

pub mod client;
pub mod roles;

pub use client::{invoke, list_nets, net_command, role_of, Error, Result};
pub use roles::{Adc, Battery, Dac, Gpio, I2c, Measurement, Net, Spi, Supply, Usb};

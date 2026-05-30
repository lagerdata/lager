// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Typed, net-name-based wrappers that read like the Python `lager` SDK.
//!
//! Each wrapper is a thin newtype over a net name. The primary method names
//! mirror the Python net API exactly (`adc.input()`, `gpio.output("high")`,
//! `supply.set_voltage(3.3)`, `supply.voltage()`); ergonomic aliases
//! (`adc.read()`, `gpio.set_high()`, `dac.set_voltage()`) are layered on top.
//!
//! Construction with `get()` is infallible (it just stores the name); the HTTP
//! round-trip — and therefore the `Result` — happens when you call a method.

use serde_json::json;

use crate::client::{self, net_command, Result};

/// An empty params object, for no-argument actions.
fn no_params() -> serde_json::Value {
    json!({})
}

// ================================ ADC =====================================

/// An analog-to-digital converter net. `Adc::get("adc1").input()`.
pub struct Adc {
    net: String,
}

impl Adc {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Read the input voltage (volts). Mirrors Python `adc.input()`.
    pub fn input(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "input", no_params())?)
    }

    /// Ergonomic alias for [`Adc::input`].
    pub fn read(&self) -> Result<f64> {
        self.input()
    }
}

// ================================ DAC =====================================

/// A digital-to-analog converter net. `Dac::get("dac1").output(2.5)`.
pub struct Dac {
    net: String,
}

impl Dac {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Set the output voltage (volts). Mirrors Python `dac.output(voltage)`.
    pub fn output(&self, voltage: f64) -> Result<()> {
        net_command(&self.net, "output", json!({ "voltage": voltage }))?;
        Ok(())
    }

    /// Read back the current output voltage. Mirrors Python `dac.get_voltage()`.
    pub fn get_voltage(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "get_voltage", no_params())?)
    }

    /// Ergonomic alias for [`Dac::output`].
    pub fn set_voltage(&self, voltage: f64) -> Result<()> {
        self.output(voltage)
    }

    /// Ergonomic alias for [`Dac::get_voltage`].
    pub fn read(&self) -> Result<f64> {
        self.get_voltage()
    }
}

// ================================ GPIO ====================================

/// A general-purpose digital I/O net. `Gpio::get("gpio1").set_high()`.
pub struct Gpio {
    net: String,
}

impl Gpio {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Read the pin level (0 or 1). Mirrors Python `gpio.input()`.
    pub fn input(&self) -> Result<i64> {
        client::as_i64(&net_command(&self.net, "input", no_params())?)
    }

    /// Set the pin level. Accepts `"high"`/`"low"`, `"on"`/`"off"`, `"1"`/`"0"`.
    /// Mirrors Python `gpio.output(level)`.
    pub fn output(&self, level: &str) -> Result<()> {
        net_command(&self.net, "output", json!({ "level": level }))?;
        Ok(())
    }

    /// Block until the pin reaches `level`, returning elapsed seconds.
    /// Mirrors Python `gpio.wait_for_level(level, timeout=...)`.
    pub fn wait_for_level(&self, level: &str, timeout: Option<f64>) -> Result<f64> {
        let mut params = json!({ "level": level });
        if let Some(t) = timeout {
            params["timeout"] = json!(t);
        }
        client::as_f64(&net_command(&self.net, "wait_for_level", params)?)
    }

    /// Ergonomic alias: drive the pin high.
    pub fn set_high(&self) -> Result<()> {
        self.output("high")
    }

    /// Ergonomic alias: drive the pin low.
    pub fn set_low(&self) -> Result<()> {
        self.output("low")
    }

    /// Ergonomic alias: drive the pin from a bool.
    pub fn set(&self, high: bool) -> Result<()> {
        self.output(if high { "high" } else { "low" })
    }

    /// Ergonomic alias for [`Gpio::input`] as a bool.
    pub fn read(&self) -> Result<bool> {
        Ok(self.input()? != 0)
    }
}

// ============================== POWER SUPPLY ==============================

/// A measured voltage/current/power snapshot from [`Supply::measure`].
#[derive(Debug, Clone, Copy)]
pub struct Measurement {
    pub voltage: f64,
    pub current: f64,
    pub power: f64,
}

/// A power-supply net. `Supply::get("supply2").set_voltage(3.3)`.
pub struct Supply {
    net: String,
}

impl Supply {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Set the output voltage setpoint (volts).
    pub fn set_voltage(&self, voltage: f64) -> Result<()> {
        net_command(&self.net, "set_voltage", json!({ "voltage": voltage }))?;
        Ok(())
    }

    /// Set the output current limit (amps).
    pub fn set_current(&self, current: f64) -> Result<()> {
        net_command(&self.net, "set_current", json!({ "current": current }))?;
        Ok(())
    }

    /// Measured output voltage (volts).
    pub fn voltage(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "voltage", no_params())?)
    }

    /// Measured output current (amps).
    pub fn current(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "current", no_params())?)
    }

    /// Measured output power (watts).
    pub fn power(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "power", no_params())?)
    }

    /// Enable the output.
    pub fn enable(&self) -> Result<()> {
        net_command(&self.net, "enable", no_params())?;
        Ok(())
    }

    /// Disable the output.
    pub fn disable(&self) -> Result<()> {
        net_command(&self.net, "disable", no_params())?;
        Ok(())
    }

    /// Set + enable over-voltage protection (volts).
    pub fn set_ovp(&self, voltage: f64) -> Result<()> {
        net_command(&self.net, "set_ovp", json!({ "voltage": voltage }))?;
        Ok(())
    }

    /// Set + enable over-current protection (amps).
    pub fn set_ocp(&self, current: f64) -> Result<()> {
        net_command(&self.net, "set_ocp", json!({ "current": current }))?;
        Ok(())
    }

    pub fn get_ovp_limit(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "get_ovp_limit", no_params())?)
    }

    pub fn get_ocp_limit(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "get_ocp_limit", no_params())?)
    }

    pub fn is_ovp(&self) -> Result<bool> {
        client::as_bool(&net_command(&self.net, "is_ovp", no_params())?)
    }

    pub fn is_ocp(&self) -> Result<bool> {
        client::as_bool(&net_command(&self.net, "is_ocp", no_params())?)
    }

    pub fn clear_ovp(&self) -> Result<()> {
        net_command(&self.net, "clear_ovp", no_params())?;
        Ok(())
    }

    pub fn clear_ocp(&self) -> Result<()> {
        net_command(&self.net, "clear_ocp", no_params())?;
        Ok(())
    }

    /// Convenience: read voltage, current, and power in one call each.
    pub fn measure(&self) -> Result<Measurement> {
        Ok(Measurement {
            voltage: self.voltage()?,
            current: self.current()?,
            power: self.power()?,
        })
    }
}

// ================================ BATTERY =================================

/// A battery-simulator net. `Battery::get("battery1").set_soc(80.0)`.
pub struct Battery {
    net: String,
}

impl Battery {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Set state of charge (percent). Mirrors Python `battery.soc(percent)`.
    pub fn set_soc(&self, percent: f64) -> Result<()> {
        net_command(&self.net, "soc", json!({ "percent": percent }))?;
        Ok(())
    }

    /// Read state of charge (percent). Mirrors Python `battery.get_soc()`.
    pub fn soc(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "get_soc", no_params())?)
    }

    /// Set open-circuit voltage (volts). Mirrors Python `battery.voc(voltage)`.
    pub fn set_voc(&self, voltage: f64) -> Result<()> {
        net_command(&self.net, "voc", json!({ "voltage": voltage }))?;
        Ok(())
    }

    /// Read open-circuit voltage (volts). Mirrors Python `battery.get_voc()`.
    pub fn voc(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "get_voc", no_params())?)
    }

    /// Measured terminal voltage (volts).
    pub fn terminal_voltage(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "terminal_voltage", no_params())?)
    }

    /// Measured current (amps).
    pub fn current(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "current", no_params())?)
    }

    /// Equivalent series resistance (ohms).
    pub fn esr(&self) -> Result<f64> {
        client::as_f64(&net_command(&self.net, "esr", no_params())?)
    }

    /// Set capacity (amp-hours). Mirrors Python `battery.set_capacity(capacity)`.
    pub fn set_capacity(&self, capacity: f64) -> Result<()> {
        net_command(&self.net, "set_capacity", json!({ "capacity": capacity }))?;
        Ok(())
    }

    /// Set current limit (amps). Mirrors Python `battery.current_limit(current)`.
    pub fn set_current_limit(&self, current: f64) -> Result<()> {
        net_command(&self.net, "current_limit", json!({ "current": current }))?;
        Ok(())
    }

    /// Enable the simulated output.
    pub fn enable(&self) -> Result<()> {
        net_command(&self.net, "enable", no_params())?;
        Ok(())
    }

    /// Disable the simulated output.
    pub fn disable(&self) -> Result<()> {
        net_command(&self.net, "disable", no_params())?;
        Ok(())
    }
}

// ================================== I2C ===================================

/// An I2C bus net. `I2c::get("i2c1").scan()`.
pub struct I2c {
    net: String,
}

impl I2c {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Configure the bus. Omitted (`None`) fields keep their stored value.
    pub fn config(&self, frequency_hz: Option<u32>, pull_ups: Option<bool>) -> Result<()> {
        let mut params = json!({});
        if let Some(f) = frequency_hz {
            params["frequency_hz"] = json!(f);
        }
        if let Some(p) = pull_ups {
            params["pull_ups"] = json!(p);
        }
        net_command(&self.net, "config", params)?;
        Ok(())
    }

    /// Scan for devices, returning the 7-bit addresses that ACK.
    pub fn scan(&self) -> Result<Vec<u8>> {
        client::as_u8_vec(&net_command(&self.net, "scan", no_params())?)
    }

    /// Read `num_bytes` from a device.
    pub fn read(&self, address: u8, num_bytes: usize) -> Result<Vec<u8>> {
        let params = json!({ "address": address, "num_bytes": num_bytes });
        client::as_u8_vec(&net_command(&self.net, "read", params)?)
    }

    /// Write bytes to a device.
    pub fn write(&self, address: u8, data: &[u8]) -> Result<()> {
        let params = json!({ "address": address, "data": data });
        net_command(&self.net, "write", params)?;
        Ok(())
    }

    /// Write then read (repeated start) in one transaction.
    pub fn write_read(&self, address: u8, data: &[u8], num_bytes: usize) -> Result<Vec<u8>> {
        let params = json!({ "address": address, "data": data, "num_bytes": num_bytes });
        client::as_u8_vec(&net_command(&self.net, "write_read", params)?)
    }
}

// ================================== SPI ===================================

/// An SPI bus net. `Spi::get("spi1").transfer(4, &[0x9F])`.
pub struct Spi {
    net: String,
}

impl Spi {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Configure the bus. Omitted (`None`) fields keep their stored value.
    pub fn config(
        &self,
        mode: Option<u8>,
        frequency_hz: Option<u32>,
        bit_order: Option<&str>,
        word_size: Option<u8>,
    ) -> Result<()> {
        let mut params = json!({});
        if let Some(m) = mode {
            params["mode"] = json!(m);
        }
        if let Some(f) = frequency_hz {
            params["frequency_hz"] = json!(f);
        }
        if let Some(b) = bit_order {
            params["bit_order"] = json!(b);
        }
        if let Some(w) = word_size {
            params["word_size"] = json!(w);
        }
        net_command(&self.net, "config", params)?;
        Ok(())
    }

    /// Read `n_words`, clocking out fill bytes.
    pub fn read(&self, n_words: usize) -> Result<Vec<u8>> {
        client::as_u8_vec(&net_command(&self.net, "read", json!({ "n_words": n_words }))?)
    }

    /// Full-duplex transfer; the response is the same length as `data`.
    pub fn read_write(&self, data: &[u8]) -> Result<Vec<u8>> {
        client::as_u8_vec(&net_command(&self.net, "read_write", json!({ "data": data }))?)
    }

    /// Transfer `n_words`, padding/truncating `data` to fit.
    pub fn transfer(&self, n_words: usize, data: &[u8]) -> Result<Vec<u8>> {
        let params = json!({ "n_words": n_words, "data": data });
        client::as_u8_vec(&net_command(&self.net, "transfer", params)?)
    }

    /// Write only (response discarded).
    pub fn write(&self, data: &[u8]) -> Result<()> {
        net_command(&self.net, "write", json!({ "data": data }))?;
        Ok(())
    }
}

// ================================== USB ===================================

/// A switchable USB hub port net. `Usb::get("usb1").enable()`.
pub struct Usb {
    net: String,
}

impl Usb {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    pub fn enable(&self) -> Result<()> {
        net_command(&self.net, "enable", no_params())?;
        Ok(())
    }

    pub fn disable(&self) -> Result<()> {
        net_command(&self.net, "disable", no_params())?;
        Ok(())
    }

    pub fn toggle(&self) -> Result<()> {
        net_command(&self.net, "toggle", no_params())?;
        Ok(())
    }
}

// ============================ generic escape hatch =========================

/// A generic net for roles/actions without a typed wrapper.
/// `Net::get("scope1").command("run", json!({}))`.
pub struct Net {
    net: String,
}

impl Net {
    pub fn get(net: &str) -> Self {
        Self { net: net.to_string() }
    }

    /// Call any allow-listed action with arbitrary params; returns raw `data`.
    pub fn command(&self, action: &str, params: serde_json::Value) -> Result<serde_json::Value> {
        net_command(&self.net, action, params)
    }
}

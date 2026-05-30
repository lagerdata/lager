// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Low-level transport for the Lager box role API.
//!
//! Everything talks to `POST /net/command` on the box's HTTP server (default
//! `http://localhost:9000`, overridable with `LAGER_BOX_HTTP`). The typed
//! wrappers in [`crate::roles`] are built on top of [`net_command`].

use serde_json::{json, Value};
use std::fmt;

/// Result alias used throughout the crate.
pub type Result<T> = std::result::Result<T, Error>;

/// An error talking to the box.
#[derive(Debug)]
pub enum Error {
    /// Could not reach the box / network or protocol failure.
    Transport(String),
    /// The box accepted the request but reported `success: false`.
    Box(String),
    /// The box's `data` field was not the shape we expected.
    Decode(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Transport(m) => write!(f, "transport error: {m}"),
            Error::Box(m) => write!(f, "box error: {m}"),
            Error::Decode(m) => write!(f, "decode error: {m}"),
        }
    }
}

impl std::error::Error for Error {}

fn box_http_base() -> String {
    std::env::var("LAGER_BOX_HTTP").unwrap_or_else(|_| "http://localhost:9000".to_string())
}

fn hardware_http_base() -> String {
    std::env::var("LAGER_HARDWARE_HTTP").unwrap_or_else(|_| "http://localhost:8080".to_string())
}

/// Invoke `action` on the net `netname`, returning the method's value (`data`).
///
/// `params` is a JSON object whose keys are the underlying Python method's
/// keyword-argument names (e.g. `{"voltage": 3.3}`). Pass `json!({})` for
/// no-argument actions. The box infers the net's role and runs
/// `Net.get(netname, role).<action>(**params)`.
pub fn net_command(netname: &str, action: &str, params: Value) -> Result<Value> {
    let url = format!("{}/net/command", box_http_base());
    let body = json!({ "netname": netname, "action": action, "params": params });

    match ureq::post(&url).send_json(body) {
        Ok(resp) => parse_envelope(netname, action, resp),
        // The box returns 4xx/5xx with a JSON `{success:false,error}` body.
        Err(ureq::Error::Status(code, resp)) => match resp.into_json::<Value>() {
            Ok(v) => {
                let msg = v["error"].as_str().unwrap_or("unknown box error");
                Err(Error::Box(format!("{netname} {action}: {msg}")))
            }
            Err(_) => Err(Error::Box(format!("{netname} {action}: HTTP {code}"))),
        },
        Err(e) => Err(Error::Transport(e.to_string())),
    }
}

fn parse_envelope(netname: &str, action: &str, resp: ureq::Response) -> Result<Value> {
    let v: Value = resp
        .into_json()
        .map_err(|e| Error::Transport(e.to_string()))?;
    if v["success"].as_bool() == Some(true) {
        Ok(v.get("data").cloned().unwrap_or(Value::Null))
    } else {
        let msg = v["error"].as_str().unwrap_or("unknown box error");
        Err(Error::Box(format!("{netname} {action}: {msg}")))
    }
}

/// List the box's saved nets (`GET /nets/list`). Each element is the raw
/// `net_info` object from `saved_nets.json` (name, role, instrument, …).
pub fn list_nets() -> Result<Vec<Value>> {
    let url = format!("{}/nets/list", box_http_base());
    let v: Value = match ureq::get(&url).call() {
        Ok(resp) => resp.into_json().map_err(|e| Error::Transport(e.to_string()))?,
        Err(e) => return Err(Error::Transport(e.to_string())),
    };
    match v {
        Value::Array(a) => Ok(a),
        other => Err(Error::Decode(format!("expected array from /nets/list, got {other}"))),
    }
}

/// Resolve the role string of a net via `/nets/list`, or `None` if not found.
pub fn role_of(netname: &str) -> Result<Option<String>> {
    Ok(list_nets()?.into_iter().find_map(|n| {
        if n.get("name").and_then(Value::as_str) == Some(netname) {
            n.get("role").and_then(Value::as_str).map(String::from)
        } else {
            None
        }
    }))
}

/// Generic escape hatch over the low-level driver proxy (`POST /invoke`,
/// default `http://localhost:8080`). Prefer [`net_command`] / the typed
/// wrappers; this is for driver-specific calls the role API does not cover.
pub fn invoke(device: &str, function: &str, args: Value, kwargs: Value, net_info: Value) -> Result<Value> {
    let url = format!("{}/invoke", hardware_http_base());
    let body = json!({
        "device": device, "function": function,
        "args": args, "kwargs": kwargs, "net_info": net_info,
    });
    match ureq::post(&url).send_json(body) {
        Ok(resp) => resp.into_json().map_err(|e| Error::Transport(e.to_string())),
        Err(ureq::Error::Status(code, resp)) => {
            let detail = resp.into_string().unwrap_or_default();
            Err(Error::Box(format!("invoke {device}.{function}: HTTP {code} {detail}")))
        }
        Err(e) => Err(Error::Transport(e.to_string())),
    }
}

// ----------------------------- value coercion ------------------------------

/// Coerce a `data` value to `f64`.
pub fn as_f64(v: &Value) -> Result<f64> {
    v.as_f64()
        .ok_or_else(|| Error::Decode(format!("expected a number, got {v}")))
}

/// Coerce a `data` value to `i64` (accepts an integral float).
pub fn as_i64(v: &Value) -> Result<i64> {
    v.as_i64()
        .or_else(|| v.as_f64().map(|f| f as i64))
        .ok_or_else(|| Error::Decode(format!("expected an integer, got {v}")))
}

/// Coerce a `data` value to `bool` (accepts 0/1 and numeric truthiness).
pub fn as_bool(v: &Value) -> Result<bool> {
    if let Some(b) = v.as_bool() {
        return Ok(b);
    }
    if let Some(i) = v.as_i64() {
        return Ok(i != 0);
    }
    Err(Error::Decode(format!("expected a bool, got {v}")))
}

/// Coerce a `data` array of byte-sized integers to `Vec<u8>`.
pub fn as_u8_vec(v: &Value) -> Result<Vec<u8>> {
    let arr = v
        .as_array()
        .ok_or_else(|| Error::Decode(format!("expected an array, got {v}")))?;
    arr.iter()
        .map(|x| {
            x.as_u64()
                .filter(|n| *n <= 0xFF)
                .map(|n| n as u8)
                .ok_or_else(|| Error::Decode(format!("expected a byte (0-255), got {x}")))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn f64_from_int_and_float() {
        assert_eq!(as_f64(&json!(3)).unwrap(), 3.0);
        assert_eq!(as_f64(&json!(3.3)).unwrap(), 3.3);
        assert!(as_f64(&json!("x")).is_err());
        assert!(as_f64(&Value::Null).is_err());
    }

    #[test]
    fn i64_accepts_integral_float() {
        assert_eq!(as_i64(&json!(1)).unwrap(), 1);
        assert_eq!(as_i64(&json!(1.0)).unwrap(), 1);
    }

    #[test]
    fn bool_accepts_numeric_truthiness() {
        assert!(as_bool(&json!(true)).unwrap());
        assert!(!as_bool(&json!(0)).unwrap());
        assert!(as_bool(&json!(1)).unwrap());
    }

    #[test]
    fn u8_vec_validates_byte_range() {
        assert_eq!(as_u8_vec(&json!([0x48, 0x50])).unwrap(), vec![0x48, 0x50]);
        assert!(as_u8_vec(&json!([256])).is_err());
        assert!(as_u8_vec(&json!("nope")).is_err());
    }
}

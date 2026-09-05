// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Oscilloscope daemon.
//!
//! Opens the attached PicoScope on a dedicated hardware thread and serves one
//! WebSocket endpoint carrying commands as JSON text and captures as binary
//! LSCP frames.

use anyhow::{Context, Result};
use daemon::oscilloscope::{pico, Oscilloscope, PicoScope2000};
use daemon::scope_thread;
use daemon::server::{self, ServerConfig};
use tracing_subscriber::EnvFilter;

fn init_tracing() {
    // Default to info. Per-poll driver detail sits at trace, which is what
    // keeps the log from growing by ~1 GB/day as it did when the readiness
    // check printed unconditionally.
    let filter = EnvFilter::try_from_env("LAGER_SCOPE_LOG")
        .unwrap_or_else(|_| EnvFilter::new("info"));

    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_target(false)
        .init();
}

/// Open whichever PicoScope is attached.
///
/// The legacy 2000-series driver is tried first because it is the one with a
/// full `Oscilloscope` implementation behind it. If no legacy unit answers,
/// the modern families are probed so the failure can name the instrument
/// that *is* plugged in -- previously any non-2204A scope produced the
/// legacy driver's "no unit found", which sent people looking at USB cables
/// when the real answer was that their model needs a different driver.
fn open_scope() -> Result<Box<dyn Oscilloscope>> {
    let legacy_error = match PicoScope2000::new() {
        Ok(scope) => return Ok(Box::new(scope)),
        Err(e) => e,
    };

    // No 2000-series unit on the legacy API, so look for one of the modern
    // families. Detection already opened and identified the unit, so the
    // handle is adopted rather than reopened -- reopening would race against
    // the close, and some units refuse a second open for a moment after.
    match pico::detect(None) {
        Ok(found) => {
            tracing::info!(
                model = %found.capabilities.model,
                serial = %found.capabilities.serial,
                family = found.family.as_str(),
                channels = found.capabilities.analog_channels,
                "opened PicoScope"
            );
            let scope =
                pico::PicoScopeModern::adopt(found.api, found.handle, found.capabilities)?;
            Ok(Box::new(scope))
        }
        // Nothing at all is attached: the legacy driver's message is the
        // useful one, since it names the library and search path.
        Err(_) => Err(legacy_error),
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    init_tracing();

    let config = ServerConfig::from_env();
    tracing::info!(
        tcp_port = ?config.tcp_port,
        socket = ?config.unix_socket,
        "starting oscilloscope daemon"
    );

    // Open before serving, so a missing scope is a clear startup failure
    // rather than a listener that accepts connections and then errors on
    // every command.
    let scope = scope_thread::spawn(open_scope).context("opening oscilloscope")?;

    server::serve(config, scope).await
}

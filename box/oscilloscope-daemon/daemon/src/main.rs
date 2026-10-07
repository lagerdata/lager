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
        Err(modern_error) => Err(open_failure(legacy_error, modern_error)),
    }
}

/// The error to report when neither driver opened a scope.
///
/// With no modern unit attached the legacy driver's message is the useful
/// one, since it names the library and search path. A modern unit that was
/// found and failed is reported as itself: answering "no 2000-series unit"
/// for a 5000-series scope that is attached but refusing to open sends
/// people to check a cable that is fine.
fn open_failure(legacy_error: anyhow::Error, modern_error: anyhow::Error) -> anyhow::Error {
    if modern_error.downcast_ref::<pico::NoUnitFound>().is_some() {
        legacy_error
    } else {
        modern_error
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

    // Serve whether or not a scope is attached. Opening first looked like the
    // careful order -- a missing scope became a clear startup failure instead
    // of a listener that errors on every command -- but the failure that
    // actually turned up was neither: a scope left mid-transfer by a daemon
    // killed while capturing blocks forever inside the driver's open call, so
    // the daemon never failed and never listened, and every client got a bare
    // "connection refused". Commands now answer with the reason, and the
    // hardware thread keeps trying to open in the background.
    let scope = scope_thread::spawn(open_scope).context("starting the oscilloscope thread")?;

    tokio::select! {
        result = server::serve(config, scope.clone()) => result,
        signal = stop_requested() => {
            tracing::info!(signal = signal?, "stopping: closing the oscilloscope");
            // A daemon that exits with the unit armed leaves it mid-transfer,
            // and the next open can block inside the driver until the unit is
            // replugged. Stopping it and closing it first is what avoids that.
            let closed = tokio::task::spawn_blocking(move || scope.shutdown(SHUTDOWN_WAIT))
                .await
                .unwrap_or(false);
            if !closed {
                tracing::warn!(
                    wait = ?SHUTDOWN_WAIT,
                    "the oscilloscope thread did not finish in time; exiting without it"
                );
            }
            Ok(())
        }
    }
}

/// Longest the scope gets to stop and close once the daemon is told to go,
/// inside the ten seconds `docker stop` allows between TERM and KILL.
const SHUTDOWN_WAIT: std::time::Duration = std::time::Duration::from_secs(5);

/// Wait for SIGTERM or SIGINT, and say which came.
async fn stop_requested() -> Result<&'static str> {
    use tokio::signal::unix::{signal, SignalKind};
    let mut terminate = signal(SignalKind::terminate()).context("listening for SIGTERM")?;
    let mut interrupt = signal(SignalKind::interrupt()).context("listening for SIGINT")?;
    Ok(tokio::select! {
        _ = terminate.recv() => "SIGTERM",
        _ = interrupt.recv() => "SIGINT",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn with_no_modern_unit_attached_the_legacy_error_stands() {
        let reported = open_failure(
            anyhow::anyhow!("ps2000: no unit found"),
            pico::NoUnitFound("no supported PicoScope was found".into()).into(),
        );
        assert_eq!(reported.to_string(), "ps2000: no unit found");
    }

    #[test]
    fn a_modern_unit_that_was_found_and_failed_is_reported_as_itself() {
        let reported = open_failure(
            anyhow::anyhow!("ps2000: no unit found"),
            anyhow::anyhow!("a PicoScope was found but could not be opened"),
        );
        assert!(reported.to_string().contains("could not be opened"), "{reported}");
    }
}

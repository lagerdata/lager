// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! The scope's screen read over SCPI, for a scope with no capture card or
//! whose HDMI output gives no picture.
//!
//! `:DISPlay:DATA?` returns the screen as a BMP, the same picture the HDMI
//! output carries at the panel's own resolution, so everything downstream
//! treats it as one more video frame. It is slow (a screenshot is about
//! 1.8 MB, and the scope takes a while to produce one), so the live view
//! updates a few times a second rather than at the card's rate.
//!
//! A thread reads screenshots back to back and keeps the newest, as the
//! capture card's thread does. It shares the scope's one SCPI connection,
//! taking it for each read and letting go between reads so that settings
//! and measurements get through.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::Result;

use super::scpi::{self, Scpi};
use super::video::{Encoding, FrameSource, Latest, RawFrame};

/// The spec that selects this source in place of a capture card.
pub const SPEC: &str = "screen";

/// How long one screenshot may take.
const READ_TIMEOUT: Duration = Duration::from_secs(15);
/// The gap between screenshots, in which commands waiting for the
/// connection take it.
const PAUSE: Duration = Duration::from_millis(20);

pub struct ScreenDump {
    latest: Arc<Latest>,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl ScreenDump {
    pub fn start(link: Arc<Mutex<Box<dyn Scpi>>>) -> Result<ScreenDump> {
        let latest = Arc::new(Latest::default());
        let stop = Arc::new(AtomicBool::new(false));
        let thread = std::thread::Builder::new()
            .name("rigol-screen".into())
            .spawn({
                let latest = latest.clone();
                let stop = stop.clone();
                move || run(&link, &latest, &stop)
            })?;
        Ok(ScreenDump {
            latest,
            stop,
            thread: Some(thread),
        })
    }
}

impl Drop for ScreenDump {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

/// Read one screenshot.
pub fn read(scpi: &mut dyn Scpi) -> Result<Vec<u8>> {
    scpi.set_timeout(READ_TIMEOUT);
    let image = scpi.query_block(":DISPlay:DATA?");
    scpi.set_timeout(scpi::DEFAULT_TIMEOUT);
    image
}

fn run(link: &Mutex<Box<dyn Scpi>>, latest: &Latest, stop: &AtomicBool) {
    let mut sequence = 0u64;
    let mut logged = false;
    while !stop.load(Ordering::Relaxed) {
        let begun = Instant::now();
        let image = {
            let mut scpi = link.lock().unwrap_or_else(|p| p.into_inner());
            read(scpi.as_mut())
        };
        let bytes = match image {
            Ok(bytes) => bytes,
            Err(e) => {
                latest.fail(format!("reading the scope's screen failed: {e:#}"));
                return;
            }
        };
        sequence += 1;
        if !logged {
            tracing::info!(
                bytes = bytes.len(),
                ms = begun.elapsed().as_millis() as u64,
                "reading the live view from the scope's screen over SCPI"
            );
            logged = true;
        }
        latest.store(RawFrame {
            encoding: Encoding::Bmp,
            width: 0,
            height: 0,
            bytes,
            sequence,
            captured_ns: crate::oscilloscope::monotonic_ns(),
        });
        std::thread::sleep(PAUSE);
    }
}

impl FrameSource for ScreenDump {
    fn newest(&self) -> Result<u64> {
        self.latest.newest()
    }

    fn next_after(&self, after: u64, timeout: Duration) -> Result<Option<RawFrame>> {
        self.latest.next_after(after, timeout)
    }

    fn describe(&self) -> String {
        "the scope's screen over SCPI".into()
    }

    /// A screenshot is taken while holding the connection, so one begun
    /// after a command has completed already shows its effect.
    fn settle_frames(&self) -> u64 {
        1
    }

    fn frame_wait(&self) -> Duration {
        READ_TIMEOUT + Duration::from_secs(1)
    }
}

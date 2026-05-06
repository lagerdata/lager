// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use crate::oscilloscope::Oscilloscope;
use anyhow::Result;
use crossbeam::queue::SegQueue;
use protocol::{CaptureMode, TriggeredCapture};
use std::sync::{Arc, Mutex};
use tokio::sync::mpsc;

const MAX_BROWSER_QUEUE_SIZE: usize = 50;
const STREAMING_WAIT_MS: u64 = 10;

/// Handle oscilloscope data streaming with proper buffer management
pub async fn handle_scope_streaming(
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
    tx_database: mpsc::Sender<TriggeredCapture>,
    browser_queue: Arc<SegQueue<TriggeredCapture>>,
) -> Result<()> {
    println!("Starting oscilloscope data streaming");

    loop {
        let data = {
            let trigger_position = {
                let scope = oscilloscope.lock().unwrap();
                scope.get_trigger_position()?
            }; // scope is dropped here, releasing the lock
            let capture_mode = {
                let scope = oscilloscope.lock().unwrap();
                scope.get_capture_mode().unwrap_or(CaptureMode::Normal)
            };
            let mut scope = oscilloscope.lock().unwrap();
            if scope.is_ready()? {
                let result = scope.get_triggered_data().ok();
                if matches!(capture_mode, CaptureMode::Single) {
                    _ = scope.stop_triggered_capture();
                } else {
                    _ = scope.start_triggered_capture(trigger_position).ok();
                }
                result
            } else {
                None
            }
        }; // MutexGuard dropped here

        if let Some(data) = data {
            // Send to database (reliable, large buffer - 50,000)
            if let Err(e) = tx_database.send(data.clone()).await {
                eprintln!("Failed to send data to database: {}", e);
            }

            // Send to browser (unreliable, larger buffer - ~50, drop oldest when full)
            // Check if browser queue is full and drop oldest if needed
            if browser_queue.len() >= MAX_BROWSER_QUEUE_SIZE {
                browser_queue.pop(); // Drop one oldest item
            }
            browser_queue.push(data);
        }

        // Small delay to prevent overwhelming the system
        tokio::time::sleep(tokio::time::Duration::from_millis(STREAMING_WAIT_MS)).await;
    }
}

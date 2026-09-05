// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! The single owner of the oscilloscope.
//!
//! Every FFI call happens on one dedicated OS thread. Async tasks talk to it
//! by sending a [`ScopeRequest`] with a oneshot reply channel, so no tokio
//! worker ever blocks inside a driver call.
//!
//! This replaces an `Arc<Mutex<Box<dyn Oscilloscope>>>` that was locked
//! directly inside async handlers, with `ps2000_get_values` running on a
//! 32k buffer while the lock was held. The measured symptom was command RTT
//! p99 degrading from 1.91 ms to 16.01 ms when a second channel was enabled,
//! while p50 barely moved -- contention, not load. See `tests/bench/BASELINE.md`.
//!
//! Acquisition also lives here, as a single loop rather than one per client.
//! Captures are published as `Arc<CaptureFrame>` over a broadcast channel, so
//! N subscribers share one allocation and a slow subscriber drops frames
//! instead of stalling the hardware.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::time::{Duration, Instant};

use anyhow::Result;
use protocol::{
    CaptureFrame, CaptureMode, ChannelId, Coupling, ScopeCapabilities, TriggerSlope,
};
use tokio::sync::{broadcast, mpsc, oneshot};

use crate::oscilloscope::Oscilloscope;
use protocol::{Measurement, MeasurementSet};

/// Broadcast depth. Enough to absorb a brief consumer stall without the
/// hardware loop noticing; beyond this a lagging subscriber drops frames,
/// which for live waveforms is the correct outcome.
const CAPTURE_BROADCAST_DEPTH: usize = 16;

/// Bound on queued control commands. Small because commands are answered in
/// microseconds; a deep queue here would only hide a problem.
const COMMAND_QUEUE_DEPTH: usize = 64;

#[derive(Debug)]
pub enum ScopeRequest {
    EnableChannel(ChannelId),
    DisableChannel(ChannelId),
    IsChannelEnabled(ChannelId),
    SetVoltsPerDiv(ChannelId, f64),
    GetVoltsPerDiv(ChannelId),
    SetVoltsOffset(ChannelId, f64),
    GetVoltsOffset(ChannelId),
    SetCoupling(ChannelId, Coupling),
    GetCoupling(ChannelId),
    SetAttenuation(ChannelId, f64),
    GetAttenuation(ChannelId),
    SetTimePerDiv(f64),
    GetTimePerDiv,
    SetTimeOffset(f64),
    GetTimeOffset,
    SetTriggerLevel(f64),
    GetTriggerLevel,
    SetTriggerSource(ChannelId),
    GetTriggerSource,
    SetTriggerSlope(TriggerSlope),
    GetTriggerSlope,
    SetCaptureMode(CaptureMode),
    GetCaptureMode,
    GetSampleRate,
    GetMemoryDepth,
    GetBandwidth,
    GetChannelCount,
    GetCapabilities,
    StartAcquisition(f64),
    StopAcquisition,
    ForceTrigger,
    IsReady,
    /// One-shot capture outside the streaming loop.
    GetTriggeredData,
    Measure {
        channel: ChannelId,
        which: Measurement,
    },
    MeasureAll {
        channel: ChannelId,
    },
}

#[derive(Debug)]
pub enum ScopeReply {
    Ok,
    Bool(bool),
    Float(f64),
    Usize(usize),
    Channel(ChannelId),
    Coupling(Coupling),
    Slope(TriggerSlope),
    Mode(CaptureMode),
    Capabilities(Box<ScopeCapabilities>),
    Capture(Arc<CaptureFrame>),
    Measurement(f64),
    Measurements(Box<MeasurementSet>),
    Error(String),
}

impl ScopeReply {
    pub fn error(message: impl std::fmt::Display) -> Self {
        ScopeReply::Error(message.to_string())
    }
}

type Envelope = (ScopeRequest, oneshot::Sender<ScopeReply>);

/// Handle used by async code to reach the hardware thread.
#[derive(Clone)]
pub struct ScopeHandle {
    commands: mpsc::Sender<Envelope>,
    captures: broadcast::Sender<Arc<CaptureFrame>>,
    acquiring: Arc<AtomicBool>,
    capture_count: Arc<AtomicU64>,
}

impl ScopeHandle {
    /// Send a request and await its reply. Returns an error reply rather than
    /// panicking if the hardware thread has gone away, so a driver crash
    /// surfaces to the client instead of taking the connection down.
    pub async fn request(&self, request: ScopeRequest) -> ScopeReply {
        let (tx, rx) = oneshot::channel();
        if self.commands.send((request, tx)).await.is_err() {
            return ScopeReply::error("oscilloscope thread is not running");
        }
        match rx.await {
            Ok(reply) => reply,
            Err(_) => ScopeReply::error("oscilloscope thread dropped the request"),
        }
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Arc<CaptureFrame>> {
        self.captures.subscribe()
    }

    pub fn is_acquiring(&self) -> bool {
        self.acquiring.load(Ordering::Relaxed)
    }

    pub fn capture_count(&self) -> u64 {
        self.capture_count.load(Ordering::Relaxed)
    }

    pub fn subscriber_count(&self) -> usize {
        self.captures.receiver_count()
    }
}

/// Start the hardware thread. The scope is opened on that thread so the
/// driver's handle is never touched from anywhere else.
pub fn spawn<F>(open: F) -> Result<ScopeHandle>
where
    F: FnOnce() -> Result<Box<dyn Oscilloscope>> + Send + 'static,
{
    let (command_tx, command_rx) = mpsc::channel::<Envelope>(COMMAND_QUEUE_DEPTH);
    let (capture_tx, _) = broadcast::channel(CAPTURE_BROADCAST_DEPTH);
    let acquiring = Arc::new(AtomicBool::new(false));
    let capture_count = Arc::new(AtomicU64::new(0));

    // Report the open result back before the thread enters its loop, so a
    // failure to find a scope is a startup error rather than a silent hang.
    let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<()>>();

    let thread_captures = capture_tx.clone();
    let thread_acquiring = acquiring.clone();
    let thread_count = capture_count.clone();

    std::thread::Builder::new()
        .name("scope-hw".into())
        .spawn(move || {
            let scope = match open() {
                Ok(scope) => {
                    let _ = ready_tx.send(Ok(()));
                    scope
                }
                Err(e) => {
                    let _ = ready_tx.send(Err(e));
                    return;
                }
            };
            run(
                scope,
                command_rx,
                thread_captures,
                thread_acquiring,
                thread_count,
            );
        })?;

    ready_rx.recv()??;

    Ok(ScopeHandle {
        commands: command_tx,
        captures: capture_tx,
        acquiring,
        capture_count,
    })
}

fn run(
    mut scope: Box<dyn Oscilloscope>,
    mut commands: mpsc::Receiver<Envelope>,
    captures: broadcast::Sender<Arc<CaptureFrame>>,
    acquiring: Arc<AtomicBool>,
    capture_count: Arc<AtomicU64>,
) {
    let mut sequence: u64 = 0;
    // When idle this blocks on the command channel and consumes nothing.
    // The old design polled every 10 ms whether or not anything was
    // acquiring, which is what produced ~1 GB/day of readiness logging.
    let mut next_poll: Option<Instant> = None;

    loop {
        let acquiring_now = acquiring.load(Ordering::Relaxed);

        let envelope = if acquiring_now {
            let wait = next_poll
                .map(|at| at.saturating_duration_since(Instant::now()))
                .unwrap_or(Duration::ZERO);
            match commands.try_recv() {
                Ok(envelope) => Some(envelope),
                Err(mpsc::error::TryRecvError::Disconnected) => break,
                Err(mpsc::error::TryRecvError::Empty) => {
                    if !wait.is_zero() {
                        std::thread::sleep(wait.min(Duration::from_millis(5)));
                    }
                    None
                }
            }
        } else {
            match commands.blocking_recv() {
                Some(envelope) => Some(envelope),
                None => break,
            }
        };

        if let Some((request, reply_to)) = envelope {
            let reply = handle(&mut scope, request, &acquiring);
            // A client that hung up mid-request is normal, not an error.
            let _ = reply_to.send(reply);
            continue;
        }

        if !acquiring_now {
            continue;
        }

        match scope.is_ready() {
            Ok(true) => match scope.get_triggered_data() {
                Ok(mut frame) => {
                    sequence += 1;
                    frame.seq = sequence;
                    capture_count.fetch_add(1, Ordering::Relaxed);

                    // Send failure only means nobody is subscribed. The
                    // acquisition loop keeps running so a reconnecting
                    // client sees live data immediately.
                    let _ = captures.send(Arc::new(frame));

                    let mode = scope.get_capture_mode().unwrap_or(CaptureMode::Normal);
                    if mode == CaptureMode::Single {
                        acquiring.store(false, Ordering::Relaxed);
                        let _ = scope.stop_triggered_capture();
                    } else {
                        let position = scope.get_trigger_position().unwrap_or(50.0);
                        if let Err(e) = scope.start_triggered_capture(position) {
                            tracing::warn!(error = %e, "failed to rearm capture");
                            acquiring.store(false, Ordering::Relaxed);
                        }
                    }
                    next_poll = Some(Instant::now());
                }
                Err(e) => {
                    tracing::warn!(error = %e, "capture read failed");
                    next_poll = Some(Instant::now() + Duration::from_millis(10));
                }
            },
            Ok(false) => {
                next_poll = Some(Instant::now() + scope.suggested_poll_interval());
            }
            Err(e) => {
                tracing::warn!(error = %e, "readiness check failed, stopping acquisition");
                acquiring.store(false, Ordering::Relaxed);
            }
        }
    }

    tracing::info!("oscilloscope thread shutting down");
    let _ = scope.stop_triggered_capture();
}

fn handle(
    scope: &mut Box<dyn Oscilloscope>,
    request: ScopeRequest,
    acquiring: &AtomicBool,
) -> ScopeReply {
    /// Map `Result<T>` onto a reply, turning driver errors into a message
    /// the client actually receives rather than a log line it never sees.
    macro_rules! reply {
        ($expr:expr, $ok:expr) => {
            match $expr {
                Ok(value) => {
                    let _ = value;
                    $ok
                }
                Err(e) => ScopeReply::error(e),
            }
        };
    }
    macro_rules! reply_value {
        ($expr:expr, $variant:path) => {
            match $expr {
                Ok(value) => $variant(value),
                Err(e) => ScopeReply::error(e),
            }
        };
    }

    match request {
        ScopeRequest::EnableChannel(c) => reply!(scope.enable_channel(c), ScopeReply::Ok),
        ScopeRequest::DisableChannel(c) => reply!(scope.disable_channel(c), ScopeReply::Ok),
        ScopeRequest::IsChannelEnabled(c) => {
            reply_value!(scope.is_channel_enabled(c), ScopeReply::Bool)
        }
        ScopeRequest::SetVoltsPerDiv(c, v) => {
            reply!(scope.set_volts_per_div(c, v), ScopeReply::Ok)
        }
        ScopeRequest::GetVoltsPerDiv(c) => {
            reply_value!(scope.get_volts_per_div(c), ScopeReply::Float)
        }
        ScopeRequest::SetVoltsOffset(c, v) => {
            reply!(scope.set_volts_offset(c, v), ScopeReply::Ok)
        }
        ScopeRequest::GetVoltsOffset(c) => {
            reply_value!(scope.get_volts_offset(c), ScopeReply::Float)
        }
        ScopeRequest::SetCoupling(c, k) => reply!(scope.set_coupling(c, k), ScopeReply::Ok),
        ScopeRequest::GetCoupling(c) => reply_value!(scope.get_coupling(c), ScopeReply::Coupling),
        ScopeRequest::SetAttenuation(c, a) => {
            reply!(scope.set_attenuation(c, a), ScopeReply::Ok)
        }
        ScopeRequest::GetAttenuation(c) => {
            reply_value!(scope.get_attenuation(c), ScopeReply::Float)
        }
        ScopeRequest::SetTimePerDiv(t) => reply!(scope.set_time_per_div(t), ScopeReply::Ok),
        ScopeRequest::GetTimePerDiv => reply_value!(scope.get_time_per_div(), ScopeReply::Float),
        ScopeRequest::SetTimeOffset(t) => reply!(scope.set_time_offset(t), ScopeReply::Ok),
        ScopeRequest::GetTimeOffset => reply_value!(scope.get_time_offset(), ScopeReply::Float),
        ScopeRequest::SetTriggerLevel(l) => reply!(scope.set_trigger_level(l), ScopeReply::Ok),
        ScopeRequest::GetTriggerLevel => {
            reply_value!(scope.get_trigger_level(), ScopeReply::Float)
        }
        ScopeRequest::SetTriggerSource(c) => {
            reply!(scope.set_trigger_source(c), ScopeReply::Ok)
        }
        ScopeRequest::GetTriggerSource => {
            reply_value!(scope.get_trigger_source(), ScopeReply::Channel)
        }
        ScopeRequest::SetTriggerSlope(s) => reply!(scope.set_trigger_slope(s), ScopeReply::Ok),
        ScopeRequest::GetTriggerSlope => {
            reply_value!(scope.get_trigger_slope(), ScopeReply::Slope)
        }
        ScopeRequest::SetCaptureMode(m) => reply!(scope.set_capture_mode(m), ScopeReply::Ok),
        ScopeRequest::GetCaptureMode => reply_value!(scope.get_capture_mode(), ScopeReply::Mode),
        ScopeRequest::GetSampleRate => reply_value!(scope.get_sample_rate(), ScopeReply::Float),
        ScopeRequest::GetMemoryDepth => reply_value!(scope.get_memory_depth(), ScopeReply::Usize),
        ScopeRequest::GetBandwidth => reply_value!(scope.get_bandwidth(), ScopeReply::Float),
        ScopeRequest::GetChannelCount => {
            reply_value!(scope.get_channel_count(), ScopeReply::Usize)
        }
        ScopeRequest::GetCapabilities => match scope.capabilities() {
            Ok(capabilities) => ScopeReply::Capabilities(Box::new(capabilities)),
            Err(e) => ScopeReply::error(e),
        },
        ScopeRequest::StartAcquisition(position) => match scope.start_triggered_capture(position) {
            Ok(()) => {
                acquiring.store(true, Ordering::Relaxed);
                ScopeReply::Ok
            }
            Err(e) => ScopeReply::error(e),
        },
        ScopeRequest::StopAcquisition => {
            acquiring.store(false, Ordering::Relaxed);
            reply!(scope.stop_triggered_capture(), ScopeReply::Ok)
        }
        ScopeRequest::ForceTrigger => reply!(scope.force_trigger(), ScopeReply::Ok),
        ScopeRequest::IsReady => reply_value!(scope.is_ready(), ScopeReply::Bool),
        ScopeRequest::GetTriggeredData => match scope.get_triggered_data() {
            Ok(frame) => ScopeReply::Capture(Arc::new(frame)),
            Err(e) => ScopeReply::error(e),
        },
        ScopeRequest::Measure { channel, which } => {
            match measure_now(scope, channel).and_then(|set| {
                set.get(which)
                    .ok_or_else(|| anyhow::anyhow!(
                        "{:?} needs at least one full cycle in the capture",
                        which
                    ))
            }) {
                Ok(value) => ScopeReply::Measurement(value),
                Err(e) => ScopeReply::error(e),
            }
        }
        ScopeRequest::MeasureAll { channel } => match measure_now(scope, channel) {
            Ok(set) => ScopeReply::Measurements(Box::new(set)),
            Err(e) => ScopeReply::error(e),
        },
    }
}

/// Measure against the most recent capture the hardware can give us.
fn measure_now(scope: &mut Box<dyn Oscilloscope>, channel: ChannelId) -> Result<MeasurementSet> {
    let frame = scope.get_triggered_data()?;
    let index = frame
        .channels
        .iter()
        .position(|c| c.channel == channel)
        .ok_or_else(|| {
            anyhow::anyhow!("channel {channel} is not enabled, so there is nothing to measure")
        })?;
    protocol::measure_channel(&frame, index)
        .ok_or_else(|| anyhow::anyhow!("capture held no samples for channel {channel}"))
}

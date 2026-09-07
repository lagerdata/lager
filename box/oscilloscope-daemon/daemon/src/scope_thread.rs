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

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
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
        let message = message.to_string();
        // Every driver error routed to a client passes through here, and
        // until now none of them were written down: a session where the
        // scope failed `run_block` at every timebase until the daemon was
        // restarted left a 266 MB log with not one warning in it, so there
        // was nothing to investigate afterwards. Driver errors are supposed
        // to be rare; if they are not, that is the thing worth seeing.
        tracing::warn!(error = %message, "oscilloscope command failed");
        ScopeReply::Error(message)
    }
}

type Envelope = (ScopeRequest, oneshot::Sender<ScopeReply>);

/// How far the hardware thread has got with opening the scope.
///
/// Needed because the thread cannot answer anything while it is inside the
/// driver's open call, and that call is not guaranteed to return: a scope left
/// mid-transfer by a daemon that was killed while capturing blocks there
/// indefinitely. Commands are answered from this instead of being queued for a
/// thread that may never read them.
#[derive(Clone, Debug)]
enum OpenState {
    /// Inside the driver's open call.
    Opening,
    Open,
    /// The last attempt failed, with this reason. The thread keeps trying.
    Failed(String),
}

/// Handle used by async code to reach the hardware thread.
#[derive(Clone)]
pub struct ScopeHandle {
    commands: mpsc::Sender<Envelope>,
    captures: broadcast::Sender<Arc<CaptureFrame>>,
    acquiring: Arc<AtomicBool>,
    capture_count: Arc<AtomicU64>,
    open_state: Arc<Mutex<OpenState>>,
}

impl ScopeHandle {
    /// Send a request and await its reply. Returns an error reply rather than
    /// panicking if the hardware thread has gone away, so a driver crash
    /// surfaces to the client instead of taking the connection down.
    pub async fn request(&self, request: ScopeRequest) -> ScopeReply {
        // Answered here when there is no scope to answer with. The hardware
        // thread does not read its channel until the open returns, so a
        // request sent while it is still in there would wait on a reply that
        // may never come -- and the alternative the daemon used to take, not
        // serving at all until the scope opened, gave every client a bare
        // "connection refused" with nothing to say why.
        match &*self.open_state.lock().expect("open state mutex poisoned") {
            OpenState::Open => {}
            OpenState::Opening => {
                return ScopeReply::error(
                    "still opening the oscilloscope; if this does not clear, \
                     the scope is likely wedged and needs reconnecting",
                );
            }
            OpenState::Failed(reason) => {
                return ScopeReply::error(format!("no oscilloscope available: {reason}"));
            }
        }

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

/// How long to wait for the scope before serving without it.
///
/// A healthy open takes well under a second, so this is slack for a slow
/// enumeration rather than a real budget. Waiting at all keeps the common case
/// honest: the startup log still says whether a scope was found.
const OPEN_TIMEOUT: Duration = Duration::from_secs(10);

/// How long to leave between attempts to open the scope.
///
/// The daemon used to exit when the open failed and be restarted by the
/// supervisor every two seconds, and that loop is what made attaching a scope
/// to a running box work at all. Retrying here keeps that, now that a failed
/// open no longer takes the process down.
const OPEN_RETRY_INTERVAL: Duration = Duration::from_secs(2);

/// How often to repeat the reason a scope could not be opened.
///
/// Every attempt would be a line every two seconds for as long as a box has no
/// scope attached, which is the normal state of most boxes. This daemon has
/// filled a 266 MB log once already.
const OPEN_COMPLAINT_INTERVAL: Duration = Duration::from_secs(300);

/// Start the hardware thread. The scope is opened on that thread so the
/// driver's handle is never touched from anywhere else.
///
/// Returns as soon as the scope opens, or after [`OPEN_TIMEOUT`], whichever
/// comes first -- the handle is returned either way. Serving without a scope
/// beats not serving: an open that never returns, which is what a scope left
/// mid-transfer does, otherwise means no listener, and a client cannot tell
/// "connection refused" from a daemon that was never installed. Commands now
/// answer with the reason instead, and the thread keeps trying to open, so a
/// scope that is reconnected is picked up without anyone restarting anything.
pub fn spawn<F>(open: F) -> Result<ScopeHandle>
where
    F: FnMut() -> Result<Box<dyn Oscilloscope>> + Send + 'static,
{
    let (command_tx, command_rx) = mpsc::channel::<Envelope>(COMMAND_QUEUE_DEPTH);
    let (capture_tx, _) = broadcast::channel(CAPTURE_BROADCAST_DEPTH);
    let acquiring = Arc::new(AtomicBool::new(false));
    let capture_count = Arc::new(AtomicU64::new(0));
    let open_state = Arc::new(Mutex::new(OpenState::Opening));

    // Carries the first outcome, so startup can log what happened rather than
    // leaving it to be discovered by a failing command later.
    let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<()>>();

    let thread_captures = capture_tx.clone();
    let thread_acquiring = acquiring.clone();
    let thread_count = capture_count.clone();
    let thread_state = open_state.clone();

    let mut open = open;
    std::thread::Builder::new()
        .name("scope-hw".into())
        .spawn(move || {
            let mut attempt: u64 = 0;
            let mut complained_at: Option<Instant> = None;
            let scope = loop {
                attempt += 1;
                match open() {
                    Ok(scope) => {
                        *thread_state.lock().expect("open state mutex poisoned") =
                            OpenState::Open;
                        let _ = ready_tx.send(Ok(()));
                        if attempt > 1 {
                            tracing::info!(attempt, "oscilloscope opened");
                        }
                        break scope;
                    }
                    Err(e) => {
                        // `{:#}` so the causes come with it: the useful part
                        // is usually the innermost one, naming the library
                        // and the search path.
                        let reason = format!("{e:#}");
                        let due = complained_at
                            .is_none_or(|at| at.elapsed() >= OPEN_COMPLAINT_INTERVAL);
                        if due {
                            tracing::error!(
                                attempt,
                                error = %reason,
                                retry_in = ?OPEN_RETRY_INTERVAL,
                                "cannot open the oscilloscope; commands will \
                                 answer with this until it can be opened"
                            );
                            complained_at = Some(Instant::now());
                        }
                        *thread_state.lock().expect("open state mutex poisoned") =
                            OpenState::Failed(reason);
                        let _ = ready_tx.send(Err(e));
                        std::thread::sleep(OPEN_RETRY_INTERVAL);
                    }
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

    match ready_rx.recv_timeout(OPEN_TIMEOUT) {
        Ok(Ok(())) => tracing::info!("oscilloscope opened"),
        Ok(Err(e)) => tracing::warn!(
            error = %format!("{e:#}"),
            "no oscilloscope yet; serving anyway and retrying"
        ),
        // Still inside the driver's open call. Nothing to report but the wait
        // itself, and that is worth reporting: it is the shape of a wedged
        // scope, which no amount of waiting fixes.
        Err(_) => tracing::warn!(
            waited = ?OPEN_TIMEOUT,
            "the oscilloscope has not opened yet; serving anyway"
        ),
    }

    Ok(ScopeHandle {
        commands: command_tx,
        captures: capture_tx,
        acquiring,
        capture_count,
        open_state,
    })
}

fn run(
    mut scope: Box<dyn Oscilloscope>,
    mut commands: mpsc::Receiver<Envelope>,
    captures: broadcast::Sender<Arc<CaptureFrame>>,
    acquiring: Arc<AtomicBool>,
    capture_count: Arc<AtomicU64>,
) {
    let mut state = LoopState {
        sequence: 0,
        captured_since_arm: false,
        last_frame: None,
    };
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
            let reply = handle(&mut scope, request, &acquiring, &mut state);
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
                    state.sequence += 1;
                    frame.seq = state.sequence;
                    state.captured_since_arm = true;
                    capture_count.fetch_add(1, Ordering::Relaxed);

                    // Kept as well as sent, so a command that needs samples
                    // has the ones just published rather than re-reading a
                    // device that is already collecting the next block.
                    let frame = Arc::new(frame);
                    state.last_frame = Some(frame.clone());

                    // Send failure only means nobody is subscribed. The
                    // acquisition loop keeps running so a reconnecting
                    // client sees live data immediately.
                    let _ = captures.send(frame);

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

/// State the acquisition loop owns but command handling also has to see.
struct LoopState {
    /// Capture sequence number, shared between the acquisition loop and the
    /// one-shot `GetTriggeredData` path. One counter, not two, because a
    /// client uses the number to tell one capture from the next and to match
    /// a binary frame to the reply that announced it; two counters would hand
    /// out the same number twice.
    sequence: u64,
    /// Whether a capture has been produced since the last arm.
    ///
    /// While the loop is running it polls the driver's readiness flag and
    /// consumes the capture immediately, so a client asking the driver
    /// directly loses that race and sees "not ready" almost every time even
    /// though captures are streaming. This is the readiness a client can
    /// actually observe.
    captured_since_arm: bool,
    /// The last capture the loop published, while it is acquiring.
    ///
    /// Reading the driver on demand does not work during acquisition. The
    /// loop consumes each capture and re-arms straight away, so the device is
    /// always mid-block when a command arrives, and a read-out at that point
    /// hands back the block already latched -- the same samples on every
    /// call. Measurements were frozen for exactly this reason: the panel
    /// polled, the driver answered, and the answer never changed.
    ///
    /// Serving from here instead is also the right answer rather than merely
    /// a working one: these samples are the frame the client was just sent,
    /// so a measurement or a cursor reading describes the trace on screen.
    last_frame: Option<Arc<CaptureFrame>>,
}

fn handle(
    scope: &mut Box<dyn Oscilloscope>,
    request: ScopeRequest,
    acquiring: &AtomicBool,
    state: &mut LoopState,
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
                // Arming discards whatever was captured before, so readiness
                // starts over: otherwise a client polling after a re-arm gets
                // "ready" from the previous acquisition and reads a stale
                // capture as though it were the new one.
                state.captured_since_arm = false;
                // And the published frame goes with it. A re-arm is how a new
                // timebase, range or window takes effect, so keeping the last
                // one servable would answer questions about the new settings
                // with samples taken under the old ones.
                state.last_frame = None;
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
        // Answered from what the loop has seen, not from the driver, whenever
        // the loop is the one watching the driver. Asking the hardware here
        // while it is acquiring is a race the caller cannot win.
        ScopeRequest::IsReady => {
            if state.captured_since_arm {
                ScopeReply::Bool(true)
            } else if acquiring.load(Ordering::Relaxed) {
                ScopeReply::Bool(false)
            } else {
                reply_value!(scope.is_ready(), ScopeReply::Bool)
            }
        }
        ScopeRequest::GetTriggeredData => match samples_now(scope, state) {
            Ok(frame) => ScopeReply::Capture(frame),
            Err(e) => ScopeReply::error(e),
        },
        ScopeRequest::Measure { channel, which } => {
            match measure_now(scope, state, channel).and_then(|set| {
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
        ScopeRequest::MeasureAll { channel } => match measure_now(scope, state, channel) {
            Ok(set) => ScopeReply::Measurements(Box::new(set)),
            Err(e) => ScopeReply::error(e),
        },
    }
}

/// The most recent samples, from wherever they can actually be had.
///
/// While the loop is acquiring, that is the frame it last published: the
/// device is mid-block, and reading it out then returns the block already
/// latched, which is how a polling client ends up with the same samples on
/// every call.
///
/// Idle, there is no published frame to prefer and the latched block really
/// is the last capture, so the driver is read directly. That is also what
/// makes a one-shot work: arm, then ask.
fn samples_now(
    scope: &mut Box<dyn Oscilloscope>,
    state: &mut LoopState,
) -> Result<Arc<CaptureFrame>> {
    if let Some(frame) = state.last_frame.clone() {
        return Ok(frame);
    }
    let mut frame = scope.get_triggered_data()?;
    state.sequence += 1;
    frame.seq = state.sequence;
    Ok(Arc::new(frame))
}

/// Measure against the most recent capture, so the numbers follow the signal.
fn measure_now(
    scope: &mut Box<dyn Oscilloscope>,
    state: &mut LoopState,
    channel: ChannelId,
) -> Result<MeasurementSet> {
    let frame = samples_now(scope, state)?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use protocol::ChannelFrame;
    use std::sync::atomic::AtomicUsize;

    /// A scope that implements only the capture surface these tests drive.
    ///
    /// The two real drivers both need the PicoScope SDK to build, so the
    /// interaction between the acquisition loop and command handling had no
    /// test at all -- which is where both bugs below lived. Everything
    /// outside capture panics rather than returning a plausible zero, so a
    /// test that grows into untested territory says so instead of passing on
    /// a fabricated value.
    struct FakeScope {
        /// Shared with the harness, which flips readiness and counts
        /// read-outs from outside the boxed trait object.
        ready: Arc<AtomicBool>,
        captures_read: Arc<AtomicUsize>,
    }

    impl FakeScope {
        fn new() -> Self {
            FakeScope {
                ready: Arc::new(AtomicBool::new(false)),
                captures_read: Arc::new(AtomicUsize::new(0)),
            }
        }
    }

    /// Generate the trait methods these tests never call.
    macro_rules! unused {
        ($($name:ident($($arg:ty),*) -> $ret:ty;)*) => {
            $(fn $name(&self $(, _: $arg)*) -> anyhow::Result<$ret> {
                unimplemented!(concat!(stringify!($name), " is not part of the capture path"))
            })*
        };
    }
    macro_rules! unused_mut {
        ($($name:ident($($arg:ty),*);)*) => {
            $(fn $name(&mut self $(, _: $arg)*) -> anyhow::Result<()> {
                unimplemented!(concat!(stringify!($name), " is not part of the capture path"))
            })*
        };
    }

    impl Oscilloscope for FakeScope {
        fn start_triggered_capture(&mut self, _position: f64) -> anyhow::Result<()> {
            Ok(())
        }

        fn stop_triggered_capture(&mut self) -> anyhow::Result<()> {
            Ok(())
        }

        fn is_ready(&self) -> anyhow::Result<bool> {
            Ok(self.ready.load(Ordering::Relaxed))
        }

        fn get_triggered_data(&self) -> anyhow::Result<CaptureFrame> {
            // Counted and folded into the samples, so a test can tell one
            // read-out from the next. A real driver hands back the same
            // latched block until something re-arms, which is the bug these
            // tests exist for -- here every read differs, so serving a stale
            // frame is what shows up rather than what hides.
            let nth = self.captures_read.fetch_add(1, Ordering::Relaxed) as i16;
            Ok(CaptureFrame {
                // Zero, as a real driver leaves it: the sequence number is
                // the daemon's to assign, not the hardware's.
                seq: 0,
                capture_mono_ns: 0,
                sample_interval_ns: 1.0,
                pre_trigger_samples: 1,
                post_trigger_samples: 1,
                samples_per_channel: 2,
                resolution_bits: 8,
                overflow_mask: 0,
                flags: 0,
                channels: vec![ChannelFrame {
                    channel: ChannelId::Alphabetic('A'),
                    range_code: 0,
                    coupling: Coupling::DC,
                    scale_v_per_count: 1.0,
                    offset_v: 0.0,
                }],
                samples: vec![nth, nth + 10],
            })
        }

        fn get_capture_mode(&self) -> anyhow::Result<CaptureMode> {
            Ok(CaptureMode::Normal)
        }

        fn get_trigger_position(&self) -> anyhow::Result<f64> {
            Ok(50.0)
        }

        unused! {
            is_channel_enabled(ChannelId) -> bool;
            get_volts_per_div(ChannelId) -> f64;
            get_volts_offset(ChannelId) -> f64;
            get_coupling(ChannelId) -> Coupling;
            get_attenuation(ChannelId) -> f64;
            get_trigger_level() -> f64;
            get_time_per_div() -> f64;
            get_time_offset() -> f64;
            get_trigger_source() -> ChannelId;
            get_trigger_slope() -> TriggerSlope;
            get_cursor_position(crate::oscilloscope::Cursor) -> f64;
            measure_horizontal_cursor_delta() -> f64;
            measure_vertical_cursor_delta() -> f64;
            measure_duty_cycle(ChannelId) -> f64;
            measure_frequency(ChannelId) -> f64;
            measure_period(ChannelId) -> f64;
            measure_rms(ChannelId) -> f64;
            measure_peak_to_peak(ChannelId) -> f64;
            measure_average(ChannelId) -> f64;
            measure_min(ChannelId) -> f64;
            get_data(ChannelId) -> Vec<f64>;
            get_sample_rate() -> f64;
            get_memory_depth() -> usize;
            get_bandwidth() -> f64;
            get_channel_count() -> usize;
            capabilities() -> ScopeCapabilities;
        }

        unused_mut! {
            enable_channel(ChannelId);
            disable_channel(ChannelId);
            set_volts_per_div(ChannelId, f64);
            set_volts_offset(ChannelId, f64);
            set_coupling(ChannelId, Coupling);
            set_attenuation(ChannelId, f64);
            set_trigger_level(f64);
            set_time_per_div(f64);
            set_time_offset(f64);
            set_trigger_source(ChannelId);
            set_trigger_slope(TriggerSlope);
            set_capture_mode(CaptureMode);
            set_cursor_position(crate::oscilloscope::Cursor);
            force_trigger();
        }
    }

    /// `handle` plus the state the loop would own, for driving requests
    /// without standing up a thread.
    struct Harness {
        scope: Box<dyn Oscilloscope>,
        ready: Arc<AtomicBool>,
        captures_read: Arc<AtomicUsize>,
        acquiring: AtomicBool,
        state: LoopState,
    }

    impl Harness {
        fn new() -> Self {
            let fake = FakeScope::new();
            let ready = fake.ready.clone();
            let captures_read = fake.captures_read.clone();
            Harness {
                scope: Box::new(fake),
                ready,
                captures_read,
                acquiring: AtomicBool::new(false),
                state: LoopState {
                    sequence: 0,
                    captured_since_arm: false,
                    last_frame: None,
                },
            }
        }

        fn send(&mut self, request: ScopeRequest) -> ScopeReply {
            handle(&mut self.scope, request, &self.acquiring, &mut self.state)
        }

        /// How many times the driver has been asked for samples.
        fn captures_read(&self) -> usize {
            self.captures_read.load(Ordering::Relaxed)
        }

        fn is_ready(&mut self) -> bool {
            match self.send(ScopeRequest::IsReady) {
                ScopeReply::Bool(ready) => ready,
                other => panic!("expected a bool reply, got {other:?}"),
            }
        }

        fn capture_seq(&mut self) -> u64 {
            match self.send(ScopeRequest::GetTriggeredData) {
                ScopeReply::Capture(frame) => frame.seq,
                other => panic!("expected a capture reply, got {other:?}"),
            }
        }
    }

    #[test]
    fn one_shot_captures_get_distinct_sequence_numbers() {
        // The streaming path stamped a sequence number and the one-shot path
        // did not, so every `GetTriggeredData` came back as seq 0. A client
        // uses the number to tell one capture from the next, and to match a
        // binary frame against the reply that announced it, so a constant
        // zero silently makes three different captures look like one.
        let mut harness = Harness::new();

        assert_eq!(harness.capture_seq(), 1);
        assert_eq!(harness.capture_seq(), 2);
        assert_eq!(harness.capture_seq(), 3);
    }

    #[test]
    fn one_shot_and_streaming_captures_share_one_counter() {
        // Two counters would hand the same number to a streamed frame and a
        // requested one, which is worse than no numbering: the client would
        // accept the wrong frame as its reply.
        let mut harness = Harness::new();

        assert_eq!(harness.capture_seq(), 1);
        // Stand in for the acquisition loop publishing a frame.
        harness.state.sequence += 1;
        assert_eq!(harness.capture_seq(), 3);
    }

    #[test]
    fn readiness_while_acquiring_reports_what_the_loop_has_seen() {
        // While acquiring, the loop polls the driver and consumes the capture
        // immediately, so asking the driver here loses the race and reports
        // "not ready" while captures are streaming past. Report the loop's
        // own view instead.
        let mut harness = Harness::new();
        assert!(matches!(
            harness.send(ScopeRequest::StartAcquisition(50.0)),
            ScopeReply::Ok
        ));

        assert!(!harness.is_ready(), "nothing captured since arming yet");

        harness.state.captured_since_arm = true;
        assert!(harness.is_ready(), "the loop published a capture");
    }

    /// Stand in for the loop having just published a capture.
    ///
    /// `amplitude` sets the spread rather than an offset, so the published
    /// frames differ in Vpp -- shifting both samples equally would leave
    /// every peak-to-peak reading identical and the test could not tell a
    /// fresh capture from a stale one.
    fn publish(harness: &mut Harness, amplitude: i16) {
        harness.state.sequence += 1;
        harness.state.last_frame = Some(Arc::new(CaptureFrame {
            seq: harness.state.sequence,
            capture_mono_ns: 0,
            sample_interval_ns: 1.0,
            pre_trigger_samples: 1,
            post_trigger_samples: 1,
            samples_per_channel: 2,
            resolution_bits: 8,
            overflow_mask: 0,
            flags: 0,
            channels: vec![ChannelFrame {
                channel: ChannelId::Alphabetic('A'),
                range_code: 0,
                coupling: Coupling::DC,
                scale_v_per_count: 1.0,
                offset_v: 0.0,
            }],
            samples: vec![-amplitude, amplitude],
        }));
    }

    fn measure_vpp(harness: &mut Harness) -> f64 {
        match harness.send(ScopeRequest::MeasureAll {
            channel: ChannelId::Alphabetic('A'),
        }) {
            ScopeReply::Measurements(set) => set
                .get(Measurement::Vpp)
                .expect("the capture has samples, so Vpp resolves"),
            other => panic!("expected measurements, got {other:?}"),
        }
    }

    #[test]
    fn measurements_follow_the_captures_the_loop_publishes() {
        // The frozen-readout bug. The loop consumes each capture and re-arms
        // straight away, so the device is mid-block whenever a command lands
        // and reading it out then returns the block already latched. A panel
        // polling twice a second got the same numbers forever, while the
        // trace beside it updated fine.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));

        publish(&mut harness, 5);
        let first = measure_vpp(&mut harness);

        publish(&mut harness, 40);
        let second = measure_vpp(&mut harness);

        assert_eq!(first, 10.0, "Vpp of a -5..5 capture");
        assert_eq!(
            second, 80.0,
            "the measurement did not follow the newly published capture"
        );
    }

    #[test]
    fn measuring_while_acquiring_does_not_read_the_device() {
        // Not just an optimisation: a read-out mid-block is precisely what
        // returns stale samples, so the fix is to not do it.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));
        publish(&mut harness, 7);

        let before = harness.captures_read();
        measure_vpp(&mut harness);
        harness.send(ScopeRequest::GetTriggeredData);

        assert_eq!(
            harness.captures_read(),
            before,
            "went back to the device while the loop was acquiring"
        );
    }

    #[test]
    fn a_capture_request_gets_the_frame_the_client_was_just_sent() {
        // So a cursor reading or a measurement describes the trace on screen
        // rather than some other moment.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));
        publish(&mut harness, 3);
        let published = harness.state.last_frame.clone().unwrap();

        match harness.send(ScopeRequest::GetTriggeredData) {
            ScopeReply::Capture(frame) => {
                assert_eq!(frame.samples, published.samples);
                assert_eq!(frame.seq, published.seq);
            }
            other => panic!("expected a capture, got {other:?}"),
        }
    }

    #[test]
    fn an_idle_scope_is_read_from_the_device() {
        // With no loop running there is nothing published to prefer, and the
        // latched block really is the last capture. This is what makes a
        // one-shot work: arm, stop, then ask.
        let mut harness = Harness::new();

        let before = harness.captures_read();
        match harness.send(ScopeRequest::GetTriggeredData) {
            ScopeReply::Capture(_) => {}
            other => panic!("expected a capture, got {other:?}"),
        }
        assert_eq!(
            harness.captures_read(),
            before + 1,
            "an idle scope has to be read from the device"
        );
    }

    #[test]
    fn rearming_discards_the_published_frame() {
        // A re-arm is how a new timebase or range takes effect, so answering
        // from the frame captured under the old settings would describe a
        // window the caller has already changed.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));
        publish(&mut harness, 5);
        assert!(harness.state.last_frame.is_some());

        harness.send(ScopeRequest::StartAcquisition(25.0));

        assert!(
            harness.state.last_frame.is_none(),
            "a capture taken under the previous settings is still servable"
        );
        // And with nothing published, the next request goes to the device.
        let before = harness.captures_read();
        harness.send(ScopeRequest::GetTriggeredData);
        assert_eq!(harness.captures_read(), before + 1);
    }

    #[test]
    fn a_stopped_scope_still_reports_its_last_capture() {
        // As a stopped bench scope does: the numbers on screen describe the
        // frame still being displayed.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));
        publish(&mut harness, 9);
        harness.send(ScopeRequest::StopAcquisition);

        let before = harness.captures_read();
        measure_vpp(&mut harness);
        assert_eq!(
            harness.captures_read(),
            before,
            "re-read the device for a capture it had already published"
        );
    }

    #[test]
    fn readiness_falls_back_to_the_driver_when_idle() {
        // With no acquisition running there is no loop to race, so the
        // driver's own flag is the truthful answer -- it covers a capture
        // armed outside the loop.
        let mut harness = Harness::new();

        assert!(!harness.is_ready());

        harness.ready.store(true, Ordering::Relaxed);
        assert!(harness.is_ready());
    }

    #[test]
    fn rearming_clears_readiness() {
        // Otherwise a client that arms, polls, and reads gets the capture
        // from before the re-arm and treats it as the new one.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));
        harness.state.captured_since_arm = true;
        assert!(harness.is_ready());

        harness.send(ScopeRequest::StartAcquisition(50.0));
        assert!(!harness.is_ready(), "a re-arm discards the previous capture");
    }

    #[test]
    fn a_completed_single_shot_still_reports_ready() {
        // Single-shot stops the loop after publishing, so `acquiring` is
        // false while the capture is genuinely there to read. Answering from
        // the driver at that point would report "not ready" on a stopped
        // unit and the caller would wait forever for a capture it already has.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));
        harness.state.captured_since_arm = true;
        harness.acquiring.store(false, Ordering::Relaxed);

        assert!(harness.is_ready());
    }

    /// A handle in `state`, whose command channel is already closed.
    ///
    /// Closed deliberately: if a request ever reaches the channel these tests
    /// get "thread is not running" rather than the state's own message, so
    /// they cannot pass by accident when the short-circuit is removed.
    fn handle_in(state: OpenState) -> ScopeHandle {
        let (commands, rx) = mpsc::channel::<Envelope>(1);
        drop(rx);
        let (captures, _) = broadcast::channel(1);
        ScopeHandle {
            commands,
            captures,
            acquiring: Arc::new(AtomicBool::new(false)),
            capture_count: Arc::new(AtomicU64::new(0)),
            open_state: Arc::new(Mutex::new(state)),
        }
    }

    fn error_from(reply: ScopeReply) -> String {
        match reply {
            ScopeReply::Error(message) => message,
            other => panic!("expected an error reply, got {other:?}"),
        }
    }

    #[tokio::test]
    async fn a_command_that_arrives_before_the_scope_opens_says_so() {
        // The hardware thread is inside the driver's open call and is not
        // reading its channel, so this cannot be answered by sending it on.
        // It used to not arise, because the daemon did not listen until the
        // scope was open -- which turned a wedged scope into "connection
        // refused", indistinguishable from no daemon at all.
        let handle = handle_in(OpenState::Opening);

        let message = error_from(handle.request(ScopeRequest::IsReady).await);
        assert!(
            message.contains("still opening"),
            "should say the open is in progress, got {message:?}"
        );
        assert!(
            message.contains("reconnect"),
            "and what to do if it stays that way, got {message:?}"
        );
    }

    #[tokio::test]
    async fn a_command_with_no_scope_attached_carries_the_reason() {
        // The reason is the whole value of answering: "no PicoScope
        // 2000-series device found" tells someone to check the cable, where a
        // refused connection tells them nothing.
        let handle = handle_in(OpenState::Failed(
            "no PicoScope 2000-series device found".into(),
        ));

        let message = error_from(handle.request(ScopeRequest::GetCapabilities).await);
        assert!(
            message.contains("no PicoScope 2000-series device found"),
            "the driver's reason should survive, got {message:?}"
        );
    }

    #[test]
    fn a_scope_that_cannot_be_opened_still_leaves_a_daemon_to_talk_to() {
        // The point of the change: spawn hands back a usable handle instead
        // of an error that takes the process down before it listens.
        let handle = spawn(|| anyhow::bail!("nothing plugged in"))
            .expect("spawn should succeed without a scope");

        let message = error_from(
            tokio::runtime::Runtime::new()
                .unwrap()
                .block_on(handle.request(ScopeRequest::IsReady)),
        );
        assert!(
            message.contains("nothing plugged in"),
            "got {message:?}"
        );
    }

    #[test]
    fn the_open_is_retried_so_a_scope_attached_later_is_picked_up() {
        // Exiting on a failed open and being restarted by the supervisor is
        // what used to make this work. Now that the daemon stays up, the
        // retry has to live here instead -- without it, plugging a scope into
        // a running box would need someone to restart the daemon by hand.
        let attempts = Arc::new(AtomicUsize::new(0));
        let counter = attempts.clone();

        let handle = spawn(move || {
            // Fails once, then succeeds, so this covers both the retry and
            // the transition out of Failed.
            if counter.fetch_add(1, Ordering::SeqCst) == 0 {
                anyhow::bail!("not yet");
            }
            Ok(Box::new(FakeScope::new()) as Box<dyn Oscilloscope>)
        })
        .expect("spawn should succeed");

        // Just past one interval, so this tracks the constant rather than a
        // number copied out of it.
        std::thread::sleep(OPEN_RETRY_INTERVAL + Duration::from_millis(750));

        assert!(
            attempts.load(Ordering::SeqCst) >= 2,
            "the open should have been retried"
        );
        assert!(
            matches!(
                *handle.open_state.lock().unwrap(),
                OpenState::Open
            ),
            "the second attempt succeeded, so the scope should be open"
        );
    }
}

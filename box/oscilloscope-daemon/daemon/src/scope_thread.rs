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
//! Captures are published as [`Published`] over a broadcast channel, so N
//! subscribers share one allocation and one encoding, and a slow subscriber
//! drops frames instead of stalling the hardware.
//!
//! The loop also owns what happens between the hardware and a published
//! frame: whether a capture triggered, averaging across captures, trigger
//! holdoff, and roll mode, where a slow timebase streams continuously
//! instead of capturing blocks. After every change it publishes a
//! [`ScopeState`], so a client can keep its controls in step with the
//! instrument whoever changed it.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::Result;
use protocol::lscp::{FLAG_ENVELOPE, FLAG_STREAMING, FLAG_TRIGGERED, NO_SAMPLE};
use protocol::state::{merge_display, MAX_AVERAGE_COUNT, ROLL_THRESHOLD_S_PER_DIV};
use protocol::{
    AcquisitionMode, AcquisitionState, CaptureFrame, CaptureMode, ChannelId, ChannelState,
    Coupling, RollMode, ScopeCapabilities, ScopeState, TimebaseState, TriggerSlope, TriggerState,
};
use tokio::sync::{broadcast, mpsc, oneshot, watch};

use crate::oscilloscope::pico;
use crate::oscilloscope::pico::ps2000::monotonic_ns;
use crate::oscilloscope::{Oscilloscope, RollInfo, RollPlan, RollSink};
use protocol::{Measurement, MeasurementSet};

/// Broadcast depth. Enough to absorb a brief consumer stall without the
/// hardware loop noticing; beyond this a lagging subscriber drops frames,
/// which for live waveforms is the correct outcome.
const CAPTURE_BROADCAST_DEPTH: usize = 16;

/// Bound on queued control commands. Small because commands are answered in
/// microseconds; a deep queue here would only hide a problem.
const COMMAND_QUEUE_DEPTH: usize = 64;

/// Averaging count until someone asks for another. Sixteen captures take
/// noise down by a factor of four, and settle in a fraction of a second at
/// the capture rates this family runs at.
const DEFAULT_AVERAGE_COUNT: u32 = 16;

/// Longest trigger holdoff accepted. A holdoff is measured in the time
/// between features of a signal, and ten seconds of screen doing nothing is
/// indistinguishable from a hang.
const MAX_HOLDOFF_S: f64 = 10.0;

/// Largest display state accepted, serialized. What the pages keep there --
/// cursors, persistence, zoom, math -- is a few hundred bytes.
const MAX_DISPLAY_BYTES: usize = 64 * 1024;

/// Minimum/maximum pairs across a rolling screen: about one per pixel of a
/// wide plot, so the trace is as detailed as the display can show.
const ROLL_COLUMNS: usize = 2000;

/// History a rolling screen carries before its left edge, at the least.
///
/// A page draws the screen behind live, by the lateness of the stream it has
/// seen, so that it never scrolls past the newest data -- and a screen drawn
/// that far behind needs that much from before the screen to fill its left
/// edge. Without it the left 6% of a 100 ms/div screen was blank.
const ROLL_HISTORY: Duration = Duration::from_millis(250);

/// How often roll mode collects what has streamed in. Well under a frame, so
/// each published screen is at most this stale.
const ROLL_POLL_INTERVAL: Duration = Duration::from_millis(4);

/// Interval between published roll frames: 120 a second, enough for a 120 Hz
/// display, which is where an uneven scroll shows most. Each is the whole
/// screen, so a client that takes fewer loses nothing but frames.
const ROLL_FRAME_INTERVAL: Duration = Duration::from_nanos(8_333_333);

/// A capture as the loop published it: the frame, and its LSCP encoding,
/// made once and shared by every connection that sends it.
#[derive(Clone, Debug)]
pub struct Published {
    pub frame: Arc<CaptureFrame>,
    pub encoded: bytes::Bytes,
}

impl Published {
    pub fn new(frame: Arc<CaptureFrame>) -> Self {
        let encoded = bytes::Bytes::from(frame.encode());
        Published { frame, encoded }
    }
}

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
    GetState,
    SetDisplay(serde_json::Value),
    GetDisplay,
    SetAcquisition(AcquisitionMode, Option<u32>),
    GetAcquisition,
    SetHoldoff(f64),
    GetHoldoff,
    SetRoll(RollMode),
    GetRoll,
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
    State(Box<ScopeState>),
    Display(serde_json::Value),
    Acquisition(AcquisitionMode, u32),
    Roll(RollMode, bool),
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

pub(crate) type Envelope = (ScopeRequest, oneshot::Sender<ScopeReply>);

/// What the hardware thread reads off its queue.
pub(crate) enum Message {
    Request(Envelope),
    /// The daemon is stopping: end any acquisition and close the scope.
    Shutdown,
}

/// How far the hardware thread has got with opening the scope.
///
/// Needed because the thread cannot answer anything while it is inside the
/// driver's open call, and that call is not guaranteed to return: a scope left
/// mid-transfer by a daemon that was killed while capturing blocks there
/// indefinitely. Commands are answered from this instead of being queued for a
/// thread that may never read them.
#[derive(Clone, Debug)]
enum OpenState {
    /// Inside the driver's open call, since `since`. `last_failure` is why
    /// the attempt before this one failed, if one did.
    Opening {
        since: Instant,
        last_failure: Option<String>,
    },
    Open,
    /// The last attempt failed, with this reason. The thread keeps trying.
    Failed(String),
}

impl OpenState {
    fn opening(last_failure: Option<String>) -> Self {
        OpenState::Opening {
            since: Instant::now(),
            last_failure,
        }
    }

    /// What a request is told when there is no scope to answer it.
    fn refusal(&self) -> Option<String> {
        match self {
            OpenState::Open => None,
            // A retry, which takes moments unless the scope is wedged. Until
            // it is clearly stuck, why the last attempt failed is still the
            // news: on a box with no scope attached, that is every request.
            OpenState::Opening {
                since,
                last_failure: Some(reason),
            } if since.elapsed() < OPEN_TIMEOUT => {
                Some(format!("no oscilloscope available: {reason}"))
            }
            OpenState::Opening { .. } => Some(
                "still opening the oscilloscope; if this does not clear, \
                 the scope is likely wedged and needs reconnecting"
                    .to_string(),
            ),
            OpenState::Failed(reason) => Some(format!("no oscilloscope available: {reason}")),
        }
    }
}

/// Lock a mutex whose holder may have panicked. Everything these guard is
/// written whole, so what a poisoned lock holds is still valid, and a panic
/// that already cost the scope should not cost every request after it.
fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// Handle used by async code to reach the hardware thread.
#[derive(Clone)]
pub struct ScopeHandle {
    commands: mpsc::Sender<Message>,
    captures: broadcast::Sender<Published>,
    acquiring: Arc<AtomicBool>,
    capture_count: Arc<AtomicU64>,
    open_state: Arc<Mutex<OpenState>>,
    state: watch::Receiver<Arc<ScopeState>>,
    /// Set by `shutdown`, for a hardware thread that is not reading its
    /// queue: one between attempts to open the scope.
    stopping: Arc<AtomicBool>,
    thread: Arc<Mutex<Option<std::thread::JoinHandle<()>>>>,
}

/// An open handle with no hardware thread behind it.
#[cfg(test)]
pub(crate) struct Detached {
    pub handle: ScopeHandle,
    /// What the handle was asked, for the test to answer when it likes, or
    /// not at all.
    pub requests: mpsc::Receiver<Message>,
    /// Publishes to the handle's subscribers.
    pub captures: broadcast::Sender<Published>,
}

#[cfg(test)]
impl ScopeHandle {
    pub(crate) fn detached() -> Detached {
        let (commands, requests) = mpsc::channel(COMMAND_QUEUE_DEPTH);
        let (captures, _) = broadcast::channel(CAPTURE_BROADCAST_DEPTH);
        let (_state_tx, state) = watch::channel(Arc::new(unopened_state()));
        let handle = ScopeHandle {
            commands,
            captures: captures.clone(),
            acquiring: Arc::new(AtomicBool::new(false)),
            capture_count: Arc::new(AtomicU64::new(0)),
            open_state: Arc::new(Mutex::new(OpenState::Open)),
            state,
            stopping: Arc::new(AtomicBool::new(false)),
            thread: Arc::new(Mutex::new(None)),
        };
        Detached { handle, requests, captures }
    }
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
        //
        // The guard goes before the reply is built: building one logs.
        let refusal = lock(&self.open_state).refusal();
        if let Some(message) = refusal {
            return ScopeReply::error(message);
        }

        let (tx, rx) = oneshot::channel();
        if self.commands.send(Message::Request((request, tx))).await.is_err() {
            return ScopeReply::error("oscilloscope thread is not running");
        }
        match rx.await {
            Ok(reply) => reply,
            Err(_) => ScopeReply::error("oscilloscope thread dropped the request"),
        }
    }

    /// Stop the hardware thread, and wait up to `wait` for it to finish.
    ///
    /// Any acquisition ends and the scope is closed, so the next daemon finds
    /// the unit idle; one killed mid-transfer leaves the next open blocked
    /// inside the driver for minutes. False if the thread did not finish in
    /// time, which happens when it is inside the driver's open call: nothing
    /// can interrupt that.
    pub fn shutdown(&self, wait: Duration) -> bool {
        self.stopping.store(true, Ordering::Relaxed);
        // Wakes a thread waiting for its next command. A full queue means it
        // is busy, and a busy thread checks `stopping` between commands.
        let _ = self.commands.try_send(Message::Shutdown);
        let Some(thread) = lock(&self.thread).take() else {
            return true;
        };
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let _ = thread.join();
            let _ = done_tx.send(());
        });
        done_rx.recv_timeout(wait).is_ok()
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Published> {
        self.captures.subscribe()
    }

    /// The scope's settings as last published, and every change after.
    pub fn watch_state(&self) -> watch::Receiver<Arc<ScopeState>> {
        self.state.clone()
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
    let (command_tx, command_rx) = mpsc::channel::<Message>(COMMAND_QUEUE_DEPTH);
    let (capture_tx, _) = broadcast::channel(CAPTURE_BROADCAST_DEPTH);
    let (state_tx, state_rx) = watch::channel(Arc::new(unopened_state()));
    let acquiring = Arc::new(AtomicBool::new(false));
    let capture_count = Arc::new(AtomicU64::new(0));
    let open_state = Arc::new(Mutex::new(OpenState::opening(None)));
    let stopping = Arc::new(AtomicBool::new(false));

    // Carries the first outcome, so startup can log what happened rather than
    // leaving it to be discovered by a failing command later.
    let (ready_tx, ready_rx) = std::sync::mpsc::channel::<Result<()>>();

    let shared = Shared {
        captures: capture_tx.clone(),
        acquiring: acquiring.clone(),
        capture_count: capture_count.clone(),
        state: state_tx,
        stopping: stopping.clone(),
    };
    let thread_state = open_state.clone();
    let thread = std::thread::Builder::new()
        .name("scope-hw".into())
        .spawn(move || hardware_thread(open, command_rx, shared, thread_state, ready_tx))?;

    match ready_rx.recv_timeout(OPEN_TIMEOUT) {
        // Logged by the thread, which also sees an open slower than this.
        Ok(Ok(())) => {}
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
        state: state_rx,
        stopping,
        thread: Arc::new(Mutex::new(Some(thread))),
    })
}

/// What the hardware thread shares with the handles.
struct Shared {
    captures: broadcast::Sender<Published>,
    acquiring: Arc<AtomicBool>,
    capture_count: Arc<AtomicU64>,
    state: watch::Sender<Arc<ScopeState>>,
    stopping: Arc<AtomicBool>,
}

/// Why the acquisition loop returned.
enum Ended {
    /// The daemon is stopping, or nothing can send commands any more.
    Shutdown,
    /// The unit stopped answering, with the error that said so.
    UnitGone(String),
}

/// Open the scope and run it, and open it again whenever it is lost or a
/// driver call panics, until the daemon stops.
///
/// A panic used to end this thread and, with it, every command after: the
/// handles stayed up and answered "not running" until someone restarted the
/// daemon. Unwinding drops the scope, and dropping it closes the unit, so
/// starting over from the open is safe.
fn hardware_thread<F>(
    mut open: F,
    mut commands: mpsc::Receiver<Message>,
    shared: Shared,
    open_state: Arc<Mutex<OpenState>>,
    ready: std::sync::mpsc::Sender<Result<()>>,
) where
    F: FnMut() -> Result<Box<dyn Oscilloscope>>,
{
    let mut attempt: u64 = 0;
    let mut complained_at: Option<Instant> = None;
    let mut last_failure: Option<String> = None;
    loop {
        let scope = loop {
            if shared.stopping.load(Ordering::Relaxed) {
                return;
            }
            attempt += 1;
            *lock(&open_state) = OpenState::opening(last_failure.clone());
            let opened = std::panic::catch_unwind(std::panic::AssertUnwindSafe(&mut open))
                .unwrap_or_else(|panic| {
                    Err(anyhow::anyhow!(
                        "the driver panicked while opening the scope: {}",
                        panic_message(&*panic)
                    ))
                });
            match opened {
                Ok(scope) => {
                    *lock(&open_state) = OpenState::Open;
                    let _ = ready.send(Ok(()));
                    tracing::info!(attempt, "oscilloscope opened");
                    break scope;
                }
                Err(e) => {
                    // `{:#}` so the causes come with it: the useful part is
                    // usually the innermost one, naming the library and the
                    // search path.
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
                    *lock(&open_state) = OpenState::Failed(reason.clone());
                    last_failure = Some(reason);
                    let _ = ready.send(Err(e));
                    sleep_unless_stopping(OPEN_RETRY_INTERVAL, &shared.stopping);
                }
            }
        };
        attempt = 0;
        complained_at = None;

        let ended = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            run(scope, &mut commands, &shared)
        }));
        shared.acquiring.store(false, Ordering::Relaxed);
        let reason = match ended {
            Ok(Ended::Shutdown) => return,
            Ok(Ended::UnitGone(reason)) => {
                tracing::warn!(error = %reason, "the oscilloscope stopped answering; closed it, and opening it again");
                reason
            }
            Err(panic) => {
                let message = panic_message(&*panic);
                tracing::error!(panic = %message, "the oscilloscope thread panicked; closed the scope, and opening it again");
                format!("the driver panicked: {message}")
            }
        };
        *lock(&open_state) = OpenState::Failed(reason.clone());
        // Requests sent to the scope that went. They are refused rather than
        // carried over: a setting meant for that unit is not necessarily
        // wanted on whatever opens next.
        while let Ok(message) = commands.try_recv() {
            match message {
                Message::Request((_, reply_to)) => {
                    let _ = reply_to.send(ScopeReply::Error(format!(
                        "the oscilloscope was lost before this was answered: {reason}"
                    )));
                }
                Message::Shutdown => return,
            }
        }
        // Subscribers are told there is no scope, rather than left showing
        // the settings of one that has gone.
        let mut unopened = unopened_state();
        unopened.version = shared.state.borrow().version + 1;
        shared.state.send_replace(Arc::new(unopened));
        last_failure = Some(reason);
    }
}

fn panic_message(panic: &(dyn std::any::Any + Send)) -> String {
    panic
        .downcast_ref::<&str>()
        .map(|s| s.to_string())
        .or_else(|| panic.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "no message".to_string())
}

/// Sleep, waking early if the daemon starts stopping.
fn sleep_unless_stopping(duration: Duration, stopping: &AtomicBool) {
    let until = Instant::now() + duration;
    while !stopping.load(Ordering::Relaxed) {
        let left = until.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return;
        }
        std::thread::sleep(left.min(Duration::from_millis(50)));
    }
}

/// What a subscriber is told before there is a scope to describe.
fn unopened_state() -> ScopeState {
    ScopeState {
        version: 0,
        acquiring: false,
        rolling: false,
        capture_mode: CaptureMode::Auto,
        trigger: TriggerState {
            source: ChannelId::Alphabetic('A'),
            level: 0.0,
            slope: TriggerSlope::Rising,
            holdoff_s: 0.0,
            position_percent: 50.0,
        },
        timebase: TimebaseState {
            time_per_div: 0.0,
            time_offset: 0.0,
            sample_interval_ns: 0.0,
            memory_depth: 0,
            roll: RollMode::Auto,
        },
        acquisition: AcquisitionState {
            mode: AcquisitionMode::Normal,
            average_count: DEFAULT_AVERAGE_COUNT,
        },
        channels: Vec::new(),
        display: serde_json::json!({}),
    }
}

fn run(
    mut scope: Box<dyn Oscilloscope>,
    commands: &mut mpsc::Receiver<Message>,
    shared: &Shared,
) -> Ended {
    let Shared {
        captures,
        acquiring,
        capture_count,
        state: state_tx,
        stopping,
    } = shared;
    let mut state = LoopState::new();
    // Carried on from any scope this thread had before, so that neither
    // number a client sees ever goes backwards. The loop numbers every
    // capture it publishes, and only those, so the count is the sequence.
    state.sequence = capture_count.load(Ordering::Relaxed);
    state.version = state_tx.borrow().version;
    state.refresh_trigger(&*scope);
    publish_state(&*scope, &mut state, acquiring, state_tx);

    // When idle this blocks on the command channel and consumes nothing.
    // The old design polled every 10 ms whether or not anything was
    // acquiring, which is what produced ~1 GB/day of readiness logging.
    let mut next_poll: Option<Instant> = None;
    let mut waiting: Vec<Waiting> = Vec::new();

    let ended = loop {
        if stopping.load(Ordering::Relaxed) {
            break Ended::Shutdown;
        }
        if let Some(reason) = state.unit_gone.take() {
            break Ended::UnitGone(reason);
        }
        let busy = acquiring.load(Ordering::Relaxed) || state.roller.is_some();
        if !waiting.is_empty() {
            settle_waiting(&mut scope, acquiring, &mut state, &mut waiting, busy);
        }

        let message = if busy {
            let wait = next_poll
                .map(|at| at.saturating_duration_since(Instant::now()))
                .unwrap_or(Duration::ZERO);
            match commands.try_recv() {
                Ok(message) => Some(message),
                Err(mpsc::error::TryRecvError::Disconnected) => break Ended::Shutdown,
                Err(mpsc::error::TryRecvError::Empty) => {
                    if !wait.is_zero() {
                        std::thread::sleep(wait.min(Duration::from_millis(5)));
                    }
                    None
                }
            }
        } else {
            match commands.blocking_recv() {
                Some(message) => Some(message),
                None => break Ended::Shutdown,
            }
        };

        match message {
            Some(Message::Shutdown) => break Ended::Shutdown,
            Some(Message::Request((request, reply_to))) => {
                if waiting.len() < MAX_WAITING
                    && waits_for_a_capture(&request, &state, acquiring)
                {
                    waiting.push(Waiting {
                        request,
                        reply_to,
                        deadline: Instant::now() + CAPTURE_WAIT,
                    });
                    continue;
                }
                let (reply, changed) = serve(&mut scope, acquiring, &mut state, request);
                // A client that hung up mid-request is normal, not an error.
                let _ = reply_to.send(reply);
                if changed {
                    publish_state(&*scope, &mut state, acquiring, state_tx);
                    next_poll = None;
                }
                continue;
            }
            None => {}
        }

        if state.roller.is_some() {
            if let Err(e) = roll_step(&mut scope, &mut state, captures, capture_count) {
                if pico::status::unit_is_gone(&e) {
                    break Ended::UnitGone(format!("{e:#}"));
                }
                tracing::warn!(error = %e, "roll mode stopped");
                stop_rolling(&mut scope, &mut state);
                acquiring.store(false, Ordering::Relaxed);
                publish_state(&*scope, &mut state, acquiring, state_tx);
            }
            if state.last_frame.is_some() {
                answer_waiting(&mut scope, acquiring, &mut state, &mut waiting);
            }
            next_poll = Some(Instant::now() + ROLL_POLL_INTERVAL);
            continue;
        }

        if !acquiring.load(Ordering::Relaxed) {
            continue;
        }

        // An arm held back until the holdoff after the last trigger ran out.
        if let Some(due) = state.arm_at {
            if Instant::now() < due {
                next_poll = Some(due);
                continue;
            }
            state.arm_at = None;
            if let Err(e) = scope.rearm() {
                if pico::status::unit_is_gone(&e) {
                    break Ended::UnitGone(format!("{e:#}"));
                }
                tracing::warn!(error = %e, "failed to rearm capture");
                acquiring.store(false, Ordering::Relaxed);
                publish_state(&*scope, &mut state, acquiring, state_tx);
            }
            next_poll = Some(Instant::now() + scope.suggested_poll_interval());
            continue;
        }

        match scope.is_ready() {
            Ok(true) => match scope.get_triggered_data() {
                Ok(frame) => {
                    let ready_at = Instant::now();
                    let frame = Arc::new(state.process(frame));
                    capture_count.fetch_add(1, Ordering::Relaxed);

                    // Kept as well as sent, so a command that needs samples
                    // has the ones just published rather than re-reading a
                    // device that is already collecting the next block.
                    state.last_frame = Some(frame.clone());
                    state.captured_since_arm = true;

                    // Send failure only means nobody is subscribed. The
                    // acquisition loop keeps running so a reconnecting
                    // client sees live data immediately.
                    let _ = captures.send(Published::new(frame.clone()));
                    answer_waiting(&mut scope, acquiring, &mut state, &mut waiting);

                    if state.trigger.mode == CaptureMode::Single {
                        acquiring.store(false, Ordering::Relaxed);
                        let _ = scope.stop_triggered_capture();
                        // The driver returns a completed single-shot to
                        // normal; the state says so, and that it stopped.
                        state.refresh_trigger(&*scope);
                        publish_state(&*scope, &mut state, acquiring, state_tx);
                        continue;
                    }

                    let due = holdoff_deadline(ready_at, &frame, state.holdoff);
                    if due > Instant::now() {
                        state.arm_at = Some(due);
                        next_poll = Some(due);
                        continue;
                    }
                    if let Err(e) = scope.rearm() {
                        if pico::status::unit_is_gone(&e) {
                            break Ended::UnitGone(format!("{e:#}"));
                        }
                        tracing::warn!(error = %e, "failed to rearm capture");
                        acquiring.store(false, Ordering::Relaxed);
                        publish_state(&*scope, &mut state, acquiring, state_tx);
                    }
                    // Not immediately, which is what this once did. A freshly
                    // armed block cannot be complete, so the only thing a
                    // zero-delay poll can find is a readiness flag left over
                    // from the block just read -- and acting on that reads the
                    // new block while the device is still filling it. The
                    // samples that come back are then whatever the buffer
                    // holds mid-collection, with the trigger nowhere near
                    // where the arm put it, which is a trace that jumps
                    // sideways every few frames however steady the signal is.
                    //
                    // The same interval the not-ready branch below waits, for
                    // the same reason: polling faster than the capture can
                    // complete cannot produce data.
                    next_poll = Some(Instant::now() + scope.suggested_poll_interval());
                }
                Err(e) => {
                    if pico::status::unit_is_gone(&e) {
                        break Ended::UnitGone(format!("{e:#}"));
                    }
                    tracing::warn!(error = %e, "capture read failed");
                    next_poll = Some(Instant::now() + Duration::from_millis(10));
                }
            },
            Ok(false) => {
                next_poll = Some(Instant::now() + scope.suggested_poll_interval());
            }
            Err(e) => {
                if pico::status::unit_is_gone(&e) {
                    break Ended::UnitGone(format!("{e:#}"));
                }
                tracing::warn!(error = %e, "readiness check failed, stopping acquisition");
                acquiring.store(false, Ordering::Relaxed);
                publish_state(&*scope, &mut state, acquiring, state_tx);
            }
        }
    };

    let farewell = match &ended {
        Ended::Shutdown => {
            tracing::info!("oscilloscope thread shutting down");
            "the oscilloscope daemon is stopping".to_string()
        }
        Ended::UnitGone(reason) => format!("the oscilloscope stopped answering: {reason}"),
    };
    for held in waiting.drain(..) {
        let _ = held.reply_to.send(ScopeReply::Error(farewell.clone()));
    }
    stop_rolling(&mut scope, &mut state);
    let _ = scope.stop_triggered_capture();
    ended
}

/// Longest a request for samples waits for the first capture at the current
/// settings. Inside the box client's own ten seconds, so the caller is told
/// why rather than only that it gave up.
const CAPTURE_WAIT: Duration = Duration::from_secs(8);

/// Requests that may wait for a capture at once. A page polling its
/// measurements on a scope that never triggers adds about one a second.
const MAX_WAITING: usize = 64;

/// A request for samples, held until the loop has some at the current
/// settings.
struct Waiting {
    request: ScopeRequest,
    reply_to: oneshot::Sender<ScopeReply>,
    deadline: Instant,
}

/// Whether a request has to wait for the loop's next capture: it wants
/// samples, nothing has been published since the settings last changed, and
/// a capture is on its way.
fn waits_for_a_capture(request: &ScopeRequest, state: &LoopState, acquiring: &AtomicBool) -> bool {
    matches!(
        request,
        ScopeRequest::GetTriggeredData
            | ScopeRequest::Measure { .. }
            | ScopeRequest::MeasureAll { .. }
    ) && state.last_frame.is_none()
        && (acquiring.load(Ordering::Relaxed) || state.roller.is_some())
}

/// Answer every waiting request from the capture just published.
fn answer_waiting(
    scope: &mut Box<dyn Oscilloscope>,
    acquiring: &AtomicBool,
    state: &mut LoopState,
    waiting: &mut Vec<Waiting>,
) {
    for held in waiting.drain(..) {
        let (reply, _) = serve(scope, acquiring, state, held.request);
        let _ = held.reply_to.send(reply);
    }
}

/// Answer the waiting requests nothing more is coming for: all of them once
/// the scope stops or a capture is there, and any whose wait has run out.
fn settle_waiting(
    scope: &mut Box<dyn Oscilloscope>,
    acquiring: &AtomicBool,
    state: &mut LoopState,
    waiting: &mut Vec<Waiting>,
    busy: bool,
) {
    let now = Instant::now();
    let captured = state.last_frame.is_some();
    let (due, still): (Vec<_>, Vec<_>) = std::mem::take(waiting)
        .into_iter()
        .partition(|held| !busy || captured || held.deadline <= now);
    *waiting = still;
    for held in due {
        let reply = if busy && !captured {
            ScopeReply::error(format!(
                "no capture completed in {} s at the current settings; in normal \
                 mode, check that the signal crosses the trigger level",
                CAPTURE_WAIT.as_secs()
            ))
        } else {
            serve(scope, acquiring, state, held.request).0
        };
        let _ = held.reply_to.send(reply);
    }
}

/// When the next block may be armed, after a capture that became ready at
/// `ready_at`.
///
/// Holdoff counts from the trigger, which was a post-trigger segment's worth
/// of samples before the block finished. With no holdoff the answer is now.
fn holdoff_deadline(ready_at: Instant, frame: &CaptureFrame, holdoff: Duration) -> Instant {
    if holdoff.is_zero() {
        return ready_at;
    }
    let post_ns = f64::from(frame.post_trigger_samples) * frame.sample_interval_ns;
    let post = Duration::from_nanos(post_ns.max(0.0) as u64);
    ready_at.checked_sub(post).unwrap_or(ready_at) + holdoff
}

/// Whether answering this request leaves the published capture describing a
/// scope that no longer exists.
///
/// Every setting here re-arms the hardware on this family: the drivers stop,
/// reprogram and restart the block, because that is how a change takes effect
/// mid-acquisition. So the samples last published were taken under the old
/// setting and must not go on being served -- a `Measure` after a new
/// timebase would answer from the old window, and `IsReady` would report a
/// capture belonging to a configuration the caller has already replaced.
///
/// Listed rather than derived so that adding a request has to say which it
/// is. The reads are all here too, as the `false` arm, for the same reason.
fn invalidates_the_published_capture(request: &ScopeRequest) -> bool {
    match request {
        ScopeRequest::EnableChannel(_)
        | ScopeRequest::DisableChannel(_)
        | ScopeRequest::SetVoltsPerDiv(..)
        | ScopeRequest::SetVoltsOffset(..)
        | ScopeRequest::SetCoupling(..)
        | ScopeRequest::SetAttenuation(..)
        | ScopeRequest::SetTimePerDiv(_)
        | ScopeRequest::SetTimeOffset(_)
        | ScopeRequest::SetTriggerLevel(_)
        | ScopeRequest::SetTriggerSource(_)
        | ScopeRequest::SetTriggerSlope(_)
        | ScopeRequest::SetCaptureMode(_)
        | ScopeRequest::StartAcquisition(_)
        // A new way of combining captures, and a switch between blocks and
        // streaming, both make what was published unlike what comes next.
        | ScopeRequest::SetAcquisition(..)
        | ScopeRequest::SetRoll(_) => true,

        // Stopping keeps it. The last capture is what a stopped scope is
        // showing, and a measurement of it is the reading on screen -- the
        // same as reading a held trace off a bench scope.
        ScopeRequest::StopAcquisition
        // Forcing asks for a capture rather than changing what one would
        // contain, and the loop replaces the frame as soon as one arrives.
        | ScopeRequest::ForceTrigger
        // Holdoff changes when captures are taken, not what is in them.
        | ScopeRequest::SetHoldoff(_)
        // The display state is the clients'; the samples do not see it.
        | ScopeRequest::SetDisplay(_)
        | ScopeRequest::IsChannelEnabled(_)
        | ScopeRequest::GetVoltsPerDiv(_)
        | ScopeRequest::GetVoltsOffset(_)
        | ScopeRequest::GetCoupling(_)
        | ScopeRequest::GetAttenuation(_)
        | ScopeRequest::GetTimePerDiv
        | ScopeRequest::GetTimeOffset
        | ScopeRequest::GetTriggerLevel
        | ScopeRequest::GetTriggerSource
        | ScopeRequest::GetTriggerSlope
        | ScopeRequest::GetCaptureMode
        | ScopeRequest::GetSampleRate
        | ScopeRequest::GetMemoryDepth
        | ScopeRequest::GetBandwidth
        | ScopeRequest::GetChannelCount
        | ScopeRequest::GetCapabilities
        | ScopeRequest::IsReady
        | ScopeRequest::GetTriggeredData
        | ScopeRequest::Measure { .. }
        | ScopeRequest::MeasureAll { .. }
        | ScopeRequest::GetState
        | ScopeRequest::GetDisplay
        | ScopeRequest::GetAcquisition
        | ScopeRequest::GetHoldoff
        | ScopeRequest::GetRoll => false,
    }
}

/// Answer one request, with the steps around `handle` that keep the
/// acquisition in line with it. Returns the reply and whether the state
/// subscribers are shown may have changed.
fn serve(
    scope: &mut Box<dyn Oscilloscope>,
    acquiring: &AtomicBool,
    state: &mut LoopState,
    request: ScopeRequest,
) -> (ScopeReply, bool) {
    let pauses_roll = touches_hardware(&request);
    // Forcing re-arms the unit too, and can fail part-way through.
    let rearms = pauses_roll || matches!(request, ScopeRequest::ForceTrigger);
    let changes = changes_state(&request);
    // Streaming runs on setup the setters are about to change, and the driver
    // cannot reprogram it mid-stream. It stops for them and starts again,
    // below, with whatever they leave.
    let was_rolling = state.roller.is_some();
    if pauses_roll && was_rolling {
        stop_rolling(scope, state);
    }
    let reply = handle(scope, request, acquiring, state);
    let refused = rearms && matches!(reply, ScopeReply::Error(_));
    if pauses_roll || changes {
        state.refresh_trigger(&**scope);
        reconcile_roll(scope, acquiring, state, was_rolling);
    }
    // A setting the hardware refused can stop the unit on its way to the
    // refusal. The drivers put it back; this is the backstop for one that
    // does not, because an acquisition polling a stopped unit is a frozen
    // trace with no error anywhere.
    if refused && state.roller.is_none() && acquiring.load(Ordering::Relaxed) {
        state.arm_at = None;
        if let Err(e) = scope.rearm() {
            tracing::warn!(error = %e, "failed to rearm after a refused setting");
            acquiring.store(false, Ordering::Relaxed);
        }
    }
    (reply, changes || refused)
}

/// Whether this request programs the device, which roll mode has to stop
/// streaming for.
fn touches_hardware(request: &ScopeRequest) -> bool {
    match request {
        // Changes how the loop combines or displays captures, not the device.
        ScopeRequest::SetAcquisition(..) | ScopeRequest::SetRoll(_) => false,
        ScopeRequest::StopAcquisition => true,
        // Refused while rolling, before it reaches the device.
        ScopeRequest::ForceTrigger => false,
        other => invalidates_the_published_capture(other),
    }
}

/// Whether answering this request changes what `GetState` would say.
fn changes_state(request: &ScopeRequest) -> bool {
    invalidates_the_published_capture(request)
        || matches!(
            request,
            ScopeRequest::StopAcquisition
                | ScopeRequest::SetHoldoff(_)
                | ScopeRequest::SetDisplay(_)
        )
}

/// The trigger as the loop needs it once per capture, read from the driver
/// after every change rather than on every frame.
#[derive(Debug, Clone, Copy, PartialEq)]
struct TriggerCache {
    mode: CaptureMode,
    source: ChannelId,
    /// Volts at the probe tip.
    level: f64,
    slope: TriggerSlope,
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
    /// Bumped with every published state.
    version: u64,
    display: serde_json::Value,
    acquisition: AcquisitionMode,
    average_count: u32,
    averager: Averager,
    holdoff: Duration,
    roll: RollMode,
    /// Present while streaming in roll mode.
    roller: Option<Roller>,
    /// An arm held back by the holdoff, and when it is due.
    arm_at: Option<Instant>,
    /// The next capture is the one a `ForceTrigger` asked for.
    forced: bool,
    trigger: TriggerCache,
    /// A driver call said the unit has gone, with what it said. The loop
    /// closes the scope and opens it again.
    unit_gone: Option<String>,
}

impl LoopState {
    fn new() -> Self {
        LoopState {
            sequence: 0,
            captured_since_arm: false,
            last_frame: None,
            version: 0,
            display: serde_json::json!({}),
            acquisition: AcquisitionMode::Normal,
            average_count: DEFAULT_AVERAGE_COUNT,
            averager: Averager::default(),
            holdoff: Duration::ZERO,
            roll: RollMode::Auto,
            roller: None,
            arm_at: None,
            forced: false,
            trigger: TriggerCache {
                mode: CaptureMode::Auto,
                source: ChannelId::Alphabetic('A'),
                level: 0.0,
                slope: TriggerSlope::Rising,
            },
            unit_gone: None,
        }
    }

    /// The reply for a driver error, noting whether it says the unit is gone.
    fn failed(&mut self, error: anyhow::Error) -> ScopeReply {
        if pico::status::unit_is_gone(&error) {
            self.unit_gone = Some(format!("{error:#}"));
        }
        ScopeReply::error(error)
    }

    fn refresh_trigger(&mut self, scope: &dyn Oscilloscope) {
        let current = self.trigger;
        self.trigger = TriggerCache {
            mode: scope.get_capture_mode().unwrap_or(current.mode),
            source: scope.get_trigger_source().unwrap_or(current.source),
            level: scope.get_trigger_level().unwrap_or(current.level),
            slope: scope.get_trigger_slope().unwrap_or(current.slope),
        };
    }

    /// Number a capture, say whether it triggered, and fold it into the
    /// average if one is running.
    fn process(&mut self, mut frame: CaptureFrame) -> CaptureFrame {
        self.sequence += 1;
        frame.seq = self.sequence;

        // Normal and single wait for the trigger, so a block that completed
        // triggered. Auto also completes when the timeout runs out, and so
        // does a forced capture, so for those the samples have to say.
        let forced = std::mem::take(&mut self.forced);
        let triggered = match self.trigger.mode {
            CaptureMode::Normal | CaptureMode::Single if !forced => true,
            _ => protocol::trigger::frame_is_triggered(
                &frame,
                self.trigger.source,
                self.trigger.level,
                self.trigger.slope,
            ),
        };
        if triggered {
            frame.flags |= FLAG_TRIGGERED;
        } else {
            frame.flags &= !FLAG_TRIGGERED;
        }

        if self.acquisition == AcquisitionMode::Average && self.average_count > 1 {
            frame = self.averager.feed(frame, self.average_count);
        }
        frame
    }
}

/// A running average of captures, sample by sample.
///
/// The first `count` captures are averaged equally, and after that each new
/// one takes a `1/count` share -- the exponential form bench scopes use, so a
/// change in the signal shows within `count` captures rather than waiting for
/// a whole new batch. Anything that changes what a sample means (the
/// channels, the depth) starts it over.
#[derive(Debug, Default)]
struct Averager {
    sums: Vec<f32>,
    taken: u32,
    shape: Option<(Vec<ChannelId>, u32)>,
}

impl Averager {
    fn reset(&mut self) {
        self.sums.clear();
        self.taken = 0;
        self.shape = None;
    }

    fn feed(&mut self, frame: CaptureFrame, count: u32) -> CaptureFrame {
        let shape = (
            frame.channels.iter().map(|c| c.channel).collect::<Vec<_>>(),
            frame.samples_per_channel,
        );
        if self.shape.as_ref() != Some(&shape) || self.sums.len() != frame.samples.len() {
            self.sums = frame.samples.iter().map(|&s| f32::from(s)).collect();
            self.taken = 1;
            self.shape = Some(shape);
            return frame;
        }

        self.taken = (self.taken + 1).min(count.max(1));
        let weight = 1.0 / self.taken as f32;
        for (sum, &sample) in self.sums.iter_mut().zip(&frame.samples) {
            *sum += (f32::from(sample) - *sum) * weight;
        }

        let mut averaged = frame;
        // Rounded back to counts: on an 8-bit part a count is 256 of these,
        // so the averaged trace keeps the resolution it gained.
        averaged.samples = self
            .sums
            .iter()
            .map(|&sum| sum.round().clamp(-32767.0, 32767.0) as i16)
            .collect();
        averaged
    }
}

/// A rolling screen of minimum/maximum pairs, fed by the driver's stream.
struct Roller {
    info: RollInfo,
    time_per_div: f64,
    /// Pairs kept before the screen: see `ROLL_HISTORY`.
    history: usize,
    rings: Vec<VecDeque<(i16, i16)>>,
    sink: RollSink,
    /// Pairs have arrived since the last frame was published.
    fresh: bool,
    /// When the next screen is due, on a fixed schedule so the frames are
    /// evenly spaced whatever the poll happens to land on.
    next_publish: Option<Instant>,
}

impl Roller {
    fn new(info: RollInfo, time_per_div: f64) -> Self {
        // A quarter of a screen, or the history time if that is more, and
        // never more than a second screen.
        let for_time = if info.bucket_ns > 0.0 {
            (ROLL_HISTORY.as_nanos() as f64 / info.bucket_ns).ceil() as usize
        } else {
            0
        };
        let history = for_time.max(ROLL_COLUMNS / 4).min(ROLL_COLUMNS);
        let rings = info
            .channels
            .iter()
            .map(|_| VecDeque::with_capacity(ROLL_COLUMNS + history))
            .collect();
        Roller {
            info,
            time_per_div,
            history,
            rings,
            sink: RollSink::default(),
            fresh: false,
            next_publish: None,
        }
    }

    fn absorb(&mut self) -> usize {
        let mut arrived = 0;
        for (ring, pairs) in self.rings.iter_mut().zip(&self.sink.pairs) {
            arrived = arrived.max(pairs.len());
            for &pair in pairs {
                if ring.len() == ROLL_COLUMNS + self.history {
                    ring.pop_front();
                }
                ring.push_back(pair);
            }
        }
        if arrived > 0 {
            self.fresh = true;
        }
        arrived
    }

    /// The whole screen as one frame, newest pair at the right edge, with
    /// the part of the screen not yet streamed marked as not captured.
    ///
    /// With `history`, the pairs from before the screen come first, for a
    /// page that draws behind live; without, the frame is the screen alone,
    /// which is what a measurement or a script reads.
    fn snapshot(&self, history: bool) -> CaptureFrame {
        let pairs_per_channel = ROLL_COLUMNS + if history { self.history } else { 0 };
        let per_channel = pairs_per_channel * 2;
        let mut samples = Vec::with_capacity(per_channel * self.rings.len());
        for ring in &self.rings {
            let kept = ring.len().min(pairs_per_channel);
            samples.extend(std::iter::repeat_n(NO_SAMPLE, (pairs_per_channel - kept) * 2));
            for &(low, high) in ring.range(ring.len() - kept..) {
                samples.push(low);
                samples.push(high);
            }
        }
        CaptureFrame {
            seq: 0,
            capture_mono_ns: monotonic_ns(),
            // Half the pair's interval, so the pairs, read as samples, span
            // the time they cover.
            sample_interval_ns: self.info.bucket_ns / 2.0,
            // Now is the right edge.
            pre_trigger_samples: per_channel as u32,
            post_trigger_samples: 0,
            samples_per_channel: per_channel as u32,
            resolution_bits: self.info.resolution_bits,
            overflow_mask: 0,
            flags: FLAG_STREAMING | FLAG_ENVELOPE,
            screen_samples: if history { (ROLL_COLUMNS * 2) as u32 } else { 0 },
            channels: self.info.channels.clone(),
            samples,
        }
    }

    /// The time/div the screen actually spans.
    fn screen_time_per_div(&self) -> f64 {
        self.info.bucket_ns * ROLL_COLUMNS as f64 / 1e9 / 10.0
    }
}

/// The time/div to roll at, or None if the scope should capture blocks.
fn roll_time_per_div(
    scope: &dyn Oscilloscope,
    acquiring: bool,
    state: &LoopState,
) -> Option<f64> {
    if !acquiring || !scope.supports_roll() {
        return None;
    }
    let time_per_div = scope.requested_time_per_div().ok()?;
    let rolls = match state.roll {
        RollMode::Off => false,
        RollMode::On => true,
        // A hair under the threshold still counts, so a time/div read back
        // through a float conversion does not flip the mode.
        RollMode::Auto => {
            state.trigger.mode == CaptureMode::Auto
                && time_per_div >= ROLL_THRESHOLD_S_PER_DIV * 0.999
        }
    };
    rolls.then_some(time_per_div)
}

/// Start or stop rolling to match the settings the last request left.
///
/// `was_rolling` says the loop stopped streaming for that request, so if the
/// scope should now capture blocks instead, nothing has armed one yet.
fn reconcile_roll(
    scope: &mut Box<dyn Oscilloscope>,
    acquiring: &AtomicBool,
    state: &mut LoopState,
    was_rolling: bool,
) {
    let is_acquiring = acquiring.load(Ordering::Relaxed);
    let want = roll_time_per_div(&**scope, is_acquiring, state);
    let current = state.roller.as_ref().map(|r| r.time_per_div);
    match (want, current) {
        (Some(wanted), Some(rolling)) if (wanted - rolling).abs() <= wanted * 1e-9 => {}
        (Some(wanted), _) => {
            stop_rolling(scope, state);
            let plan = RollPlan {
                bucket_ns: wanted * 10.0 * 1e9 / ROLL_COLUMNS as f64,
            };
            match scope.start_roll(&plan) {
                Ok(info) => {
                    state.arm_at = None;
                    state.averager.reset();
                    state.roller = Some(Roller::new(info, wanted));
                }
                Err(e) => {
                    // Blocks still work, and are what the scope was doing.
                    tracing::warn!(error = %e, "could not start roll mode; capturing blocks");
                    let position = scope.get_trigger_position().unwrap_or(50.0);
                    if let Err(e) = scope.start_triggered_capture(position) {
                        tracing::warn!(error = %e, "failed to arm after roll mode was refused");
                        acquiring.store(false, Ordering::Relaxed);
                    }
                }
            }
        }
        (None, current) => {
            if current.is_some() {
                stop_rolling(scope, state);
            }
            if is_acquiring && (current.is_some() || was_rolling) {
                let position = scope.get_trigger_position().unwrap_or(50.0);
                if let Err(e) = scope.start_triggered_capture(position) {
                    tracing::warn!(error = %e, "failed to arm after leaving roll mode");
                    acquiring.store(false, Ordering::Relaxed);
                }
            }
        }
    }
}

fn stop_rolling(scope: &mut Box<dyn Oscilloscope>, state: &mut LoopState) {
    if state.roller.take().is_some() {
        if let Err(e) = scope.stop_roll() {
            tracing::warn!(error = %e, "stopping roll mode");
        }
    }
}

/// Collect what has streamed in, and publish the screen if it changed.
fn roll_step(
    scope: &mut Box<dyn Oscilloscope>,
    state: &mut LoopState,
    captures: &broadcast::Sender<Published>,
    capture_count: &AtomicU64,
) -> Result<()> {
    let Some(roller) = state.roller.as_mut() else {
        return Ok(());
    };
    roller.sink.clear();
    scope.poll_roll(&mut roller.sink)?;
    roller.absorb();

    let now = Instant::now();
    let due = roller.next_publish.is_none_or(|at| now >= at);
    if !(roller.fresh && due) {
        return Ok(());
    }
    let mut frame = roller.snapshot(true);
    let mut screen = roller.snapshot(false);
    roller.fresh = false;
    roller.next_publish = Some(match roller.next_publish {
        Some(at) if at + ROLL_FRAME_INTERVAL >= now => at + ROLL_FRAME_INTERVAL,
        _ => now + ROLL_FRAME_INTERVAL,
    });

    state.sequence += 1;
    frame.seq = state.sequence;
    screen.seq = state.sequence;
    screen.capture_mono_ns = frame.capture_mono_ns;
    let frame = Arc::new(frame);
    state.last_frame = Some(Arc::new(screen));
    state.captured_since_arm = true;
    capture_count.fetch_add(1, Ordering::Relaxed);
    let _ = captures.send(Published::new(frame));
    Ok(())
}

/// Everything the controls need, read from the driver and the loop.
fn build_state(scope: &dyn Oscilloscope, state: &LoopState, acquiring: bool) -> ScopeState {
    let labels: Vec<ChannelId> = scope
        .capabilities()
        .map(|caps| {
            caps.channel_labels
                .iter()
                .filter_map(|label| label.chars().next())
                .map(ChannelId::Alphabetic)
                .collect()
        })
        .unwrap_or_default();
    let channels = labels
        .into_iter()
        .filter_map(|channel| {
            Some(ChannelState {
                channel,
                enabled: scope.is_channel_enabled(channel).ok()?,
                volts_per_div: scope.get_volts_per_div(channel).ok()?,
                volts_offset: scope.get_volts_offset(channel).unwrap_or(0.0),
                coupling: scope.get_coupling(channel).unwrap_or_default(),
                attenuation: scope.get_attenuation(channel).unwrap_or(1.0),
            })
        })
        .collect();

    let (time_per_div, sample_interval_ns) = match &state.roller {
        Some(roller) => (roller.screen_time_per_div(), roller.info.bucket_ns / 2.0),
        None => (
            scope.get_time_per_div().unwrap_or(0.0),
            scope
                .get_sample_rate()
                .ok()
                .filter(|rate| *rate > 0.0)
                .map(|rate| 1e9 / rate)
                .unwrap_or(0.0),
        ),
    };

    ScopeState {
        version: state.version,
        acquiring,
        rolling: state.roller.is_some(),
        capture_mode: state.trigger.mode,
        trigger: TriggerState {
            source: state.trigger.source,
            level: state.trigger.level,
            slope: state.trigger.slope,
            holdoff_s: state.holdoff.as_secs_f64(),
            position_percent: scope.get_trigger_position().unwrap_or(50.0),
        },
        timebase: TimebaseState {
            time_per_div,
            time_offset: scope.get_time_offset().unwrap_or(0.0),
            sample_interval_ns,
            memory_depth: scope.current_memory_depth().unwrap_or(0),
            roll: state.roll,
        },
        acquisition: AcquisitionState {
            mode: state.acquisition,
            average_count: state.average_count,
        },
        channels,
        display: state.display.clone(),
    }
}

fn publish_state(
    scope: &dyn Oscilloscope,
    state: &mut LoopState,
    acquiring: &AtomicBool,
    state_tx: &watch::Sender<Arc<ScopeState>>,
) {
    state.version += 1;
    let snapshot = build_state(scope, state, acquiring.load(Ordering::Relaxed));
    state_tx.send_replace(Arc::new(snapshot));
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
                Err(e) => state.failed(e),
            }
        };
    }
    macro_rules! reply_value {
        ($expr:expr, $variant:path) => {
            match $expr {
                Ok(value) => $variant(value),
                Err(e) => state.failed(e),
            }
        };
    }

    // A setting that re-arms the scope makes the published capture obsolete,
    // and on this family almost every setter re-arms -- the drivers stop,
    // reprogram and restart the block so the change takes effect. Only
    // StartAcquisition used to say so, and it is the one path the drivers do
    // not take when a setter re-arms on its own, so the cache outlived nearly
    // every change to it: a measurement taken after a new timebase described
    // samples from the old one, still stamped with the interval in force when
    // they were read rather than when they were captured.
    if invalidates_the_published_capture(&request) {
        state.captured_since_arm = false;
        state.last_frame = None;
        // An average of captures taken under the old settings is not an
        // average of anything the scope is doing now.
        state.averager.reset();
        // A setting the driver applies re-arms the unit, which ends the
        // holdoff wait. One the loop applies does not: the block already read
        // is still sitting there, flagged ready, and dropping the wait had
        // the loop read it out and publish it a second time.
        if touches_hardware(&request) {
            state.arm_at = None;
        }
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
        ScopeRequest::GetTimePerDiv => match &state.roller {
            // Rolling, the screen spans exactly what was asked for.
            Some(roller) => ScopeReply::Float(roller.screen_time_per_div()),
            None => reply_value!(scope.get_time_per_div(), ScopeReply::Float),
        },
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
        // The block at the current settings, as the published state has it.
        // The 2000 series answered `get_memory_depth` with the model's whole
        // memory, and a client working out a capture's span as depth / rate
        // put the trigger in the wrong place whenever the block was shorter.
        ScopeRequest::GetMemoryDepth => reply_value!(scope.current_memory_depth(), ScopeReply::Usize),
        ScopeRequest::GetBandwidth => reply_value!(scope.get_bandwidth(), ScopeReply::Float),
        ScopeRequest::GetChannelCount => {
            reply_value!(scope.get_channel_count(), ScopeReply::Usize)
        }
        ScopeRequest::GetCapabilities => match scope.capabilities() {
            Ok(capabilities) => ScopeReply::Capabilities(Box::new(capabilities)),
            Err(e) => state.failed(e),
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
            Err(e) => state.failed(e),
        },
        ScopeRequest::StopAcquisition => {
            acquiring.store(false, Ordering::Relaxed);
            state.arm_at = None;
            reply!(scope.stop_triggered_capture(), ScopeReply::Ok)
        }
        ScopeRequest::ForceTrigger => {
            if state.roller.is_some() {
                // Stopped for this request, and restarted after it: there is
                // no trigger in roll mode to force.
                return ScopeReply::error("roll mode runs without a trigger, so there is nothing to force");
            }
            if !acquiring.load(Ordering::Relaxed) {
                // As on a bench scope, where Force does nothing in STOP. It
                // used to arm a block that nothing read, and leave the last
                // capture standing as though it were the forced one.
                return ScopeReply::error(
                    "the scope is stopped, so there is no trigger to force; run it, or \
                     arm a single capture, first",
                );
            }
            state.forced = true;
            match scope.force_trigger() {
                Ok(()) => {
                    // Forcing is asking for a capture now. Left waiting out a
                    // holdoff, the loop would re-arm over the forced block
                    // when the wait ended, and the force would come to nothing.
                    state.arm_at = None;
                    ScopeReply::Ok
                }
                Err(e) => {
                    state.forced = false;
                    state.failed(e)
                }
            }
        }
        // Answered from what the loop has seen, never from the driver: the
        // loop consumes each capture as it completes, so the driver's flag
        // loses that race while acquiring, and when idle it can describe a
        // block from before the settings changed, which nothing will serve.
        // Ready means a capture request would be answered with one now.
        ScopeRequest::IsReady => ScopeReply::Bool(state.captured_since_arm),
        ScopeRequest::GetTriggeredData => match samples_now(state, acquiring) {
            Ok(frame) => ScopeReply::Capture(frame),
            Err(e) => ScopeReply::error(e),
        },
        ScopeRequest::Measure { channel, which } => {
            match measure_now(state, acquiring, channel).and_then(|set| {
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
        ScopeRequest::MeasureAll { channel } => match measure_now(state, acquiring, channel) {
            Ok(set) => ScopeReply::Measurements(Box::new(set)),
            Err(e) => ScopeReply::error(e),
        },
        ScopeRequest::GetState => ScopeReply::State(Box::new(build_state(
            &**scope,
            state,
            acquiring.load(Ordering::Relaxed),
        ))),
        ScopeRequest::SetDisplay(patch) => {
            let mut display = state.display.clone();
            merge_display(&mut display, patch);
            // It goes to every subscriber with every state change, so one
            // client could otherwise make each of those as large as it liked.
            let size = serde_json::to_vec(&display).map_or(usize::MAX, |bytes| bytes.len());
            if size > MAX_DISPLAY_BYTES {
                return ScopeReply::error(format!(
                    "the display settings would come to {size} bytes, over the limit of \
                     {MAX_DISPLAY_BYTES}"
                ));
            }
            state.display = display;
            ScopeReply::Ok
        }
        ScopeRequest::GetDisplay => ScopeReply::Display(state.display.clone()),
        ScopeRequest::SetAcquisition(mode, count) => {
            if let Some(count) = count {
                if !(1..=MAX_AVERAGE_COUNT).contains(&count) {
                    return ScopeReply::error(format!(
                        "an average of {count} captures is outside 1 to {MAX_AVERAGE_COUNT}"
                    ));
                }
            }
            if mode == AcquisitionMode::Peak {
                let supported = scope.capabilities().map(|c| c.peak_detect).unwrap_or(false);
                if !supported {
                    let model = scope
                        .capabilities()
                        .map(|c| c.model)
                        .unwrap_or_else(|_| "this scope".into());
                    let roll = if scope.supports_roll() {
                        "; roll mode (50 ms/div or slower, trigger auto) keeps the minimum and \
                         maximum of every interval"
                    } else {
                        ""
                    };
                    return ScopeReply::error(format!(
                        "the {model} returns one sample per interval in block mode, so there is \
                         nothing between samples for peak detect to keep{roll}"
                    ));
                }
            }
            state.acquisition = mode;
            if let Some(count) = count {
                state.average_count = count;
            }
            state.averager.reset();
            ScopeReply::Ok
        }
        ScopeRequest::GetAcquisition => {
            ScopeReply::Acquisition(state.acquisition, state.average_count)
        }
        ScopeRequest::SetHoldoff(seconds) => {
            if !seconds.is_finite() || !(0.0..=MAX_HOLDOFF_S).contains(&seconds) {
                return ScopeReply::error(format!(
                    "a holdoff of {seconds} s is outside 0 to {MAX_HOLDOFF_S} s"
                ));
            }
            state.holdoff = Duration::from_secs_f64(seconds);
            ScopeReply::Ok
        }
        ScopeRequest::GetHoldoff => ScopeReply::Float(state.holdoff.as_secs_f64()),
        ScopeRequest::SetRoll(mode) => {
            if mode == RollMode::On && !scope.supports_roll() {
                return ScopeReply::error(
                    "this scope cannot stream, so it has no roll mode",
                );
            }
            state.roll = mode;
            ScopeReply::Ok
        }
        ScopeRequest::GetRoll => ScopeReply::Roll(state.roll, state.roller.is_some()),
    }
}

/// The most recent samples: the frame the loop last published.
///
/// The device is never read here. While the loop is acquiring it is
/// mid-block, and a read-out then hands back the block already latched --
/// the same samples on every call, or ones taken before the last setting
/// change and stamped with the new one. Idle, what it holds predates the
/// settings too: changing one clears the published frame precisely because
/// that frame no longer describes the scope. A request that arrives while a
/// capture is on its way waits for it in `run`, so "arm, then ask" gets the
/// capture it armed.
fn samples_now(state: &LoopState, acquiring: &AtomicBool) -> Result<Arc<CaptureFrame>> {
    if let Some(frame) = state.last_frame.clone() {
        return Ok(frame);
    }
    if state.roller.is_some() {
        anyhow::bail!("roll mode has not collected a screen yet; ask again in a moment");
    }
    if acquiring.load(Ordering::Relaxed) {
        anyhow::bail!("no capture has completed at the current settings yet; ask again in a moment");
    }
    anyhow::bail!(
        "there is no capture at the current settings; start the scope, or take a single \
         capture, first"
    )
}

/// Measure against the most recent capture, so the numbers follow the signal.
fn measure_now(
    state: &LoopState,
    acquiring: &AtomicBool,
    channel: ChannelId,
) -> Result<MeasurementSet> {
    let frame = samples_now(state, acquiring)?;
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
        /// Captures armed, by `start_triggered_capture` or a re-arm.
        arms: Arc<AtomicUsize>,
        /// Set when the scope is dropped, which is what closes a real unit.
        dropped: Arc<AtomicBool>,
        refuse_force: bool,
        /// Panics reading the capture mode, which `run` does first.
        panic_on_mode: bool,
        /// Answers readiness the way an unplugged unit does.
        gone: Arc<AtomicBool>,
    }

    impl FakeScope {
        fn new() -> Self {
            FakeScope {
                ready: Arc::new(AtomicBool::new(false)),
                captures_read: Arc::new(AtomicUsize::new(0)),
                arms: Arc::new(AtomicUsize::new(0)),
                dropped: Arc::new(AtomicBool::new(false)),
                refuse_force: false,
                panic_on_mode: false,
                gone: Arc::new(AtomicBool::new(false)),
            }
        }
    }

    impl Drop for FakeScope {
        fn drop(&mut self) {
            self.dropped.store(true, Ordering::SeqCst);
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
            self.arms.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }

        fn stop_triggered_capture(&mut self) -> anyhow::Result<()> {
            Ok(())
        }

        fn is_ready(&self) -> anyhow::Result<bool> {
            if self.gone.load(Ordering::SeqCst) {
                return Err(crate::oscilloscope::pico::status::PicoStatusError::new(
                    "ps5000aIsReady",
                    crate::oscilloscope::pico::status::NOT_RESPONDING,
                )
                .into());
            }
            Ok(self.ready.load(Ordering::Relaxed))
        }

        fn force_trigger(&mut self) -> anyhow::Result<()> {
            if self.refuse_force {
                anyhow::bail!("the unit refused the forced capture");
            }
            Ok(())
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
                screen_samples: 0,
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
            if self.panic_on_mode {
                panic!("a driver bug");
            }
            Ok(CaptureMode::Normal)
        }

        fn get_trigger_position(&self) -> anyhow::Result<f64> {
            Ok(50.0)
        }

        /// Implemented rather than left unimplemented with the other setters:
        /// a timebase change re-arms the scope, which puts it on the capture
        /// path. The driver does the arming; there is nothing to record here.
        fn set_time_per_div(&mut self, _seconds: f64) -> anyhow::Result<()> {
            Ok(())
        }

        // Read by the state the loop publishes after every change, so a
        // fake that panicked on them could not be driven through a setter.
        fn is_channel_enabled(&self, _channel: ChannelId) -> anyhow::Result<bool> {
            Ok(true)
        }
        fn get_volts_per_div(&self, _channel: ChannelId) -> anyhow::Result<f64> {
            Ok(1.0)
        }
        fn get_volts_offset(&self, _channel: ChannelId) -> anyhow::Result<f64> {
            Ok(0.0)
        }
        fn get_coupling(&self, _channel: ChannelId) -> anyhow::Result<Coupling> {
            Ok(Coupling::DC)
        }
        fn get_attenuation(&self, _channel: ChannelId) -> anyhow::Result<f64> {
            Ok(1.0)
        }
        fn get_trigger_level(&self) -> anyhow::Result<f64> {
            Ok(0.0)
        }
        fn get_time_per_div(&self) -> anyhow::Result<f64> {
            Ok(1e-3)
        }
        fn get_time_offset(&self) -> anyhow::Result<f64> {
            Ok(0.0)
        }
        fn get_trigger_source(&self) -> anyhow::Result<ChannelId> {
            Ok(ChannelId::Alphabetic('A'))
        }
        fn get_trigger_slope(&self) -> anyhow::Result<TriggerSlope> {
            Ok(TriggerSlope::Rising)
        }
        fn get_sample_rate(&self) -> anyhow::Result<f64> {
            Ok(1e6)
        }
        /// The whole memory, which is not what a capture holds.
        fn get_memory_depth(&self) -> anyhow::Result<usize> {
            Ok(8000)
        }
        fn current_memory_depth(&self) -> anyhow::Result<usize> {
            Ok(2)
        }
        fn capabilities(&self) -> anyhow::Result<ScopeCapabilities> {
            Ok(fake_capabilities())
        }

        unused! {
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
            get_bandwidth() -> f64;
            get_channel_count() -> usize;
        }

        unused_mut! {
            enable_channel(ChannelId);
            disable_channel(ChannelId);
            set_volts_per_div(ChannelId, f64);
            set_volts_offset(ChannelId, f64);
            set_coupling(ChannelId, Coupling);
            set_attenuation(ChannelId, f64);
            set_trigger_level(f64);
            set_time_offset(f64);
            set_trigger_source(ChannelId);
            set_trigger_slope(TriggerSlope);
            set_capture_mode(CaptureMode);
            set_cursor_position(crate::oscilloscope::Cursor);
        }
    }

    /// `handle` plus the state the loop would own, for driving requests
    /// without standing up a thread.
    struct Harness {
        scope: Box<dyn Oscilloscope>,
        ready: Arc<AtomicBool>,
        captures_read: Arc<AtomicUsize>,
        arms: Arc<AtomicUsize>,
        acquiring: AtomicBool,
        state: LoopState,
    }

    impl Harness {
        fn new() -> Self {
            Harness::with(FakeScope::new())
        }

        fn with(fake: FakeScope) -> Self {
            let ready = fake.ready.clone();
            let captures_read = fake.captures_read.clone();
            let arms = fake.arms.clone();
            Harness {
                scope: Box::new(fake),
                ready,
                captures_read,
                arms,
                acquiring: AtomicBool::new(false),
                state: LoopState::new(),
            }
        }

        /// Captures armed so far.
        fn arms(&self) -> usize {
            self.arms.load(Ordering::SeqCst)
        }

        fn send(&mut self, request: ScopeRequest) -> ScopeReply {
            handle(&mut self.scope, request, &self.acquiring, &mut self.state)
        }

        /// As the loop sends it, with the re-arm after a refusal.
        fn serve(&mut self, request: ScopeRequest) -> ScopeReply {
            serve(&mut self.scope, &self.acquiring, &mut self.state, request).0
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

    }

    #[test]
    fn an_idle_scope_with_no_capture_at_its_settings_says_so() {
        // It used to be read out instead, handing back whatever block the
        // unit last held: one taken before the settings changed, labelled
        // with the new ones, or the same block on every call.
        let mut harness = Harness::new();
        harness.ready.store(true, Ordering::Relaxed);

        let message = error_from(harness.send(ScopeRequest::GetTriggeredData));
        assert!(message.contains("start the scope"), "say what to do: {message}");
        assert!(matches!(
            harness.send(ScopeRequest::MeasureAll {
                channel: ChannelId::Alphabetic('A'),
            }),
            ScopeReply::Error(_)
        ));
        assert_eq!(harness.captures_read(), 0, "the device was read");
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
            screen_samples: 0,
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
        // Nor is the device read for one: it is mid-block. In the loop the
        // request waits for the next capture; here it is told to ask again.
        let message = error_from(harness.send(ScopeRequest::GetTriggeredData));
        assert!(message.contains("ask again"), "{message}");
        assert_eq!(harness.captures_read(), 0);
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
    fn readiness_when_idle_is_whether_a_capture_would_be_served() {
        // The driver's flag can describe a block from before the settings
        // changed, which nothing serves: a client that saw it, then asked
        // for the capture, was refused.
        let mut harness = Harness::new();
        harness.ready.store(true, Ordering::Relaxed);
        assert!(!harness.is_ready());

        publish(&mut harness, 1);
        harness.state.captured_since_arm = true;
        assert!(harness.is_ready());
    }

    #[test]
    fn forcing_a_stopped_scope_is_refused() {
        // As on a bench scope. It used to arm a block nothing read, and the
        // capture still being served was the one from before the force.
        let mut harness = Harness::new();
        let message = error_from(harness.serve(ScopeRequest::ForceTrigger));
        assert!(message.contains("stopped"), "{message}");
        assert!(!harness.state.forced);
        assert_eq!(harness.arms(), 0, "nothing should have been armed");
    }

    #[test]
    fn a_refused_force_while_acquiring_leaves_the_scope_armed() {
        let mut fake = FakeScope::new();
        fake.refuse_force = true;
        let mut harness = Harness::with(fake);
        harness.serve(ScopeRequest::StartAcquisition(50.0));
        let armed = harness.arms();

        assert!(matches!(harness.serve(ScopeRequest::ForceTrigger), ScopeReply::Error(_)));
        assert!(!harness.state.forced, "the next capture is not a forced one");
        assert_eq!(harness.arms(), armed + 1, "a refused force has to re-arm the unit");
        assert!(harness.acquiring.load(Ordering::Relaxed));
    }

    #[test]
    fn a_force_during_holdoff_is_captured_rather_than_re_armed_over() {
        let mut harness = Harness::new();
        harness.serve(ScopeRequest::StartAcquisition(50.0));
        harness.state.arm_at = Some(Instant::now() + Duration::from_secs(5));

        assert!(matches!(harness.serve(ScopeRequest::ForceTrigger), ScopeReply::Ok));
        assert_eq!(harness.state.arm_at, None);
    }

    #[test]
    fn a_setting_the_loop_applies_keeps_the_holdoff_wait() {
        // The block from before the wait is still flagged ready on the unit:
        // dropping the wait had the loop read it out again and publish the
        // same capture twice.
        let mut harness = Harness::new();
        harness.serve(ScopeRequest::StartAcquisition(50.0));
        let due = Instant::now() + Duration::from_secs(5);
        harness.state.arm_at = Some(due);

        harness.serve(ScopeRequest::SetAcquisition(AcquisitionMode::Average, Some(16)));
        assert_eq!(harness.state.arm_at, Some(due));

        // One the driver applies re-arms the unit, which ends the wait.
        harness.serve(ScopeRequest::SetTimePerDiv(1e-3));
        assert_eq!(harness.state.arm_at, None);
    }

    fn waiting_for(request: ScopeRequest, deadline: Instant) -> (Waiting, oneshot::Receiver<ScopeReply>) {
        let (reply_to, reply) = oneshot::channel();
        (Waiting { request, reply_to, deadline }, reply)
    }

    #[test]
    fn a_wait_for_a_capture_that_never_comes_says_why() {
        let mut harness = Harness::new();
        harness.serve(ScopeRequest::StartAcquisition(50.0));
        let now = Instant::now();
        let (expired, mut expired_reply) = waiting_for(ScopeRequest::GetTriggeredData, now);
        let (patient, mut patient_reply) =
            waiting_for(ScopeRequest::GetTriggeredData, now + Duration::from_secs(60));
        let mut waiting = vec![expired, patient];

        settle_waiting(&mut harness.scope, &harness.acquiring, &mut harness.state, &mut waiting, true);

        let message = error_from(expired_reply.try_recv().expect("answered"));
        assert!(message.contains("trigger level"), "{message}");
        assert!(patient_reply.try_recv().is_err(), "still inside its wait");
        assert_eq!(waiting.len(), 1);
    }

    #[test]
    fn waiting_requests_are_answered_with_the_capture_that_arrives() {
        let mut harness = Harness::new();
        harness.serve(ScopeRequest::StartAcquisition(50.0));
        let (held, mut reply) =
            waiting_for(ScopeRequest::GetTriggeredData, Instant::now() + Duration::from_secs(60));
        let mut waiting = vec![held];
        assert!(waits_for_a_capture(
            &ScopeRequest::GetTriggeredData,
            &harness.state,
            &harness.acquiring
        ));

        publish(&mut harness, 4);
        answer_waiting(&mut harness.scope, &harness.acquiring, &mut harness.state, &mut waiting);

        match reply.try_recv().expect("answered") {
            ScopeReply::Capture(frame) => assert_eq!(frame.samples, vec![-4, 4]),
            other => panic!("expected the capture, got {other:?}"),
        }
        assert!(waiting.is_empty());
    }

    #[test]
    fn stopping_answers_the_requests_waiting_for_a_capture() {
        let mut harness = Harness::new();
        let (held, mut reply) =
            waiting_for(ScopeRequest::GetTriggeredData, Instant::now() + Duration::from_secs(60));
        let mut waiting = vec![held];

        settle_waiting(&mut harness.scope, &harness.acquiring, &mut harness.state, &mut waiting, false);

        let message = error_from(reply.try_recv().expect("answered"));
        assert!(message.contains("start the scope"), "{message}");
    }

    #[test]
    fn only_requests_for_samples_wait() {
        let mut harness = Harness::new();
        harness.serve(ScopeRequest::StartAcquisition(50.0));
        assert!(!waits_for_a_capture(&ScopeRequest::IsReady, &harness.state, &harness.acquiring));
        assert!(!waits_for_a_capture(&ScopeRequest::GetState, &harness.state, &harness.acquiring));

        publish(&mut harness, 1);
        assert!(
            !waits_for_a_capture(&ScopeRequest::GetTriggeredData, &harness.state, &harness.acquiring),
            "there is a capture to answer with"
        );
    }

    #[test]
    fn memory_depth_is_the_block_a_capture_holds() {
        let mut harness = Harness::new();
        assert!(matches!(harness.send(ScopeRequest::GetMemoryDepth), ScopeReply::Usize(2)));
    }

    #[test]
    fn an_oversized_display_state_is_refused_and_changes_nothing() {
        let mut harness = Harness::new();
        harness.send(ScopeRequest::SetDisplay(serde_json::json!({"xy": true})));
        let blob = "x".repeat(MAX_DISPLAY_BYTES);

        let message = error_from(harness.send(ScopeRequest::SetDisplay(serde_json::json!({"notes": blob}))));
        assert!(message.contains("limit"), "{message}");
        match harness.send(ScopeRequest::GetDisplay) {
            ScopeReply::Display(display) => assert_eq!(display, serde_json::json!({"xy": true})),
            other => panic!("expected the display back, got {other:?}"),
        }
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

    #[test]
    fn a_setting_that_re_arms_discards_the_published_capture() {
        // Only StartAcquisition used to say so, and a setter that re-arms
        // inside the driver never reaches it -- so a measurement taken after
        // a new timebase was answered from the window before it.
        for request in [
            ScopeRequest::SetTimePerDiv(1e-3),
            ScopeRequest::SetTimeOffset(0.0),
            ScopeRequest::SetVoltsPerDiv(ChannelId::Alphabetic('A'), 1.0),
            ScopeRequest::SetVoltsOffset(ChannelId::Alphabetic('A'), 0.0),
            ScopeRequest::SetCoupling(ChannelId::Alphabetic('A'), Coupling::DC),
            ScopeRequest::SetAttenuation(ChannelId::Alphabetic('A'), 10.0),
            ScopeRequest::SetTriggerLevel(1.0),
            ScopeRequest::SetTriggerSource(ChannelId::Alphabetic('A')),
            ScopeRequest::SetTriggerSlope(TriggerSlope::Rising),
            ScopeRequest::SetCaptureMode(CaptureMode::Normal),
            ScopeRequest::EnableChannel(ChannelId::Alphabetic('B')),
            ScopeRequest::DisableChannel(ChannelId::Alphabetic('B')),
            ScopeRequest::StartAcquisition(50.0),
        ] {
            assert!(
                invalidates_the_published_capture(&request),
                "{request:?} re-arms the scope, so the published capture is stale"
            );
        }
    }

    #[test]
    fn reading_and_stopping_keep_the_published_capture() {
        // A stopped scope is still showing its last capture, and measuring it
        // is reading the held trace -- so a stop must not throw it away.
        for request in [
            ScopeRequest::StopAcquisition,
            ScopeRequest::IsReady,
            ScopeRequest::GetTriggeredData,
            ScopeRequest::GetTimePerDiv,
            ScopeRequest::GetCaptureMode,
            ScopeRequest::GetCapabilities,
            ScopeRequest::MeasureAll {
                channel: ChannelId::Alphabetic('A'),
            },
        ] {
            assert!(
                !invalidates_the_published_capture(&request),
                "{request:?} does not change what a capture would contain"
            );
        }
    }

    #[test]
    fn a_new_timebase_invalidates_the_capture_taken_under_the_old_one() {
        // The whole point, through the handler rather than the predicate: a
        // client that changes the timebase and measures must not be answered
        // from the window it just replaced.
        let mut harness = Harness::new();
        harness.send(ScopeRequest::StartAcquisition(50.0));
        publish(&mut harness, 100);
        harness.state.captured_since_arm = true;

        harness.send(ScopeRequest::SetTimePerDiv(1e-3));

        assert!(
            harness.state.last_frame.is_none(),
            "a capture from the previous timebase was left servable"
        );
        assert!(!harness.state.captured_since_arm);
    }

    /// A handle in `state`, whose command channel is already closed.
    ///
    /// Closed deliberately: if a request ever reaches the channel these tests
    /// get "thread is not running" rather than the state's own message, so
    /// they cannot pass by accident when the short-circuit is removed.
    fn handle_in(state: OpenState) -> ScopeHandle {
        let (commands, rx) = mpsc::channel::<Message>(1);
        drop(rx);
        let (captures, _) = broadcast::channel(1);
        let (_state_tx, state_rx) = watch::channel(Arc::new(unopened_state()));
        ScopeHandle {
            commands,
            captures,
            acquiring: Arc::new(AtomicBool::new(false)),
            capture_count: Arc::new(AtomicU64::new(0)),
            open_state: Arc::new(Mutex::new(state)),
            state: state_rx,
            stopping: Arc::new(AtomicBool::new(false)),
            thread: Arc::new(Mutex::new(None)),
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
        let handle = handle_in(OpenState::opening(None));

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
    async fn a_retry_in_progress_says_why_the_last_attempt_failed() {
        // Each retry used to replace the reason with "still opening", so
        // for most of every retry interval the daemon hid why it had no scope.
        let handle = handle_in(OpenState::opening(Some("no PicoScope found".into())));

        let message = error_from(handle.request(ScopeRequest::IsReady).await);
        assert!(message.contains("no PicoScope found"), "{message:?}");
    }

    /// Opens a fresh `FakeScope` each time, made by `make`, and counts the opens.
    fn opening_fakes(
        opens: Arc<AtomicUsize>,
        mut make: impl FnMut(usize) -> FakeScope + Send + 'static,
    ) -> impl FnMut() -> anyhow::Result<Box<dyn Oscilloscope>> + Send + 'static {
        move || {
            let nth = opens.fetch_add(1, Ordering::SeqCst);
            Ok(Box::new(make(nth)) as Box<dyn Oscilloscope>)
        }
    }

    /// Wait, up to five seconds, for `done`.
    fn eventually(what: &str, done: impl Fn() -> bool) {
        let until = Instant::now() + Duration::from_secs(5);
        while !done() {
            assert!(Instant::now() < until, "timed out waiting for {what}");
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    #[test]
    fn a_request_for_samples_waits_for_the_capture_it_armed() {
        // Arm, then ask, straight away: answered with the capture that arm
        // produced, not refused for being early and not the block before it.
        let fake = FakeScope::new();
        let ready = fake.ready.clone();
        let mut fake = Some(fake);
        let handle = spawn(move || Ok(Box::new(fake.take().expect("opened once")) as Box<dyn Oscilloscope>))
            .expect("spawn");
        let runtime = tokio::runtime::Runtime::new().unwrap();

        runtime.block_on(async {
            assert!(matches!(handle.request(ScopeRequest::StartAcquisition(50.0)).await, ScopeReply::Ok));
            let asked = tokio::spawn({
                let handle = handle.clone();
                async move { handle.request(ScopeRequest::GetTriggeredData).await }
            });
            tokio::time::sleep(Duration::from_millis(100)).await;
            assert!(!asked.is_finished(), "answered before any capture completed");

            ready.store(true, Ordering::Relaxed);
            let reply = tokio::time::timeout(Duration::from_secs(5), asked)
                .await
                .expect("the capture should have answered it")
                .unwrap();
            match reply {
                ScopeReply::Capture(frame) => assert!(frame.seq >= 1),
                other => panic!("expected a capture, got {other:?}"),
            }
        });
        assert!(handle.shutdown(Duration::from_secs(5)));
    }

    #[test]
    fn shutting_down_stops_the_scope_and_closes_it() {
        // A daemon killed with the unit armed left it to the kernel to tidy
        // up, and the next start could find it still claimed.
        let fake = FakeScope::new();
        let dropped = fake.dropped.clone();
        let mut fake = Some(fake);
        let handle = spawn(move || Ok(Box::new(fake.take().expect("opened once")) as Box<dyn Oscilloscope>))
            .expect("spawn");
        let runtime = tokio::runtime::Runtime::new().unwrap();
        runtime.block_on(handle.request(ScopeRequest::StartAcquisition(50.0)));

        assert!(handle.shutdown(Duration::from_secs(5)), "the thread should have finished");
        assert!(dropped.load(Ordering::SeqCst), "the scope was not closed");
        assert!(matches!(
            runtime.block_on(handle.request(ScopeRequest::IsReady)),
            ScopeReply::Error(_)
        ));
    }

    #[test]
    fn shutting_down_between_attempts_to_open_does_not_wait_out_the_retry() {
        let handle = spawn(|| anyhow::bail!("nothing plugged in")).expect("spawn");
        let started = Instant::now();
        assert!(handle.shutdown(Duration::from_secs(5)));
        assert!(started.elapsed() < OPEN_RETRY_INTERVAL, "took {:?}", started.elapsed());
    }

    #[test]
    fn a_panic_in_the_driver_closes_the_scope_and_opens_it_again() {
        // A panic used to end the thread, leaving a daemon that answered
        // every command with "not running" until someone restarted it.
        let opens = Arc::new(AtomicUsize::new(0));
        let first_dropped = Arc::new(AtomicBool::new(false));
        let flag = first_dropped.clone();
        let handle = spawn(opening_fakes(opens.clone(), move |nth| {
            let mut fake = FakeScope::new();
            if nth == 0 {
                fake.panic_on_mode = true;
                fake.dropped = flag.clone();
            }
            fake
        }))
        .expect("spawn");

        eventually("a second open", || opens.load(Ordering::SeqCst) >= 2);
        eventually("the scope to be open", || matches!(*lock(&handle.open_state), OpenState::Open));
        assert!(first_dropped.load(Ordering::SeqCst), "the panicked scope was not closed");
        let runtime = tokio::runtime::Runtime::new().unwrap();
        assert!(matches!(
            runtime.block_on(handle.request(ScopeRequest::GetCapabilities)),
            ScopeReply::Capabilities(_)
        ));
        assert!(handle.shutdown(Duration::from_secs(5)));
    }

    #[test]
    fn a_unit_that_stops_answering_is_closed_and_opened_again() {
        // Unplugged mid-acquisition, the loop polled the dead handle for
        // ever; plugging the unit back in did nothing until a restart.
        let opens = Arc::new(AtomicUsize::new(0));
        let first = FakeScope::new();
        let (ready, gone, first_dropped) = (first.ready.clone(), first.gone.clone(), first.dropped.clone());
        let mut first = Some(first);
        let handle = spawn(opening_fakes(opens.clone(), move |_| {
            first.take().unwrap_or_else(|| {
                let fake = FakeScope::new();
                fake.ready.store(true, Ordering::Relaxed);
                fake
            })
        }))
        .expect("spawn");
        let runtime = tokio::runtime::Runtime::new().unwrap();

        ready.store(true, Ordering::Relaxed);
        runtime.block_on(handle.request(ScopeRequest::StartAcquisition(50.0)));
        eventually("a capture", || handle.capture_count.load(Ordering::Relaxed) >= 3);
        let version = handle.state.borrow().version;

        gone.store(true, Ordering::SeqCst);
        eventually("a second open", || opens.load(Ordering::SeqCst) >= 2);
        eventually("the scope to be open", || matches!(*lock(&handle.open_state), OpenState::Open));
        assert!(first_dropped.load(Ordering::SeqCst), "the lost scope was not closed");
        assert!(handle.state.borrow().version > version, "the state went backwards");

        // And numbering carries on from the scope before, so a client does
        // not take the new unit's first captures for ones it already has.
        let before = handle.capture_count.load(Ordering::Relaxed);
        runtime.block_on(handle.request(ScopeRequest::StartAcquisition(50.0)));
        let frame = runtime.block_on(async {
            let mut captures = handle.subscribe();
            tokio::time::timeout(Duration::from_secs(5), captures.recv())
                .await
                .expect("a capture from the new scope")
                .expect("the stream is open")
        });
        assert!(frame.frame.seq > before, "seq {} after {before}", frame.frame.seq);
        assert!(handle.shutdown(Duration::from_secs(5)));
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

    fn fake_capabilities() -> ScopeCapabilities {
        ScopeCapabilities {
            family: protocol::DriverFamily::Ps2000,
            model: "2204A".into(),
            serial: "TEST/1".into(),
            analog_channels: 1,
            channel_labels: vec!["A".into()],
            resolution: protocol::capabilities::ResolutionSupport::fixed(8),
            voltage_ranges: Vec::new(),
            max_sample_rate_hz: 1e8,
            max_memory_samples: 8000,
            bandwidth_hz: None,
            analog_offset: false,
            bandwidth_limiter: false,
            digital_ports: 0,
            rapid_block: false,
            streaming_mode: false,
            smart_probes: false,
            signal_generator: None,
            advanced_triggers: Vec::new(),
            roll_mode: false,
            peak_detect: false,
        }
    }

    /// A frame of one channel at 1 V a count, crossing from -1000 to +1000
    /// at `edge`, with the trigger point at `pre`.
    fn edge_frame(len: usize, edge: usize, pre: u32) -> CaptureFrame {
        CaptureFrame {
            seq: 0,
            capture_mono_ns: 0,
            sample_interval_ns: 1000.0,
            pre_trigger_samples: pre,
            post_trigger_samples: len as u32 - pre,
            samples_per_channel: len as u32,
            resolution_bits: 8,
            overflow_mask: 0,
            screen_samples: 0,
            flags: FLAG_TRIGGERED,
            channels: vec![ChannelFrame {
                channel: ChannelId::Alphabetic('A'),
                range_code: 0,
                coupling: Coupling::DC,
                scale_v_per_count: 1.0,
                offset_v: 0.0,
            }],
            samples: (0..len).map(|i| if i >= edge { 1000 } else { -1000 }).collect(),
        }
    }

    fn loop_state(mode: CaptureMode) -> LoopState {
        let mut state = LoopState::new();
        state.trigger = TriggerCache {
            mode,
            source: ChannelId::Alphabetic('A'),
            level: 0.0,
            slope: TriggerSlope::Rising,
        };
        state
    }

    #[test]
    fn a_block_completed_in_normal_mode_triggered() {
        // Normal waits for the trigger, so completing is triggering -- even
        // when the samples do not show it clearly.
        let mut state = loop_state(CaptureMode::Normal);
        let frame = state.process(edge_frame(100, 10, 50));
        assert!(frame.is_triggered());
    }

    #[test]
    fn an_auto_block_is_triggered_only_if_it_crosses_at_the_trigger_point() {
        // A driver used to stamp every frame triggered, so a scope that was
        // auto-triggering on nothing showed "Trig'd" over a sliding trace.
        let mut state = loop_state(CaptureMode::Auto);
        assert!(state.process(edge_frame(100, 50, 50)).is_triggered());
        assert!(!state.process(edge_frame(100, 10, 50)).is_triggered());
    }

    #[test]
    fn a_forced_capture_is_judged_by_its_samples_even_in_normal() {
        let mut state = loop_state(CaptureMode::Normal);
        state.forced = true;
        assert!(!state.process(edge_frame(100, 10, 50)).is_triggered());
        // And only that one capture: the next is an ordinary normal block.
        assert!(state.process(edge_frame(100, 10, 50)).is_triggered());
    }

    #[test]
    fn processing_numbers_captures_in_order() {
        let mut state = loop_state(CaptureMode::Normal);
        let first = state.process(edge_frame(10, 5, 5)).seq;
        let second = state.process(edge_frame(10, 5, 5)).seq;
        assert_eq!(second, first + 1);
    }

    fn constant_frame(value: i16) -> CaptureFrame {
        let mut frame = edge_frame(4, 0, 2);
        frame.samples = vec![value; 4];
        frame
    }

    #[test]
    fn averaging_settles_on_the_mean_of_alternating_captures() {
        let mut averager = Averager::default();
        let mut last = Vec::new();
        for i in 0..64 {
            let value = if i % 2 == 0 { 1000 } else { -1000 };
            last = averager.feed(constant_frame(value), 16).samples;
        }
        // An exponential average of +1000/-1000 hovers near zero, within
        // one share of the swing.
        assert!(last.iter().all(|&s| s.abs() <= 2000 / 16 + 1), "{last:?}");
    }

    #[test]
    fn averaging_equal_captures_changes_nothing() {
        let mut averager = Averager::default();
        for _ in 0..5 {
            assert_eq!(averager.feed(constant_frame(512), 8).samples, vec![512; 4]);
        }
    }

    #[test]
    fn averaging_starts_over_when_the_block_changes_shape() {
        let mut averager = Averager::default();
        averager.feed(constant_frame(1000), 16);
        let mut longer = edge_frame(8, 0, 4);
        longer.samples = vec![-500; 8];
        // A new length means a new timebase: nothing to average it with.
        assert_eq!(averager.feed(longer, 16).samples, vec![-500; 8]);
    }

    #[test]
    fn a_setting_change_restarts_the_average() {
        let mut harness = Harness::new();
        harness.state.acquisition = AcquisitionMode::Average;
        harness.state.averager.feed(constant_frame(1000), 16);
        assert!(harness.state.averager.taken > 0);
        harness.send(ScopeRequest::SetTimePerDiv(1e-3));
        assert_eq!(harness.state.averager.taken, 0);
    }

    #[test]
    fn no_holdoff_arms_straight_away() {
        let ready = Instant::now();
        assert_eq!(holdoff_deadline(ready, &edge_frame(100, 50, 50), Duration::ZERO), ready);
    }

    #[test]
    fn holdoff_counts_from_the_trigger_not_from_the_end_of_the_block() {
        // 50 post-trigger samples at 1 us: the trigger was 50 us before the
        // block finished, so a 1 ms holdoff is due 950 us after it.
        let ready = Instant::now() + Duration::from_secs(1);
        let due = holdoff_deadline(ready, &edge_frame(100, 50, 50), Duration::from_millis(1));
        assert_eq!(due - ready, Duration::from_micros(950));
    }

    #[test]
    fn holdoff_outside_its_range_is_refused() {
        let mut harness = Harness::new();
        for bad in [-1.0, f64::NAN, MAX_HOLDOFF_S + 1.0] {
            assert!(matches!(harness.send(ScopeRequest::SetHoldoff(bad)), ScopeReply::Error(_)));
        }
        assert!(matches!(harness.send(ScopeRequest::SetHoldoff(0.25)), ScopeReply::Ok));
        match harness.send(ScopeRequest::GetHoldoff) {
            ScopeReply::Float(s) => assert!((s - 0.25).abs() < 1e-12),
            other => panic!("expected the holdoff back, got {other:?}"),
        }
    }

    #[test]
    fn an_average_count_outside_its_range_is_refused() {
        let mut harness = Harness::new();
        for bad in [0, MAX_AVERAGE_COUNT + 1] {
            assert!(matches!(
                harness.send(ScopeRequest::SetAcquisition(AcquisitionMode::Average, Some(bad))),
                ScopeReply::Error(_)
            ));
        }
        assert!(matches!(
            harness.send(ScopeRequest::SetAcquisition(AcquisitionMode::Average, Some(64))),
            ScopeReply::Ok
        ));
        assert!(matches!(
            harness.send(ScopeRequest::GetAcquisition),
            ScopeReply::Acquisition(AcquisitionMode::Average, 64)
        ));
    }

    #[test]
    fn peak_detect_is_refused_on_a_scope_without_it_and_says_why() {
        let mut harness = Harness::new();
        match harness.send(ScopeRequest::SetAcquisition(AcquisitionMode::Peak, None)) {
            ScopeReply::Error(message) => {
                assert!(message.contains("2204A"), "name the unit: {message}");
                assert!(message.contains("one sample per interval"), "say why: {message}");
            }
            other => panic!("peak detect was accepted: {other:?}"),
        }
        // And nothing changed.
        assert!(matches!(
            harness.send(ScopeRequest::GetAcquisition),
            ScopeReply::Acquisition(AcquisitionMode::Normal, _)
        ));
    }

    #[test]
    fn display_settings_merge_and_come_back() {
        let mut harness = Harness::new();
        harness.send(ScopeRequest::SetDisplay(serde_json::json!({"persistence": 0.5})));
        harness.send(ScopeRequest::SetDisplay(serde_json::json!({"xy": true})));
        match harness.send(ScopeRequest::GetDisplay) {
            ScopeReply::Display(display) => {
                assert_eq!(display, serde_json::json!({"persistence": 0.5, "xy": true}))
            }
            other => panic!("expected the display back, got {other:?}"),
        }
    }

    #[test]
    fn the_state_describes_the_scope_and_the_loop() {
        let mut harness = Harness::new();
        harness.send(ScopeRequest::SetHoldoff(0.1));
        harness.send(ScopeRequest::SetDisplay(serde_json::json!({"xy": true})));
        let state = match harness.send(ScopeRequest::GetState) {
            ScopeReply::State(state) => state,
            other => panic!("expected a state, got {other:?}"),
        };
        assert_eq!(state.channels.len(), 1);
        assert_eq!(state.channels[0].channel, ChannelId::Alphabetic('A'));
        assert!((state.trigger.holdoff_s - 0.1).abs() < 1e-12);
        assert_eq!(state.display, serde_json::json!({"xy": true}));
        assert!(!state.rolling);
        assert!((state.timebase.sample_interval_ns - 1000.0).abs() < 1e-9);
    }

    #[test]
    fn what_changes_the_state_is_what_a_client_can_see_move() {
        for request in [
            ScopeRequest::SetTimePerDiv(1e-3),
            ScopeRequest::StopAcquisition,
            ScopeRequest::SetHoldoff(0.0),
            ScopeRequest::SetDisplay(serde_json::json!({})),
            ScopeRequest::SetAcquisition(AcquisitionMode::Normal, None),
            ScopeRequest::SetRoll(RollMode::Off),
        ] {
            assert!(changes_state(&request), "{request:?}");
        }
        for request in [ScopeRequest::GetState, ScopeRequest::IsReady, ScopeRequest::GetDisplay] {
            assert!(!changes_state(&request), "{request:?}");
        }
    }

    fn roll_info(channels: usize) -> RollInfo {
        RollInfo {
            bucket_ns: 1000.0,
            channels: (0..channels)
                .map(|i| ChannelFrame {
                    channel: ChannelId::Alphabetic((b'A' + i as u8) as char),
                    range_code: 0,
                    coupling: Coupling::DC,
                    scale_v_per_count: 1.0,
                    offset_v: 0.0,
                })
                .collect(),
            resolution_bits: 8,
        }
    }

    #[test]
    fn a_roll_screen_fills_from_the_right() {
        let mut roller = Roller::new(roll_info(1), 0.05);
        roller.sink.pairs = vec![vec![(-5, 5), (-6, 6)]];
        assert_eq!(roller.absorb(), 2);

        let frame = roller.snapshot(false);
        assert!(frame.is_envelope());
        assert_eq!(frame.samples_per_channel as usize, ROLL_COLUMNS * 2);
        assert_eq!(frame.screen_samples, 0, "a screen alone is all screen");
        let tail = &frame.samples[frame.samples.len() - 4..];
        assert_eq!(tail, &[-5, 5, -6, 6], "newest pairs sit at the right edge");
        assert!(frame.samples[..frame.samples.len() - 4].iter().all(|&s| s == NO_SAMPLE));
        // Now is the right edge.
        assert_eq!(frame.pre_trigger_samples, frame.samples_per_channel);
    }

    #[test]
    fn a_full_roll_screen_keeps_only_the_newest_columns() {
        let mut roller = Roller::new(roll_info(2), 0.05);
        let pairs: Vec<(i16, i16)> = (0..ROLL_COLUMNS as i16 + 10).map(|i| (i, i)).collect();
        roller.sink.pairs = vec![pairs.clone(), pairs];
        roller.absorb();
        let frame = roller.snapshot(false);
        let per_channel = ROLL_COLUMNS * 2;
        assert_eq!(frame.samples.len(), per_channel * 2);
        assert_eq!(frame.samples[0], 10, "the oldest ten pairs scrolled off");
        assert_eq!(frame.samples[per_channel - 1], ROLL_COLUMNS as i16 + 9);
    }

    #[test]
    fn a_streamed_screen_carries_history_before_its_left_edge() {
        // 1 us pairs: 250 ms of history is more than a second screen, so a
        // second screen is what is kept.
        let mut roller = Roller::new(roll_info(1), 0.05);
        assert_eq!(roller.history, ROLL_COLUMNS);
        let total = ROLL_COLUMNS * 3;
        let pairs: Vec<(i16, i16)> = (0..total as i16).map(|i| (i, i)).collect();
        roller.sink.pairs = vec![pairs];
        roller.absorb();

        let streamed = roller.snapshot(true);
        assert_eq!(streamed.samples_per_channel as usize, (ROLL_COLUMNS + roller.history) * 2);
        assert_eq!(streamed.screen_samples as usize, ROLL_COLUMNS * 2);
        assert_eq!(*streamed.samples.last().unwrap(), total as i16 - 1);

        let screen = roller.snapshot(false);
        assert_eq!(screen.samples[0], (total - ROLL_COLUMNS) as i16,
                   "the screen alone starts where the streamed screen's history ends");
    }

    #[test]
    fn history_is_a_quarter_screen_at_the_least() {
        let mut info = roll_info(1);
        // 1 s/div: a 10 s screen of 5 ms pairs, where 250 ms is 50 pairs.
        info.bucket_ns = 1.0 * 10.0 * 1e9 / ROLL_COLUMNS as f64;
        assert_eq!(Roller::new(info, 1.0).history, ROLL_COLUMNS / 4);
    }

    #[test]
    fn a_roll_screen_spans_the_time_per_div_it_was_planned_for() {
        let mut info = roll_info(1);
        // 50 ms/div over 2000 columns is 250 us a pair.
        info.bucket_ns = 0.05 * 10.0 * 1e9 / ROLL_COLUMNS as f64;
        let roller = Roller::new(info, 0.05);
        assert!((roller.screen_time_per_div() - 0.05).abs() < 1e-12);
    }

    /// A scope that can roll, recording how it was driven.
    struct RollingScope {
        log: Arc<Mutex<Vec<String>>>,
        time_per_div: f64,
        mode: CaptureMode,
        streamed: u32,
    }

    impl RollingScope {
        fn new(log: Arc<Mutex<Vec<String>>>) -> Self {
            RollingScope { log, time_per_div: 0.1, mode: CaptureMode::Auto, streamed: 0 }
        }
        fn note(&self, entry: &str) {
            self.log.lock().unwrap().push(entry.to_string());
        }
    }

    impl Oscilloscope for RollingScope {
        fn start_triggered_capture(&mut self, _position: f64) -> anyhow::Result<()> {
            self.note("arm");
            Ok(())
        }
        fn stop_triggered_capture(&mut self) -> anyhow::Result<()> {
            self.note("stop");
            Ok(())
        }
        fn is_ready(&self) -> anyhow::Result<bool> {
            Ok(false)
        }
        fn get_triggered_data(&self) -> anyhow::Result<CaptureFrame> {
            anyhow::bail!("no block while rolling")
        }
        fn get_capture_mode(&self) -> anyhow::Result<CaptureMode> {
            Ok(self.mode)
        }
        fn set_capture_mode(&mut self, mode: CaptureMode) -> anyhow::Result<()> {
            self.mode = mode;
            Ok(())
        }
        fn get_trigger_position(&self) -> anyhow::Result<f64> {
            Ok(50.0)
        }
        fn set_time_per_div(&mut self, seconds: f64) -> anyhow::Result<()> {
            self.time_per_div = seconds;
            Ok(())
        }
        fn get_time_per_div(&self) -> anyhow::Result<f64> {
            Ok(self.time_per_div)
        }
        fn supports_roll(&self) -> bool {
            true
        }
        fn start_roll(&mut self, plan: &RollPlan) -> anyhow::Result<RollInfo> {
            self.note(&format!("roll {:.0}", plan.bucket_ns));
            let mut info = roll_info(1);
            info.bucket_ns = plan.bucket_ns;
            Ok(info)
        }
        fn poll_roll(&mut self, sink: &mut RollSink) -> anyhow::Result<usize> {
            self.streamed += 1;
            sink.pairs.resize(1, Vec::new());
            sink.pairs[0].push((-1, 1));
            Ok(1)
        }
        fn stop_roll(&mut self) -> anyhow::Result<()> {
            self.note("unroll");
            Ok(())
        }
        fn is_channel_enabled(&self, _c: ChannelId) -> anyhow::Result<bool> { Ok(true) }
        fn get_volts_per_div(&self, _c: ChannelId) -> anyhow::Result<f64> { Ok(1.0) }
        fn get_volts_offset(&self, _c: ChannelId) -> anyhow::Result<f64> { Ok(0.0) }
        fn get_coupling(&self, _c: ChannelId) -> anyhow::Result<Coupling> { Ok(Coupling::DC) }
        fn get_attenuation(&self, _c: ChannelId) -> anyhow::Result<f64> { Ok(1.0) }
        fn get_trigger_level(&self) -> anyhow::Result<f64> { Ok(0.0) }
        fn set_trigger_level(&mut self, level: f64) -> anyhow::Result<()> {
            // Refused the way the hardware refuses, without re-arming: what
            // the loop has to recover from.
            if level.abs() > 10.0 {
                self.note("stop");
                anyhow::bail!("Voltage out of range");
            }
            Ok(())
        }
        fn get_time_offset(&self) -> anyhow::Result<f64> { Ok(0.0) }
        fn get_trigger_source(&self) -> anyhow::Result<ChannelId> { Ok(ChannelId::Alphabetic('A')) }
        fn get_trigger_slope(&self) -> anyhow::Result<TriggerSlope> { Ok(TriggerSlope::Rising) }
        fn get_sample_rate(&self) -> anyhow::Result<f64> { Ok(1e6) }
        fn get_memory_depth(&self) -> anyhow::Result<usize> { Ok(8000) }
        fn capabilities(&self) -> anyhow::Result<ScopeCapabilities> {
            let mut caps = fake_capabilities();
            caps.roll_mode = true;
            Ok(caps)
        }

        unused! {
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
            get_bandwidth() -> f64;
            get_channel_count() -> usize;
        }

        unused_mut! {
            enable_channel(ChannelId);
            disable_channel(ChannelId);
            set_volts_per_div(ChannelId, f64);
            set_volts_offset(ChannelId, f64);
            set_coupling(ChannelId, Coupling);
            set_attenuation(ChannelId, f64);
            set_time_offset(f64);
            set_trigger_source(ChannelId);
            set_trigger_slope(TriggerSlope);
            set_cursor_position(crate::oscilloscope::Cursor);
            force_trigger();
        }
    }

    /// Drive requests through the steps the loop takes around `handle`.
    fn drive(
        scope: &mut Box<dyn Oscilloscope>,
        acquiring: &AtomicBool,
        state: &mut LoopState,
        request: ScopeRequest,
    ) -> ScopeReply {
        serve(scope, acquiring, state, request).0
    }

    #[test]
    fn a_refused_setting_while_acquiring_leaves_the_scope_armed() {
        // The freeze: a trigger level the hardware refused left the unit
        // stopped while the loop went on polling it.
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut scope: Box<dyn Oscilloscope> = Box::new(RollingScope::new(log.clone()));
        let acquiring = AtomicBool::new(false);
        let mut state = LoopState::new();
        drive(&mut scope, &acquiring, &mut state, ScopeRequest::SetTimePerDiv(1e-3));
        drive(&mut scope, &acquiring, &mut state, ScopeRequest::StartAcquisition(50.0));
        log.lock().unwrap().clear();

        let reply = drive(&mut scope, &acquiring, &mut state, ScopeRequest::SetTriggerLevel(99.0));

        assert!(matches!(reply, ScopeReply::Error(_)), "the refusal is still reported");
        assert!(acquiring.load(Ordering::Relaxed));
        assert_eq!(log.lock().unwrap().last().map(String::as_str), Some("arm"),
                   "a refused setting has to leave the scope armed: {:?}", log.lock().unwrap());
    }

    #[test]
    fn a_refused_setting_while_stopped_arms_nothing() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut scope: Box<dyn Oscilloscope> = Box::new(RollingScope::new(log.clone()));
        let acquiring = AtomicBool::new(false);
        let mut state = LoopState::new();

        let (reply, changed) =
            serve(&mut scope, &acquiring, &mut state, ScopeRequest::SetTriggerLevel(99.0));

        assert!(matches!(reply, ScopeReply::Error(_)));
        assert!(changed, "subscribers are shown the settings that stand");
        assert!(!log.lock().unwrap().iter().any(|e| e == "arm"), "{:?}", log.lock().unwrap());
    }

    #[test]
    fn a_slow_timebase_in_auto_rolls_and_normal_goes_back_to_blocks() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut scope: Box<dyn Oscilloscope> = Box::new(RollingScope::new(log.clone()));
        let acquiring = AtomicBool::new(false);
        let mut state = LoopState::new();

        drive(&mut scope, &acquiring, &mut state, ScopeRequest::StartAcquisition(50.0));
        assert!(state.roller.is_some(), "100 ms/div in auto should roll");
        // 100 ms/div is a 1 s screen, 2000 pairs: 500 us each.
        assert!(log.lock().unwrap().iter().any(|e| e == "roll 500000"), "{:?}", log.lock().unwrap());

        drive(&mut scope, &acquiring, &mut state, ScopeRequest::SetCaptureMode(CaptureMode::Normal));
        assert!(state.roller.is_none(), "normal waits for a trigger roll mode would ignore");
        assert_eq!(log.lock().unwrap().last().map(String::as_str), Some("arm"),
                   "leaving roll mode has to arm a block: {:?}", log.lock().unwrap());
    }

    #[test]
    fn a_fast_timebase_captures_blocks_and_roll_on_forces_streaming() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut scope: Box<dyn Oscilloscope> = Box::new(RollingScope::new(log.clone()));
        let acquiring = AtomicBool::new(false);
        let mut state = LoopState::new();

        drive(&mut scope, &acquiring, &mut state, ScopeRequest::SetTimePerDiv(1e-3));
        drive(&mut scope, &acquiring, &mut state, ScopeRequest::StartAcquisition(50.0));
        assert!(state.roller.is_none(), "1 ms/div is block territory");

        drive(&mut scope, &acquiring, &mut state, ScopeRequest::SetRoll(RollMode::On));
        assert!(state.roller.is_some(), "roll on streams at any timebase");

        drive(&mut scope, &acquiring, &mut state, ScopeRequest::StopAcquisition);
        assert!(state.roller.is_none(), "stopping stops the stream");
        assert!(log.lock().unwrap().iter().any(|e| e == "unroll"));
    }

    #[test]
    fn a_rolling_scope_publishes_whole_screens() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut scope: Box<dyn Oscilloscope> = Box::new(RollingScope::new(log));
        let acquiring = AtomicBool::new(false);
        let mut state = LoopState::new();
        let (captures, mut receiver) = broadcast::channel(4);
        let count = AtomicU64::new(0);

        drive(&mut scope, &acquiring, &mut state, ScopeRequest::StartAcquisition(50.0));
        roll_step(&mut scope, &mut state, &captures, &count).unwrap();

        let published = receiver.try_recv().expect("a screen was published");
        assert!(published.frame.is_envelope());
        assert_eq!(published.frame.seq, state.sequence);
        assert_eq!(published.encoded.len(), published.frame.encoded_len());
        // And it is what a measurement reads.
        assert!(state.last_frame.is_some());
    }

    #[test]
    fn forcing_a_trigger_while_rolling_is_refused_and_the_stream_goes_on() {
        let log = Arc::new(Mutex::new(Vec::new()));
        let mut scope: Box<dyn Oscilloscope> = Box::new(RollingScope::new(log));
        let acquiring = AtomicBool::new(false);
        let mut state = LoopState::new();
        drive(&mut scope, &acquiring, &mut state, ScopeRequest::StartAcquisition(50.0));
        assert!(matches!(
            drive(&mut scope, &acquiring, &mut state, ScopeRequest::ForceTrigger),
            ScopeReply::Error(_)
        ));
        assert!(state.roller.is_some());
    }
}

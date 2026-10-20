// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Rigol MSO5000 series.
//!
//! Settings, measurements and exact samples go over SCPI. The live trace is
//! taken from the scope's HDMI output through a capture card: each frame is
//! the scope's own renderer compositing hundreds of thousands of
//! acquisitions, at the card's frame rate, where reading the screen over
//! USB-TMC manages a tenth of that. Each channel is picked out of the frame
//! by colour, and each pixel column becomes one (minimum, maximum) pair of an
//! envelope frame, scaled by the settings SCPI reports. A scope without a
//! working HDMI picture can be read through its own screenshot instead
//! (`dump`), the same picture at a few frames a second.
//!
//! Nobody looks at this scope's screen, so the daemon sets its display up
//! for the camera, not for a person.

pub mod dump;
pub mod extract;
pub mod frame;
pub mod scpi;
pub mod screen;
pub mod video;

use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::{bail, Context, Result};
use protocol::lscp::{FLAG_ENVELOPE, FLAG_TRIGGERED};
use protocol::{
    AcquisitionMode, CaptureFrame, CaptureMode, ChannelFrame, ChannelId, Coupling, DriverFamily,
    Measurement, MeasurementSet, ScopeCapabilities, TriggerSlope,
};

use self::extract::{Envelopes, COUNTS_PER_DIV};
use self::scpi::{DeviceLock, Scpi};
use self::screen::{measurement_bar, Palette, ScreenMap, HORIZONTAL_DIVISIONS, MSO5000_COLOURS};
use self::video::FrameSource;
use super::{Cursor, Oscilloscope};

/// Where the scope and its capture card are, from the daemon's environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// The VISA address the net stores, or `tcp:<host>[:<port>]`.
    pub scpi: String,
    /// `/dev/videoN`, `usb:<port>` for the card on that USB port, or
    /// `screen` to read the scope's own screenshot over SCPI. None
    /// for a scope with no card: everything but the live view still works.
    pub video: Option<String>,
}

impl Config {
    pub const SCPI_ENV: &'static str = "LAGER_SCOPE_SCPI";
    pub const VIDEO_ENV: &'static str = "LAGER_SCOPE_VIDEO";

    pub fn from_env() -> Result<Config> {
        let scpi = std::env::var(Self::SCPI_ENV)
            .with_context(|| format!("{} names no instrument", Self::SCPI_ENV))?;
        let video = std::env::var(Self::VIDEO_ENV).ok().filter(|v| !v.is_empty());
        Ok(Config { scpi, video })
    }
}

/// Longest a command may take to complete.
const COMPLETION_WAIT: Duration = Duration::from_secs(20);
/// SCPI subsystems whose settings the daemon caches: a raw command to one
/// of these, or one that can change anything, means reading them again.
const CACHED_SUBSYSTEMS: [&str; 8] = [
    ":CHAN", ":TIM", ":TRIG", ":ACQ", ":AUT", ":RUN", "*RST", "*RCL",
];
/// Whether a raw command can change a setting the daemon caches. Every
/// `;`-separated command is checked, with or without its leading colon.
fn changes_cached_settings(command: &str) -> bool {
    command.split(';').any(|part| {
        let part = part.trim().to_ascii_uppercase();
        if part.contains('?') {
            return false;
        }
        CACHED_SUBSYSTEMS.iter().any(|prefix| {
            part.starts_with(prefix)
                || prefix.strip_prefix(':').is_some_and(|bare| part.starts_with(bare))
        })
    })
}

/// How long the cross-process lock is waited for at open.
const LOCK_WAIT: Duration = Duration::from_secs(2);
/// What the scope answers for a measurement it cannot make.
const NO_MEASUREMENT: f64 = 9e36;
/// Longest the scope takes to draw the readout a `:MEASure:<item>?` adds,
/// through the capture card: the reply comes before the screen redraws, and
/// HDMI lags another 190-330 ms.
const READOUT_WAIT: Duration = Duration::from_millis(1500);
/// Samples of the screen record a rise or fall has to span for the record to
/// time it. Under that it is the record's spacing being measured, 10 us at
/// 1 ms/div, not the edge, which the scope times on its full memory.
const RESOLVED_EDGE_SAMPLES: f64 = 3.0;

#[derive(Debug, Clone, Copy, PartialEq)]
struct ChannelCache {
    enabled: bool,
    volts_per_div: f64,
    offset: f64,
    coupling: Coupling,
    probe: f64,
}

#[derive(Debug, Clone, PartialEq)]
struct Settings {
    channels: Vec<ChannelCache>,
    time_per_div: f64,
    time_offset: f64,
    trigger_level: f64,
    /// Zero-based.
    trigger_source: usize,
    trigger_slope: TriggerSlope,
    capture_mode: CaptureMode,
    sample_rate: f64,
}

/// What the capture path changes from `&self`.
#[derive(Default)]
struct Work {
    envelopes: Envelopes,
    /// The newest video frame turned into a capture.
    consumed: u64,
    /// Frames up to this one predate the last change.
    stale_through: u64,
    /// In single, the scope has stopped on its trigger.
    single_done: bool,
    /// Watching for the scope's side menu over the right of the trace.
    menu: MenuWatch,
}

/// Frame times, in the card's nanoseconds, of the watch for the side menu.
/// The menu can come back after pairing hid it, and nothing the daemon
/// sends was found to open it, so the frames are watched for its shape: the
/// right of the area empty while a trace crosses the rest.
#[derive(Default)]
struct MenuWatch {
    /// Since when the right has been empty.
    bare_since: Option<u64>,
    /// When the screen was last checked for the menu.
    checked_at: Option<u64>,
    /// The last check found nothing to hide, so the empty right is the
    /// signal's own; it is not checked again until a trace reaches it.
    ruled_out: bool,
    /// Due to be checked when the scope is next re-armed.
    due: bool,
}

/// How long the right of the area stays empty before the menu is looked for.
const MENU_WATCH_NS: u64 = 1_000_000_000;
/// Least time between two looks: each one stops the scope and redraws its
/// grid twice.
const MENU_CHECK_GAP_NS: u64 = 10_000_000_000;

impl MenuWatch {
    /// Note one frame, taken at `at`, and whether its right was empty.
    fn saw(&mut self, at: u64, right_bare: bool) {
        if !right_bare {
            self.bare_since = None;
            self.ruled_out = false;
            return;
        }
        let since = *self.bare_since.get_or_insert(at);
        let rested = self
            .checked_at
            .is_none_or(|checked| at.saturating_sub(checked) >= MENU_CHECK_GAP_NS);
        if !self.ruled_out && rested && at.saturating_sub(since) >= MENU_WATCH_NS {
            self.due = true;
            self.checked_at = Some(at);
        }
    }

    /// What a check found.
    fn checked(&mut self, side: Side) {
        self.bare_since = None;
        self.ruled_out = side != Side::Uncovered;
    }
}

/// What calibration found on the right of the waveform area.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Side {
    /// Nothing covered it.
    Clear,
    /// The side menu covered it, and is hidden now.
    Uncovered,
    /// Something covered it that hiding the menu did not move.
    StillCovered,
}

pub struct RigolMso5000 {
    /// Shared with the screenshot thread when the live view is read over
    /// SCPI.
    scpi: Arc<Mutex<Box<dyn Scpi>>>,
    /// The capture card and where the waveform is in its picture.
    screen: Option<Screen>,
    /// A capture card that could not be paired, kept to pair again when
    /// acquisition next starts: the card can be missing, or its picture,
    /// from the cable or the HDMI output, and every command still works
    /// without it.
    unpaired: Option<Unpaired>,
    /// Opens a capture card by the spec the net names it with.
    open_card: CardOpener,
    /// Whether pairing may still switch the scope's HDMI output on.
    may_switch_hdmi: bool,
    _lock: Option<DeviceLock>,
    capabilities: ScopeCapabilities,
    firmware: String,
    settings: Settings,
    palette: Palette,
    work: Mutex<Work>,
}

struct Screen {
    video: Box<dyn FrameSource>,
    map: ScreenMap,
}

struct Unpaired {
    /// What to try again.
    retry: Retry,
    /// Why the last pairing failed, with its causes.
    reason: String,
}

enum Retry {
    /// A card that opened but showed no picture of this scope.
    Pair(Box<dyn FrameSource>),
    /// A card that could not be opened at all, by its spec: not on its
    /// port, or held by something else.
    Open(String),
}

/// Opens the capture card a spec names: `video::open`, or a stand-in in tests.
type CardOpener = Box<dyn Fn(&str) -> Result<Box<dyn FrameSource>> + Send + Sync>;

/// The capture card a scope is opened with.
enum Card {
    None,
    Opened(Box<dyn FrameSource>),
    /// Named, but it could not be opened: the spec, and why.
    Unopened { spec: String, reason: String },
}

/// Open the card `spec` names. One that cannot be opened leaves the scope
/// usable, as one with no picture does: every command but the live view
/// works, and the card is tried again when acquisition starts.
fn open_named_card(spec: &str, open_card: &CardOpener) -> Card {
    match open_card(spec) {
        Ok(video) => Card::Opened(video),
        Err(e) => Card::Unopened {
            spec: spec.to_string(),
            reason: format!("opening the capture card {spec}: {e:#}"),
        },
    }
}

/// Pair `video` with the scope, or say why not. A scope that stopped
/// answering is an error; a picture that does not follow the scope is the
/// `Err` inside, so the scope can be used without it.
///
/// With `may_switch_hdmi`, a card that sees no picture at all has the
/// scope's HDMI output switched on, which a power cycle turns off, and the
/// flag is cleared: it is done once per open, so a scope with no card on its
/// HDMI is not sent through its menus at every start.
fn pair(
    link: &Arc<Mutex<Box<dyn Scpi>>>,
    model: &str,
    video: Box<dyn FrameSource>,
    may_switch_hdmi: &mut bool,
) -> Result<std::result::Result<Screen, Unpaired>> {
    let mut calibrated = calibrate(link, video.as_ref());
    let mut switched = false;
    if let Err(e) = &calibrated {
        if *may_switch_hdmi && e.chain().any(|c| c.is::<screen::NoPicture>()) {
            *may_switch_hdmi = false;
            tracing::info!("no picture from the scope; switching its HDMI output on (Utility > IO > HDMI > Output)");
            toggle_hdmi_output(link.as_ref())?;
            std::thread::sleep(HDMI_LOCK_WAIT);
            calibrated = calibrate(link, video.as_ref());
            switched = true;
            if calibrated.as_ref().is_err_and(|e| !super::unit_is_gone(e)) {
                // The output was on already, and the picture is missing for
                // another reason, the cable or the card: put it back.
                toggle_hdmi_output(link.as_ref())?;
                press_key(link.as_ref(), "MOFF")?;
            }
        }
    }
    match calibrated {
        Ok((map, _)) => {
            if switched {
                tracing::info!("switched the scope's HDMI output on");
            }
            tracing::info!(
                video = %video.describe(),
                area = ?(map.left, map.top, map.width, map.height),
                px_per_div = ?(map.px_per_div_x, map.px_per_div_y),
                "measured the waveform area"
            );
            prepare_display(lock_link(link).as_mut());
            Ok(Ok(Screen { video, map }))
        }
        Err(e) if super::unit_is_gone(&e) => Err(e),
        Err(e) => {
            let tried = if switched {
                "; switching the scope's HDMI output on did not bring a picture, so it was left \
                 as it was"
            } else {
                ""
            };
            let reason = format!(
                "pairing the {model} with the capture card {}: {e:#}{tried}",
                video.describe()
            );
            tracing::warn!(error = %reason, "no live view; the scope is usable without it");
            Ok(Err(Unpaired { retry: Retry::Pair(video), reason }))
        }
    }
}

/// What `*IDN?` says.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Identity {
    pub model: String,
    pub serial: String,
    pub firmware: String,
}

impl Identity {
    pub fn parse(idn: &str) -> Result<Identity> {
        let fields: Vec<&str> = idn.trim().split(',').map(str::trim).collect();
        if fields.len() < 4 || !fields[0].to_ascii_uppercase().contains("RIGOL") {
            bail!("the instrument identifies as {idn:?}, not a Rigol");
        }
        let model = fields[1].to_ascii_uppercase();
        if !model.starts_with("MSO5") || model.len() != 7 {
            bail!("the instrument is a {model}; this backend drives the MSO5000 series");
        }
        Ok(Identity {
            model,
            serial: fields[2].to_string(),
            firmware: fields[3].to_string(),
        })
    }

    /// The last digit of the model is its channel count: MSO5074 has four,
    /// MSO5072 two.
    pub fn channels(&self) -> Result<u8> {
        match self.model.as_bytes().last() {
            Some(&d @ (b'2' | b'4')) => Ok(d - b'0'),
            _ => bail!("cannot tell the channel count of a {}", self.model),
        }
    }

    /// The middle digits are the bandwidth in tens of MHz: MSO5204 is 200 MHz.
    pub fn bandwidth_hz(&self) -> Option<f64> {
        self.model
            .get(4..6)
            .and_then(|d| d.parse::<u32>().ok())
            .map(|tens| f64::from(tens) * 10e6)
    }
}

fn channel_index(channel: ChannelId, count: usize) -> Result<usize> {
    let index = match channel {
        ChannelId::Alphabetic(c) => (c as u32).wrapping_sub('A' as u32) as usize,
        ChannelId::Numeric(n) => usize::from(n),
    };
    if index >= count {
        bail!("this scope has channels A to {}, not {channel}", (b'A' + count as u8 - 1) as char);
    }
    Ok(index)
}

fn letter(index: usize) -> ChannelId {
    ChannelId::Alphabetic((b'A' + index as u8) as char)
}

fn parse_f64(reply: &str, what: &str) -> Result<f64> {
    reply
        .trim()
        .parse::<f64>()
        .with_context(|| format!("the scope answered {reply:?} for {what}"))
}

fn parse_coupling(reply: &str) -> Coupling {
    match reply.trim().to_ascii_uppercase().as_str() {
        "AC" => Coupling::AC,
        "GND" => Coupling::GND,
        _ => Coupling::DC,
    }
}

fn parse_slope(reply: &str) -> TriggerSlope {
    match reply.trim().to_ascii_uppercase().as_str() {
        "NEG" | "NEGATIVE" => TriggerSlope::Falling,
        "RFAL" | "RFALL" => TriggerSlope::Either,
        _ => TriggerSlope::Rising,
    }
}

fn parse_sweep(reply: &str) -> CaptureMode {
    match reply.trim().to_ascii_uppercase().as_str() {
        "NORM" | "NORMAL" => CaptureMode::Normal,
        "SING" | "SINGLE" => CaptureMode::Single,
        _ => CaptureMode::Auto,
    }
}

/// `CHAN2` and `CHANnel2` both name channel B.
fn parse_source(reply: &str) -> Option<usize> {
    let digits: String = reply.chars().filter(|c| c.is_ascii_digit()).collect();
    let upper = reply.trim().to_ascii_uppercase();
    if !upper.starts_with("CHAN") {
        return None;
    }
    digits.parse::<usize>().ok().and_then(|n| n.checked_sub(1))
}

/// Volts per division the MSO5000 takes at a 1x probe, 1-2-5 from 500 uV.
const VOLTS_PER_DIV: [f64; 14] = [
    5e-4, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0,
];

fn volts_per_div_ladder() -> Vec<f64> {
    VOLTS_PER_DIV.to_vec()
}

fn capabilities_for(identity: &Identity) -> Result<ScopeCapabilities> {
    let channels = identity.channels()?;
    let voltage_ranges = volts_per_div_ladder()
        .into_iter()
        .enumerate()
        .map(|(code, volts_per_div)| protocol::capabilities::VoltageRange {
            code: code as u8,
            // Full scale is four divisions either side of centre.
            full_scale_volts: volts_per_div * 4.0,
            label: format_volts(volts_per_div * 4.0),
        })
        .collect();
    Ok(ScopeCapabilities {
        family: DriverFamily::RigolMso5000,
        model: identity.model.clone(),
        serial: identity.serial.clone(),
        analog_channels: channels,
        channel_labels: ScopeCapabilities::default_labels(channels),
        resolution: protocol::capabilities::ResolutionSupport::fixed(8),
        voltage_ranges,
        max_sample_rate_hz: 8e9,
        max_memory_samples: 100_000_000,
        bandwidth_hz: identity.bandwidth_hz(),
        analog_offset: true,
        bandwidth_limiter: false,
        digital_ports: 0,
        rapid_block: false,
        streaming_mode: false,
        smart_probes: false,
        signal_generator: None,
        advanced_triggers: Vec::new(),
        roll_mode: false,
        peak_detect: true,
        timebase_steps: protocol::capabilities::TimebaseSteps::OneTwoFive,
    })
}

fn format_volts(volts: f64) -> String {
    if volts < 1.0 {
        format!("+/-{} mV", (volts * 1000.0).round())
    } else {
        format!("+/-{} V", volts)
    }
}

impl RigolMso5000 {
    /// Open the scope and the capture card `config` names.
    pub fn open(config: &Config) -> Result<RigolMso5000> {
        let address = scpi::Address::parse(&config.scpi)?;
        let lock = DeviceLock::acquire(&config.scpi, LOCK_WAIT)?;
        let scpi = scpi::open(&address)
            .with_context(|| format!("opening the scope at {}", config.scpi))?;
        let link = Arc::new(Mutex::new(scpi));
        let open_card: CardOpener = Box::new(video::open);
        let card = match &config.video {
            Some(spec) if spec == dump::SPEC => Card::Opened(Box::new(dump::ScreenDump::start(link.clone())?)),
            Some(spec) => open_named_card(spec, &open_card),
            None => {
                tracing::warn!(
                    "no capture card is paired with this scope, so it has no live view; \
                     settings, measurements and exact samples still work"
                );
                Card::None
            }
        };
        Self::with_link(link, card, open_card, Some(lock))
    }

    /// Open over an established connection and frame source.
    pub fn with(
        scpi: Box<dyn Scpi>,
        video: Option<Box<dyn FrameSource>>,
        lock: Option<DeviceLock>,
    ) -> Result<RigolMso5000> {
        let card = video.map_or(Card::None, Card::Opened);
        Self::with_link(Arc::new(Mutex::new(scpi)), card, Box::new(video::open), lock)
    }

    /// Open over an established connection, with the card `spec` names
    /// opened by `open_card`, as `open` does with the real one.
    #[cfg(test)]
    fn with_card_named(scpi: Box<dyn Scpi>, spec: &str, open_card: CardOpener) -> Result<RigolMso5000> {
        let card = open_named_card(spec, &open_card);
        Self::with_link(Arc::new(Mutex::new(scpi)), card, open_card, None)
    }

    /// Open over a connection that reads its live view from the scope's own
    /// screenshot.
    pub fn with_screen_dump(scpi: Box<dyn Scpi>, lock: Option<DeviceLock>) -> Result<RigolMso5000> {
        let link = Arc::new(Mutex::new(scpi));
        let video = Box::new(dump::ScreenDump::start(link.clone())?);
        Self::with_link(link, Card::Opened(video), Box::new(video::open), lock)
    }

    /// Open over a connection that a frame source may share.
    fn with_link(
        link: Arc<Mutex<Box<dyn Scpi>>>,
        card: Card,
        open_card: CardOpener,
        lock: Option<DeviceLock>,
    ) -> Result<RigolMso5000> {
        let identity = {
            let mut scpi = lock_link(&link);
            // Anything a previous process left in the error queue would be
            // reported against the first command here.
            scpi.write("*CLS")?;
            Identity::parse(&scpi.query("*IDN?")?)?
        };
        let capabilities = capabilities_for(&identity)?;
        let mut may_switch_hdmi = true;
        let (screen, unpaired) = match card {
            Card::Opened(video) => match pair(&link, &identity.model, video, &mut may_switch_hdmi)? {
                Ok(screen) => (Some(screen), None),
                Err(unpaired) => (None, Some(unpaired)),
            },
            Card::Unopened { spec, reason } => {
                tracing::warn!(error = %reason, "no live view; the scope is usable without it");
                (None, Some(Unpaired { retry: Retry::Open(spec), reason }))
            }
            Card::None => (None, None),
        };
        let settings = read_settings(
            lock_link(&link).as_mut(),
            usize::from(capabilities.analog_channels),
        )?;
        tracing::info!(
            model = %identity.model,
            serial = %identity.serial,
            firmware = %identity.firmware,
            live_view = screen.is_some(),
            "opened Rigol scope"
        );
        let settle = screen.as_ref().map_or(0, |s| s.video.settle_frames());
        let newest = screen
            .as_ref()
            .and_then(|s| s.video.newest().ok())
            .unwrap_or(0);
        Ok(RigolMso5000 {
            scpi: link,
            screen,
            unpaired,
            open_card,
            may_switch_hdmi,
            _lock: lock,
            capabilities,
            firmware: identity.firmware,
            settings,
            palette: Palette::new(&MSO5000_COLOURS),
            work: Mutex::new(Work {
                stale_through: newest + settle,
                ..Work::default()
            }),
        })
    }

    pub fn firmware(&self) -> &str {
        &self.firmware
    }

    fn link(&self) -> std::sync::MutexGuard<'_, Box<dyn Scpi>> {
        lock_link(&self.scpi)
    }

    /// Frames to let pass after a change.
    fn settle_frames(&self) -> u64 {
        self.screen.as_ref().map_or(0, |s| s.video.settle_frames())
    }

    fn work(&self) -> std::sync::MutexGuard<'_, Work> {
        self.work.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn index(&self, channel: ChannelId) -> Result<usize> {
        channel_index(channel, self.settings.channels.len())
    }

    /// Send a setting, wait for the scope to finish applying it, and say
    /// what it refused: a bad command raises no error at the SCPI layer, it
    /// only goes into the error queue.
    fn apply(&mut self, command: &str) -> Result<()> {
        {
            let mut scpi = self.link();
            scpi.write(command)?;
            // Long enough for the slowest command, an autoscale or a reset;
            // most finish at once.
            scpi.set_timeout(COMPLETION_WAIT);
            let completed = scpi.query("*OPC?");
            scpi.set_timeout(scpi::DEFAULT_TIMEOUT);
            completed?;
            check_errors(scpi.as_mut(), command)?;
        }
        self.mark_stale();
        Ok(())
    }

    fn query_f64(&self, query: &str) -> Result<f64> {
        let reply = self.link().query(query)?;
        parse_f64(&reply, query)
    }

    /// Run `set` with channel `i` shown. An MSO5000 ignores a vertical scale
    /// or offset sent to a hidden channel, with no error, where a PicoScope
    /// takes it; showing the channel for the change keeps the two alike.
    fn while_shown(&mut self, i: usize, set: impl FnOnce(&mut Self) -> Result<()>) -> Result<()> {
        let n = i + 1;
        let hidden = self.query_f64(&format!(":CHANnel{n}:DISPlay?"))? == 0.0;
        if hidden {
            self.apply(&format!(":CHANnel{n}:DISPlay ON"))?;
        }
        let result = set(self);
        if hidden {
            self.apply(&format!(":CHANnel{n}:DISPlay OFF"))?;
        }
        result
    }

    /// Frames already captured, or on their way, show the screen as it was.
    fn mark_stale(&self) {
        let newest = self
            .screen
            .as_ref()
            .and_then(|s| s.video.newest().ok())
            .unwrap_or(0);
        let settle = self.settle_frames();
        let mut work = self.work();
        work.stale_through = newest + settle;
        work.single_done = false;
    }

    fn refresh_sample_rate(&mut self) {
        if let Ok(rate) = self.query_f64(":ACQuire:SRATe?") {
            self.settings.sample_rate = rate;
        }
    }

    /// Pair the capture card that could not be paired before. Called when
    /// acquisition starts, so plugging the card back in, or turning the
    /// scope's HDMI output back on, and starting again brings the live view
    /// back.
    fn pair_again(&mut self) -> Result<()> {
        let Some(Unpaired { retry, .. }) = self.unpaired.take() else {
            return Ok(());
        };
        let video = match retry {
            Retry::Pair(video) => video,
            Retry::Open(spec) => match open_named_card(&spec, &self.open_card) {
                Card::Opened(video) => video,
                Card::Unopened { spec, reason } => {
                    tracing::warn!(error = %reason, "the capture card still cannot be opened");
                    self.unpaired = Some(Unpaired { retry: Retry::Open(spec), reason });
                    return Ok(());
                }
                Card::None => return Ok(()),
            },
        };
        match pair(&self.scpi, &self.capabilities.model, video, &mut self.may_switch_hdmi)? {
            Ok(screen) => {
                let newest = screen.video.newest().unwrap_or(0);
                let settle = screen.video.settle_frames();
                self.screen = Some(screen);
                let mut work = self.work();
                work.consumed = newest;
                work.stale_through = newest + settle;
            }
            Err(unpaired) => self.unpaired = Some(unpaired),
        }
        Ok(())
    }

    fn screen(&self) -> Result<&Screen> {
        if let Some(unpaired) = &self.unpaired {
            bail!("no live view: {}", unpaired.reason);
        }
        self.screen.as_ref().with_context(|| {
            format!(
                "no capture card is paired with this {}, so it has no live view; add the \
                 card to the scope net",
                self.capabilities.model
            )
        })
    }

    /// Pixel column of the trigger point, counted from the left of the area.
    fn trigger_column(&self, map: &ScreenMap) -> f64 {
        let s = &self.settings;
        let divs = if s.time_per_div > 0.0 {
            s.time_offset / s.time_per_div
        } else {
            0.0
        };
        map.center_x - map.left as f64 - divs * map.px_per_div_x
    }

    fn envelope_frame(&self, raw: &video::RawFrame, triggered: bool) -> Result<CaptureFrame> {
        let map = &self.screen()?.map;
        let frame = raw.decode()?;
        let mut work = self.work();
        let wanted = self
            .settings
            .channels
            .iter()
            .enumerate()
            .filter(|(_, c)| c.enabled)
            .fold(0u8, |mask, (i, _)| mask | (1 << i));
        let trigger_column = self.trigger_column(map);
        // The readouts the user keeps on the scope are drawn over the trace.
        let bar = measurement_bar(&frame, map);
        work.envelopes
            .extract_masked(&frame, map, &self.palette, wanted, Some(trigger_column), bar);
        let right_bare = work.envelopes.right_side_bare(wanted);
        work.menu.saw(raw.captured_ns, right_bare);
        let env = &work.envelopes;
        let columns = env.columns;
        let per_channel = columns * 2;

        let mut channels = Vec::new();
        let mut samples = Vec::with_capacity(per_channel * wanted.count_ones() as usize);
        let mut overflow_mask = 0u16;
        for (index, cache) in self.settings.channels.iter().enumerate() {
            if !cache.enabled {
                continue;
            }
            channels.push(ChannelFrame {
                channel: letter(index),
                range_code: 0,
                coupling: cache.coupling,
                scale_v_per_count: (cache.volts_per_div / COUNTS_PER_DIV) as f32,
                // The scope draws a signal of V volts at V + offset.
                offset_v: -cache.offset as f32,
            });
            samples.extend_from_slice(&env.samples[index]);
            if env.clipped & (1 << index) != 0 {
                overflow_mask |= 1 << index;
            }
        }

        let column_ns = self.settings.time_per_div / map.px_per_div_x * 1e9;
        let pre = (self.trigger_column(map).round() * 2.0).clamp(0.0, per_channel as f64) as u32;
        let mut flags = FLAG_ENVELOPE;
        if triggered {
            flags |= FLAG_TRIGGERED;
        }
        Ok(CaptureFrame {
            seq: 0,
            capture_mono_ns: raw.captured_ns,
            sample_interval_ns: column_ns / 2.0,
            pre_trigger_samples: pre,
            post_trigger_samples: per_channel as u32 - pre,
            samples_per_channel: per_channel as u32,
            resolution_bits: resolution_bits(map.height),
            overflow_mask,
            flags,
            screen_samples: 0,
            channels,
            samples,
        })
    }

    /// Check the screen for the side menu with the grid, as pairing does,
    /// and hide it if it is there. MOFF toggles the menu, so it is pressed
    /// only when the check sees it. A failed check leaves the live view as
    /// it was: it is still the scope's picture, only narrower. A scope that
    /// stopped answering is the error.
    fn hide_side_menu(&mut self) -> Result<()> {
        let Some(screen) = &self.screen else {
            return Ok(());
        };
        let side = match calibrate(&self.scpi, screen.video.as_ref()) {
            Ok((map, side)) => {
                if let Some(screen) = self.screen.as_mut() {
                    screen.map = map;
                }
                side
            }
            Err(e) if super::unit_is_gone(&e) => return Err(e),
            Err(e) => {
                tracing::warn!(error = %format!("{e:#}"), "could not check the scope's screen for its side menu");
                Side::StillCovered
            }
        };
        self.mark_stale();
        self.work().menu.checked(side);
        Ok(())
    }

    /// A query, and when it goes unanswered, what the scope said about it.
    /// An MSO5000 meets a header it does not know with -100 in its error
    /// queue and no reply, so the timeout alone does not say why, and the
    /// error would otherwise be blamed on the next command checked.
    fn query_checked(&self, query: &str) -> Result<String> {
        let mut scpi = self.link();
        match scpi.query(query) {
            Ok(reply) => Ok(reply),
            Err(e) if super::unit_is_gone(&e) => Err(e),
            Err(e) => {
                check_errors(scpi.as_mut(), query)?;
                Err(e.context(format!("the scope did not answer {query}")))
            }
        }
    }

    /// Readouts in the scope's measurement bar, from a frame newer than
    /// `after`, and that frame's sequence. None with no live view to see
    /// the bar in.
    fn readouts_after(&self, after: u64, wait: Duration) -> Option<(usize, u64)> {
        let screen = self.screen().ok()?;
        let raw = screen.video.next_after(after, wait).ok()??;
        let frame = raw.decode().ok()?;
        let tiles = measurement_bar(&frame, &screen.map).map_or(0, |bar| bar.tiles);
        Some((tiles, raw.sequence))
    }

    /// Ask the scope one `:MEASure:<item>?`, and leave its measurement bar
    /// as it was. The query puts the item's readout at the front of the
    /// bar, or moves the user's own there when it is already shown, and
    /// `:MEASure:CLEar ITEM<n>` counts readouts oldest first, so the one a
    /// query added is the last. It is cleared only when the live view sees
    /// the bar gain exactly one: a readout the user put there is never
    /// cleared, and with no live view to look at, nothing is.
    fn measure_query(&mut self, query: &str) -> Result<String> {
        let before = self
            .screen()
            .ok()
            .and_then(|s| s.video.newest().ok())
            .and_then(|newest| self.readouts_after(newest.saturating_sub(1), READOUT_WAIT));
        let reply = self.query_checked(query)?;
        if let Some((before, mut seen)) = before {
            let deadline = Instant::now() + READOUT_WAIT;
            let mut added = None;
            while Instant::now() < deadline {
                let Some((tiles, sequence)) = self.readouts_after(seen, Duration::from_millis(200)) else {
                    break;
                };
                seen = sequence;
                if tiles != before {
                    added = (tiles == before + 1).then_some(tiles);
                    break;
                }
            }
            if let Some(item) = added {
                self.apply(&format!(":MEASure:CLEar ITEM{item}"))?;
            }
        }
        // The frames since show the readout; the bar is left out of the
        // live view anyway, but its redraw is not the signal.
        self.mark_stale();
        Ok(reply)
    }

    fn not_offered(&self, what: &str) -> anyhow::Error {
        anyhow::anyhow!("the {} does not offer {what} through this driver", self.capabilities.model)
    }

    /// The screen record of `channels` (zero-based), 8-bit and exact. It
    /// reads what the scope has on screen and leaves acquisition running.
    fn record(&mut self, channels: &[usize]) -> Result<CaptureFrame> {
        let mut scpi = self.link();
        scpi.write(":WAVeform:MODE NORMal")?;
        scpi.write(":WAVeform:FORMat BYTE")?;
        let mut frames = Vec::new();
        let mut traces: Vec<Vec<i16>> = Vec::new();
        let mut first: Option<Preamble> = None;
        for &i in channels {
            scpi.write(&format!(":WAVeform:SOURce CHANnel{}", i + 1))?;
            let preamble = Preamble::parse(&scpi.query(":WAVeform:PREamble?")?)?;
            let data = scpi.query_block(":WAVeform:DATA?")?;
            let reference = preamble.y_reference;
            traces.push(
                data.iter()
                    .map(|&b| (f64::from(b) - reference).round() as i16)
                    .collect(),
            );
            frames.push(ChannelFrame {
                channel: letter(i),
                range_code: 0,
                coupling: self.settings.channels[i].coupling,
                scale_v_per_count: preamble.y_increment as f32,
                offset_v: (-preamble.y_origin * preamble.y_increment) as f32,
            });
            first.get_or_insert(preamble);
        }
        let preamble = first.context("no channel answered")?;
        let points = traces.iter().map(Vec::len).min().unwrap_or(0);
        let pre = if preamble.x_increment > 0.0 {
            (-preamble.x_origin / preamble.x_increment)
                .round()
                .clamp(0.0, points as f64) as u32
        } else {
            0
        };
        let mut samples = Vec::with_capacity(points * traces.len());
        for trace in &traces {
            samples.extend_from_slice(&trace[..points]);
        }
        if preamble.points != 0 && preamble.points != points {
            tracing::debug!(
                announced = preamble.points,
                read = points,
                "the waveform record was shorter than its preamble said"
            );
        }
        Ok(CaptureFrame {
            seq: 0,
            capture_mono_ns: crate::oscilloscope::monotonic_ns(),
            sample_interval_ns: preamble.x_increment * 1e9,
            pre_trigger_samples: pre,
            post_trigger_samples: points as u32 - pre,
            samples_per_channel: points as u32,
            resolution_bits: 8,
            overflow_mask: 0,
            flags: 0,
            screen_samples: 0,
            channels: frames,
            samples,
        })
    }
}

/// Bits of vertical resolution the picture has: enough to number its rows.
fn resolution_bits(rows: usize) -> u8 {
    (usize::BITS - rows.max(2).saturating_sub(1).leading_zeros()) as u8
}

/// Read the error queue, failing on the first entry that is not "no error".
fn check_errors(scpi: &mut dyn Scpi, after: &str) -> Result<()> {
    let mut errors = Vec::new();
    for _ in 0..8 {
        let reply = scpi.query(":SYSTem:ERRor?")?;
        let code = reply
            .split(',')
            .next()
            .and_then(|c| c.trim().parse::<i32>().ok())
            .unwrap_or(0);
        if code == 0 {
            break;
        }
        errors.push(reply);
    }
    if errors.is_empty() {
        Ok(())
    } else {
        bail!("the scope refused {after}: {}", errors.join("; "))
    }
}

fn lock_link(link: &Mutex<Box<dyn Scpi>>) -> std::sync::MutexGuard<'_, Box<dyn Scpi>> {
    link.lock().unwrap_or_else(|p| p.into_inner())
}

/// Wait until the source has delivered a frame enough frames after the
/// newest one now to show the screen as it is, and decode it.
fn settled_frame(video: &dyn FrameSource) -> Result<frame::VideoFrame> {
    let target = video.newest()? + video.settle_frames();
    let wait = video.frame_wait() * u32::try_from(video.settle_frames().max(1)).unwrap_or(1);
    let deadline = Instant::now() + wait;
    let mut newest = None;
    while Instant::now() < deadline {
        let after = newest.as_ref().map_or(0, |f: &video::RawFrame| f.sequence);
        if let Some(raw) = video.next_after(after, Duration::from_millis(100))? {
            let done = raw.sequence >= target;
            newest = Some(raw);
            if done {
                break;
            }
        }
    }
    match newest {
        Some(raw) if raw.sequence >= target => raw.decode(),
        _ => Err(screen::NoPicture(format!(
            "{} delivered no picture in {} s; check that the scope is on and, for a capture \
             card, that its HDMI output is connected to the card and switched on (Utility > IO > \
             HDMI > Output)",
            video.describe(),
            wait.as_secs()
        ))
        .into()),
    }
}

/// Front-panel keys that toggle the HDMI output: Utility opens at its top
/// page whatever was shown last, IO is F1 there, HDMI is F6 in IO, and F1
/// on the HDMI page toggles Output. The scope has no SCPI command for it.
/// Checked against the screen of an MSO5074 on firmware 00.01.03.00.03.
const HDMI_OUTPUT_KEYS: [&str; 4] = ["UTILity", "F1", "F6", "F1"];
/// Between key presses, for the scope to draw the page the next key acts on.
const KEY_PAUSE: Duration = Duration::from_millis(300);
/// For the capture card to lock onto the picture once the output is on.
const HDMI_LOCK_WAIT: Duration = Duration::from_secs(3);

fn press_key(link: &Mutex<Box<dyn Scpi>>, key: &str) -> Result<()> {
    {
        let mut scpi = lock_link(link);
        scpi.write(&format!(":SYSTem:KEY:PRESs {key}"))?;
        scpi.query("*OPC?")?;
    }
    std::thread::sleep(KEY_PAUSE);
    Ok(())
}

/// Leaves the HDMI page shown; pairing finds it over the right of the grid
/// and hides it.
fn toggle_hdmi_output(link: &Mutex<Box<dyn Scpi>>) -> Result<()> {
    for key in HDMI_OUTPUT_KEYS {
        press_key(link, key)?;
    }
    Ok(())
}

/// Measure where the waveform is in the video, by showing the grid and then
/// hiding it. A frame that does not change with the grid is not this scope's.
///
/// The connection is held only for each command, not while waiting for a
/// frame: the frames may themselves be read over it.
fn calibrate(link: &Mutex<Box<dyn Scpi>>, video: &dyn FrameSource) -> Result<(ScreenMap, Side)> {
    let command = |command: &str| -> Result<()> {
        let mut scpi = lock_link(link);
        scpi.write(command)?;
        scpi.query("*OPC?")?;
        Ok(())
    };
    let status = lock_link(link).query(":TRIGger:STATus?")?;
    let was_running = !status.trim().eq_ignore_ascii_case("STOP");
    // Frozen and cleared, so that only the grid differs between the two
    // frames: a trace lying on a grid line hides that line's change.
    lock_link(link).write(":STOP")?;
    let result = (|| {
        command(":DISPlay:CLEar")?;
        let grid_frames = || -> Result<_> {
            command(":DISPlay:GRID FULL")?;
            let with_grid = settled_frame(video)?;
            command(":DISPlay:GRID NONE")?;
            let without_grid = settled_frame(video)?;
            let map = screen::measure(&with_grid, &without_grid)?;
            Ok((screen::right_side_covered(&with_grid, &without_grid, &map), map))
        };
        let (covered, map) = grid_frames()?;
        if !covered {
            return Ok((map, Side::Clear));
        }
        // The side menu hides a fifth of the trace. MOFF toggles it, so it
        // is pressed only when the menu is seen, and pressed back if that
        // did not uncover the right of the grid.
        command(":SYSTem:KEY:PRESs MOFF")?;
        let (still_covered, uncovered) = grid_frames()?;
        if still_covered {
            command(":SYSTem:KEY:PRESs MOFF")?;
            tracing::warn!("the right of the scope's screen is covered and hiding its menu did not uncover it");
            return Ok((map, Side::StillCovered));
        }
        tracing::info!("hid the scope's menu, which covered the right of the trace");
        Ok((uncovered, Side::Uncovered))
    })();
    if was_running {
        lock_link(link).write(":RUN")?;
    }
    result
}

/// Set the display up to be read by the daemon: no grid, no persistence,
/// traces drawn as lines at full brightness. Best effort: a setting the
/// firmware lacks leaves the picture readable, only less clean.
fn prepare_display(scpi: &mut dyn Scpi) {
    for command in [
        ":DISPlay:GRID NONE",
        ":DISPlay:TYPE VECTors",
        ":DISPlay:GRADing:TIME MIN",
        ":DISPlay:WBRightness 100",
    ] {
        let applied = scpi
            .write(command)
            .and_then(|()| scpi.query("*OPC?").map(drop))
            .and_then(|()| check_errors(scpi, command));
        if let Err(e) = applied {
            tracing::warn!(command, error = %e, "display setting not applied");
        }
    }
}

fn read_settings(scpi: &mut dyn Scpi, channels: usize) -> Result<Settings> {
    let mut f64_of = |query: &str| -> Result<f64> { parse_f64(&scpi.query(query)?, query) };
    let mut caches = Vec::with_capacity(channels);
    for n in 1..=channels {
        caches.push(ChannelCache {
            enabled: f64_of(&format!(":CHANnel{n}:DISPlay?"))? != 0.0,
            volts_per_div: f64_of(&format!(":CHANnel{n}:SCALe?"))?,
            offset: f64_of(&format!(":CHANnel{n}:OFFSet?"))?,
            coupling: Coupling::DC,
            probe: f64_of(&format!(":CHANnel{n}:PROBe?"))?,
        });
    }
    let time_per_div = f64_of(":TIMebase:MAIN:SCALe?")?;
    let time_offset = f64_of(":TIMebase:MAIN:OFFSet?")?;
    let trigger_level = f64_of(":TRIGger:EDGE:LEVel?")?;
    let sample_rate = f64_of(":ACQuire:SRATe?").unwrap_or(0.0);
    for (i, cache) in caches.iter_mut().enumerate() {
        cache.coupling = parse_coupling(&scpi.query(&format!(":CHANnel{}:COUPling?", i + 1))?);
    }
    let trigger_source = parse_source(&scpi.query(":TRIGger:EDGE:SOURce?")?)
        .filter(|&i| i < channels)
        .unwrap_or(0);
    let trigger_slope = parse_slope(&scpi.query(":TRIGger:EDGE:SLOPe?")?);
    let capture_mode = parse_sweep(&scpi.query(":TRIGger:SWEep?")?);
    Ok(Settings {
        channels: caches,
        time_per_div,
        time_offset,
        trigger_level,
        trigger_source,
        trigger_slope,
        capture_mode,
        sample_rate,
    })
}

/// The scope's tokens for each `Measurement`, as `measure_one` asks for them.
const MEASUREMENT_ITEMS: [&str; 14] = [
    "VMAX", "VMIN", "VPP", "VAVG", "VRMS", "PERiod", "FREQuency", "PDUTy", "NDUTy", "PWIDth",
    "NWIDth", "RTIMe", "FTIMe", "OVERshoot",
];

/// Where `which` is in `MEASUREMENT_ITEMS`, and whether the scope gives it as
/// a ratio that is reported in percent.
fn measurement_item(which: Measurement) -> (usize, bool) {
    match which {
        Measurement::Vmax => (0, false),
        Measurement::Vmin => (1, false),
        Measurement::Vpp => (2, false),
        Measurement::Vavg => (3, false),
        Measurement::Vrms => (4, false),
        Measurement::Period => (5, false),
        Measurement::Frequency => (6, false),
        Measurement::DutyCyclePositive => (7, true),
        Measurement::DutyCycleNegative => (8, true),
        Measurement::PulseWidthPositive => (9, false),
        Measurement::PulseWidthNegative => (10, false),
        Measurement::RiseTime => (11, false),
        Measurement::FallTime => (12, false),
        Measurement::Overshoot => (13, true),
    }
}

/// Second-level `:MEASure` queries that read a setting, not a measurement:
/// they add no readout.
const MEASURE_SETTINGS: [&str; 4] = ["SOUR", "SOURCE", "MODE", "AMS"];

/// Whether `command` is one `:MEASure:<item>?`, which adds a readout to the
/// scope's measurement bar.
fn is_measurement_query(command: &str) -> bool {
    let upper = command.trim().to_ascii_uppercase();
    if upper.contains(';') {
        return false;
    }
    let Some(header) = upper.strip_suffix('?') else {
        return false;
    };
    let header = header.strip_prefix(':').unwrap_or(header);
    let mut levels = header.split(':');
    let (Some(root), Some(item), None) = (levels.next(), levels.next(), levels.next()) else {
        return false;
    };
    matches!(root, "MEAS" | "MEASURE") && !item.is_empty() && !MEASURE_SETTINGS.contains(&item)
}

/// A `:MEASure:<item>?` answer, or `None` for one the scope could not make
/// (9.9E37).
fn scope_measurement(reply: &str) -> Option<f64> {
    reply
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite() && v.abs() < NO_MEASUREMENT)
}

/// `:WAVeform:PREamble?`: format, type, points, count, x increment, x
/// origin, x reference, y increment, y origin, y reference.
#[derive(Debug, Clone, Copy, PartialEq)]
struct Preamble {
    points: usize,
    x_increment: f64,
    x_origin: f64,
    y_increment: f64,
    y_origin: f64,
    y_reference: f64,
}

impl Preamble {
    fn parse(reply: &str) -> Result<Preamble> {
        let fields: Vec<f64> = reply
            .split(',')
            .map(|f| f.trim().parse::<f64>())
            .collect::<std::result::Result<_, _>>()
            .with_context(|| format!("the waveform preamble {reply:?} is not ten numbers"))?;
        if fields.len() < 10 {
            bail!("the waveform preamble {reply:?} is not ten numbers");
        }
        Ok(Preamble {
            points: fields[2] as usize,
            x_increment: fields[4],
            x_origin: fields[5],
            y_increment: fields[7],
            y_origin: fields[8],
            y_reference: fields[9],
        })
    }
}

impl Oscilloscope for RigolMso5000 {
    fn enable_channel(&mut self, channel: ChannelId) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:DISPlay ON", i + 1))?;
        self.settings.channels[i].enabled = true;
        self.refresh_sample_rate();
        Ok(())
    }

    fn disable_channel(&mut self, channel: ChannelId) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:DISPlay OFF", i + 1))?;
        self.settings.channels[i].enabled = false;
        self.refresh_sample_rate();
        Ok(())
    }

    fn is_channel_enabled(&self, channel: ChannelId) -> Result<bool> {
        Ok(self.settings.channels[self.index(channel)?].enabled)
    }

    fn set_volts_per_div(&mut self, channel: ChannelId, volts_per_div: f64) -> Result<()> {
        let i = self.index(channel)?;
        self.while_shown(i, |scope| {
            let n = i + 1;
            scope.apply(&format!(":CHANnel{n}:SCALe {volts_per_div}"))?;
            // The scope rounds to what it has, and keeps the offset in
            // divisions, so the offset in volts moves with the scale.
            scope.settings.channels[i].volts_per_div =
                scope.query_f64(&format!(":CHANnel{n}:SCALe?"))?;
            scope.settings.channels[i].offset = scope.query_f64(&format!(":CHANnel{n}:OFFSet?"))?;
            Ok(())
        })
    }

    fn get_volts_per_div(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings.channels[self.index(channel)?].volts_per_div)
    }

    fn set_volts_offset(&mut self, channel: ChannelId, volts_offset: f64) -> Result<()> {
        let i = self.index(channel)?;
        self.while_shown(i, |scope| {
            scope.apply(&format!(":CHANnel{}:OFFSet {volts_offset}", i + 1))?;
            scope.settings.channels[i].offset =
                scope.query_f64(&format!(":CHANnel{}:OFFSet?", i + 1))?;
            Ok(())
        })
    }

    fn get_volts_offset(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings.channels[self.index(channel)?].offset)
    }

    fn set_coupling(&mut self, channel: ChannelId, coupling: Coupling) -> Result<()> {
        let i = self.index(channel)?;
        let token = match coupling {
            Coupling::DC => "DC",
            Coupling::AC => "AC",
            Coupling::GND => "GND",
        };
        self.apply(&format!(":CHANnel{}:COUPling {token}", i + 1))?;
        self.settings.channels[i].coupling = coupling;
        Ok(())
    }

    fn get_coupling(&self, channel: ChannelId) -> Result<Coupling> {
        Ok(self.settings.channels[self.index(channel)?].coupling)
    }

    fn set_attenuation(&mut self, channel: ChannelId, attenuation: f64) -> Result<()> {
        let i = self.index(channel)?;
        self.apply(&format!(":CHANnel{}:PROBe {attenuation}", i + 1))?;
        let n = i + 1;
        self.settings.channels[i].probe = self.query_f64(&format!(":CHANnel{n}:PROBe?"))?;
        // The scale and offset are in volts at the probe tip, so they move
        // with the ratio.
        self.settings.channels[i].volts_per_div = self.query_f64(&format!(":CHANnel{n}:SCALe?"))?;
        self.settings.channels[i].offset = self.query_f64(&format!(":CHANnel{n}:OFFSet?"))?;
        Ok(())
    }

    fn get_attenuation(&self, channel: ChannelId) -> Result<f64> {
        Ok(self.settings.channels[self.index(channel)?].probe)
    }

    fn set_trigger_level(&mut self, trigger_level: f64) -> Result<()> {
        self.apply(&format!(":TRIGger:EDGE:LEVel {trigger_level}"))?;
        // The scope clamps the level to the screen.
        self.settings.trigger_level = self.query_f64(":TRIGger:EDGE:LEVel?")?;
        Ok(())
    }

    fn get_trigger_level(&self) -> Result<f64> {
        Ok(self.settings.trigger_level)
    }

    fn set_time_per_div(&mut self, time_per_div: f64) -> Result<()> {
        self.apply(&format!(":TIMebase:MAIN:SCALe {time_per_div}"))?;
        self.settings.time_per_div = self.query_f64(":TIMebase:MAIN:SCALe?")?;
        self.settings.time_offset = self.query_f64(":TIMebase:MAIN:OFFSet?")?;
        self.refresh_sample_rate();
        Ok(())
    }

    fn get_time_per_div(&self) -> Result<f64> {
        Ok(self.settings.time_per_div)
    }

    /// Unlike a PicoScope, the scope moves its window itself, as far as its
    /// memory reaches: positive looks later.
    fn set_time_offset(&mut self, time_offset: f64) -> Result<()> {
        self.apply(&format!(":TIMebase:MAIN:OFFSet {time_offset}"))?;
        self.settings.time_offset = self.query_f64(":TIMebase:MAIN:OFFSet?")?;
        Ok(())
    }

    fn get_time_offset(&self) -> Result<f64> {
        Ok(self.settings.time_offset)
    }

    fn set_trigger_source(&mut self, trigger_source: ChannelId) -> Result<()> {
        let i = self.index(trigger_source)?;
        self.apply(":TRIGger:MODE EDGE")?;
        self.apply(&format!(":TRIGger:EDGE:SOURce CHANnel{}", i + 1))?;
        self.settings.trigger_source = i;
        self.settings.trigger_level = self.query_f64(":TRIGger:EDGE:LEVel?")?;
        Ok(())
    }

    fn get_trigger_source(&self) -> Result<ChannelId> {
        Ok(letter(self.settings.trigger_source))
    }

    fn set_trigger_slope(&mut self, trigger_slope: TriggerSlope) -> Result<()> {
        let token = match trigger_slope {
            TriggerSlope::Rising => "POSitive",
            TriggerSlope::Falling => "NEGative",
            TriggerSlope::Either => "RFALl",
            TriggerSlope::Neither => bail!("the scope's edge trigger has no 'neither' slope"),
        };
        self.apply(&format!(":TRIGger:EDGE:SLOPe {token}"))?;
        self.settings.trigger_slope = trigger_slope;
        Ok(())
    }

    fn get_trigger_slope(&self) -> Result<TriggerSlope> {
        Ok(self.settings.trigger_slope)
    }

    fn set_capture_mode(&mut self, capture_mode: CaptureMode) -> Result<()> {
        let token = match capture_mode {
            CaptureMode::Auto => "AUTO",
            CaptureMode::Normal => "NORMal",
            CaptureMode::Single => "SINGle",
        };
        self.apply(&format!(":TRIGger:SWEep {token}"))?;
        self.settings.capture_mode = capture_mode;
        Ok(())
    }

    fn get_capture_mode(&self) -> Result<CaptureMode> {
        Ok(self.settings.capture_mode)
    }

    fn set_cursor_position(&mut self, _cursor: Cursor) -> Result<()> {
        Err(self.not_offered("hardware cursors"))
    }

    fn get_cursor_position(&self, _cursor: Cursor) -> Result<f64> {
        Err(self.not_offered("hardware cursors"))
    }

    fn measure_horizontal_cursor_delta(&self) -> Result<f64> {
        Err(self.not_offered("hardware cursors"))
    }

    fn measure_vertical_cursor_delta(&self) -> Result<f64> {
        Err(self.not_offered("hardware cursors"))
    }

    // The loop measures through `measure_all`, which these predate.
    fn measure_duty_cycle(&self, _channel: ChannelId) -> Result<f64> {
        Err(self.not_offered("single measurements outside measure_all"))
    }

    fn measure_frequency(&self, _channel: ChannelId) -> Result<f64> {
        Err(self.not_offered("single measurements outside measure_all"))
    }

    fn measure_period(&self, _channel: ChannelId) -> Result<f64> {
        Err(self.not_offered("single measurements outside measure_all"))
    }

    fn measure_rms(&self, _channel: ChannelId) -> Result<f64> {
        Err(self.not_offered("single measurements outside measure_all"))
    }

    fn measure_peak_to_peak(&self, _channel: ChannelId) -> Result<f64> {
        Err(self.not_offered("single measurements outside measure_all"))
    }

    fn measure_average(&self, _channel: ChannelId) -> Result<f64> {
        Err(self.not_offered("single measurements outside measure_all"))
    }

    fn measure_min(&self, _channel: ChannelId) -> Result<f64> {
        Err(self.not_offered("single measurements outside measure_all"))
    }

    fn get_data(&self, _channel: ChannelId) -> Result<Vec<f64>> {
        Err(self.not_offered("get_data; read the record instead"))
    }

    fn get_sample_rate(&self) -> Result<f64> {
        Ok(self.settings.sample_rate)
    }

    fn get_memory_depth(&self) -> Result<usize> {
        self.current_memory_depth()
    }

    /// The samples the screen spans at the current rate.
    fn current_memory_depth(&self) -> Result<usize> {
        let s = &self.settings;
        Ok((s.sample_rate * s.time_per_div * HORIZONTAL_DIVISIONS).round().max(0.0) as usize)
    }

    fn get_bandwidth(&self) -> Result<f64> {
        self.capabilities
            .bandwidth_hz
            .context("the model's bandwidth is not known")
    }

    fn get_channel_count(&self) -> Result<usize> {
        Ok(self.settings.channels.len())
    }

    /// Where the offset puts the trigger, as a percentage across the screen.
    fn get_trigger_position(&self) -> Result<f64> {
        let s = &self.settings;
        if s.time_per_div <= 0.0 {
            return Ok(50.0);
        }
        Ok(50.0 - s.time_offset / (s.time_per_div * HORIZONTAL_DIVISIONS) * 100.0)
    }

    /// Run, or arm one capture in single. The position is the scope's own
    /// offset, set with `set_time_offset`; the percentage a PicoScope takes
    /// here is not needed.
    fn start_triggered_capture(&mut self, _trigger_position_percent: f64) -> Result<()> {
        self.pair_again()?;
        let command = if self.settings.capture_mode == CaptureMode::Single {
            ":SINGle"
        } else {
            ":RUN"
        };
        self.apply(command)
    }

    fn stop_triggered_capture(&mut self) -> Result<()> {
        // Not `apply`: the frozen screen is still the capture, and stopping
        // does not change what it shows.
        let mut scpi = self.link();
        scpi.write(":STOP")?;
        scpi.query("*OPC?")?;
        Ok(())
    }

    /// The scope acquires on its own and shows it on its own screen, so the
    /// daemon starting or stopping leaves it running or stopped as it was.
    /// Stopping it here froze every measurement after a daemon restart.
    fn release_capture(&mut self) -> Result<()> {
        Ok(())
    }

    /// A newer frame than the last one used, taken since the last change.
    /// In single, only once the scope has stopped on its trigger.
    fn is_ready(&self) -> Result<bool> {
        let Some(screen) = &self.screen else {
            return Ok(false);
        };
        let newest = screen.video.newest()?;
        if self.settings.capture_mode == CaptureMode::Single {
            let done = self.work().single_done;
            if !done {
                let status = self.link().query(":TRIGger:STATus?")?;
                if !status.trim().eq_ignore_ascii_case("STOP") {
                    return Ok(false);
                }
                let settle = self.settle_frames();
                let mut work = self.work();
                work.single_done = true;
                work.stale_through = newest + settle;
                return Ok(false);
            }
        }
        let work = self.work();
        Ok(newest > work.consumed.max(work.stale_through))
    }

    fn get_triggered_data(&self) -> Result<CaptureFrame> {
        let after = {
            let work = self.work();
            work.consumed.max(work.stale_through)
        };
        let raw = self
            .screen()?
            .video
            .next_after(after, Duration::ZERO)?
            .context("no new video frame since the last capture")?;
        self.work().consumed = raw.sequence;
        let triggered = match self.settings.capture_mode {
            CaptureMode::Single => true,
            _ => {
                let status = self.link().query(":TRIGger:STATus?")?;
                status.trim().eq_ignore_ascii_case("TD")
            }
        };
        self.envelope_frame(&raw, triggered)
    }

    fn force_trigger(&mut self) -> Result<()> {
        let mut scpi = self.link();
        scpi.write(":TFORce")?;
        scpi.query("*OPC?")?;
        Ok(())
    }

    fn capabilities(&self) -> Result<ScopeCapabilities> {
        Ok(self.capabilities.clone())
    }

    /// Frames arrive at the card's rate; a quarter of a 60 fps frame keeps
    /// the latency low without spinning.
    fn suggested_poll_interval(&self) -> Duration {
        Duration::from_millis(4)
    }

    /// The scope keeps acquiring on its own; there is nothing to arm. The
    /// loop calls this between frames, so the side menu the frames suggest
    /// is open is looked for here.
    fn rearm(&mut self) -> Result<()> {
        if std::mem::take(&mut self.work().menu.due) {
            self.hide_side_menu()?;
        }
        Ok(())
    }

    fn reports_trigger(&self) -> bool {
        true
    }

    fn live_view_problem(&self) -> Option<String> {
        self.unpaired.as_ref().map(|u| match u.retry {
            Retry::Pair(_) => format!(
                "{}. Turn on the scope's HDMI output (Utility > IO > HDMI > Output), then start \
                 acquisition again",
                u.reason
            ),
            Retry::Open(_) => format!(
                "{}. Check that the capture card is on the port it was paired on and that \
                 nothing else is using it, or pair it again with `lager nets set-capture`, \
                 then start acquisition again",
                u.reason
            ),
        })
    }

    fn answers_from_instrument(&self) -> bool {
        true
    }

    fn measure_one(&mut self, channel: ChannelId, which: Measurement) -> Option<Result<Option<f64>>> {
        Some((|| {
            let i = self.index(channel)?;
            if !self.settings.channels[i].enabled {
                bail!("channel {channel} is not enabled, so there is nothing to measure");
            }
            let (item, percent) = measurement_item(which);
            // The source is the user's front-panel setting too: put back.
            let source = self.query_checked(":MEASure:SOURce?")?;
            let wanted = format!("CHAN{}", i + 1);
            let moved = !source.trim().eq_ignore_ascii_case(&wanted);
            if moved {
                self.apply(&format!(":MEASure:SOURce CHANnel{}", i + 1))?;
            }
            let reply = self.measure_query(&format!(":MEASure:{}?", MEASUREMENT_ITEMS[item]));
            if moved && !source.trim().is_empty() {
                // Refused for a channel hidden since; the reading stands.
                if let Err(e) = self.apply(&format!(":MEASure:SOURce {}", source.trim())) {
                    tracing::warn!(error = %format!("{e:#}"), "could not put the scope's measurement source back");
                }
            }
            Ok(scope_measurement(&reply?).map(|v| if percent { v * 100.0 } else { v }))
        })())
    }

    /// Computed from the channel's screen record, as the PicoScope's are, and
    /// with no `:MEASure` query at all. Asking the scope item by item took 14
    /// queries, about 4.8 s on the bench, and even the two kept for rise and
    /// fall stalled the live view about a second at every refresh while
    /// their readouts were drawn over the trace. `measure_one` still asks
    /// the scope for its full-memory reading.
    fn measure_all(&mut self, channel: ChannelId) -> Option<Result<MeasurementSet>> {
        Some((|| {
            let i = self.index(channel)?;
            if !self.settings.channels[i].enabled {
                bail!("channel {channel} is not enabled, so there is nothing to measure");
            }
            let record = self.record(&[i])?;
            let mut set = protocol::measure_channel(&record, 0).with_context(|| {
                format!("the scope has no samples for channel {channel}; is there a signal on it?")
            })?;
            // The record has a sample per screen column, 10 us apart at
            // 1 ms/div, far coarser than most edges: a 70 ns rise reads as
            // one sample. An edge the record does not resolve is left out
            // rather than reported as its spacing.
            let shortest = RESOLVED_EDGE_SAMPLES * record.sample_interval_ns / 1e9;
            set.rise_time = set.rise_time.filter(|&t| t >= shortest);
            set.fall_time = set.fall_time.filter(|&t| t >= shortest);
            Ok(set)
        })())
    }

    /// The scope's screen record, 8-bit and exact, for each enabled channel.
    fn read_record(&mut self) -> Option<Result<CaptureFrame>> {
        Some((|| {
            let enabled: Vec<usize> = (0..self.settings.channels.len())
                .filter(|&i| self.settings.channels[i].enabled)
                .collect();
            if enabled.is_empty() {
                bail!("no channel is enabled, so there is no record to read");
            }
            self.record(&enabled)
        })())
    }

    fn set_acquisition(
        &mut self,
        mode: AcquisitionMode,
        average_count: u32,
    ) -> Option<Result<()>> {
        Some((|| match mode {
            AcquisitionMode::Normal => self.apply(":ACQuire:TYPE NORMal"),
            AcquisitionMode::Peak => self.apply(":ACQuire:TYPE PEAK"),
            AcquisitionMode::Average => {
                if !(2..=65536).contains(&average_count) || !average_count.is_power_of_two() {
                    bail!(
                        "the {} averages a power of two from 2 to 65536 captures, not {average_count}",
                        self.capabilities.model
                    );
                }
                self.apply(":ACQuire:TYPE AVERages")?;
                self.apply(&format!(":ACQuire:AVERages {average_count}"))
            }
        })())
    }

    fn set_holdoff(&mut self, seconds: f64) -> Option<Result<()>> {
        // The scope's least holdoff is no holdoff to speak of.
        let seconds = seconds.max(1.6e-8);
        Some(self.apply(&format!(":TRIGger:HOLDoff {seconds}")))
    }

    fn scpi(&mut self, command: &str) -> Option<Result<Option<String>>> {
        Some((|| {
            // A query may carry a setting before it, as in
            // `:CHANnel1:OFFSet 1;*OPC?`, so both paths check.
            let reply = if is_measurement_query(command) {
                Some(self.measure_query(command)?)
            } else if command.contains('?') {
                Some(self.query_checked(command)?)
            } else {
                self.apply(command)?;
                None
            };
            if changes_cached_settings(command) {
                let channels = self.settings.channels.len();
                let settings = read_settings(self.link().as_mut(), channels)?;
                self.settings = settings;
                self.refresh_sample_rate();
            }
            Ok(reply)
        })())
    }
}

#[cfg(test)]
mod tests;

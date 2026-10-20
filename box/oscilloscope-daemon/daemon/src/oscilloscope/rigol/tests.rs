// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! The backend against a simulated MSO5204: an instrument that keeps its
//! settings, answers SCPI as the scope does, and draws its screen into the
//! video the way the capture card would see it.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use super::frame::{encode_bmp, rgb_to_ycc, Layout, VideoFrame};
use super::video::{Encoding, FrameSource, RawFrame};
use super::*;

/// The simulated screen: 640 x 480, the waveform area 10 x 8 divisions of
/// 50 pixels about (320, 240).
const WIDTH: usize = 640;
const HEIGHT: usize = 480;
const CX: usize = 320;
const CY: usize = 240;
const PX_PER_DIV: usize = 50;
/// Where the side menu starts, just right of the grid line three divisions
/// right of centre, as on the MSO5074.
const MENU_LEFT: usize = CX + 3 * PX_PER_DIV + 10;
/// The card's frame interval, at 30 frames a second.
const FRAME_NS: u64 = 33_333_333;

#[derive(Debug, Clone)]
struct Channel {
    display: bool,
    scale: f64,
    offset: f64,
    coupling: &'static str,
    probe: f64,
    /// The DC level on this channel's input, in volts at the probe tip.
    signal: f64,
}

#[derive(Debug)]
struct Instrument {
    idn: String,
    grid: bool,
    status: &'static str,
    channels: Vec<Channel>,
    time_scale: f64,
    time_offset: f64,
    level: f64,
    source: usize,
    slope: &'static str,
    sweep: &'static str,
    acquire_type: &'static str,
    averages: u32,
    holdoff: f64,
    measure_source: usize,
    errors: VecDeque<String>,
    /// Every command written, for the tests to inspect.
    sent: Vec<String>,
    /// The HDMI output (Utility > IO > HDMI > Output), off after a power
    /// cycle.
    on_hdmi: bool,
    /// Whether the card is on the scope's HDMI at all.
    cable: bool,
    /// The side menu, open over the right fifth of the grid.
    menu: bool,
    /// The menu page shown, as the keys that reached it.
    page: &'static str,
    /// Where the traces stop, for a signal that does not cross the screen.
    traces_end_at: Option<usize>,
    /// The readouts in the measurement bar, newest first, as their items.
    readouts: Vec<String>,
    /// Every query asked, for the tests to inspect.
    queries: Vec<String>,
    /// The screen record `:WAVeform:DATA?` returns, as 8-bit codes.
    record: Vec<u8>,
}

impl Instrument {
    fn mso5204() -> Instrument {
        let channel = |display, signal| Channel {
            display,
            scale: 1.0,
            offset: 0.0,
            coupling: "DC",
            probe: 1.0,
            signal,
        };
        Instrument {
            idn: "RIGOL TECHNOLOGIES,MSO5204,MS5A244000001,00.01.03.00.03".into(),
            grid: true,
            status: "AUTO",
            channels: vec![channel(true, 1.0), channel(false, -2.0), channel(false, 0.0), channel(false, 0.0)],
            time_scale: 1e-3,
            time_offset: 0.0,
            level: 0.0,
            source: 0,
            slope: "POS",
            sweep: "AUTO",
            acquire_type: "NORM",
            averages: 2,
            holdoff: 1.6e-8,
            measure_source: 0,
            errors: VecDeque::new(),
            sent: Vec::new(),
            on_hdmi: true,
            cable: true,
            menu: false,
            page: "",
            traces_end_at: None,
            readouts: Vec::new(),
            queries: Vec::new(),
            // A ramp from 0 to 0.9 V at 25 mV a code.
            record: (0..10).map(|i| 127 + i * 4).collect(),
        }
    }

    /// Show `item`'s readout at the front of the bar, as a measurement
    /// query does: an item already shown moves there, and the bar holds ten.
    fn read_out(&mut self, item: &str) {
        self.readouts.retain(|shown| shown != item);
        self.readouts.insert(0, item.to_string());
        self.readouts.truncate(10);
    }

    /// Apply a command, as the scope would, or queue an error.
    fn write(&mut self, command: &str) {
        self.sent.push(command.to_string());
        let upper = command.trim().to_ascii_uppercase();
        let (head, arg) = upper.split_once(' ').unwrap_or((upper.as_str(), ""));
        let number = || arg.parse::<f64>().ok();
        let channel = |head: &str| -> Option<usize> {
            head.strip_prefix(":CHANNEL")
                .and_then(|rest| rest.chars().next())
                .and_then(|c| c.to_digit(10))
                .map(|n| n as usize - 1)
        };
        let ok = match head {
            "*CLS" => {
                self.errors.clear();
                true
            }
            ":RUN" => {
                self.status = if self.sweep == "AUTO" { "AUTO" } else { "WAIT" };
                true
            }
            ":STOP" => {
                self.status = "STOP";
                true
            }
            ":SINGLE" => {
                self.status = "WAIT";
                true
            }
            ":TFORCE" => true,
            ":SYSTEM:KEY:PRESS" => {
                // The pages the HDMI output is under, as on the MSO5074;
                // other keys on them do other things, and are not modelled.
                match (arg, self.page) {
                    ("MOFF", _) => {
                        self.menu = !self.menu;
                        self.page = "";
                    }
                    ("UTILITY", _) => {
                        self.menu = true;
                        self.page = "utility";
                    }
                    ("F1", "utility") if self.menu => self.page = "io",
                    ("F6", "io") if self.menu => self.page = "hdmi",
                    ("F1", "hdmi") if self.menu => self.on_hdmi = !self.on_hdmi,
                    _ => panic!("pressed {arg} on the {:?} page", self.page),
                }
                true
            }
            ":DISPLAY:GRID" => {
                self.grid = arg != "NONE";
                true
            }
            ":DISPLAY:TYPE" | ":DISPLAY:GRADING:TIME" | ":DISPLAY:WBRIGHTNESS" | ":DISPLAY:CLEAR" => true,
            ":TIMEBASE:MAIN:SCALE" => number().map(|v| self.time_scale = v).is_some(),
            ":TIMEBASE:MAIN:OFFSET" => number().map(|v| self.time_offset = v).is_some(),
            ":TRIGGER:MODE" => arg == "EDGE",
            ":TRIGGER:EDGE:LEVEL" => number()
                .map(|v| {
                    // Clamped to the screen of the source.
                    let c = &self.channels[self.source];
                    let limit = 5.0 * c.scale;
                    self.level = v.clamp(-limit - c.offset, limit - c.offset);
                })
                .is_some(),
            ":TRIGGER:EDGE:SOURCE" => match arg.strip_prefix("CHANNEL").and_then(|n| n.parse::<usize>().ok()) {
                Some(n @ 1..=4) => {
                    self.source = n - 1;
                    true
                }
                _ => false,
            },
            ":TRIGGER:EDGE:SLOPE" => {
                self.slope = match arg {
                    "POSITIVE" => "POS",
                    "NEGATIVE" => "NEG",
                    "RFALL" => "RFAL",
                    _ => return self.error(command),
                };
                true
            }
            ":TRIGGER:SWEEP" => {
                self.sweep = match arg {
                    "AUTO" => "AUTO",
                    "NORMAL" => "NORM",
                    "SINGLE" => "SING",
                    _ => return self.error(command),
                };
                true
            }
            ":TRIGGER:HOLDOFF" => number().map(|v| self.holdoff = v).is_some(),
            ":ACQUIRE:TYPE" => {
                self.acquire_type = match arg {
                    "NORMAL" => "NORM",
                    "AVERAGES" => "AVER",
                    "PEAK" => "PEAK",
                    _ => return self.error(command),
                };
                true
            }
            ":ACQUIRE:AVERAGES" => number().map(|v| self.averages = v as u32).is_some(),
            ":MEASURE:SOURCE" => match arg
                .strip_prefix("CHANNEL")
                .or_else(|| arg.strip_prefix("CHAN"))
                .and_then(|n| n.parse::<usize>().ok())
            {
                Some(n) => {
                    self.measure_source = n - 1;
                    true
                }
                None => false,
            },
            // Readouts are numbered oldest first.
            ":MEASURE:CLEAR" => match arg {
                "ALL" => {
                    self.readouts.clear();
                    true
                }
                item => match item.strip_prefix("ITEM").and_then(|n| n.parse::<usize>().ok()) {
                    Some(n) if (1..=self.readouts.len()).contains(&n) => {
                        let index = self.readouts.len() - n;
                        self.readouts.remove(index);
                        true
                    }
                    _ => false,
                },
            },
            ":MEASURE:ITEM" => match arg.split_once(',') {
                Some((item, _source)) => {
                    let item = item.to_string();
                    self.read_out(&item);
                    true
                }
                None => false,
            },
            ":WAVEFORM:MODE" | ":WAVEFORM:FORMAT" | ":WAVEFORM:SOURCE" => true,
            _ => match channel(head) {
                Some(n) if n < 4 => {
                    let field = head.splitn(3, ':').nth(2).unwrap_or("");
                    let c = &mut self.channels[n];
                    match field {
                        "DISPLAY" => {
                            c.display = arg == "ON" || arg == "1";
                            true
                        }
                        // A hidden channel ignores its scale and offset,
                        // with no error, as the MSO5074 on the bench did.
                        "SCALE" | "OFFSET" if !c.display => true,
                        // Rounded to 1-2-5, as the scope does. The offset
                        // is kept in divisions, so it scales with the change:
                        // 2.56 V at 2 V/div is 1.28 V at 1 V/div.
                        "SCALE" => number()
                            .map(|v| {
                                let scale = volts_per_div_ladder()
                                    .into_iter()
                                    .min_by(|a, b| (a - v).abs().total_cmp(&(b - v).abs()))
                                    .unwrap()
                                    * c.probe;
                                c.offset = c.offset / c.scale * scale;
                                c.scale = scale;
                            })
                            .is_some(),
                        "OFFSET" => number().map(|v| c.offset = v).is_some(),
                        "COUPLING" => {
                            c.coupling = match arg {
                                "AC" => "AC",
                                "DC" => "DC",
                                "GND" => "GND",
                                _ => return self.error(command),
                            };
                            true
                        }
                        "PROBE" => number()
                            .map(|v| {
                                c.scale = c.scale / c.probe * v;
                                c.offset = c.offset / c.probe * v;
                                c.probe = v;
                            })
                            .is_some(),
                        _ => false,
                    }
                }
                _ => false,
            },
        };
        if !ok {
            self.error(command);
        }
    }

    fn error(&mut self, command: &str) {
        self.errors
            .push_back(format!("-113,\"Undefined header; {command}\""));
    }

    /// The reply to `query`, or nothing for a header the scope rejects
    /// without answering.
    fn query(&mut self, query: &str) -> Vec<u8> {
        self.queries.push(query.to_string());
        let upper = query.trim().to_ascii_uppercase();
        // Headers this firmware rejects with -100 and no reply.
        if matches!(upper.as_str(), ":MEASURE:MPAREA?" | ":MEASURE:VAVERAGE?" | ":MEASURE:RRDELAY?") {
            self.errors.push_back("-100,\"Command err\"".into());
            return Vec::new();
        }
        if let Some(item) = upper.strip_prefix(":MEASURE:").and_then(|q| q.strip_suffix('?')) {
            if item != "SOURCE" {
                let item = item.to_string();
                self.read_out(&item);
            }
        }
        let channel = |n: usize| &self.channels[n];
        let text = match upper.as_str() {
            "*IDN?" => self.idn.clone(),
            "*OPC?" => "1".into(),
            ":SYSTEM:ERROR?" => self
                .errors
                .pop_front()
                .unwrap_or_else(|| "0,\"No error\"".into()),
            ":TRIGGER:STATUS?" => self.status.into(),
            ":TIMEBASE:MAIN:SCALE?" => format!("{:e}", self.time_scale),
            ":TIMEBASE:MAIN:OFFSET?" => format!("{:e}", self.time_offset),
            ":TRIGGER:EDGE:LEVEL?" => format!("{:e}", self.level),
            ":TRIGGER:EDGE:SOURCE?" => format!("CHAN{}", self.source + 1),
            ":TRIGGER:EDGE:SLOPE?" => self.slope.into(),
            ":TRIGGER:SWEEP?" => self.sweep.into(),
            ":ACQUIRE:SRATE?" => "1.000000e+09".into(),
            ":MEASURE:SOURCE?" => format!("CHAN{}", self.measure_source + 1),
            ":MEASURE:VMAX?" => format!("{:e}", channel(self.measure_source).signal),
            ":MEASURE:VMIN?" => format!("{:e}", channel(self.measure_source).signal),
            ":MEASURE:VPP?" => "0.000000e+00".into(),
            ":MEASURE:VAVG?" | ":MEASURE:VRMS?" => format!("{:e}", channel(self.measure_source).signal),
            ":MEASURE:PDUTY?" => "2.500000e-01".into(),
            ":MEASURE:NDUTY?" => "7.500000e-01".into(),
            ":MEASURE:RTIME?" => "7.000000e-08".into(),
            ":MEASURE:FTIME?" => "6.000000e-08".into(),
            q if q.starts_with(":MEASURE:") => "9.9E37".into(),
            ":WAVEFORM:PREAMBLE?" => {
                // Ten points at 1 us, trigger at the fifth; 25 mV a code.
                "0,0,10,1,1.000000e-06,-5.000000e-06,0,2.500000e-02,0,127".into()
            }
            ":DISPLAY:DATA?" => {
                let image = encode_bmp(&self.draw());
                let length = image.len().to_string();
                let mut reply = format!("#{}{length}", length.len()).into_bytes();
                reply.extend_from_slice(&image);
                reply.push(b'\n');
                return reply;
            }
            ":WAVEFORM:DATA?" => {
                let mut reply = format!("#2{:02}", self.record.len()).into_bytes();
                reply.extend_from_slice(&self.record);
                reply.push(b'\n');
                return reply;
            }
            q => match q.strip_prefix(":CHANNEL").and_then(|r| r.split_once(':')) {
                Some((n, field)) => {
                    let c = channel(n.parse::<usize>().unwrap() - 1);
                    match field {
                        "DISPLAY?" => if c.display { "1" } else { "0" }.into(),
                        "SCALE?" => format!("{:e}", c.scale),
                        "OFFSET?" => format!("{:e}", c.offset),
                        "COUPLING?" => c.coupling.into(),
                        "PROBE?" => format!("{:e}", c.probe),
                        _ => {
                            self.error(query);
                            String::new()
                        }
                    }
                }
                None => {
                    self.error(query);
                    String::new()
                }
            },
        };
        let mut reply = text.into_bytes();
        reply.push(b'\n');
        reply
    }

    /// The screen as the capture card sees it.
    fn draw(&self) -> VideoFrame {
        let mut frame = VideoFrame::blank(WIDTH, HEIGHT, Layout::Ycc);
        if !self.on_hdmi || !self.cable {
            return frame;
        }
        let half_w = 5 * PX_PER_DIV;
        let half_h = 4 * PX_PER_DIV;
        if self.grid {
            let grey = (100, 128, 128);
            for k in -4i64..=4 {
                let x = (CX as i64 + k * PX_PER_DIV as i64) as usize;
                for y in (CY - half_h..=CY + half_h).step_by(2) {
                    frame.put(x, y, grey);
                }
            }
            for k in -3i64..=3 {
                let y = (CY as i64 + k * PX_PER_DIV as i64) as usize;
                for x in (CX - half_w..=CX + half_w).step_by(2) {
                    frame.put(x, y, grey);
                }
            }
        }
        for (i, c) in self.channels.iter().enumerate() {
            if !c.display {
                continue;
            }
            let divs = (c.signal + c.offset) / c.scale;
            let y = (CY as f64 - divs * PX_PER_DIV as f64).round();
            if y < (CY - half_h) as f64 || y > (CY + half_h) as f64 {
                continue;
            }
            let (r, g, b) = MSO5000_COLOURS[i];
            let end = self.traces_end_at.unwrap_or(CX + half_w);
            for x in CX - half_w..=end {
                frame.put(x, y as usize, rgb_to_ycc(r, g, b));
            }
        }
        if !self.readouts.is_empty() {
            // The bar as an MSO5074 draws it, in divisions from the top left
            // of the area: a grey top border 7.2 down, 0.476 long plus 0.949
            // a readout, and each readout's label in its channel's colour
            // below it.
            let (left, top) = (CX - half_w, CY - half_h);
            let div = PX_PER_DIV as f64;
            let border_y = top + (7.2 * div) as usize;
            let right = left + ((0.476 + 0.949 * self.readouts.len() as f64) * div) as usize;
            let grey = rgb_to_ycc(52, 80, 84);
            for x in left + 4..=right {
                frame.put(x, border_y, grey);
                frame.put(x, border_y + 1, grey);
            }
            let (r, g, b) = MSO5000_COLOURS[0];
            for x in (left + 30..right.saturating_sub(10)).step_by(3) {
                frame.put(x, border_y + 15, rgb_to_ycc(r, g, b));
            }
        }
        if self.menu {
            for y in CY - half_h..=CY + half_h {
                for x in MENU_LEFT..=CX + half_w {
                    frame.put(x, y, (40, 128, 128));
                }
            }
        }
        frame
    }
}

struct FakeScpi {
    instrument: Arc<Mutex<Instrument>>,
    replies: VecDeque<Vec<u8>>,
}

impl Scpi for FakeScpi {
    /// Each `;`-separated command in turn, as the scope takes them; a header
    /// without its leading colon is the same command.
    fn write(&mut self, command: &str) -> Result<()> {
        let mut instrument = self.instrument.lock().unwrap();
        let mut answers = Vec::new();
        for part in command.split(';') {
            let part = part.trim();
            let part = if part.starts_with([':', '*']) {
                part.to_string()
            } else {
                format!(":{part}")
            };
            if part.contains('?') {
                let answer = instrument.query(&part);
                if !answer.is_empty() {
                    answers.push(answer);
                }
            } else {
                instrument.write(&part);
            }
        }
        if !answers.is_empty() {
            self.replies.push_back(answers.concat());
        }
        Ok(())
    }

    fn read_message(&mut self) -> Result<Vec<u8>> {
        self.replies.pop_front().context("read with no query outstanding")
    }
}

/// Each frame asked for is a fresh drawing of the instrument's screen, as a
/// card streaming continuously would deliver.
struct FakeScreen {
    instrument: Arc<Mutex<Instrument>>,
    sequence: Mutex<u64>,
}

impl FrameSource for FakeScreen {
    fn newest(&self) -> Result<u64> {
        Ok(*self.sequence.lock().unwrap())
    }

    fn next_after(&self, after: u64, _timeout: Duration) -> Result<Option<RawFrame>> {
        let mut sequence = self.sequence.lock().unwrap();
        *sequence += 1;
        if *sequence <= after {
            *sequence = after + 1;
        }
        let frame = self.instrument.lock().unwrap().draw();
        Ok(Some(RawFrame {
            encoding: Encoding::Yuyv,
            width: frame.width,
            height: frame.height,
            bytes: to_yuyv(&frame),
            sequence: *sequence,
            captured_ns: *sequence * FRAME_NS,
        }))
    }

    fn describe(&self) -> String {
        "the simulated screen".into()
    }
}

fn to_yuyv(frame: &VideoFrame) -> Vec<u8> {
    let mut out = Vec::with_capacity(frame.width * frame.height * 2);
    for y in 0..frame.height {
        for x in (0..frame.width).step_by(2) {
            let (y0, cb0, cr0) = frame.ycc(x, y);
            let (y1, cb1, cr1) = frame.ycc(x + 1, y);
            // The coloured pixel of a pair keeps its chroma.
            let (cb, cr) = if cb0.abs_diff(128) + cr0.abs_diff(128) >= cb1.abs_diff(128) + cr1.abs_diff(128) {
                (cb0, cr0)
            } else {
                (cb1, cr1)
            };
            out.extend_from_slice(&[y0, cb, y1, cr]);
        }
    }
    out
}

struct Bench {
    instrument: Arc<Mutex<Instrument>>,
    scope: RigolMso5000,
}

fn bench_with(instrument: Instrument) -> Result<Bench> {
    let instrument = Arc::new(Mutex::new(instrument));
    let scpi = FakeScpi {
        instrument: instrument.clone(),
        replies: VecDeque::new(),
    };
    let screen = FakeScreen {
        instrument: instrument.clone(),
        sequence: Mutex::new(0),
    };
    let scope = RigolMso5000::with(Box::new(scpi), Some(Box::new(screen)), None)?;
    Ok(Bench { instrument, scope })
}

fn bench() -> Bench {
    bench_with(Instrument::mso5204()).expect("the simulated scope opens")
}

impl Bench {
    fn sent(&self) -> Vec<String> {
        self.instrument.lock().unwrap().sent.clone()
    }

    /// Poll as the acquisition loop does until a capture is ready.
    fn capture(&mut self) -> CaptureFrame {
        for _ in 0..20 {
            if self.scope.is_ready().unwrap() {
                return self.scope.get_triggered_data().unwrap();
            }
            // The card keeps delivering frames between polls.
            let _ = self.scope.screen.as_ref().unwrap().video.next_after(0, Duration::ZERO);
        }
        panic!("no capture became ready");
    }
}

impl Bench {
    /// Run the acquisition loop for `seconds` of frames: capture, then
    /// re-arm, as the loop does after each one.
    fn run_for(&mut self, seconds: f64) -> CaptureFrame {
        let mut frame = self.capture();
        let until = frame.capture_mono_ns + (seconds * 1e9) as u64;
        while frame.capture_mono_ns < until {
            self.scope.rearm().unwrap();
            frame = self.capture();
        }
        frame
    }

    fn moff_presses(&self) -> usize {
        self.sent().iter().filter(|c| c.contains("MOFF")).count()
    }
}

/// Columns of channel A with a trace, out of those in the frame.
fn traced_columns(frame: &CaptureFrame) -> (usize, usize) {
    let samples = frame.channel_samples(0).unwrap();
    let traced = samples.chunks(2).filter(|pair| pair[0] != protocol::lscp::NO_SAMPLE).count();
    (traced, samples.len() / 2)
}

#[test]
fn a_side_menu_opened_after_pairing_is_hidden() {
    let mut bench = bench();
    let (traced, columns) = traced_columns(&bench.capture());
    assert_eq!(traced, columns, "paired with the full width in view");
    bench.instrument.lock().unwrap().menu = true;
    let covered = bench.capture();
    assert!(traced_columns(&covered).0 < columns * 85 / 100);

    // Not on the first frames, which could be a trace that ends there.
    bench.run_for(0.5);
    assert_eq!(bench.moff_presses(), 0);
    let frame = bench.run_for(1.5);
    assert_eq!(bench.moff_presses(), 1, "pressed once, after the check saw the menu");
    assert!(!bench.instrument.lock().unwrap().menu);
    assert_eq!(traced_columns(&frame).0, columns, "the full width is back");
    let instrument = bench.instrument.lock().unwrap();
    assert!(!instrument.grid, "the check leaves the grid off");
    assert_eq!(instrument.status, "AUTO", "and the scope running");
}

#[test]
fn a_menu_that_keeps_coming_back_is_looked_for_every_ten_seconds() {
    let mut bench = bench();
    bench.instrument.lock().unwrap().menu = true;
    bench.run_for(1.5);
    assert_eq!(bench.moff_presses(), 1);
    bench.instrument.lock().unwrap().menu = true;
    bench.run_for(5.0);
    assert_eq!(bench.moff_presses(), 1, "too soon after the last look");
    bench.run_for(6.0);
    assert_eq!(bench.moff_presses(), 2);
}

#[test]
fn a_trace_that_ends_by_itself_presses_nothing() {
    let mut bench = bench();
    bench.instrument.lock().unwrap().traces_end_at = Some(MENU_LEFT);
    let checks = |bench: &Bench| bench.sent().iter().filter(|c| c.as_str() == ":DISPlay:GRID FULL").count();
    let before = checks(&bench);
    bench.run_for(1.5);
    assert_eq!(checks(&bench), before + 1, "the screen was checked once");
    assert_eq!(bench.moff_presses(), 0, "and the check saw no menu");
    // The empty right is the signal's, so it is not checked again while it
    // stays that way.
    bench.run_for(12.0);
    assert_eq!(checks(&bench), before + 1);
}

fn pair_volts(frame: &CaptureFrame, channel: usize, column: usize) -> (f64, f64) {
    let samples = frame.channel_samples(channel).unwrap();
    let c = &frame.channels[channel];
    let volts = |count: i16| f64::from(count) * f64::from(c.scale_v_per_count) + f64::from(c.offset_v);
    (volts(samples[column * 2]), volts(samples[column * 2 + 1]))
}

#[test]
fn an_mso5204_opens_as_four_lettered_channels() {
    let bench = bench();
    let caps = bench.scope.capabilities().unwrap();
    assert_eq!(caps.family, DriverFamily::RigolMso5000);
    assert_eq!(caps.model, "MSO5204");
    assert_eq!(caps.analog_channels, 4);
    assert_eq!(caps.channel_labels, vec!["A", "B", "C", "D"]);
    assert_eq!(caps.bandwidth_hz, Some(200e6));
    assert!(caps.peak_detect && !caps.roll_mode);
}

#[test]
fn opening_measures_the_screen_and_leaves_the_grid_off() {
    let bench = bench();
    let map = bench.scope.screen.as_ref().unwrap().map;
    assert!((map.center_x - CX as f64).abs() < 0.5, "{map:?}");
    assert!((map.center_y - CY as f64).abs() < 0.5, "{map:?}");
    assert!((map.px_per_div_x - PX_PER_DIV as f64).abs() < 0.5, "{map:?}");
    let instrument = bench.instrument.lock().unwrap();
    assert!(!instrument.grid);
    assert_eq!(instrument.status, "AUTO", "left running as it was found");
}

fn keys_pressed(bench: &Bench) -> Vec<String> {
    bench
        .sent()
        .iter()
        .filter_map(|c| c.strip_prefix(":SYSTem:KEY:PRESs ").map(str::to_string))
        .collect()
}

#[test]
fn a_power_cycled_scope_has_its_hdmi_output_switched_on() {
    let mut instrument = Instrument::mso5204();
    instrument.on_hdmi = false;
    let mut bench = bench_with(instrument).expect("opens");
    assert!(bench.scope.live_view_problem().is_none());
    // Pairing found the HDMI page it was left on over the grid, and hid it.
    assert_eq!(keys_pressed(&bench), ["UTILity", "F1", "F6", "F1", "MOFF"]);
    {
        let instrument = bench.instrument.lock().unwrap();
        assert!(instrument.on_hdmi && !instrument.menu);
        assert_eq!(instrument.status, "AUTO", "left running as it was found");
    }
    let (traced, columns) = traced_columns(&bench.capture());
    assert_eq!(traced, columns);
}

#[test]
fn a_scope_with_no_picture_opens_without_a_live_view_and_pairs_when_it_returns() {
    // A scope whose picture does not reach the card. That used to fail the
    // open, so every command failed with it.
    let mut instrument = Instrument::mso5204();
    instrument.cable = false;
    let mut bench = bench_with(instrument).expect("opens without the picture");
    let problem = bench.scope.live_view_problem().expect("says why there is no live view");
    assert!(problem.contains("not showing this scope"), "{problem}");
    assert!(problem.contains("Utility > IO > HDMI > Output"), "{problem}");
    // Switching the output did not help, so it was put back as it was.
    assert!(problem.contains("did not bring a picture"), "{problem}");
    assert_eq!(keys_pressed(&bench), ["UTILity", "F1", "F6", "F1", "UTILity", "F1", "F6", "F1", "MOFF"]);
    {
        let instrument = bench.instrument.lock().unwrap();
        assert!(instrument.on_hdmi && !instrument.menu);
    }
    assert_eq!(bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Vmax).unwrap().unwrap(), Some(1.0));

    // Starting still runs the scope, and tries the card again, but not the
    // scope's menus.
    bench.scope.start_triggered_capture(50.0).unwrap();
    assert_eq!(keys_pressed(&bench).len(), 9);
    assert_eq!(bench.instrument.lock().unwrap().status, "AUTO");
    assert!(bench.scope.live_view_problem().is_some());
    let message = format!("{:#}", bench.scope.get_triggered_data().unwrap_err());
    assert!(message.contains("no live view"), "{message}");

    // The cable back: the next start pairs, and captures follow.
    bench.instrument.lock().unwrap().cable = true;
    bench.scope.start_triggered_capture(50.0).unwrap();
    assert!(bench.scope.live_view_problem().is_none());
    assert!(bench.scope.screen.is_some());
    let frame = bench.capture();
    assert!(!frame.channels.is_empty());
}

/// A scope whose capture card, `usb:3-3`, opens only while `plugged` is
/// set; the number of tries is counted.
fn bench_with_card(plugged: bool) -> (Bench, Arc<std::sync::atomic::AtomicBool>, Arc<std::sync::atomic::AtomicUsize>) {
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
    let instrument = Arc::new(Mutex::new(Instrument::mso5204()));
    let scpi = FakeScpi {
        instrument: instrument.clone(),
        replies: VecDeque::new(),
    };
    let present = Arc::new(AtomicBool::new(plugged));
    let tries = Arc::new(AtomicUsize::new(0));
    let open_card: CardOpener = {
        let (instrument, present, tries) = (instrument.clone(), present.clone(), tries.clone());
        Box::new(move |spec: &str| {
            tries.fetch_add(1, Ordering::SeqCst);
            assert_eq!(spec, "usb:3-3");
            if !present.load(Ordering::SeqCst) {
                bail!("no capture card is on USB port 3-3; check that it is plugged into the port the net was created with (video devices are on USB ports 3-2.4.2)");
            }
            Ok(Box::new(FakeScreen {
                instrument: instrument.clone(),
                sequence: Mutex::new(0),
            }) as Box<dyn FrameSource>)
        })
    };
    let scope = RigolMso5000::with_card_named(Box::new(scpi), "usb:3-3", open_card).expect("opens");
    (Bench { instrument, scope }, present, tries)
}

#[test]
fn a_capture_card_that_cannot_be_opened_leaves_the_scope_usable() {
    // It used to fail the whole open, so the daemon refused status,
    // settings and measurements as well as the live view.
    let (mut bench, _, tries) = bench_with_card(false);
    let problem = bench.scope.live_view_problem().expect("says why there is no live view");
    assert!(problem.contains("opening the capture card usb:3-3"), "{problem}");
    assert!(problem.contains("video devices are on USB ports 3-2.4.2"), "{problem}");
    assert!(problem.contains("lager nets set-capture"), "{problem}");
    assert!(!problem.contains("HDMI output"), "the output is not the problem: {problem}");
    assert_eq!(bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Vmax).unwrap().unwrap(), Some(1.0));
    bench.scope.set_volts_per_div(ChannelId::Alphabetic('A'), 2.0).unwrap();
    assert_eq!(bench.scope.get_volts_per_div(ChannelId::Alphabetic('A')).unwrap(), 2.0);
    let message = format!("{:#}", bench.scope.get_triggered_data().unwrap_err());
    assert!(message.contains("no live view"), "{message}");
    assert_eq!(tries.load(std::sync::atomic::Ordering::SeqCst), 1, "tried once, at open");
}

#[test]
fn a_capture_card_plugged_back_in_pairs_at_the_next_start() {
    use std::sync::atomic::Ordering;
    let (mut bench, present, tries) = bench_with_card(false);
    // Still missing: starting runs the scope, and says the same.
    bench.scope.start_triggered_capture(50.0).unwrap();
    assert_eq!(bench.instrument.lock().unwrap().status, "AUTO");
    assert!(bench.scope.live_view_problem().unwrap().contains("set-capture"));
    assert_eq!(tries.load(Ordering::SeqCst), 2);

    present.store(true, Ordering::SeqCst);
    bench.scope.start_triggered_capture(50.0).unwrap();
    assert!(bench.scope.live_view_problem().is_none());
    assert_eq!(tries.load(Ordering::SeqCst), 3);
    let frame = bench.capture();
    assert!(!frame.channels.is_empty());
    // Paired now: a later start opens nothing more.
    bench.scope.start_triggered_capture(50.0).unwrap();
    assert_eq!(tries.load(Ordering::SeqCst), 3);
}

#[test]
fn a_capture_card_that_opens_pairs_at_once() {
    let (mut bench, _, tries) = bench_with_card(true);
    assert!(bench.scope.live_view_problem().is_none());
    assert!(!bench.capture().channels.is_empty());
    assert_eq!(tries.load(std::sync::atomic::Ordering::SeqCst), 1);
}

#[test]
fn another_instrument_is_refused_by_name() {
    let mut instrument = Instrument::mso5204();
    instrument.idn = "RIGOL TECHNOLOGIES,DS1104Z,DS1ZA000001,00.04.04".into();
    let message = format!("{:#}", bench_with(instrument).err().unwrap());
    assert!(message.contains("MSO5000 series"), "{message}");
}

#[test]
fn a_capture_is_an_envelope_in_volts() {
    let mut bench = bench();
    let frame = bench.capture();
    assert!(frame.is_envelope());
    assert_eq!(frame.channels.len(), 1, "only A is on");
    assert_eq!(frame.channels[0].channel, ChannelId::Alphabetic('A'));
    let (min, max) = pair_volts(&frame, 0, 100);
    assert!((min - 1.0).abs() < 0.03 && (max - 1.0).abs() < 0.03, "{min} {max}");
    // 1 ms/div across 50 pixels a division: 20 us a column, two samples each.
    assert!((frame.sample_interval_ns - 10_000.0).abs() < 1.0, "{}", frame.sample_interval_ns);
    // The trigger sits mid-screen with no offset.
    assert_eq!(frame.pre_trigger_samples, 500);
}

#[test]
fn a_change_holds_captures_back_until_the_screen_has_redrawn() {
    let mut bench = bench();
    bench.capture();
    bench.scope.set_volts_per_div(ChannelId::Alphabetic('A'), 0.5).unwrap();
    assert!(!bench.scope.is_ready().unwrap(), "the next frame predates the change");
    let frame = bench.capture();
    let (min, _) = pair_volts(&frame, 0, 100);
    assert!((min - 1.0).abs() < 0.02, "still 1 V, now at 0.5 V/div: {min}");
}

#[test]
fn settings_read_back_what_the_scope_rounded_and_clamped_to() {
    let mut bench = bench();
    bench.scope.set_volts_per_div(ChannelId::Alphabetic('A'), 0.3).unwrap();
    assert_eq!(bench.scope.get_volts_per_div(ChannelId::Alphabetic('A')).unwrap(), 0.2);
    bench.scope.set_trigger_level(50.0).unwrap();
    assert_eq!(bench.scope.get_trigger_level().unwrap(), 1.0, "clamped to the screen");
}

#[test]
fn a_command_the_scope_refuses_is_an_error_not_silence() {
    let mut bench = bench();
    let message = bench
        .scope
        .set_trigger_slope(TriggerSlope::Neither)
        .unwrap_err()
        .to_string();
    assert!(message.contains("neither"), "{message}");
    // One the scope itself rejects comes back from its error queue.
    bench.instrument.lock().unwrap().idn.clear();
    let refused = Oscilloscope::scpi(&mut bench.scope, ":BOGus:COMMand 1").unwrap();
    let message = refused.unwrap_err().to_string();
    assert!(message.contains("Undefined header"), "{message}");
}

#[test]
fn channels_map_to_letters_both_ways() {
    let mut bench = bench();
    bench.scope.enable_channel(ChannelId::Alphabetic('B')).unwrap();
    assert!(bench.sent().contains(&":CHANnel2:DISPlay ON".to_string()));
    bench.scope.set_trigger_source(ChannelId::Alphabetic('C')).unwrap();
    assert_eq!(bench.scope.get_trigger_source().unwrap(), ChannelId::Alphabetic('C'));
    assert!(bench.scope.enable_channel(ChannelId::Alphabetic('E')).is_err());
    let frame = bench.capture();
    let (b_min, _) = pair_volts(&frame, 1, 10);
    assert!((b_min + 2.0).abs() < 0.03, "B carries -2 V: {b_min}");
}

#[test]
fn the_offset_moves_the_trigger_point_and_the_position_follows() {
    let mut bench = bench();
    // Two divisions later: the trigger moves two divisions left.
    bench.scope.set_time_offset(2e-3).unwrap();
    assert!((bench.scope.get_trigger_position().unwrap() - 30.0).abs() < 1e-9);
    let frame = bench.capture();
    assert_eq!(frame.pre_trigger_samples, 2 * (250 - 100));
}

#[test]
fn a_hidden_channel_takes_its_scale_and_offset_and_stays_hidden() {
    let mut bench = bench();
    let c = ChannelId::Alphabetic('C');
    bench.scope.disable_channel(c).unwrap();
    bench.scope.set_volts_per_div(c, 2.0).unwrap();
    bench.scope.set_volts_offset(c, 1.5).unwrap();
    assert_eq!(bench.scope.get_volts_per_div(c).unwrap(), 2.0);
    assert_eq!(bench.scope.get_volts_offset(c).unwrap(), 1.5);
    let instrument = bench.instrument.lock().unwrap();
    assert_eq!((instrument.channels[2].scale, instrument.channels[2].offset), (2.0, 1.5));
    assert!(!instrument.channels[2].display);
}

#[test]
fn the_trigger_comes_from_the_scope_status() {
    let mut bench = bench();
    let untriggered = bench.capture();
    assert!(!untriggered.is_triggered(), "AUTO means it ran without a trigger");
    bench.instrument.lock().unwrap().status = "TD";
    let triggered = bench.capture();
    assert!(triggered.is_triggered());
}

#[test]
fn single_waits_for_the_scope_to_stop_on_its_trigger() {
    let mut bench = bench();
    bench.scope.set_capture_mode(CaptureMode::Single).unwrap();
    bench.scope.start_triggered_capture(50.0).unwrap();
    for _ in 0..5 {
        let _ = bench.scope.screen.as_ref().unwrap().video.next_after(0, Duration::ZERO);
        assert!(!bench.scope.is_ready().unwrap(), "still waiting for a trigger");
    }
    bench.instrument.lock().unwrap().status = "STOP";
    let frame = bench.capture();
    assert!(frame.is_triggered());
}

#[test]
fn measurements_are_computed_from_the_channel_s_record() {
    let mut bench = bench();
    bench.scope.enable_channel(ChannelId::Alphabetic('B')).unwrap();
    let (sent, asked) = {
        let instrument = bench.instrument.lock().unwrap();
        (instrument.sent.len(), instrument.queries.len())
    };
    let set = bench.scope.measure_all(ChannelId::Alphabetic('A')).unwrap().unwrap();
    let instrument = bench.instrument.lock().unwrap();
    let sent = instrument.sent[sent..].to_vec();
    let asked = instrument.queries[asked..].to_vec();
    // Nothing asked of the scope's measurements: each query stalls the live
    // view and draws a readout over the trace.
    assert!(
        !sent.iter().chain(&asked).any(|c| c.to_ascii_uppercase().starts_with(":MEAS")),
        "{sent:?} {asked:?}"
    );
    assert_eq!(
        sent.iter().filter(|c| c.starts_with(":WAVeform:SOURce")).collect::<Vec<_>>(),
        [":WAVeform:SOURce CHANnel1"],
        "only the measured channel is read, though B is on too"
    );
    assert!(instrument.readouts.is_empty());
    drop(instrument);
    let record = bench.scope.record(&[0]).unwrap();
    let computed = protocol::measure_channel(&record, 0).unwrap();
    // The fake's record is a ramp from 0 to 0.9 V over nine samples, so its
    // rise spans seven of them: resolved, and kept.
    assert_eq!(set, computed);
    assert!(set.rise_time.is_some());
    assert!((set.vmax - 0.9).abs() < 1e-6 && set.vmin.abs() < 1e-6, "{set:?}");
    let off = bench.scope.measure_all(ChannelId::Alphabetic('D')).unwrap();
    assert!(off.unwrap_err().to_string().contains("not enabled"));
}

#[test]
fn an_edge_the_record_does_not_resolve_is_left_out_of_the_set() {
    let mut bench = bench();
    // A square wave whose edges each fall between two samples.
    bench.instrument.lock().unwrap().record = [127, 127, 167, 167, 127, 127, 167, 167, 127, 127].to_vec();
    let set = bench.scope.measure_all(ChannelId::Alphabetic('A')).unwrap().unwrap();
    assert_eq!((set.rise_time, set.fall_time), (None, None), "{set:?}");
    assert!((set.vpp - 1.0).abs() < 1e-6, "the levels are still the record's: {set:?}");
}

#[test]
fn one_measurement_asks_the_scope_for_that_item_alone() {
    let mut bench = bench();
    let before = bench.sent().len();
    let duty = bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::DutyCyclePositive);
    assert_eq!(duty.unwrap().unwrap(), Some(25.0), "a ratio, reported in percent");
    // The source was CH1 already; the readout the query added is cleared.
    assert_eq!(bench.sent()[before..], [":MEASure:CLEar ITEM1"]);
    let period = bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Period);
    assert_eq!(period.unwrap().unwrap(), None, "9.9E37 is no measurement");
}

#[test]
fn measuring_one_item_leaves_no_measurement_bar_over_the_trace() {
    let mut bench = bench();
    bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Vmax).unwrap().unwrap();
    assert!(bench.instrument.lock().unwrap().readouts.is_empty());
    assert!(!bench.sent().iter().any(|c| c.contains("CLEar ALL")));
}

#[test]
fn measuring_keeps_the_readouts_the_user_put_on_the_scope() {
    let mut bench = bench();
    bench.instrument.lock().unwrap().readouts = vec!["FREQUENCY".into(), "VMIN".into()];
    bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Vpp).unwrap().unwrap();
    // VPP went to the front, as the third readout, and only it was cleared.
    assert_eq!(bench.instrument.lock().unwrap().readouts, ["FREQUENCY", "VMIN"]);
    assert!(bench.sent().contains(&":MEASure:CLEar ITEM3".to_string()), "{:?}", bench.sent());
}

#[test]
fn measuring_an_item_the_user_already_shows_clears_nothing() {
    let mut bench = bench();
    bench.instrument.lock().unwrap().readouts = vec!["FREQUENCY".into(), "VPP".into()];
    bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Vpp).unwrap().unwrap();
    // The scope moved the user's own readout to the front; it stays.
    assert_eq!(bench.instrument.lock().unwrap().readouts, ["VPP", "FREQUENCY"]);
    assert!(!bench.sent().iter().any(|c| c.starts_with(":MEASure:CLEar")), "{:?}", bench.sent());
}

#[test]
fn measuring_puts_the_user_s_measurement_source_back() {
    let mut bench = bench();
    bench.scope.enable_channel(ChannelId::Alphabetic('B')).unwrap();
    bench.instrument.lock().unwrap().measure_source = 1;
    bench.scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Vmax).unwrap().unwrap();
    let sent = bench.sent();
    assert!(sent.contains(&":MEASure:SOURce CHANnel1".to_string()), "{sent:?}");
    assert_eq!(bench.instrument.lock().unwrap().measure_source, 1, "back on CH2");
}

#[test]
fn without_a_live_view_a_measurement_clears_nothing() {
    let instrument = Arc::new(Mutex::new(Instrument::mso5204()));
    instrument.lock().unwrap().readouts = vec!["FREQUENCY".into()];
    let scpi = FakeScpi { instrument: instrument.clone(), replies: VecDeque::new() };
    let mut scope = RigolMso5000::with(Box::new(scpi), None, None).unwrap();
    scope.measure_one(ChannelId::Alphabetic('A'), Measurement::Vmax).unwrap().unwrap();
    // With nothing to see the bar in, the user's readout cannot be told
    // from the one added, so both are left.
    let instrument = instrument.lock().unwrap();
    assert_eq!(instrument.readouts, ["VMAX", "FREQUENCY"]);
    assert!(!instrument.sent.iter().any(|c| c.starts_with(":MEASure:CLEar")));
}

#[test]
fn a_readout_on_the_scope_is_not_read_as_trace() {
    let mut bench = bench();
    bench.instrument.lock().unwrap().readouts = vec!["FREQUENCY".into(), "VPP".into(), "VMAX".into()];
    let frame = bench.capture();
    let volts = frame.channel_volts(0).unwrap();
    let lowest = volts.iter().copied().filter(|v| v.is_finite()).fold(f64::MAX, f64::min);
    // Channel A is a 1 V level; the readouts' labels are 3.5 div down.
    assert!((lowest - 1.0).abs() < 0.1, "channel A reaches {lowest} V");
}

#[test]
fn a_query_the_scope_rejects_names_itself_at_once() {
    let mut bench = bench();
    let refused = Oscilloscope::scpi(&mut bench.scope, ":MEASure:MPARea?").unwrap().unwrap_err();
    let message = format!("{refused:#}");
    assert!(message.contains(":MEASure:MPARea?") && message.contains("-100"), "{message}");
    // Nothing left in the queue for the next command to be blamed for.
    Oscilloscope::scpi(&mut bench.scope, ":MEASure:CLEar ALL").unwrap().unwrap();
}

#[test]
fn a_raw_measurement_query_leaves_the_bar_as_it_was() {
    let mut bench = bench();
    let reply = Oscilloscope::scpi(&mut bench.scope, ":MEASure:VMAX?").unwrap().unwrap();
    assert!(reply.is_some());
    assert!(bench.instrument.lock().unwrap().readouts.is_empty());
    // A setting read is not a measurement, and adds nothing to clear.
    let before = bench.sent().len();
    Oscilloscope::scpi(&mut bench.scope, ":MEASure:SOURce?").unwrap().unwrap();
    assert_eq!(bench.sent().len(), before);
}

#[test]
fn only_a_single_item_query_counts_as_a_measurement() {
    assert!(is_measurement_query(":MEASure:VPP?"));
    assert!(is_measurement_query("meas:rtim?"));
    assert!(!is_measurement_query(":MEASure:SOURce?"));
    assert!(!is_measurement_query(":MEASure:STATistic:ITEM? MAX,VPP"));
    assert!(!is_measurement_query(":MEASure:VPP?;*OPC?"));
    assert!(!is_measurement_query(":MEASure:CLEar ALL"));
    assert!(!is_measurement_query(":TRIGger:STATus?"));
}

#[test]
fn the_daemon_starting_or_stopping_leaves_the_scope_running() {
    let mut bench = bench();
    bench.scope.start_triggered_capture(50.0).unwrap();
    let before = bench.sent().len();
    bench.scope.release_capture().unwrap();
    assert_eq!(bench.sent().len(), before, "nothing sent, so no :STOP");
}

#[test]
fn the_record_is_the_scope_s_exact_samples() {
    let mut bench = bench();
    let record = bench.scope.read_record().unwrap().unwrap();
    assert!(!record.is_envelope());
    assert_eq!(record.samples_per_channel, 10);
    assert_eq!(record.pre_trigger_samples, 5);
    assert_eq!(record.sample_interval_ns, 1000.0);
    let volts = record.channel_volts(0).unwrap();
    assert!((volts[0] - 0.0).abs() < 1e-6 && (volts[9] - 0.9).abs() < 1e-6, "{volts:?}");
}

#[test]
fn averaging_and_peak_detect_are_the_scope_s() {
    let mut bench = bench();
    bench.scope.set_acquisition(AcquisitionMode::Average, 64).unwrap().unwrap();
    {
        let instrument = bench.instrument.lock().unwrap();
        assert_eq!((instrument.acquire_type, instrument.averages), ("AVER", 64));
    }
    let refused = bench.scope.set_acquisition(AcquisitionMode::Average, 10).unwrap();
    assert!(refused.unwrap_err().to_string().contains("power of two"));
    bench.scope.set_acquisition(AcquisitionMode::Peak, 64).unwrap().unwrap();
    assert_eq!(bench.instrument.lock().unwrap().acquire_type, "PEAK");
    bench.scope.set_holdoff(0.25).unwrap().unwrap();
    assert_eq!(bench.instrument.lock().unwrap().holdoff, 0.25);
}

#[test]
fn raw_scpi_answers_queries_and_keeps_the_cache_true() {
    let mut bench = bench();
    let idn = Oscilloscope::scpi(&mut bench.scope, "*IDN?").unwrap().unwrap();
    assert!(idn.unwrap().contains("MSO5204"));
    // Changed behind the daemon's back, then read back through it.
    Oscilloscope::scpi(&mut bench.scope, ":TIMebase:MAIN:SCALe 0.002")
        .unwrap()
        .unwrap();
    assert_eq!(bench.scope.get_time_per_div().unwrap(), 0.002);
}

#[test]
fn a_new_scale_moves_the_offset_the_live_view_converts_with() {
    let mut bench = bench();
    let a = ChannelId::Alphabetic('A');
    bench.scope.set_volts_per_div(a, 2.0).unwrap();
    bench.scope.set_volts_offset(a, 2.56).unwrap();
    // The scope keeps the offset in divisions: 2.56 V at 2 V/div is
    // 1.28 V at 1 V/div.
    bench.scope.set_volts_per_div(a, 1.0).unwrap();
    assert!((bench.scope.get_volts_offset(a).unwrap() - 1.28).abs() < 1e-9);
    let frame = bench.capture();
    assert!((frame.channels[0].offset_v + 1.28).abs() < 1e-6, "{}", frame.channels[0].offset_v);
}

#[test]
fn every_form_of_a_raw_setting_refreshes_the_cache() {
    let a = ChannelId::Alphabetic('A');
    for (command, offset) in [
        (":CHANnel1:OFFSet 0.5", 0.5),
        ("CHANnel1:OFFSet 0.6", 0.6),
        (":TIMebase:MAIN:SCALe 0.001;:CHANnel1:OFFSet 0.7", 0.7),
        (":CHANnel1:OFFSet 0.8;*OPC?", 0.8),
    ] {
        let mut bench = bench();
        Oscilloscope::scpi(&mut bench.scope, command).unwrap().unwrap();
        assert_eq!(bench.scope.get_volts_offset(a).unwrap(), offset, "{command}");
    }
}

#[test]
fn a_raw_query_alone_reads_nothing_back() {
    let mut bench = bench();
    let before = bench.sent().len();
    Oscilloscope::scpi(&mut bench.scope, ":CHANnel1:OFFSet?").unwrap().unwrap();
    assert_eq!(bench.sent().len(), before, "nothing written, so nothing re-read");
}

#[test]
fn without_a_capture_card_everything_but_the_live_view_works() {
    let instrument = Arc::new(Mutex::new(Instrument::mso5204()));
    let scpi = FakeScpi {
        instrument: instrument.clone(),
        replies: VecDeque::new(),
    };
    let mut scope = RigolMso5000::with(Box::new(scpi), None, None).unwrap();
    assert!(instrument.lock().unwrap().grid, "the display is left as it was");
    scope.set_volts_per_div(ChannelId::Alphabetic('A'), 2.0).unwrap();
    assert!(scope.measure_all(ChannelId::Alphabetic('A')).unwrap().is_ok());
    assert!(scope.read_record().unwrap().is_ok());
    assert!(!scope.is_ready().unwrap());
    let message = scope.get_triggered_data().unwrap_err().to_string();
    assert!(message.contains("no capture card"), "{message}");
}

#[test]
fn identities_give_channels_and_bandwidth() {
    let two = Identity::parse("RIGOL TECHNOLOGIES,MSO5072,MS5A1,00.01.03").unwrap();
    assert_eq!(two.channels().unwrap(), 2);
    assert_eq!(two.bandwidth_hz(), Some(70e6));
    let four = Identity::parse("RIGOL TECHNOLOGIES,MSO5354,MS5A2,00.01.03").unwrap();
    assert_eq!(four.channels().unwrap(), 4);
    assert_eq!(four.bandwidth_hz(), Some(350e6));
    assert!(Identity::parse("KEYSIGHT,DSOX1204G,1,2").is_err());
}

#[test]
fn the_volts_per_div_ladder_is_1_2_5_from_500_uv_to_10_v() {
    let ladder = volts_per_div_ladder();
    assert_eq!(ladder.first(), Some(&5e-4));
    assert_eq!(ladder.last(), Some(&10.0));
    assert_eq!(ladder.len(), 14);
    assert!(ladder.windows(2).all(|w| w[1] / w[0] <= 2.5 + 1e-9));
}

#[test]
fn without_hdmi_the_live_view_is_read_from_the_screenshot() {
    let instrument = Arc::new(Mutex::new(Instrument::mso5204()));
    let scpi = FakeScpi {
        instrument: instrument.clone(),
        replies: VecDeque::new(),
    };
    let scope = RigolMso5000::with_screen_dump(Box::new(scpi), None).unwrap();
    let map = scope.screen.as_ref().unwrap().map;
    assert!((map.px_per_div_x - PX_PER_DIV as f64).abs() < 0.5, "{map:?}");
    assert!(!instrument.lock().unwrap().grid);

    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    while !scope.is_ready().unwrap() {
        assert!(std::time::Instant::now() < deadline, "no capture became ready");
        std::thread::sleep(Duration::from_millis(5));
    }
    let frame = scope.get_triggered_data().unwrap();
    let (min, max) = pair_volts(&frame, 0, 100);
    assert!((min - 1.0).abs() < 0.03 && (max - 1.0).abs() < 0.03, "{min} {max}");
}

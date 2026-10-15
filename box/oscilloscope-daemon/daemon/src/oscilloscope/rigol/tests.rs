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
    /// Whether the screen the card sees follows this instrument at all.
    on_hdmi: bool,
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
        }
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
            ":MEASURE:SOURCE" => match arg.strip_prefix("CHANNEL").and_then(|n| n.parse::<usize>().ok()) {
                Some(n) => {
                    self.measure_source = n - 1;
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
                        // Rounded to 1-2-5, as the scope does.
                        "SCALE" => number()
                            .map(|v| {
                                c.scale = volts_per_div_ladder()
                                    .into_iter()
                                    .min_by(|a, b| (a - v).abs().total_cmp(&(b - v).abs()))
                                    .unwrap()
                                    * c.probe
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

    fn query(&mut self, query: &str) -> Vec<u8> {
        let upper = query.trim().to_ascii_uppercase();
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
            ":MEASURE:VMAX?" => format!("{:e}", channel(self.measure_source).signal),
            ":MEASURE:VMIN?" => format!("{:e}", channel(self.measure_source).signal),
            ":MEASURE:VPP?" => "0.000000e+00".into(),
            ":MEASURE:VAVG?" | ":MEASURE:VRMS?" => format!("{:e}", channel(self.measure_source).signal),
            ":MEASURE:PDUTY?" => "2.500000e-01".into(),
            ":MEASURE:NDUTY?" => "7.500000e-01".into(),
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
                let mut reply = b"#210".to_vec();
                reply.extend((0..10).map(|i| 127 + i * 4));
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
        if !self.on_hdmi {
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
            for x in CX - half_w..=CX + half_w {
                frame.put(x, y as usize, rgb_to_ycc(r, g, b));
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
    fn write(&mut self, command: &str) -> Result<()> {
        let mut instrument = self.instrument.lock().unwrap();
        if command.contains('?') {
            let reply = instrument.query(command);
            self.replies.push_back(reply);
        } else {
            instrument.write(command);
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
            captured_ns: 0,
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

#[test]
fn a_capture_card_showing_something_else_is_refused() {
    let mut instrument = Instrument::mso5204();
    instrument.on_hdmi = false;
    let message = format!("{:#}", bench_with(instrument).err().unwrap());
    assert!(message.contains("not showing this scope"), "{message}");
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
fn measurements_are_the_scope_s_own_with_ratios_in_percent() {
    let mut bench = bench();
    let set = bench.scope.measure_all(ChannelId::Alphabetic('A')).unwrap().unwrap();
    assert_eq!(set.vmax, 1.0);
    assert_eq!(set.duty_cycle_positive, Some(25.0));
    assert_eq!(set.period, None, "9.9E37 is no measurement");
    assert!(bench.sent().contains(&":MEASure:SOURce CHANnel1".to_string()));
    let off = bench.scope.measure_all(ChannelId::Alphabetic('D')).unwrap();
    assert!(off.unwrap_err().to_string().contains("not enabled"));
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

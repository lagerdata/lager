// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! Tests for the DS1000Z backend against a simulated scope: enough of the
//! DS1000Z's SCPI to open, set, measure and read the screen record, with an
//! error queue that refuses what it does not know.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use anyhow::Result;
use protocol::lscp::FLAG_TRIGGERED;
use protocol::{CaptureMode, ChannelId, DriverFamily, Measurement, TriggerSlope};

use super::{HORIZONTAL_DIVISIONS, Identity, RigolDs1000z};
use crate::oscilloscope::Oscilloscope;
use crate::oscilloscope::rigol::scpi::Scpi;

const POINTS: usize = 1200;
/// Counts per vertical division in the DS1000Z's byte record.
const COUNTS_PER_DIV: f64 = 25.0;
const Y_REFERENCE: f64 = 127.0;

#[derive(Debug, Clone)]
struct Channel {
    display: bool,
    scale: f64,
    offset: f64,
    probe: f64,
    coupling: &'static str,
}

#[derive(Debug)]
struct State {
    idn: String,
    channels: Vec<Channel>,
    time_scale: f64,
    time_offset: f64,
    level: f64,
    source: usize,
    slope: String,
    sweep: String,
    status: &'static str,
    wave_source: usize,
    /// Byte the record holds for every point of channel n, before Y_REFERENCE.
    counts: Vec<i16>,
    measurements: Vec<(String, String)>,
    errors: VecDeque<String>,
    sent: Vec<String>,
    reply: Option<Vec<u8>>,
    max_transfer: Option<u32>,
}

impl State {
    fn ds1054z() -> State {
        State {
            idn: "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA000000001,00.04.04.SP4".into(),
            channels: vec![
                Channel {
                    display: true,
                    scale: 1.0,
                    offset: 0.0,
                    probe: 10.0,
                    coupling: "DC",
                },
                Channel {
                    display: false,
                    scale: 1.0,
                    offset: 0.0,
                    probe: 10.0,
                    coupling: "DC",
                },
                Channel {
                    display: false,
                    scale: 1.0,
                    offset: 0.0,
                    probe: 10.0,
                    coupling: "AC",
                },
                Channel {
                    display: false,
                    scale: 1.0,
                    offset: 0.0,
                    probe: 10.0,
                    coupling: "DC",
                },
            ],
            time_scale: 1e-3,
            time_offset: 0.0,
            level: 0.5,
            source: 0,
            slope: "POS".into(),
            sweep: "AUTO".into(),
            status: "TD",
            wave_source: 0,
            counts: vec![25, -50, 0, 0],
            measurements: Vec::new(),
            errors: VecDeque::new(),
            sent: Vec::new(),
            reply: None,
            max_transfer: None,
        }
    }

    fn answer(&mut self, text: &str) {
        self.reply = Some(format!("{text}\n").into_bytes());
    }

    /// One command, without `;`. Queries leave their reply for the read.
    fn handle(&mut self, command: &str) {
        let c = command.trim().trim_start_matches(':');
        let upper = c.to_ascii_uppercase();
        let (head, arg) = upper
            .split_once(' ')
            .map_or((upper.as_str(), ""), |(h, a)| (h, a.trim()));
        let raw_arg = c.split_once(' ').map_or("", |(_, a)| a.trim());
        if let Some(rest) = head
            .strip_prefix("CHANNEL")
            .or_else(|| head.strip_prefix("CHAN"))
        {
            let (n, field) = rest.split_once(':').unwrap_or((rest, ""));
            let i: usize = n.parse::<usize>().unwrap_or(0).wrapping_sub(1);
            if i >= self.channels.len() {
                return self.refuse();
            }
            let ch = &mut self.channels[i];
            match field {
                "DISPLAY?" | "DISP?" => {
                    let v = u8::from(ch.display);
                    self.answer(&v.to_string());
                }
                "DISPLAY" | "DISP" => ch.display = arg == "ON" || arg == "1",
                "SCALE?" | "SCAL?" => {
                    let v = ch.scale;
                    self.answer(&format!("{v:e}"));
                }
                "SCALE" | "SCAL" => ch.scale = raw_arg.parse().unwrap_or(ch.scale),
                "OFFSET?" | "OFFS?" => {
                    let v = ch.offset;
                    self.answer(&format!("{v:e}"));
                }
                "OFFSET" | "OFFS" => ch.offset = raw_arg.parse().unwrap_or(ch.offset),
                "PROBE?" | "PROB?" => {
                    let v = ch.probe;
                    self.answer(&format!("{v:e}"));
                }
                "PROBE" | "PROB" => ch.probe = raw_arg.parse().unwrap_or(ch.probe),
                "COUPLING?" | "COUP?" => {
                    let v = ch.coupling;
                    self.answer(v);
                }
                "COUPLING" | "COUP" => {
                    ch.coupling = match arg {
                        "AC" => "AC",
                        "GND" => "GND",
                        _ => "DC",
                    }
                }
                _ => self.refuse(),
            }
            return;
        }
        match head {
            "*IDN?" => {
                let idn = self.idn.clone();
                self.answer(&idn);
            }
            "*CLS" => self.errors.clear(),
            "*OPC?" => self.answer("1"),
            "SYSTEM:ERROR?" | "SYST:ERR?" => {
                let e = self
                    .errors
                    .pop_front()
                    .unwrap_or_else(|| "0,\"No error\"".into());
                self.answer(&e);
            }
            "TIMEBASE:MAIN:SCALE?" => {
                let v = self.time_scale;
                self.answer(&format!("{v:e}"));
            }
            "TIMEBASE:MAIN:SCALE" => self.time_scale = raw_arg.parse().unwrap_or(self.time_scale),
            "TIMEBASE:MAIN:OFFSET?" => {
                let v = self.time_offset;
                self.answer(&format!("{v:e}"));
            }
            "TIMEBASE:MAIN:OFFSET" => {
                self.time_offset = raw_arg.parse().unwrap_or(self.time_offset)
            }
            "TRIGGER:EDGE:LEVEL?" => {
                let v = self.level;
                self.answer(&format!("{v:e}"));
            }
            "TRIGGER:EDGE:LEVEL" => self.level = raw_arg.parse().unwrap_or(self.level),
            "TRIGGER:EDGE:SOURCE?" => {
                let v = format!("CHAN{}", self.source + 1);
                self.answer(&v);
            }
            "TRIGGER:EDGE:SOURCE" => {
                self.source = arg
                    .trim_start_matches("CHANNEL")
                    .trim_start_matches("CHAN")
                    .parse::<usize>()
                    .unwrap_or(1)
                    - 1;
            }
            "TRIGGER:EDGE:SLOPE?" => {
                let v = self.slope.clone();
                self.answer(&v);
            }
            "TRIGGER:EDGE:SLOPE" => self.slope = arg.into(),
            "TRIGGER:SWEEP?" => {
                let v = self.sweep.clone();
                self.answer(&v);
            }
            "TRIGGER:SWEEP" => self.sweep = arg.into(),
            "TRIGGER:MODE" | "TRIGGER:HOLDOFF" | "RUN" | "TFORCE" => {}
            "STOP" => self.status = "STOP",
            "SINGLE" => self.status = "WAIT",
            // As a DS1054Z autoscaled onto its 1 kHz probe-comp square.
            "AUTOSCALE" | "AUT" => {
                self.time_scale = 2e-4;
                self.channels[0].scale = 1.0;
                self.sweep = "AUTO".into();
            }
            "TRIGGER:STATUS?" => {
                let v = self.status;
                self.answer(v);
            }
            "ACQUIRE:SRATE?" => self.answer("2.500000e+08"),
            "ACQUIRE:MDEPTH?" => self.answer("AUTO"),
            "WAVEFORM:MODE" | "WAVEFORM:FORMAT" => {}
            "WAVEFORM:SOURCE" => {
                self.wave_source = arg
                    .trim_start_matches("CHANNEL")
                    .trim_start_matches("CHAN")
                    .parse::<usize>()
                    .unwrap_or(1)
                    - 1;
            }
            "WAVEFORM:PREAMBLE?" => {
                let ch = &self.channels[self.wave_source];
                let x_inc = self.time_scale * HORIZONTAL_DIVISIONS / POINTS as f64;
                let x_origin = -self.time_scale * HORIZONTAL_DIVISIONS / 2.0 - self.time_offset;
                let y_inc = ch.scale / COUNTS_PER_DIV;
                let preamble =
                    format!("0,0,{POINTS},1,{x_inc:e},{x_origin:e},0,{y_inc:e},0,{Y_REFERENCE}");
                self.answer(&preamble);
            }
            "WAVEFORM:DATA?" => {
                let byte =
                    u8::try_from(i16::from(Y_REFERENCE as u8) + self.counts[self.wave_source])
                        .expect("test counts stay within a byte");
                let mut block = format!("#9{POINTS:09}").into_bytes();
                block.extend(std::iter::repeat_n(byte, POINTS));
                block.push(b'\n');
                self.reply = Some(block);
            }
            "MEASURE:ITEM?" => {
                let v = self
                    .measurements
                    .iter()
                    .find(|(k, _)| k.eq_ignore_ascii_case(raw_arg))
                    .map_or_else(|| "9.9E37".to_string(), |(_, v)| v.clone());
                self.answer(&v);
            }
            _ => self.refuse(),
        }
    }

    fn refuse(&mut self) {
        self.errors.push_back("-113,\"Undefined header\"".into());
    }
}

/// The simulated scope's end of the link; `State` stays reachable for checks.
struct Fake(Arc<Mutex<State>>);

impl Scpi for Fake {
    fn set_max_transfer(&mut self, bytes: u32) {
        self.0.lock().expect("test lock").max_transfer = Some(bytes);
    }

    fn write(&mut self, command: &str) -> Result<()> {
        let mut s = self.0.lock().expect("test lock");
        s.sent.push(command.to_string());
        for part in command.split(';') {
            s.handle(part);
        }
        Ok(())
    }

    fn read_message(&mut self) -> Result<Vec<u8>> {
        self.0
            .lock()
            .expect("test lock")
            .reply
            .take()
            .ok_or_else(|| anyhow::anyhow!("query timed out"))
    }
}

fn open(state: State) -> (RigolDs1000z, Arc<Mutex<State>>) {
    let shared = Arc::new(Mutex::new(state));
    let scope =
        RigolDs1000z::with(Box::new(Fake(shared.clone())), None).expect("simulated scope opens");
    (scope, shared)
}

fn sent(state: &Arc<Mutex<State>>) -> Vec<String> {
    state.lock().expect("test lock").sent.clone()
}

const A: ChannelId = ChannelId::Alphabetic('A');
const B: ChannelId = ChannelId::Alphabetic('B');

#[test]
fn ds1000z_models_are_recognised_by_name() {
    let id = |model: &str| Identity::parse(&format!("RIGOL TECHNOLOGIES,{model},SN1,00.04.04"));
    let ds1054z = id("DS1054Z").expect("DS1054Z");
    assert_eq!((ds1054z.channels(), ds1054z.bandwidth_hz()), (4, 50e6));
    let ds1104z = id("DS1104Z Plus").expect("DS1104Z Plus");
    assert_eq!((ds1104z.channels(), ds1104z.bandwidth_hz()), (4, 100e6));
    let mso = id("MSO1104Z").expect("MSO1104Z");
    assert_eq!(mso.channels(), 4);
    let ds1202 = id("DS1202Z-E").expect("DS1202Z-E");
    assert_eq!((ds1202.channels(), ds1202.bandwidth_hz()), (2, 200e6));
}

#[test]
fn other_scopes_are_not_taken_for_a_ds1000z() {
    let id = |idn: &str| Identity::parse(idn);
    assert!(id("RIGOL TECHNOLOGIES,MSO5204,MS5A1,00.01.03").is_err());
    assert!(id("RIGOL TECHNOLOGIES,DS2302A,DS2A1,00.03.02").is_err());
    assert!(id("RIGOL TECHNOLOGIES,DS1102E,DS1EB1,00.02.06").is_err());
    assert!(id("KEYSIGHT TECHNOLOGIES,DSOX1204G,CN1,02.12").is_err());
}

#[test]
fn opening_reads_the_scope_and_reports_its_capabilities() {
    let (scope, state) = open(State::ds1054z());
    let caps = scope.capabilities().expect("capabilities");
    assert_eq!(caps.family, DriverFamily::RigolDs1000z);
    assert_eq!((caps.model.as_str(), caps.analog_channels), ("DS1054Z", 4));
    assert_eq!(caps.bandwidth_hz, Some(50e6));
    assert_eq!(caps.voltage_ranges.len(), 13);
    assert!(scope.is_channel_enabled(A).expect("A"));
    assert!(!scope.is_channel_enabled(B).expect("B"));
    assert_eq!(scope.get_attenuation(A).expect("probe"), 10.0);
    assert_eq!(scope.get_capture_mode().expect("sweep"), CaptureMode::Auto);
    assert_eq!(scope.firmware(), "00.04.04.SP4");
    assert_eq!(
        sent(&state)[0],
        "*CLS",
        "the error queue is cleared before anything else"
    );
}

#[test]
fn a_setting_is_sent_and_read_back() {
    let (mut scope, state) = open(State::ds1054z());
    scope.set_volts_per_div(A, 0.5).expect("set");
    assert!(sent(&state).contains(&":CHANnel1:SCALe 0.5".to_string()));
    assert_eq!(scope.get_volts_per_div(A).expect("get"), 0.5);
    scope.set_time_per_div(2e-6).expect("timebase");
    assert_eq!(scope.get_time_per_div().expect("timebase"), 2e-6);
}

#[test]
fn trigger_slopes_map_to_the_scopes_tokens() {
    let (mut scope, state) = open(State::ds1054z());
    scope
        .set_trigger_slope(TriggerSlope::Either)
        .expect("either");
    assert!(sent(&state).contains(&":TRIGger:EDGE:SLOPe RFALl".to_string()));
    let before = sent(&state).len();
    assert!(scope.set_trigger_slope(TriggerSlope::Neither).is_err());
    assert_eq!(
        sent(&state).len(),
        before,
        "nothing is sent for a slope the scope lacks"
    );
}

#[test]
fn a_command_the_scope_refuses_is_an_error() {
    let (mut scope, _) = open(State::ds1054z());
    let err = scope
        .scpi(":NOT:A:COMMand 1")
        .expect("scpi is offered")
        .expect_err("refused");
    assert!(format!("{err:#}").contains("Undefined header"), "{err:#}");
}

#[test]
fn a_capture_is_the_screen_record_in_volts() {
    let (scope, _) = open(State::ds1054z());
    let frame = scope.get_triggered_data().expect("capture");
    assert_eq!(frame.channels.len(), 1, "only A is on");
    assert_eq!(frame.samples_per_channel as usize, POINTS);
    assert_ne!(
        frame.flags & FLAG_TRIGGERED,
        0,
        "status TD is a triggered capture"
    );
    // 25 counts at 1 V/div and 25 counts a division: 1 V.
    let volts = scope.get_data(A).expect("data");
    assert_eq!(volts.len(), POINTS);
    assert!((volts[0] - 1.0).abs() < 1e-6, "{}", volts[0]);
    // 1 ms/div over twelve divisions in 1200 points: 10 us a point.
    assert!(
        (frame.sample_interval_ns - 10_000.0).abs() < 1e-3,
        "{}",
        frame.sample_interval_ns
    );
    assert_eq!(
        frame.pre_trigger_samples as usize,
        POINTS / 2,
        "the trigger is mid-screen"
    );
}

#[test]
fn autoscale_is_recognized_in_any_spelling() {
    for command in [":AUToscale", ":AUT", "autoscale", " :aut "] {
        assert!(super::is_autoscale(command), "{command}");
    }
    for command in [":ACQuire:TYPE NORMal", ":AU", ""] {
        assert!(!super::is_autoscale(command), "{command}");
    }
}

#[test]
fn opening_caps_the_usb_transfer_size() {
    let (_scope, state) = open(State::ds1054z());
    assert_eq!(state.lock().expect("test lock").max_transfer, Some(500));
}

#[test]
fn a_capture_sends_one_command_a_message() {
    let (scope, state) = open(State::ds1054z());
    state.lock().expect("test lock").sent.clear();
    scope.get_triggered_data().expect("capture");
    let compound: Vec<String> = sent(&state)
        .into_iter()
        .filter(|c| c.contains(';'))
        .collect();
    assert!(compound.is_empty(), "{compound:?}");
}

#[test]
fn an_auto_capture_is_not_marked_triggered() {
    let mut state = State::ds1054z();
    state.status = "AUTO";
    let (scope, _) = open(state);
    let frame = scope.get_triggered_data().expect("capture");
    assert_eq!(frame.flags & FLAG_TRIGGERED, 0);
}

#[test]
fn a_record_read_on_a_trigger_is_marked_triggered() {
    let mut state = State::ds1054z();
    state.status = "TD";
    let (mut scope, state) = open(state);
    let frame = scope.read_record().expect("a record").expect("read");
    assert_ne!(frame.flags & FLAG_TRIGGERED, 0);
    state.lock().expect("test lock").status = "AUTO";
    let frame = scope.read_record().expect("a record").expect("read");
    assert_eq!(frame.flags & FLAG_TRIGGERED, 0);
}

#[test]
fn a_single_record_is_triggered_only_once_the_scope_stops() {
    let mut state = State::ds1054z();
    state.sweep = "SING".into();
    state.status = "WAIT";
    let (mut scope, state) = open(state);
    let frame = scope.read_record().expect("a record").expect("read");
    assert_eq!(frame.flags & FLAG_TRIGGERED, 0);
    state.lock().expect("test lock").status = "STOP";
    let frame = scope.read_record().expect("a record").expect("read");
    assert_ne!(frame.flags & FLAG_TRIGGERED, 0);
}

#[test]
fn settings_are_read_again_after_a_raw_autoscale() {
    let mut state = State::ds1054z();
    state.sweep = "NORM".into();
    let (mut scope, _) = open(state);
    scope
        .scpi(":AUToscale")
        .expect("handled")
        .expect("autoscale");
    assert!((scope.get_time_per_div().expect("timebase") - 2e-4).abs() < 1e-12);
    assert_eq!(scope.get_capture_mode().expect("sweep"), CaptureMode::Auto);
}

#[test]
fn the_live_view_is_paced() {
    let (scope, _) = open(State::ds1054z());
    assert!(scope.is_ready().expect("first"));
    scope.get_triggered_data().expect("capture");
    assert!(!scope.is_ready().expect("right after a capture"));
}

#[test]
fn a_single_capture_is_published_once() {
    let mut state = State::ds1054z();
    state.sweep = "SING".into();
    let (mut scope, state) = open(state);
    scope.start_triggered_capture(50.0).expect("arm");
    assert!(sent(&state).contains(&":SINGle".to_string()));
    assert!(
        !scope.is_ready().expect("waiting"),
        "the scope has not stopped yet"
    );
    state.lock().expect("test lock").status = "STOP";
    assert!(scope.is_ready().expect("stopped"));
    scope.get_triggered_data().expect("capture");
    assert!(
        !scope.is_ready().expect("published"),
        "the frozen screen is not published twice"
    );
    scope.start_triggered_capture(50.0).expect("re-arm");
    assert!(
        !scope.is_ready().expect("re-armed"),
        "re-arming waits for the next stop"
    );
    state.lock().expect("test lock").status = "STOP";
    assert!(scope.is_ready().expect("re-armed and stopped"));
}

#[test]
fn a_measurement_names_its_source_and_leaves_the_front_panel_alone() {
    let mut state = State::ds1054z();
    state.measurements = vec![
        ("VPP,CHANNEL1".into(), "3.300000e+00".into()),
        ("PDUTY,CHANNEL1".into(), "2.500000e-01".into()),
    ];
    let (mut scope, state) = open(state);
    assert_eq!(
        scope
            .measure_one(A, Measurement::Vpp)
            .expect("offered")
            .expect("vpp"),
        Some(3.3)
    );
    assert_eq!(
        scope.measure_duty_cycle(A).expect("duty"),
        25.0,
        "a ratio reported in percent"
    );
    assert_eq!(
        scope
            .measure_one(A, Measurement::Frequency)
            .expect("offered")
            .expect("query"),
        None,
        "9.9E37 means no value"
    );
    let sent = sent(&state);
    assert!(
        sent.contains(&":MEASure:ITEM? VPP,CHANnel1".to_string()),
        "{sent:?}"
    );
    assert!(
        !sent.iter().any(|c| c.starts_with(":MEASure:SOURce")),
        "{sent:?}"
    );
}

#[test]
fn a_hidden_channel_is_not_measured() {
    let (mut scope, _) = open(State::ds1054z());
    assert!(
        scope
            .measure_one(B, Measurement::Vpp)
            .expect("offered")
            .is_err()
    );
}

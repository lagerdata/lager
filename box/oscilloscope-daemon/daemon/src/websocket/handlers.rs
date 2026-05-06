// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use crate::oscilloscope::Oscilloscope;
use anyhow::Result;
use futures_util::stream::{SplitSink, SplitStream};
use futures_util::{SinkExt, StreamExt};
use protocol::{CaptureMode, Command, Response, WebSocketMessage};
use serde_json;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::sync::{Arc, Mutex};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::WebSocketStream;
use tungstenite::protocol::Message;

static CLEAR_BUFFER_FLAG: AtomicBool = AtomicBool::new(false);

pub async fn handle_commands(
    mut stream: SplitStream<WebSocketStream<TcpStream>>,
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
    tx_outgoing: mpsc::Sender<WebSocketMessage>,
) -> Result<()> {
    while let Some(message) = stream.next().await {
        // Convert tungstenite::Error to anyhow::Errorf

        let message = message?;
        match message {
            Message::Text(text) => match serde_json::from_str::<Command>(&text) {
                Ok(command) => match handle_command_internal(oscilloscope.clone(), command) {
                    Ok(response_message) => {
                        let websocket_message = match response_message {
                            Message::Text(text) => {
                                match serde_json::from_str::<Response>(&text) {
                                    Ok(response) => WebSocketMessage::Response(response),
                                    Err(_) => continue, // Skip invalid messages
                                }
                            }
                            _ => continue, // Skip non-text messages
                        };
                        tx_outgoing.send(websocket_message).await?;
                    }
                    Err(e) => {
                        eprintln!("Error handling command: {}", e);
                    }
                },
                Err(e) => {
                    eprintln!("Error parsing command: {}", e);
                }
            },
            Message::Binary(_data) => {}
            Message::Close(_) => break, // Exit loop on close
            Message::Ping(_ping) => {
                eprintln!("Received ping, but no pong handling implemented");
            }
            Message::Pong(_) => {}
            Message::Frame(_) => {}
        }
    }
    Ok(())
}

fn handle_command_internal(
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
    command: Command,
) -> Result<Message> {
    println!("[DEBUG handle_command_internal] Received command: {:?}", command);
    let response = match command {
        Command::EnableChannel { channel } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.enable_channel(channel) {
                Ok(()) => Response::ConfigureChannelEnabled,
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::DisableChannel { channel } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.disable_channel(channel) {
                Ok(()) => Response::ConfigureChannelDisabled,
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::IsChannelEnabled { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.is_channel_enabled(channel) {
                Ok(is_enabled) => Response::IsChannelEnabled {
                    channel,
                    is_enabled,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetChannelCount => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_channel_count() {
                Ok(count) => Response::GetChannelCount {
                    channel_count: count,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetSampleRate => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_sample_rate() {
                Ok(rate) => Response::GetSampleRate { sample_rate: rate },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetVoltsPerDiv {
            channel,
            volts_per_div,
        } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_volts_per_div(channel, volts_per_div) {
                Ok(()) => Response::ConfigureChannelVoltsPerDiv {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetVoltsPerDiv { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_volts_per_div(channel) {
                Ok(volts_per_div) => Response::GetVoltsPerDiv {
                    channel,
                    volts_per_div,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTimePerDiv { time_per_div } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_time_per_div(time_per_div) {
                Ok(()) => Response::ConfigureTimePerDiv {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTimePerDiv => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_time_per_div() {
                Ok(time_per_div) => Response::GetTimePerDiv { time_per_div },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetCoupling { channel, coupling } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_coupling(channel, coupling) {
                Ok(()) => Response::ConfigureChannelCoupling {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetCoupling { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_coupling(channel) {
                Ok(coupling) => Response::GetCoupling { channel, coupling },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTriggerLevel { trigger_level } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_trigger_level(trigger_level) {
                Ok(()) => Response::ConfigureTriggerLevel {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTriggerLevel => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_trigger_level() {
                Ok(trigger_level) => Response::GetTriggerLevel { trigger_level },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTriggerSource { trigger_source } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_trigger_source(trigger_source) {
                Ok(()) => Response::ConfigureTriggerSource {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTriggerSource => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_trigger_source() {
                Ok(trigger_source) => Response::GetTriggerSource { trigger_source },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetTriggerSlope { trigger_slope } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_trigger_slope(trigger_slope) {
                Ok(()) => Response::ConfigureTriggerSlope {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetTriggerSlope => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_trigger_slope() {
                Ok(trigger_slope) => Response::GetTriggerSlope { trigger_slope },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetCaptureMode { capture_mode } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_capture_mode(capture_mode) {
                Ok(()) => Response::ConfigureCaptureMode {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetCaptureMode => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_capture_mode() {
                Ok(capture_mode) => Response::GetCaptureMode { capture_mode },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }

        Command::StartAcquisition {
            trigger_position_percent,
        } => {
            println!("[DEBUG handle_command] Received StartAcquisition with trigger_position_percent={}", trigger_position_percent);
            let mut scope = oscilloscope.lock().unwrap();
            println!("[DEBUG handle_command] Got oscilloscope lock, calling start_triggered_capture");
            match scope.start_triggered_capture(trigger_position_percent) {
                Ok(()) => {
                    println!("[DEBUG handle_command] start_triggered_capture succeeded");
                    Response::StartAcquisition
                }
                Err(e) => {
                    println!("[DEBUG handle_command] start_triggered_capture FAILED: {}", e);
                    Response::Error {
                        message: e.to_string(),
                    }
                }
            }
        }
        Command::StopAcquisition => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.stop_triggered_capture() {
                Ok(()) => {
                    CLEAR_BUFFER_FLAG.store(true, Ordering::Relaxed);
                    println!("Stopped triggering");
                    Response::StopAcquisition
                }
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::IsReady => {
            let scope = oscilloscope.lock().unwrap();
            match scope.is_ready() {
                Ok(is_ready) => Response::IsReady { is_ready },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::SetAttenuation {
            channel,
            attenuation,
        } => {
            let mut scope = oscilloscope.lock().unwrap();
            match scope.set_attenuation(channel, attenuation) {
                Ok(()) => Response::ConfigureChannelAttenuation {},
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }
        Command::GetAttenuation { channel } => {
            let scope = oscilloscope.lock().unwrap();
            match scope.get_attenuation(channel) {
                Ok(attenuation) => Response::GetAttenuation {
                    channel,
                    attenuation,
                },
                Err(e) => Response::Error {
                    message: e.to_string(),
                },
            }
        }

        _ => Response::Error {
            message: "Unsupported command".to_string(),
        },
    };

    Ok(Message::Text(serde_json::to_string(&response)?))
}

pub async fn handle_scope_streaming(
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
    tx: mpsc::Sender<WebSocketMessage>,
) -> Result<()> {
    println!("[DEBUG handle_scope_streaming] Starting streaming loop");
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
                println!("[DEBUG handle_scope_streaming] Scope is ready, getting triggered data");
                let result = scope.get_triggered_data().ok();
                if let Some(ref data) = result {
                    println!("[DEBUG handle_scope_streaming] Got {} samples, trigger_pos={}, sample_interval={}ns",
                        data.samples.len(), data.trigger_position, data.sample_interval_ns);
                }
                if matches!(capture_mode, CaptureMode::Single) {
                    println!("[DEBUG handle_scope_streaming] Single mode - stopping capture");
                    _ = scope.stop_triggered_capture();
                    None
                } else {
                    println!("[DEBUG handle_scope_streaming] Auto/Normal mode - restarting capture");
                    _ = scope.start_triggered_capture(trigger_position).ok();
                    result
                }
            } else {
                None
            }
        }; // MutexGuard dropped here

        if let Some(ref data) = data {
            println!("[DEBUG handle_scope_streaming] Sending TriggeredData with {} samples to WebSocket",
                data.samples.len());
            let websocket_message = WebSocketMessage::TriggeredData(data.clone());
            tx.send(websocket_message).await?;
        }
        tokio::time::sleep(tokio::time::Duration::from_millis(10)).await;
    }
}

pub async fn handle_outgoing_messages(
    mut sink: SplitSink<WebSocketStream<TcpStream>, Message>,
    mut rx: mpsc::Receiver<WebSocketMessage>,
) -> Result<()> {
    println!("[DEBUG handle_outgoing_messages] Starting outgoing message handler");
    loop {
        if let Some(websocket_message) = rx.recv().await {
            // Convert WebSocketMessage to Message
            let message = match &websocket_message {
                WebSocketMessage::Response(response) => {
                    println!("[DEBUG handle_outgoing_messages] Sending Response");
                    Message::Text(serde_json::to_string(&response)?)
                }
                WebSocketMessage::TriggeredData(data) => {
                    println!("[DEBUG handle_outgoing_messages] Sending TriggeredData with {} samples",
                        data.samples.len());
                    Message::Text(serde_json::to_string(&websocket_message)?)
                }
                WebSocketMessage::Command(_) => {
                    // Commands shouldn't be sent back to client, skip
                    continue;
                }
            };
            sink.send(message).await?;
            if let Err(e) = sink.flush().await {
                eprintln!("Error flushing message to client: {}", e);
            }
            if CLEAR_BUFFER_FLAG.load(Ordering::Relaxed) {
                CLEAR_BUFFER_FLAG.store(false, Ordering::Relaxed);
                while rx.try_recv().is_ok() {
                    // Discard the messages
                }
                println!("Cleared buffer");
            }
        }
    }
}

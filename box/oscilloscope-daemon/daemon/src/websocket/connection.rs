// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use super::handlers::{handle_commands, handle_outgoing_messages, handle_scope_streaming};
use crate::oscilloscope::Oscilloscope;
use anyhow::Result;
use futures_util::StreamExt;
use futures_util::stream::{SplitSink, SplitStream};
use protocol::WebSocketMessage;
use std::sync::{Arc, Mutex};
use tokio::net::TcpStream;
use tokio::sync::mpsc;
use tokio_tungstenite::WebSocketStream;
use tungstenite::protocol::Message;

pub struct WebSocketConnection {
    sink: SplitSink<WebSocketStream<TcpStream>, Message>,
    stream: SplitStream<WebSocketStream<TcpStream>>,
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
    tx_outgoing: mpsc::Sender<WebSocketMessage>,
    rx_outgoing: mpsc::Receiver<WebSocketMessage>,
}

impl WebSocketConnection {
    pub fn new(
        stream: WebSocketStream<TcpStream>,
        oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
    ) -> Self {
        let (sink, stream) = stream.split();
        let (tx_outgoing, rx_outgoing) = mpsc::channel(50_000);
        Self {
            sink,
            stream,
            oscilloscope,
            tx_outgoing,
            rx_outgoing,
        }
    }

    pub async fn handle(self) -> Result<()> {
        tokio::select! {
            _ = handle_commands(self.stream, self.oscilloscope.clone(), self.tx_outgoing.clone()) => {},
            _ = handle_scope_streaming(self.oscilloscope, self.tx_outgoing.clone()) => {}
            _ = handle_outgoing_messages(self.sink, self.rx_outgoing) => {}
        }

        Ok(())
    }
}

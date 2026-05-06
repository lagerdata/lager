// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use anyhow::Result;
use crossbeam::queue::SegQueue;
use daemon::oscilloscope::create_oscilloscope;
use daemon::oscilloscope::Oscilloscope;
use daemon::webtransport::streaming::handle_scope_streaming as handle_webtransport_streaming;
use daemon::webtransport::{
    create_wtransport_server, handle_browser_connections, handle_commands_connections,
    handle_database_connections,
};
use daemon::websocket::handlers::{handle_commands, handle_outgoing_messages, handle_scope_streaming as handle_websocket_streaming};
use futures_util::StreamExt;
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio_tungstenite::accept_async;

const MAX_DATABASE_CHANNEL_SIZE: usize = 50_000;
const WEBSOCKET_COMMAND_PORT: u16 = 8085;

/// Handle WebSocket command connections (for CLI/Python clients)
async fn handle_websocket_commands(
    listener: TcpListener,
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
) -> Result<()> {
    println!("WebSocket commands server listening on port {}", WEBSOCKET_COMMAND_PORT);
    loop {
        let (stream, addr) = listener.accept().await?;
        println!("WebSocket connection from: {}", addr);

        let oscilloscope = oscilloscope.clone();
        tokio::spawn(async move {
            match accept_async(stream).await {
                Ok(ws_stream) => {
                    let (sink, stream) = ws_stream.split();
                    let (tx_outgoing, rx_outgoing) = mpsc::channel(100);

                    // Clone tx for streaming task
                    let tx_streaming = tx_outgoing.clone();
                    let oscilloscope_streaming = oscilloscope.clone();

                    let handle_commands_task = handle_commands(stream, oscilloscope, tx_outgoing);
                    let handle_outgoing_task = handle_outgoing_messages(sink, rx_outgoing);
                    let handle_streaming_task = handle_websocket_streaming(oscilloscope_streaming, tx_streaming);

                    tokio::select! {
                        result = handle_commands_task => {
                            if let Err(e) = result {
                                eprintln!("WebSocket commands error: {}", e);
                            }
                        }
                        result = handle_outgoing_task => {
                            if let Err(e) = result {
                                eprintln!("WebSocket outgoing error: {}", e);
                            }
                        }
                        result = handle_streaming_task => {
                            if let Err(e) = result {
                                eprintln!("WebSocket streaming error: {}", e);
                            }
                        }
                    }
                    println!("WebSocket connection closed: {}", addr);
                }
                Err(e) => {
                    eprintln!("WebSocket handshake error: {}", e);
                }
            }
        });
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    println!("Starting oscilloscope daemon");

    // Create WebSocket listener for CLI commands
    let ws_listener = TcpListener::bind(format!("0.0.0.0:{}", WEBSOCKET_COMMAND_PORT)).await?;
    println!("WebSocket commands listener created on port {}", WEBSOCKET_COMMAND_PORT);

    // Create WebTransport endpoints
    println!("Creating WebTransport endpoints...");
    let commands_endpoint = create_wtransport_server(8082).await?;
    println!("Commands endpoint created successfully");
    let browser_endpoint = create_wtransport_server(8083).await?;
    println!("Browser endpoint created successfully");
    let database_endpoint = create_wtransport_server(8084).await?;
    println!("Database endpoint created successfully");

    println!("Endpoints:");
    println!("  WebSocket Commands (CLI): 0.0.0.0:8085");
    println!("  WebTransport Commands:    0.0.0.0:8082");
    println!("  WebTransport Browser:     0.0.0.0:8083");
    println!("  WebTransport Database:    0.0.0.0:8084");
    println!("Starting servers...");

    let oscilloscope = Arc::new(Mutex::new(create_oscilloscope()?));
    println!("Oscilloscope created");

    // Create channels for data distribution
    let (tx_database_data, rx_database_data) = mpsc::channel(MAX_DATABASE_CHANNEL_SIZE); // Large buffer for database
    let browser_queue = Arc::new(SegQueue::new()); // Small buffer for browser (~10 items)

    // Start WebTransport oscilloscope data streaming (for database and browser clients)
    let scope_ref = Arc::clone(&oscilloscope);
    let browser_queue_for_streaming = Arc::clone(&browser_queue);
    tokio::spawn(async move {
        if let Err(e) =
            handle_webtransport_streaming(scope_ref, tx_database_data, browser_queue_for_streaming).await
        {
            eprintln!("WebTransport scope streaming error: {}", e);
        }
    });

    // Start all servers (WebSocket + WebTransport)
    tokio::select! {
        ws_result = handle_websocket_commands(ws_listener, oscilloscope.clone()) => {
            if let Err(e) = ws_result {
                eprintln!("WebSocket commands listener error: {}", e);
            } else {
                println!("WebSocket commands listener stopped normally");
            }
        }
        commands_result = handle_commands_connections(commands_endpoint, oscilloscope.clone()) => {
            if let Err(e) = commands_result {
                eprintln!("WebTransport commands listener error: {}", e);
            } else {
                println!("WebTransport commands listener stopped normally");
            }
        }
        browser_result = handle_browser_connections(browser_endpoint, browser_queue) => {
            if let Err(e) = browser_result {
                eprintln!("WebTransport browser listener error: {}", e);
            } else {
                println!("WebTransport browser listener stopped normally");
            }
        }
        database_result = handle_database_connections(database_endpoint, rx_database_data) => {
            if let Err(e) = database_result {
                eprintln!("WebTransport database listener error: {}", e);
            } else {
                println!("WebTransport database listener stopped normally");
            }
        }
    }
    Ok(())
}

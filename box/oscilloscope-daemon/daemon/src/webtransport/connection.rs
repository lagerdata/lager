// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use super::handlers;
use crate::oscilloscope::Oscilloscope;
use crossbeam::queue::SegQueue;
use protocol::{Command, TriggeredCapture};
use serde_json;
use std::sync::{Arc, Mutex};
use tokio::io::AsyncReadExt;
use tokio::sync::mpsc;
use wtransport::endpoint::SessionRequest;

/// Handle WebTransport commands connection (bidirectional)
pub async fn handle_commands_connection(
    incoming_request: SessionRequest,
    oscilloscope: Arc<Mutex<Box<dyn Oscilloscope>>>,
) {
    let connection = match incoming_request.accept().await {
        Ok(conn) => conn,
        Err(e) => {
            eprintln!("Failed to accept WebTransport commands connection: {}", e);
            return;
        }
    };

    println!("WebTransport commands connection established");

    loop {
        match connection.accept_bi().await {
            Ok(stream) => {
                let (mut send_stream, mut recv_stream) = stream;

                // Read command from client
                let mut buffer = Vec::new();
                if let Ok(_) = recv_stream.read_to_end(&mut buffer).await {
                    let command_text = String::from_utf8_lossy(&buffer);
                    println!("Received command: {}", command_text);

                    // Parse and handle command
                    match serde_json::from_str::<Command>(&command_text) {
                        Ok(command) => {
                            match handlers::handle_command_internal(oscilloscope.clone(), command) {
                                Ok(response_message) => {
                                    // Extract the response from the tungstenite message
                                    match response_message {
                                        tungstenite::protocol::Message::Text(response_text) => {
                                            // Send response back to client
                                            if let Err(e) = send_stream
                                                .write_all(response_text.as_bytes())
                                                .await
                                            {
                                                eprintln!("Failed to send response: {}", e);
                                            }
                                        }
                                        _ => {
                                            eprintln!("Unexpected response message type");
                                        }
                                    }
                                }
                                Err(e) => {
                                    eprintln!("Error handling command: {}", e);
                                }
                            }
                        }
                        Err(e) => {
                            eprintln!("Error parsing command: {}", e);
                        }
                    }
                }
            }
            Err(e) => {
                eprintln!("Error accepting bidirectional stream: {}", e);
                break;
            }
        }
    }
}

/// Handle WebTransport browser connection (unreliable, small buffer)
pub async fn handle_browser_connection(
    incoming_request: SessionRequest,
    browser_queue: Arc<SegQueue<TriggeredCapture>>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let connection = match incoming_request.accept().await {
        Ok(conn) => conn,
        Err(e) => {
            eprintln!("Failed to accept WebTransport browser connection: {}", e);
            return Err(e.into());
        }
    };

    println!("WebTransport browser connection established");

    // Send data to client (unreliable - drop oldest when full)
    loop {
        if let Some(data) = browser_queue.pop() {
            // Create a new unidirectional stream for each data packet
            let send_stream = match connection.open_uni().await {
                Ok(stream) => stream,
                Err(e) => {
                    eprintln!("Failed to create unidirectional stream: {}", e);
                    break;
                }
            };
            let mut send_stream = send_stream.await?;

            let json_data = serde_json::to_string(&data).unwrap();
            if let Err(e) = send_stream.write_all(json_data.as_bytes()).await {
                eprintln!("Failed to send browser data: {}", e);
                break;
            }
            // Properly close the stream to prevent accumulation
            if let Err(e) = send_stream.finish().await {
                eprintln!("Failed to finish browser data stream: {}", e);
            }
        } else {
            // No data available, wait a bit
            tokio::time::sleep(tokio::time::Duration::from_millis(10)).await;
        }
    }

    Ok(())
}

/// Handle WebTransport database connection (reliable, large buffer)
pub async fn handle_database_connection(
    incoming_request: SessionRequest,
    mut rx_data: mpsc::Receiver<TriggeredCapture>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let connection = match incoming_request.accept().await {
        Ok(conn) => conn,
        Err(e) => {
            eprintln!("Failed to accept WebTransport database connection: {}", e);
            return Err(e.into());
        }
    };

    println!("WebTransport database connection established");

    // Send data to client (reliable - don't lose any data)
    while let Some(data) = rx_data.recv().await {
        // Create a new unidirectional stream for each data packet
        let send_stream = match connection.open_uni().await {
            Ok(stream) => stream,
            Err(e) => {
                eprintln!("Failed to create unidirectional stream: {}", e);
                break;
            }
        };
        let mut send_stream = send_stream.await?;

        let json_data = serde_json::to_string(&data).unwrap();
        if let Err(e) = send_stream.write_all(json_data.as_bytes()).await {
            eprintln!("Failed to send database data: {}", e);
            break;
        }
    }

    Ok(())
}

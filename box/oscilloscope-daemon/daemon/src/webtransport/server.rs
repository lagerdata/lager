// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use super::connection;
use crate::oscilloscope::Oscilloscope;
use anyhow::Result;
use crossbeam::queue::SegQueue;
use protocol::TriggeredCapture;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio::sync::mpsc;
use wtransport::endpoint::endpoint_side::Server;
use wtransport::{Endpoint, Identity, ServerConfig};

/// Create a WebTransport server endpoint
pub async fn create_wtransport_server(port: u16) -> Result<Endpoint<Server>> {
    // Load the certificate and private key from files
    let identity = Identity::load_pemfiles("certs/server.crt", "certs/server.key").await?;

    let config = ServerConfig::builder()
        .with_bind_address(SocketAddr::from(([0, 0, 0, 0], port)))
        .with_identity(identity)
        .build();

    let endpoint = Endpoint::server(config)?;
    Ok(endpoint)
}

/// Handle WebTransport connections for commands (bidirectional)
pub async fn handle_commands_connections(
    endpoint: Endpoint<Server>,
    oscilloscope: Arc<std::sync::Mutex<Box<dyn Oscilloscope>>>,
) -> Result<()> {
    println!(
        "WebTransport commands server listening on port {}",
        endpoint.local_addr()?.port()
    );

    loop {
        let incoming_session = endpoint.accept().await;
        let incoming_request = incoming_session.await?;
        println!(
            "New WebTransport commands connection from {}",
            incoming_request.remote_address()
        );

        let scope_ref = Arc::clone(&oscilloscope);
        tokio::spawn(connection::handle_commands_connection(
            incoming_request,
            scope_ref,
        ));
    }
}

/// Handle WebTransport connections for browser data (unreliable, small buffer)
pub async fn handle_browser_connections(
    endpoint: Endpoint<Server>,
    browser_queue: Arc<SegQueue<TriggeredCapture>>,
) -> Result<()> {
    println!(
        "WebTransport browser server listening on port {}",
        endpoint.local_addr()?.port()
    );

    loop {
        let incoming_session = endpoint.accept().await;
        let incoming_request = incoming_session.await?;
        println!(
            "New WebTransport browser connection from {}",
            incoming_request.remote_address()
        );

        let queue_ref = Arc::clone(&browser_queue);
        tokio::spawn(async move {
            if let Err(e) = connection::handle_browser_connection(incoming_request, queue_ref).await
            {
                eprintln!("Browser connection error: {}", e);
            }
        });
    }
}

/// Handle WebTransport connections for database data (reliable, large buffer)
pub async fn handle_database_connections(
    endpoint: Endpoint<Server>,
    rx_data: mpsc::Receiver<TriggeredCapture>,
) -> Result<()> {
    println!(
        "WebTransport database server listening on port {}",
        endpoint.local_addr()?.port()
    );

    // Wait for the first connection
    let incoming_session = endpoint.accept().await;
    let incoming_request = incoming_session.await?;
    println!(
        "New WebTransport database connection from {}",
        incoming_request.remote_address()
    );

    // Handle the single database connection
    if let Err(e) = connection::handle_database_connection(incoming_request, rx_data).await {
        eprintln!("Database connection error: {}", e);
    }

    Ok(())
}

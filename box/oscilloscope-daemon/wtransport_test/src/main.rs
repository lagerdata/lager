// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

use anyhow::Result;
use std::net::SocketAddr;
use wtransport::{Endpoint, Identity, ServerConfig};

#[tokio::main]
async fn main() -> Result<()> {
    println!("Starting simple wtransport test server");

    // Load the certificate and private key from files
    let identity = Identity::load_pemfiles("../certs/server.crt", "../certs/server.key").await?;

    let config = ServerConfig::builder()
        .with_bind_address(SocketAddr::from(([0, 0, 0, 0], 8081)))
        .with_identity(identity)
        .build();

    let endpoint = Endpoint::server(config)?;
    println!("WebTransport server listening on port 8081");
    println!("You can test this with: https://localhost:8081");

    // Accept incoming connections
    loop {
        let incoming_session = endpoint.accept().await;
        let incoming_request = incoming_session.await?;
        println!(
            "New WebTransport connection from {}",
            incoming_request.remote_address()
        );

        tokio::spawn(handle_connection(incoming_request));
    }
}

async fn handle_connection(incoming_request: wtransport::endpoint::SessionRequest) {
    // Accept the WebTransport connection
    let connection = match incoming_request.accept().await {
        Ok(conn) => conn,
        Err(e) => {
            eprintln!("Failed to accept WebTransport connection: {}", e);
            return;
        }
    };

    println!("WebTransport connection established");

    // Handle bidirectional streams
    loop {
        match connection.accept_bi().await {
            Ok(stream) => {
                let (mut send_stream, mut recv_stream) = stream;

                // Read data from client
                let mut buffer = Vec::new();
                if let Ok(_) =
                    tokio::io::AsyncReadExt::read_to_end(&mut recv_stream, &mut buffer).await
                {
                    let message = String::from_utf8_lossy(&buffer);
                    println!("Received from client: {}", message);

                    // Send response back
                    let response = format!("Echo: {}", message);
                    if let Err(e) =
                        tokio::io::AsyncWriteExt::write_all(&mut send_stream, response.as_bytes())
                            .await
                    {
                        eprintln!("Failed to send response: {}", e);
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

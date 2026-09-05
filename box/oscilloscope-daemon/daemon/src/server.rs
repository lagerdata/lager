// Copyright 2024-2026 Lager Data
// SPDX-License-Identifier: Apache-2.0

//! The daemon's only listener.
//!
//! One WebSocket endpoint carries both planes: JSON text frames for commands
//! and responses, binary LSCP frames for captures. This replaces four
//! listeners (a JSON WebSocket plus three WebTransport/QUIC ports) whose
//! TLS material was a ten-year self-signed certificate -- invalid for
//! `serverCertificateHashes`, which caps pinned certificates at 14 days, so
//! the browser path could never have completed a handshake.
//!
//! Accepts connections on TCP and, when configured, on a Unix domain socket.
//! The UDS is how `box_http_server` relays frames on port 9000 without
//! copying them through a second encode: it forwards opaque bytes.

use std::path::PathBuf;

use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use protocol::{Command, Response, WebSocketMessage};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, UnixListener};
use tokio_tungstenite::accept_async;
use tungstenite::Message;

use crate::handlers;
use crate::scope_thread::ScopeHandle;

pub struct ServerConfig {
    pub tcp_port: Option<u16>,
    pub tcp_bind: String,
    pub unix_socket: Option<PathBuf>,
}

impl ServerConfig {
    /// Read the listener configuration from the environment.
    ///
    /// Both listeners are optional but at least one must be present, since a
    /// daemon nobody can reach is the failure mode this rewrite exists to
    /// fix. Defaults keep the historical TCP port working.
    pub fn from_env() -> Self {
        let tcp_port = match std::env::var("LAGER_SCOPE_DATA_PORT") {
            Ok(value) if value.eq_ignore_ascii_case("off") => None,
            Ok(value) => value.parse().ok(),
            Err(_) => Some(8085),
        };
        let unix_socket = std::env::var("LAGER_SCOPE_SOCKET")
            .ok()
            .map(PathBuf::from)
            .or_else(|| Some(PathBuf::from("/tmp/lager-scope.sock")));

        // Loopback by default. The browser and CLI reach captures through the
        // box HTTP server's relay on :9000, which authenticates; a port bound
        // to 0.0.0.0 would let anything that can route to the container drive
        // the hardware with no token at all.
        let tcp_bind = std::env::var("LAGER_SCOPE_DATA_BIND")
            .unwrap_or_else(|_| "127.0.0.1".to_string());

        Self {
            tcp_port,
            tcp_bind,
            unix_socket,
        }
    }
}

pub async fn serve(config: ServerConfig, scope: ScopeHandle) -> Result<()> {
    let mut tasks = Vec::new();

    if let Some(port) = config.tcp_port {
        let listener = TcpListener::bind((config.tcp_bind.as_str(), port))
            .await
            .with_context(|| format!("binding scope data port {}:{port}", config.tcp_bind))?;
        tracing::info!(bind = %config.tcp_bind, port, "listening on TCP");
        let scope = scope.clone();
        tasks.push(tokio::spawn(async move {
            loop {
                match listener.accept().await {
                    Ok((stream, peer)) => {
                        // Captures are latency-sensitive and already packed,
                        // so Nagle would only add delay.
                        if let Err(e) = stream.set_nodelay(true) {
                            tracing::warn!(error = %e, "could not disable Nagle");
                        }
                        let scope = scope.clone();
                        tokio::spawn(async move {
                            tracing::info!(%peer, "client connected");
                            if let Err(e) = serve_connection(stream, scope).await {
                                tracing::debug!(%peer, error = %e, "connection ended");
                            }
                        });
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "TCP accept failed");
                    }
                }
            }
        }));
    }

    if let Some(path) = config.unix_socket {
        // A stale socket file from an unclean shutdown would make bind fail.
        if path.exists() {
            let _ = std::fs::remove_file(&path);
        }
        let listener = UnixListener::bind(&path)
            .with_context(|| format!("binding scope socket {}", path.display()))?;
        // The relay in box_http_server runs as a different user than the
        // daemon in some box images, so the socket has to be group writable.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o660));
        }
        tracing::info!(path = %path.display(), "listening on Unix socket");
        let scope = scope.clone();
        tasks.push(tokio::spawn(async move {
            loop {
                match listener.accept().await {
                    Ok((stream, _)) => {
                        let scope = scope.clone();
                        tokio::spawn(async move {
                            tracing::debug!("relay connected");
                            if let Err(e) = serve_connection(stream, scope).await {
                                tracing::debug!(error = %e, "relay connection ended");
                            }
                        });
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "Unix accept failed");
                    }
                }
            }
        }));
    }

    if tasks.is_empty() {
        anyhow::bail!(
            "no listeners configured: set LAGER_SCOPE_DATA_PORT or LAGER_SCOPE_SOCKET"
        );
    }

    // An accept loop only ends by panicking or being cancelled, so waiting
    // on them in order would pin us to the first one and let a second
    // listener die unnoticed -- the daemon would keep answering on TCP while
    // the Unix socket silently stopped accepting. Waiting for whichever
    // finishes first makes any listener's death a fatal, visible error.
    let (result, _index, remaining) = futures_util::future::select_all(tasks).await;
    for task in remaining {
        task.abort();
    }
    result.context("a scope listener stopped unexpectedly")?;
    anyhow::bail!("a scope listener returned when it should have run forever")
}

/// Drive one client: read commands, write responses, forward captures.
async fn serve_connection<S>(stream: S, scope: ScopeHandle) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let ws = accept_async(stream).await?;
    let (mut sink, mut source) = ws.split();

    // No subscription until the client asks for one. A control-only client
    // (CLI, Python driver) would otherwise be sent the whole capture stream,
    // which wastes bandwidth and puts binary frames in front of the reply it
    // is waiting for.
    let mut captures: Option<
        tokio::sync::broadcast::Receiver<std::sync::Arc<protocol::CaptureFrame>>,
    > = None;

    loop {
        tokio::select! {
            incoming = source.next() => {
                let Some(message) = incoming else { break };
                match message? {
                    Message::Text(text) => {
                        // Subscription is connection state, not hardware
                        // state, so it is handled here rather than on the
                        // hardware thread.
                        match serde_json::from_str::<Command>(&text) {
                            Ok(Command::Subscribe) => {
                                if captures.is_none() {
                                    captures = Some(scope.subscribe());
                                }
                                sink.send(Message::Text(
                                    encode(Response::Subscribed).into())).await?;
                                continue;
                            }
                            Ok(Command::Unsubscribe) => {
                                captures = None;
                                sink.send(Message::Text(
                                    encode(Response::Unsubscribed).into())).await?;
                                continue;
                            }
                            _ => {}
                        }

                        let (reply, frame) = dispatch_text(&text, &scope).await;
                        sink.send(Message::Text(reply.into())).await?;
                        if let Some(frame) = frame {
                            sink.send(Message::Binary(frame.encode().into())).await?;
                        }
                    }
                    Message::Binary(_) => {
                        // Nothing sends us binary. Say so rather than
                        // dropping it silently, which is what the old
                        // handler did for every unrecognised frame.
                        let reply = encode(Response::Error {
                            message: "binary frames are not accepted on the command plane"
                                .into(),
                        });
                        sink.send(Message::Text(reply.into())).await?;
                    }
                    Message::Ping(payload) => {
                        // The old handler logged pings and never ponged, so
                        // idle clients were dropped by intermediaries.
                        sink.send(Message::Pong(payload)).await?;
                    }
                    Message::Close(_) => break,
                    Message::Pong(_) | Message::Frame(_) => {}
                }
            }

            // Parks forever while unsubscribed, so this arm simply never
            // fires rather than needing the loop restructured.
            capture = async {
                match captures.as_mut() {
                    Some(receiver) => receiver.recv().await,
                    None => std::future::pending().await,
                }
            } => {
                match capture {
                    Ok(frame) => {
                        sink.send(Message::Binary(frame.encode().into())).await?;
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(missed)) => {
                        // This client could not keep up. Dropping stale
                        // captures is right for a live display; tell the
                        // client so it can report the gap rather than
                        // silently showing an incomplete record.
                        tracing::debug!(missed, "client lagged, dropped captures");
                        let notice = encode(Response::Error {
                            message: format!(
                                "dropped {missed} captures: client is not keeping up"
                            ),
                        });
                        sink.send(Message::Text(notice.into())).await?;
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }
        }
    }

    Ok(())
}

/// Parse and run one command, always producing a reply.
async fn dispatch_text(
    text: &str,
    scope: &ScopeHandle,
) -> (String, Option<std::sync::Arc<protocol::CaptureFrame>>) {
    match serde_json::from_str::<Command>(text) {
        Ok(command) => {
            let outcome = handlers::handle(command, scope).await;
            (encode(outcome.response), outcome.frame)
        }
        Err(e) => {
            // Previously this was logged and the client was left waiting
            // forever for a reply that never came.
            (
                encode(Response::Error {
                    message: format!("could not parse command: {e}"),
                }),
                None,
            )
        }
    }
}

fn encode(response: Response) -> String {
    // The wrapper keeps responses distinguishable from commands on a socket
    // that carries both directions.
    match serde_json::to_string(&WebSocketMessage::Response(response)) {
        Ok(text) => text,
        Err(e) => format!(
            r#"{{"Response":{{"response":"Error","message":"failed to encode response: {e}"}}}}"#
        ),
    }
}

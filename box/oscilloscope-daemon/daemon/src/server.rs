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
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use protocol::{Command, Response, ScopeState, WebSocketMessage};
use tokio::io::{AsyncRead, AsyncWrite};
use tokio::net::{TcpListener, UnixListener};
use tokio_tungstenite::accept_async;
use tungstenite::Message;

use crate::handlers;
use crate::scope_thread::{Published, ScopeHandle};

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

/// Pause after a failed accept before the next.
const ACCEPT_RETRY: Duration = Duration::from_millis(100);

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
                            if let Err(e) = serve_connection(stream, scope, COMMAND_TIMEOUT).await {
                                tracing::debug!(%peer, error = %e, "connection ended");
                            }
                        });
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "TCP accept failed");
                        // Out of descriptors, the next accept fails the same
                        // way at once: without a pause this loop spins a core
                        // and floods the log until a connection closes.
                        tokio::time::sleep(ACCEPT_RETRY).await;
                    }
                }
            }
        }));
    }

    let socket_path = config.unix_socket.clone();
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
                            if let Err(e) = serve_connection(stream, scope, COMMAND_TIMEOUT).await {
                                tracing::debug!(error = %e, "relay connection ended");
                            }
                        });
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "Unix accept failed");
                        tokio::time::sleep(ACCEPT_RETRY).await;
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
    // Spawned tasks outlive the future that spawned them, so a daemon that
    // stopped serving to shut down would go on accepting connections to a
    // scope that is closing.
    let _listeners = StopOnDrop {
        tasks: tasks.iter().map(|task| task.abort_handle()).collect(),
        socket: socket_path,
    };

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

/// Stops the listeners, and takes the socket file away, when serving stops.
struct StopOnDrop {
    tasks: Vec<tokio::task::AbortHandle>,
    socket: Option<PathBuf>,
}

impl Drop for StopOnDrop {
    fn drop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
        if let Some(path) = &self.socket {
            let _ = std::fs::remove_file(path);
        }
    }
}

/// What a subscribed connection has asked for, and where it has got to.
struct Subscription {
    captures: tokio::sync::broadcast::Receiver<Published>,
    /// Frames this connection may still be sent. None sends every capture
    /// as it is taken, which is what a client that predates flow control
    /// asked for by subscribing.
    credits: Option<u32>,
    /// The newest capture not yet sent. Each arrival replaces the last, so
    /// what goes out when credit returns is the scope now, not a queue of
    /// the scope as it was.
    pending: Option<Published>,
    /// Interval between frames, from `max_fps`.
    min_interval: Option<Duration>,
    /// When the next frame is due under `max_fps`.
    ///
    /// Kept as a schedule rather than derived from when the last frame went:
    /// tokio's timer rounds a sleep up to the millisecond, so "one interval
    /// after the last send" loses a millisecond or so a frame, and a stream
    /// asked for at 60 ran at 56.
    next_due: Option<Instant>,
}

impl Subscription {
    fn new(captures: tokio::sync::broadcast::Receiver<Published>, credits: Option<u32>, max_fps: Option<f64>) -> Self {
        // Clamped, because `from_secs_f64` panics on what a tiny rate makes
        // of an interval, and that panic would take the connection with it.
        let min_interval = max_fps
            .filter(|fps| fps.is_finite() && *fps > 0.0)
            .map(|fps| Duration::from_secs_f64(1.0 / fps.clamp(MIN_FPS, MAX_FPS)));
        Subscription {
            captures,
            credits,
            pending: None,
            min_interval,
            next_due: None,
        }
    }

    fn has_credit(&self) -> bool {
        self.credits.is_none_or(|credits| credits > 0)
    }

    /// When the frame rate cap next allows a frame, if it is holding one.
    fn paced_until(&self) -> Option<Instant> {
        self.min_interval?;
        let due = self.next_due?;
        (due > Instant::now()).then_some(due)
    }

    /// The pending frame, if credit and pacing allow it to go now.
    fn take_sendable(&mut self) -> Option<Published> {
        if !self.has_credit() || self.paced_until().is_some() {
            return None;
        }
        let frame = self.pending.take()?;
        if let Some(credits) = self.credits.as_mut() {
            *credits -= 1;
        }
        if let Some(interval) = self.min_interval {
            self.next_due = Some(next_due(self.next_due, interval, Instant::now()));
        }
        Some(frame)
    }
}

/// The range a `max_fps` is held to: a frame every ten seconds at the
/// slowest, and at the fastest more often than any display draws.
const MIN_FPS: f64 = 0.1;
const MAX_FPS: f64 = 1000.0;

/// When the frame after one sent at `now` is due, `previous` having been when
/// that one was.
///
/// One interval after the previous due time, so the average rate is exactly
/// the one asked for: a frame that goes late is followed by one that goes
/// early. After an idle spell -- nothing captured, or no credit -- that would
/// let a burst through to catch up, so a schedule more than an interval
/// behind starts again from now.
fn next_due(previous: Option<Instant>, interval: Duration, now: Instant) -> Instant {
    match previous {
        Some(due) if due + interval >= now => due + interval,
        _ => now + interval,
    }
}

/// Most credit a connection may hold. A display returns one per frame drawn
/// and needs a handful in flight to cover the round trip; more than this is
/// a client asking to be sent a queue, which is what credit exists to stop.
const MAX_CREDITS: u32 = 64;

/// Longest a command is waited for before the client is told it went
/// unanswered. Past every wait the scope thread has of its own, so this only
/// fires for a unit wedged inside a driver call -- which used to leave the
/// client waiting for ever. The command may still be carried out when the
/// thread gets to it.
const COMMAND_TIMEOUT: Duration = Duration::from_secs(30);

/// Commands a connection may have waiting behind the one being answered.
/// Past this the socket is not read until one finishes, which holds a client
/// that pipelines without limit to the pace of the scope.
const MAX_QUEUED_COMMANDS: usize = 32;

/// A command waiting its turn, or the reason one could not be read.
enum Queued {
    Command(Command),
    Unparsable(String),
}

/// A command being answered: the reply, and the capture that goes with it.
type Answering = std::pin::Pin<
    Box<dyn std::future::Future<Output = (String, Option<Arc<protocol::CaptureFrame>>)> + Send>,
>;

/// Drive one client: read commands, write responses, forward captures.
///
/// Commands are answered one at a time and in the order they came, but not
/// in the read loop: a command that waits on the scope -- a capture at a
/// slow timebase, or a wedged unit -- would otherwise stop the captures,
/// credit, pings and state pushes on this connection until it was answered.
async fn serve_connection<S>(stream: S, scope: ScopeHandle, command_timeout: Duration) -> Result<()>
where
    S: AsyncRead + AsyncWrite + Unpin + Send + 'static,
{
    let ws = accept_async(stream).await?;
    let (mut sink, mut source) = ws.split();

    // No subscription until the client asks for one. A control-only client
    // (CLI, Python driver) would otherwise be sent the whole capture stream,
    // which wastes bandwidth and puts binary frames in front of the reply it
    // is waiting for.
    let mut subscription: Option<Subscription> = None;
    // State pushes, for a connection that subscribed with `state`.
    let mut states: Option<tokio::sync::watch::Receiver<Arc<ScopeState>>> = None;
    let mut queued: std::collections::VecDeque<Queued> = std::collections::VecDeque::new();
    let mut answering: Option<Answering> = None;

    loop {
        // Subscription changes are connection state, not hardware state, so
        // they are handled here rather than on the hardware thread -- but in
        // their turn, so their replies keep the order of the commands.
        while answering.is_none() {
            let Some(next) = queued.pop_front() else { break };
            match next {
                Queued::Unparsable(message) => {
                    sink.send(Message::Text(encode(Response::Error { message }).into())).await?;
                }
                Queued::Command(Command::Subscribe { credits, max_fps, state }) => {
                    let receiver = match subscription.take() {
                        Some(existing) => existing.captures,
                        None => scope.subscribe(),
                    };
                    let credits = credits.map(|c| c.min(MAX_CREDITS));
                    subscription = Some(Subscription::new(receiver, credits, max_fps));
                    sink.send(Message::Text(encode(Response::Subscribed).into())).await?;
                    if state {
                        let mut receiver = scope.watch_state();
                        let current = receiver.borrow_and_update().clone();
                        sink.send(Message::Text(encode(Response::State {
                            state: Box::new((*current).clone()),
                        }).into())).await?;
                        states = Some(receiver);
                    } else {
                        states = None;
                    }
                }
                Queued::Command(Command::Unsubscribe) => {
                    subscription = None;
                    states = None;
                    sink.send(Message::Text(encode(Response::Unsubscribed).into())).await?;
                }
                Queued::Command(command) => {
                    answering = Some(answer(command, scope.clone(), command_timeout));
                }
            }
        }

        let paced_until = subscription
            .as_ref()
            .filter(|s| s.pending.is_some() && s.has_credit())
            .and_then(Subscription::paced_until);

        tokio::select! {
            incoming = source.next(), if queued.len() < MAX_QUEUED_COMMANDS => {
                let Some(message) = incoming else { break };
                match message? {
                    Message::Text(text) => match serde_json::from_str::<Command>(&text) {
                        // Unanswered by design (see Command::Credit), and
                        // what keeps the stream moving, so it acts at once.
                        Ok(Command::Credit { count }) => {
                            if let Some(sub) = subscription.as_mut() {
                                if let Some(credits) = sub.credits.as_mut() {
                                    *credits = credits.saturating_add(count).min(MAX_CREDITS);
                                }
                                if let Some(frame) = sub.take_sendable() {
                                    sink.send(Message::Binary(frame.encoded)).await?;
                                }
                            }
                        }
                        Ok(command) => queued.push_back(Queued::Command(command)),
                        // Previously this was logged and the client was left
                        // waiting forever for a reply that never came.
                        Err(e) => queued.push_back(Queued::Unparsable(format!(
                            "could not parse command: {e}"
                        ))),
                    },
                    Message::Binary(_) => {
                        // Nothing sends us binary. Say so rather than
                        // dropping it silently, which is what the old
                        // handler did for every unrecognised frame.
                        queued.push_back(Queued::Unparsable(
                            "binary frames are not accepted on the command plane".into(),
                        ));
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

            (reply, frame) = async {
                match answering.as_mut() {
                    Some(answer) => answer.await,
                    None => std::future::pending().await,
                }
            } => {
                answering = None;
                sink.send(Message::Text(reply.into())).await?;
                if let Some(frame) = frame {
                    sink.send(Message::Binary(frame.encode().into())).await?;
                }
            }

            // Parks forever while unsubscribed, so this arm simply never
            // fires rather than needing the loop restructured.
            capture = async {
                match subscription.as_mut() {
                    Some(sub) => sub.captures.recv().await,
                    None => std::future::pending().await,
                }
            } => {
                let Some(sub) = subscription.as_mut() else { continue };
                match capture {
                    Ok(frame) => {
                        sub.pending = Some(frame);
                        if let Some(frame) = sub.take_sendable() {
                            sink.send(Message::Binary(frame.encoded)).await?;
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(missed)) => {
                        // With credit, skipping stale captures is the design:
                        // the next one received is newer and replaces them.
                        // Without it, the client expected every capture and
                        // is told how many it did not get.
                        if sub.credits.is_none() {
                            tracing::debug!(missed, "client lagged, dropped captures");
                            let notice = encode(Response::Error {
                                message: format!(
                                    "dropped {missed} captures: client is not keeping up"
                                ),
                            });
                            sink.send(Message::Text(notice.into())).await?;
                        }
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                }
            }

            // A frame held back only by the frame rate cap goes when it lifts.
            _ = async {
                match paced_until {
                    Some(at) => tokio::time::sleep_until(at.into()).await,
                    None => std::future::pending().await,
                }
            } => {
                if let Some(sub) = subscription.as_mut() {
                    if let Some(frame) = sub.take_sendable() {
                        sink.send(Message::Binary(frame.encoded)).await?;
                    }
                }
            }

            changed = async {
                match states.as_mut() {
                    Some(receiver) => receiver.changed().await,
                    None => std::future::pending().await,
                }
            } => {
                if changed.is_err() {
                    states = None;
                    continue;
                }
                let current = match states.as_mut() {
                    Some(receiver) => receiver.borrow_and_update().clone(),
                    None => continue,
                };
                sink.send(Message::Text(encode(Response::State {
                    state: Box::new((*current).clone()),
                }).into())).await?;
            }
        }
    }

    Ok(())
}

/// Run one command, always producing a reply.
fn answer(command: Command, scope: ScopeHandle, timeout: Duration) -> Answering {
    Box::pin(async move {
        match tokio::time::timeout(timeout, handlers::handle(command, &scope)).await {
            Ok(outcome) => (encode(outcome.response), outcome.frame),
            Err(_) => (
                encode(Response::Error {
                    message: format!(
                        "the oscilloscope did not answer within {} s; it may be wedged, \
                         and need reconnecting",
                        timeout.as_secs_f64()
                    ),
                }),
                None,
            ),
        }
    })
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frames_follow_a_schedule_not_the_last_send() {
        // Sent a millisecond late, the next is still due on the schedule.
        let interval = Duration::from_micros(16_667);
        let start = Instant::now();
        let first = next_due(None, interval, start);
        assert_eq!(first, start + interval);
        let late = first + Duration::from_millis(1);
        assert_eq!(next_due(Some(first), interval, late), first + interval);
    }

    #[test]
    fn an_idle_spell_restarts_the_schedule_rather_than_bursting() {
        let interval = Duration::from_millis(10);
        let start = Instant::now();
        let due = start + interval;
        let much_later = start + Duration::from_secs(1);
        assert_eq!(next_due(Some(due), interval, much_later), much_later + interval);
    }

    fn published(seq: u64) -> Published {
        Published::new(Arc::new(protocol::CaptureFrame {
            seq,
            capture_mono_ns: 0,
            sample_interval_ns: 1.0,
            pre_trigger_samples: 0,
            post_trigger_samples: 1,
            samples_per_channel: 1,
            resolution_bits: 8,
            overflow_mask: 0,
            screen_samples: 0,
            flags: 0,
            channels: vec![],
            samples: vec![],
        }))
    }

    fn subscription(credits: Option<u32>, max_fps: Option<f64>) -> Subscription {
        let (sender, receiver) = tokio::sync::broadcast::channel(4);
        drop(sender);
        Subscription::new(receiver, credits, max_fps)
    }

    #[test]
    fn without_credit_nothing_is_sent_and_the_newest_waits() {
        let mut sub = subscription(Some(1), None);
        sub.pending = Some(published(1));
        assert_eq!(sub.take_sendable().map(|p| p.frame.seq), Some(1));
        sub.pending = Some(published(2));
        sub.pending = Some(published(3));
        assert!(sub.take_sendable().is_none(), "no credit left");
        sub.credits = Some(1);
        assert_eq!(sub.take_sendable().map(|p| p.frame.seq), Some(3), "the newest, not a queue");
    }

    #[test]
    fn a_subscriber_without_credits_is_sent_everything() {
        let mut sub = subscription(None, None);
        for seq in 1..=5 {
            sub.pending = Some(published(seq));
            assert_eq!(sub.take_sendable().map(|p| p.frame.seq), Some(seq));
        }
    }

    #[test]
    fn a_frame_rate_cap_holds_the_next_frame_until_it_is_due() {
        let mut sub = subscription(Some(10), Some(10.0));
        sub.pending = Some(published(1));
        assert!(sub.take_sendable().is_some());
        sub.pending = Some(published(2));
        assert!(sub.take_sendable().is_none(), "100 ms have not passed");
        assert!(sub.paced_until().is_some());
    }

    #[test]
    fn an_extreme_frame_rate_cap_is_held_to_its_range() {
        // A tiny one made an interval `from_secs_f64` panics on.
        assert_eq!(subscription(None, Some(1e-300)).min_interval, Some(Duration::from_secs(10)));
        assert_eq!(subscription(None, Some(1e300)).min_interval, Some(Duration::from_millis(1)));
        assert_eq!(subscription(None, Some(f64::NAN)).min_interval, None);
    }

    use crate::scope_thread::{Detached, Message as ScopeMessage, ScopeReply};
    use tokio_tungstenite::WebSocketStream;

    type Client = WebSocketStream<tokio::io::DuplexStream>;

    async fn connect(scope: ScopeHandle, timeout: Duration) -> Client {
        let (client, server) = tokio::io::duplex(1 << 20);
        tokio::spawn(serve_connection(server, scope, timeout));
        tokio_tungstenite::client_async("ws://scope/", client)
            .await
            .expect("handshake")
            .0
    }

    async fn send(client: &mut Client, command: serde_json::Value) {
        client.send(Message::Text(command.to_string().into())).await.unwrap();
    }

    /// The next message other than a ping or pong, within two seconds.
    async fn next(client: &mut Client) -> Message {
        loop {
            let message = tokio::time::timeout(Duration::from_secs(2), client.next())
                .await
                .expect("a message within two seconds")
                .expect("the connection is open")
                .expect("a valid message");
            if !matches!(message, Message::Ping(_) | Message::Pong(_)) {
                return message;
            }
        }
    }

    /// The `response` of the next message, which has to be a reply.
    async fn next_reply(client: &mut Client) -> (String, serde_json::Value) {
        match next(client).await {
            Message::Text(text) => {
                let reply: serde_json::Value = serde_json::from_str(&text).unwrap();
                let body = reply["Response"].clone();
                (body["response"].as_str().unwrap_or_default().to_string(), body)
            }
            other => panic!("expected a reply, got {other:?}"),
        }
    }

    async fn request_reaching(requests: &mut tokio::sync::mpsc::Receiver<ScopeMessage>) -> tokio::sync::oneshot::Sender<ScopeReply> {
        match requests.recv().await {
            Some(ScopeMessage::Request((_, reply_to))) => reply_to,
            _ => panic!("the command did not reach the scope"),
        }
    }

    #[tokio::test]
    async fn a_command_the_scope_never_answers_is_answered_with_an_error() {
        // The client used to wait for ever on a unit wedged in a driver call.
        let Detached { handle, requests: _held, .. } = ScopeHandle::detached();
        let mut client = connect(handle, Duration::from_millis(200)).await;

        send(&mut client, serde_json::json!({"command": "IsReady"})).await;
        let (kind, body) = next_reply(&mut client).await;
        assert_eq!(kind, "Error");
        assert!(body["message"].as_str().unwrap().contains("did not answer"), "{body}");
    }

    #[tokio::test]
    async fn captures_and_pings_keep_flowing_while_a_command_is_answered() {
        // A command used to be awaited inside the read loop, so a capture at
        // a slow timebase froze the stream on every connection that asked.
        let Detached { handle, mut requests, captures } = ScopeHandle::detached();
        let mut client = connect(handle, Duration::from_secs(30)).await;
        send(&mut client, serde_json::json!({"command": "Subscribe"})).await;
        assert_eq!(next_reply(&mut client).await.0, "Subscribed");

        send(&mut client, serde_json::json!({"command": "IsReady"})).await;
        let reply_to = request_reaching(&mut requests).await;

        captures.send(published(7)).unwrap();
        match next(&mut client).await {
            Message::Binary(bytes) => assert_eq!(bytes, published(7).encoded),
            other => panic!("expected the capture, got {other:?}"),
        }
        client.send(Message::Ping(bytes::Bytes::from_static(b"still there?"))).await.unwrap();
        let pong = tokio::time::timeout(Duration::from_secs(2), async {
            loop {
                if let Some(Ok(Message::Pong(_))) = client.next().await {
                    return;
                }
            }
        });
        pong.await.expect("a pong while the command waits");

        reply_to.send(ScopeReply::Bool(true)).unwrap();
        assert_eq!(next_reply(&mut client).await.0, "IsReady");
    }

    #[tokio::test]
    async fn replies_keep_the_order_of_the_commands() {
        // The clients match a reply to the command before it, so a subscribe
        // or a parse error answered while a command waited would be taken
        // for that command's answer.
        let Detached { handle, mut requests, .. } = ScopeHandle::detached();
        let mut client = connect(handle, Duration::from_secs(30)).await;
        send(&mut client, serde_json::json!({"command": "IsReady"})).await;
        send(&mut client, serde_json::json!({"command": "Subscribe"})).await;
        send(&mut client, serde_json::json!({"command": "NoSuchCommand"})).await;
        let reply_to = request_reaching(&mut requests).await;
        // Long enough for anything answered out of turn to arrive first.
        tokio::time::sleep(Duration::from_millis(50)).await;
        reply_to.send(ScopeReply::Bool(false)).unwrap();

        let mut order = Vec::new();
        for _ in 0..3 {
            order.push(next_reply(&mut client).await.0);
        }
        assert_eq!(order, ["IsReady", "Subscribed", "Error"]);
    }
}

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Synchronous client for the oscilloscope daemon's WebSocket control plane.

The daemon speaks JSON text frames for commands/responses and LSCP binary
frames for captures on the same socket (see ``lscp.py``). This module is the
one place in the box that knows how to reach it, so the HTTP relay, the
hardware_service adapter, and the Python driver all agree on the endpoint and
on how a daemon error becomes a Python exception.

The daemon binds loopback only; everything outside the container arrives
through the box HTTP server's authenticated relay on :9000.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Commands whose reply is followed by a second, binary LSCP frame. Callers that
# issue these must read the capture frame too or the socket desynchronizes --
# the next command would read this frame as its own reply.
BINARY_REPLY_COMMANDS = frozenset({"GetTriggeredData"})

DEFAULT_TIMEOUT = 10.0


class ScopeDaemonError(RuntimeError):
    """The daemon replied with an Error response, or could not be reached."""


class ScopeDaemonUnavailable(ScopeDaemonError):
    """The daemon is not running or not accepting connections.

    Distinguished from a command error because the caller's remedy differs: a
    box with no daemon needs the service started, not a different command.
    """


def daemon_url() -> str:
    """WebSocket URL of the local daemon.

    The trailing slash is required, not cosmetic: h11 rejects an empty request
    target, so ``ws://host:port`` fails in the handshake before it reaches the
    daemon.
    """
    override = os.environ.get("LAGER_SCOPE_DATA_URL")
    if override:
        return override
    host = os.environ.get("LAGER_SCOPE_DATA_HOST", "127.0.0.1")
    port = os.environ.get("LAGER_SCOPE_DATA_PORT", "8085")
    return "ws://%s:%s/" % (host, port)


class _DaemonReceiveThread(threading.Thread):
    """A `simple_websocket` receive thread that does not outlive its process.

    `simple_websocket` starts its receive thread without ``daemon=True``, so
    an open connection keeps the interpreter alive: a script that took one
    capture and returned would print its results and then sit there until the
    socket happened to close, which looks exactly like a hung script. Nothing
    here is worth blocking process exit for -- an abandoned connection is the
    daemon's to reap.
    """

    def __init__(self, *args, **kwargs):
        kwargs["daemon"] = True
        super().__init__(*args, **kwargs)


class ScopeDaemonClient:
    """One WebSocket connection to the daemon.

    Not thread-safe by construction -- a command and its reply are two
    operations on one socket, so concurrent callers would read each other's
    replies. An internal lock makes each ``command`` atomic; callers that need
    a multi-step sequence to be atomic should hold their own lock (the
    hardware_service ``device_id`` lock already does this for net commands).
    """

    def __init__(self, url: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._url = url or daemon_url()
        self._timeout = timeout
        self._ws = None
        self._lock = threading.Lock()

    # -- connection ------------------------------------------------------
    def connect(self):
        import simple_websocket

        if self._ws is not None:
            return self._ws
        try:
            self._ws = simple_websocket.Client(
                self._url, thread_class=_DaemonReceiveThread)
        except Exception as e:
            raise ScopeDaemonUnavailable(
                "oscilloscope daemon unreachable at %s: %s" % (self._url, e)) from e
        return self._ws

    def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                # A close on an already-dead socket is not an error worth
                # propagating; the caller is done with it either way.
                pass

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    # -- commands --------------------------------------------------------
    def command(self, name: str, timeout: float | None = None, **params):
        """Send one command and return its parsed JSON response.

        For commands in ``BINARY_REPLY_COMMANDS`` the trailing binary frame is
        read as well and returned as ``(response, frame_bytes)``.
        """
        payload = {"command": name}
        payload.update({k: v for k, v in params.items() if v is not None})
        wait = self._timeout if timeout is None else timeout

        with self._lock:
            ws = self.connect()
            try:
                ws.send(json.dumps(payload))
                reply = self._await_text(ws, name, wait)
            except (ScopeDaemonError, ScopeDaemonUnavailable):
                raise
            except Exception as e:
                self.close()
                raise ScopeDaemonUnavailable(
                    "oscilloscope daemon connection lost during %s: %s" % (name, e)) from e

            response = _unwrap(reply, name)

            if name in BINARY_REPLY_COMMANDS:
                frame = self._await_capture(ws, name, wait, response.get("seq"))
                return response, frame

            return response

    def _await_text(self, ws, name: str, wait: float) -> str:
        """Read frames until a text one arrives, skipping capture frames.

        A connection that has issued ``Subscribe`` receives LSCP binary
        captures interleaved with command replies. Treating the first frame as
        the reply would read a capture as the response and leave every
        subsequent command reading the previous one's answer -- an off-by-one
        that returns plausible-looking wrong numbers rather than an error.

        Bounded so a client that is subscribed to a fast stream cannot spin
        here forever if its reply never comes.
        """
        deadline = time.monotonic() + wait
        skipped = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ScopeDaemonError(
                    "timed out waiting for %s response%s" % (
                        name,
                        " (skipped %d capture frames)" % skipped if skipped else ""))

            frame = ws.receive(timeout=remaining)
            if frame is None:
                continue
            if isinstance(frame, (bytes, bytearray)):
                skipped += 1
                continue
            if skipped:
                logger.debug("skipped %d capture frames awaiting %s reply", skipped, name)
            return frame

    def _await_capture(self, ws, name: str, wait: float, expected_seq) -> bytes:
        """Read the capture frame that answers a command, by sequence number.

        The acknowledgement names the sequence it is about. On a subscribed
        connection, broadcast captures share the socket, so the next binary
        frame is not necessarily the requested one; matching on `seq` returns
        the capture that was actually asked for instead of whichever arrived
        first.
        """
        deadline = time.monotonic() + wait
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ScopeDaemonError("timed out waiting for %s capture" % name)

            frame = ws.receive(timeout=remaining)
            if frame is None:
                continue
            if not isinstance(frame, (bytes, bytearray)):
                # A text frame here is a daemon-side error notice (a lag
                # warning, for instance). Surface it rather than hanging.
                _unwrap(frame, name)
                continue

            frame = bytes(frame)
            if expected_seq is None:
                return frame

            from . import lscp
            try:
                if lscp.peek_seq(frame) == expected_seq:
                    return frame
            except lscp.LscpError as e:
                self.close()
                raise ScopeDaemonError("malformed capture after %s: %s" % (name, e)) from e

    def capture(self, timeout: float | None = None):
        """Fetch one triggered capture, decoded into a ``CaptureFrame``."""
        from . import lscp

        _response, frame = self.command("GetTriggeredData", timeout=timeout)
        return lscp.decode(frame)


def _unwrap(reply, command_name: str) -> dict:
    """Parse a daemon reply, raising on the Error response."""
    try:
        message = json.loads(reply)
    except (TypeError, ValueError) as e:
        raise ScopeDaemonError(
            "malformed %s response: %r" % (command_name, reply)) from e

    # The daemon wraps responses as {"Response": {...}} on the WebSocket.
    response = message.get("Response", message) if isinstance(message, dict) else message
    if not isinstance(response, dict):
        raise ScopeDaemonError("malformed %s response: %r" % (command_name, reply))

    if response.get("response") == "Error":
        raise ScopeDaemonError(response.get("message") or "unknown daemon error")
    return response


def command(name: str, timeout: float | None = None, **params):
    """One-shot command on a short-lived connection.

    For the HTTP handlers, where a persistent connection would outlive the
    request and hold a daemon slot open for nothing.
    """
    with ScopeDaemonClient(timeout=timeout or DEFAULT_TIMEOUT) as client:
        return client.command(name, timeout=timeout, **params)

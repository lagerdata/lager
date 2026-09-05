# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Oscilloscope routes on the box HTTP server (:9000).

Scope nets reach the hardware the same way every other net does -- through
:9000 -- rather than through a second published port. That is the whole point
of this module: the daemon binds loopback only, and this relay is the bridge.

Three surfaces:

``GET  /scope``
    The UI itself, plus its assets under ``/scope/static/``. Previously this
    needed a second HTTP server on port 8081 that the box never published, so
    the page could not be opened from a browser.

``GET  /scope/<net>/stream``
    Issues a short-lived ticket naming the WebSocket URL to open, plus the
    unit's detected capabilities so a client can build its controls in one
    round trip instead of probing.

``GET  /scope/<net>/ws``
    The relay. Copies frames verbatim between the browser and the daemon, so
    LSCP binary captures pass through untouched -- no decode, re-encode, or
    JSON on the hot path. This is what keeps the relay's cost independent of
    capture size.

``/scope`` Socket.IO namespace
    Live state (connected/streaming, capability changes) for UI elements that
    should react without polling. Deliberately NOT the capture path: Socket.IO
    framing plus its threading-mode fallback would throttle the stream that
    the LSCP work exists to speed up.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time

from flask import jsonify, request, send_from_directory

logger = logging.getLogger(__name__)

# How long a ticket stays redeemable. Long enough for a page load and a user
# clicking "connect", short enough that a stale tab cannot resume streaming a
# net that has since been reassigned to different hardware.
TICKET_TTL_SECONDS = 120.0

# Bounded so a client that requests tickets in a loop cannot grow the table
# without limit. Eviction is oldest-first; a legitimate client holds one.
MAX_ACTIVE_TICKETS = 64

# Relay receive slice. The pumps block for this long, then re-check the stop
# flag, so a closed peer tears down both directions within one slice instead
# of leaking a thread until the next frame happens to arrive.
_PUMP_SLICE_SECONDS = 0.5

_tickets: dict[str, dict] = {}
_tickets_lock = threading.Lock()


def _issue_ticket(netname: str) -> str:
    token = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _tickets_lock:
        # Drop expired entries on every issue; there is no background sweeper
        # and this is the only path that grows the table.
        for stale in [t for t, m in _tickets.items() if m["expires"] <= now]:
            del _tickets[stale]
        while len(_tickets) >= MAX_ACTIVE_TICKETS:
            oldest = min(_tickets, key=lambda t: _tickets[t]["expires"])
            del _tickets[oldest]
        _tickets[token] = {"net": netname, "expires": now + TICKET_TTL_SECONDS}
    return token


def _redeem_ticket(token: str, netname: str) -> bool:
    """Validate a token for this net.

    Not single-use: a browser that drops its socket reconnects with the same
    ticket rather than round-tripping for a new one, which matters on a flaky
    link where reconnects are exactly when you least want an extra request.
    """
    if not token:
        return False
    now = time.monotonic()
    with _tickets_lock:
        meta = _tickets.get(token)
        if meta is None:
            return False
        if meta["expires"] <= now:
            del _tickets[token]
            return False
        return meta["net"] == netname


def _resolve_scope_net(netname: str):
    """Return the saved-net record for a scope net, or None.

    Kept tolerant of the role string because scope nets have been saved as
    both "scope" and "analog" depending on when they were created.
    """
    from lager.nets.net import Net

    for entry in Net.get_local_nets():
        if entry.get("name") != netname:
            continue
        if entry.get("role") in ("scope", "analog"):
            return entry
    return None


def _static_dir() -> str:
    """Directory holding the UI assets, resolved relative to this package."""
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "scope")


def register_scope_routes(app):
    """Register the scope HTTP + WebSocket routes on the Flask app."""

    @app.route("/scope", methods=["GET"])
    @app.route("/scope/", methods=["GET"])
    def scope_ui():
        # Served from :9000 alongside every other net surface. The previous
        # UI needed a second HTTP server on :8081 that was never published,
        # so the page was unreachable from a browser off the box.
        return send_from_directory(_static_dir(), "index.html")

    @app.route("/scope/static/<path:filename>", methods=["GET"])
    def scope_ui_asset(filename):
        # send_from_directory rejects paths that escape the directory, so a
        # traversal attempt cannot reach outside the asset folder.
        return send_from_directory(_static_dir(), filename)

    @app.route("/scope/<netname>/stream", methods=["GET"])
    def scope_stream_ticket(netname):
        from lager.measurement.scope import daemon_client

        rec = _resolve_scope_net(netname)
        if rec is None:
            return jsonify({
                "success": False,
                "error": "Net '%s' not found or is not a scope net" % netname,
            }), 404

        capabilities = None
        capability_error = None
        try:
            response = daemon_client.command("GetCapabilities", timeout=5.0)
            capabilities = response.get("capabilities")
        except daemon_client.ScopeDaemonUnavailable as e:
            # No daemon means no stream, so fail loudly rather than handing
            # out a ticket that cannot be redeemed.
            return jsonify({"success": False, "error": str(e)}), 503
        except daemon_client.ScopeDaemonError as e:
            # The daemon is up but could not describe the unit -- an older
            # daemon, or no scope attached. A ticket is still useful (commands
            # work), so report the gap instead of refusing.
            capability_error = str(e)
            logger.warning("scope %s: capability probe failed: %s", netname, e)

        token = _issue_ticket(netname)
        return jsonify({
            "success": True,
            "net": netname,
            "instrument": rec.get("instrument") or "",
            "ws_path": "/scope/%s/ws?token=%s" % (netname, token),
            "token": token,
            "expires_in": int(TICKET_TTL_SECONDS),
            "capabilities": capabilities,
            "capability_error": capability_error,
        })

    # websocket=True is required, not optional: Werkzeug's router matches
    # upgrade requests only against rules declared this way, and raises
    # WebsocketMismatch (a 400 that never reaches the view) otherwise.
    @app.route("/scope/<netname>/ws", methods=["GET"], websocket=True)
    def scope_stream_relay(netname):
        import simple_websocket

        from lager.measurement.scope import daemon_client

        token = request.args.get("token", "")
        if not _redeem_ticket(token, netname):
            return jsonify({
                "success": False,
                "error": "Invalid or expired stream ticket; GET /scope/%s/stream first"
                         % netname,
            }), 403

        try:
            browser = simple_websocket.Server(_no_compression(request.environ))
        except simple_websocket.ConnectionError:
            return jsonify({"success": False, "error": "Expected a WebSocket upgrade"}), 400

        try:
            upstream = simple_websocket.Client(daemon_client.daemon_url())
        except Exception as e:
            logger.warning("scope %s: daemon unreachable for relay: %s", netname, e)
            # Report through the socket the client already has open; a 503
            # cannot be sent after the upgrade completed.
            try:
                browser.send(json.dumps({"Response": {
                    "response": "Error",
                    "message": "oscilloscope daemon unreachable: %s" % e,
                }}))
                browser.close()
            except Exception:
                pass
            return ""

        _disable_nagle(browser, upstream)
        _relay(browser, upstream, netname)
        return ""

    logger.info("Scope routes registered (ticket + WebSocket relay on /scope)")


def _disable_nagle(*sockets) -> None:
    """Turn off Nagle on both ends of the relay.

    Command frames are tens of bytes. With Nagle on, a small write waits for
    the peer's ACK of the previous segment, and the peer's delayed-ACK timer
    holds that ACK back -- the two together produce the ~40 ms stalls measured
    on STG-2 as a command-RTT p99 of 42 ms against a 2.6 ms median. Captures
    are large enough to fill segments and never saw it, which is why this shows
    up only on the control direction.
    """
    import socket as _socket

    for wrapper in sockets:
        sock = getattr(wrapper, "sock", None)
        if sock is None:
            continue
        try:
            sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        except OSError as e:
            # A Unix-domain socket has no TCP_NODELAY; nothing to do and
            # nothing wrong, so do not let it break the relay.
            logger.debug("TCP_NODELAY not applied: %s", e)


def _no_compression(environ: dict) -> dict:
    """Return a copy of ``environ`` with the client's compression offer removed.

    simple_websocket always answers with ``PerMessageDeflate``, and wsproto
    negotiates it whenever the client offered it -- which browsers and the
    ``websockets`` library both do by default. Deflating LSCP captures is pure
    loss: the payload is raw ADC counts with little redundancy, so it burns CPU
    per frame for almost no size win, and measured on STG-2 it added ~2.6 ms to
    median capture-to-client latency with ~45 ms spikes on the shared GIL.

    Removing the offer from the copy the handshake reads is the only lever
    available, since Server hardcodes the extension it responds with. The
    original environ is left untouched so nothing else sees a doctored request.
    """
    if "HTTP_SEC_WEBSOCKET_EXTENSIONS" not in environ:
        return environ
    stripped = dict(environ)
    del stripped["HTTP_SEC_WEBSOCKET_EXTENSIONS"]
    return stripped


def _relay(browser, upstream, netname: str) -> None:
    """Copy frames verbatim in both directions until either side closes.

    Frames are forwarded as received -- ``bytes`` stay binary, ``str`` stays
    text -- so an LSCP capture crosses the relay without being decoded. The
    relay never parses the protocol, which also means a daemon protocol change
    needs no change here.
    """
    stop = threading.Event()
    stats = {"to_daemon": 0, "to_browser": 0, "bytes": 0}

    def pump(name, source, sink, counter):
        try:
            while not stop.is_set():
                try:
                    message = source.receive(timeout=_PUMP_SLICE_SECONDS)
                except TimeoutError:
                    # simple_websocket raises rather than returning None on
                    # some versions; both mean "no frame yet".
                    continue
                if message is None:
                    continue
                sink.send(message)
                stats[counter] += 1
                if isinstance(message, (bytes, bytearray)):
                    stats["bytes"] += len(message)
        except Exception as e:
            # A peer closing is the normal way a stream ends, so this is
            # debug, not a warning; the finally below is what matters.
            logger.debug("scope %s: %s pump ended: %s", netname, name, e)
        finally:
            stop.set()

    up = threading.Thread(
        target=pump, args=("browser->daemon", browser, upstream, "to_daemon"),
        name="scope-relay-up", daemon=True)
    up.start()

    # The high-rate direction runs on the request thread: it is the one that
    # must not be delayed, and keeping it here avoids an extra hand-off.
    pump("daemon->browser", upstream, browser, "to_browser")

    up.join(timeout=2.0)
    for side in (upstream, browser):
        try:
            side.close()
        except Exception:
            pass

    logger.info(
        "scope %s: relay closed (%d frames up, %d frames down, %.1f MiB)",
        netname, stats["to_daemon"], stats["to_browser"],
        stats["bytes"] / (1024 * 1024))


def register_scope_socketio(socketio):
    """Register the ``/scope`` Socket.IO namespace for live state.

    Control-plane only. Captures go over the relay above.
    """
    from flask_socketio import emit

    @socketio.on("connect", namespace="/scope")
    def scope_connect(_auth=None):
        emit("state", {"connected": True})

    @socketio.on("capabilities", namespace="/scope")
    def scope_capabilities(_data=None):
        from lager.measurement.scope import daemon_client

        try:
            response = daemon_client.command("GetCapabilities", timeout=5.0)
            emit("capabilities", {
                "success": True,
                "capabilities": response.get("capabilities"),
            })
        except daemon_client.ScopeDaemonError as e:
            emit("capabilities", {"success": False, "error": str(e)})

    logger.info("Scope Socket.IO namespace registered (/scope)")

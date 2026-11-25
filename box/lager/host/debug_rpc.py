# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Debug-net calls from a Box container, served by the agent.

On a Linux Box the debug net object (``lager.nets.debug_net.DebugNet``) runs
in the container, next to the probe. Where the container has no probe, the
container holds a stand-in (``lager.hostlink.debug_proxy``) that sends each
call here, and the real object runs in this process instead.

RTT is a byte stream, so it is not returned in a response: ``rtt_open``
starts the real RTT session here and bridges it to one loopback TCP
connection, whose port it returns.
"""
from __future__ import annotations

import base64
import logging
import os
import select
import socket
import tempfile
import threading

from flask import Flask, jsonify, request

from lager.hostlink import config, protocol

logger = logging.getLogger(__name__)

CALL_PATH = "/v1/debug/call"

# What a container may ask for. Everything else is refused, not looked up.
_METHODS = frozenset({
    "connect", "disconnect", "reset", "halt", "flash", "erase",
    "read_memory", "status",
})
_DESCRIBED = ("serial", "backend", "device", "gdb_port", "rtt_telnet_port",
              "openocd_tcl_port")
_ACCEPT_TIMEOUT_S = 15.0
_POLL_S = 0.1


def _make(name, net_info):
    from lager.nets.debug_net import make_debug

    return make_debug(name, net_info)


def _describe(dbg) -> dict:
    return {key: getattr(dbg, key, None) for key in _DESCRIBED}


def _bridge(session, listener: socket.socket) -> None:
    """Carry one client's bytes to and from an open RTT session."""
    client = None
    try:
        listener.settimeout(_ACCEPT_TIMEOUT_S)
        client, _ = listener.accept()
        listener.close()
        while True:
            readable, _, _ = select.select([client], [], [], 0)
            if readable:
                data = client.recv(4096)
                if not data:
                    break
                session.write(data)
            data = session.read_some(timeout=_POLL_S)
            if data:
                client.sendall(data)
    except OSError as exc:
        logger.info("RTT bridge closed: %s", exc)
    except Exception:
        logger.exception("RTT bridge failed")
    finally:
        for sock in (client, listener):
            try:
                if sock is not None:
                    sock.close()
            except OSError:
                pass
        try:
            session.__exit__(None, None, None)
        except Exception:
            logger.exception("closing the RTT session failed")


def _rtt_open(dbg, kwargs) -> dict:
    session = dbg.rtt(**kwargs)
    session.__enter__()
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((config.DEFAULT_BIND, 0))
        listener.listen(1)
    except OSError:
        session.__exit__(None, None, None)
        raise
    port = listener.getsockname()[1]
    threading.Thread(target=_bridge, args=(session, listener),
                     name=f"rtt-bridge-{port}", daemon=True).start()
    return {"port": port}


def _flash(dbg, args, kwargs, firmware) -> object:
    """Flash from bytes: the container's path means nothing here."""
    suffix = os.path.splitext(firmware.get("name") or "")[1] or ".bin"
    fd, path = tempfile.mkstemp(prefix="lager-flash-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(base64.b64decode(firmware["data"]))
        return dbg.flash(path, *args[1:], **kwargs)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def install(app: Flask) -> Flask:
    """Add the debug route. The agent's token guard already covers it."""

    @app.route(CALL_PATH, methods=["POST"])
    def _hostlink_debug_call():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("method"), str):
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, "Body must name a method.")), 400
        method = data["method"]
        args = data.get("args") or []
        kwargs = data.get("kwargs") or {}
        try:
            dbg = _make(data.get("name"), data.get("net_info") or {})
            if method == "describe":
                result = _describe(dbg)
            elif method == "rtt_open":
                result = _rtt_open(dbg, kwargs)
            elif method == "flash" and isinstance(data.get("firmware"), dict):
                result = _flash(dbg, args, kwargs, data["firmware"])
            elif method in _METHODS:
                result = getattr(dbg, method)(*args, **kwargs)
            else:
                return jsonify(protocol.error_body(
                    protocol.ERR_BAD_REQUEST, f"Unknown debug method {method!r}.")), 400
        except Exception as exc:  # the caller re-raises this in the script
            logger.info("debug %s failed: %s", method, exc)
            return jsonify({"error": str(exc), "type": type(exc).__name__,
                            "module": type(exc).__module__}), 500
        if isinstance(result, (bytes, bytearray)):
            return jsonify({"result_b64": base64.b64encode(bytes(result)).decode()})
        return app.response_class(
            __import__("json").dumps({"result": result}, default=str),
            mimetype="application/json")

    return app

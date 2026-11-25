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
import binascii
import logging
import os
import re
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
# connect() overrides that the debug net reads as a path OR base64 content.
# From the container only content is accepted: a path would name a host file.
_SCRIPT_KWARGS = ("script", "openocd_config", "jlink_script")
_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,15}$")
# The firmware types DebugNet.flash() accepts. The temp file takes its suffix
# from this table, never from the request, so no character the container sent
# reaches the file name.
_FLASH_SUFFIXES = {".hex": ".hex", ".bin": ".bin", ".elf": ".elf"}


class _Refused(ValueError):
    """A request the agent will not carry out; answered with 400."""


def _host_safe_net_info(net_info) -> dict:
    """``net_info`` without the ``*_path`` keys, which the debug net would
    open on this machine. The base64 forms of the same scripts are kept."""
    if not isinstance(net_info, dict):
        return {}
    return {k: v for k, v in net_info.items()
            if not (isinstance(k, str) and k.endswith("_path"))}


def _check_script_kwargs(kwargs) -> None:
    for key in _SCRIPT_KWARGS:
        value = kwargs.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if not isinstance(value, str):
            raise _Refused(f"{key} must be the script's content, base64-encoded")
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            raise _Refused(f"{key} must be the script's content, base64-encoded, "
                           "not a path") from None
        # '/' is a base64 character: the debug net tries a value as a path
        # before decoding it, so one that names a file here is refused too.
        if os.path.exists(value):
            raise _Refused(f"{key} must be the script's content, not a path")


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


def _flash_address(args, kwargs):
    """The one argument flash() takes besides the file: ``flash_address``,
    positional (as the container's stand-in sends it) or by name. Anything
    else is refused, so nothing from the request can stand in for the file."""
    if len(args) > 2 or set(kwargs) - {"flash_address"}:
        raise _Refused("flash takes the firmware and flash_address only")
    if len(args) == 2 and "flash_address" in kwargs:
        raise _Refused("flash_address given twice")
    if "flash_address" in kwargs:
        return kwargs["flash_address"]
    return args[1] if len(args) == 2 else None


def _flash(dbg, firmware, flash_address) -> object:
    """Flash from bytes: the container's path means nothing here."""
    ext = os.path.splitext(str(firmware.get("name") or ""))[1].lower()
    suffix = _FLASH_SUFFIXES.get(ext)
    if suffix is None:
        if _SUFFIX.match(ext):
            # The same refusal DebugNet.flash() gives, before any file is written.
            raise ValueError(f"Unsupported firmware file type: {ext}")
        suffix = ".bin"
    fd, path = tempfile.mkstemp(prefix="lager-flash-", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(base64.b64decode(firmware["data"]))
        return dbg.flash(path, flash_address)
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
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, "args must be a list and kwargs an object.")), 400
        firmware = data.get("firmware")
        flash_address = None
        try:
            if method == "flash" and not (isinstance(firmware, dict)
                                          and isinstance(firmware.get("data"), str)):
                # A path here would be read on this machine, not in the container.
                raise _Refused("flash needs the firmware's bytes, not a path")
            if method == "flash":
                flash_address = _flash_address(args, kwargs)
            if method == "connect":
                _check_script_kwargs(kwargs)
        except _Refused as exc:
            return jsonify(protocol.error_body(protocol.ERR_BAD_REQUEST, str(exc))), 400
        try:
            dbg = _make(data.get("name"), _host_safe_net_info(data.get("net_info")))
            if method == "describe":
                result = _describe(dbg)
            elif method == "rtt_open":
                result = _rtt_open(dbg, kwargs)
            elif method == "flash":
                result = _flash(dbg, firmware, flash_address)
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

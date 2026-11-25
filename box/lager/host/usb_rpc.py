# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""USB hub calls and USB discovery for a Box container, served by the agent.

On a Linux Box the hub drivers (``lager.automation.usb_hub``) run in the
container, next to the hub. Where the container has no USB, it holds a
stand-in (``lager.hostlink.usb_proxy``) that sends each call here, and the
real driver runs in this process instead.

The same goes for looking at the bus: which devices are attached, and which
of them are instruments (``lager instruments``).
"""
from __future__ import annotations

import json
import logging
import sys

from flask import Flask, jsonify, request

from lager.hostlink import protocol

logger = logging.getLogger(__name__)

CALL_PATH = "/v1/usb/call"
IDS_PATH = "/v1/usb/ids"
INSTRUMENTS_PATH = "/v1/instruments/list"

# What a container may ask for. Everything else is refused, not looked up.
_METHODS = frozenset({
    "enable", "disable", "toggle", "state", "cycle", "recover", "states",
    "_lock_key",
})
_ERROR_FIELDS = ("classification", "usb_context_healthy", "detail")


def _controller(net_info):
    from lager.automation.usb_hub import dispatcher

    return dispatcher._controller_for(net_info)


def _note_powered_on(controller, method, args, result) -> None:
    """What the Box's dispatcher records after it turns a port on."""
    from lager.automation.usb_hub import usb_net

    powered = method in ("enable", "recover") or (method == "toggle" and result)
    if powered and len(args) >= 2:
        usb_net.note_powered_on(controller, args[1])


def _usb_ids():
    """The bus as ``[[vid, pid, [serial, ...]], ...]``, or None if unknown."""
    from lager.util import self_restart

    ids = self_restart.enumerated_usb_ids()
    if ids is None:
        return None
    return [[vid, pid, serials] for (vid, pid), serials in sorted(ids.items())]


def _list_instruments():
    if sys.platform == "darwin":
        from . import darwin_scan

        return darwin_scan.list_instruments()
    from lager.http_handlers import usb_scanner

    return usb_scanner.list_instruments()


def install(app: Flask) -> Flask:
    """Add the USB routes. The agent's token guard already covers them."""

    @app.route(IDS_PATH, methods=["GET"])
    def _hostlink_usb_ids():
        return jsonify({"devices": _usb_ids()})

    @app.route(INSTRUMENTS_PATH, methods=["GET"])
    def _hostlink_instruments_list():
        try:
            return jsonify({"instruments": _list_instruments()})
        except Exception as exc:  # the container reports it as its own scan failing
            logger.exception("instrument scan failed")
            return jsonify({"error": str(exc), "type": type(exc).__name__}), 500

    @app.route(CALL_PATH, methods=["POST"])
    def _hostlink_usb_call():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("method"), str):
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, "Body must name a method.")), 400
        method = data["method"]
        if method not in _METHODS:
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, f"Unknown USB method {method!r}.")), 400
        args = data.get("args") or []
        kwargs = data.get("kwargs") or {}
        try:
            controller = _controller(data.get("net_info") or {})
            result = getattr(controller, method)(*args, **kwargs)
            _note_powered_on(controller, method, args, result)
        except Exception as exc:  # the caller re-raises this as the same class
            logger.info("usb %s failed: %s: %s", method, type(exc).__name__, exc)
            body = {"error": str(exc), "type": type(exc).__name__}
            for field in _ERROR_FIELDS:
                value = getattr(exc, field, None)
                if value is not None:
                    body[field] = value
            return app.response_class(json.dumps(body, default=str),
                                      status=500, mimetype="application/json")
        return app.response_class(json.dumps({"result": result}, default=str),
                                  mimetype="application/json")

    return app

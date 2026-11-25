# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Any net's driver, run in the agent for a script in the container.

On a Linux Box ``Net.get('x', NetType.WattMeter)`` in a script returns the
driver itself, which opens the instrument from the script's process. Where
the container has no hardware, the script holds a stand-in
(``lager.hostlink.net_proxy``) and the object ``Net.get`` would have
returned lives here instead. The stand-in forwards every method call and
attribute read, so the script sees the driver's whole interface, not a
subset chosen in advance.

An object lives as long as the script's stand-in: ``open`` makes it,
``close`` drops it, and every object is dropped when the script runner asks
the hardware service to release its USB claims, which it does before each
script. A value with no JSON form (an HSM session, a generator) stays here
and is handed back as a handle of its own.
"""
from __future__ import annotations

import inspect
import itertools
import json
import logging
import threading

from flask import Flask, jsonify, request

from lager.hostlink import protocol, wire

logger = logging.getLogger(__name__)

OPEN_PATH = "/v1/net/open"
OPEN_DRIVER_PATH = "/v1/net/open_driver"
CALL_PATH = "/v1/net/call"
CLOSE_PATH = "/v1/net/close"
RELEASE_PATH = "/cache/release_direct_usb"
# Dunder methods a stand-in may forward; no other underscore name is reachable.
_ALLOWED_DUNDERS = frozenset({"__enter__", "__exit__", "__next__", "__iter__",
                              "__len__", "__getitem__", "__contains__", "__call__"})

_lock = threading.Lock()
_objects = {}
_ids = itertools.count(1)


class _Entry:
    def __init__(self, obj):
        self.obj = obj
        self.lock = threading.RLock()


def _make(name, role):
    from lager.nets.constants import NetType
    from lager.nets.net import Net

    return Net.get(name, type=NetType.from_role(role))


def _dispatcher(module, name):
    """An instance of the Box dispatcher class ``module.name``; nothing else."""
    import importlib

    from lager.dispatchers.base import BaseDispatcher

    if not (isinstance(module, str) and isinstance(name, str)
            and module.startswith("lager.") and name.isidentifier()):
        raise ValueError("Not a Box dispatcher.")
    cls = getattr(importlib.import_module(module), name, None)
    if not (isinstance(cls, type) and issubclass(cls, BaseDispatcher)):
        raise ValueError("Not a Box dispatcher.")
    return cls()


def describe(obj) -> dict:
    """The public surface of ``obj``: what a stand-in may call and read."""
    methods, values = [], []
    cls = type(obj)
    for attr in dir(obj):
        if attr.startswith("_") and attr not in _ALLOWED_DUNDERS:
            continue
        static = inspect.getattr_static(cls, attr, None)
        if isinstance(static, property):
            values.append(attr)  # read on demand; a property may touch hardware
            continue
        try:
            value = getattr(obj, attr)
        except Exception:  # noqa: BLE001 - an attribute that raises is not offered
            continue
        (methods if callable(value) else values).append(attr)
    return {"methods": methods, "values": values, "type": cls.__name__,
            "module": cls.__module__, "repr": repr(obj)[:200]}


def _register(obj) -> dict:
    handle = next(_ids)
    with _lock:
        _objects[handle] = _Entry(obj)
    return dict(describe(obj), __handle__=handle)


def _close(obj) -> None:
    close = getattr(obj, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - closing is best effort
            logger.info("closing %s failed: %s", type(obj).__name__, exc)


def release_all() -> int:
    """Drop every object: a new script is about to open the instruments."""
    with _lock:
        entries = list(_objects.values())
        _objects.clear()
    for entry in entries:
        with entry.lock:
            _close(entry.obj)
    return len(entries)


def _error(exc):
    cls = type(exc)
    body = {"error": str(exc), "type": cls.__name__, "module": cls.__module__,
            "stop": isinstance(exc, StopIteration)}
    return body, 500


def _bad(message):
    return jsonify(protocol.error_body(protocol.ERR_BAD_REQUEST, message)), 400


def install(app: Flask) -> Flask:
    """Add the routes. The agent's token guard already covers them."""

    @app.before_request
    def _hostlink_net_release():
        if request.path == RELEASE_PATH and request.method == "POST":
            dropped = release_all()
            if dropped:
                logger.info("released %d script-held driver object(s)", dropped)

    def respond(payload, status=200):
        return app.response_class(json.dumps(payload), status=status,
                                  mimetype="application/json")

    @app.route(OPEN_PATH, methods=["POST"])
    def _hostlink_net_open():
        data = request.get_json(silent=True) or {}
        name, role = data.get("name"), data.get("role")
        if not isinstance(name, str) or not isinstance(role, str):
            return _bad("Body must name a net and its role.")
        try:
            return respond(_register(_make(name, role)))
        except Exception as exc:  # the stand-in raises it as the same class
            logger.info("net open %s (%s) failed: %s: %s", name, role,
                        type(exc).__name__, exc)
            return respond(*_error(exc))

    @app.route(OPEN_DRIVER_PATH, methods=["POST"])
    def _hostlink_net_open_driver():
        data = request.get_json(silent=True) or {}
        try:
            dispatcher = _dispatcher(data.get("module"), data.get("class"))
        except (ValueError, ImportError) as exc:
            return _bad(str(exc))
        try:
            driver = dispatcher._make_driver(*wire.decode(data.get("args") or []),
                                             **wire.decode(data.get("kwargs") or {}))
            return respond(_register(driver))
        except Exception as exc:  # the stand-in raises it as the same class
            logger.info("%s._make_driver failed: %s: %s", data.get("class"),
                        type(exc).__name__, exc)
            return respond(*_error(exc))

    @app.route(CALL_PATH, methods=["POST"])
    def _hostlink_net_call():
        data = request.get_json(silent=True) or {}
        attr = data.get("attr")
        if not isinstance(attr, str) or (attr.startswith("_")
                                         and attr not in _ALLOWED_DUNDERS):
            return _bad("Body must name a public attribute.")
        with _lock:
            entry = _objects.get(data.get("handle"))
        if entry is None:
            return respond({"error": "This net object is no longer open on the host "
                                     "agent. Call Net.get again.",
                            "type": "RuntimeError", "module": "builtins",
                            "stop": False}, 500)
        try:
            with entry.lock:
                target = getattr(entry.obj, attr)
                if data.get("get"):
                    result = target
                else:
                    args = wire.decode(data.get("args") or [])
                    kwargs = wire.decode(data.get("kwargs") or {})
                    result = target(*args, **kwargs)
                    if attr == "__enter__" and result is entry.obj:
                        return respond({"result": None, "self": True})
                return respond({"result": wire.encode(result, _register)})
        except Exception as exc:  # the stand-in raises it as the same class
            if not isinstance(exc, StopIteration):
                logger.info("net %s failed: %s: %s", attr, type(exc).__name__, exc)
            return respond(*_error(exc))

    @app.route(CLOSE_PATH, methods=["POST"])
    def _hostlink_net_close():
        data = request.get_json(silent=True) or {}
        with _lock:
            entry = _objects.pop(data.get("handle"), None)
        if entry is not None:
            with entry.lock:
                _close(entry.obj)
        return respond({"closed": entry is not None})

    return app

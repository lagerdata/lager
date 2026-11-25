# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""A net whose driver runs in the host agent, for a script in the container.

``Net.get`` returns one of these where the driver it would have returned
needs hardware the container does not have. The real object is made in the
agent (``lager.host.net_rpc``); every method call and attribute read on the
stand-in is carried out there, so a script uses it exactly as it would use
the driver on a Linux Box. A failure is raised as the driver's own exception
class when that class can be imported here, else as ``RuntimeError``.

Used only when the container is started with ``LAGER_HOSTLINK=1``; a Linux
Box never imports this.

Not carried: a callable passed as an argument (a callback), and
``isinstance`` checks against the driver's class.
"""
from __future__ import annotations

import builtins
import importlib
import json

import requests

from . import wire

BASE_URL = "http://localhost:8080"
OPEN_URL = BASE_URL + "/v1/net/open"
CALL_URL = BASE_URL + "/v1/net/call"
OPEN_DRIVER_URL = BASE_URL + "/v1/net/open_driver"
CLOSE_URL = BASE_URL + "/v1/net/close"
# Opening may enumerate a bus; a call may be a long measurement.
_BASE_TIMEOUT_S = 120.0
_CLOSE_TIMEOUT_S = 10.0
_MAX_EXTRA_S = 24 * 3600.0
# A number passed under one of these names is how long the call itself takes.
_DURATION_NAMES = ("duration", "timeout", "seconds", "wait", "off_time", "duration_s",
                   "timeout_s")


def _error_class(payload):
    name, module = payload.get("type"), payload.get("module")
    if payload.get("stop"):
        return StopIteration
    cls = getattr(builtins, name or "", None)
    if module not in (None, "builtins") or cls is None:
        try:
            cls = getattr(importlib.import_module(module), name)
        except Exception:  # noqa: BLE001 - the class is not importable here
            cls = cls if module in (None, "builtins") else None
    if isinstance(cls, type) and issubclass(cls, BaseException):
        return cls
    return RuntimeError


def _raise(payload):
    cls = _error_class(payload)
    message = payload.get("error") or str(payload)
    try:
        error = cls(message)
        if str(error) != message:
            # The class builds its own text from its argument (a prefix, say);
            # the text that was built on the agent is the one to show.
            error = cls.__new__(cls)
            error.args = (message,)
            if str(error) != message:
                error = cls(message)
    except Exception:  # noqa: BLE001 - a class with another signature
        error = RuntimeError(f"{payload.get('type')}: {message}")
    raise error


def _post(url, body, timeout):
    resp = requests.post(url, data=json.dumps(body),
                         headers={"Content-Type": "application/json"}, timeout=timeout)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": resp.text, "type": "RuntimeError"}
    if not isinstance(payload, dict):
        payload = {"error": str(payload), "type": "RuntimeError"}
    if not resp.ok:
        _raise(payload)
    return payload


def _budget(args, kwargs):
    extra = [v for k, v in kwargs.items()
             if k in _DURATION_NAMES and isinstance(v, (int, float))
             and not isinstance(v, bool)]
    extra += [v for v in args if isinstance(v, (int, float)) and not isinstance(v, bool)]
    longest = max([v for v in extra if 0 < v <= _MAX_EXTRA_S], default=0.0)
    return _BASE_TIMEOUT_S + longest


class HostObject:
    """An object that lives in the host agent."""

    def __init__(self, description):
        state = self.__dict__
        state["_handle"] = description["__handle__"]
        state["_methods"] = frozenset(description.get("methods") or ())
        state["_values"] = frozenset(description.get("values") or ())
        state["_type"] = description.get("type") or "object"
        state["_repr"] = description.get("repr") or ""
        state["_closed"] = False

    # -- the wire ---------------------------------------------------------
    def _send(self, attr, args=(), kwargs=None, get=False):
        kwargs = kwargs or {}
        body = {"handle": self._handle, "attr": attr, "get": get,
                "args": wire.encode(list(args)), "kwargs": wire.encode(dict(kwargs))}
        payload = _post(CALL_URL, body, _budget(args, kwargs))
        if payload.get("self"):
            return self
        return wire.decode(payload.get("result"), HostObject)

    # -- the driver's surface --------------------------------------------
    def __getattr__(self, attr):
        if attr in self._methods:
            def method(*args, **kwargs):
                return self._send(attr, args, kwargs)
            method.__name__ = attr
            return method
        if attr in self._values:
            return self._send(attr, get=True)
        raise AttributeError(f"'{self._type}' object has no attribute '{attr}'")

    def __setattr__(self, attr, value):
        raise AttributeError(f"cannot set '{attr}': this {self._type} runs on the host "
                             "agent; use its methods")

    def __dir__(self):
        return sorted(set(self._methods) | set(self._values))

    def __repr__(self):
        return self._repr or f"<{self._type} on the host agent>"

    def __enter__(self):
        return self._send("__enter__") if "__enter__" in self._methods else self

    def __exit__(self, exc_type, exc, tb):
        if "__exit__" in self._methods:
            # The exception itself stays here; the driver is told one happened.
            return bool(self._send("__exit__", (None, None, None)))
        self.close()
        return False

    def _dunder(self, name, *args, **kwargs):
        # TypeError is what Python raises for an object without the protocol,
        # and what list() and len() expect from one.
        if name not in self._methods:
            raise TypeError(f"'{self._type}' object does not support {name}")
        return self._send(name, args, kwargs)

    def __iter__(self):
        return self

    def __next__(self):
        return self._dunder("__next__")

    def __len__(self):
        return self._dunder("__len__")

    def __getitem__(self, key):
        return self._dunder("__getitem__", key)

    def __contains__(self, item):
        return self._dunder("__contains__", item)

    def __call__(self, *args, **kwargs):
        return self._dunder("__call__", *args, **kwargs)

    def close(self):
        """Close the driver, as its own ``close`` would, and let go of it."""
        if self._closed:
            return
        self.__dict__["_closed"] = True
        try:
            _post(CLOSE_URL, {"handle": self._handle}, _CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - the agent drops it at the next script anyway
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown
            pass


def open_net(name, role):
    """The stand-in for the saved net ``name`` in ``role`` (its saved name)."""
    return HostObject(_post(OPEN_URL, {"name": name, "role": role}, _BASE_TIMEOUT_S))


def driver_maker(dispatcher):
    """A ``_make_driver`` for ``dispatcher`` whose driver lives in the agent.

    The module-level helpers (``lager.power.supply.voltage`` and the like)
    get their driver from their dispatcher's ``_make_driver``. This one asks
    the agent to run that same method of that same dispatcher class, and
    returns a stand-in for what it made.
    """
    cls = type(dispatcher)
    target = {"module": cls.__module__, "class": cls.__qualname__}

    def _make_driver(*args, **kwargs):
        body = dict(target, args=wire.encode(list(args)), kwargs=wire.encode(dict(kwargs)))
        return HostObject(_post(OPEN_DRIVER_URL, body, _BASE_TIMEOUT_S))

    return _make_driver

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Limit what the hardware service's ``/invoke`` reaches on the host.

On a Linux Box ``/invoke`` runs inside the container, so a caller that names
an unexpected module or function reaches nothing outside it. In the macOS host
agent the same route runs as the Mac's user, so a container that can name any
``lager.*`` module and any attribute of it could run code on the Mac (for
example, write a script where the J-Link wrapper is looked up). This guard,
installed only by the agent, admits the requests the Box's own callers make
and refuses the rest:

* ``device`` is a bare driver name: letters, digits and underscores, no dots.
* The module it resolves to is a driver in one of /invoke's driver packages,
  or a top-level ``lager.<name>_hs`` adapter. Other top-level modules
  (``lager.core``, ``lager.hardware_service``, ...) are refused.
* ``function`` is a public name: it does not start with an underscore.
* Where /invoke would use the module itself as the device (a module with no
  factory), the function must be one defined in that module, not a name it
  imported (``subprocess.run`` re-exported by a driver, say).
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import re

from flask import Flask, jsonify, request

from lager.hostlink import protocol

_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_PUBLIC = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _resolve(device_name: str):
    """The module path /invoke would import for ``device_name``, or None."""
    from lager.hardware_service import device_import_paths

    for path in device_import_paths(device_name):
        try:
            if importlib.util.find_spec(path) is not None:
                return path
        except (ImportError, ValueError):
            continue
    return None


def check(data) -> str | None:
    """None when the request may proceed, otherwise the reason it may not."""
    if not isinstance(data, dict):
        return None  # /invoke answers a malformed body itself
    device_name = data.get("device")
    function_name = data.get("function")
    if not device_name or not function_name:
        return None  # /invoke answers a missing field itself
    if not isinstance(device_name, str) or not _NAME.match(device_name):
        return f"device {device_name!r} is not a driver name"
    if not isinstance(function_name, str) or not _PUBLIC.match(function_name):
        return f"function {function_name!r} is not a public name"

    path = _resolve(device_name)
    if path is None:
        return None  # /invoke answers "Hardware module not found" itself
    if path == f"lager.{device_name}" and not device_name.endswith("_hs"):
        return f"{path} is not a hardware driver"

    module = importlib.import_module(path)
    if hasattr(module, "create_device") or hasattr(module, "create"):
        return None
    class_name = "".join(word.capitalize() for word in device_name.split("_"))
    if data.get("net_info") and hasattr(module, class_name):
        return None
    # /invoke would use the module itself: only its own functions.
    member = getattr(module, function_name, None)
    if member is None:
        return None  # /invoke answers "Function not found" itself
    if inspect.isfunction(member) and member.__module__ == module.__name__:
        return None
    return f"{function_name!r} is not a function of {path}"


def install(app: Flask) -> None:
    """Refuse ``/invoke`` requests that :func:`check` does not admit."""

    def _guard():
        if request.path != "/invoke":
            return None
        reason = check(request.get_json(silent=True))
        if reason is None:
            return None
        return jsonify(protocol.error_body(
            protocol.ERR_BAD_REQUEST,
            f"Refused by the host agent: {reason}.")), 403

    app.before_request_funcs.setdefault(None, []).append(_guard)

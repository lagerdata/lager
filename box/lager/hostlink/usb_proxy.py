# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""A USB hub controller, for a container whose hub is on the host.

A stand-in for the hub drivers in ``lager.automation.usb_hub`` (Acroname,
YKUSH, Plugable) with the same methods. Each call goes to the agent through
the relay on ``localhost:8080``, where the real driver runs
(``lager.host.usb_rpc``). Used only when the container is started with
``LAGER_HOSTLINK=1``; a Linux Box never imports this.

A driver error is raised here as the same class with the same message and
fields, so the Box's handlers report it exactly as they would on Linux.
"""
from __future__ import annotations

import json

import requests

RELAY_URL = "http://localhost:8080/v1/usb/call"
IDS_URL = "http://localhost:8080/v1/usb/ids"
INSTRUMENTS_URL = "http://localhost:8080/v1/instruments/list"
# Reading the bus is quick; a full instrument scan may handshake with an arm.
_IDS_TIMEOUT_S = 10.0
_SCAN_TIMEOUT_S = 60.0
# A power cycle holds the port off for up to ten seconds, then waits for the
# device to come back; the drivers bound their own operations below this.
_CALL_TIMEOUT_S = 120.0
# Fields a driver error may carry besides its message.
_ERROR_FIELDS = ("classification", "usb_context_healthy", "detail")


def _error_class(name):
    """The Box's exception class called ``name``, or RuntimeError."""
    from ..automation.usb_hub import usb_net

    if name == "KeyError":
        return KeyError
    cls = getattr(usb_net, name or "", None)
    if isinstance(cls, type) and issubclass(cls, Exception):
        return cls
    return RuntimeError


def _raise(payload):
    message = payload.get("error") or str(payload)
    error = _error_class(payload.get("type"))(message)
    for field in _ERROR_FIELDS:
        if field in payload:
            setattr(error, field, payload[field])
    raise error


class HostUSBNet:
    """Hub port operations carried out by the host agent."""

    def __init__(self, net_info):
        self._net_info = dict(net_info or {})

    def _call(self, method, *args, **kwargs):
        body = {"net_info": self._net_info, "method": method,
                "args": list(args), "kwargs": kwargs}
        resp = requests.post(RELAY_URL, data=json.dumps(body, default=str),
                             headers={"Content-Type": "application/json"},
                             timeout=_CALL_TIMEOUT_S)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text}
        if not resp.ok:
            _raise(payload if isinstance(payload, dict) else {"error": str(payload)})
        return payload.get("result")

    def enable(self, net_name, port):
        return self._call("enable", net_name, port)

    def disable(self, net_name, port):
        return self._call("disable", net_name, port)

    def toggle(self, net_name, port):
        return self._call("toggle", net_name, port)

    def state(self, net_name, port):
        return self._call("state", net_name, port)

    def cycle(self, net_name, port, off_time=None):
        return self._call("cycle", net_name, port, off_time)

    def recover(self, net_name, port):
        return self._call("recover", net_name, port)

    def states(self, ports, timeout=None):
        ports = list(ports)
        raw = self._call("states", ports, timeout=timeout) or {}
        # JSON turned the port keys into strings; give back the caller's own.
        return {port: raw[str(port)] for port in ports if str(port) in raw}

    def _lock_key(self):
        return self._call("_lock_key")


def controller_for(net_info):
    return HostUSBNet(net_info)


def enumerated_usb_ids():
    """``lager.util.self_restart.enumerated_usb_ids``, for the host's bus.

    ``{(vid, pid): [serial, ...]}``, or None when the host cannot say -- the
    same "unknown" a Linux Box gets when sysfs cannot be read.
    """
    try:
        resp = requests.get(IDS_URL, timeout=_IDS_TIMEOUT_S)
        devices = resp.json().get("devices") if resp.ok else None
        if devices is None:
            return None
        return {(int(vid), int(pid)): list(serials) for vid, pid, serials in devices}
    except Exception:  # noqa: BLE001 - unreachable or malformed is "unknown"
        return None


def list_instruments():
    """``usb_scanner.list_instruments``, scanned on the host."""
    resp = requests.get(INSTRUMENTS_URL, timeout=_SCAN_TIMEOUT_S)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": resp.text}
    if not resp.ok or not isinstance(payload.get("instruments"), list):
        raise RuntimeError(payload.get("error") or "the host agent returned no instrument list")
    return payload["instruments"]

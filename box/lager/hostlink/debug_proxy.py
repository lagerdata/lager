# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""The debug net, for a container whose probe is on the host.

A stand-in for ``lager.nets.debug_net.DebugNet`` with the same methods. Each
call goes to the agent through the relay on ``localhost:8080``, where the
real object runs (``lager.host.debug_rpc``). Used only when the container is
started with ``LAGER_HOSTLINK=1``; a Linux Box never imports this.
"""
from __future__ import annotations

import base64
import contextlib
import importlib
import json
import os
import select
import socket

import requests

from . import tcp_forward

RELAY_URL = "http://localhost:8080/v1/debug/call"
# Flash and erase run for as long as the target takes.
_CALL_TIMEOUT_S = 600.0


def _error(payload):
    """The error the real debug net raised, as the same class where possible.

    A script that catches ``lager.debug.JLinkAlreadyRunningError`` must catch
    it here too. Only classes from this package are looked up; anything else,
    and any class that cannot be built from a message, is a RuntimeError.
    """
    name, module = payload.get("type"), payload.get("module") or ""
    message = (payload.get("message") or payload.get("error")
               or name or "debug operation failed")
    if isinstance(name, str) and (module == "lager" or module.startswith("lager.")):
        try:
            cls = getattr(importlib.import_module(module), name, None)
            if isinstance(cls, type) and issubclass(cls, Exception):
                return cls(message)
        except Exception:  # noqa: BLE001 - fall through to the plain error
            pass
    return RuntimeError(message)


class _HostRtt:
    """An RTT session bridged from the host. Same surface as ``lager.debug.RTT``."""

    def __init__(self, owner, kwargs):
        self._owner = owner
        self._kwargs = kwargs
        self._socket = None
        self.channel = kwargs.get("channel", 0)

    def __enter__(self):
        port = self._owner._call("rtt_open", **self._kwargs)["port"]
        self._socket = socket.create_connection((tcp_forward.target_host(), port), timeout=10.0)
        self._socket.settimeout(None)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        return False

    def read_some(self, timeout=1.0):
        if self._socket is None:
            return None
        ready, _, _ = select.select([self._socket], [], [], timeout)
        if not ready:
            return None
        try:
            data = self._socket.recv(4096)
        except OSError:
            data = b""
        if not data:
            self.__exit__(None, None, None)
            return None
        return data

    def write(self, data):
        if self._socket is None:
            raise RuntimeError("RTT not connected")
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self._socket.send(data)


class HostDebugNet:
    """Debug operations carried out by the host agent."""

    def __init__(self, name, net_info):
        from ..nets.constants import NetType

        self.name = name
        self.type = NetType.Debug
        self._net_info = net_info or {}
        self._described = None

    def _call(self, method, *args, firmware=None, **kwargs):
        body = {"name": self.name, "net_info": self._net_info,
                "method": method, "args": list(args), "kwargs": kwargs}
        if firmware is not None:
            body["firmware"] = firmware
        resp = requests.post(RELAY_URL, data=json.dumps(body, default=str),
                             headers={"Content-Type": "application/json"},
                             timeout=_CALL_TIMEOUT_S)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text}
        if not resp.ok:
            raise _error(payload if isinstance(payload, dict) else {"error": str(payload)})
        if "result_b64" in payload:
            return base64.b64decode(payload["result_b64"])
        return payload.get("result")

    def __getattr__(self, name):
        # serial, backend, gdb_port, ...: whatever the real object reports.
        if name.startswith("_"):
            raise AttributeError(name)
        if self._described is None:
            self._described = self._call("describe")
        try:
            return self._described[name]
        except KeyError:
            raise AttributeError(name) from None

    def connect(self, *args, **kwargs):
        # A script or OpenOCD config named by its path in this container is
        # sent as its content: the agent accepts content only, because a path
        # there would name a file on the Mac.
        for key in SCRIPT_KWARGS:
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip() and os.path.isfile(value):
                with open(value, "rb") as fh:
                    kwargs[key] = base64.b64encode(fh.read()).decode()
        return self._call("connect", *args, **kwargs)

    def disconnect(self):
        return self._call("disconnect")

    def reset(self, halt=False):
        return self._call("reset", halt=halt)

    def halt(self):
        return self._call("halt")

    def flash(self, firmware_path, flash_address=None):
        with open(firmware_path, "rb") as fh:
            firmware = {"name": os.path.basename(str(firmware_path)),
                        "data": base64.b64encode(fh.read()).decode()}
        return self._call("flash", str(firmware_path), flash_address, firmware=firmware)

    def erase(self, start=None, length=None):
        return self._call("erase", start=start, length=length)

    def read_memory(self, address, length):
        return self._call("read_memory", address, length)

    def status(self):
        return self._call("status")

    def rtt(self, channel=0, search_addr=None, search_size=None, chunk_size=None):
        return _HostRtt(self, dict(channel=channel, search_addr=search_addr,
                                   search_size=search_size, chunk_size=chunk_size))

    def rtt_defmt(self, elf, channel=0, search_addr=None, search_size=None,
                  chunk_size=None, *, defmt_print_bin=None, read_timeout=0.5):
        from ..nets.debug_net import _DefmtRtt

        raw = self.rtt(channel=channel, search_addr=search_addr,
                       search_size=search_size, chunk_size=chunk_size)
        return _DefmtRtt(raw, elf, defmt_print_bin=defmt_print_bin,
                         read_timeout=read_timeout)

    @contextlib.contextmanager
    def session(self, *, speed=None, transport=None, connect=True,
                ignore_if_connected=True, disconnect_on_exit=True):
        if connect:
            self.connect(speed=speed, transport=transport,
                         ignore_if_connected=ignore_if_connected)
        try:
            yield self
        finally:
            if disconnect_on_exit:
                try:
                    self.disconnect()
                except Exception:  # noqa: BLE001 - teardown must not mask errors
                    pass


SCRIPT_KWARGS = ("script", "openocd_config", "jlink_script")


def make_debug(name, net_info=None):
    return HostDebugNet(name, net_info or {})

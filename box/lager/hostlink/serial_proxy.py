# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Serial ports, for a container whose ports are on the host.

On a Linux Box ``/dev/ttyUSB0`` is a device in the container. Where the
container has no USB, the port is on the host and the agent serves it
(``lager.host.serial_rpc``). This module is the container's side:

* ``HostSerial`` is a pySerial port that reaches ``/dev/ttyUSB0`` through the
  agent. It is pySerial's own RFC 2217 client, so reads, writes, line
  settings, modem lines and break behave as pySerial documents them.
* ``install_pyserial`` makes ``serial.Serial("/dev/ttyUSB0")`` return one, so
  the Box's code and a user's script open a port the way they do on Linux.
  Any port that exists in the container is opened natively, untouched.
* ``install_lookups`` replaces the sysfs lookups in ``serial_id`` with calls
  to the agent.

Used only when the container is started with ``LAGER_HOSTLINK=1``; a Linux
Box never imports this.
"""
from __future__ import annotations

import os
import re

import serial
import serial.rfc2217

RELAY = "http://localhost:8080"
LOOKUP_URL = RELAY + "/v1/serial/lookup"
OPEN_URL = RELAY + "/v1/serial/open"
CLOSE_URL = RELAY + "/v1/serial/close"
_TIMEOUT_S = 15.0

# The names a Linux Box gives USB serial ports. Only these are sent to the
# host; anything else is not a port the host could be holding.
_HOST_PORT = re.compile(r"^/dev/tty(USB|ACM)\d+$")
# Message a Linux Box's reconnect logic recognises as "the adapter left".
_GONE = "device disconnected (the host closed the port)"

_LOOKUPS = ("identity_for_tty", "resolve_identity", "resolve_tty", "list_cables")


def _post(url, body):
    import requests  # not at import: this module loads in every Python process

    resp = requests.post(url, json=body, timeout=_TIMEOUT_S)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"error": resp.text}
    if not isinstance(payload, dict):
        payload = {"error": str(payload)}
    return resp.ok, payload


def lookup(function, *args, **kwargs):
    """Run one of the Box's serial lookups on the host; None if it cannot."""
    try:
        ok, payload = _post(LOOKUP_URL, {"function": function,
                                         "args": list(args), "kwargs": kwargs})
    except Exception:  # noqa: BLE001 - an unreachable host finds nothing
        return None
    return payload.get("result") if ok else None


def find_device_by_serial(serial_number):
    return lookup("find_device_by_serial", serial_number)


def install_lookups(namespace) -> None:
    """Replace ``serial_id``'s sysfs lookups (``namespace`` is its globals)."""
    def proxy(function, empty):
        def call(*args, **kwargs):
            result = lookup(function, *args, **kwargs)
            return empty if result is None else result
        call.__name__ = function
        return call

    for function in _LOOKUPS:
        namespace[function] = proxy(function, [] if function == "list_cables" else None)


def is_host_port(port) -> bool:
    """True for a Linux USB serial name that is not a device in this container."""
    return (isinstance(port, str) and bool(_HOST_PORT.match(port))
            and not os.path.exists(port))


def _connect_host() -> str:
    from lager.hostlink import config

    return config.load_endpoint().connect_host


class HostSerial(serial.rfc2217.Serial):
    """``/dev/ttyUSB0`` on the host, as a pySerial port."""

    def from_url(self, url):
        """Ask the agent to open the port; where its server is listening."""
        ok, payload = _post(OPEN_URL, {"port": url})
        if not ok or "tcp_port" not in payload:
            number = payload.get("errno")
            message = payload.get("error") or "the host agent did not open the port"
            raise OSError(number, message) if number else OSError(message)
        self._network_timeout = 5
        return (_connect_host(), int(payload["tcp_port"]))

    def close(self):
        """Close, and have the host release the device before returning.

        Without the second step the host notices the closed connection a
        moment later, and reopening straight away would find the port held.
        """
        was_open = self.is_open
        super().close()
        if was_open:
            try:
                _post(CLOSE_URL, {"port": self.portstr})
            except Exception:  # noqa: BLE001 - the host releases it on its own
                pass

    def _gone(self, exc):
        return serial.SerialException(f"{_GONE}: {exc}")

    def read(self, size=1):
        try:
            return super().read(size)
        except serial.PortNotOpenError:
            raise
        except serial.SerialException as exc:
            raise self._gone(exc) from exc

    def write(self, data):
        try:
            return super().write(data)
        except serial.PortNotOpenError:
            raise
        except (serial.SerialException, OSError) as exc:
            raise self._gone(exc) from exc


_native_serial = serial.Serial


class Serial(_native_serial):
    """``serial.Serial`` that hands a host port to ``HostSerial``."""

    _lager_hostlink = True

    def __new__(cls, port=None, *args, **kwargs):
        if cls is Serial and is_host_port(port):
            return HostSerial(port, *args, **kwargs)
        return super().__new__(cls)


def install_pyserial() -> None:
    """Make ``serial.Serial`` open host ports through the agent."""
    # This file can be loaded twice (see site/sitecustomize.py); install once.
    if not getattr(serial.Serial, "_lager_hostlink", False):
        serial.Serial = Serial

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Serial ports for a Box container, served by the agent.

Two things a container without USB cannot do for itself:

* **Find a port.** Which tty belongs to which USB device is answered by the
  Box's own lookups (``lager.devices.serial_id`` and the UART bridge), run
  here against the agent's view of the bus.
* **Open a port.** The agent opens the real device and serves it to one
  client over RFC 2217, the standard "serial port over TCP" protocol that
  pySerial speaks: data, line settings, modem lines and break all cross.
  The container's stand-in (``lager.hostlink.serial_proxy``) connects to it.

A port is held exclusively, as the Box holds it on Linux: a second open fails
with the error a locked port gives there.
"""
from __future__ import annotations

import errno
import logging
import socket
import sys
import threading
import time

from flask import Flask, jsonify, request

from lager.hostlink import config, protocol

logger = logging.getLogger(__name__)

LOOKUP_PATH = "/v1/serial/lookup"
OPEN_PATH = "/v1/serial/open"
CLOSE_PATH = "/v1/serial/close"

# What a container may ask for. Everything else is refused, not looked up.
_LOOKUPS = frozenset({
    "identity_for_tty", "resolve_identity", "resolve_tty", "list_cables",
    "find_device_by_serial", "port_exists", "resolve_link",
})
# How long an opened port waits for the container to connect.
ACCEPT_WAIT_S = 10.0
_READ_POLL_S = 0.05
# A client that exits without closing is noticed a moment later; an open that
# arrives in that moment waits this long before calling the port busy.
BUSY_GRACE_S = 0.5
# Linux's numbers, whatever this host's are: the container is Linux.
_EAGAIN, _ENOENT, _EIO = 11, 2, 5
_MODEM_POLL_S = 0.5

_sessions_lock = threading.Lock()
_sessions = {}  # Linux tty path -> _Session


def _refresh() -> None:
    if sys.platform == "darwin":
        from . import darwin_scan

        darwin_scan.refresh()


def _host_device(tty: str):
    """The device to open for ``tty``, or None if it is not attached."""
    if sys.platform == "darwin":
        from . import darwin_serial

        return darwin_serial.NAMES.host_device(tty)
    return tty


def _session_key(tty: str) -> str:
    """One key per port, whichever name opens it: a by-id or by-path link and
    ``/dev/ttyUSB0`` for the same device are one port, held once."""
    if sys.platform == "darwin":
        from . import darwin_serial

        return darwin_serial.NAMES.linux_path(tty) or tty
    return tty


def _missing(tty: str) -> str:
    if sys.platform == "darwin":
        from . import darwin_serial

        return darwin_serial.missing_message(tty)
    return f"No such file or directory: {tty!r}"


def _lookup(function, args, kwargs):
    from lager.devices import serial_id
    from lager.protocols.uart.uart_bridge import UARTBridge

    if function == "find_device_by_serial":
        return UARTBridge._find_device_by_serial(None, *args, **kwargs)
    return getattr(serial_id, function)(*args, **kwargs)


class _Connection:
    """What pySerial's RFC 2217 server writes its replies to."""

    def __init__(self, sock):
        self._sock = sock
        self._lock = threading.Lock()

    def write(self, data) -> None:
        with self._lock:
            self._sock.sendall(data)


class _Session(threading.Thread):
    """One opened port, serving one client until either side goes away."""

    def __init__(self, tty, device, listener):
        super().__init__(name=f"serial-{tty}", daemon=True)
        self.tty = tty
        self._device = device
        self._listener = listener
        self._alive = True

    def run(self) -> None:
        sock = None
        try:
            self._listener.settimeout(ACCEPT_WAIT_S)
            sock, _ = self._listener.accept()
            self._listener.close()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._serve(sock)
        except socket.timeout:
            logger.info("%s: opened but never connected to; closing", self.tty)
        except Exception as exc:  # noqa: BLE001 - either side going away ends it
            logger.info("%s: session ended: %s", self.tty, exc)
        finally:
            self._alive = False
            for closable in (sock, self._listener, self._device):
                try:
                    if closable is not None:
                        closable.close()
                except Exception:  # noqa: BLE001
                    pass
            with _sessions_lock:
                if _sessions.get(self.tty) is self:
                    del _sessions[self.tty]

    def _serve(self, sock) -> None:
        import serial.rfc2217

        manager = serial.rfc2217.PortManager(self._device, _Connection(sock))
        pump = threading.Thread(target=self._device_to_client, args=(sock, manager),
                                name=f"serial-{self.tty}-rx", daemon=True)
        pump.start()
        sock.settimeout(_MODEM_POLL_S)
        while self._alive:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                manager.check_modem_lines()
                continue
            if not data:
                break
            self._device.write(b"".join(manager.filter(data)))

    def _device_to_client(self, sock, manager) -> None:
        try:
            while self._alive:
                data = self._device.read(self._device.in_waiting or 1)
                if data:
                    sock.sendall(b"".join(manager.escape(data)))
        except Exception as exc:  # noqa: BLE001 - unplugged, or the client left
            logger.info("%s: device side ended: %s", self.tty, exc)
        finally:
            self._alive = False
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _busy(tty):
    # The text and number a Linux Box reports for a port another opener holds.
    return OSError(_EAGAIN, f"Could not exclusively lock port {tty}: "
                                 f"[Errno 11] Resource temporarily unavailable")


def _open_device(path):
    import serial

    return serial.Serial(path, exclusive=True, timeout=_READ_POLL_S)


def open_port(tty: str, bind: str = config.DEFAULT_BIND) -> int:
    """Open ``tty`` and return the TCP port its RFC 2217 server listens on."""
    import serial

    _refresh()
    device_path = _host_device(tty)
    if not device_path:
        raise FileNotFoundError(_ENOENT, _missing(tty))
    key = _session_key(tty)
    deadline = time.monotonic() + BUSY_GRACE_S
    while True:
        with _sessions_lock:
            held = key in _sessions
        if not held or time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    with _sessions_lock:
        if key in _sessions:
            raise _busy(tty)
        try:
            device = _open_device(device_path)
        except serial.SerialException as exc:
            if "lock" in str(exc).lower() or getattr(exc, "errno", None) in (
                    errno.EAGAIN, errno.EBUSY):
                raise _busy(tty) from exc
            raise OSError(_EIO, str(exc).replace(device_path, tty)) from exc
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind((bind, 0))
            listener.listen(1)
        except OSError:
            device.close()
            listener.close()
            raise
        session = _Session(key, device, listener)
        _sessions[key] = session
        session.start()
        return listener.getsockname()[1]


def close_port(tty: str, wait_s: float = 2.0) -> bool:
    """End the session holding ``tty`` and wait for the device to be released."""
    with _sessions_lock:
        session = _sessions.get(_session_key(tty))
    if session is None:
        return False
    _end([session], wait_s)
    return True


def close_all(wait_s: float = 2.0) -> None:
    """End every session (for tests and shutdown)."""
    with _sessions_lock:
        sessions = list(_sessions.values())
    _end(sessions, wait_s)


def _end(sessions, wait_s: float) -> None:
    for session in sessions:
        session._alive = False
        for closable in (session._listener, session._device):
            try:
                closable.close()
            except Exception:  # noqa: BLE001
                pass
    deadline = time.monotonic() + wait_s
    for session in sessions:
        session.join(max(0.0, deadline - time.monotonic()))


def _failure(exc):
    body = {"error": str(exc), "type": type(exc).__name__}
    if isinstance(exc, OSError) and exc.errno is not None:
        body["errno"] = exc.errno
        body["error"] = exc.strerror or str(exc)
    return jsonify(body), 500


def install(app: Flask, bind: str = config.DEFAULT_BIND) -> Flask:
    """Add the serial routes. The agent's token guard already covers them."""

    @app.route(LOOKUP_PATH, methods=["POST"])
    def _hostlink_serial_lookup():
        data = request.get_json(silent=True)
        function = data.get("function") if isinstance(data, dict) else None
        if function not in _LOOKUPS:
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, f"Unknown serial lookup {function!r}.")), 400
        try:
            _refresh()
            result = _lookup(function, data.get("args") or [], data.get("kwargs") or {})
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            logger.exception("serial lookup %s failed", function)
            return _failure(exc)
        return jsonify({"result": result})

    @app.route(OPEN_PATH, methods=["POST"])
    def _hostlink_serial_open():
        data = request.get_json(silent=True)
        tty = data.get("port") if isinstance(data, dict) else None
        if not isinstance(tty, str) or not tty:
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, "Body must name a port.")), 400
        try:
            return jsonify({"tcp_port": open_port(tty, bind)})
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            logger.info("open %s failed: %s", tty, exc)
            return _failure(exc)

    @app.route(CLOSE_PATH, methods=["POST"])
    def _hostlink_serial_close():
        data = request.get_json(silent=True)
        tty = data.get("port") if isinstance(data, dict) else None
        if not isinstance(tty, str) or not tty:
            return jsonify(protocol.error_body(
                protocol.ERR_BAD_REQUEST, "Body must name a port.")), 400
        return jsonify({"closed": close_port(tty)})

    return app

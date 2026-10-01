# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE GATT sessions for on-box scripts (`lager python`).

A script reaches the box's own session service (the ``/ble`` Socket.IO
namespace and ``/ble/command`` on ``localhost:9000``) exactly as the CLI and
lager-net do from outside, so it gets the same behavior: a held connection,
notifications buffered in order, the negotiated MTU, chunked writes, and the
box's adapter sharing ("in use" errors, release, cleanup when the script
ends). Protocol notes: ``docs/reference/ble-sessions.md``.

    from lager.ble import Session

    with Session("AA:BB:CC:DD:EE:01") as s:
        s.subscribe(NOTIFY_UUID)
        s.write(WRITE_UUID, request, chunk=True)
        reply = s.recv(timeout=2.0).data

The older ``Central``/``Client`` classes talk to bleak directly and do not
share the adapter with sessions; prefer ``Session``.
"""
import collections
import os
import queue
import threading
import time

BOX_URL = os.environ.get("LAGER_BOX_LOCAL_URL", "http://127.0.0.1:9000")
NAMESPACE = "/ble"

# Client-side waits, matching the CLI's client (the box bounds each
# operation itself and reports a stall as `timeout`).
DEFAULT_OP_TIMEOUT = 30.0
OPEN_OVERHEAD = 25.0
CLOSE_TIMEOUT = 8.0
CLOSED_WAIT = 6.0
BOX_CHUNK_BOUND = 10.0
ATT_MAX_VALUE = 512

Notification = collections.namedtuple("Notification", "seq char handle timestamp data")
Notification.__doc__ = """One notification: the box's counter `seq` (from 1),
the characteristic UUID and handle, box time `timestamp` (Unix seconds) and
the value `data` (bytes)."""


class SessionError(Exception):
    """A failed operation; ``code`` is the box's error code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class SessionClosed(SessionError):
    """The session ended; ``code`` is the reason (``disconnected``, ...)."""


def _post_command(action, params=None, timeout=30.0):
    import requests

    resp = requests.post(BOX_URL + "/ble/command",
                         json={"action": action, "params": params or {}},
                         timeout=timeout)
    body = resp.json()
    if not body.get("success"):
        raise SessionError(body.get("code") or "ble_error",
                           body.get("error") or "HTTP %d" % resp.status_code)
    return body.get("value") or {}


def adapter():
    """Whether this box has a powered Bluetooth adapter.

    Returns ``{"available": bool, "adapters": [...], "reason": str or None}``.
    Answers even while a session holds the adapter; check it first and skip
    BLE tests on a box without a radio.
    """
    return _post_command("adapter", timeout=30.0)


def scan(timeout=5.0, name_contains=None):
    """Scan through the box's BLE service.

    Returns dicts with ``name``, ``address``, ``address_type`` (``public`` or
    ``random``), ``random_type`` (``static``, ``resolvable``,
    ``non-resolvable`` or None), ``rssi`` and ``uuids``. Raises
    SessionError with code ``adapter_busy`` while a session is open.
    """
    params = {"timeout": timeout}
    if name_contains:
        params["name_contains"] = name_contains
    return _post_command("scan", params, timeout=timeout + 30.0).get("devices", [])


class Session:
    """A held GATT connection through the box's session service.

    Opens on construction. Close it with :meth:`close`, a ``with`` block, or
    by ending the script: the box closes a session whose client is gone.
    """

    def __init__(self, address, *, connect_timeout=10.0, idle_timeout=300.0,
                 holder=None, op_timeout=DEFAULT_OP_TIMEOUT, sio=None):
        import socketio

        self.op_timeout = op_timeout
        self.sio = sio or socketio.Client(reconnection=False)
        self._notifications = queue.Queue()
        self._closed = None
        self._connected = False
        self._opened = False
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._results = {}
        self._cv = threading.Condition()
        self._order_lock = threading.Lock()
        self._held = {}
        self._next_e = 1
        self.sio.on("ble_result", self._in_order(self._on_result), namespace=NAMESPACE)
        self.sio.on("ble_notify", self._in_order(self._on_notify), namespace=NAMESPACE)
        self.sio.on("ble_closed", self._in_order(self._on_closed), namespace=NAMESPACE)
        self.sio.on("disconnect", self._on_disconnect, namespace=NAMESPACE)

        self.sio.connect(BOX_URL, namespaces=[NAMESPACE], wait_timeout=10)
        self._connected = True
        if holder is None:
            holder = "lager python %s" % os.environ.get("LAGER_PROCESS_ID", os.getpid())
        try:
            self._info = self._call("ble_open", timeout=connect_timeout + OPEN_OVERHEAD,
                                    address=address, connect_timeout=connect_timeout,
                                    idle_timeout=idle_timeout, holder=holder)
        except BaseException:
            self._disconnect()
            raise
        self._opened = True

    # -- box events (python-socketio runs each on its own thread) ----------

    def _in_order(self, handler):
        """Run box events in the order the box sent them (its counter ``e``)."""
        def receive(data):
            e = data.get("e") if isinstance(data, dict) else None
            if isinstance(e, bool) or not isinstance(e, int):
                handler(data)
                return
            data = {k: v for k, v in data.items() if k != "e"}
            with self._order_lock:
                self._held[e] = (handler, data)
                while self._next_e in self._held:
                    run, payload = self._held.pop(self._next_e)
                    self._next_e += 1
                    run(payload)
        return receive

    def _on_result(self, data):
        with self._cv:
            self._results[data.get("seq")] = data
            self._cv.notify_all()

    def _on_notify(self, data):
        for item in (data or {}).get("items") or []:
            try:
                value = bytes.fromhex(item.get("data") or "")
            except ValueError:
                continue
            self._notifications.put(Notification(item.get("n"), item.get("char"),
                                                 item.get("handle"), item.get("ts"), value))

    def _on_closed(self, data):
        with self._cv:
            self._closed = data or {"reason": "unknown", "message": ""}
            self._opened = False
            self._cv.notify_all()

    def _on_disconnect(self, *_args):
        with self._cv:
            self._connected = False
            if self._closed is None:
                self._closed = {"reason": "connection_lost",
                                "message": "The connection to the box was lost"}
            self._opened = False
            self._cv.notify_all()

    # -- requests ----------------------------------------------------------

    def _call(self, event, timeout=None, **fields):
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
            self.sio.emit(event, dict(fields, seq=seq), namespace=NAMESPACE)
        wait = timeout or self.op_timeout
        deadline = time.monotonic() + wait
        with self._cv:
            while seq not in self._results:
                if not self._connected:
                    raise self._closed_error()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SessionError("timeout", "The box did not answer %s within %.0fs"
                                       % (event, wait))
                self._cv.wait(min(remaining, 0.5))
            result = self._results.pop(seq)
        if not result.get("ok"):
            raise SessionError(result.get("code") or "ble_error",
                               result.get("message") or "BLE session error")
        return result.get("value") or {}

    def _closed_error(self):
        closed = self._closed or {"reason": "connection_lost", "message": ""}
        return SessionClosed(closed.get("reason") or "unknown",
                             closed.get("message") or "The BLE session ended")

    @staticmethod
    def _target(char, handle):
        return {"handle": handle} if handle is not None else {"char": char}

    # -- properties --------------------------------------------------------

    @property
    def address(self):
        return self._info.get("address")

    @property
    def mtu(self):
        """The negotiated ATT MTU (23 when the box could not read it)."""
        return int(self._info.get("mtu") or 23)

    @property
    def mtu_is_measured(self):
        return self._info.get("mtu_source") == "bluez"

    @property
    def max_write_len(self):
        """The largest single write: MTU - 3, at most 512."""
        return min(max(self.mtu - 3, 1), ATT_MAX_VALUE)

    @property
    def services(self):
        """The GATT table, each characteristic with its handle."""
        return self._info.get("services", [])

    @property
    def close_reason(self):
        """Why the session ended, or None while it is open."""
        return (self._closed or {}).get("reason")

    # -- operations --------------------------------------------------------

    def subscribe(self, char=None, handle=None):
        """Turn on notifications (or indications) before writing."""
        return self._call("ble_subscribe", **self._target(char, handle))

    def unsubscribe(self, char=None, handle=None):
        return self._call("ble_unsubscribe", **self._target(char, handle))

    def write(self, char, data, *, response=True, chunk=False, chunk_size=None, handle=None):
        """Write raw bytes.

        ``chunk`` splits them into writes of ``max_write_len`` bytes, sent in
        order; ``chunk_size`` caps the pieces lower (and turns chunking on).
        Without chunking a write is at most 512 bytes.
        """
        fields = self._target(char, handle)
        if chunk_size is not None:
            fields["chunk_size"] = chunk_size
            chunk = True
        timeout = self.op_timeout
        if chunk:
            size = min(self.max_write_len, chunk_size or ATT_MAX_VALUE)
            chunks = max(-(-len(data) // max(size, 1)), 1)
            timeout = max(timeout, chunks * BOX_CHUNK_BOUND + 5.0)
        return self._call("ble_write", timeout=timeout, data=bytes(data).hex(),
                          response=response, chunk=chunk, **fields)

    def read(self, char=None, handle=None):
        value = self._call("ble_read", **self._target(char, handle))
        return bytes.fromhex(value.get("data") or "")

    def info(self):
        """Refresh and return the MTU and GATT table."""
        self._info = self._call("ble_info")
        return self._info

    def ping(self):
        """Reset the idle timer (notifications do not count as activity)."""
        return self._call("ble_ping")

    def recv(self, timeout=None):
        """The next notification, waiting up to ``timeout`` seconds.

        Raises TimeoutError when none arrives. After the session ends, every
        notification received before the end is still returned first; then
        this raises SessionClosed.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                return self._notifications.get(timeout=0.1)
            except queue.Empty:
                pass
            if self._closed is not None and self._notifications.empty():
                raise self._closed_error()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("No BLE notification within %ss" % timeout)

    def try_recv(self):
        """The next notification if one already arrived, else None."""
        try:
            return self._notifications.get_nowait()
        except queue.Empty:
            if self._closed is not None:
                raise self._closed_error()
            return None

    def close(self):
        """End the session (best effort; the box also ends it on disconnect)."""
        if self._opened and self._connected:
            try:
                self._call("ble_close", timeout=CLOSE_TIMEOUT)
            except SessionError:
                pass
            deadline = time.monotonic() + CLOSED_WAIT
            with self._cv:
                while self._closed is None and time.monotonic() < deadline:
                    self._cv.wait(0.1)
        self._opened = False
        self._disconnect()

    def _disconnect(self):
        try:
            self.sio.disconnect()
        except Exception:  # noqa: BLE001 — already on the way out
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

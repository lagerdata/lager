# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Socket.IO client for BLE GATT sessions (the box's ``/ble`` namespace).

A session holds one GATT connection open on the box so a caller can
subscribe, write, read and receive notifications across many operations.
Bytes only: framing and any application protocol are the caller's.
Protocol notes: ``docs/reference/ble-sessions.md``.

Every request carries ``seq`` (1, 2, 3, ... per connection); the box answers
each with a ``ble_result`` carrying the same ``seq``. Notifications arrive as
``ble_notify`` batches and are queued here until read, so nothing that
arrives between reads is lost. ``ble_closed`` is the last event of a session.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Optional

import socketio

NAMESPACE = '/ble'

# Client-side wait for one operation's ble_result. Above the box's own
# per-operation bound (10s per write chunk), which reports a wedged call
# itself.
DEFAULT_OP_TIMEOUT = 30.0
# Added to the connect timeout while waiting for ble_open: the box adds 20s
# for connect + service discovery, plus the gateway round trip.
OPEN_OVERHEAD = 25.0
# How long the close request may take before we drop the socket anyway.
CLOSE_TIMEOUT = 8.0
# How long to wait for ble_closed after the close result. The box sends it
# once BlueZ has disconnected, which it bounds at 5s.
CLOSED_WAIT = 6.0
# The ATT limit on one attribute value, and so on one write (see the box).
ATT_MAX_VALUE = 512
# The box bounds each ATT write of a chunked write separately, so a chunked
# write that keeps making progress may take this long per chunk.
BOX_CHUNK_BOUND = 10.0


class BLESessionError(Exception):
    """A failed session operation, with the box's error ``code``."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class BLESessionClosed(BLESessionError):
    """The session ended (``code`` is the box's close reason)."""


class BLESessionClient:
    """One ``/ble`` Socket.IO connection and at most one open session.

    Usage::

        client = BLESessionClient('http://<box>:9000')
        client.connect()
        info = client.open('AA:BB:CC:DD:EE:01')
        client.subscribe(NOTIFY_UUID)
        client.write(WRITE_UUID, payload, chunk=True)
        note = client.get_notification(timeout=2.0)
        client.close()
        client.disconnect()
    """

    def __init__(self, box_url: str, *, op_timeout: float = DEFAULT_OP_TIMEOUT,
                 sio: Optional[socketio.Client] = None):
        self.box_url = box_url
        self.op_timeout = op_timeout
        self.sio = sio or socketio.Client(reconnection=False)
        self.notifications: queue.Queue = queue.Queue()
        # The ble_closed payload once the session ended, else None.
        self.closed: Optional[dict] = None
        self.connected = False
        self.opened = False
        # Negotiated ATT MTU of the open session (23 until open() returns).
        self.mtu = 23
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._results: dict[int, dict] = {}
        self._cv = threading.Condition()
        # Reorder buffer for box events (see _in_order).
        self._order_lock = threading.Lock()
        self._held: dict[int, tuple] = {}
        self._next_e = 1
        self._register_handlers()

    # -- event handlers ---------------------------------------------------

    def _register_handlers(self):
        self.sio.on('ble_result', self._in_order(self._on_result), namespace=NAMESPACE)
        self.sio.on('ble_notify', self._in_order(self._on_notify), namespace=NAMESPACE)
        self.sio.on('ble_closed', self._in_order(self._on_closed), namespace=NAMESPACE)
        self.sio.on('disconnect', self._on_disconnect, namespace=NAMESPACE)

    def _in_order(self, handler):
        """Run box events in the order the box sent them.

        python-engineio's client hands every incoming message to a new
        thread, so two events that arrive back to back can be handled in
        either order: a burst of notifications came out with a 64-item batch
        behind the next one. The box stamps each event with ``e`` (1, 2, 3,
        ... per connection); events are held until all earlier ones ran. A
        box that sends no ``e`` gets its events handled as they come.
        """
        def receive(data):
            e = data.get('e') if isinstance(data, dict) else None
            if isinstance(e, bool) or not isinstance(e, int):
                handler(data)
                return
            data = {k: v for k, v in data.items() if k != 'e'}
            with self._order_lock:
                self._held[e] = (handler, data)
                while self._next_e in self._held:
                    run, payload = self._held.pop(self._next_e)
                    self._next_e += 1
                    run(payload)
        return receive

    def _on_result(self, data):
        with self._cv:
            self._results[data.get('seq')] = data
            self._cv.notify_all()

    def _on_notify(self, data):
        for item in (data or {}).get('items') or []:
            try:
                item = dict(item, data=bytes.fromhex(item.get('data') or ''))
            except ValueError:
                continue
            self.notifications.put(item)

    def _on_closed(self, data):
        with self._cv:
            self.closed = data or {'reason': 'unknown', 'message': ''}
            self.opened = False
            self._cv.notify_all()

    def _on_disconnect(self, *_args):
        with self._cv:
            self.connected = False
            if self.opened and self.closed is None:
                self.closed = {'reason': 'connection_lost',
                               'message': 'The connection to the box was lost'}
            self.opened = False
            self._cv.notify_all()

    # -- connection -------------------------------------------------------

    def connect(self, headers: Optional[dict] = None, wait_timeout: float = 10):
        """Open the Socket.IO connection (raises on failure)."""
        self.sio.connect(self.box_url, namespaces=[NAMESPACE],
                         wait_timeout=wait_timeout, headers=headers or {})
        self.connected = True

    def disconnect(self):
        """Close the socket. The box ends any open session with it."""
        try:
            self.sio.disconnect()
        except Exception:  # noqa: BLE001 — already on the way out
            pass
        self.connected = False

    # -- operations -------------------------------------------------------

    def _call(self, event: str, timeout: Optional[float] = None, **fields) -> dict:
        """Send one request and wait for its ble_result; return its value."""
        with self._seq_lock:
            self._seq += 1
            seq = self._seq
            self.sio.emit(event, dict(fields, seq=seq), namespace=NAMESPACE)
        deadline = time.monotonic() + (timeout or self.op_timeout)
        with self._cv:
            while seq not in self._results:
                if not self.connected:
                    raise BLESessionClosed('connection_lost',
                                           'The connection to the box was lost')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BLESessionError(
                        'timeout', 'The box did not answer %s within %.0fs'
                        % (event, timeout or self.op_timeout))
                self._cv.wait(min(remaining, 0.5))
            result = self._results.pop(seq)
        if not result.get('ok'):
            raise BLESessionError(result.get('code') or 'ble_error',
                                  result.get('message') or 'BLE session error')
        return result.get('value') or {}

    @staticmethod
    def _target(char: Optional[str], handle: Optional[int]) -> dict:
        if handle is not None:
            return {'handle': handle}
        return {'char': char}

    def open(self, address: str, *, connect_timeout: float = 10.0,
             idle_timeout: float = 300.0, holder: Optional[str] = None) -> dict:
        """Connect to *address*; returns ``{address, mtu, mtu_source, services}``."""
        fields = {'address': address, 'connect_timeout': connect_timeout,
                  'idle_timeout': idle_timeout}
        if holder:
            fields['holder'] = holder
        with self._cv:
            self.closed = None
        value = self._call('ble_open', timeout=connect_timeout + OPEN_OVERHEAD, **fields)
        self.opened = True
        self.mtu = int(value.get('mtu') or 23)
        return value

    def subscribe(self, char: Optional[str] = None, handle: Optional[int] = None) -> dict:
        return self._call('ble_subscribe', **self._target(char, handle))

    def unsubscribe(self, char: Optional[str] = None, handle: Optional[int] = None) -> dict:
        return self._call('ble_unsubscribe', **self._target(char, handle))

    def write(self, char: Optional[str], data: bytes, *, response: bool = True,
              chunk: bool = False, handle: Optional[int] = None,
              chunk_size: Optional[int] = None) -> dict:
        """Write raw bytes; ``chunk`` splits them into writes of
        ``min(mtu - 3, 512)`` bytes, or of ``chunk_size`` bytes when that is
        smaller (which also turns chunking on)."""
        fields = dict(self._target(char, handle))
        if chunk_size is not None:
            fields['chunk_size'] = chunk_size
            chunk = True
        return self._call('ble_write',
                          timeout=self.write_timeout(len(data), chunk, chunk_size),
                          data=bytes(data).hex(), response=response, chunk=chunk, **fields)

    def write_timeout(self, length: int, chunk: bool,
                      chunk_size: Optional[int] = None) -> float:
        """Client-side wait for a write: longer for a many-chunk write.

        A stalled chunk still fails fast: the box reports ``timeout`` after
        its per-chunk bound.
        """
        if not chunk:
            return self.op_timeout
        size = min(max(self.mtu - 3, 1), chunk_size or ATT_MAX_VALUE)
        chunks = max(-(-length // size), 1)
        return max(self.op_timeout, chunks * BOX_CHUNK_BOUND + 5.0)

    def read(self, char: Optional[str] = None, handle: Optional[int] = None) -> bytes:
        value = self._call('ble_read', **self._target(char, handle))
        return bytes.fromhex(value.get('data') or '')

    def info(self) -> dict:
        return self._call('ble_info')

    def ping(self) -> dict:
        return self._call('ble_ping')

    def close(self) -> None:
        """End the session (best effort: the box also ends it on disconnect)."""
        if not self.opened or not self.connected:
            return
        try:
            self._call('ble_close', timeout=CLOSE_TIMEOUT)
        except BLESessionError:
            pass
        # Wait briefly for ble_closed so notifications before it are queued.
        deadline = time.monotonic() + CLOSED_WAIT
        with self._cv:
            while self.closed is None and time.monotonic() < deadline:
                self._cv.wait(0.1)
        self.opened = False

    def get_notification(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Next queued notification, or None after *timeout* (None: don't wait).

        Items are ``{n, char, handle, ts, data: bytes}``.
        """
        try:
            if timeout is None:
                return self.notifications.get_nowait()
            return self.notifications.get(timeout=timeout)
        except queue.Empty:
            return None

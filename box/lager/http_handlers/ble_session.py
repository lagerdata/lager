# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE GATT sessions over the ``/ble`` Socket.IO namespace.

``POST /ble/command`` connects, enumerates and disconnects inside one call.
This module holds a GATT connection open across many operations so a remote
client (``lager ble session``, lager-net's ``BleSession``) can subscribe,
write, read and receive notifications without SSH. It moves bytes only: no
framing, reassembly or application protocol. Design notes:
``docs/reference/ble-sessions.md``.

Client -> box events carry ``seq`` (1, 2, 3, ... per Socket.IO connection):
``ble_open``, ``ble_subscribe``, ``ble_unsubscribe``, ``ble_write``,
``ble_read``, ``ble_info``, ``ble_ping``, ``ble_close``.
Box -> client: ``ble_result`` (one per seq), ``ble_notify`` (a batch of
notifications) and ``ble_closed`` (always the last event of a session).

Threading. The server runs Flask-SocketIO in threading mode, which dispatches
every incoming event on its own thread, so two events one client sent
back-to-back can reach us out of order. Each connection therefore gets a
``_Channel`` that runs operations strictly in ``seq`` order on the shared
bleak event loop (``ble.get_bleak_loop()``), holding early arrivals until the
gap fills. Everything that touches bleak runs on that loop.

Everything the box sends goes through one outbox thread, in the order it was
queued, so a client sees results and notifications in the order the box
produced them. Consecutive notifications are coalesced into one ``ble_notify``
without any batching timer.

Adapter. There is one Bluetooth adapter. An open session holds
``bt_adapter_lock`` for its whole lifetime and registers itself as the adapter
holder, so ``/ble/command`` and ``/blufi/command`` fail fast with 409 instead
of queueing behind it (see ``ble.acquire_adapter``).
"""
import asyncio
import collections
import logging
import re
import threading
import time

from flask import Flask, jsonify, request

from lager.http_handlers.ble import (
    _BLE_ADDRESS_RE,
    adapter_busy_message,
    adapter_holder_info,
    ble_target,
    bluez_unavailable_hint,
    bt_adapter_lock,
    clear_adapter_holder,
    get_bleak_loop,
    run_bleak,
    set_adapter_holder,
)

logger = logging.getLogger(__name__)

NAMESPACE = '/ble'

DEFAULT_CONNECT_TIMEOUT = 10.0
CONNECT_TIMEOUT_RANGE = (1.0, 120.0)
# bleak's own `timeout` only bounds its find-by-address scan; connect plus
# service discovery gets this much more on top (same widening as run_bleak's
# callers in ble.py).
CONNECT_OVERHEAD = 20.0
DEFAULT_IDLE_TIMEOUT = 300.0
IDLE_TIMEOUT_RANGE = (5.0, 3600.0)
# Box-side bound on each subscribe/read/write (per chunk) operation. A wedged
# BlueZ call returns `timeout` and closes the session: after it the link state
# is unknown.
OP_TIMEOUT = 10.0
# How long a missing seq may hold up later ones before the session fails with
# protocol_error. Only our own clients speak this protocol, so a gap is a bug.
SEQ_GAP_TIMEOUT = 2.0
# Largest payload one ble_write accepts (far above any GATT message, far
# below the Socket.IO message cap).
MAX_WRITE_BYTES = 64 * 1024
# No attribute value is longer than this (Core spec, ATT), so no single ATT
# write, long writes included, can carry more. BlueZ refuses a longer one
# with "Invalid Length"; a peripheral silently drops a longer command.
ATT_MAX_VALUE = 512
# Notifications queued for a client that is not draining them. Overflow ends
# the session: a silent gap would corrupt whatever protocol the caller layers
# on this stream.
NOTIFY_QUEUE_MAX_BYTES = 4 * 1024 * 1024
NOTIFY_QUEUE_MAX_ITEMS = 16384
# Idle / client-gone check period.
WATCHDOG_INTERVAL = 1.0
# Bound on the disconnect during teardown.
DISCONNECT_TIMEOUT = 5.0

# Bluetooth base UUID, for expanding 16/32-bit short UUIDs.
_BASE_UUID_SUFFIX = '-0000-1000-8000-00805f9b34fb'
_SHORT_UUID_RE = re.compile(r'^(0x)?([0-9a-f]{4}|[0-9a-f]{8})$')

# The SocketIO instance the namespace is registered on (None in unit tests
# that drive the channel directly with their own emit function).
_socketio = None

# sid -> _Channel. Guarded by _channels_lock.
_channels = {}
_channels_lock = threading.Lock()


class SessionError(Exception):
    """An operation failure with a wire error code."""

    def __init__(self, code, message, close=False):
        super().__init__(message)
        self.code = code
        self.message = message
        # Whether the session must end after reporting this failure.
        self.close = close


def normalize_uuid(value):
    """Lowercase a UUID and expand 16/32-bit short forms to 128-bit."""
    text = str(value).strip().lower()
    m = _SHORT_UUID_RE.match(text)
    if m:
        return m.group(2).rjust(8, '0') + _BASE_UUID_SUFFIX
    return text


def _bounded_float(params, key, default, bounds):
    raw = params.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SessionError('invalid_argument', '%s must be a number' % key)
    lo, hi = bounds
    if not lo <= value <= hi:
        raise SessionError('invalid_argument',
                           '%s must be between %g and %g seconds' % (key, lo, hi))
    return value


def classify_error(exc, link_lost=False):
    """Map a bleak/BlueZ exception to ``(code, message)``.

    bleak's exception classes are matched by name so this works against the
    stubbed bleak the unit tests load.
    """
    hint = bluez_unavailable_hint(exc)
    if hint:
        return 'bluez_unavailable', hint
    text = str(exc)
    name = type(exc).__name__
    if link_lost or 'Not connected' in text:
        return 'disconnected', 'The peripheral disconnected'
    if name == 'BleakCharacteristicNotFoundError':
        return 'unknown_characteristic', text
    dbus = '%s %s' % (getattr(exc, 'dbus_error', '') or '', text)
    for marker in ('NotPermitted', 'NotSupported', 'NotAuthorized'):
        if marker in dbus:
            return 'not_permitted', 'Not permitted by the peripheral: %s' % text
    return 'ble_error', 'BLE error: %s' % text


def _device_vanished(exc):
    """True when bleak lost the device between its scan and the connect."""
    text = str(exc)
    return (("device '" in text and "' not found" in text)
            or 'removed from BlueZ' in text)


def _make_client(target, timeout, disconnected_callback):
    """Build the BleakClient for a session (patched in unit tests).

    `target` is an address or BlueZ's BLEDevice record (see ble.ble_target).
    """
    from bleak import BleakClient

    return BleakClient(target, timeout=timeout,
                       disconnected_callback=disconnected_callback)


def max_write_len(mtu):
    """Largest payload one ATT write carries: MTU - 3, capped at 512."""
    return min(max(mtu - 3, 1), ATT_MAX_VALUE)


def negotiated_mtu(client):
    """Return ``(mtu, source)`` for a connected client.

    BlueZ runs the ATT MTU exchange itself at connect (offering main.conf's
    ExchangeMTU, 517 by default) before service discovery, and BlueZ >= 5.62
    publishes the result as the ``MTU`` property of every GattCharacteristic1.
    bleak keeps that live property dict as ``char.obj``. ``client.mtu_size`` is
    not used: on bleak 0.22 BlueZ it returns 23 unless ``_acquire_mtu()`` ran,
    and that calls AcquireNotify/AcquireWrite, which has side effects.
    """
    for service in client.services:
        for char in service.characteristics:
            props = getattr(char, 'obj', None)
            if isinstance(props, dict) and isinstance(props.get('MTU'), int):
                return props['MTU'], 'bluez'
    return 23, 'default'


# ---------------------------------------------------------------------------
# Outbox: one thread emits everything, in queue order
# ---------------------------------------------------------------------------

class _Outbox:
    """Ordered delivery of box -> client events.

    Entries are ``(channel, event, payload)``; ``event is None`` marks a
    notification item. The thread drains everything queued and emits it in
    order, folding runs of one channel's notifications into a single
    ``ble_notify``.
    """

    def __init__(self):
        self._queue = collections.deque()
        self._cv = threading.Condition()
        self._thread = None

    def put(self, channel, event, payload):
        with self._cv:
            self._queue.append((channel, event, payload))
            self._ensure_thread()
            self._cv.notify()

    def _ensure_thread(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._run, name='ble-session-outbox', daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                batch = list(self._queue)
                self._queue.clear()
            self._emit_batch(batch)

    @staticmethod
    def _emit_batch(batch):
        run_channel, run_items = None, []

        def flush_run():
            if run_items:
                run_channel.notify_drained(run_items)
                run_channel.send('ble_notify', {'items': list(run_items)})
                run_items.clear()

        for channel, event, payload in batch:
            if event is None:
                if channel is not run_channel:
                    flush_run()
                    run_channel = channel
                run_items.append(payload)
                continue
            flush_run()
            channel.send(event, payload)
        flush_run()


_outbox = _Outbox()


# ---------------------------------------------------------------------------
# Channel: one Socket.IO connection, at most one open session
# ---------------------------------------------------------------------------

class _Channel:
    """Ordered executor and session state for one Socket.IO connection.

    Every attribute below except the reorder buffer is only touched on the
    bleak loop thread.
    """

    def __init__(self, sid, emit, client_gone=None):
        self.sid = sid
        self._emit = emit
        self._client_gone = client_gone or (lambda: False)
        self.loop = get_bleak_loop()

        # Reorder buffer, filled from handler threads.
        self._lock = threading.Lock()
        self._pending = {}
        self._next_seq = 1
        self._draining = False
        self._gap_timer = None

        # Session state: idle -> opening -> open -> closing -> idle.
        self.state = 'idle'
        self.address = None
        self.holder = None
        self.client = None
        self.mtu = 23
        self.mtu_source = 'default'
        self.services = []
        self._chars = []  # [(uuid, handle, properties, bleak char)]
        self.subscriptions = {}  # handle -> uuid
        self.idle_timeout = DEFAULT_IDLE_TIMEOUT
        self.opened_at = None
        self.last_op = time.monotonic()
        self._holds_adapter = False
        self._link_lost = False
        self._op_running = False
        self._deferred_close = None
        self._watchdog = None

        # Notification accounting (queued-but-not-emitted, for overflow).
        self._notify_lock = threading.Lock()
        # Event counter stamped on everything sent (see send()).
        self._eseq = 0
        self._queued_items = 0
        self._queued_bytes = 0
        self._notify_n = 0
        self._overflowed = False
        self._dropped = 0

    # -- outbound -----------------------------------------------------------

    def send(self, event, payload):
        """Emit one event to this channel's client (outbox thread).

        Stamps it with ``e``, this connection's event counter. python-socketio
        clients dispatch each incoming message on its own thread, so a burst
        can be handled out of order; the CLI client puts events back in ``e``
        order before acting on them.
        """
        self._eseq += 1
        payload = dict(payload, e=self._eseq)
        try:
            self._emit(event, payload)
        except Exception:  # noqa: BLE001 — a gone client must not kill the outbox
            logger.exception("[BLE session] emit %s to %s failed", event, self.sid)

    def _queue(self, event, payload):
        _outbox.put(self, event, payload)

    def _result(self, seq, value=None, error=None):
        if error is None:
            self._queue('ble_result', {'seq': seq, 'ok': True, 'value': value or {}})
        else:
            code, message = error
            self._queue('ble_result', {'seq': seq, 'ok': False,
                                       'code': code, 'message': message})

    def notify_drained(self, items):
        with self._notify_lock:
            self._queued_items -= len(items)
            self._queued_bytes -= sum(len(i['data']) // 2 for i in items)

    # -- inbound ------------------------------------------------------------

    def submit(self, op, data):
        """Queue one client operation (any thread)."""
        if not isinstance(data, dict):
            data = {}
        seq = data.get('seq')
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            self._result(seq, error=('invalid_argument',
                                     'seq must be a positive integer'))
            return
        with self._lock:
            if seq < self._next_seq or seq in self._pending:
                self._result(seq, error=('protocol_error',
                                         'seq %d was already used' % seq))
                return
            self._pending[seq] = (op, data)
        self.loop.call_soon_threadsafe(self._pump)

    def _pump(self):
        """Start the drain task if it is not running (bleak loop)."""
        if not self._draining:
            self._draining = True
            asyncio.ensure_future(self._drain())

    async def _drain(self):
        try:
            while True:
                with self._lock:
                    item = self._pending.pop(self._next_seq, None)
                    if item is None:
                        waiting = bool(self._pending)
                    else:
                        seq = self._next_seq
                        self._next_seq += 1
                if item is None:
                    if waiting and self._gap_timer is None:
                        self._gap_timer = self.loop.call_later(
                            SEQ_GAP_TIMEOUT, self._gap_expired)
                    return
                if self._gap_timer is not None:
                    self._gap_timer.cancel()
                    self._gap_timer = None
                await self._execute(seq, *item)
        finally:
            self._draining = False

    def _gap_expired(self):
        self._gap_timer = None
        with self._lock:
            if not self._pending or self._next_seq in self._pending:
                return
            missing = self._next_seq
            stranded = sorted(self._pending)
            self._pending.clear()
            self._next_seq = stranded[-1] + 1
        message = 'seq %d never arrived' % missing
        for seq in stranded:
            self._result(seq, error=('protocol_error', message))
        asyncio.ensure_future(self._close('protocol_error', message))

    # -- execution ----------------------------------------------------------

    async def _execute(self, seq, op, data):
        handler = _OPS.get(op)
        if op != 'ping':
            self.last_op = time.monotonic()
        if op == 'close':
            # Result first, then ble_closed, as documented.
            self._result(seq, {})
            await self._close('client', 'Closed by the client')
            return
        self._op_running = True
        close_after = None
        try:
            if op != 'open' and self.state != 'open':
                raise SessionError('not_open', 'No BLE session is open')
            value = await handler(self, data)
            self._result(seq, value)
        except SessionError as e:
            self._result(seq, error=(e.code, e.message))
            if e.close:
                close_after = (e.code, e.message)
        except asyncio.TimeoutError:
            message = '%s did not finish within %gs' % (op, OP_TIMEOUT)
            self._result(seq, error=('timeout', message))
            close_after = ('timeout', message)
        except Exception as e:  # noqa: BLE001 — bleak/BlueZ errors are data
            code, message = classify_error(e, self._link_lost)
            logger.warning("[BLE session] %s failed: %s", op, e)
            self._result(seq, error=(code, message))
            if code in ('disconnected', 'bluez_unavailable'):
                close_after = (code, message)
        finally:
            self._op_running = False
            self.last_op = time.monotonic()
        if self._deferred_close is not None:
            close_after, self._deferred_close = self._deferred_close, None
        if close_after is not None and self.state == 'open':
            await self._close(*close_after)

    # -- lifecycle ----------------------------------------------------------

    def describe(self):
        """Holder info for adapter_busy errors and GET /ble/sessions."""
        now = time.monotonic()
        return {
            'address': self.address,
            'holder': self.holder,
            'state': self.state,
            'opened_s': round(now - self.opened_at, 1) if self.opened_at else 0.0,
            'idle_s': round(now - self.last_op, 1),
            'idle_timeout': self.idle_timeout,
            'mtu': self.mtu,
            'subscriptions': sorted(set(self.subscriptions.values())),
        }

    async def open(self, data):
        if self.state != 'idle':
            raise SessionError('session_active',
                               'A BLE session is already open on this connection')
        address = data.get('address') or ''
        if not _BLE_ADDRESS_RE.match(address):
            raise SessionError('invalid_argument',
                               'Invalid BLE address format. Use XX:XX:XX:XX:XX:XX')
        connect_timeout = _bounded_float(data, 'connect_timeout',
                                         DEFAULT_CONNECT_TIMEOUT, CONNECT_TIMEOUT_RANGE)
        idle_timeout = _bounded_float(data, 'idle_timeout',
                                      DEFAULT_IDLE_TIMEOUT, IDLE_TIMEOUT_RANGE)
        holder = data.get('holder')
        holder = str(holder)[:200] if holder else None
        busy = adapter_holder_info()
        if busy is not None:
            raise SessionError('adapter_busy', adapter_busy_message(busy))

        self.state = 'opening'
        self.address = address.upper().replace('-', ':')
        self.holder = holder
        self.idle_timeout = idle_timeout
        self._link_lost = False
        try:
            # Never block the loop on the adapter lock: a /ble/command request
            # holding it may itself be waiting on this loop in run_bleak.
            acquired = await self.loop.run_in_executor(
                None, bt_adapter_lock.acquire, True, connect_timeout)
            if not acquired:
                raise SessionError(
                    'adapter_busy',
                    "The box's Bluetooth adapter stayed busy for %gs "
                    "(another BLE, BluFi or session operation)" % connect_timeout)
            self._holds_adapter = True
            set_adapter_holder(self, self.describe)

            await self._connect(connect_timeout)
            self._load_services()
            self.mtu, self.mtu_source = negotiated_mtu(self.client)
        except BaseException:
            await self._release_link()
            self.state = 'idle'
            self.address = None
            raise

        self.state = 'open'
        self.opened_at = time.monotonic()
        self.last_op = self.opened_at
        with self._notify_lock:
            self._notify_n = 0
            self._overflowed = False
            self._dropped = 0
        self._watchdog = asyncio.ensure_future(self._watch())
        logger.info("[BLE session] %s opened %s (mtu %d, %s)",
                    self.sid, self.address, self.mtu, self.mtu_source)
        return self.info_value()

    async def _connect(self, connect_timeout):
        """Connect self.client, retrying once if BlueZ loses the device.

        bleak's connect first scans for the address, then looks the device
        up again in BlueZ. BlueZ can drop a temporary (unbonded) device in
        between, when that scan stops, and bleak then fails with a plain
        BleakError "device 'dev_...' not found". The device is still there,
        so a second attempt nearly always connects.
        """
        deadline = self.loop.time() + connect_timeout + CONNECT_OVERHEAD
        for attempt in (1, 2):
            target = await ble_target(self.address)
            self.client = _make_client(target, connect_timeout, self._on_link_lost)
            try:
                await asyncio.wait_for(self.client.connect(),
                                       max(deadline - self.loop.time(), 0.1))
                return
            except asyncio.TimeoutError:
                raise SessionError('connect_failed',
                                   'Timed out connecting to %s' % self.address)
            except Exception as e:  # noqa: BLE001
                hint = bluez_unavailable_hint(e)
                if hint:
                    raise SessionError('bluez_unavailable', hint)
                vanished = _device_vanished(e)
                if vanished and attempt == 1:
                    logger.info("[BLE session] %s vanished from BlueZ before connect; "
                                "retrying once", self.address)
                    self.client = None
                    continue
                if vanished or type(e).__name__ == 'BleakDeviceNotFoundError':
                    raise SessionError(
                        'device_not_found',
                        'Device %s was not found. Check it is advertising and '
                        'in range (lager ble scan)' % self.address)
                raise SessionError('connect_failed',
                                   'Could not connect to %s: %s' % (self.address, e))

    def _load_services(self):
        services, chars = [], []
        for service in self.client.services:
            entry = {
                'uuid': str(service.uuid),
                'description': service.description,
                'characteristics': [],
            }
            for char in service.characteristics:
                props = list(char.properties)
                entry['characteristics'].append({
                    'uuid': str(char.uuid),
                    'handle': char.handle,
                    'description': char.description,
                    'properties': props,
                })
                chars.append((normalize_uuid(char.uuid), char.handle, props, char))
            services.append(entry)
        self.services = services
        self._chars = chars

    def info_value(self):
        return {
            'address': self.address,
            'mtu': self.mtu,
            'mtu_source': self.mtu_source,
            'services': self.services,
        }

    def _on_link_lost(self, _client=None):
        """bleak's disconnected_callback (bleak loop)."""
        if self.state not in ('open', 'opening'):
            return
        self._link_lost = True
        reason = ('disconnected', 'The peripheral disconnected')
        if self.state == 'opening':
            return  # open() reports the failure itself
        if self._op_running:
            # The in-flight operation reports `disconnected` first; _execute
            # closes after it.
            self._deferred_close = reason
        else:
            asyncio.ensure_future(self._close(*reason))

    async def _watch(self):
        try:
            while self.state == 'open':
                await asyncio.sleep(WATCHDOG_INTERVAL)
                if self.state != 'open':
                    return
                if self._client_gone():
                    await self.destroy()
                    return
                if self._op_running:
                    continue
                if time.monotonic() - self.last_op > self.idle_timeout:
                    await self._close(
                        'idle_timeout',
                        'No client operation for %gs' % self.idle_timeout)
                    return
        except asyncio.CancelledError:
            pass

    async def _release_link(self):
        client, self.client = self.client, None
        if client is not None and not self._link_lost:
            try:
                await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
            except BaseException as e:  # noqa: BLE001 — teardown is best effort
                logger.warning("[BLE session] disconnect of %s failed: %s", self.address, e)
        if self._holds_adapter:
            self._holds_adapter = False
            clear_adapter_holder(self)
            bt_adapter_lock.release()

    async def _close(self, reason, message=''):
        """Tear the session down and send ble_closed. Idempotent."""
        if self.state != 'open':
            return
        self.state = 'closing'
        watchdog, self._watchdog = self._watchdog, None
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
        await self._release_link()
        address = self.address
        self.subscriptions = {}
        self.state = 'idle'
        self.opened_at = None
        logger.info("[BLE session] %s closed %s: %s", self.sid, address, reason)
        self._queue('ble_closed', {'reason': reason, 'message': message,
                                   'address': address})

    async def destroy(self):
        """The client is gone: close and forget this channel."""
        with _channels_lock:
            if _channels.get(self.sid) is self:
                del _channels[self.sid]
        await self._close('client', 'The client disconnected')

    async def release(self, why):
        await self._close('released', why)

    # -- notifications ------------------------------------------------------

    def _on_notify(self, char, data):
        """bleak notification callback (bleak loop)."""
        payload = bytes(data)
        with self._notify_lock:
            self._notify_n += 1
            if self._overflowed:
                self._dropped += 1
                return
            if (self._queued_items + 1 > NOTIFY_QUEUE_MAX_ITEMS
                    or self._queued_bytes + len(payload) > NOTIFY_QUEUE_MAX_BYTES):
                self._overflowed = True
                self._dropped += 1
                overflow = True
            else:
                self._queued_items += 1
                self._queued_bytes += len(payload)
                overflow = False
                item = {
                    'n': self._notify_n,
                    'char': str(char.uuid),
                    'handle': char.handle,
                    'ts': time.time(),
                    'data': payload.hex(),
                }
        if overflow:
            message = ('The client did not drain notifications; more than %d '
                       'notifications / %d bytes were queued'
                       % (NOTIFY_QUEUE_MAX_ITEMS, NOTIFY_QUEUE_MAX_BYTES))
            asyncio.ensure_future(self._close('overflow', message))
            return
        self._queue(None, item)

    # -- operations ---------------------------------------------------------

    def _resolve(self, data):
        """Find the characteristic named by ``char`` (UUID) or ``handle``."""
        if data.get('handle') is not None:
            handle = data.get('handle')
            if isinstance(handle, bool) or not isinstance(handle, int):
                raise SessionError('invalid_argument', 'handle must be an integer')
            matches = [c for c in self._chars if c[1] == handle]
            label = 'handle %d' % handle
        elif data.get('char'):
            uuid = normalize_uuid(data['char'])
            matches = [c for c in self._chars if c[0] == uuid]
            label = uuid
        else:
            raise SessionError('invalid_argument', 'char (UUID) or handle is required')
        if not matches:
            raise SessionError('unknown_characteristic',
                               'No characteristic %s on %s' % (label, self.address))
        if len(matches) > 1:
            raise SessionError(
                'ambiguous_characteristic',
                'Characteristic %s appears %d times (handles %s); pass a handle'
                % (label, len(matches), ', '.join(str(c[1]) for c in matches)))
        return matches[0]

    @staticmethod
    def _require(props, wanted, what, uuid):
        if not any(p in props for p in wanted):
            raise SessionError(
                'not_permitted',
                'Characteristic %s does not support %s (properties: %s)'
                % (uuid, what, ', '.join(props) or 'none'))

    async def op_subscribe(self, data):
        uuid, handle, props, char = self._resolve(data)
        self._require(props, ('notify', 'indicate'), 'notify or indicate', uuid)
        mode = 'notify' if 'notify' in props else 'indicate'
        if handle not in self.subscriptions:
            # bleak registers the callback before BlueZ writes the CCCD, so a
            # notification sent the moment it is written is not missed.
            await asyncio.wait_for(self.client.start_notify(char, self._on_notify),
                                   OP_TIMEOUT)
            self.subscriptions[handle] = uuid
        return {'char': uuid, 'handle': handle, 'mode': mode}

    async def op_unsubscribe(self, data):
        uuid, handle, _props, char = self._resolve(data)
        if handle in self.subscriptions:
            await asyncio.wait_for(self.client.stop_notify(char), OP_TIMEOUT)
            del self.subscriptions[handle]
        return {'char': uuid, 'handle': handle}

    async def op_write(self, data):
        uuid, handle, props, char = self._resolve(data)
        try:
            payload = bytes.fromhex(data.get('data') or '')
        except (TypeError, ValueError) as e:
            raise SessionError('invalid_argument', 'Invalid hex data: %s' % e)
        if len(payload) > MAX_WRITE_BYTES:
            raise SessionError('invalid_argument',
                               'Write of %d bytes exceeds the %d-byte limit'
                               % (len(payload), MAX_WRITE_BYTES))
        response = data.get('response', True) is not False
        if response:
            self._require(props, ('write',), 'write with response', uuid)
        else:
            self._require(props, ('write-without-response',),
                          'write without response', uuid)
        chunk_size = data.get('chunk_size')
        if chunk_size is not None and (isinstance(chunk_size, bool)
                                       or not isinstance(chunk_size, int)
                                       or not 1 <= chunk_size <= ATT_MAX_VALUE):
            raise SessionError('invalid_argument',
                               'chunk_size must be an integer from 1 to %d' % ATT_MAX_VALUE)
        if data.get('chunk') or chunk_size is not None:
            # chunk_size caps the pieces below what the MTU allows, for a
            # peripheral whose receive buffer is smaller than MTU - 3.
            size = min(max_write_len(self.mtu), chunk_size or ATT_MAX_VALUE)
            chunks = [payload[i:i + size] for i in range(0, len(payload), size)] or [b'']
        elif len(payload) > ATT_MAX_VALUE:
            raise SessionError(
                'invalid_argument',
                'A single BLE write carries at most %d bytes; send these %d bytes '
                'with chunk' % (ATT_MAX_VALUE, len(payload)))
        else:
            chunks = [payload]
        for piece in chunks:
            await asyncio.wait_for(
                self.client.write_gatt_char(char, piece, response=response), OP_TIMEOUT)
        return {'char': uuid, 'handle': handle, 'bytes': len(payload), 'chunks': len(chunks)}

    async def op_read(self, data):
        uuid, handle, props, char = self._resolve(data)
        self._require(props, ('read',), 'read', uuid)
        value = await asyncio.wait_for(self.client.read_gatt_char(char), OP_TIMEOUT)
        return {'char': uuid, 'handle': handle, 'data': bytes(value).hex()}

    async def op_info(self, _data):
        return self.info_value()

    async def op_ping(self, _data):
        self.last_op = time.monotonic()
        return {'idle_timeout': self.idle_timeout, 'idle_s': 0.0}


_OPS = {
    'open': _Channel.open,
    'subscribe': _Channel.op_subscribe,
    'unsubscribe': _Channel.op_unsubscribe,
    'write': _Channel.op_write,
    'read': _Channel.op_read,
    'info': _Channel.op_info,
    'ping': _Channel.op_ping,
    'close': None,  # handled in _execute
}


# ---------------------------------------------------------------------------
# Channel registry
# ---------------------------------------------------------------------------

def _client_gone(sid):
    """True when *sid* is no longer connected to the /ble namespace.

    Covers the race documented at uart.py's _client_gone: a disconnect handled
    before the in-flight ble_open registered its session leaves nothing to
    clean it up. Returns False when the manager cannot be introspected, so an
    unknown answer never tears down a live session.
    """
    try:
        manager = _socketio.server.manager
    except AttributeError:
        return False
    try:
        return not manager.is_connected(sid, NAMESPACE)
    except Exception:  # noqa: BLE001 — liveness is advisory, never fatal
        return False


def _socketio_emit(sid):
    def emit(event, payload):
        _socketio.emit(event, payload, namespace=NAMESPACE, room=sid)
    return emit


def get_channel(sid, emit=None, client_gone=None):
    """The channel for *sid*, created on first use."""
    with _channels_lock:
        channel = _channels.get(sid)
        if channel is None:
            channel = _Channel(sid, emit or _socketio_emit(sid),
                               client_gone or (lambda: _client_gone(sid)))
            _channels[sid] = channel
        return channel


def handle_event(sid, op, data):
    """Entry point for one client event (Socket.IO handler threads)."""
    get_channel(sid).submit(op, data)


def drop_channel(sid):
    """The Socket.IO connection closed: end its session, if any."""
    with _channels_lock:
        channel = _channels.get(sid)
    if channel is None:
        return
    try:
        run_bleak(channel.destroy(), DISCONNECT_TIMEOUT + 5.0)
    except Exception:  # noqa: BLE001
        logger.exception("[BLE session] teardown for %s failed", sid)


def open_sessions():
    """Channels that currently hold an open session."""
    with _channels_lock:
        return [c for c in _channels.values() if c.state == 'open']


def cleanup_ble_sessions():
    """Close every session (server shutdown)."""
    for channel in open_sessions():
        try:
            run_bleak(channel._close('shutdown', 'The box server is shutting down'),
                      DISCONNECT_TIMEOUT + 1.0)
        except Exception as e:  # noqa: BLE001
            logger.error("Error closing BLE session %s: %s", channel.sid, e)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_EVENTS = {
    'ble_open': 'open',
    'ble_subscribe': 'subscribe',
    'ble_unsubscribe': 'unsubscribe',
    'ble_write': 'write',
    'ble_read': 'read',
    'ble_info': 'info',
    'ble_ping': 'ping',
    'ble_close': 'close',
}


def register_ble_session_socketio(socketio) -> None:
    """Register the /ble namespace handlers."""
    global _socketio
    _socketio = socketio

    @socketio.on('connect', namespace=NAMESPACE)
    def handle_ble_connect():
        logger.info("BLE session client connected: %s", request.sid)
        socketio.emit('connected', {'status': 'ready', 'session_id': request.sid},
                      namespace=NAMESPACE, room=request.sid)

    @socketio.on('disconnect', namespace=NAMESPACE)
    def handle_ble_disconnect():
        logger.info("BLE session client disconnected: %s", request.sid)
        drop_channel(request.sid)

    for event, op in _EVENTS.items():
        def handler(data=None, _op=op):
            handle_event(request.sid, _op, data)
        socketio.on(event, namespace=NAMESPACE)(handler)


def register_ble_session_routes(app: Flask) -> None:
    """GET /ble/sessions and POST /ble/sessions/release."""

    @app.route('/ble/sessions', methods=['GET'])
    def ble_sessions_list():
        sessions = [c.describe() for c in open_sessions()]
        return jsonify({'success': True, 'sessions': sessions})

    @app.route('/ble/sessions/release', methods=['POST'])
    def ble_sessions_release():
        """Force-close open sessions (all, or the one for ``address``)."""
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        address = (data.get('address') or '').upper().replace('-', ':') or None
        targets = [c for c in open_sessions() if address in (None, c.address)]
        if not targets:
            return jsonify({'success': False,
                            'error': 'No open BLE session%s'
                                     % (' for %s' % address if address else '')}), 404
        released = []
        for channel in targets:
            info = channel.describe()
            try:
                run_bleak(channel.release('Released by another client'),
                          DISCONNECT_TIMEOUT + 5.0)
            except Exception as e:  # noqa: BLE001
                logger.exception("[BLE session] release of %s failed", channel.sid)
                return jsonify({'success': False, 'error': str(e)}), 500
            released.append(info)
        return jsonify({'success': True, 'released': released})

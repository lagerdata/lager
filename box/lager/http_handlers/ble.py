# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
BLE HTTP handler for the Lager Box HTTP+WebSocket Server.

POST /ble/command replaces the old :5000 ``ble.py`` impl-script path. BLE is a
box-level capability (the box's own Bluetooth adapter), not a saved net, so it
gets a dedicated endpoint like /usb/command rather than a /net/command role.

bleak is asyncio-only and box_http_server runs Flask-SocketIO in threading
mode, so all bleak coroutines execute on one dedicated event-loop thread
(created lazily, shared across requests). Requests submit coroutines with
``asyncio.run_coroutine_threadsafe`` and block on the result with a widened
timeout. There is a single BT adapter, so every one-shot BLE operation
additionally serializes under ``bt_adapter_lock`` — the same lock the blufi
handler takes, because BluFi drives the same adapter through its own internal
bleak thread.

One-shot GATT actions (read, write, notify, pair) still connect, do one
operation, and disconnect. Held-link work uses the ``session_*`` actions:
one open session owns the adapter until close, peer disconnect, its
``max_lifetime`` cap (default 300 s), or ``idle_timeout`` (default 60 s) without
any session request; other ``/ble/command`` and BluFi actions fail fast with 409
while it is open. Notifications buffered when a session ends other than by
``session_close`` stay pollable for 60 s. Of the session actions, only session_open and session_close take
``bt_adapter_lock``; operations on an open session never do, so a long notify poll cannot block
those 409s or session_close.
"""

import asyncio
import collections
import concurrent.futures
import contextlib
import logging
import re
import threading
import time
import uuid

from flask import Flask, jsonify, request

logger = logging.getLogger(__name__)

_VALID_ACTIONS = (
    "scan",
    "info",
    "connect",
    "disconnect",
    "read",
    "write",
    "notify",
    "pair",
    "session_open",
    "session_pair",
    "session_read",
    "session_write",
    "session_notify_start",
    "session_notify_poll",
    "session_notify_stop",
    "session_close",
)

# XX:XX:XX:XX:XX:XX (colons or dashes)
_BLE_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")

SESSION_DEFAULT_MAX_LIFETIME_S = 300.0
SESSION_MAX_LIFETIME_CEILING_S = 1800.0
SESSION_DEFAULT_IDLE_TIMEOUT_S = 60.0
SESSION_MIN_TIMEOUT_S = 5.0
_CLIENT_CLOSE_REASON = "closed"
_ENDED_RETENTION_S = 60.0
_REAPER_INTERVAL_S = 1.0
_ENDED_SESSIONS_KEPT = 16
_NOTIFY_BUFFER_MAX = 10000

# Module-level so tests can fake the clocks.
_monotonic = time.monotonic
_wall_clock = time.time

# One BT adapter on the box: serialize every BLE *and* BluFi operation.
# blufi.py imports this lock so the two handlers can't fight over the radio.
bt_adapter_lock = threading.Lock()

# Dedicated event-loop thread for bleak coroutines (created on first use).
_bleak_loop = None
_bleak_loop_guard = threading.Lock()

# Single held GATT session (v1). _sessions_lock guards _sessions, _ended,
# _retained and each session's activity fields;
# bleak awaits run on the bleak loop via run_bleak.
_sessions_lock = threading.Lock()
_sessions = {}  # session_id -> _BleSession
_ended = collections.OrderedDict()  # recently ended session_id -> reason
_retained = {}  # ended session_id -> session whose notify buffers are still pollable


class BleSessionConflict(Exception):
    """A held BLE session owns the adapter; the requested action cannot run."""


# bleak reaches the HOST's bluetoothd over the mounted /var/run/dbus socket; the
# container runs no bluetoothd of its own. With BlueZ missing on the host (Ubuntu
# Server ships without it) every call fails with D-Bus ServiceUnknown for
# org.bluez, which says nothing about the fix. cli/core/net_helpers.py carries
# the same text for boxes whose image predates this one; a unit test pins them.
BLUEZ_UNAVAILABLE_MESSAGE = (
    "BlueZ is not running on the box host. Run `lager update` for this box, "
    "or on the box run: sudo apt install -y bluez && sudo systemctl enable --now bluetooth"
)


def bluez_unavailable_hint(exc):
    """BLUEZ_UNAVAILABLE_MESSAGE when `exc` is the host having no BlueZ, else None."""
    text = str(exc)
    if "org.bluez" in text and "ServiceUnknown" in text:
        return BLUEZ_UNAVAILABLE_MESSAGE
    return None


def get_bleak_loop():
    """Return the shared bleak event loop, starting its thread on first use."""
    global _bleak_loop
    with _bleak_loop_guard:
        if _bleak_loop is None or _bleak_loop.is_closed():
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="ble-bleak-loop", daemon=True
            )
            thread.start()
            _bleak_loop = loop
        return _bleak_loop


def run_bleak(coro, timeout):
    """Run a coroutine on the bleak loop from Flask's worker thread."""
    future = asyncio.run_coroutine_threadsafe(coro, get_bleak_loop())
    try:
        return future.result(timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise


def _char_key(char):
    """Stable dict key for a characteristic UUID string or handle."""
    if isinstance(char, int):
        return char
    return char.strip().lower()


class _NotifyBuf:
    __slots__ = ("count", "label", "messages")

    def __init__(self, label):
        self.label = label
        self.count = 0
        self.messages = collections.deque(maxlen=_NOTIFY_BUFFER_MAX)


class _BleSession:
    """Client, loop and activity event are filled in by _session_open_async."""

    __slots__ = (
        "activity",
        "address",
        "client",
        "created_at",
        "ended_at",
        "ended_reason",
        "idle_timeout",
        "in_flight",
        "last_activity",
        "loop",
        "max_lifetime",
        "notify_bufs",
        "notify_chars",
        "session_id",
    )

    def __init__(self, session_id, address, max_lifetime, idle_timeout):
        self.session_id = session_id
        self.address = address
        self.max_lifetime = max_lifetime
        self.idle_timeout = idle_timeout
        self.in_flight = 0
        self.client = None
        self.loop = None
        self.activity = None  # set on notification or session end
        self.created_at = _monotonic()
        self.last_activity = self.created_at
        self.ended_at = None
        self.ended_reason = None
        self.notify_bufs = {}  # char_key -> _NotifyBuf
        self.notify_chars = {}  # char_key -> original char for stop_notify


def _session_remaining_s(session):
    return session.max_lifetime - (_monotonic() - session.created_at)


def _reap_reason(session):
    """Why the session should be reaped now, or None. Caller holds _sessions_lock."""
    if _session_remaining_s(session) <= 0:
        return "lifetime expired"
    idle_s = _monotonic() - session.last_activity
    if session.in_flight == 0 and idle_s >= session.idle_timeout:
        return "idle timeout"
    return None


@contextlib.contextmanager
def _session_request(session):
    """Counts the request as activity at start and end; busy sessions never idle."""
    with _sessions_lock:
        session.in_flight += 1
        session.last_activity = _monotonic()
    try:
        yield
    finally:
        with _sessions_lock:
            session.in_flight -= 1
            session.last_activity = _monotonic()


def _ended_error(session_id, reason):
    return ValueError("BLE session %s ended: %s" % (session_id, reason))


def _wake_pollers(session):
    try:
        session.loop.call_soon_threadsafe(session.activity.set)
    except RuntimeError:
        logger.debug("bleak loop closed; cannot wake pollers", exc_info=True)


def _end_session(session, reason):
    """Unregister the session and wake pollers. Only the first caller wins, so
    the disconnect callback fired by our own teardown cannot overwrite the
    reason. Returns whether this call ended it."""
    with _sessions_lock:
        if _sessions.pop(session.session_id, None) is None:
            return False
        session.ended_reason = reason
        session.ended_at = _monotonic()
        _ended[session.session_id] = reason
        if reason != _CLIENT_CLOSE_REASON:
            _retained[session.session_id] = session
        while len(_ended) > _ENDED_SESSIONS_KEPT:
            evicted, _ = _ended.popitem(last=False)
            _retained.pop(evicted, None)
    logger.info("BLE session %s ended (%s)", session.session_id, reason)
    _wake_pollers(session)
    return True


async def _disconnect_client(client):
    try:
        await client.disconnect()
    except Exception:
        logger.debug("session disconnect failed", exc_info=True)


async def _teardown_session_async(session):
    for key, char in list(session.notify_chars.items()):
        try:
            await session.client.stop_notify(char)
        except Exception:
            logger.debug("session stop_notify failed for %s", key, exc_info=True)
    session.notify_chars.clear()
    await _disconnect_client(session.client)


def _close_session(session, reason):
    """End and tear down a session. Caller holds bt_adapter_lock."""
    if not _end_session(session, reason):
        return
    try:
        run_bleak(_teardown_session_async(session), 30.0)
    except Exception:
        logger.debug("BLE session teardown failed", exc_info=True)


def _drop_stale_retained():
    now = _monotonic()
    with _sessions_lock:
        stale = [
            sid
            for sid, s in _retained.items()
            if now - s.ended_at >= _ENDED_RETENTION_S
        ]
        for sid in stale:
            del _retained[sid]


def reap_expired_sessions():
    """Close sessions past lifetime or idle limits and drop stale retained
    buffers; one reaper tick."""
    _drop_stale_retained()
    with _sessions_lock:
        doomed = [
            (s, reason)
            for s in _sessions.values()
            if (reason := _reap_reason(s)) is not None
        ]
    if not doomed:
        return
    with bt_adapter_lock:
        for session, reason in doomed:
            _close_session(session, reason)


def _reaper_loop():
    while True:
        time.sleep(_REAPER_INTERVAL_S)
        try:
            reap_expired_sessions()
        except Exception:
            logger.exception("BLE session reaper failed")


_reaper_guard = threading.Lock()
_reaper_thread = None


def _ensure_reaper():
    global _reaper_thread
    with _reaper_guard:
        if _reaper_thread is None:
            _reaper_thread = threading.Thread(
                target=_reaper_loop, name="ble-session-reaper", daemon=True
            )
            _reaper_thread.start()


def ble_session_busy_message():
    """Error text when a held session blocks other BLE/BluFi work, or None."""
    with _sessions_lock:
        if not _sessions:
            return None
        sid = next(iter(_sessions))
        return (
            "BLE session %s is open; close it before other BLE/BluFi operations" % sid
        )


def require_no_ble_session():
    """Raise BleSessionConflict if a GATT session owns the adapter."""
    msg = ble_session_busy_message()
    if msg:
        raise BleSessionConflict(msg)


def _get_live_session(session_id):
    with _sessions_lock:
        session = _sessions.get(session_id)
        reason = _ended.get(session_id)
    if session is not None:
        return session
    if reason is not None:
        raise _ended_error(session_id, reason)
    raise ValueError("unknown BLE session_id")


def _get_pollable_session(session_id):
    """Live session, or an ended one whose buffers are still retained."""
    with _sessions_lock:
        retained = _retained.get(session_id)
    if retained is not None:
        return retained
    return _get_live_session(session_id)


def _require_session_id(params):
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id is required")
    return session_id.strip()


async def _scan_async(timeout):
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = []
    for address, (device, adv) in found.items():
        devices.append(
            {
                "name": device.name or address,
                "address": address,
                "rssi": adv.rssi if adv is not None else -100,
                "uuids": list(adv.service_uuids or []) if adv is not None else [],
            }
        )
    return devices


async def _bluez_known_device(address):
    """BlueZ's existing record for `address` as a BLEDevice, else the address.

    Given a bare address, bleak scans before connecting, and BlueZ does not
    re-announce a device it already holds, so that scan often misses a device
    sitting right next to the box. Uses bleak 0.22 internals (the box pins it).
    """
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager
        from bleak.backends.device import BLEDevice

        manager = await get_global_bluez_manager()
        device_name = "dev_" + address.upper().replace(":", "_").replace("-", "_")
        path = "%s/%s" % (manager.get_default_adapter(), device_name)
        props = manager._properties.get(path, {}).get("org.bluez.Device1")
    except Exception:
        logger.debug("BlueZ device lookup failed; bleak will scan", exc_info=True)
        return address
    if not props:
        return address
    details = {"path": path, "props": props}
    return BLEDevice(
        props.get("Address", address), props.get("Alias"), details, props.get("RSSI", -127)
    )


async def _bleak_client(address, **kwargs):
    """A BleakClient for `address`, connecting via BlueZ's record when it has one."""
    from bleak import BleakClient

    return BleakClient(await _bluez_known_device(address), **kwargs)


async def _device_info_async(address, timeout):
    """Connect and enumerate services/characteristics (used by info+connect)."""
    async with await _bleak_client(address, timeout=timeout) as client:
        services = []
        for service in client.services:
            services.append(
                {
                    "uuid": str(service.uuid),
                    "description": service.description,
                    "characteristics": [
                        {
                            "uuid": str(char.uuid),
                            "description": char.description,
                            "properties": list(char.properties),
                        }
                        for char in service.characteristics
                    ],
                }
            )
    return {"address": address, "connected": True, "services": services}


async def _disconnect_async(address):
    """Ensure a device is disconnected. BLE links via bleak are transient, so
    this connects briefly and lets the context exit tear the link down —
    mirroring the old impl script's explicit-user-intent semantics."""
    try:
        async with await _bleak_client(address, timeout=5.0):
            pass
        return {"address": address, "disconnected": True}
    except Exception as e:
        # Unreachable means already disconnected — that's the desired state.
        return {
            "address": address,
            "disconnected": True,
            "note": bluez_unavailable_hint(e) or "Device not reachable: %s" % e,
        }


async def _read_async(address, char, timeout):
    async with await _bleak_client(address, timeout=timeout) as client:
        data = await client.read_gatt_char(char)
    return bytes(data)


async def _write_async(address, char, data, response, timeout):
    async with await _bleak_client(address, timeout=timeout) as client:
        await client.write_gatt_char(char, data, response=response)


async def _notify_async(address, char, max_messages, duration, timeout):
    """Subscribe, collect up to max_messages notifications or until duration
    seconds pass, then unsubscribe. Returns (messages, timed_out)."""
    messages = []
    done = asyncio.Event()

    def on_notify(_char, data):
        if len(messages) < max_messages:
            messages.append(bytes(data))
        if len(messages) >= max_messages:
            done.set()

    async with await _bleak_client(address, timeout=timeout) as client:
        await client.start_notify(char, on_notify)
        try:
            await asyncio.wait_for(done.wait(), timeout=duration)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
        try:
            await client.stop_notify(char)
        except Exception:
            # The link is torn down on exit anyway; the messages are what
            # the caller asked for.
            logger.debug("stop_notify failed", exc_info=True)
    return messages, timed_out


async def _pair_async(address, timeout):
    async with await _bleak_client(address, timeout=timeout) as client:
        await client.pair()


async def _session_open_async(session, timeout):
    def on_disconnect(_client):
        _end_session(session, "peer disconnected")

    session.loop = asyncio.get_running_loop()
    session.activity = asyncio.Event()
    client = await _bleak_client(
        session.address, timeout=timeout, disconnected_callback=on_disconnect
    )
    try:
        await client.connect()
    except asyncio.CancelledError:
        await _disconnect_client(client)
        raise
    session.client = client
    with _sessions_lock:
        session.last_activity = _monotonic()
        _sessions[session.session_id] = session


async def _session_read_async(session, char):
    data = await session.client.read_gatt_char(char)
    return bytes(data)


async def _session_write_async(session, char, data, response):
    await session.client.write_gatt_char(char, data, response=response)


async def _session_pair_async(session):
    await session.client.pair()


async def _session_notify_start_async(session, char):
    key = _char_key(char)
    if key in session.notify_chars:
        raise ValueError("notifications already started for this characteristic")

    buf = _NotifyBuf(char)

    def on_notify(_c, data):
        buf.count += 1
        buf.messages.append(
            {
                "char": buf.label,
                "n": buf.count,
                "ts": _wall_clock(),
                "data": bytes(data).hex(),
            }
        )
        session.activity.set()

    # Claim the key before awaiting so a concurrent start for the same char
    # is refused instead of subscribing twice.
    session.notify_bufs[key] = buf
    session.notify_chars[key] = char
    try:
        await session.client.start_notify(char, on_notify)
    except BaseException:
        if session.notify_chars.get(key) is char:
            session.notify_chars.pop(key)
            session.notify_bufs.pop(key, None)
        raise


async def _session_notify_stop_async(session, char):
    key = _char_key(char)
    stored = session.notify_chars.pop(key, None)
    session.notify_bufs.pop(key, None)
    if stored is None:
        raise ValueError("notifications are not active for this characteristic")
    try:
        await session.client.stop_notify(stored)
    except Exception:
        logger.debug("session stop_notify failed", exc_info=True)


def _poll_keys(session, char_filter):
    if char_filter is None:
        return list(session.notify_bufs.keys())
    key = _char_key(char_filter)
    if key not in session.notify_bufs:
        raise ValueError("notifications are not active for this characteristic")
    return [key]


def _drain_round_robin(session, keys, out, max_messages):
    """Append buffered messages one per char per pass so interleaved notifies
    stay interleaved, until the buffers are empty or max_messages is reached."""
    progressed = True
    while progressed and len(out) < max_messages:
        progressed = False
        for key in keys:
            buf = session.notify_bufs.get(key)
            if buf is None or not buf.messages:
                continue
            out.append(buf.messages.popleft())
            progressed = True
            if len(out) >= max_messages:
                break


async def _session_notify_poll_async(session, char_filter, max_messages, wait):
    """Return buffered notifications, waiting up to `wait` seconds for the first.

    Returns (messages, timed_out). Raises if the session ended with nothing
    buffered.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    out = []
    while True:
        session.activity.clear()
        _drain_round_robin(session, _poll_keys(session, char_filter), out, max_messages)
        if out:
            return out, False
        if session.ended_reason is not None:
            raise _ended_error(session.session_id, session.ended_reason)
        remaining = deadline - loop.time()
        if remaining <= 0:
            return out, True
        try:
            await asyncio.wait_for(session.activity.wait(), remaining)
        except asyncio.TimeoutError:
            pass


def scan(params):
    require_no_ble_session()
    timeout = float(params.get("timeout") or 5.0)
    if not 0.1 <= timeout <= 300.0:
        raise ValueError("timeout must be between 0.1 and 300 seconds")
    name_contains = params.get("name_contains")
    name_exact = params.get("name_exact")

    devices = run_bleak(_scan_async(timeout), timeout + 20.0)

    if name_exact:
        devices = [d for d in devices if d["name"] == name_exact]
    if name_contains:
        devices = [d for d in devices if name_contains.lower() in d["name"].lower()]
    devices.sort(key=lambda d: (d["name"] == d["address"], d["name"]))

    return {
        "message": "Found %d device(s)" % len(devices),
        "value": {"devices": devices},
    }


def _require_address(params):
    address = params.get("address") or ""
    if not _BLE_ADDRESS_RE.match(address):
        raise ValueError("Invalid BLE address format. Use XX:XX:XX:XX:XX:XX")
    return address


def info(params):
    require_no_ble_session()
    address = _require_address(params)
    timeout = float(params.get("timeout") or 10.0)
    result = run_bleak(_device_info_async(address, timeout), timeout + 20.0)
    return {
        "message": "Connected to %s: %d service(s)"
        % (address, len(result["services"])),
        "value": result,
    }


def disconnect(params):
    require_no_ble_session()
    address = _require_address(params)
    result = run_bleak(_disconnect_async(address), 30.0)
    return {
        "message": "Disconnected from %s" % address,
        "value": result,
    }


def _require_char(params):
    """A characteristic UUID string, or an integer handle."""
    char = params.get("char")
    if isinstance(char, bool):
        char = None
    if isinstance(char, int):
        if char < 0:
            raise ValueError("char handle must be non-negative")
        return char
    if isinstance(char, str) and char.strip():
        return char.strip()
    raise ValueError("char (characteristic UUID or handle) is required")


def _optional_char(params):
    """Like _require_char, but absent/None means no filter."""
    if "char" not in params or params.get("char") is None:
        return None
    return _require_char(params)


def _number(params, key, default):
    # An explicit 0 is a value to range-check, not a request for the default.
    value = params.get(key)
    return default if value is None else float(value)


def _timeout(params, default):
    timeout = _number(params, "timeout", default)
    if not 0.1 <= timeout <= 300.0:
        raise ValueError("timeout must be between 0.1 and 300 seconds")
    return timeout


def read(params):
    require_no_ble_session()
    address = _require_address(params)
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    data = run_bleak(_read_async(address, char, timeout), timeout + 20.0)
    return {
        "message": "Read %d byte(s) from %s" % (len(data), char),
        "value": {"address": address, "char": char, "data": data.hex()},
    }


def write(params):
    require_no_ble_session()
    address = _require_address(params)
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    raw = params.get("data")
    if not isinstance(raw, str):
        raise ValueError("data (hex string) is required")
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        raise ValueError("data must be a hex string, e.g. '0102ff'")
    # bleak picks write-with-response or without from the characteristic's
    # properties when response is None.
    response = params.get("response")
    if response is not None:
        response = bool(response)
    run_bleak(_write_async(address, char, data, response, timeout), timeout + 20.0)
    return {
        "message": "Wrote %d byte(s) to %s" % (len(data), char),
        "value": {"address": address, "char": char, "length": len(data)},
    }


def notify(params):
    require_no_ble_session()
    address = _require_address(params)
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    max_messages = int(_number(params, "max_messages", 1))
    if not 1 <= max_messages <= 10000:
        raise ValueError("max_messages must be between 1 and 10000")
    duration = _number(params, "duration", 10.0)
    if not 0.1 <= duration <= 300.0:
        raise ValueError("duration must be between 0.1 and 300 seconds")
    messages, timed_out = run_bleak(
        _notify_async(address, char, max_messages, duration, timeout),
        timeout + duration + 20.0,
    )
    return {
        "message": "Received %d notification(s) from %s" % (len(messages), char),
        "value": {
            "address": address,
            "char": char,
            "messages": [m.hex() for m in messages],
            "timed_out": timed_out,
        },
    }


def pair(params):
    require_no_ble_session()
    address = _require_address(params)
    timeout = _timeout(params, 10.0)
    run_bleak(_pair_async(address, timeout), timeout + 20.0)
    return {
        "message": "Paired with %s" % address,
        "value": {"address": address, "paired": True},
    }


def _session_limits(params):
    max_lifetime = _number(params, "max_lifetime", SESSION_DEFAULT_MAX_LIFETIME_S)
    if not SESSION_MIN_TIMEOUT_S <= max_lifetime <= SESSION_MAX_LIFETIME_CEILING_S:
        raise ValueError(
            "max_lifetime must be between %g and %g seconds"
            % (SESSION_MIN_TIMEOUT_S, SESSION_MAX_LIFETIME_CEILING_S)
        )
    default_idle = min(SESSION_DEFAULT_IDLE_TIMEOUT_S, max_lifetime)
    idle_timeout = _number(params, "idle_timeout", default_idle)
    if not SESSION_MIN_TIMEOUT_S <= idle_timeout <= max_lifetime:
        raise ValueError(
            "idle_timeout must be between %g and max_lifetime (%g) seconds"
            % (SESSION_MIN_TIMEOUT_S, max_lifetime)
        )
    return max_lifetime, idle_timeout


def session_open(params):
    address = _require_address(params)
    timeout = _timeout(params, 10.0)
    max_lifetime, idle_timeout = _session_limits(params)
    require_no_ble_session()
    _ensure_reaper()
    session = _BleSession(uuid.uuid4().hex, address, max_lifetime, idle_timeout)
    run_bleak(_session_open_async(session, timeout), timeout + 20.0)
    return {
        "message": "Opened BLE session %s to %s" % (session.session_id, address),
        "value": {
            "session_id": session.session_id,
            "address": address,
            "max_lifetime": max_lifetime,
            "idle_timeout": idle_timeout,
        },
    }


def session_pair(params):
    session = _get_live_session(_require_session_id(params))
    timeout = _timeout(params, 10.0)
    with _session_request(session):
        run_bleak(_session_pair_async(session), timeout + 20.0)
    return {
        "message": "Paired session %s with %s" % (session.session_id, session.address),
        "value": {
            "session_id": session.session_id,
            "address": session.address,
            "paired": True,
        },
    }


def session_read(params):
    session = _get_live_session(_require_session_id(params))
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    with _session_request(session):
        data = run_bleak(_session_read_async(session, char), timeout + 20.0)
    return {
        "message": "Read %d byte(s) from %s" % (len(data), char),
        "value": {"session_id": session.session_id, "char": char, "data": data.hex()},
    }


def session_write(params):
    session = _get_live_session(_require_session_id(params))
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    raw = params.get("data")
    if not isinstance(raw, str):
        raise ValueError("data (hex string) is required")
    try:
        data = bytes.fromhex(raw)
    except ValueError:
        raise ValueError("data must be a hex string, e.g. '0102ff'")
    response = params.get("response")
    if response is not None:
        response = bool(response)
    with _session_request(session):
        run_bleak(_session_write_async(session, char, data, response), timeout + 20.0)
    return {
        "message": "Wrote %d byte(s) to %s" % (len(data), char),
        "value": {"session_id": session.session_id, "char": char, "length": len(data)},
    }


def session_notify_start(params):
    session = _get_live_session(_require_session_id(params))
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    with _session_request(session):
        run_bleak(_session_notify_start_async(session, char), timeout + 20.0)
    return {
        "message": "Started notifications on %s" % char,
        "value": {"session_id": session.session_id, "char": char},
    }


def session_notify_stop(params):
    session = _get_live_session(_require_session_id(params))
    char = _require_char(params)
    timeout = _timeout(params, 10.0)
    with _session_request(session):
        run_bleak(_session_notify_stop_async(session, char), timeout + 20.0)
    return {
        "message": "Stopped notifications on %s" % char,
        "value": {"session_id": session.session_id, "char": char},
    }


def session_notify_poll(params):
    session = _get_pollable_session(_require_session_id(params))
    char = _optional_char(params)
    max_messages = int(_number(params, "max_messages", 1))
    if not 1 <= max_messages <= 10000:
        raise ValueError("max_messages must be between 1 and 10000")
    wait = _number(params, "wait", 0.0)
    if not 0.0 <= wait <= 300.0:
        raise ValueError("wait must be between 0 and 300 seconds")
    if session.ended_reason is None and not session.notify_bufs:
        raise ValueError("no notifications are active; call session_notify_start first")
    wait = min(wait, max(0.0, _session_remaining_s(session)))
    with _session_request(session):
        messages, timed_out = run_bleak(
            _session_notify_poll_async(session, char, max_messages, wait), wait + 20.0
        )
    return {
        "message": "Received %d notification(s)" % len(messages),
        "value": {
            "session_id": session.session_id,
            "messages": messages,
            "timed_out": timed_out,
            "ended": session.ended_reason,
        },
    }


def session_close(params):
    """Idempotent: an unknown or already-ended session_id still succeeds.

    Without session_id, closes nothing unless ``force`` is set, which closes
    whatever session is open (recovery for a client that lost the id).
    """
    session_id = params.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        session_id = session_id.strip()
        with _sessions_lock:
            session = _sessions.get(session_id)
            _retained.pop(session_id, None)
        targets = [session] if session is not None else []
    else:
        with _sessions_lock:
            targets = list(_sessions.values())
        if not targets:
            return {
                "message": "No BLE session to close",
                "value": {"closed": True, "session_id": None},
            }
        if not params.get("force"):
            raise ValueError("session_id is required")
        session_id = targets[0].session_id
    for session in targets:
        _close_session(session, _CLIENT_CLOSE_REASON)
    return {
        "message": "Closed BLE session %s" % session_id,
        "value": {"closed": True, "session_id": session_id},
    }


_ACTIONS = {
    "scan": scan,
    "info": info,
    "connect": info,  # connect == connect + enumerate services, like the old script
    "disconnect": disconnect,
    "read": read,
    "write": write,
    "notify": notify,
    "pair": pair,
    "session_open": session_open,
    "session_pair": session_pair,
    "session_read": session_read,
    "session_write": session_write,
    "session_notify_start": session_notify_start,
    "session_notify_poll": session_notify_poll,
    "session_notify_stop": session_notify_stop,
    "session_close": session_close,
}

# Run on the held link without bt_adapter_lock so a long poll cannot block
# the 409s for other BLE/BluFi work or session_close.
_SESSION_OPERATION_ACTIONS = frozenset(
    (
        "session_pair",
        "session_read",
        "session_write",
        "session_notify_start",
        "session_notify_poll",
        "session_notify_stop",
    )
)


def register_ble_routes(app: Flask) -> None:
    """Register the /ble/command route on the Flask app."""

    @app.route("/ble/command", methods=["POST"])
    def ble_command_http():
        """
        Execute a BLE command using the box's Bluetooth adapter.

        Request body:
            { "action": "<one-shot or session_* action>",
              "params": { ... } }
        """
        try:
            data = request.get_json() or {}
            action = data.get("action")
            params = data.get("params") or {}

            if action not in _VALID_ACTIONS:
                return jsonify(
                    {
                        "success": False,
                        "error": (
                            "action (scan|info|connect|disconnect|read|write|"
                            "notify|pair|session_open|session_pair|session_read|"
                            "session_write|session_notify_start|session_notify_poll|"
                            "session_notify_stop|session_close) is required"
                        ),
                    }
                ), 400

            if action in _SESSION_OPERATION_ACTIONS:
                adapter_guard = contextlib.nullcontext()
            else:
                adapter_guard = bt_adapter_lock
            try:
                with adapter_guard:
                    result = _ACTIONS[action](params)
            except BleSessionConflict as e:
                return jsonify({"success": False, "error": str(e)}), 409
            except ValueError as e:
                return jsonify({"success": False, "error": str(e)}), 400
            except Exception as e:
                # bleak errors (adapter off, device unreachable, connect
                # timeout) are hardware errors, not server bugs.
                logger.exception("[HTTP] /ble/command %s failed", action)
                return jsonify(
                    {
                        "success": False,
                        "error": bluez_unavailable_hint(e) or "BLE error: %s" % e,
                    }
                ), 502

            logger.info("[HTTP] /ble/command %s ok", action)
            return jsonify({"success": True, "action": action, **result})

        except Exception as e:
            logger.exception("[HTTP] /ble/command unexpected error")
            return jsonify({"success": False, "error": str(e)}), 500

# BLE GATT sessions

Status: **approved; implemented** on `cf/ble-gatt-sessions` (box, CLI) and the
lager-net `ble-session` feature. Not yet hardware-verified. "Decisions" at the end
records what review settled and where the code differs from the first draft.

## Problem

`POST /ble/command` can scan, connect, list GATT services and disconnect.
Every call is self-contained: `info`/`connect` open a link, enumerate and drop
it inside one `async with BleakClient(...)`. There is no way to hold a link
open and exchange data with a peripheral from off the box. Anyone who needs
that today runs their own tool on the box over SSH.

This adds a **BLE session**: a persistent GATT connection owned by the box and
driven from the CLI or the Rust crate, with no SSH. Lager moves bytes and
nothing else. Framing, request/response matching, encryption and every other
protocol concern stay in the caller's code.

## Goals and non-goals

Goals:

- Open a connection by address, keep it across many operations, close it
  explicitly or automatically (client gone, idle, link lost).
- Report the negotiated ATT MTU.
- Subscribe to notifications/indications, then stream every notification to
  the client as raw bytes with characteristic UUID, handle and timestamp,
  including unsolicited ones. Nothing is lost silently.
- Write raw bytes (with or without response), optionally chunked to
  `mtu - 3`, in order.
- Read a characteristic. Return service discovery info for the open link.
- Clear, typed errors.
- One gateway round trip per operation; a two-step request/response
  exchange finishes well inside 5 s end to end.

Non-goals (v1):

- Any application protocol. No framing, no reassembly, no crypto.
- Pairing/bonding APIs. (See "Security and pairing" for what BlueZ does on
  its own.)
- Per-session control of connection parameters or MTU. Neither is exposed by
  BlueZ's D-Bus API; see the MTU and connection-parameter sections.
- Async (`--features async`) Rust sessions. `Uart` and `RttSession` are
  blocking-only; this follows them. The async client keeps building because
  the new code is behind its own feature.
- Coordinating with on-box `lager python` scripts that use
  `lager.ble.Client` directly. Those already bypass `bt_adapter_lock` today;
  this change does not make that worse and does not fix it.

## Transport: a `/ble` Socket.IO namespace

Same shape as `/uart` and `/rtt` on the `:9000` server, for the same reasons:

- Notifications are server push. Polling over HTTP would add a round trip and
  latency to every reply.
- One WebSocket stays open for the whole session, so each operation is one
  frame each way. The gateway authenticates the handshake once instead of
  every request (the contract already covers Socket.IO handshakes on `:9000`,
  and namespaces share the one `/socket.io/` path, so no gateway change).
- The session's lifetime is tied to the Socket.IO connection, which gives
  "client went away" cleanup for free.

Code lives in a new module, `box/lager/http_handlers/ble_session.py`,
registered from `box_http_server.py` next to `register_ble_routes` (with the
same `try/except` import guard). `http_handlers/` is copied into the image as
a whole directory, so **no `box.Dockerfile` change** is needed. It reuses
`get_bleak_loop()`, `bluez_unavailable_hint()` and `BLUEZ_UNAVAILABLE_MESSAGE`
from `ble.py`.

### Events

Payload bytes are lowercase hex under a `data` key, the same codec `/uart` and
`/rtt` use (`lager-net`'s `nets/sio.rs`), so neither client grows a second
encoding.

Client → box. Every event carries `seq`, a per-connection integer that starts
at 1 and increments by 1:

| Event | Fields | Result `value` |
|---|---|---|
| `ble_open` | `address`, `connect_timeout` (s, default 10), `idle_timeout` (s, default 300), `holder` (optional label) | `{address, mtu, mtu_source, services}` |
| `ble_subscribe` | `char` (UUID) or `handle` | `{char, handle, mode: "notify"\|"indicate"}` |
| `ble_unsubscribe` | `char` or `handle` | `{char, handle}` |
| `ble_write` | `char` or `handle`, `data` (hex), `response` (bool, default true), `chunk` (bool, default false) | `{char, handle, bytes, chunks}` |
| `ble_read` | `char` or `handle` | `{char, handle, data}` |
| `ble_info` | none | `{address, mtu, mtu_source, services}` |
| `ble_ping` | none | `{idle_timeout, idle_s}` (resets the idle timer and nothing else) |
| `ble_close` | none | `{}`, then `ble_closed` |

Box → client:

| Event | Fields |
|---|---|
| `ble_result` | `seq`, `ok: true`, `value` **or** `seq`, `ok: false`, `code`, `message` |
| `ble_notify` | `items: [{n, char, handle, ts, data}, ...]` |
| `ble_closed` | `reason`, `message`, `address` (always the last event of a session) |

`services` has exactly the shape `info` returns today (uuid, description,
characteristics with uuid/description/properties), plus each
characteristic's `handle`, so the two cannot drift.

Every box → client event also carries `e`, a per-connection counter in the
order the outbox sent it. python-engineio's client runs each incoming message
on its own thread, so the CLI client holds events until every earlier `e` has
run. This is a precaution: two events that arrive back to back could
otherwise be handled in either order. The Rust client runs its callbacks in
one thread and needs no reordering.

`n` in `ble_notify` is a per-session counter starting at 1. A client that sees
a gap in `n` knows the box dropped data. With the overflow rule below, that
never happens without a `ble_closed` event, but it lets a client check.

`ts` is box wall-clock time (Unix seconds, float), taken in the bleak
notification callback. It is when the notification reached bleak over D-Bus,
not the over-the-air time. It is good for ordering and rough latency, not for
sub-millisecond timing.

### Why explicit `seq` instead of Socket.IO acks

The server runs `async_mode='threading'`, and python-socketio dispatches each
incoming event on its own thread. Two events sent back-to-back by one client
can therefore reach the handler out of order. Requirement 5 (ordered writes)
needs a guarantee, so each session runs a **single executor coroutine on the
bleak loop**. Handler threads hand it `(seq, op)`, and it runs operations
strictly in `seq` order, holding early arrivals until the gap fills. If a
missing `seq` does not arrive within 2 s, the box ends the session with
`protocol_error`. Only our own clients speak this protocol, so a gap is a bug.

Results come back as `ble_result` on the same event stream as `ble_notify`, so
the client sees results and notifications in the order the box produced
them. Acks would put results on a separate callback path. That is fine in
python-socketio but less predictable in `rust_socketio` 0.6, and the ordering
between an ack and a notification would be unclear.

## Session lifecycle

```
            ble_open ok
  (none) ───────────────▶ OPEN ──ble_close──────────────┐
     ▲                     │  ├─ socket disconnect ─────┤
     │                     │  ├─ idle timeout ──────────┤
     │                     │  ├─ link lost (bleak cb) ──┤──▶ CLOSING ──▶ ble_closed ──▶ (none)
     │                     │  ├─ notify buffer overflow ┤
     │                     │  ├─ force release (HTTP) ──┤
     │                     │  └─ server shutdown ───────┘
     └─ ble_open failed (ble_result ok:false; no session registered)
```

- **Open.** `BleakClient(address, timeout=connect_timeout,
  disconnected_callback=...)`, then `connect()`. bleak scans for the address
  first if BlueZ does not already know it, which is part of the same
  timeout. BlueZ runs the MTU exchange and service discovery before
  `connect()` returns, so the reported MTU and services are final.
- **One session per Socket.IO connection**, keyed by `request.sid` as in
  `/uart`. A second `ble_open` on the same connection returns
  `session_active`.
- **Close** always runs the same teardown: stop the executor, `disconnect()`
  the client (bounded to 5 s), release adapter ownership, drop the session,
  and emit `ble_closed{reason}`. `reason` is one of `client`, `disconnected`,
  `idle_timeout`, `overflow`, `protocol_error`, `released`, `shutdown`,
  `timeout` or `bluez_unavailable`.
  Teardown is idempotent, because several of these can race.
- **Link lost.** bleak's `disconnected_callback` fires on the bleak loop.
  Operations still in the queue fail with `disconnected`, the notifications
  already queued are flushed first, then `ble_closed{reason:
  "disconnected"}` is sent.
- **Client gone.** Socket.IO `disconnect` tears the session down. The
  open-vs-disconnect race documented in `uart.py` (`_client_gone`) exists
  here too: a `disconnect` can run before the in-flight `ble_open` registers.
  The session's watchdog therefore checks `manager.is_connected(sid, '/ble')`
  every second, as `/uart` does. A client that stops answering without
  closing its socket (host asleep, VPN dropped) keeps the session for up to
  the engine.io ping window of about 85 s. Force release covers that.
- **Idle timeout.** The session closes after `idle_timeout` seconds with no
  client **operation**. Incoming notifications do not count: they prove the
  peripheral is alive, not that anyone is listening. The default is 300 s and
  the allowed range is 5–3600 s. `ble_ping` exists so a client that only
  listens can stay open. This catches a live process that leaked the session
  (a test that forgot to close it while its socket stays up). The Rust `Drop`
  impl and the CLI's `atexit` handle the ordinary cases.
- **Operation timeouts.** Each operation is bounded on the box: read and
  write 10 s per chunk, subscribe 10 s, open `connect_timeout + 20` s. This
  mirrors `run_bleak`'s widened timeouts, so a wedged BlueZ call returns
  `timeout` instead of hanging the executor. After an operation timeout the
  link state is unknown, so the session is closed as well.
- **Inspection and release.** `GET /ble/sessions` lists open sessions
  (address, holder, opened/idle seconds, MTU, subscriptions).
  `POST /ble/sessions/release` force-closes one or all of them, like
  `POST /uart/sessions/<net>/release`. The CLI exposes these as `lager ble
  sessions` and `lager ble session --force`.

## MTU

What BlueZ and bleak 0.22.2 do, from their source:

- **BlueZ runs the ATT Exchange MTU itself as central** at connect time,
  before service discovery. It offers the `ExchangeMTU` value from
  `/etc/bluetooth/main.conf` `[GATT]`, which defaults to 517 (the LE maximum).
  A peripheral that supports 512 therefore ends up at 512 with no action from
  us. The peripheral never has to start the exchange.
- **The MTU cannot be chosen per connection** through D-Bus. The only control
  is that host-wide `ExchangeMTU` setting.
- **`BleakClient.mtu_size` is not usable as-is on 0.22.2 BlueZ.** It returns
  23 and warns unless the private `_acquire_mtu()` ran first.
  `_acquire_mtu()` calls `AcquireWrite` or `AcquireNotify` on some
  characteristic. `AcquireNotify` turns notifications on for that
  characteristic, and `AcquireWrite` claims the write fd, so it has side
  effects we must not cause before the caller subscribes.
- **The negotiated value is readable without side effects.** BlueZ ≥ 5.62
  exposes an `MTU` property on every `GattCharacteristic1`. bleak surfaces it
  as `char.max_write_without_response_size`, which is `MTU - 3` and reads the
  live property. Ubuntu 22.04 ships BlueZ 5.64 and 24.04 ships 5.72, so every
  supported box host has it.

Plan: after `connect()`, report `mtu = max_write_without_response_size + 3`
from any characteristic, with `mtu_source: "bluez"`. Hosts with an older BlueZ
(no property, bleak falls back to 20) or a peripheral with no characteristics
get `mtu: 23` and `mtu_source: "default"`, so the client can tell that the MTU
was assumed, not measured.

**No write is longer than 512 bytes.** ATT caps an attribute value at 512
bytes, so chunks are `min(mtu - 3, 512)`: at MTU 517, `mtu - 3` would be 514.
Hardware testing found this: BlueZ refuses a 514-byte write with
`org.bluez.Error.InvalidArguments: Invalid Length`, and a peripheral drops a
514-byte write command silently. An unchunked write over 512 bytes is refused
up front with `invalid_argument`.

**`chunk_size` caps the pieces lower still** (1–512), for a peripheral that
accepts less than `mtu - 3` per write. Hardware testing met one: BlueZ 5.64
acting as a GATT server for a D-Bus application refused values over 510 bytes
at MTU 517, although both sides reported the MTU correctly.

**Writes longer than `mtu - 3` matter.** A write-with-response longer than
`mtu - 3` makes BlueZ use the ATT long-write procedure (Prepare Write + Execute
Write). A simple peripheral may not support it. The box does not refuse such a
write, because some peripherals support long writes. The docs and the `chunk`
option point at this.

## Connection parameters

Nothing in BlueZ's D-Bus API or in bleak sets connection interval, latency or
supervision timeout, per connection or at connect time. What the host does, as
far as we can tell from the kernel and BlueZ source:

- **Initial parameters at connect** come from the kernel's defaults. bluetoothd
  sets them host-wide at startup from `main.conf` `[LE]`
  (`MinConnectionInterval`, `MaxConnectionInterval`, `ConnectionLatency`,
  `ConnectionSupervisionTimeout`) through the mgmt "Set Default System
  Configuration" command. The exception is a device for which bluetoothd has
  stored parameters under `/var/lib/bluetooth/<adapter>/<device>/info`
  `[ConnectionParameters]`. Those are loaded and used at connect instead.
- **After connecting, Linux as central does not start a connection-parameter
  update on its own.** It answers a peripheral's L2CAP Connection Parameter
  Update Request. The controller answers LL_CONNECTION_PARAM_REQ. So a
  peripheral that never asks should never see one from the box.
- **Other link-layer procedures the controller may start by itself** at
  connect are Data Length Update (LL_LENGTH_REQ), PHY update (LL_PHY_REQ) and
  feature exchange. These depend on the controller, not on bleak or BlueZ.

Hardware result (box as central, BlueZ 5.64, Realtek controller; a second
BlueZ host as peripheral), from a btmon capture over a full session: no
Connection Update or Connection Parameter Request in either direction, and
no PHY update. One Exchange MTU each way (the peripheral's BlueZ also runs a
GATT client), the remote-features read, and **one LE Data Length Change**,
although the box host sent no LE Set Data Length command. The controller or
the peripheral started that Data Length Update; an air sniffer is needed to
say which. A peripheral that cannot handle a Data Length Update would need
that checked.

Decision for v1: **Lager does not change connection parameters**, and the docs
say so and name the host-wide `main.conf` knobs. The hardware test plan
records the real link-layer traffic with `btmon` on the box host, so we know
rather than assume what the box's controller sends. If a peripheral needs
specific initial parameters, a later change can manage the `[LE]` section in
`lager install`/`lager update`. That is host-wide and needs a bluetoothd
restart, so it is out of scope here.

## Notifications

- `ble_subscribe` checks that the characteristic has `notify` or `indicate`
  (else `not_permitted`), then calls `start_notify`. bleak registers the
  callback **before** it asks BlueZ to write the CCCD (`StartNotify`), so a
  notification the peripheral sends the instant the CCCD is written is
  captured. When a characteristic supports both, BlueZ picks notify.
- **Box buffer.** The bleak callback appends
  `{n, char, handle, ts, data}` to the session's queue. An emitter thread
  drains **everything currently queued** into one `ble_notify` event and emits
  it right away, with no timer and none of UART's 50 ms batching. This keeps
  per-event overhead low during a burst without delaying a lone
  notification. An item waits in the queue only while the previous emit is
  running.
- **Limit and overflow.** The queue holds at most **4 MiB of payload or
  16384 notifications**. It only fills if the socket stops draining while the
  peripheral keeps sending. On overflow the session **ends** with
  `ble_closed{reason: "overflow"}`, after flushing what was queued. It never
  drops data silently. The callers layer protocols on this stream, and a
  silent gap would corrupt them in a way that is hard to diagnose. A loud
  failure is recoverable.
- **Client buffer.** The Rust session and the CLI client keep everything they
  receive until the caller reads it (the same as `Uart`), so notifications
  that arrive between reads, including unsolicited ones, are never lost on
  the client either.
- **Ordering and EATT.** Order is guaranteed per ATT bearer only. Two BlueZ
  5.64 hosts negotiate Enhanced ATT (EATT, L2CAP PSM 0x27) and the server then
  spreads notifications over several bearers, using Multiple Handle Value
  Notifications (opcode 0x23). Hardware testing between two boxes saw a
  500-notification burst arrive with a 64-notification run behind the next
  437, and a btmon capture on the central confirmed the EATT channels. Nothing
  above the radio reorders: `n` is assigned in bleak's callback order, and the
  session keeps that order end to end. A single-bearer peripheral (most
  embedded firmware) delivers in order. Pinning the box host to one bearer
  (`[GATT] Channels = 1` in `main.conf`) is a possible follow-up, managed like
  the `[LE]` connection-parameter keys.
- **Throughput caveat.** bleak on BlueZ receives notifications as D-Bus
  `PropertiesChanged` signals, one per notification. That is fine for request
  and response traffic. The hardware plan includes a burst test with a
  counter in the payload to confirm nothing is dropped below the box
  (between the controller and bleak) at the rates we care about.

## Writes

- Check the characteristic's properties first: `write` for with-response,
  `write-without-response` for without. Else `not_permitted`, with the
  properties the characteristic does have.
- `chunk: false` sends one `write_gatt_char` (the long-write caveat above
  applies). `chunk: true` splits the payload into pieces of at most
  `mtu - 3` bytes and writes them in order on the session executor. It waits
  for each with-response write to complete before sending the next. Nothing
  else from that session can run between the chunks, so the chunks of one
  message are never interleaved with another operation.
- Ordering across operations comes from the `seq` executor. A caller may
  pipeline writes. The Rust `write()` still blocks for its result by default,
  so an error surfaces at the call that caused it.
- Payload limit per `ble_write` is 64 KiB. That is well below the Socket.IO
  message cap and far above any realistic GATT message.

## Characteristic addressing

Operations take `char` (UUID) or `handle`. A UUID that appears more than once
in the GATT table (the same characteristic UUID in two services) returns
`ambiguous_characteristic` and lists the candidate handles. The caller then
uses `handle`. An unknown UUID or handle returns `unknown_characteristic`.

## Locking

The box has one adapter. Every existing BLE and BluFi operation holds
`bt_adapter_lock` for its duration and blocks without a timeout while
waiting for it.

**Options considered**

1. Hold `bt_adapter_lock` for the whole session. Simple, but every
   `lager ble scan` and BluFi call would hang, possibly for the rest of the
   session, with no message saying why.
2. Take `bt_adapter_lock` per session operation only. Scans and BluFi
   connections could then run in the middle of a session. The adapter can
   usually do that, but scanning during a connection disturbs link timing on
   some controllers. BluFi could also connect a second device underneath a
   peripheral that expects exclusive use of the host.

**Decision.** A session takes **adapter ownership** for its lifetime: it
acquires `bt_adapter_lock` at open, holds it until teardown, and registers as
the adapter holder (`ble.set_adapter_holder`):

- `ble_open` fails fast with `adapter_busy` while another session is open.
  The error names the holder label, the address and the idle time.
- While a session is open, `/ble/command` (scan/info/connect/disconnect) and
  `/blufi/command` return **409 `adapter_busy`** right away with the same
  details, instead of queueing behind the session.
- A scan or BluFi call already running when the session opens finishes
  first: `ble_open` acquires the lock with a bounded wait (the connect
  timeout) and fails with `adapter_busy` rather than hanging. It waits on an
  executor thread, never on the bleak loop, because a `/ble/command` request
  holding the lock is itself waiting on that loop in `run_bleak`.
- One-shot handlers call `ble.acquire_adapter()`, which polls the lock in
  0.25 s slices and returns the holder's description as soon as a session is
  found to hold it. They still queue normally behind each other.
- **v1 is one session per box.** The data model (sessions keyed by sid, with
  the address in every payload) does not rule out several sessions to
  different addresses later. Ownership would then become per address, with
  scans refused while any session is open.

**Box locks and multiple users.** The box lock (`lager boxes lock`,
auto-lock) is advisory and enforced by clients. The box never checks it on a
request, and `/uart` does not either. So:

- `lager ble session` uses `resolve_box_locked`, like every other `lager ble`
  command. It holds the auto-lock (with heartbeat) for as long as the command
  runs.
- The Rust crate does not auto-lock (it does not for UART either). The docs
  tell test authors to use `LagerBox::lock()` if they share a box.
- Across users, the adapter-ownership check is the hard guarantee. Two people
  cannot both hold the radio, and the loser gets a message that says who has
  it and how to take it over (`lager ble session --force`).

## Errors

Every failure is a `ble_result{ok: false, code, message}` for an operation, or
`ble_closed{reason}` when the session ends. Codes:

| Code | When | Session |
|---|---|---|
| `device_not_found` | bleak's `BleakDeviceNotFoundError`: address not seen during the connect scan | not opened |
| `connect_failed` | connect raised, or timed out (`message` says which) | not opened |
| `adapter_busy` | another session, or a queued BLE/BluFi operation, holds the adapter | not opened |
| `bluez_unavailable` | D-Bus `ServiceUnknown` for `org.bluez`; `message` is `BLUEZ_UNAVAILABLE_MESSAGE` | not opened / closed |
| `session_active` | `ble_open` on a connection that already has a session | unchanged |
| `not_open` | an operation with no open session | none |
| `unknown_characteristic` | UUID or handle not in the GATT table | stays open |
| `ambiguous_characteristic` | UUID appears more than once; `message` lists the handles | stays open |
| `not_permitted` | property missing (write, write-without-response, notify/indicate, read), or BlueZ `NotPermitted`/`NotSupported`/`NotAuthorized` | stays open |
| `invalid_argument` | bad address, hex, timeout range, payload too large | stays open |
| `disconnected` | link lost while the operation ran; `ble_closed{disconnected}` follows | closed |
| `timeout` | an operation exceeded its box-side bound | closed |
| `protocol_error` | `seq` gap not filled within 2 s | closed |
| `ble_error` | any other bleak/BlueZ error, with the text | stays open |

CLI: every code maps to a `LagerError`. `bluez_unavailable` reuses the
`BLUEZ_UNAVAILABLE_MESSAGE` remedy (the unit test that pins the two strings
together gets extended). A failed Socket.IO connect goes through
`connection_error`.

Rust: a new `Error::Ble { kind: BleErrorKind, message: String }` variant.
`Error` is `#[non_exhaustive]`, so adding it is not a breaking change.
`BleErrorKind` is also `#[non_exhaustive]` and mirrors the codes above.
Transport failures stay `Error::Connection`/`Error::Stream`, and client-side
waits stay `Error::Timeout`.

## Latency

- One persistent WebSocket through the gateway, authenticated once at the
  handshake. After that, each operation costs one round trip plus BLE time.
- Notifications are pushed as they arrive, with no polling and no batching
  timer.
- Budget for a two-step exchange (write request, receive the reply
  notifications, write again, receive again): 2 × (1 gateway RTT for the write
  + BLE time for the write) + 2 × (½ RTT for the notifications). With a
  gateway RTT of 150 ms and a 30–50 ms connection interval that comes to
  about 1 s, well inside the 5 s budget. `open` (scan + connect + discovery,
  usually 1–5 s) is paid once per session and is not part of the exchange.
- The hardware plan measures the real number: the CLI's one-shot mode prints
  the time from the write result to the first notification.

## Radio presence and address type

Added after a client listed what its BLE tests need:

- **`/ble/command` action `adapter`** (`lager ble adapter`, lager-net
  `Ble::adapter()`): lists BlueZ's adapters with address and power state and
  answers `available` plus a `reason` (no adapter, powered off, no BlueZ). It
  takes no adapter lock and is never refused by a session, so a test can
  check it first and skip on a box without a radio. The CLI exits 1 when BLE
  is unavailable, and the command is on the read-only list for box locks.
- **Scan results carry `address_type`** (`public`/`random`, from BlueZ's
  `Device1.AddressType`) **and `random_type`** (`static`, `resolvable`,
  `non-resolvable`, from the address's two most significant bits), so a test
  can assert a static random address. BluFi scans and older boxes leave both
  null.

## On-box scripts (`lager python`)

Scripts run in the box container next to the `:9000` server, so
`lager.ble.Session` (`box/lager/protocols/ble/session.py`) is a client of the
same `/ble` namespace over `127.0.0.1:9000`, and `lager.ble.adapter()` /
`scan()` post to `/ble/command`. Scripts therefore share the adapter with
remote sessions and `lager ble`, and a script that dies releases its session
when its socket closes. The API mirrors lager-net's `BleSession` (`recv`
raises `TimeoutError`; after the end, buffered notifications come first, then
`SessionClosed`). It duplicates the CLI's `BLESessionClient` (~200 lines):
the box image cannot import the CLI package, and the CLI wheel does not ship
box code.

The older `Central`/`Client` still drive bleak directly and stay outside
adapter sharing; `Central.connect`/`pair` now use `ble_target`
(`protocols/ble/target.py`, shared with the HTTP handlers).

## Security and pairing

No pairing API in v1. If a characteristic needs encryption, BlueZ reacts to
the ATT "insufficient authentication/encryption" error by starting pairing on
its own (Just Works when no agent is registered). The operation then either
succeeds or fails with `not_permitted`. The docs state this, so a peripheral
that must never be paired is not surprised by it.

## Python CLI

New module `cli/commands/communication/ble_session_client.py` with a
`BLESessionClient` class. It is the counterpart of `UARTWebSocketClient`: it
owns the python-socketio client, the gateway auth headers
(`auth_headers_for_url`/`ws_handshake_recovery`), `seq`, and the queue of
results and notifications. It works as a library for other CLI code as well
as for the command below.

`lager ble session ADDRESS [--box BOX]`, **interactive** (needs a TTY):

```
ble> info
ble> mtu
ble> sub 12345678-1234-5678-1234-56789abcdef1
ble> write 12345678-1234-5678-1234-56789abcdef2 0102030405 [--no-response] [--chunk]
ble> read 12345678-1234-5678-1234-56789abcdef3
ble> unsub 12345678-1234-5678-1234-56789abcdef1
ble> close
```

Notifications print between prompts as `[ts] <uuid> <hex>`.

**One-shot mode**, for scripts and the hardware plan. It runs whenever any of
these flags is given:

```
lager ble session AA:BB:CC:DD:EE:01 --box <BOX> \
    --subscribe <UUID> [--subscribe <UUID> ...] \
    --write <UUID>:<HEX> [--write ...] [--no-response] [--chunk] \
    --read <UUID> \
    --listen <SECONDS> \
    [--idle-timeout S] [--connect-timeout S] [--json]
```

This mode opens the session, runs the subscribes, then the writes (in the order
given), then the reads, prints notifications for `--listen` seconds, and closes.
(Click cannot keep the relative order of two repeatable options, so writes and
reads are not interleaved.)
`--json` prints one JSON object per line (open result, each result, each
notification, close) for machine checking.

Plus `lager ble sessions` (list), `lager ble sessions --release [--address A]`,
and `lager ble session --force` (release, then continue). Releasing takes the
box lock (`resolve_box_locked`); listing does not.

The on-box Python reference (`reference/python/ble.mdx`) documents
`lager.ble.Client`, which already has read/write/notify for on-box scripts.
That page gets a short section that points remote callers at sessions and
says on-box scripts do not take part in adapter ownership.

## Rust `lager-net`

New feature `ble-session = ["blocking", "dep:rust_socketio"]`, a new module
`src/nets/ble_session.rs`, and the codec shared through `nets/sio.rs`.

```rust
let lager = LagerBox::connect("my-box")?;
let mut s = lager.ble_session("AA:BB:CC:DD:EE:01", BleSessionOptions::default())?;

assert!(s.mtu() >= 247);                        // negotiated ATT MTU
let svc = s.services();                         // &[BleService], same type as Ble::info
s.subscribe(NOTIFY_UUID)?;                      // writes the CCCD; before any write
s.write(WRITE_UUID, &request, WriteOptions::chunked())?;   // ≤ mtu-3 per ATT write, in order
let n: BleNotification = s.recv(Duration::from_secs(2))?; // blocks; Error::Timeout if none
while let Some(n) = s.try_recv()? { /* drain without blocking */ }
let v = s.read(READ_UUID)?;
s.close()?;                                     // also on Drop
```

- `BleSessionOptions { connect_timeout, idle_timeout, op_timeout, holder }`.
  `op_timeout` is the client-side wait for each `ble_result`. Its default is
  box bound + 5 s.
- `WriteOptions { response: bool, chunk: bool }` with `::default()` (with
  response, no chunking), `::without_response()` and `::chunked()`.
- `BleNotification { seq: u64, char_uuid: String, handle: u16, timestamp: f64,
  data: Vec<u8> }`.
- `mtu() -> u16`, `max_write_len() -> usize` (`mtu - 3`), `mtu_is_measured() -> bool`.
- `subscribe`, `unsubscribe`, `read`, `write`, `ping` and `info` block on
  their `ble_result`. Notifications that arrive meanwhile go to the internal
  buffer, like `Uart::pump`.
- After the link drops, `recv`/`try_recv` **first return every buffered
  notification**. Only then do they return `Error::Ble { kind:
  Disconnected, .. }`. The caller never loses the notifications that came
  before the drop.
- Handle-based variants (`subscribe_handle`, `write_handle`, …) cover
  duplicate UUIDs.
- Structure is the same as `Uart`: `socket: Option<Client>`, an mpsc channel
  fed by the event callbacks, and `shutdown()` from both `close()` and `Drop`.
  Payload builders, event parsing and the buffer/state logic are pure
  functions. Tests call them by feeding events into the channel with no
  socket.

## Tests

**Box unit tests** (`test/unit/box/test_ble_session.py`). They use a fake
`BleakClient` that scripts connect results, a services table (including a
duplicate UUID), MTU property values, notification bursts and a disconnect
callback. The Flask-SocketIO `test_client` on the `/ble` namespace drives
them. Cases:

- Open success reports MTU from the property; the default path reports
  `mtu_source: "default"`.
- Device not found, connect timeout and BlueZ `ServiceUnknown` each give the
  right code and message.
- Subscribe before write; a notification fired during `start_notify` is
  delivered.
- Chunking splits into `mtu - 3` pieces in order; with-response chunks wait
  for each other.
- Out-of-order `seq` is reordered; an unfilled gap gives `protocol_error`.
- Unknown and ambiguous characteristics; `not_permitted` for each property.
- Link loss flushes queued notifications, fails the in-flight operation with
  `disconnected`, then sends `ble_closed`.
- Idle timeout: notifications do not reset it, `ble_ping` does.
- Overflow closes the session and never drops data silently; `n` has no gaps.
- Socket disconnect tears down; the `_client_gone` race is covered.
- Adapter ownership: `/ble/command` and `/blufi/command` get 409 during a
  session; a second `ble_open` gets `adapter_busy`.
- `GET /ble/sessions` and `POST /ble/sessions/release`.

**CLI unit tests** (`test/unit/cli/test_ble_session.py`) with a fake
socketio client: argument parsing, the one-shot ordering, `--json` output,
error code → `LagerError` mapping, `--force` calls release, and the pinned
BlueZ message.

**Rust**: unit tests in `ble_session.rs` for payload builders, `ble_result`
and `ble_notify` parsing, ordered buffering, disconnect-after-drain and error
mapping. `httpmock` cannot serve Socket.IO, so these are pure tests like the
ones in `rtt.rs`/`sio.rs`. The `/ble/sessions` HTTP helpers get `httpmock`
tests. CI builds `--features ble-session` and `--no-default-features
--features async`.

`test/COVERAGE.md` rows and counts are updated for each new file (and the
`box` row checked by hand; see the platform-gated caveat in that file).

## Docs

- `docs/source/reference/cli/ble.mdx`: `session`, `sessions`, one-shot flags,
  errors, locking, MTU and connection-parameter notes.
- `docs/source/reference/rust/ble.mdx`: a `BleSession` section (feature flag,
  API, example, the notes above).
- `docs/source/reference/python/ble.mdx`: the short section described above.
- The matching `docs/source/zh/...` pages, because `tools/check_translations.py`
  fails on untranslated English edits.
- `CHANGELOG.md` under `## [Unreleased]`. `cli/__init__.py` is not bumped.
- lager-rs: `CHANGELOG.md` and `README.md` feature table.

## Hardware test plan (draft; finalized after implementation)

This needs a box with a BLE peripheral in range whose firmware (a) exposes a
notify characteristic and a write characteristic, (b) sends several
notifications in reply to one write, (c) sends a notification by itself on a
timer or button, and (d) can be reset or powered off to force a disconnect. A
dev-kit sample (a UART-over-BLE echo service) covers (a)–(c).
`<ADDR>`/`<NOTIFY>`/`<WRITE>` are placeholders.

1. `lager ble scan --box <BOX> --timeout 5` — peripheral is visible.
2. `lager ble session <ADDR> --box <BOX> --json --listen 0` — opens and
   closes; check `mtu` (expect 247–517 depending on the peripheral) and
   `mtu_source: "bluez"`.
3. `lager ble session <ADDR> --box <BOX> --subscribe <NOTIFY> --write <WRITE>:<600-byte hex> --chunk --listen 3 --json`
   — `chunks` = ceil(600 / (mtu−3)); the multi-notification reply arrives in
   order with increasing `n`; note the write→first-notification time.
4. The same with `--no-response`.
5. `lager ble session <ADDR> --box <BOX> --subscribe <NOTIFY> --listen 30 --json`
   — unsolicited notifications arrive with no write.
6. Run step 5, then reset/power off the peripheral mid-listen (e.g. `lager
   supply <NET> disable --box <BOX>`) — buffered notifications are printed,
   then `ble_closed` with reason `disconnected`, and the command exits
   non-zero with a clear message.
7. `lager ble session <ADDR> --box <BOX> --subscribe <NOTIFY> --idle-timeout 10 --listen 20 --json`
   — closes at about 10 s with `idle_timeout`.
8. Terminal A: `lager ble session <ADDR> --box <BOX>` (interactive, left open).
   Terminal B: `lager ble scan --box <BOX>` → immediate `adapter_busy`
   naming A's session. Then `lager ble sessions --box <BOX>`, then `lager ble
   session <ADDR> --box <BOX> --force --listen 0` → takes over.
9. Kill terminal A's process with `kill -9` and run `lager ble sessions --box
   <BOX>` repeatedly — the session is released within about 85 s (ping
   window).
10. Link-layer check: in a second terminal, `lager ssh --box <BOX> -- sudo
    btmon -t` while repeating step 3. Confirm the MTU exchange and look for
    any LE Connection Update / LL_CONNECTION_PARAM_REQ / PHY / Data Length
    procedure started by the box.
11. Burst check: have the peripheral send 500 notifications with a counter as
    fast as it can and confirm there are no counter gaps in the `--json`
    output.

## Decisions (review, 2026-09-30)

All seven proposals were accepted as written:

1. Adapter ownership: scans and BluFi fail fast with 409 while a session is open.
2. One session per box in v1.
3. Overflow ends the session (reason `overflow`); nothing is dropped silently.
4. Idle timeout 300 s by default; notifications do not count as activity.
5. Connection parameters are documented, not managed.
6. Rust feature `ble-session`. Instead of comparing versions, the box
   advertises `capabilities.bleSession` in `GET /status`, and
   `LagerBox::ble_session` returns `Error::UnsupportedByBox` when it is
   absent. The CLI maps the Socket.IO "namespace failed" error to the same
   advice.
7. This doc stays in the PR, in `docs/reference/`.

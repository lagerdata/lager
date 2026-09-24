# Lager Gateway Auth Contract

**Version: 1** · Status: stable · Last updated: 2026-09-24

This document is the normative specification of the authentication contract
between Lager clients and boxes fronted by an authenticating reverse proxy
(a *gateway*). Anything that fronts a Lager box and honors this contract can
gate it — the gateway and auth server are deliberately not part of Lager
itself, so control planes built on Lager (e.g. enterprise deployments) own
identity while every Lager client works against them unchanged.

Conforming implementations of the client side:

- `cli/gateway_auth.py` (Python CLI; test suite `test/unit/cli/test_gateway_auth.py`)
- `lager-rs/src/auth.rs` (Rust crate; test suite `lager-rs/tests/gateway_auth.rs`)

If an implementation and this document disagree, this document wins; fix the
implementation or amend the spec with a version bump (see
[Versioning](#versioning)).

The key words MUST, MUST NOT, SHOULD, and MAY are used as in RFC 2119.

---

## 1. Actors

| Actor | Role |
| --- | --- |
| **Box** | The Lager hardware service (`:9000` API, `:8765` debug service, Socket.IO namespaces). Auth-unaware; never sees or checks credentials. The box also runs an MCP server on `:8100`, which is **not** part of the gateway-fronted surface — see §6.4. |
| **Gateway** | Reverse proxy in front of the box. Verifies bearer tokens and either forwards traffic or denies it. |
| **Auth server** | Issues and refreshes access tokens. Discovered by clients via the gateway's denial header. |
| **Client** | Anything speaking to the box: the Lager CLI, the `lager-net` Rust crate, or third-party code. |

A plain (ungated) box has no gateway. The contract is designed so that
against a plain box **no part of it runs**: no header is attached, no store
is consulted beyond a lookup, and ordinary application 401/403 responses are
never intercepted.

## 2. Discovery header

The gateway advertises its auth server on every denial with a response
header:

```
X-Gateway-Auth-Url: <auth server base URL>
```

- The value is the auth server's base URL, no trailing slash
  (e.g. `https://auth.example.com`).
- A response is a **gateway denial** if and only if its status is
  **401, 403, or 503** *and* it carries this header. Clients MUST NOT treat
  a 401/403/503 without the header as a gateway denial — those are ordinary
  application responses from the box and must pass through untouched.
- The gateway MUST attach the header to 401 denials and SHOULD attach it to
  403 and 503 denials.

### Denial status semantics

| Status | Meaning | Required client behavior |
| --- | --- | --- |
| 401 | No credential, or the credential was rejected (expired/revoked). | Resolve a credential and retry (§6), or fail with an actionable "log in to `<auth url>`" error. |
| 403 | Credential is valid but the account has no access grant for this box. | Fail; tell the user to request access. Do not retry. |
| 503 | The gateway could not reach its auth server to verify. | Fail; tell the user to retry shortly. Do not retry automatically. |

## 3. Auth server HTTP API

All endpoints are JSON over HTTPS, rooted at the discovered base URL.
Clients use a **10-second timeout** for auth-server requests.

### 3.1 Login

```
POST <url>/api/auth/login
{"email": "...", "password": "..."}
```

- `200` → `{"accessToken": "<jwt>", "user": {...}}`. The response MAY set
  cookies (typically an httpOnly refresh token); clients MUST capture and
  store them (§5) — they are the refresh credential.
- `200` with `{"mfaRequired": true, "mfaToken": "..."}` → the account needs
  a second factor; continue with §3.2.
- Non-200 → login failed; the body's `message` or `error` field, when
  present, is the human-readable reason.

### 3.2 MFA step

```
POST <url>/api/auth/login/mfa
{"mfaToken": "<from login>", "code": "<TOTP or backup code>"}
```

Same success/cookie semantics as §3.1.

### 3.3 Refresh

```
POST <url>/api/auth/refresh
Cookie: <the cookies captured at login, plus any later rotations>
```

- `200` → `{"accessToken": "<jwt>"}`. The response MAY rotate cookies via
  `Set-Cookie`; clients MUST merge rotated cookies **over** the stored ones
  (keep cookies the server did not re-send) and persist the result.
- Any failure (non-200, network error, missing `accessToken`) → the refresh
  is simply unusable; clients fall back to "no credential" behavior. A
  failed refresh MUST NOT clear the stored session.

## 4. Tokens

- The credential is an **access token** sent as `Authorization: Bearer
  <token>` on requests to the box.
- Tokens are opaque to clients, with one carve-out: when the token parses as
  a JWT, clients MAY read its `exp` claim **without verifying the
  signature** to decide when to refresh proactively. The gateway is the only
  verifier.
- **Expiry margin:** a token is treated as expired when `exp` is within
  **60 seconds** of the current time.
- A token that does not parse as a JWT is treated as already expired: a
  refresh is attempted first, and if no refresh credential exists the token
  is sent as-is (the gateway decides).

## 5. Client token store

Clients share one on-disk session store so that a single `lager login`
serves every client on the machine.

- **Path:** `~/.lager_gateway_auth`, overridable with the
  `LAGER_GATEWAY_AUTH_FILE` environment variable.
- **Permissions:** writers MUST set mode `0600` (best-effort on non-POSIX).
- **Format** (JSON):

```json
{
  "boxes": {
    "<box host>": "<auth server base URL>"
  },
  "authServers": {
    "<auth server base URL>": {
      "accessToken": "<jwt>",
      "cookies": { "<name>": "<value>" }
    }
  }
}
```

- `boxes` maps a box **hostname or IP without port** (e.g. `192.168.1.42`,
  `box.tailnet.ts.net`) to the auth server learned from its discovery
  header. Recording this mapping is what makes proactive attach (§6) work
  on later runs and in other clients.
- `authServers` is keyed by auth server URL so one machine can hold
  sessions for boxes gated by different deployments.
- `cookies` is a flat name→value map: exactly what the auth server set at
  login, updated by refresh rotations (§3.3).
- Readers MUST tolerate a missing or unparseable file (treat as `{}`).
  Writers MUST load-modify-save so that unknown top-level keys and unknown
  fields inside entries are preserved — future spec versions may add
  fields.

## 6. Request flow on the client

### 6.1 Credential sources and precedence

1. **Pinned token** — an explicitly supplied token (builder API, or the
   `LAGER_GATEWAY_TOKEN` environment variable). Attached verbatim to every
   request. Never refreshed, never written to the store, and never replaced
   by store resolution. If the gateway rejects it, the call fails
   immediately (no retry). Intended for CI.
2. **Session store** — the token for the box's auth server (from the
   `boxes` mapping), refreshed per §3.3 when stale.

### 6.2 Proactive attach

If the box is already known to be gated (a `boxes` entry exists), clients
MUST attach the bearer token on the first request rather than waiting for a
denial. If the box is not in the store, the first request goes out bare —
this is what keeps plain boxes zero-overhead.

A pinned token (§6.1) satisfies this rule for free: it goes on every request,
so the first contact with a box no store has ever heard of already carries
it, and no `boxes` entry is consulted.

### 6.3 Handling a denial

On a gateway denial (§2):

1. Record the box→auth-server mapping in the store — except when a pinned
   token is in play. A pinned client never reads that mapping (§6.2), so the
   entry buys nothing, and CI is exactly where writing it costs: a
   self-hosted runner keeps its filesystem between jobs, and the entry
   outlives the address it names. A pinned client writes no store at all.
2. For a **401** when not using a pinned token: resolve a credential from
   the store — refreshing if stale, and never re-sending the exact token
   the gateway just rejected — then retry the request **once** within the
   same call. Clients SHOULD do this in-call retry; a client MAY instead
   fail with an actionable error that tells the user to re-run (the mapping
   recorded in step 1 makes the re-run authenticate). A second denial after
   the retry is terminal.
3. For **403**/**503**, or a 401 with no resolvable credential: fail with
   an error that names the auth server (`lager login <url>` is the fix for
   401).

### 6.4 Coverage

The bearer token MUST be attached to **all** traffic destined for the box
host, not just the `:9000` API — the gateway fronts everything:

- the debug service (`:8765`), including streaming (RTT) requests;
- WebSocket and Socket.IO handshakes (e.g. the `/uart` namespace), via the
  `Authorization` header on the opening HTTP request;
- any other HTTP endpoint on the box host.

**Except `:8100` (MCP), which the gateway MUST NOT forward.** The MCP server is
an in-fabric service, reachable over the local network, Tailscale or the
corporate VPN and no further. It is deliberately excluded rather than
overlooked, for two reasons:

- By default it performs no authentication of its own, and it disables
  DNS-rebinding protection on purpose (`box/lager/mcp/server.py`) because the
  box is reached at an arbitrary LAN address that cannot be known ahead of time.
  That is a reasonable posture for a service on an internal fabric and a poor
  one for a published service.

  Its one credential is optional and box-local: a static bearer token an
  operator turns on with `lager box-config mcp-token` (`box/lager/mcp/auth.py`).
  That token is **not** the credential this contract describes. The auth server
  does not mint it, the gateway cannot verify it, it never expires, and it
  crosses the network in cleartext HTTP. It narrows who on the fabric can use
  the server; it does not make `:8100` fit to publish, and it does not change
  this exclusion.
- Its tool surface is not read-only. `LAGER_MCP_ALLOW_CONTROL` adds hardware
  control, and `LAGER_MCP_ALLOW_EXEC` adds `box_exec`, `read_file`,
  `write_file` and `list_dir` — arbitrary command execution and file writes,
  available to anything that can reach the port. Publishing `:8100` on a box
  with either gate on would put remote code execution on whatever network the
  gateway fronts.

Should MCP ever need to be reachable through the gateway, that is a change to
this contract: `:8100` joins the coverage list below, the gateway enforces the
same bearer policy it applies to `:9000`, and the version bumps.

## 7. Gateway requirements

A conforming gateway:

- MUST verify `Authorization: Bearer` tokens minted by its auth server and
  forward authenticated, authorized traffic to the box unmodified.
- MUST deny unauthenticated traffic with 401 + the discovery header (§2).
- SHOULD deny "authenticated but not granted" with 403 and "cannot verify"
  with 503, both carrying the discovery header.
- MUST NOT strip or alter the header on the box's own responses. (The box
  never emits `X-Gateway-Auth-Url`, so there is no collision.)
- MUST cover every box port it exposes (9000, 8765, WebSocket upgrades)
  with the same policy — clients assume one credential works box-wide.
- MUST NOT forward `:8100` (MCP). It is an in-fabric service, and its only
  credential is an optional box-local token the gateway cannot verify (§6.4).
- MAY offer the box's raw-TCP debug ports through `CONNECT` tunnels on
  `:8765` (§10), under the same bearer policy. It MUST NOT publish those
  ports unauthenticated instead.

## 8. Environment variables (client side)

| Variable | Meaning |
| --- | --- |
| `LAGER_GATEWAY_TOKEN` | Pinned bearer token (§6.1). Highest-precedence credential after an explicit builder token. |
| `LAGER_GATEWAY_AUTH_FILE` | Overrides the token store path (§5). |

## 9. Versioning

This contract is versioned by the integer at the top of this file.

- **Additive changes** (new optional store fields, new denial statuses a
  client may ignore) do not bump the version but MUST be noted in the
  changelog below.
- **Breaking changes** (header rename, store schema change, new required
  endpoint, changed retry semantics) bump the version and MUST keep the
  previous behavior working for at least one minor release of the CLI and
  the Rust crate.
- Changes MUST land with matching updates to both reference
  implementations and their test suites before this document's version or
  changelog is updated.

### Changelog

- **v1** (2026-07-22): initial written spec, documenting the contract as
  shipped in CLI ≥ 0.32.0 (`lager login`) and lager-net 0.2.0.
- **v1** (2026-08-26): recorded the `:8100` (MCP) decision in §6.4 and §7.
  Additive clarification of an unstated boundary; no version bump per §9.
- **v1** (2026-09-10): the Python CLI now implements §6.1 pinned tokens
  (`LAGER_GATEWAY_TOKEN`), which until now only lager-rs did.
- **v1** (2026-09-18): §6.4 and §7 name the box MCP server's optional box-local
  bearer token and say why it changes nothing here: `:8100` stays excluded.
  Additive clarification; no version bump per §9.

  Writing that client found two places where §6.1 and the sections below it
  disagreed, both now stated: §6.2 said the first request to an unknown box
  goes out bare, and §6.3 said a denial records the mapping
  *unconditionally* — neither of which can hold for a token that §6.1
  already attaches to every request and keeps out of the store. Both are
  clarifications of an interaction the spec left unstated, not new
  requirements, so no version bump per §9.

  §6.3 as clarified also caught a real divergence: lager-rs *records* the
  mapping whatever the credential, so a Rust CI job holding only a pinned
  token leaves a token store behind exactly as the Python one used to. That
  is a known non-conformance rather than an open spec question — the fix is
  one guard in `GatewayAuth::learn_auth_server`, written and tested in
  lagerdata/lager-rs#7, which is open and not merged.

  §9 asks that a change reach both reference implementations. Until #7
  merges, §6.3's pinned exception is met by the Python client and pending in
  the Rust one, and this note is the record of that gap. A third-party
  client should implement §6.3 as written: the requirement is not in
  question, only one implementation's conformance with it.
- **v1** (2026-09-24): §10 specifies debug tunnels, an HTTP `CONNECT` on the
  debug-service port that reaches the box's raw-TCP debug ports through the
  gateway. Optional for gateways and additive for clients, so no version
  bump per §9. The Python CLI implements the client side
  (`cli/gateway_tunnel.py`, tests in `test/unit/cli/test_gateway_tunnel.py`).
  lager-rs has no counterpart to implement: it drives debug nets only over
  the HTTP debug service and Socket.IO, and never opens a raw debug port.

## 10. Debug tunnels (optional)

The debug servers on a box speak raw TCP, not HTTP: GDB, OpenOCD's telnet and
TCL interfaces, and RTT. A gateway can only check a bearer token on HTTP, so a
box it fronts does not publish those ports, and a debugger on the client
machine cannot reach them directly. A gateway MAY instead offer them through
an HTTP `CONNECT` tunnel on the debug-service port. Supporting it is optional
for gateways, and using it is optional for clients.

### 10.1 Handshake

The client opens a TCP connection to the box host on port **8765** and sends
a `CONNECT` request carrying the same credential as any other request (§6):

```
CONNECT <host>:<port> HTTP/1.1
Host: <host>:<port>
Authorization: Bearer <token>

```

- The request target MAY be the standard authority form `<host>:<port>` or
  the bare port `<port>`. The host part is **ignored**: a tunnel only ever
  reaches the named port in the Lager container, never another host.
- `Authorization` follows §6.1 and §6.2 exactly: a pinned token, or the
  store's token for a known-gated box, or nothing for a box not yet known to
  be gated. It is resolved **per tunnel**. A client MUST NOT reuse a token
  resolved for an earlier tunnel without checking its expiry (§4), because
  one debugging session can outlive many tokens.
- A gateway MUST authorize each tunnel on its own, never from a cached
  verdict, so a client MAY open one tunnel per debugger connection.

### 10.2 Responses

| Status | Headers | Meaning |
| --- | --- | --- |
| `200 Connection Established` | — | Tunnel open. Every byte after the response head is raw TCP to that port, in both directions. Bytes MAY follow the head in the same segment; a client MUST forward them. |
| `401` | discovery header | No credential, or a rejected one. A gateway denial (§2), handled per §6.3, including the in-call retry on first contact. |
| `403` | discovery header | Signed in but not authorized for this box. A gateway denial (§2). |
| `403` | **no** discovery header, `text/plain` | The port is not one the gateway tunnels (§10.3). Not a denial: the client records nothing. |
| `502` | `text/plain` | Nothing is listening on that port in the Lager container, for example a debug server that has not started. |
| `503` | discovery header | The gateway cannot reach its auth server. A gateway denial (§2). |

Any other answer means the service on 8765 does not support tunnels (§10.4).

### 10.3 Tunnelable ports

A gateway MUST refuse (`403` without the discovery header) every port
outside the debug ranges, and SHOULD accept all of these:

| Service | Ports |
| --- | --- |
| GDB server | 2331–2342 |
| OpenOCD telnet | 4444–4447 |
| OpenOCD TCL | 6666–6669 |
| RTT telnet | 9090–9097 |

A client SHOULD NOT filter ports itself; the gateway's answer is
authoritative, and a later gateway may widen the list.

### 10.4 Gateways and boxes without tunnel support

- A gateway that does **not** enforce auth MAY still accept `CONNECT`, with
  no credential required.
- An older gateway that predates this section forwards the `CONNECT` to the
  box's own debug service (after checking auth, if it enforces it), or
  splices it there unchanged if it is a pass-through. A plain box with no
  gateway answers on 8765 itself. In all of these cases the answer is the
  Lager debug service's `501`, not a `200`.

A client therefore cannot learn from the `CONNECT` alone whether a box has
an old gateway or none. The Python CLI (`cli/gateway_tunnel.py`,
`choose_route`) decides like this:

1. `200` → tunnel.
2. Anything that is not a tunnel, on a box never seen to answer with a
   gateway denial → a plain box; connect to the debug port directly.
3. Not a tunnel, on a box known to be gated (§5 `boxes` entry) → try the
   debug port directly; if that fails, report that the box's gateway needs
   updating.

Step 3 is the only one that connects to a debug port just to learn
something. A client SHOULD NOT probe debug ports on a box it has no reason
to think is gated: accepting a connection can have side effects on the
target, such as a debug server halting the CPU when a GDB client attaches.

### 10.5 Revocation

A gateway MAY close an open tunnel when the user's access to the box is
revoked; it is expected to recheck about once a minute. The client sees the
connection close. It SHOULD tell the user and MUST NOT reopen the tunnel in
a loop: the next tunnel request is authorized afresh and gets the denial.

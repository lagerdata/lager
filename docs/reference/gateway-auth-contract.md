# Lager Gateway Auth Contract

**Version: 1** · Status: stable · Last updated: 2026-07-22

This document is the normative specification of the authentication contract
between Lager clients and boxes fronted by an authenticating reverse proxy
(a *gateway*). Anything that fronts a Lager box and honors this contract can
gate it — the gateway and auth server are deliberately not part of Lager
itself, so control planes built on Lager (e.g. enterprise deployments) own
identity while every Lager client works against them unchanged.

Conforming implementations of the client side:

- `cli/gateway_auth.py` (Python CLI; test suite `cli/tests/test_gateway_auth.py`)
- `lager-rs/src/auth.rs` (Rust crate; test suite `lager-rs/tests/gateway_auth.rs`)

If an implementation and this document disagree, this document wins; fix the
implementation or amend the spec with a version bump (see
[Versioning](#versioning)).

The key words MUST, MUST NOT, SHOULD, and MAY are used as in RFC 2119.

---

## 1. Actors

| Actor | Role |
| --- | --- |
| **Box** | The Lager hardware service (`:9000` API, `:8765` debug service, Socket.IO namespaces). Auth-unaware; never sees or checks credentials. |
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

### 6.3 Handling a denial

On a gateway denial (§2):

1. Record the box→auth-server mapping in the store, unconditionally.
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

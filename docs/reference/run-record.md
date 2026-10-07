# Lager Run Record

**Version: 1** · Status: draft · Last updated: 2026-10-07

This document is the normative specification of the **Run Record**: one
JSON document per `lager python` invocation that describes what ran, on
which box, in what state, and what came out. The machine-readable schema
is [`run-record.v1.schema.json`](run-record.v1.schema.json). If this
document and the schema disagree, this document wins; fix the schema.

Lager produces the record with or without a control plane. It is
evidence an engineer holds locally. Anything that wants stronger
guarantees, such as verified identity, signatures, or an append-only
archive, consumes the record from the box outbox (§7) and wraps it. A
wrapper MUST NOT change the record it wraps.

The key words MUST, MUST NOT, SHOULD, and MAY are used as in RFC 2119.

---

## 1. Observers

Each field has one observer. The observer is the only party that can
state the value truthfully.

| Observer | Fields |
| --- | --- |
| **Box** | Everything outside `clientAsserted`. The box receives the code, runs the process, emits the output, sees the exit, and holds the output files. |
| **Client** | `clientAsserted` only. The command line, the Git commit of the local checkout, and the client's own view of what it sent and received. |

A consumer SHOULD trust box-observed fields over `clientAsserted` fields.
When the two disagree (for example `clientAsserted.files` vs `code.files`),
the record is still valid. The disagreement is itself evidence.

## 2. Identity of a record

- `runId` is the `LAGER_PROCESS_ID` the client mints for the run (a UUID).
  If the client sends none, the box mints one. It is unique per run.
- `boxSequence` is a counter the box persists in
  `/etc/lager/run_records/sequence`. It starts at 1 and increments by one
  for every run the box accepts, including runs that later fail. A gap
  in `boxSequence` across the records a consumer holds means a record is
  missing.
- `recordHash` is not a field. It is computed:
  `sha256(JCS(record))`, lowercase hex, where JCS is RFC 8785 (JSON
  Canonicalization Scheme). Any language with a JCS implementation can
  reproduce it.

## 3. Lifecycle

1. **Open.** When the box accepts a `/python` request, it assigns
   `boxSequence`, records `startedAt`, `box`, `boxState`, `code`,
   `environmentPassed` and `clientAsserted`, and writes the record to
   `/etc/lager/run_records/open/<runId>.json`.
2. **Final.** When the process exits, the box sets `finishedAt`, `exit`,
   `outputs` and `log`, sets `state` to `final`, and moves the file
   atomically to the outbox (§7). A final record never changes again.
3. **Lost.** On start, the box finalizes every record left in `open/`
   with `exit.reason = "lost"`, `exit.code = null` and
   `finishedAt = null`. A box restart therefore never leaves a sequence
   gap.

## 4. Fields

All timestamps are UTC, RFC 3339, with exactly six fractional digits and
a `Z` suffix: `2026-10-07T18:04:11.123456Z`.

All hashes are SHA-256, lowercase hex.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | Always `"lager.run-record/v1"`. |
| `runId` | string | §2. |
| `boxSequence` | integer | §2. |
| `state` | `"final"` | Only final records leave the box. |
| `startedAt` | timestamp | When the box spawned the process. |
| `finishedAt` | timestamp or null | When the process exited. Null only for `lost`. |
| `box.boxId` | string | Content of `/etc/lager/box_id`, or `"unknown"`. |
| `box.hostname` | string or null | The host's hostname. |
| `boxState` | object | §4.1. |
| `code` | object | §4.2. |
| `environmentPassed` | string[] | Names of variables the client forwarded with `--passenv`, sorted. **Names only, never values.** |
| `exit` | object | §4.3. |
| `outputs` | object[] | §4.4. |
| `log` | object | §4.5. |
| `clientAsserted` | object or null | §4.6. Null when the client sent nothing. |

### 4.1 `boxState`

Captured at spawn, before any user code runs.

| Field | Source |
| --- | --- |
| `lagerVersion` | First field of `/etc/lager/version` (`box_version\|cli_version`). |
| `deployedByCliVersion` | Second field of `/etc/lager/version`. |
| `ref` | Content of `/etc/lager/ref` (`<ref>@<sha>`), or null. |
| `os` | `PRETTY_NAME`, `ID` and `VERSION_ID` from the host's `os-release`. |
| `kernel` | `uname -r`. |
| `nets` | The parsed content of `/etc/lager/saved_nets.json`, verbatim, or `[]`. |
| `netsHash` | `sha256(JCS(nets))`. Two runs with the same `netsHash` ran against the same net configuration. |

### 4.2 `code`

What the box received, hashed by the box.

| Field | Meaning |
| --- | --- |
| `kind` | `"script"` (a single file) or `"module"` (a zip). |
| `archiveSha256` | For `module`, the hash of the zip as received. Null for `script`. |
| `files` | One entry per file: `{path, size, sha256}`, sorted by `path`. For `script`, one entry. For `module`, every regular file in the zip, including files added with `--add-file` and `.lager` includes. |
| `entrypoint` | The path the box executed. |
| `args` | Arguments passed to the script. |

### 4.3 `exit`

| Field | Meaning |
| --- | --- |
| `code` | Process exit code, after the same normalization the CLI applies (`-N` → `128+N`). Null for `lost`. |
| `reason` | One of the values below. |
| `timeoutSeconds` | The timeout in force, or 0 for none. |

| `reason` | When |
| --- | --- |
| `exited` | The process exited by itself. Any exit code. |
| `timeout` | The timeout elapsed and the box stopped the process. |
| `cancelled` | A client requested the stop through `/python/kill`. |
| `disconnected` | The client disconnected and the box stopped the process. |
| `lost` | The box restarted before the run finished (§3). |

### 4.4 `outputs`

One entry per file the client declared with `--download`, hashed by the
box after the process exits: `{name, size, sha256, retained}`.

- If a declared file does not exist, the entry has `size` and `sha256`
  null.
- `retained` is true when the bytes are kept in the blob store (§7).
  Files larger than the box's retention cap are hashed but not retained.

A client that downloads a file MUST be able to check its bytes against
this entry.

### 4.5 `log`

The log is the output the box streamed, in order, as the exact
`Lager-Output-Version: 1` frames for file numbers 1 (stdout), 2 (stderr)
and 3 (output channel): `<fileno> <length> <bytes>`. Keepalive frames
(file number 0) and the exit frame are excluded.

| Field | Meaning |
| --- | --- |
| `sha256` | Hash of the log bytes. |
| `size` | Length of the log bytes. |
| `retained` | As in §4.4. |

A client that received the whole stream can compute the same hash. If it
matches, the client saw the complete output.

### 4.6 `clientAsserted`

Sent by the client with the request. The box records it but does not
verify it.

| Field | Meaning |
| --- | --- |
| `cliVersion` | The client's version. |
| `argv` | The command line. Values given to `--env` MUST be replaced with `<redacted>`. |
| `git` | `{commit, dirty}` for the repository containing the runnable, or null. |
| `files` | The client's own `{path, size, sha256}` list for what it sent. |

## 5. Privacy

- Environment variable values never appear in a record, whether passed
  with `--env` or `--passenv`.
- `boxState.nets` is copied verbatim. Do not store credentials in net
  definitions.
- The log and output files are the user's data. A consumer that retains
  them takes on their handling.

## 6. Local copy

After a run, the client fetches the final record from
`GET :5000/run-records/<runId>` and writes it as
`<runId>.lager-run.json` beside the downloaded files. The client checks
each downloaded file and its own received log against the record, and
warns on any mismatch.

## 7. Outbox

The box writes final records and retained bytes where any consumer can
collect them:

```
/etc/lager/run_records/outbox/<runId>.json   final records
/etc/lager/run_records/blobs/<sha256>        retained log and output bytes
```

- Writes are atomic (write to a temporary name, then rename).
- A consumer deletes an outbox record only after it has durably stored
  it. Lager never deletes from the outbox.
- A blob is safe to delete once no outbox record references it.
- With no consumer installed, the outbox grows. The box SHOULD expose
  its size in `/status` so the operator can see it.

## 8. Versioning

`schema` names the version. Adding an optional field is a minor change
and keeps `v1`. Removing a field, changing a type, or changing how a hash
is computed requires `v2`. A consumer MUST reject a `schema` value it
does not know.

# Lager Run Record

**Version: 1** · Status: draft · Last updated: 2026-10-07

This document specifies the **Run Record**: one JSON document per
`lager python` invocation that describes what ran, on which box, in what
state, and what came out.

The schema, [`run-record.v1.schema.json`](run-record.v1.schema.json), is
normative for structure: which fields exist, their types, and which are
required. This document is normative for meaning: where each value comes
from and how it is computed. A test (`test/unit/box/test_run_record_schema.py`)
checks that the example validates, that records the box produces validate, and
that every field in the schema is described here.

Lager produces the record with or without a control plane. Anything that
wants stronger guarantees, such as verified identity, signatures, or an
append-only archive, collects the record from the box (§8) and wraps it. A
wrapper MUST NOT change the record it wraps.

The key words MUST, MUST NOT, SHOULD, and MAY are used as in RFC 2119.

---

## 1. Observers

Each field has one observer: the only party that can state the value
truthfully.

| Observer | Fields | Trust |
| --- | --- | --- |
| **Box** | Everything except the two sections below. The box receives the code, runs the process, emits the output, sees the exit, and holds the output files. | Observed. |
| **Client** | `clientAsserted`. The command line, where the code sits in its Git repository, labels. | Recorded, not verified. |
| **Script** | `scriptAsserted`. Facts only the running test knows, such as the firmware on the device under test. | Recorded, not verified. |

A consumer SHOULD trust box-observed fields over asserted ones. When they
disagree (for example `clientAsserted.files` vs `code.files`), the record is
still valid. The disagreement is itself evidence.

## 2. Identity

- `runId` is the `LAGER_PROCESS_ID` the client mints for the run, a UUID. If
  the client sends none, or one that is already recorded, the box mints one.
- `box.boxId` identifies the box's record store. The box mints it, a UUID,
  the first time it records a run, and keeps it in
  `/etc/lager/run_records/box_uid`. It is never `unknown`. It survives Lager
  updates and container rebuilds. It does not survive losing that directory,
  as an OS re-image does; the box then mints a new `boxId` and `boxSequence`
  starts again at 1.
- `box.hardwareId` is `mac:<address>`: the lowest MAC address among the
  host's physical network interfaces (virtual interfaces such as bridges and
  `docker0` are excluded). It survives re-imaging, so it links a box's old
  and new `boxId`. It is null when no physical interface is visible.
- `boxSequence` starts at 1 and increments by one for every run the box
  accepts, including runs that fail to start. It is scoped to `boxId`. A gap
  in `boxSequence` means a record is missing.
- `recordHash` is not a field. It is computed as `sha256(JCS(record))`,
  lowercase hex, where JCS is RFC 8785 (JSON Canonicalization Scheme).

## 3. Time

Every timestamp in the record comes from the box's clock. Lab boxes often
have no time synchronization, so the record states whether the clock was
synchronized (`boxState.clock`). A consumer that judges staleness or ordering
from timestamps MUST take `boxState.clock.synced` into account. A control
plane that receives the record SHOULD record its own receipt time, and that
time is authoritative when the two disagree.

All timestamps are UTC, RFC 3339, with exactly six fractional digits and a `Z`
suffix: `2026-10-07T18:04:11.123456Z`.

## 4. Lifecycle

1. **Open.** When the box accepts a `/python` request, it assigns
   `boxSequence` and records `startedAt`, `box`, `boxState`, `code`,
   `environmentPassed` and `clientAsserted`. A request with nothing to run is
   rejected before this point and is not recorded.
2. **Final.** When the process ends, the box sets `finishedAt`, `exit`,
   `outputs`, `log` and `scriptAsserted`, and stores the record (§8). A final
   record never changes again.
3. **Lost.** When the box's execution service starts, it finalizes every run
   still open, which happens only after a restart mid-run, with
   `exit.reason = "lost"`. A restart therefore never leaves a gap in
   `boxSequence`.

## 5. Recording policy

A box records in one of two modes, set in `/etc/lager/run_records/policy.json`
as `{"mode": "local"}` or `{"mode": "collected"}`. With no file, the mode is
`local`.

| | `local` | `collected` |
| --- | --- | --- |
| Use | A plain box with nothing collecting records. | A box whose records a consumer collects (§8). |
| Records kept | The most recent 500. | All of them, until the consumer collects them. Never pruned. |
| Log and output bytes | Not retained. Hashes only. | Retained up to the cap in `retention.maxBlobBytes`. |
| Cannot write a record | The run goes ahead unrecorded, and the box logs it. | **The box refuses the run** with HTTP 503. |

In `collected` mode the box MUST NOT start a run it cannot record. For an
evidence system an unrecorded run is worse than a refused one. "Cannot
record" includes a disk with less free space than two full-size blobs plus
16 MiB. A run whose record fails to finalize after it started is the one case
the box cannot refuse; it shows as a gap in `boxSequence`.

## 6. Fields

All hashes are SHA-256, lowercase hex.

| Field | Meaning |
| --- | --- |
| `schema` | Always `"lager.run-record/v1"`. |
| `runId` | §2. |
| `boxSequence` | §2. |
| `state` | Always `"final"`. Only final records leave the box. |
| `startedAt` | When the box accepted the run. |
| `finishedAt` | When the run ended. Null for `lost`. |
| `box` | §6.1. |
| `boxState` | §6.2. |
| `code` | §6.3. |
| `environmentPassed` | Names of the variables the client forwarded with `--passenv`, sorted. **Names only, never values.** |
| `exit` | §6.4. |
| `outputs` | §6.5. |
| `log` | §6.6. |
| `retention` | §6.7. |
| `clientAsserted` | §6.8. Null when the client sent nothing. |
| `scriptAsserted` | §6.9. Null when the script asserted nothing. |

### 6.1 `box`

| Field | Meaning |
| --- | --- |
| `boxId` | §2. |
| `boxName` | A human name for the box: `/etc/lager/box_id` if present, else the hostname. Informational; not an identity. |
| `hostname` | The host's hostname. |
| `hardwareId` | §2. |

### 6.2 `boxState`

Captured when the box accepts the run, before any user code runs.

| Field | Source |
| --- | --- |
| `lagerVersion` | First field of `/etc/lager/version` (`box_version\|cli_version`). |
| `deployedByCliVersion` | Second field of `/etc/lager/version`. |
| `ref` | Content of `/etc/lager/ref` (`<ref>@<sha>`), or null. |
| `os` | From the host's `/etc/os-release`: `prettyName` is `PRETTY_NAME`, `id` is `ID`, `versionId` is `VERSION_ID`. Null when the file is not visible. |
| `kernel` | The running kernel release (`uname -r`). |
| `clock` | From the kernel's clock discipline (adjtimex(2)): `synced` is the signal `timedatectl` reports as "System clock synchronized", and `maxErrorMicroseconds` is the kernel's estimate of the maximum error. Both null when the box cannot read them. |
| `nets` | The parsed content of `/etc/lager/saved_nets.json`, verbatim, or `[]`. |
| `netsHash` | `sha256(JCS(nets))`. Two runs with the same `netsHash` ran against the same net configuration. |
| `instruments` | §6.2.1. |

#### 6.2.1 `boxState.instruments`

The physical units behind the nets, so a consumer can look up a unit's
calibration and maintenance history by serial number. One entry per distinct
instrument and address in `nets`, sorted by `connection`. Read from the
addresses the nets are saved with. The box sends no command to an instrument
to fill this in.

| Field | Meaning |
| --- | --- |
| `type` | The instrument type, as the net names it (e.g. `Keithley_2281S`). |
| `connection` | The address the box uses: VISA resource, `serial://` resource, or device path. |
| `serialNumber` | The unit's serial number, or null when the address carries none. |
| `serialSource` | Where `serialNumber` came from (below), or null. |
| `firmwareVersion` | The instrument's firmware version. Always null in this version (§6.2.2). |
| `nets` | Names of the nets that use this instrument, sorted. |

| `serialSource` | Meaning |
| --- | --- |
| `usb-device` | The USB serial number of the instrument itself, from its VISA address. For USB-TMC instruments this is the instrument's own serial number, and the box can only open the unit it names. |
| `usb-adapter` | The USB serial number of a cable or adapter between the box and the instrument (for example a USB-to-RS-232 cable). It identifies the cable, not the instrument. |
| `idn` | Reserved for the serial number an instrument reports in its `*IDN?` response. |

A consumer MUST NOT treat a `usb-adapter` serial number as the instrument's.

#### 6.2.2 Reserved for a later version

Reading `firmwareVersion` and an `idn` serial number requires sending
`*IDN?` to each instrument as a run starts, which can stall on a busy
instrument. This version does not do it.

### 6.3 `code`

What the box received, hashed by the box.

| Field | Meaning |
| --- | --- |
| `kind` | `"script"` (a single file) or `"module"` (a zip). |
| `archiveSha256` | For `module`, the hash of the zip as received. Null for `script`. |
| `files` | One entry per file, `{path, size, sha256}`, sorted by `path`. For `script`, one entry. For `module`, every regular file in the zip, including files added with `--add-file` and `.lager` includes. `path` is the path inside the upload. `clientAsserted.files` maps it to the repository. |
| `entrypoint` | The path the box executed. |
| `args` | Arguments passed to the script. |

### 6.4 `exit`

| Field | Meaning |
| --- | --- |
| `code` | The process exit code, normalized as the CLI normalizes it (`-N` becomes `128+N`). Null for `lost` and `start-failed`. |
| `reason` | One of the values below. |
| `timeoutSeconds` | The timeout the box enforced, or 0 for none. Attached runs are capped at the box's ceiling (300 s). |
| `cancelRequestedAt` | When `/python/kill` first asked this run to stop, or null. Who asked is not known to the box; a gateway in front of it can attribute the request by the `Lager-Process-Id` header the client sends. |

| `reason` | When |
| --- | --- |
| `exited` | The process exited by itself, with any exit code. |
| `timeout` | The timeout elapsed and the box stopped the process. |
| `cancelled` | `/python/kill` asked for the stop. Takes precedence over the others. |
| `disconnected` | The client went away and the box stopped the process. |
| `start-failed` | The run was accepted but its process never started (for example a failed `pip install`). |
| `lost` | The box restarted before the run finished (§4). |

### 6.5 `outputs`

One entry per file the client declared with `--download`, hashed by the box
after the process exits. `name` is the path the client declared, and `size`,
`sha256` and `retained` describe the file.

- If a declared file does not exist, `size` and `sha256` are null.
- `retained` is true when the bytes are kept for collection (§8).
- For a `lost` run, `outputs` is empty: the run never reached the point where
  its files were final.

A client that downloads a file can check its bytes against this entry.

### 6.6 `log`

The log is the output the box streamed, in order, as the exact
`Lager-Output-Version: 1` frames for file numbers 1 (stdout), 2 (stderr) and
3 (output channel): `<fileno> <length> <bytes>`. Empty frames, keepalive
frames (file number 0) and the exit frame are excluded.

| Field | Meaning |
| --- | --- |
| `sha256` | Hash of the log bytes. |
| `size` | Length of the log bytes. |
| `retained` | As in §6.5. |

A client that received the whole stream can compute the same hash. If it
matches, the client saw the complete output.

For a `lost` run, `sha256` and `size` describe whatever the box had captured
when it went down, and `retained` is false: the log may end mid-frame, and
nobody can say it is the whole output.

### 6.7 `retention`

| Field | Meaning |
| --- | --- |
| `mode` | The recording policy in force (§5). |
| `maxBlobBytes` | The largest log or output file the box retains. 0 in `local` mode, where nothing is retained. A `retained: false` entry with a size above this was too large. |

### 6.8 `clientAsserted`

Sent by the client with the request. The box records it but does not verify
it.

| Field | Meaning |
| --- | --- |
| `cliVersion` | The client's version. |
| `argv` | The command line. Values given to `--env` are replaced with `<redacted>`. |
| `git` | The repository containing the runnable, or null: `commit` is its `HEAD`, and `dirty` is true when tracked files have uncommitted changes. |
| `repoRelativeRoot` | The runnable's directory relative to the repository root, using `/`, or null. |
| `files` | The client's own list of what it sent: `{path, repoPath, size, sha256}`. `path` matches `code.files[].path`. `repoPath` is the file's path in the repository, or null for a file outside it. |
| `labels` | Key-value strings the launcher attached with `--label KEY=VALUE`, for example an execution ID. At most 32. |

`repoRelativeRoot` and `repoPath` are hints. A consumer that checks the
box-observed hashes against a commit uses them to find each file; if a hint
is wrong, the hashes do not match and the check fails, which is the right
outcome.

### 6.9 `scriptAsserted`

Key-values the running script recorded with `lager.record_value(key, value)`.
The box records them verbatim and does not verify them.

- Keys are 1 to 128 characters of letters, digits and `. _ : / -`, starting
  with a letter or digit. Values are strings (at most 4096 characters),
  numbers, booleans or null.
- At most 256 keys. The last value written for a key wins.

## 7. Privacy

- Environment variable values never appear in a record, whether passed with
  `--env` or `--passenv`.
- `boxState.nets` is copied verbatim. Do not store credentials in net
  definitions.
- Labels and script assertions are recorded verbatim. Do not put secrets in
  them.
- The log and output files are the user's data. A consumer that retains them
  takes on their handling.

## 8. Where records go

```
/etc/lager/run_records/final/<runId>.json   final records (both modes)
/etc/lager/run_records/outbox/<runId>.json  collected mode: records to collect
/etc/lager/run_records/blobs/<sha256>       collected mode: retained bytes
```

- `GET :5000/run-records/<runId>` returns a final record from `final/`. It
  waits up to 10 seconds for a run that is still finishing, then answers 202.
  It answers 404 for a run the box has no record of.
- In `collected` mode, a consumer deletes an outbox record only after it has
  durably stored it. Lager never deletes from the outbox.
- A blob is safe to delete once no outbox record references it.
- `GET :5000/status` reports the mode and the outbox size under
  `runRecords`.

After a run, the CLI fetches the final record and writes it as
`<runId>.lager-run.json` beside the downloaded files, unless given
`--no-run-record`. It checks each downloaded file and the output it received
against the record, and warns on any mismatch.

## 9. Versioning

`schema` names the version. Adding an optional field is a minor change and
keeps `v1`. Removing a field, changing a type, or changing how a value or hash
is computed requires `v2`. A consumer MUST reject a `schema` value it does not
know.

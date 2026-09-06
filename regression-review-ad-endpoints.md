# Regression review: `ad/endpoints` vs `main`

**Date:** 2026-07-12
**Scope:** 10 commits, 105 files, +7,949 / −5,906 (merge-base `0f4b643`)
**Theme:** Migration of CLI↔box communication from the legacy `:5000` exec path
(upload a Python impl script, spawn a process on the box) to the warm `:9000`
HTTP API backed by `hardware_service`, plus deletion of nine `cli/impl/*.py`
scripts and the claim-release handoff for `lager python`.

**Test status:** full unit suite passes — `1760 passed, 5 skipped, 36 subtests passed`.

---

## Verdict

No crashes, data-loss bugs, or security regressions were found. The migration
is well-structured: path-traversal guards were preserved (and centralized in
`lager.binaries.store`), new endpoints have solid test coverage, and the
documented design decisions (`docs/reference/net-9000-coverage.md`) match the
code. The findings below are mostly **behavioral regressions for users on old
box images or with automation that parsed the old output**, plus a few
resource-lifecycle risks on the box.

---

## 1. Intentional but high-impact: hard cutover with no `:5000` fallback

~20 commands (`supply`, `battery`, `adc`, `dac`, `gpi/gpo`, `spi`, `i2c`,
`watt`, `energy`, `eload`, `solar`, `arm`, `webcam`, `router`, `ble`, `wifi`,
`blufi`, `uart`, `nets`, `binaries`, …) are now **9000-only**. On `main`, some
of these (e.g. `supply`) fell back to the `:5000` exec path when `:9000` was
missing. On this branch an outdated box gets a hard error telling the user to
run `lager box update`.

This is documented as intentional, and the error messages are good. But until
the fleet is updated, a new CLI against an old box **fails for nearly
everything**. Two consequences deserve attention:

- **Lock enforcement silently degrades on old images** (`cli/box_storage.py:456-466`).
  Lock checks moved from `:5000/lock` to `:9000/lock`. Against an image without
  the `:9000` lock route, the CLI warns once and **proceeds unlocked** — a
  consistency/safety regression for shared boxes until they're updated.
- **`lager boxes` live status probes `:9000` only** (`cli/commands/box/boxes.py`).
  Old-image boxes that are up and serving `:5000` show as offline/unversioned.

## 2. Error-reporting regressions (medium)

These change what a user sees when something goes wrong, in ways that can
misdirect debugging:

- **Unreachable box masquerades as "no nets configured"** —
  `fetch_nets()` (`cli/core/net_helpers.py:171-208`) swallows connection and
  parse errors and returns `[]`. Callers like `validate_net_exists()` then
  print "No {role} nets configured" when the real problem is that `:9000` is
  down or the box is unreachable. (Partially pre-existing — the old
  `run_net_py` also returned `[]` on failure — but this is now the primary
  lookup path for every Tier-1 command.)
- **Version-skew warning rarely fires** — `check_and_warn()` is only invoked
  from `resolve_and_validate_box_with_name()` (used by `hello`, `diagnose`,
  `lock`). The common path for Tier-1 commands, `resolve_and_validate_box()`,
  never calls it, and `lager nets` has its own resolver that also skips it.
  The call placement is unchanged from `main`, but the branch's stated goal
  ("warn instead of silently degrading") is only partially met: most commands
  hard-fail on `:9000` without the friendly "your box image is too old"
  warning. Additionally, `version_skew.py:85-87` returns silently when
  nothing is listening on `:9000` at all — the exact old-image scenario.
- **Solar automation exit codes collapsed** — the old impl script exited with
  typed codes (2 library-missing, 3 device-not-found, 4 device-busy,
  5 backend). All failures now exit 1 via `post_net_command`
  (`cli/core/net_helpers.py:286-295`); the box maps every backend error to a
  generic 502. Any CI scripts branching on those exit codes will break.
- **BLE/WiFi/BluFi failures no longer emit structured JSON** — the old impl
  scripts printed a `JSON Output:` block even on failure
  (`connected: false`, etc.). The new path prints a one-line `Error: …` and
  exits 1. Scripts parsing failure JSON will break; success JSON is preserved.

## 3. Box-side resource-lifecycle risks (medium)

- **Dual pyvisa sessions on one EA PSB supply** — `solar_hs.py` opens its own
  `EA(instr=address)` while a supply net on the same unit uses the separate
  `ea` driver. Both stay cached warm in `hardware_service`. They share the
  per-address *lock* but not a pyvisa *session*; two open USB-TMC sessions to
  one instrument risks `[Errno 16] Resource busy` / `Query INTERRUPTED` when
  supply and solar nets coexist on the same hardware.
- **`/cache/clear` no longer releases direct-USB claims** — Tier-1 drivers now
  live in dispatcher/singleton caches that only the new
  `/cache/release_direct_usb` endpoint drains. Old callers that POST
  `/cache/clear` expecting a full USB handoff (the pre-v0.16.8 behavior) will
  leave LabJack/Phidget/Joulescope claims open.
- **Claim-release handoff failure is silent** — `python/executor.py:47-68`
  logs release failures at `debug` only. If `hardware_service` is down or
  slow, `lager python` proceeds and the user's script hits an opaque
  exclusive-claim error (LJM 1230, libusb Resource busy) with no hint that
  the handoff failed.
- **Energy integration clamp raised 30s → 120s**
  (`box/lager/http_handlers/net_command.py:521-525`) — deliberate, but any
  proxy in front of `:9000` with a <120s read timeout will now kill
  long integrations that used to be impossible.

## 4. Functional parity gaps vs deleted impl scripts

Parity audit of the nine deleted `cli/impl/*.py` scripts: **router, blufi,
arm, net, query_instruments, and solar actions have full parity**. Gaps:

| Severity | Gap | Detail |
|---|---|---|
| Medium | `lager wifi connect --interface` partially ignored | The CLI still accepts `--interface`, but the box's nmcli path (`box/lager/protocols/wifi/connect.py:44-47`) never passes `ifname`; only the wpa_supplicant fallback honors it. The deleted impl script passed `ifname <interface>` to nmcli. Multi-radio setups silently connect on the default interface. |
| Low | WiFi status lost signal strength | Old status showed `Connected (Signal: -45dBm)`; new `get_wifi_status()` never extracts RSSI. |
| Low | WiFi connect response lost post-connect status block | Old success JSON embedded a `status` sub-object; new response is `{ssid, connected, interface, method}`. |
| Low | Solar resistance bounds only enforced client-side | CLI validates 0.1–100 Ω but the box handler only rejects `<= 0`; direct `:9000` callers (e.g. the Rust crate) can set out-of-range values. |
| Low | Solar lost client-side net preflight | Unknown net names now fail with a box error instead of the friendlier list of available solar nets. |

(Note: the webcam per-net `url` action exists on the box handler but not the
CLI — this matches `main`, which also only exposed `url-all`; not a regression.)

## 5. Smaller issues (low)

- **`lager box diagnose` can traceback on old boxes** —
  `_fetch_net_info` (`cli/commands/box/diagnose.py:55,73`) does
  `[n for n in nets if n.get('name') == net]` on the raw JSON. If an older
  image returns the `{"nets": [...]}` wrapper shape (which `fetch_nets()`
  explicitly tolerates), `n` is a string and `.get()` raises an uncaught
  `AttributeError`.
- **UART net listing has no fallback** — `uart.py` queries only
  `/uart/nets/list` while everything else uses `fetch_nets()` (which tries
  `/nets/list` first). Inconsistent; breaks if `/uart/nets/list` is ever
  retired.
- **Binaries error JSON shape differs across ports** — `:5000` errors include
  `"status": "error"`; the new `:9000` handler returns `{"error": …}` only.
  Clients checking the `status` field will mis-parse `:9000` failures.
- **`arm_hs` missing from the Dockerfile import smoke-check**
  (`box.Dockerfile:238-240`) — the check imports every other `*_hs` adapter;
  `arm_hs` (the only one that caches a serial handle in `device_cache`) would
  fail only at first use on-box.
- **`gpo --hold` is now a documented no-op** — pin state persists via the warm
  driver cache, so the flag prints a note instead of blocking. Scripts that
  relied on *hold-then-release-on-exit* semantics behave differently (the
  level now persists indefinitely).
- **Battery success output** uses raw ANSI green instead of the `[OK]` prefix
  used by supply and `post_net_command` — scripts grepping `[OK]` miss it.
- **Release/invoke race during USB-claim drain** — the dispatcher cache drain
  in `_release_direct_usb_claims()` (`hardware_service.py:800-815`) can close
  a driver that a concurrent `/invoke` just fetched. The code documents this
  as benign, but a `:9000` request racing a `lager python` launch can see one
  mid-transaction failure.

## 6. What checked out clean

- **Security**: binaries store rejects `/`, `\`, `..` in names; downloads
  resolve via `abspath` + allowlist with the `root + os.sep` sibling-prefix
  guard (traversal tests included). nmcli/wpa calls use argv lists, no shell.
- **`python/service.py` −169 lines**: pure refactor — binaries/download logic
  extracted to shared `lager.binaries.store`; no endpoint or capability lost.
- **Dockerfile**: `COPY *.py` glob plus the expanded import smoke-check
  eliminates the historical "new root module not shipped" failure mode.
- **SIGPIPE fix**: moving `signal.signal(SIGPIPE, SIG_DFL)` from import time
  to per-invocation correctly fixes the exit-141 full-suite pytest kill,
  guarded to main-thread only (TUI worker threads).
- **`device_id` locking** in `hardware_service` correctly shares one lock
  across roles on the same physical device while keeping per-net driver
  instances — the right fix for LabJack/Joulescope multi-role serialization.
- **Tier-1 output parity**: role-specific formatting (`fmt_si`, eload state,
  thermocouple, watt) was restored with `quiet=True`; parameter mappings
  spot-checked against box handlers with no mismatches.
- **Streaming paths**: UART WebSocket, debug/RTT streaming, and Ctrl-C
  handling are unchanged; long-blocking actions (energy/watt integration,
  `gpi --wait-for`, BLE scans, BluFi provisioning) widen both HTTP legs past
  the blocking duration, matching old behavior.

## Suggested fix priorities

1. Call `check_and_warn()` from `resolve_and_validate_box()` (and
   `nets._resolve_box`) so the "old box image" warning precedes the commands
   that actually hard-fail, and consider warning on connection-refused too.
2. Make `fetch_nets()` distinguish "unreachable" from "no nets" so
   `validate_net_exists()` can report connectivity errors honestly.
3. Pass `ifname <interface>` on the nmcli path in
   `box/lager/protocols/wifi/connect.py`.
4. Unwrap the `{"nets": [...]}` shape in `diagnose._fetch_net_info`.
5. Decide whether supply+solar on one EA PSB should share a pyvisa session
   (add `ea`/`solar_hs` to the shared-VISA set) before both are used warm.
6. Log the `lager python` claim-release failure at `warning` and/or surface it
   in the script's stderr preamble.

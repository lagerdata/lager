# Changelog

All notable changes to the Lager platform are documented here. For detailed release notes, see [docs.lagerdata.com](https://docs.lagerdata.com).

## [0.23.0] - 2026-06-03

Self-service box provisioning: users can now grant USB device access by adding their own udev rules through `lager box config`, and there are first-class commands for erasing the config and getting a fresh container — no engineer-cut release required for a new device.

### Added
- **`lager box config udev add/list/remove` — user-editable host udev rules.** Grant a USB device read/write access from inside the container by vid:pid, e.g. `lager box config udev add 1209:0001 --box <BOX>`, then `lager box config apply`. This fixes the common case where a device node is owned by root so tools like `dfu-util` fail with "No DFU capable USB device available" (exit 74). Pass `--usbtmc` for SCPI/USBTMC instruments to also emit the driver-unbind rule (needed for PyVISA/libusb). Rules are stored in `box_config.json` (`udev_rules`) and installed host-side on `apply` to `/etc/udev/rules.d/99-lager-user.rules` with a `udevadm` reload+trigger, reusing the box's existing passwordless-sudo udev grant. Previously every new device required a Lager engineer to edit `box/udev_rules/99-instrument.rules` and cut a release.
- **`lager box config reset` — erase the box config to empty.** A single command that clears the config to an empty state (unlike `init`, which seeds the default `box-tools` volume). Pass `--apply` to also restart the container, so you get an erased config *and* a fresh container in one command — handy as a clean slate before a test run.
- **`lager box config restart` — restart the container without changing config.** A fresh container with the same config, useful for per-test isolation when you want a clean container between test runs.

## [0.22.1] - 2026-06-02

Documentation and metadata cleanup: the open-source tree no longer carries internal Lager Box hostnames or a real device IP address.

### Changed
- **Scrubbed identifying Lager Box names and one real device IP from all in-repo text.** `--help` output, command docstrings, source comments, the CHANGELOG, release notes, and documentation now use the placeholder `<BOX>` (and `<box-ip>`) in place of real Lager Box names; narrative references drop the names while preserving the relevant dates, versions, and meaning. Test fixtures use a neutral `test-box` token. Documentation/metadata only — no functional or API changes.

## [0.22.0] - 2026-06-01

This release makes release **tags** the single source of truth for pinning a box to a version. `lager update`/`lager install` now resolve a release-number pin to the matching `vX.Y.Z` tag instead of a same-named git branch, so the per-release version branches are no longer needed.

### Changed
- **`lager update --version X.Y.Z` / `lager install --version X.Y.Z` now resolve to the release tag `vX.Y.Z`.** Previously a bare release number was fetched as a git *branch* (`origin/X.Y.Z`), which required publishing a per-release branch next to every tag. A semver pin — with or without a leading `v`, including common pre-release suffixes (`-rc1`, `-beta2`, `-alpha`, `-preview`) — now resolves to the tag. Branch targets (`main`, `staging`, feature branches) are unchanged and still resolve to `origin/<name>`. This is backward compatible: existing `--version X.Y.Z` pins keep working, now via the tag. (`resolve_version_ref` in `cli/commands/utility/update.py`, mirrored in `cli/deployment/scripts/setup_and_deploy_box.sh`.)

### Fixed
- **Tag pins now fetch reliably on boxes that don't already have the tag.** A tag is fetched with an explicit refspec (`refs/tags/<tag>:refs/tags/<tag>`) so it becomes a local ref; `git fetch origin <tag>` alone only sets `FETCH_HEAD`, which previously left `lager update --check` reporting "update state unknown" and could block the checkout.

### Deprecated
- **Per-release version branches (`X.Y.Z`) are deprecated.** Releases no longer create them (removed from `RELEASE_PROCESS.md`); use the `vX.Y.Z` tag to pin. Existing version branches are recreatable from their tag if ever needed.

## [0.21.3] - 2026-05-29

This patch completes the 0.21.2 fix. That release stopped `lager nets tui` from fighting the `lager.pause()` stdin watcher, but the same root cause still degraded every other in-process caller that captures script output — most visibly `lager supply tui` and `lager battery tui`.

### Fixed
- **`lager supply tui`, `lager battery tui`, and net confirm prompts no longer drop keystrokes.** The 0.21.0 `lager.pause()` feature starts a daemon thread in `run_python_internal` that reads `stdin` (so Enter resumes a paused script). It only skipped that thread when the caller passed `watch_stdin_resume=False`, which 0.21.2 wired into `net_tui.py` only. Every other in-process caller that captures output via `redirect_stdout` — `cli/core/net_helpers.py:run_net_py` (behind `lager supply/battery/arm` and the measurement commands' net validation), plus the `_run_net_py` helpers in `webcam.py` and `debug/commands.py` — still leaked a daemon `stdin` reader on each call. For the power TUIs the leak came from the one pre-launch `validate_net` call, whose reader then raced Textual for the whole session; for `lager supply`/`battery`/`arm` it raced the immediately-following `click.confirm`, intermittently swallowing the first `y`/Enter.
- **The watcher is now gated structurally, not just by an opt-out flag.** `run_python_internal` only starts the stdin watcher when `sys.stdout is sys.__stdout__` — i.e. stdout has not been swapped out by `redirect_stdout`. Output-capture call sites (which all redirect stdout) can therefore never leak the reader again, even if a future one forgets the flag, while genuine foreground runs — including `lager python script.py | tee`, where stdout is piped but not reassigned — keep Enter-to-resume. The three capture helpers also pass `watch_stdin_resume=False` explicitly. Covered by new gating tests in `test/unit/cli/test_python_breakpoint_session.py`.

## [0.21.2] - 2026-05-29

This patch fixes a regression introduced in 0.21.0: the interactive `lager nets tui` would drop and mishandle keystrokes.

### Fixed
- **`lager nets tui` no longer fights the breakpoint watcher for your keystrokes.** The 0.21.0 `lager.pause()` feature starts a daemon thread inside `run_python_internal` that reads `stdin` to let you press Enter to resume a paused script. But `lager nets tui` is a Textual app that calls `run_python_internal` in-process for every backend action (scan, load, save, delete), so each call leaked a stdin-reading thread that raced the TUI's own input loop — producing dropped/erratic keypresses and unresponsive rename/edit dialogs. `run_python_internal` gains a `watch_stdin_resume` flag (default `True`, so `lager python` breakpoint resume is unchanged) and the TUI now passes `watch_stdin_resume=False`. `net_tui.py` is the only Textual caller, so no other command is affected.

## [0.21.1] - 2026-05-29

This release reworks the CLI's help and error output for newcomers: every command now shows the real `lager <type> [NET_NAME] [COMMAND] --box [BOX_NAME]` usage pattern with copy-pasteable examples, and the most common failures now print a clear problem-and-fix message instead of a raw Python traceback.

### Added
- **Actionable error messages.** Introduced a structured `LagerError` (problem + cause + suggested fixes) in `cli/errors.py`, with classifiers for connection failures, SSH/auth errors, USB-TMC system errnos (16/19/110), box-not-found, and net-not-specified. A top-level funnel in `main()` replaces raw Python tracebacks with friendly guidance; the full traceback is still available via `--debug` / `LAGER_DEBUG=1`. Wired into the highest-impact new-user paths: box selection, connection failures, bad `.lager` config, SSH/auth errors in `logs`/`install`, and net-not-specified in `i2c`/`spi`/`uart`. Adds `test/test_errors.py` (43 tests).

### Changed
- **Help is accurate, consistent, and scannable.** Replaced Click's misleading `[OPTIONS] COMMAND [ARGS]...` usage line with the real net pattern (`lager <type> [NET_NAME] [COMMAND] --box [BOX_NAME]`) via a shared `NetGroup`/`NetSubCommand`/`NetCommand` in `cli/core/net_group.py`, added a copy-pasteable Examples section to every net command, and now show `[NET_NAME]` consistently in every subcommand usage line. `lager --help` groups commands into categories (`SectionedGroup`) instead of a flat 40-item alphabetical list. Bracket placeholders (`[NET_NAME]`, `[COMMAND]`, `[BOX_NAME]`, `[IP_ADDRESS]`, ...) are now `[UPPER_SNAKE]` everywhere, short-helps are tidied, and `usb` is now a proper net group with `enable`/`disable`/`toggle` subcommands.

### Fixed
- **Fixed a broken `__main__` import and a wrong "defaults set" hint** surfaced while migrating the error paths.

## [0.21.0] - 2026-05-28

Interactive breakpoints for `lager python` scripts. A long-running test can now pause itself mid-run so the operator can inspect the bench with ad-hoc `lager` commands (or a live Python prompt) and then continue — the workflow customers asked for when debugging tests against a device in an unknown state. Before this, `lager python` scripts ran start-to-finish with `stdin` set to `DEVNULL` and there was no way to hold execution at a chosen point.

### Added
- **`lager.pause(label=None, *, timeout=None, interactive=False)`** — drop it anywhere in a `lager python` script and execution blocks at that line. Resume three ways: press **Enter** in the script's foreground terminal, run **`lager python --continue <id> --box <box>`** from any terminal, or let it **auto-resume after `timeout` seconds (default 300)**. A paused script holds no box-wide lock, so other `lager` commands keep working against the bench while it waits. Coordination is file-based under `/tmp/lager_processes/{id}/` (`breakpoint.json` describing the pause + a `resume` marker), polled by the script — deliberately chosen over adding a channel to the timing-sensitive `stream_process_output()` path (which has 50–120 ms UART budgets). The `id` is the existing client-generated `LAGER_PROCESS_ID`, so it is known for both foreground and detached runs.
- **`POST /python/continue` and `POST /python/breakpoint`** endpoints on the box Python service (port 5000): `continue` drops the `resume` marker for a process id (returns `{resumed: bool}`); `breakpoint` reports the current pause state. Both validate the id as a UUID.
- **`lager python --continue <id>` and `lager python --console <id>`** flags (alongside `--kill`/`--reattach`, lock-check skipped), plus a foreground stdin watcher so **Enter resumes** a paused run. New session helpers `continue_python()` / `breakpoint_status()` in `cli/context/session.py`.
- **Interactive console** via `pause(interactive=True)` + `lager python --console <id>` — a Python REPL bound to a socket (a free port in the already-exposed 8081–8090 range), seeded with the paused frame's globals + locals. Read any variable, evaluate expressions, or call the script's functions. **This is the way to inspect a device the script itself holds open** (e.g. a LabJack), since it runs *inside* the paused process. It operates on a snapshot — mutations in the console do not write back into the running script.
- **Configuration**: `timeout=` (per call) or `LAGER_BREAKPOINT_TIMEOUT` (env, set via `lager python --env`) override the auto-resume duration; `timeout=0` waits indefinitely; `LAGER_BREAKPOINTS=off|0|false` makes every `pause()` a no-op (and `pause()` outside `lager python` — no `LAGER_PROCESS_ID` — is a safe no-op).
- **Dedicated docs**: a Python API guide at `docs/source/reference/python/breakpoints.mdx` covering the API, the three resume paths, the console, configuration, and the device-ownership behavior observed during hardware validation.

### Changed
- **`PYTHONBREAKPOINT` now points at `lager.breakpoint.pause`** (was `remote_pdb.set_trace`, set in `box/lager/python/executor.py` and `box/start_box.sh`). `remote_pdb` was never installed in the box image, so the builtin `breakpoint()` raised `ImportError`; it now invokes the same interactive pause as `lager.pause()`. The dead `REMOTE_PDB_HOST` / `REMOTE_PDB_PORT` envs were dropped.
- **The breakpoint banner reports the script name you invoked** (from `LAGER_RUNNABLE`) instead of the box-side temp filename — `lager python` runs a single-file script from an opaque `tmpXXXX.py`, so the location previously read `tmpXXXX.py:NN`. Line numbers are unchanged (the temp file is a verbatim copy).

### Fixed
- **`box/lager/breakpoint.py` is now copied into the box image.** `box.Dockerfile` enumerates the top-level `lager/*.py` files by name in its `COPY`, so the new module was initially omitted and `lager/__init__.py`'s import of it failed the image build; it has been added to the manifest.
- **Console output (banner, exit message, syntax errors, tracebacks) is routed to the console socket** rather than the script's stderr, so it no longer leaks into the `lager python` terminal.

### Verified on hardware (live, not just unit-tested)
- Pause/resume via **Enter** and via `lager python --continue <id>`; **auto-resume** after the timeout.
- **Interactive console**: read the captured `readings` dict, a live in-process `read_adcs()` (succeeds where a separate `lager adc` returns `LJME_DEVICE_CURRENTLY_CLAIMED_BY_ANOTHER_PROCESS`, because the paused script owns the LabJack handle), and arbitrary Python.
- Reading `supply2` and `battery1` state from a second terminal while the script was paused.
- **Device-ownership behavior documented from this testing**: a paused script keeps every instrument it opened claimed for the duration of the pause — inspect script-held devices via `--console`, shared instruments from a second terminal; and a single process can hold only one net per physical instrument at a time (Rigol `supply2`/`supply3`; dual-role Keithley `supply1`/`battery1`).

## [0.20.1] - 2026-05-27

### Added
- **`--force` flag on `lager update`.** Bypasses the "already up to date" early-exit *and* forces a clean rebuild (wipes the cached image plus the `lager-cargo` / `lager-npm-global` volumes). The version file is written to `/etc/lager/version` *before* the container is started, so a box whose update failed at container start still reports the new version and reads as "up to date" — a normal `lager update` then refuses to act. `--force` is the recovery path for that state; the "Removing cached image…" status line now names the actual trigger (`--force` vs `build inputs changed`).

### Changed
- **`lager update` is the canonical box-update command again.** It is no longer deprecated and is the documented way to update a Lager Box. Internally both surfaces always shared one implementation; this just promotes the shorter top-level spelling and drops the deprecation notice.
- **Box updates reuse the Docker build cache instead of rebuilding from scratch every run.** The post-build cleanup changed from `docker image prune -af` (which deleted *all* unused images, including the layer cache, forcing a ~40-package pip reinstall + Rust toolchain rebuild on every update) to `docker image prune -f` (dangling only). Because `box.Dockerfile` copies source *after* the heavy apt/pip/rust/nrfutil layers, a code-only update now reuses those layers and finishes in ~30s instead of ~15min; full builds happen only when the Dockerfile or requirements actually change.
- **The container image is built once per update, not twice.** `lager update` builds the image in its own step (with full build-error reporting) and now invokes `start_box.sh` with `LAGER_SKIP_BUILD=1` so the box-side script skips its redundant second `docker build`. Standalone/deploy invocations of `start_box.sh` still build as before.

### Fixed
- **`lager update` survives a transient DNS/connection blip during `git fetch`.** The fetch step retries up to 3 times (3s/6s backoff) when the box hits a transient resolver/connection error (`Could not resolve host`, `Name or service not known`, connection timeouts) — common on boxes behind a flaky WiFi resolver. Non-transient failures (auth, missing branch) still fail fast with the original clear error.
- **Docker image builds can resolve external hosts on systemd-resolved boxes.** `cli/deployment/scripts/setup_and_deploy_box.sh` now writes `/etc/docker/daemon.json` pointing Docker's container DNS at the box's real uplink resolvers (discovered from `/run/systemd/resolve/resolv.conf`, with `1.1.1.1`/`8.8.8.8` fallbacks). Boxes whose `/etc/resolv.conf` only exposes the `127.0.0.53` stub previously left Docker falling back to `8.8.8.8` inside build containers; where that resolver was unreachable, `docker build` could not clone the GitHub-hosted pip dependencies and the image build failed mid-run — the root cause behind updates that hung for ~15 minutes and then reported a container-start timeout.

### Removed
- **`lager box update`.** Removed in favor of the canonical top-level `lager update`. Any scripts or automation calling `lager box update` should switch to `lager update`.

## [0.20.0] - 2026-05-26

This release is a direct response to the "battery net not responding" incident on 2026-05-26, where a Keithley 2281S misbehavior took ~2 hours of debugging across `lsof`, `dmesg`, bare `pyvisa` probes, and hardware-service introspection to root-cause. The biggest items below — `lager diagnose`, the `usbtmc` blacklist, automatic ENODEV recovery, and cross-process device locks — collectively eliminate the most common failure modes that drove that session, and surface the rest (e.g. wedged instrument firmware that only mains-power-cycling can fix) with a single one-line diagnosis.

### Added
- **`lager diagnose <net> --box <box> [--type <role>]` — single-shot net diagnosis.** Polls three box-side endpoints in parallel (USB enumeration + USB-TMC interface-class detection + holder detection via `/proc/*/fd/*` walk + `dmesg` + `lsmod` for usbtmc on port 9000, bare `pyvisa` `*IDN?` probe on port 9000, hardware-service in-process session cache on port 8080) and classifies the net into one actionable bucket with the next step the user should take: `HOST-SIDE: usbtmc kernel module loaded` (→ `lager box update`), `HOST-SIDE: USB device claimed by multiple processes` (→ names the PIDs), `HOST-SIDE: USB device busy` (→ `lager ssh` + `sudo lsof`), `TRANSIENT: device disappeared from USB` (→ hw service auto-recovers; if not, `docker restart lager`), `TRANSIENT: device enumerated as USB-TMC but pyvisa fresh probe couldn't reach it` (→ run any net command so hw_service caches a session, or `pkill -f box_http_server` to reset libusb state — narrow window after USB re-enumeration), `INSTRUMENT WEDGED` (→ mains-side power-cycle — the case software cannot fix), `NOT ENUMERATED` (→ power/cable/upstream-hub), `NOT USB-TMC` (LabJack/Picoscope/Acroname use vendor SDKs, not pyvisa — gated on the device's sysfs interface class so we don't misclassify a healthy USB-TMC instrument that's just briefly unreachable), or `HEALTHY` (with the IDN string). `--type` is optional and auto-detected from the box's saved nets via `NetType.from_role()`. Backwards-compatible against pre-0.20 boxes: each endpoint's 404 surfaces as "endpoint not on this box (pre-0.20 image)" and the command keeps running with the available bits.
- **`usbtmc` kernel-module blacklist shipped with the box image** at `/etc/modprobe.d/blacklist-usbtmc.conf`. Without this, the kernel auto-binds the `usbtmc` driver to USB-TMC-class instruments (Keithley 2281S, Keysight, Rigol scopes, etc.) and claims interface 0; pyvisa-py's libusb backend then can't `set_configuration()` and returns `[Errno 16] Resource busy`. The race re-arms on every module load / box reboot, so a one-shot `modprobe -r usbtmc` doesn't stick — the blacklist file is the only durable fix. Deployed by `setup_and_deploy_box.sh` (new boxes) and refreshed by `lager box update` (existing boxes), mirroring the `box/udev_rules/` shape. The deploy script also attempts `sudo modprobe -r usbtmc` after installing the file so the change takes effect without a reboot; if the module can't be unloaded because an instrument is currently in use, a "reboot recommended" notice is printed.
- **`lager update` verbose status block now includes `modprobe.d:`** alongside the existing `udev rules:` line, showing whether the source file is in sync, missing, or already current, plus whether `usbtmc` is currently loaded on the host.
- **Cross-process device locks for USB-TMC drivers** via the new `lager.util.device_lock` module (`box/lager/util/device_lock.py`). Generalizes the long-standing EA-solar/supply `DeviceLockManager` pattern (`fcntl.flock` on a lockfile under `/tmp/lager_device_locks/` keyed on VISA address) and adopts it in the Keithley battery + supply, Rigol DP800, Rigol DL3021 eload, Keysight E36000, and Rigol MSO5000 scope drivers. Held only across the `open_resource()` call itself — hardware_service serializes subsequent SCPI traffic via its in-process per-address lock — so the lock guards only against the specific failure mode where a second box-side `pyvisa` client (an ad-hoc `docker exec python3 -c "import pyvisa; ..."` debug session, a TUI launched directly on the box, an MCP tool taking a shortcut) races the hardware service for the libusb interface-0 claim. Fails open if the locking infrastructure itself errors (FS issue, perms), matching EA's long-standing behavior so a transient filesystem hiccup can't take legitimate work offline. EA drivers continue to use their pre-0.20 `/tmp/lager_ea_locks/` directory via a thin `_EaDeviceLockManager` subclass that preserves the existing exception hierarchy.
- **Version-skew warning** prints once per CLI session to stderr when the CLI's minor version is ahead of the box's minor version by one or more (same major), recommending `lager box update --box <name>`. The 2026-05-26 session started with CLI 0.19.2 talking to box 0.18.3 and the first error was opaque; this single line would have cut diagnosis time by hours. Hooked into `resolve_and_validate_box_with_name`, cached per-process by box IP so a tight command loop doesn't refetch, and fails open on any error (network timeout, JSON parse, missing `cli-version` endpoint, etc.) so a flaky network can never break a working command.
- **Actionable error messages for `[Errno 16/19/110]`** via the new `map_system_error()` / `format_system_error_for_user()` helpers in `cli/context/error_handlers.py`. Detection prefers explicit `[Errno N]` substring; falls back to message heuristics (`'resource busy'`, `'no such device'`, `'timed out'`, etc.) so wrapped exceptions still match. The three errnos map to: 16 EBUSY → "USB device busy — another process holds the libusb interface" with `Try: lager diagnose <net> --box <box>`; 19 ENODEV → "Instrument disappeared from USB (re-enumeration)" with `Hw service should auto-recover; if not: sudo docker restart lager`; 110 ETIMEDOUT → "Instrument did not respond to SCPI — firmware may be wedged" with `A mains-side power-cycle of the instrument is usually required`. Raw error remains available via `LAGER_DEBUG=1`. Wired into `cli/impl/power/battery.py` and `cli/impl/power/supply.py` — the two backends that surface the trio most often. Other backends (eload, solar, scope) keep printing raw for now; trivial follow-up to extend.
- **`lager diagnose` command-specific docs** at `docs/diagnose.md` covering the three endpoints' returned shapes, the classification decision tree, sample sessions for each classification, and the `--type` semantics.

### Fixed
- **`lager battery <net> ...` and `lager supply <net> ...` no longer return `[Errno 19] No such device` until `docker restart lager`** after a USB re-enumeration of the instrument (mains power-cycle, accidental unplug, USB hub port toggle). The hardware-service retry path was gated on `_VISA_SESSION_ERROR_KEYWORDS = ('session', 'closed', 'invalid')`, which did not match libusb's ENODEV signature — so the existing retry never fired and every subsequent `/invoke` failed against the stale file descriptor. Extended the keyword tuple with `'no such device'`, `'cannot find'`, `'errno 19'`, `'enodev'`; added an explicit `_is_enodev_error()` helper; and on ENODEV the `/invoke` retry now evicts every sibling `device_cache` entry on the same VISA address (so a Keithley 2281S supply call recovers automatically when battery just hit ENODEV — they share one physical USB device) and force-closes the shared `pyvisa` session pool entry regardless of `_SHARED_VISA_DEVICE_NAMES` membership (the cached `pyvisa` handle holds a stale fd after re-enumeration even for non-shared drivers). Plain stale-session errors keep the narrower per-caller eviction; the cascade is gated strictly behind `_is_enodev_error` so we don't over-evict on every minor `pyvisa` hiccup. Live-verified on the box's Keithley 2281S via a USB driver unbind/bind sequence.
- **`lager update` Step 5b (new) re-detects the `modprobe_d/` source dir post-pull.** The update probe runs at the start of the flow, before the `git pull`; on the very first deploy that introduces the directory (i.e. this 0.20.0 PR), the pre-pull probe correctly reports `MODPROBE_SRC_PATH` empty and the install step would short-circuit with "SKIPPED (source dir missing)" even though the dir exists post-pull. Re-detects via a fresh SSH round-trip against the canonical paths if the pre-pull probe came up empty.
- **`lager diagnose` host-side holder detection now works on the actual box image.** The original `/diagnose/usb` endpoint shelled out to `sudo lsof /dev/bus/usb/<device>` to find competing libusb claims, but neither `sudo` nor `lsof` ship in the lager container; the subprocess silently exited 127 and the endpoint always returned `lsof: []`. As a result the `HOST-SIDE: USB device claimed by multiple processes` and `HOST-SIDE: USB device busy` classifications **could never fire in production** — the very buckets `lager diagnose` was designed to surface from the 2026-05-26 incident. Replaced with a `/proc/*/fd/*` walk that reads `/proc/<pid>/comm` for the process name. No external tools, no permission gymnastics, scoped to the same container PID namespace as `box_http_server` (which is where rogue holders inside the lager container live).
- **`lager diagnose` classifier no longer misclassifies a healthy USB-TMC instrument as `NOT USB-TMC`** when pyvisa's fresh-probe path can't reach it (most common cause: a stale libusb context inside `box_http_server` after a USB re-enumeration; hw_service runs in a separate process and recovers transparently). `/diagnose/usb` now reads the device's sysfs interface descriptors (`bInterfaceClass`/`bInterfaceSubClass`) and surfaces `is_usbtmc: true` for class 0xFE / subclass 0x03 devices. The classifier disambiguates accordingly: enumerated USB-TMC + fresh-probe failure → new `TRANSIENT: device enumerated as USB-TMC but pyvisa probe couldn't reach it` bucket with a concrete recovery hint; enumerated non-USB-TMC (LabJack/Picoscope/Acroname) → existing `NOT USB-TMC` hint preserved.
- **`lager diagnose` VISA-side error mapping now catches all three libusb "device not reachable" message variants.** pyvisa-py emits `[Errno 19] No such device` (libusb's standard ENODEV after a re-enumeration), `[Errno 2] Entity not found` (authorized=0, mid-bind window, or a denied open), and `No device found.` (generic vendor-not-matched-or-stale path). All three now map to `error_class: nodev` so the classifier consistently returns `TRANSIENT` instead of falling through to `UNCLEAR`.
- **`lager diagnose` VISA section renders all five fields on endpoint-returned errors** (`idn:`, `elapsed:`, `error:`, `error_class:`, `skipped:`). The pre-fix `_print_section` helper short-circuited on any `error` key in the dict, collapsing the section to a single `error:` line and dropping the `error_class:` and `elapsed:` context the user needs to interpret the failure. Now distinguishes transport-layer errors (connect failure, HTTP 5xx) — which still short-circuit with a `transport error:` line — from endpoint-structured errors, which flow through the section's lambda renderer.
- **`lager diagnose` prints an actionable message when the box is unreachable** instead of wrapping the raw urllib3 traceback. The previous output read `Could not fetch net list from box: HTTPConnectionPool(host='<box-ip>', port=5000): Max retries exceeded with url: /nets/list (Caused by NewConnectionError("HTTPConnection(host='<box-ip>', port=5000): Failed to establish a new connection: [Errno 61] Connection refused"))`. Now reads: `Box '<BOX>' unreachable at <box-ip>:5000 (connection refused). The lager container may be stopped. Check with: lager ssh --box <BOX> -- "sudo docker ps"`. `requests.exceptions.ConnectionError` and `Timeout` are caught explicitly with tailored messages (refused vs timed out); other exceptions still fall through to the catch-all.
- **`/diagnose/visa` correctly consults hw_service's session pool across processes.** `box_http_server` (port 9000) and `hardware_service` (port 8080) are separate processes, but the original `/diagnose/visa` implementation imported `_visa_resources` from `lager.hardware_service` to check for a shared session — giving `box_http_server` its own empty copy of the dict, not hw_service's live state. The skip-if-shared-session check always returned False, the fresh probe always ran, and on a healthy box with a cached hw_service session it ALWAYS hit EBUSY at `set_configuration()` — surfacing as `HOST-SIDE: USB device busy` on every diagnose call against a perfectly healthy box. Replaced with an HTTP call to `localhost:8080/diagnose/dispatcher`, which returns the canonical live state.

### Improvements
- **TUI WebSocket-failure messages call out the specific next step instead of `WebSocket connection failed: Failed to connect to WebSocket server`.** `lager battery <net> tui` and `lager supply <net> tui` now probe `http://<box>:9000/health` on connect failure and emit one of: `Action: box is reachable on :9000 but the WebSocket handshake failed — the box may be on a pre-0.20 image; lager box update --box <name>` (200 response); `Action: services may be partially up; sudo docker restart lager` (non-200); `Action: timed out reaching <box>:9000; check Tailscale, then lager box hello` (connect-timeout); `Action: cannot reach <box>:9000 — lager container may not be running; sudo docker start lager` (connect-refused). Original WS error preserved in parentheses so debug info isn't lost. Lives in `cli/core/ws_diagnose.py` so future TUIs can reuse the same diagnostic.
- **Documented "TUIs are laptop-only"** in `box/lager/README.md`. Running TUIs directly on the box was the suspected culprit of that incident (a second `pyvisa-py` client competing with hardware-service for interface 0). The OS-level `device_lock` makes this case detect-and-fail-clean instead of silent EBUSY, but the right answer is still: always launch TUIs from the laptop CLI.
- **`lager diagnose` output labels clarified.** The header line now reads `NetType: <role>` instead of `resolved role: <role>` to align with the terminology used elsewhere in the CLI. The USB section now prints `usb-tmc class: yes/no` (newly surfaced from `/diagnose/usb`) so the user can see whether the classifier is treating the device as USB-TMC — and the existing kernel-module-status line is renamed from the ambiguous `usbtmc:` to `usbtmc kmod:` so the two related fields are visually distinct. Pre-0.20 boxes (which don't return `is_usbtmc`) render the field as `usb-tmc class: —` rather than guessing.

### Verified on hardware (live, not just unit-tested)
- Item 2 (ENODEV recovery): unbind/bind sequence on the Keithley while a state-polling loop ran from the laptop; loop kept getting 200s throughout (no `[Errno 19]` ever surfaced) and the Keithley's reported state values changed in real time, confirming hardware-service evicted and reopened transparently.
- Item 3 (cross-process lock): spawned a `multiprocessing.Process` inside the lager container that grabbed the `device_lock` for 3s, then timed a competing acquire from the parent process — bounced off `DeviceLockError` in 1.51s, exactly the configured 1.5s timeout window. Pre-0.20 this would have raced through libusb into `[Errno 16] Resource busy`.
- Item 5 (`lager diagnose`): `battery1` → HEALTHY (Keithley IDN); `supply1` → HEALTHY (same Keithley, sibling role); `adc1`/`usb1`/`scope1` (LabJack/Acroname/Picoscope) → NOT USB-TMC (with the vendor-SDK hint). `--type` explicit override matches auto-detect.
- Regression smoke: `lager adc adc1` → `-10.603 V`; `lager gpi gpio1` → `HIGH (1)`. The PR1 lock changes do not affect non-pyvisa drivers (LabJack uses LJM, Picoscope uses Pico SDK, neither goes through `device_lock`).
## [0.19.2] - 2026-05-25

### Changed
- **`--ip` now accepts DNS hostnames in addition to IP addresses** on `lager boxes add`, `lager boxes edit`, `lager install`, and `lager uninstall`. Lets a Lager Box sit behind a DNS name (e.g. `box.example.com`) or a Tailscale MagicDNS short name (e.g. `box-1.tailXYZ.ts.net`) instead of requiring the operator to look up and pin a numeric address. Validation is purely syntactic — IPv4/IPv6 (incl. Tailscale `100.x.x.x`) take the existing `ipaddress.ip_address` fast path; everything else is checked against RFC 1123 hostname rules (1–63 char alphanumeric/hyphen labels, ≤253 chars total, single-label allowed for MagicDNS), with actual resolution deferred to SSH/HTTP. The shared validator lives in the new `cli/address_utils.py` (covered by 34 unit tests in `test/unit/cli/test_address_utils.py`); the four call sites all share one error path that prints a "Valid formats:" cheatsheet on failure (`install` / `uninstall` previously printed only the bare error). Inputs that already carry a scheme, port, or path (e.g. `http://...`, `host:5000`, `host/api`) are rejected with a specific message instead of the previous generic "not a valid IP" — the rest of the CLI composes `http://{addr}:port/...` itself, so an embedded one of those would conflict.

## [0.19.1] - 2026-05-25

### Fixed
- **`lager debug ... flash` and the DA1469x flash loader now quote the firmware path before handing it to OpenOCD.** OpenOCD parses its TCL commands word-by-word, so a `program` or `load_image` argument with a space in it was being chopped into two TCL words and the underlying flash op either failed loudly with `wrong # args` or hit the wrong file. In practice the path comes from `tempfile.NamedTemporaryFile()` or the fixed `~/third_party/customer-binaries/openocd/flash-loaders/da1469x/` tree (no spaces), so the bug never bit in normal operation; the fix is defensive and aligns `box/lager/debug/openocd.py`'s `OpenOcdRpc.program()` and `OpenOcdRpc.load_image()` with the existing quoting pattern in `OpenOcdRpc.rtt_setup()`. Notable for operators who relocate the flash-loader tree via `LAGER_FLASH_LOADERS_DIR=/path/with spaces/`.
- **DA1469x flash_loader ELF parser now reports a clear error on a truncated symbol-table name instead of a Python `ValueError` traceback.** `box/lager/debug/da1469x_loader.py`'s ELF32 symbol walker used `bytes.index(b'\x00', ...)` to locate the null terminator for each name in the string table, which raised an unwrapped `ValueError` if the strtab itself was truncated. Switched to `bytes.find()` with an explicit error message that names the offending offset; `_resolve_loader_symbols()` still rewraps it as `Da1469xLoaderError` so the call site error type is unchanged.

## [0.19.0] - 2026-05-23

### Added
- **OpenOCD debug backend.** Non-Segger debug probes are now first-class peers of J-Link under `lager debug` — same `connect` / `gdbserver` / `flash` / `erase` / `reset` / `memrd` / RTT surface, same multi-probe slot allocator, same TUI. The box-side dispatcher in `box/lager/debug/probes.py:resolve_backend` routes by USB VID extracted from the debug net's VISA address; the auto-mapped OpenOCD probes are SEGGER-adjacent in scope: ST-Link V2 / V2-1 / V3 (`0483`), Raspberry Pi Debug Probe (RP2040 Picoprobe / CMSIS-DAP, `2e8a`), FTDI FT232H (`0403:6014` → `c232hm.cfg`) and FT2232H (`0403:6010` → `olimex-arm-usb-ocd-h.cfg`), ARM DAPLink / NXP MK20 CMSIS-DAP (`0d28`), Atmel EDBG/mEDBG (`03eb`), and Olimex ARM-USB-OCD-H (`15ba`). Anything else stays on J-Link to preserve existing-net behaviour. The OpenOCD daemon, TCL/RPC client, and command implementations live in the new `box/lager/debug/openocd.py` (~1000 lines); `service.py`'s `handle_*` paths fan out to the right backend per request. Per-probe slot stride mirrors the J-Link layout so OpenOCD nets can run concurrently with J-Link nets on the same Lager Box.
- **DA1469x flash via the Apache Mynewt RAM-resident flash_loader (OpenOCD path).** Mainline OpenOCD has no QSPI flash driver for the Dialog/Renesas DA1469x family, so `program ... 0x16000000 verify reset` cannot touch external NOR — `lager debug SWD flash` against an FT4232H rig silently did nothing despite a green `Flashed!` log line. `box/lager/debug/da1469x_loader.py` ports the upstream GDB-script protocol (`flash.gdb` / `erase.gdb` / `flash_loader.gdb`) to pure OpenOCD TCL/RPC: brings the loader up in RAM at the convention path `/home/www-data/customer-binaries/openocd/flash-loaders/da1469x/flash_loader.elf{,.bin}` (override via `LAGER_FLASH_LOADERS_DIR`), seeds MSP/PC, disables QSPIC/MTB/MPU, sets a hardware breakpoint on `mynewt_main`, waits for `fl_state == 1`, then drives the `fl_cmd` / `fl_cmd_rc` / `fl_cmd_data` command struct in chunks and software-resets on success. Also includes an inline ELF32 symbol-table reader so the box doesn't need `pyelftools`. Wired into `service.py`'s `handle_flash` / `handle_erase` OpenOCD branches, gated on `'DA1469' in device_type.upper()`. The two loader artefacts ship under `~/third_party/customer-binaries/openocd/flash-loaders/da1469x/` (operator drops them in once per box; `start_box.sh` `mkdir -p`s the subtree on every container start so they survive `lager update`); a missing pair raises an actionable error pointing at the expected paths.
- **Concurrent multi-probe slots extended to OpenOCD.** `box/lager/debug/probes.py` adds OpenOCD telnet (`4444 + slot`) and OpenOCD TCL/RPC (`6666 + slot`) ports to the existing per-slot window, alongside the J-Link GDB stride (`2331 + 3·slot`) and shared RTT base (`9090 + 2·slot`). Slot 0 is still the legacy single-probe path (GDB 2331, RTT 9090, OpenOCD telnet 4444, OpenOCD TCL 6666); legacy nets without a parseable serial keep landing on slot 0. `start_box.sh` publishes `4444-4447` and `6666-6669` via `docker run -p`; `secure_box_firewall.sh`'s `LAGER_PORTS` admits the same windows so hardened boxes don't silently drop OpenOCD telnet/TCL traffic. The in-box `DebugNet` Python API now allocates probes through the same shared `NetsCache` slot pool as the HTTP debug service (`service._resolve_probe`), so a `Net.get(name, NetType.Debug).connect()` call inside a `lager python` script gets a distinct port window per probe instead of pinning every concurrent script to slot 0.
- **`--openocd-config` on `lager nets add` / `add-batch`.** Parallels the existing `--jlink-script` flag — the user's `.cfg` rides on the saved net (base64-encoded) and is materialised to `/tmp/lager_openocd_user.cfg` in the box-side `_build_openocd_command` before each `openocd` spawn. Required for FT4232H, which has no auto-detected interface cfg (the chip exposes four channels and Lager can't guess which one carries SWD without the user telling us); supported on every other adapter as an escape hatch for vendor-supplied configs.
- **Backend-agnostic `lager nets set-script` / `show-script` / `remove-script`.** Replaces the script-routing surface with a single trio that detects the target backend from the probe VID + the file's extension/content sniff and routes to `jlink_script` or `openocd_config` accordingly. Refuses with a clear "pass `--backend jlink|openocd`" message when probe and file signals disagree (instead of silently guessing); enforces mutual exclusivity on every write so a debug net carries either field but never both (the other is cleared with a yellow stderr notice). `SCRIPT_PATH='-'` reads from stdin. Legacy `--backend X` short-circuit is preserved for CI and scripts that already know which slot they want.
- **OpenOCD speed fallback ladder for `DebugNet.connect()`.** `connect_jlink` already walked `[requested, 4000, 1000, 500, 100]` kHz; OpenOCD's `adapter speed` is set once at daemon startup with no built-in retry, so a vendor cfg expecting 500 kHz against Lager's 4 MHz default would die silently at the first SWD transaction. The same ladder is now applied at the `DebugNet` layer for the OpenOCD branch, exposed as the pure helper `openocd_speed_ladder(requested)` (covered by 7 unit tests).
- **`--jlink-version` flag on `cli/deployment/scripts/setup_and_deploy_box.sh`.** Pin the on-box JLink/JLinkExe version at deploy time instead of taking whatever Segger ships at the moment of the box build; matches the `--lager-version` flag's shape. Deployment options table in `docs/reference/deployment/README.md` corrected to match the current flag set.
- **Docs: ST-Link, RP2040, and FTDI listed under Debug & Flashing.** `docs/source/reference/instruments/supported-instruments.mdx` calls out the OpenOCD-detected probes alongside the existing J-Link entries; `docs/source/reference/cli/nets.mdx` documents `--openocd-config`, the unified `set-script`/`show-script`/`remove-script` trio, and the OpenOCD RTT `chunk_size` knob.

### Changed
- **`DebugNet.connect()` is symmetric across backends.** OpenOCD now honors `force=False` (restart via the daemon's built-in `stop_openocd`) and `ignore_if_connected=False` (returns the existing `status()` instead of raising) with the same semantics as J-Link's `JLinkAlreadyRunningError` path. Neither flag with a running daemon raises `RuntimeError`.
- **`DebugNet.status()` always returns `{running, pid, backend, ...}`** regardless of which backend the probe routes to. Backend-specific extras (J-Link's `cmdline`, OpenOCD's daemon log path) pass through unchanged, but consumers writing portable code can now rely on the three guaranteed keys. Previously OpenOCD returned `{running, pid}` while J-Link returned a wider dict that could hand back either a `jlink_status` or `gdbserver_status` shape.
- **OpenOCD gdb / telnet / TCL ports bind to `0.0.0.0`.** OpenOCD ≥ 0.11 defaults `bindto` to `127.0.0.1`, so `docker run -p 2331-2342:2331-2342` was forwarding traffic to a listener that wasn't accepting it and off-box GDB clients timed out without an error. Now matches J-Link's all-interfaces default. TCL/RPC remains 127.0.0.1-only on the wire because the box-side service drives it locally; only when explicitly remapped does it open up.
- **Custom OpenOCD configs now load before adapter-dependent `-c` commands.** Previously Lager emitted `-c "adapter serial <s>"` and `-c "transport select swd"` *before* `-f <user.cfg>`, but those `-c` commands require an adapter driver that only gets set inside the user's cfg — OpenOCD bailed with "adapter driver is not configured" before the cfg ever loaded. The user cfg now occupies the same slot in the command line that the auto-detected `interface/*.cfg` would, and the auto `transport select` is suppressed when a user cfg is supplied (vendor cfgs almost always call it themselves at the top, and OpenOCD errors on a duplicate set).
- **Dropped the short-lived `set-openocd-config` / `show-openocd-config` / `remove-openocd-config` trio.** Now that `set-script` is backend-agnostic, the OpenOCD-specific aliases looked like a special escape hatch that J-Link doesn't need — exactly the asymmetry we wanted to avoid. They never shipped in a tagged release, so there's no installed CLI surface to break; existing scripts/CI calls migrate to `set-script --backend openocd ...`. The error-message hint in `box/lager/debug/openocd.py`'s "can't infer interface" path now points at the canonical `set-script` command too.
- **OpenOCD FTDI dispatch keys on VID/PID, not just VID.** The original "VID `0403` → some FTDI cfg" mapping fell over the moment a box had both an FT232H and an FT2232H plugged in. Now: FT232H (`0403:6014`) → `c232hm.cfg`, FT2232H (`0403:6010`) → `olimex-arm-usb-ocd-h.cfg`, FT4232H → no auto-config (`openocd_config` required, with an actionable error when missing). `_build_openocd_command` skips the auto-detected interface cfg whenever a `user_config_path` is supplied, since the two `.cfg` files would otherwise collide on adapter driver / `layout_init`. The unreachable `0x1209` (BlackMagic et al.) pseudo-mapping is dropped; users with open-hw probes whose VIDs aren't auto-mapped can still use the OpenOCD backend by setting `debug_backend: openocd` and supplying `openocd_config`.

### Fixed
- **DA1469x flash hung at chunk 4 (`0x18000`) on one box.** Two bugs in the freshly-ported pure-RPC loader path that didn't show up against the GDB-script reference flow:
  - `_fl_program` was reading `fl_cmd_rc` mid-loop the moment `fl_cmd` returned to 0. The upstream `fl_program` macro deliberately doesn't — `rc` is only checked once, after the post-loop `while fl_state != 1` poll, because the loader uses `fl_cmd_rc` as scratch during a chunk. An eager mid-loop read caught a transient address-shaped value (observed `rc=0x66A4E0`) before the loader restored `rc` to 1, raising a spurious "program failed" mid-flash.
  - `fl_cmd_data` is a *pointer var* that the upstream `apps/flash_loader` toggles between halves of a malloc'd buffer on every `LOAD_VERIFY` (`fl_rotate_databuf()`). The pure-RPC path was passing the static ELF symbol of the pointer var to `load_image`, dumping each chunk on top of loader BSS and corrupting `fl_cmd_data` itself. Chunks 0/1 limped through; chunk @ `0x10000`'s first 4 bytes formed an unmappable pointer and bus-faulted the M33, leaving `fl_cmd` latched at 5. Now reads the pointer with `mdw` before each chunk's `load_image`, matching the GDB-script flow. (The earlier per-chunk-unique tempfile + 50 ms inter-chunk sleep were workarounds for symptoms of this bug and were dropped.)
- **DA1469x `lager debug SWD flash --bin <file>,0x16000000` silently programmed offset 0.** `service.py:handle_flash`'s DA1469x branch was hardcoding `offset=0` and dropping the CLI's address argument. The CLI accepts absolute XIP addresses (matching the J-Link convention) but the loader's `fl_cmd_flash_addr` is flash-relative, so the new `xip_to_flash_offset` helper translates and bounds-checks against the SoC XIP window `[0x16000000, 0x18000000)` — flash-relative offsets passed in by mistake fail loudly with an actionable message instead of silently writing to the wrong chunk of NOR.
- **OpenOCD silent flash failures.** OpenOCD's TCL/RPC channel returns the `program` proc's stdout as plain text even when flash write/verify failed, so a bad flash looked successful to callers. `flash_image` / `flash_erase_all` / `flash_erase_range` now scan the response for `program_error` markers (`** Programming Failed **`, `** Verify Failed **`, etc.) and `Error:` lines and raise `OpenOcdRpcError` with the file path and raw output. Side effect: `Erase complete!` / `Flashed!` no longer print on rigs whose `target.cfg` declares no flash bank — those calls now fail fast with the underlying error.
- **In-box `DebugNet` Python API ignored user-supplied debug scripts.** `openocd_config` (base64-encoded content) was being looked up under the wrong key (`openocd_config_path`) and never decoded to disk; `jlink_script` was never forwarded to `connect_jlink`. Custom configs/scripts uploaded via `lager nets set-script` had no effect when scripts ran `Net.get(name, NetType.Debug).connect()` from within `lager python`. Both fields are now decoded to the same shared temp paths the HTTP debug service writes to (`/tmp/lager_jlink_script.JLinkScript` / `/tmp/lager_openocd_user.cfg`), so the J-Link `reset_device` / `read_memory` helpers that look the script up via `api._get_script_file()` pick it up unchanged. Explicit `*_path` fields still win when the file is already on the box.
- **`set-script` previously routed every upload to the `jlink_script` field.** OpenOCD configs uploaded via `lager nets set-script` were silently stored in the wrong slot and ignored at run time. Now detects backend from probe VID + file extension/content sniff and writes to the correct field; refuses ambiguous cases with a `--backend` hint instead of guessing.
- **Net Manager TUI's "script attached" indicator missed `openocd_config`-only nets.** `has_script` was computed from `jlink_script` alone, so debug nets carrying only an `openocd_config` (the new normal for FT4232H rigs) showed no indicator even though one was attached. Now checks both fields.
- **FTDIs without a programmed USB serial were broken end-to-end.** A FT4232H whose EEPROM was never burnt has no readable USB serial, so the box scanner emitted `USB0::0x0403::0x6011::::INSTR` (empty serial slot) and the chain fell apart in three places: (1) the static `CHANNEL_MAPS` UART fallback was `["0", "1", "2", "3"]`, those bare interface indices landed in the saved net's `pin` field via the TUI, and the box-side UART dispatcher (which reads `pin` as a USB serial) failed at first use with `UART bridge with serial 2 not found`; (2) the debug-probe regex `([^:]+)::INSTR` rejected the empty-serial address, so `resolve_backend()` silently fell back to J-Link for what was actually an OpenOCD-backed FT4232H — `lager debug gdbserver` came back as the canned "Failed to connect to debugger" checklist with no real cause; (3) `cli/commands/box/nets.py:show_cmd` labelled the overloaded `pin` field as "Channel:" regardless of role, hiding misconfigurations. Fixed end-to-end: `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py` grow a `_get_ttys_for_usb_device` helper that matches ttys by sysfs node instead of USB serial; `CHANNEL_MAPS` for FT2232H/FT4232H change from `["0","1","2","3"]` to `[]` so bare-index placeholders can never leak; the VISA regex relaxes to `([^:]*)`; `cli/commands/box/net_tui.py` gains a `_validate_uart_pin` guard that rejects the legacy `"0"/"1"/"2"/"3"` placeholders with an actionable EEPROM hint; `cli/commands/development/debug/commands.py` surfaces the box's structured `error` field on `gdbserver` connect failures instead of parsing it into a discarded local; `show_cmd` is now role-aware (`Pin/serial:` for UART, `Device:` for debug). Direct CLI paths (`lager nets add` / `add-all` / `add-batch`) remain unaffected so power users keep an escape hatch. A parity test pins `usb_scanner.py` and `query_instruments.py` so the two scanners can't silently drift on this contract again.
- **TUI Keithley net wizard let the user assign both `power-supply` and `battery` roles to the same net.** The 2281S's two entry functions are mutually exclusive in firmware (and even the v0.16.9 dual-role fix for the *separately-named* keithley supply / keithley battery nets requires distinct nets, not one net with both roles checked). The role selector in `cli/commands/box/net_tui.py` now treats `power-supply` and `battery` as mutually exclusive at the checkbox level for the Keithley device class, surfacing the constraint at create-time instead of at first SCPI command.
- **Unrelated openocd follow-ups: warn instead of silently dropping a `.lager` debug script attached to a non-debug net; dedup legacy UART nets that ended up with both a serial-keyed and a sysfs-keyed copy in `saved_nets.json`; and document the OpenOCD RTT `chunk_size` knob in `docs/source/reference/cli/nets.mdx`.**

## [0.18.5] - 2026-05-22

### Fixed
- **`/debug/erase` and `/debug/flash` started returning 500 (`ValueError: filedescriptor out of range in select()`) on long-running boxes.** The debug service is a long-lived process. `get_controller()` builds a fresh `gdb-multiarch` `GdbController` on every retry attempt — the retry loop exists for the J-Link/RTT startup-timing races that are routine during a flash/RTT session — but a *failed* attempt is never stored in `_gdb_controller_cache`, so `cleanup_controller()` could never reach it. Each failed attempt leaked the `gdb-multiarch` subprocess and the pipe fds to it. Once the debug service crossed 1024 open fds, every newly spawned `JLinkExe` child PTY landed at an fd ≥ `FD_SETSIZE` (1024), and `pexpect`'s `REPLWrapper` — used by the erase/flash `commander()` path — crashed in `select()`. (`/debug/connect` was unaffected: it spawns `JLinkGDBServer` via `subprocess`, not `pexpect`.) Failed-attempt controllers are now closed (`_discard_failed_controller`), and `commander()` spawns `JLinkExe` with `use_poll=True` so `pexpect` uses `poll()` — which has no `FD_SETSIZE` ceiling — instead of `select()`.

## [0.18.4] - 2026-05-20

### Fixed
- **`lager python` scripts no longer miss tight response deadlines under streaming back-pressure.** A running script's stdout/stderr were drained from their kernel pipes *inline* on the same generator that forwards bytes to the CLI over HTTP, so any stall on that socket (slow link, Nagle, retransmit) stopped pipe drainage. Once the 64 KiB pipe filled, the script blocked on its next `print()`. For scripts with tight timing budgets — e.g. a DA14695 ROM-bootloader handshake that must reply within 50–120 ms of each byte — this stretched response windows enough to fail ~90% of the time, even though the same script run directly on the host (no executor, no stream-forwarder) succeeded every time. Output is now drained on background threads into a bounded queue, decoupling the script from HTTP-write latency; stdout/stderr pipe buffers are enlarged to 1 MiB (`F_SETPIPE_SZ`); the interpreter runs with `-u` for guaranteed unbuffered I/O; and the unused stdin is closed (`DEVNULL`). Wire format and public API are unchanged.
- **Avoid a potential deadlock when launching a `lager python` script.** The per-script scheduling-priority boost (`os.setpriority(-10)`) was applied via a `preexec_fn`, which runs in the forked child between `fork()` and `exec()`. Python documents `preexec_fn` as unsafe in a multithreaded process — and the python execution service is a `ThreadingHTTPServer` — because the child can deadlock if another thread held an allocator/import lock at fork time. The boost is now applied from the parent on the child's PID, with identical effect and permission semantics (`CAP_SYS_NICE`) and no fork/exec window.

## [0.18.3] - 2026-05-15

### Added
- **`lager box update --version <older-ref>` rolls back.** The previous one-way `git rev-list HEAD..target --count` only counted commits the box was *behind* and treated any "ahead" state as in-sync, so downgrading a box that had pulled a newer ref required manual `git reset --hard` on the box. Now uses `git rev-list --left-right --count HEAD...target` to detect divergence in both directions; a pull fires when the box is ahead of the target as well as behind. An explicit second confirmation prompt (skippable via `--yes`) gates the destructive direction so a typo'd `--version` argument can't silently downgrade a box. `--check` reports "will roll back N commit(s) ahead of target" / "will switch (N ahead / M behind)".

### Improvements
- **Update flow batches read-only state into a single SSH probe.** Replaces ~11 individual `test`/`cat`/`git`/`diff`/`stat` round-trips (git-repo check, remote URL, layout, current commit, build-cache hashes, udev rule state, sudoers ownership, box-config sudoers state, `/etc/lager/version`) with one structured shell script that emits `LAGER_PROBE_<KEY>=<value>` lines parsed locally. Combined with merging fetch+rev-list, sparse-checkout+checkout+reset, flatten+verify, post-build directory setup, and verify+J-Link presence into single calls, a typical no-op `lager box update` goes from ~3-5s to ~1.6s.
- **Persist user-installed cargo crates and global npm packages across container recreation via Docker named volumes.** Adds `lager-cargo:/opt/rust/cargo` and `lager-npm-global:/home/www-data/.npm-global` mounts to `start_box.sh`'s `docker run`. Without these, every `lager box update` recreated the container from scratch and the post-run loops recompiled `cargo install` packages (e.g., `defmt-print`) from source, adding ~50-60s per update. With them, the second-and-onward run sees "already installed" and finishes in seconds. The CLI wipes both volumes alongside `docker rmi lager` whenever the build-hash changes, so a Dockerfile rustup/node bump can't leave a stale toolchain in the volume. Measured on one box: typical update 1:40 → 17s after the volumes seed.
- **Verbose output cleanup.** Probe results print as one tidy block instead of a dozen "Checking X... OK" lines; consistent step labels between progress bar and `--verbose`; noise lines dropped (e.g. "Checking remote URL" only prints when it actually migrates SSH→HTTPS); single label for the build step instead of two; `log_status` helper signature simplified.

### Fixed
- **Pull aborted on git ≥2.36 with `fatal: 'cli/__init__.py' is not a directory`.** Cone-mode sparse-checkout (default since git 2.36) rejects single-file patterns. The pre-batching version of the sparse-checkout add ran in a separate SSH call whose exit was never checked, so the failure was silently swallowed; the new batched pull script chained it with `&&`, which propagated the failure and aborted the whole pull. Now treats the `cli/__init__.py` add as best-effort to match the original behavior. Affects boxes running newer git (observed on one box at git 2.43.0; another at 2.34.1 was unaffected).

## [0.18.2] - 2026-05-13

### Added
- **`lager box update` — canonical update command.** Replaces the top-level `lager update` (now a hidden deprecation alias that still works for existing scripts and CI). Sits alongside `lager box config` under the `lager box` group.
- **`--check` / dry-run mode.** `lager box update --box X --check` reports the planned update without modifying the box: current vs target version, code/deps/container state, estimated duration. Exits 0 for no-op, 1 for would-update, 2 on error.
- **Auto Docker-cache invalidation.** Records sha256 of `Dockerfile` + `requirements.txt` at `/etc/lager/build-hash` after each build. The next update detects drift (Dockerfile or requirements changed) and triggers `docker rmi lager` before the rebuild, replacing the manual `--force` workflow. First-run-after-deploy bootstraps the hash silently without forcing a rebuild.
- **SSH ControlMaster multiplexing.** All update SSH calls reuse a single OpenSSH master connection via `cli/core/ssh_utils.SSHConnectionPool`. Per-command overhead ~300ms → ~10ms; consecutive no-op runs ~20s → ~1.6s.

### Changed
- **`lager update` hidden in `--help`** and prints a one-line deprecation notice on every invocation, nudging users toward `lager box update`. Same flag set, same behavior — old scripts keep working.
- **End-of-run output redesigned.** Single green summary line (`<BOX> updated to version 0.18.2 (main)` or `<BOX> is already at version 0.18.2 (main)` for no-op). The redundant Restart/Build status, the "Verify with:" hint, and the verbose Duration line are dropped — elapsed time appears on the progress bar itself.
- **Progress bar rewrite.** Bar width is computed from the live terminal columns with a 2-char right margin (was a fixed 30 chars and would wrap on 80-col terminals, producing stacked-line artifacts because `\r\033[2K` only clears the current row). Elapsed time moved to the left of the bar, padded to a fixed width. The 1-second re-render thread is gated on `sys.stdout.isatty()` so captured output (CI logs, pipes, redirects) gets one frame per step instead of dozens.

### Fixed
- **Cache-invalidation early-exit silently skipped rebuilds.** The hash mismatch check ran *after* the no-restart early-exit branch, so a corrupted `/etc/lager/build-hash` with code in sync took the no-op path and never rebuilt. Auto-invalidation now also fires on deps-only changes (Dockerfile/requirements moved, code unchanged) as intended.
- **Stale `/etc/lager/version` after early-exit.** The "already up to date" branch only updated the local `~/.lager` cache, leaving the on-box version file untouched, so the next `lager hello` would surface the stale value and users would re-run `lager update` thinking the previous one didn't take. The primary cause of the recurring "had to run `lager update` 2–3 times before it stuck" reports.
- **Post-restart `time.sleep(5)` race.** Replaced with a poll of `http://<box>:5000/health` (60s ceiling, exponential backoff). The 5s window was too short on slower boxes; subsequent commands raced against an unready service.
- **Flatten heuristic misfired on every run.** "Files at root + box/ absent" treated the post-flatten state as broken and wiped+refetched on every consecutive `lager update`, defeating the early-exit branch and forcing ~20s of pointless container churn each run.
- **Silent flatten failures producing broken images.** Verify `~/box/lager/box_http_server.py` + `box.Dockerfile` after the flatten step; abort cleanly if missing instead of building against an incomplete tree.
- **Swallowed git errors.** `git checkout` and `git reset --hard` failures used to print only "Failed to checkout version X" without git's underlying message. Now pass stderr through.
- **Flatten artifact blocked branch switch.** A prior flatten could clobber a root-level tracked file (e.g. `README.md`), making the working tree look modified to git. `git checkout -f` discards spurious modifications from flatten artifacts so the branch switch succeeds.

### Removed
- **`lager update --all`** and the multi-box loop it drove (~145 lines). Belongs in its own command if multi-box update returns as a feature.
- **`lager update --force`.** Obsoleted by auto cache-invalidation. The escape-hatch use case (force a rebuild when the hash heuristic misses something) is rare; `docker rmi lager && lager box update` is the manual workaround.
- **`lager update --skip-restart`.** Produced a half-update state ("pull code but don't restart") with no real workflow — `ssh lagerdata@<box> 'cd ~/box && git pull'` is clearer if that's what you want.

## [0.18.1] - 2026-05-13

### Fixed
- **J-Link GDB attach no longer halts the target CPU on ~15% of attaches.** Two compounding changes in `box/lager/debug/`: (1) `gdbserver.py` no longer passes `-ir` (init registers) to `JLinkGDBServer`. The `-ir` flag briefly halts the CPU to seed its register file, but Lager doesn't need that — RTT control-block initialization happens later via `SetRTTAddr` in `detect_and_configure_rtt()`. (2) `gdb.py` now puts GDB into non-stop async mode (`set pagination off`, `set target-async on`, `set non-stop on`) *before* `tar ext`. In GDB's default all-stop mode the inferior is implicitly halted for memory reads and monitor commands, which produced the residual ~15% halt rate after the `-ir` drop landed. The non-stop flag is locked once a target is attached, so the order matters. Bench-validated by Evan over the 5/8 testing window with no halts observed.

### Improvements
- **`lager usb enable | disable | toggle` ~2.6x faster (~3.9s saved per call on Tailscale-attached boxes).** Mirrors the supply/battery fast-path migration from 0.17.x: the CLI now POSTs to `/usb/command` on the box's port 9000 Flask server instead of uploading `cli/impl/device/usb.py` and spawning a fresh Python subprocess + brainstem/pykush imports on `:5000/python` for every call. The Acroname BrainStem singleton and YKUSH per-serial LRU already live inside the long-lived box-server process, so the speedup comes from skipping per-call subprocess+import cost. Implemented as `box/lager/http_handlers/usb.py` (new) + `register_usb_routes` wired up alongside the supply/battery registrations in `box_http_server.py`. The CLI falls back to the slow path on `ConnectionError`/`Timeout`/404, so a new CLI keeps working against older box images; real handler errors (missing net, port-state) exit fast and do *not* retry the slow path, since the slow path would just reproduce the same failure. Bench-validated on the box's Acroname 8-port over Tailscale: 6.10–6.57s (slow path via 404 fallback) → 2.18–2.66s (fast path post-deploy).

## [0.18.0] - 2026-05-12

### Added
- **`lager box config` — declarative per-box provisioning.** Replaces ad-hoc SSH-and-edit-files workflows with a single JSON manifest at `/etc/lager/box_config.json` that declares mounts, named volumes, container env vars, host apt packages, sysctl settings, in-container pip packages, cargo crates, and npm packages. `lager box config apply` reconciles a Lager Box to match. Idempotent — re-applying the same config is a no-op via SHA-256 hash comparison against the last-applied snapshot (`/etc/lager/box_config.applied_hash` + `box_config.applied.json`). Full operator command surface: `init`, `show`, `validate`, `diff`, `apply` (with `--dry-run` and `--yes`), `audit`, `status`, `edit` (opens `$EDITOR`/`nano`/`vi` and round-trips through shim validation), `copy --from --to`, `import FILE`, `export FILE`, `repair`. Multi-box fanout via `--box A,B,C` on `show` and `apply`. Every section has CRUD verbs (`mount add/remove/list`, `pip add/remove/list`, `apt add/remove/list`, `cargo add/remove/list`, `npm add/remove/list`, `sysctl set/unset/list`, `env set/unset/list`, `volume add/remove/list`).
- **npm support inside the container.** New `npm_packages` first-class field with scoped (`@types/node`) and versioned (`lodash@4.17.21`) package support. The container Dockerfile installs `nodejs npm` and sets `NPM_CONFIG_PREFIX=/home/www-data/.npm-global` (pre-created and chowned to `www-data`) so `npm install -g` works without root.
- **Rust toolchain baked into the container.** Dockerfile installs rustup into `/opt/rust` (owned by `www-data`) with `RUSTUP_HOME`, `CARGO_HOME`, and `PATH` set, so `cargo install` works from the post-bounce loop without needing the operator to install rust manually. `cargo_packages` accepts `name` or `name@version`.
- **Audit log of every config mutation.** `/etc/lager/box_config.audit.log` (JSONL, append-only) records `mount-add`, `apt-add`, `set-applied-hash`, etc. with ISO-8601 timestamps. `lager box config audit [--tail N] [--since 1h] [--verb apt-add] [--json]` host command surfaces it; filters compose for "what changed in the last hour" or "every apt operation ever."
- **Sudoers auto-bootstrap.** `lager install` (new boxes) and `lager update` (existing boxes) now install `/etc/sudoers.d/lager-box-config` with the narrow NOPASSWD grants `lager box config apply` needs (`/usr/bin/apt-get` with SETENV for DEBIAN_FRONTEND, path-scoped `tee` and `rm` for the sysctl conf, `/bin/mkdir` and `/bin/chown` for mount auto-prep, path-scoped `/bin/cp` for the rollback's snapshot restore). A marker file at `/etc/lager/.boxcfg-sudoers-v2` lets `lager update` skip re-bootstrapping when the current rule shape is already installed.
- **Automatic rollback on failed bounces.** When `lager box config apply`'s container restart fails (e.g., docker rejects a malformed mount), the previously applied snapshot is restored to `/etc/lager/box_config.json` via SSH `sudo cp` (the in-container shim is unreachable when the container is dead), sysctl is reverse-diffed back to the previous values, and a re-bounce brings the box up on the prior good config. `lager box config repair --box X` exposes the same recovery as a manual verb for situations where automatic rollback can't fire (e.g., operator hand-edited the JSON to invalid syntax outside the CLI).

### Changed
- **`lager update` container startup timeout** raised from 5 to 10 minutes (`cli/commands/utility/update.py`) so first-time docker builds with cargo + npm layers don't time out on slower boxes. Same headroom for `_bounce_container`'s SSH ceiling (300s → 900s), giving the apply path room for cargo crate compilation plus pip and npm install loops.
- **`lager box config show` reads as a tree.** Bold uppercase `HOST` / `CONTAINER` group headers with horizontal-rule underlines, bold section labels indented two spaces, and `├── /└── ` branches under each section. Mounts/volumes align around `->`; env/sysctl align around `=`; empty sections render as `(none)` leaves so operators discover what's configurable. The header carries a colored `[Up To Date]` / `[Unapplied Changes!]` marker driven by a `hash` vs `applied-hash` comparison.

### Fixed
- **SSH user resolution in `lager box config`.** `default_ssh_runner` was calling `get_box_user(box_ip)` even though that helper keys by box *name*, so every box with a stored custom SSH user silently fell back to `lagerdata`. Reverse-resolved via `get_box_name_by_ip` before the user lookup. Same runner now also uses `~/.ssh/lager_box` (the dedicated key `lager install`/`lager update` set up) via `-i`, matching the rest of the CLI's SSH conventions.
- **`DEBIAN_FRONTEND=noninteractive` silently dropped on apt-get.** Default Ubuntu sudoers' `env_reset` strips `DEBIAN_FRONTEND` set as a `sudo VAR=value cmd` argument unless `SETENV:` is granted. Packages with debconf prompts (`iptables-persistent` and friends) would hang on a prompt that never showed. The new sudoers rule grants `SETENV:` only on `/usr/bin/apt-get` so the variable propagates.
- **`cargo` not found inside the container during box-config apply.** `start_box.sh`'s cargo install loop used `bash -lc` (login shell), which re-sources `/etc/profile` and resets `PATH` — wiping the Dockerfile's `ENV PATH=/opt/rust/cargo/bin:...`. Switched to `bash -c` (non-login) so the docker ENV is honored. Same fix applied to the npm install loop.
- **Real exit codes from pip/cargo/npm install loops.** The previous `if ! cmd; then _rc=$?; ...; fi` pattern in `start_box.sh` captured `$?` *after* the `!` inversion, so `_rc` was always `0` even on real failures. Refactored to `if cmd; then : else _rc=$?; ... fi` so the script's `[ERROR] ... (rc=$_rc) for: ...` messages report accurate exit codes.
- **`lager box config edit` no longer rejects valid saves with non-zero editor exit.** Some vim plugins return `1` from `:wq` even when the save succeeded. The command now compares the tempfile contents before and after the editor exits — content changed AND non-zero exit means "user saved, proceed"; content unchanged AND non-zero means "abort." Bonus: `nano` is preferred over `vi` as the fallback editor when `$EDITOR` is unset.
- **Sudoers bootstrap detection no longer false-positive.** Previous detection used `sudo -n -l <cmd>` exit code to decide if a rule was present, but Ubuntu's default `%sudo` group grants `(ALL : ALL) ALL` (with-password) which `-l` reports as "permitted" regardless of NOPASSWD status. Replaced with a marker file at `/etc/lager/.boxcfg-sudoers-v2` written during bootstrap plus a functional `sudo -n DEBIAN_FRONTEND=... apt-get --version` probe. `lager update` now correctly re-bootstraps when the marker is missing.
- **Env values containing whitespace, `$`, backticks, or single quotes survive the bounce.** `render_docker_args.py` used to emit `--env 'KEY=hello world'` to stdout, which `start_box.sh` then interpolated unquoted into `docker run` — bash variable expansion does not re-parse quotes, so values got word-split and the literal quote characters leaked through. The renderer now writes a bash-sourceable file declaring `BOX_CONFIG_MOUNTS`, `BOX_CONFIG_ENV`, and `BOX_CONFIG_HOST_PATHS` arrays via `shlex.quote`; `start_box.sh` sources that file and uses `"${BOX_CONFIG_MOUNTS[@]}"` and `"${BOX_CONFIG_ENV[@]}"` so each element preserves its content verbatim.
- **NPM_CONFIG_PREFIX in the container's Dockerfile.** Default `npm install -g` writes to `/usr/local` (root-owned) and `~/.npm`. The container runs as `www-data` (uid 33), which has no permission for either. Pre-created `/home/www-data/.npm-global` and `/home/www-data/.npm` owned by www-data + set `NPM_CONFIG_PREFIX` and prepended `/home/www-data/.npm-global/bin` to `PATH` in the image so `npm install -g X` works non-root.

### Improvements
- **`apply` shows the pending diff inline before confirming.** When `--yes` is not passed, the confirm prompt is preceded by a per-field diff of what's about to change. Closes the most common pre-apply workflow ("run diff first, then apply") into a single command.
- **Tightened sudoers rule for apt/sysctl/apply.** `tee`, `rm`, and `sysctl --system` are path-locked to the exact files/flags `lager box config apply` invokes, so a compromised `lagerdata` account cannot escalate to root via those binaries. `apt-get` and `mkdir`/`chown` stay unscoped because the package list and host paths are user-defined.
- **flock against the in-container shim.** Two concurrent `lager box config X` invocations against the same box used to do read-modify-write on `box_config.json` and silently drop one mutation. The shim now `flock`s `/etc/lager/box_config.lock` around the whole dispatch.
- **Post-apply consistency check.** After the bounce + API-ready probe but before `set-applied-hash`, the apply path re-runs `validate` + `show` against the box. If either drifts from what was bounced (i.e., the JSON was hand-edited mid-apply), `applied-hash` is left untouched and the operator is told to re-run apply.
- **In-container shim hardening.** Dispatch table replacing a 60-line if/elif chain; a `_MIGRATIONS` scaffold ready for future schema bumps; `restore-applied` verb supports the host-side rollback path; per-mutation audit log entries.

### Internal
- Cleanup tasks B–G from `BOX_CONFIG_CLEANUP.md` all landed: redundant host-side validators deleted (validation is now box-side only), shim protocol verbs centralized in `cli/commands/box/_shim_verbs.py`, the seven `*_list_cmd` host commands collapsed into one shared helper, `_render_human` driven by a registry instead of seven copy-pasted blocks, shim dispatch consolidated, inline imports hoisted to module-top.
- Test coverage: 240+ unit tests across `test/unit/box/test_box_config.py`, `test_box_config_cli.py`, `test_render_docker_args.py`, `test_host_ops.py`, `test_mount_prep.py` covering schema validation, every CLI verb, every package-manager surface, the rollback path with snapshot existence/missing/cp-failure cases, the audit log with `--since`/`--verb` filters, the `apply` pre-confirm diff, multi-box fanout, env/sysctl/mount value-alignment in the tree renderer, and edge cases around the `assh` SSH wrapper. Verified end-to-end on a real Lager Box including the quoting regression test (env values with whitespace + `$`), the rollback path (intentional duplicate-mount-point bounce failure), and full package-manager round-trips across apt/pip/cargo/npm.

## [0.17.0] - 2026-05-05

### Added
- **Concurrent J-Link probes on a single Lager Box.** `box/lager/debug/service.py` now resolves each debug net's J-Link USB serial from its VISA address and allocates a deterministic per-probe slot (read from `saved_nets.json` via `NetsCache` in `_resolve_probe`). Slot N owns a three-port window: GDB `2331+3N`, SWO `2332+3N`, telnet `2333+3N`, plus RTT base `9090+2N`. The slot stride was widened from 1 to 3 because `JLinkGDBServer`'s default `-swoport`/`-telnetport` are `2332`/`2333` and a stride of 1 collided on those auxiliary ports; auxiliary ports are now passed explicitly so the defaults can't bite. The box service passes `-select USB=<serial>` to `JLinkGDBServer` and `-SelectEmuBySN <serial>` to `JLinkExe`, writes per-serial PID and log files, and narrows `pkill` so disconnecting probe A no longer tears down probe B. `start_box.sh` publishes the widened `2331-2342` Docker port range; `secure_box_firewall.sh` admits the same range. The CLI's `--gdb-port` default changed from `2331` to `None`, so the box's allocator is no longer clobbered on every connect; the CLI prints the effective `gdb_port` the box returned. Backwards compatible: nets without a parseable serial (legacy single probe) fall back to slot 0 / 2331 / 9090 / `/tmp/jlink_gdbserver.pid`. Includes a no-DUT smoke test (`test/unit/box/test_jlink_multi_gdbserver_select.py`) that drives `start_jlink_gdbserver` end-to-end with two distinct serials and asserts per-probe `-select USB=<sn>`, distinct ports, distinct log paths, and distinct PID files.
- **Detect the RIGOL DP811 power supply.** Both `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py` now classify USB serials starting with `DP8H` or `DP81` as `Rigol_DP811` (in addition to the existing `DP82`/`DP8G` → `Rigol_DP821` and `DP8B`/`DP83` → `Rigol_DP832` mappings). The DP811 shares VID:PID `1ab1:0e11` with the DP821/DP832, so it is added to the serial-disambiguated bucket in `_VIDPID_TO_NAME` to avoid being misclassified at scan time. `lager instruments` now lists DP811 supplies plugged into a Lager Box.
- **Multiple concurrent viewers per webcam stream.** Previously each `/stream` connection in `box/lager/automation/webcam/service.py` opened its own `cv2.VideoCapture` against `/dev/videoN`, which V4L2 serves exclusively — a second viewer either failed or got blank frames. The streamer subprocess now starts a single daemon capture thread on the first viewer that owns the device and broadcasts encoded JPEG frames to a shared buffer guarded by a `threading.Condition`. Each `/stream` handler waits on the condition for the next frame and writes it to its client, so any number of viewers can subscribe concurrently. Stop and re-start each webcam to pick up the regenerated streamer script.

### Fixed
- **`LabJackADC.input()` no longer inherits sticky AIN register state from a previous tool.** `box/lager/io/adc/labjack_t7.py:LabJackADC.input` previously called `ljm.eReadName` with zero AIN register configuration, inheriting whatever device-side state a previous tool left in `AIN_RANGE`, `AIN_NEGATIVE_CH`, `AIN_RESOLUTION_INDEX`, and `AIN_SETTLING_US`. T7 register state persists in device RAM until USB power-cycle, so if a previous tool left an AIN in differential mode with a floating negative channel, every read saturated at ~10.10 V regardless of actual signal — indistinguishable from a real wiring fault. Safe defaults (`RANGE=10.0`, `NEGATIVE_CH=199`, `RESOLUTION_INDEX=0`, `SETTLING_US=0`) are now written once per `(handle, channel)` tuple before the first `eReadName`, cached in a class-level set. Config-write failures are logged but do not raise.

### Improvements
- **Webcam capture forces MJPEG so two cameras can share a USB 2.0 bus.** Default OpenCV negotiation in `box/lager/automation/webcam/service.py` picked YUYV (uncompressed, ~150 Mbps at 640×480 30fps), which doesn't leave room for a second camera on the same bus — the kernel rejected `VIDIOC_STREAMON` with "Not enough bandwidth for altsetting". MJPEG is roughly 5× smaller and fits two cameras comfortably. The `FOURCC` is now set before width/height/fps so negotiation honors it.

## [0.16.10] - 2026-05-01

### Fixed
- **`lager debug connect` surfaces the real Segger error when J-Link cannot reach the target.** When J-Link's multi-speed retry loop in `box/lager/debug/api.py:connect_jlink` exhausted without ever reaching a target, `status['logfile']` could be set to `None` rather than absent, so `status.get('logfile', 'No log available')` returned `None` and the downstream `clean_logfile_content(None)` crashed with `AttributeError: 'NoneType' object has no attribute 'replace'` — masking the real Segger "Connecting to target failed" message that operators need to see in the dashboard. `connect_jlink` now coerces `None` to `'No log available'` at the call site, and `clean_logfile_content` returns `''` when given `None` as defense in depth.

### Internal
- Bumped seven transitive Rust dependencies in `box/oscilloscope-daemon/Cargo.lock` (`quinn-proto` → 0.11.14, `rustls-webpki` → 0.103.13, `time` → 0.3.47, `bytes` → 1.11.1, `tracing-subscriber` → 0.3.20, `rand` 0.8.6 and 0.9.4) to clear ten Dependabot security advisories on the daemon's QUIC/TLS stack. Lockfile-only change with no runtime effect on existing boxes until the daemon is rebuilt; verified with a full release build + libps2000 link on Picoscope hardware.

## [0.16.9] - 2026-04-29

### Fixed
- Resolves the **sequential half** of the Keithley 2281S dual-role known limitation from v0.16.7. When `supply1` (role `power-supply`) and `battery1` (role `battery`) are configured on the same physical Keithley 2281S, the box now opens exactly one pyvisa session per VISA address — shared by both driver classes — instead of opening two sessions and hitting `[Errno 16] Resource busy` on the second open. Scripts can alternate `lager supply <net>` and `lager battery <net>` commands against the same Keithley without restarting the box. Implemented in `box/lager/hardware_service.py` as a process-wide `_visa_resources` cache keyed by address, plus a `raw_resource=` kwarg on `box/lager/power/supply/keithley.py:create_device` and `box/lager/power/battery/keithley.py:create_device`. Both drivers track an `_owns_resource` flag so `close()` does not release the underlying USB claim when the session is shared. SCPI serialization moved to a per-address lock so supply and battery commands targeting the same Keithley serialize correctly. (Note: genuinely concurrent supply + battery operation against one Keithley is not supported by the instrument's firmware — its Power Supply and Battery Simulator entry functions are mutually exclusive — so configure one role per Keithley if you need both running at once. See Known Limitations in the release notes.) No behavior change for single-role drivers (Rigol DP821, Keysight E36xxx, EA PSB) — they continue to use the legacy per-driver-opens-its-own-session path.
- Resolves the **Keithley 2281S concurrent battery TUI + CLI known limitation** from v0.16.7. The retry path in `box/lager/hardware_service.py:/invoke` now calls `_close_device(old_device, cache_key)` *before* invoking `module.create_device(net_info)`, releasing the popped instance's USB claim so the new pyvisa session can open cleanly. Previously the popped `KeithleyBattery`/`Keithley2281S` instance stayed alive in the process and held the libusb claim, causing the recreated session's `pyvisa.ResourceManager().open_resource(addr)` to fail with `[Errno 16] Resource busy` (surfaced as `Could not open instrument at ...`). For drivers that share a pyvisa session (Keithley dual-role), the retry path also closes-and-reopens the shared resource instead of closing-and-reusing the same already-broken handle.
- **Keithley 2281S supply method-signature compatibility.** `box/lager/http_handlers/supply.py` is modeled on multi-channel drivers (Rigol DP800) and calls supply-driver methods with a `channel=` kwarg or positional channel. The Keithley 2281S supply driver follows the `SupplyNet` abstract (no `channel` parameter — the 2281S is single-channel), so the very first call into a Keithley supply hit a `TypeError` that the handler treated as hardware failure and triggered `/cache/clear`, closing the shared pyvisa session that this release's dual-role fix had just opened. `Keithley2281S.output_is_enabled` now accepts (and ignores) a `channel=None` kwarg, and six new public OCP/OVP wrapper methods (`set_overcurrent_protection_value`, `enable_overcurrent_protection`, `set_overvoltage_protection_value`, `enable_overvoltage_protection`, `clear_overcurrent_protection_trip`, `clear_overvoltage_protection_trip`) delegate to the existing private `_set_ocp` / `_set_ovp` and public `clear_ocp` / `clear_ovp` methods so the handler can call them without `AttributeError`. No new SCPI logic.
- **`lager battery <net> state` no longer falls through to a competing pyvisa session.** The battery CLI sends `action='print_state'` (the dispatcher function name), but `box/lager/http_handlers/battery.py:/battery/command` only recognized `action='state'` (matching the supply handler's name). The unrecognized action returned HTTP 400 and the CLI's `_run_backend` fell through to the python:5000 dispatcher path, which opened its own pyvisa session against the same Keithley and immediately collided with the shared session that hardware_service had just opened during the previous supply command — surfaced as `Could not open instrument at USB0::...: failed to set configuration [Errno 16] Resource busy`. `/battery/command` now accepts both `'state'` and `'print_state'`, so the CLI stays on the WebSocket → hardware_service path and reuses the shared pyvisa session this release introduces.
- **Removed the v0.16.5 `/cache/clear` band-aid from `lager python` script exit.** `cli/commands/development/python.py` previously POSTed `/cache/clear` to `hardware_service` on every script exit, Ctrl+C, and BrokenPipeError. v0.16.9 owns one persistent pyvisa session per VISA address inside hardware_service and shares it across CLI/TUI/script callers, so tearing the cache down on every script exit defeated the design and forced a re-open that often raced libusb's asynchronous release-interface (surfaced as `[Errno 16] Resource busy` on the next supply or battery command). The clears are removed; hardware_service's cache now persists for the container's lifetime as intended.
- **`hardware_service._get_or_open_visa_resource` retries on transient `Resource busy`.** When pyvisa-py + libusb returns `[Errno 16] Resource busy` on `open_resource()` — typically because a previous claim hasn't been fully released by the kernel — the open is now retried with an exponential backoff (`0.2, 0.5, 1.0, 2.0` seconds) before giving up. This makes the shared-session path resilient to libusb's async release-interface timing window without papering over genuine "device unplugged" or wiring failures.
- **`POST /cache/clear` no longer tears down shared pyvisa sessions.** The endpoint still closes cached driver wrappers and removes them from `device_cache` so a wedged driver can recover on the next `/invoke`, but the underlying per-VISA-address shared session that v0.16.9 introduced is now retained across calls — clearing it on every CLI script exit (which is what older `lager python` clients still do) defeated Phase 2's design and re-introduced the libusb release-interface race surfaced as `[Errno 16] Resource busy`. A new `POST /cache/clear_all` endpoint preserves the old behavior for the rare case (USB unplug/replug, manual debugging) where a full reset is genuinely required.
- **Cross-role concurrent use on a single Keithley 2281S now fails fast with a clear error instead of cryptic SCPI/Resource-busy traces.** The 2281S's Power Supply (`:ENTR:FUNC POW`) and Battery Simulator (`:ENTR:FUNC BATT`) entry functions are mutually exclusive in firmware — running a `lager supply <net> tui` against the same physical Keithley while simultaneously running a `lager battery <net>` command (or vice-versa) made the two clients fight over the entry function on every poll. The box now tracks the active monitoring sessions per role in `box/lager/http_handlers/state.py` (sessions store the resolved VISA address on start) and refuses an opposite-role command — both at `/supply/command` / `/battery/command` and at `start_supply_monitor` / `start_battery_monitor` — when the same address is already in active use, with a message that names the conflicting net and explains the hardware constraint. Sequential CLI cross-role workflows (which never populate the active-session dicts) are unaffected and continue to work via Phase 2's shared pyvisa session.

### Removed (was Known Limitations in v0.16.7)
- The two Keithley 2281S workarounds documented in v0.16.7 are no longer needed.

## [0.16.8] - 2026-04-28

### Added
- Recognize the SEGGER J-Link Flasher PRO (USB `1366:0105`) as a supported `debug` instrument. Added `J-Link_Flasher_Pro` to `SUPPORTED_USB` and `CHANNEL_MAPS` in both `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py`, so `lager instruments` now lists the device when it is plugged into a Lager Box.

## [0.16.7] - 2026-04-28

### Fixed
- `lager uart <net>` returned `404 — UART net not found` for every UART command, even when `lager nets` correctly listed the net. The v0.16.6 battery-handler consolidation (commit `f277402`) deleted the two-line `register_uart_routes(app)` / `register_uart_socketio(socketio)` block in `box/lager/box_http_server.py` as collateral damage. Imports stayed in place so the file still parsed; the Flask route just was never registered. Re-added the registration alongside supply and battery.
- `lager supply <net> state` (and any other one-shot supply or battery command) failed with `[Errno 16] Resource busy` immediately after exiting the TUI, succeeding only on the second invocation. Root cause: `/supply/command` and `/battery/command` returned 404 when no active WS session was found, forcing the CLI's `_run_backend` into a direct-pyvisa subprocess fallback (`cli/impl/power/supply.py` → dispatcher) that opened its own pyvisa session, conflicting with the still-cached session in `hardware_service.py`. Both endpoints now build a transient `Device` proxy via `resolve_net_proxy()` when no active WS session exists, routing through `hardware_service.py:/invoke` like the WS monitor already does. There is now exactly one pyvisa session per `(device_name, address)` regardless of TUI lifecycle. This completes v0.16.6's "VISA session ownership unified" promise.
- Concurrent TUI + CLI access on the same supply (e.g. `lager supply <net> tui` running while another terminal runs `lager supply <net> current`) no longer cascades `Resource busy` errors across subsequent commands. Previously a single transient kernel-level USB-claim collision matched the substring `'resource'` in `_is_visa_session_error()` and triggered the stale-session retry path, which popped the live cache entry and called `module.create_device()` on the same address — that second open hit `Resource busy` again because the original session was still alive in the same process, turning an isolated collision into a chain of failures. Removed `'resource'` from `_VISA_SESSION_ERROR_KEYWORDS` in `box/lager/hardware_service.py`; retry now fires only for genuine stale-session signals (`'session'`, `'closed'`, `'invalid'`). An isolated USB-busy collision is still possible on heavily-contended USB transfers but is now returned to the caller cleanly without disturbing the cache, so the next command immediately succeeds.

### Known Limitations
- Keithley 2281S configured with both a supply role (`power-supply`) and a battery role (`battery`) on the same physical USB device cannot be used concurrently (or sometimes even sequentially without restarting the box service). `box/lager/hardware_service.py` keys its driver cache by `(device_name, address)`, and the supply path uses `device_name="keithley"` while the battery path uses `device_name="keithley_battery"` — so the same USB device gets two distinct cache entries and two competing pyvisa sessions, the second of which fails with `[Errno 16] Resource busy`. Workaround: configure either the supply role or the battery role on the Keithley 2281S, not both. Proper fix (shared pyvisa Resource or merged driver class) is targeted for v0.16.8.
- Concurrent battery TUI + CLI on the Keithley 2281S can surface `[Errno 16] Resource busy`. Running `lager battery <net> tui` in one terminal while running `lager battery <net> state` (or any other one-shot battery CLI command against the same net) in another terminal can fail with `Resource busy`, even when only the battery role is configured on the Keithley (so this is distinct from the dual-role limitation above). The Bug-B retry-classification fix prevents this from cascading across subsequent commands, but does not eliminate the initial collision; the underlying contention appears to live in the Keithley pyvisa session itself rather than in `hardware_service.py`'s lock. Workaround: do not invoke battery CLI commands while a battery TUI is open against the Keithley 2281S — close the TUI first, or run TUI-only or CLI-only. Root-cause investigation tracked for v0.16.8.

## [0.16.6] - 2026-04-27

### Fixed
- `lager battery <net> tui` now works for the first time. The OLD WebSocket battery monitor in `box/lager/box_http_server.py` imported `_resolve_net_and_driver` from `lager.power.battery.dispatcher`, but the battery dispatcher (unlike the supply dispatcher) had no module-level wrapper of that name — every TUI launch crashed at module load with `ImportError: cannot import name '_resolve_net_and_driver' from 'lager.power.battery.dispatcher'`. Nobody had reported it because nobody had tested the battery TUI. Incidentally fixed by the VISA-ownership unification below; the battery monitor now also emits a `battery_driver_ready` event mirroring `supply_driver_ready` for client symmetry.
- Concurrent SCPI access on the same instrument now serializes correctly. Previously, two `/invoke` requests against the same cached driver in `box/lager/hardware_service.py` could race on the SCPI bus and produce `Query INTERRUPTED` pyvisa errors. Added a per-`(device_name, address)` `threading.Lock` that wraps the actual `func(*args, **kwargs)` call and the stale-VISA-session retry; lock is acquired per call (never held across calls). Multi-channel devices (e.g., Rigol DP821) correctly share one lock since they share one VISA session.

### Changed
- **VISA session ownership unified.** The supply and battery WebSocket monitor handlers (`box/lager/http_handlers/supply.py`, `box/lager/http_handlers/battery.py`) no longer open their own pyvisa sessions in monitor threads. They now hold a `Device` HTTP proxy (`box/lager/nets/device.py`) and route every per-tick driver call (and every TUI command) through `hardware_service.py:/invoke`. `hardware_service.py` (port 8080) is now the sole owner of pyvisa sessions per `(device_name, address)`. The v0.16.5 `POST /cache/clear` band-aid in the WS monitor is removed; the architectural fix replaces it.
- Battery handlers migrated from `box/lager/box_http_server.py` to `box/lager/http_handlers/battery.py`, mirroring the earlier supply migration. The duplicate copies in `box_http_server.py` (~670 lines: `/battery/command` HTTP route, four `/battery` WebSocket handlers, the `monitor_battery` thread) were deleted; `box_http_server.py` now imports and registers the modular handlers via `register_battery_routes` / `register_battery_socketio` / `cleanup_battery_sessions`.
- New shared helper `box/lager/dispatchers/helpers.py:resolve_net_proxy(netname, role, error_class)` returns `(device_module_name, net_info, channel)` for a saved net, mirroring the regex switches in `SupplyDispatcher._choose_driver` and `BatteryDispatcher._choose_driver`. Used by both monitor handlers to construct their `Device` proxies.
- `box/lager/power/supply/ea.py` now exposes a `create_device(net_info)` factory for `hardware_service.py:/invoke`, matching the other supply drivers.
- Two unit tests in `test/unit/cli/test_performance_improvements.py` (`test_config_parsing_cached`, `test_config_cache_invalidation_on_write`) had been silently failing since the `.lager` config format was migrated to JSON-only: they wrote INI/configparser tempfiles, but `cli/config.py:read_config_file` calls `json.load()` and `raise SystemExit(1)` on `JSONDecodeError`. Both tempfiles now write `{"LAGER": {...}}` JSON. Full unit suite now 141/141 passing (was 139/141). No CLI behavior change.

## [0.16.5] - 2026-04-27

### Fixed
- `lager supply <net> state` (and other read-only supply commands) would report `Enabled: OFF` immediately after a successful `lager supply <net> enable` on Keysight E36xxx supplies. The `KeysightE36000` constructor unconditionally called `disable_output()` as a "safe default" on every connect, so each fresh CLI invocation silently turned the output off before running its query. The disable is now gated behind the explicit `reset=True` flag (matching the existing OCP-reset block), so constructing a driver for a read or for `enable` no longer mutates output state.
- `lager supply <net> enable` on EA PSB supplies briefly dropped the output (~500ms) when the output was already on, because `EA.enable()` always ran `_clear_latched_events()` (which writes `OUTPut OFF` and waits 200ms) before turning the output back on. `enable()` is now idempotent: if `OUTPut?` reports the output is already on, it returns immediately without toggling. The off→on path that needs latched-protection clearing is unchanged.
- `lager supply <net> tui` would close after ~5 seconds with no visible error on Rigol DP821 supplies whenever a direct supply command (e.g. `lager supply <net> state`) had been run beforehand. The WebSocket monitor in `box/lager/http_handlers/supply.py` was opening its own pyvisa session via `_resolve_net_and_driver()`, conflicting with the cached VISA session held by `hardware_service.py` on port 8080 — instruments that don't tolerate concurrent USB sessions hung silently and `supply_driver_ready` never fired. The monitor thread now POSTs to `localhost:8080/cache/clear` before opening its session so the cached handle is released first. In the same path, `get_channel_limits()` and the session-store block now emit a visible `error` event on any failure (extending the pattern around `_resolve_net_and_driver`), so init failures no longer disappear into a silent 15-second timeout. The CLI captures the TUI's exit reason and prints it red to stderr after Textual's alt-screen tears down, so the message survives the screen restore.

## [0.16.4] - 2026-04-27

### Fixed
- `/instruments/list` no longer returns an empty list when served from a `ThreadingHTTPServer` worker thread. `scan_usb()`'s `with_timeout` decorator uses `signal.signal(SIGALRM, ...)`, which only works on the main thread; the resulting `ValueError` was silently swallowed by the handler, making boxes appear to have no instruments connected (e.g. LabJack T7) even when devices were plugged in. The scanner now falls back to a no-timeout direct call when not on the main thread; the inner serial/sysfs reads already have their own I/O timeouts. The CLI path was unaffected because `query_instruments.py` runs as a subprocess.

## [0.16.3] - 2026-04-24

### Added
- `lager boxes` (and `lager boxes list`) now shows a `user` column between `ip` and `version`, so boxes configured with a non-default SSH user are visible at a glance. Boxes added without `--user` display as `lagerdata` (the default).

## [0.16.2] - 2026-04-17

### Fixed
- Corrected the USB PID for the Keysight E36313A power supply (`2a8d:1202`) in `SUPPORTED_USB` for both `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py`; previously the PID was a placeholder (`????`) so the device was never recognized. Added a matching udev rule in `box/udev_rules/99-instrument.rules` (`MODE=0666` plus `usbtmc` unbind on `bind`) so PyVISA can open the instrument directly via libusb.

## [0.16.1] - 2026-04-13

### Fixed
- `bench_loader` no longer crashes when `bench.json` or `saved_nets.json` contains explicit `null` values for list or dict fields (`test_hints`, `tags`, `aliases`, `params`, `net_overrides`, `dut_slots`, `interfaces`, `channels`). Previously, `dict.get(key, default)` only substituted the default when the key was absent, so a literal `"test_hints": null` would return `None` and break downstream iteration. All affected sites now use `dict.get(key) or default` so an explicit `null` is treated the same as an absent key.

## [0.16.0] - 2026-04-13

### Added
- Lager MCP (Model Context Protocol) server, running on the box on port 8100 (FastMCP, streamable-http). Allows AI agents to discover a Lager setup and understand how nets are wired to the DUT.
- Net metadata fields: `description`, `dut_connection`, `test_hints`, and `tags`. New CLI commands and TUI flows under `lager nets` for editing them.
- Capability graph and heuristic engine (`box/lager/mcp/engine/`) that map test types to the nets available on a bench.
- Auto-generated MCP API reference built from driver introspection at image build time. The Dockerfile build now fails fast on driver renames.
- Defensive `bench.json` parser so a single malformed entry can no longer break `discover_bench`.
- New integration test `test_agent_loop` and unit tests for the bench loader, capability graph, heuristic engine, safety preflight, and MCP schemas.

### Changed
- The MCP server has moved from the CLI (`cli/mcp/`) to the box (`box/lager/mcp/`). It is now started by `start-services.sh` inside the Docker container rather than running on the developer machine.
- Every MCP tool call is now wired through an `@audited` decorator that records the call via `audit.log_tool_call`, so downstream control planes can rely on a consistent audit trail.
- `quick_io` writes now go through a `preflight_check` that enforces voltage, current, and dangerous-action constraints before hitting hardware.
- MCP errors no longer return raw tracebacks to agents; `NetType()` inputs are validated against the enum.
- `plan_firmware_test` now uses a regex-based pattern split instead of the previous unsafe `get_pattern` split.

### Security
- The `run_lager` MCP passthrough tool is now gated behind the `LAGER_MCP_ALLOW_RUN_LAGER` environment flag and is **off by default**. Operators must opt in explicitly before agents can invoke arbitrary `lager` commands.

## [0.15.2] - 2026-04-08

### Added
- `lager install --version` now accepts a release tag (e.g. `v0.15.0`) in addition to a git branch, so a box can be installed at a pinned version directly. The deployment script detects tags and uses the bare ref for `git reset --hard` instead of the (non-existent) `origin/<tag>` ref.

### Changed
- `lager install --branch` has been replaced by `lager install --version`. The new flag accepts both branches and release tags.

### Fixed
- Reverted DA1469x post-flash reset to use J-Link Commander register writes (as in 0.15.0). The GDB-based reset introduced in 0.15.1 caused regressions on DA1469x targets; Commander remains the supported path.

## [0.15.1] - 2026-04-07

### Fixed
- DA1469x post-flash reset now uses GDB-based reset instead of J-Link Commander register writes, fixing unreliable behavior on DA1469x targets after flashing. The target is reset via `gdb_reset(halt=False)` and the GDB server is stopped so the application runs freely.

## [0.15.0] - 2026-04-02

### Added
- `lager boxes lock` now accepts a `--user` flag to lock as a specific username, useful when running inside a Docker container where the effective user would otherwise be `root`

### Changed
- `lager boxes` now shows a warning when any box is locked as `root`, with instructions to use `--user` or `lager defaults add --user`
- `LAGER_USER` environment variable is now the highest-priority source when determining the lager user for lock operations (before `~/.lager` config and the OS username)
- Lock output and error messages now display the user's email address when available. External tools that lock boxes using the `<tool>:<id>:<email>` lock format will have their email extracted and shown rather than the raw lock string
- `lager update` SSH operations now use `StrictHostKeyChecking=accept-new` to avoid host-key prompts on first connection to a new box
- `lager update` Docker rebuild step now correctly passes the explicit SSH key file when one is in use
- `lager update` stop/remove step now targets the `lager` and `pigpio` containers by name instead of stopping all running containers

### Removed
- `lager boxes connect` command

## [0.14.4] - 2026-03-31

### Changed
- `lager debug flash` now erases flash by default before programming, ensuring a clean boot state. Use `--no-erase` to skip. The `--erase` flag is retained for backwards compatibility.

## [0.14.3] - 2026-03-31

### Fixed
- Supply net current limit no longer gets automatically reset to 1A on TUI startup or any CLI command
- OVP value now correctly displays in `lager supply state` output

### Changed
- Release version branches are now named `X.Y.Z` (without the `v` prefix) to distinguish them from the `vX.Y.Z` tag

## [0.14.2] - 2026-03-30

### Fixed
- `lager debug erase` and `lager debug flash` now correctly pass the JLinkScript to J-Link during the connect step. Previously only `gdbserver` passed the script, causing erase/flash to fail on MCUs that require a JLinkScript to load the correct flash algorithm (e.g. DA1469x with external QSPI flash)
- For DA1469x targets, erase now uses address-range erase instead of chip erase, and no longer halts after erase when flashing
- Fixed crash in `flash_device` when RTT is subsequently run after flashing
- Improved J-Link process management: stale PID files are now cleaned up, and JLinkGDBServer is stopped before chip erase operations to ensure exclusive hardware access

## [0.14.1] - 2026-03-24

### Fixed
- `lager update --version v0.14.0` (and any version tag) now works correctly. Previously, version tags were incorrectly resolved as remote branch refs (`origin/v0.14.0`), causing the update to fail when git could not find the ref. Tags are now resolved directly.

## [0.14.0] - 2026-03-24

### Added
- `lager install-wheel` command to install a local Python wheel file on a Lager Box. Automatically uninstalls any previously installed version of the package before installing, so the version number does not need to be bumped on every rebuild. The package name is parsed from the wheel filename per the wheel specification.

## [0.13.4] - 2026-03-23

### Removed
- **Ephemeral command lock** — The automatic command-in-progress lock that fired on every CLI command has been removed. It had multiple corner cases: supply commands never released the lock, long-running commands (like `gdbserver`) blocked all other commands, etc.
- `--force-command` flag removed from all commands (no longer needed)

### Note
- User lock (`lager boxes lock/unlock`) is unchanged — use it to reserve a box for yourself

## [0.13.3] - 2026-03-21

### Fixed
- `lager python --detach` now correctly holds the command lock while the detached process runs. Previously the lock was released immediately, allowing other commands to run against a busy box

## [0.13.2] - 2026-03-21

### Changed
- Maintenance release: updated repository configuration

## [0.13.1] - 2026-03-20

### Fixed
- `lager ssh` now properly releases the command lock when the SSH session ends. Previously, `os.execvp` replaced the Python process, preventing cleanup handlers from running, which left boxes stuck in "busy" state until the 30-minute auto-expiry

## [0.13.0] - 2026-03-20

### Added
- `--force-command` is now a local flag on all subcommands that target a box, not just a global flag
- `--force-command` added to `hello`, `install`, `uninstall`, and `boxes connect` commands

### Changed
- `lager python --detach` now keeps the command lock until the detached process finishes on the box. The lock is automatically released when the script completes
- `acquire_command_lock_with_cleanup` now checks `ctx.obj.force_command` automatically, so all commands that acquire locks support `--force-command`

### Fixed
- Locking documentation updated to reflect current behavior

## [0.12.0] - 2026-03-20

### Added
- **Command-in-progress lock** — When a `lager` command is running on a box, all other commands are blocked with a "Command in progress" error, including from the same user. Locks auto-expire after 30 minutes to handle crashed CLI processes
- **User lock (`lager boxes lock/unlock`)** — Explicitly lock a box so only you can run commands on it. Other users see a lock error until you unlock. The user who locked it can still run commands
- `--force-command` global flag to bypass command-in-progress locks
- `lager boxes` list now shows "locked by" and "busy" columns when any box has a lock or command in progress
- `lager python --kill`, `--kill-all`, and `--reattach` skip lock checks (management operations)

### Changed
- Command lock is process-based — same user cannot stomp on their own commands unless using `--force-command`
- Hardcoded control plane URL, removed `--url` flag from `lager boxes connect`

## [0.11.0] - 2026-03-18

### Added
- Nordic PPK2 (Power Profiler Kit II) support as a watt-meter and energy-analyzer instrument
- `lager watt` reads instantaneous power from PPK2 nets
- `lager energy <net> read` integrates energy and charge over a configurable duration
- `lager energy <net> stats` computes current/voltage/power statistics (mean, min, max, std)
- PPK2 auto-detection via `lager instruments` and `lager nets add-all`
- Python API: `Net.get(name, type=NetType.WattMeter)` and `Net.get(name, type=NetType.EnergyAnalyzer)` for PPK2 nets
- Unit tests for PPK2 location parsing, dispatcher routing, singleton caching, and measurement math
- Integration test suite for PPK2 hardware validation (`test/api/sensors/test_ppk2.py`)
- Webcam start/stop HTTP endpoints for dashboard control

### Changed
- `lager energy` command now uses `lager energy <NETNAME> <subcommand> --options` argument order (consistent with other commands like `lager supply`)

### Fixed
- Webcam MJPEG stream 404 for dashboard `/stream/{netName}` requests
- `Net.get()` now falls back to `address` field when `location` is not set in saved net config

## [0.10.0] - 2026-03-17

### Added
- `lager router` command group for managing routers as Lager nets
- `lager router add-net` to register a router (MikroTik hAP or compatible) as a net on a Lager Box
- `lager router connect` to verify connectivity to a router net
- `lager router interfaces` and `lager router wireless-interfaces` to inspect network interfaces
- `lager router wireless-clients` to list connected wireless clients
- `lager router dhcp-leases` to list devices that have received IP addresses
- `lager router system-info` to query router resource usage
- `lager router reboot` to reboot a router net
- `lager router enable-interface` / `lager router disable-interface` for wireless interface control
- `lager router block-internet` to drop all forwarded traffic for network isolation testing
- `lager router reset` to restore a router to a clean baseline state (removes test firewall rules, bandwidth limits, and access list entries)
- `lager router run` for arbitrary REST API calls against the router

## [0.9.0] - 2026-03-16

### Added
- `disconnect_wifi()` standalone function for the Python WiFi API
- `lager boxes` now reads project-level `.lager` files, not just the global `~/.lager`
- WiFi Python API docs updated to use standalone functions

### Fixed
- `lager boxes` showing empty results in fresh Docker containers when boxes were defined in a project-level `.lager` file
- Typo in `wifi/status.py`

## [0.8.0] - 2026-03-12

### Added
- RTT RAM search parameters for Python API: `dbg.rtt(search_addr=, search_size=, chunk_size=)`
- RTT RAM search CLI flags: `--rtt-search-addr`, `--rtt-search-size`, `--rtt-chunk-size`
- Instruments and nets HTTP handlers on Lager Box

### Fixed
- PID file path mismatch: `status()` and `rtt()` now check both `/tmp/jlink.pid` and `/tmp/jlink_gdbserver.pid`
- `detect_and_configure_rtt()` now detects running debugger correctly (was always reporting "No debugger connection")
- `erase_flash()` and `read_memory()` now check both PID file paths

## [0.7.0] - 2026-03-10

### Added
- `lager devenv terminal --attach <container_name>` to attach to a running Docker container
- `lager devenv terminal --shell <path>` to override the shell when attaching
- Jobs WebSocket client in control plane heartbeat for receiving and executing job dispatch commands

### Changed
- Default control plane URL changed to `https://api.stoutdata.ai`

## [0.6.0] - 2026-03-06

### Added
- `lager python --reattach <ID>` to stream output from detached processes (replays from start)
- `lager python --kill <ID>` to kill a specific detached process
- `lager python --kill-all` to kill all running `lager python` processes on a box
- Ctrl+D during `--reattach` detaches without killing the process
- 10 MB log cap for detached process output to prevent disk abuse
- Runtime warning when system-installed `lager` CLI shadows a virtual environment version

### Fixed
- Ctrl+C during `lager python` no longer breaks the Acroname USB hub (required box reboot before)
- `--detach` no longer hangs; returns immediately with process ID and reattach/kill hints
- `--kill` now actually kills the process (was silently doing nothing)
- `--kill <invalid-id>` shows a friendly error instead of a traceback
- Multi-user box provisioning: new users are always added to the docker group
- `start_box.sh` uses `$HOME` instead of hardcoded `/home/lagerdata` paths

### Changed
- `--kill` changed from a boolean flag to an option that takes a process ID
- Detached process output now shows box name instead of IP address

## [0.5.0] - 2026-03-05

### Added
- Control plane heartbeat client (`control_plane_client.py`) for WebSocket-based box status reporting
- `/status` endpoint on both Flask and Python HTTP servers returning box health, version, and nets
- `lager boxes connect` command to configure a box for control plane heartbeat reporting
- `websocket-client` dependency added to box Docker image

### Changed
- Refactored version file reading in `service.py` into reusable `_read_box_version()` helper
- `start-services.sh` starts control plane heartbeat when configured

## [0.4.2] - 2026-03-04

### Improved
- `lager install` and `lager uninstall` now provide detailed SSH error diagnostics (connection refused, no route to host, host key changes)
- `lager uninstall` supports `--dry-run` flag to preview what would be removed without making changes
- Deployment script uses SSH connection multiplexing for reliability over VPN connections
- Shared `host_in_known_hosts` utility extracted to `ssh_utils` for consistent host key handling across commands

## [0.4.1] - 2026-03-03

### Fixed
- `lager install` GitHub connectivity check now uses `git ls-remote` instead of `curl`, fixing deployment failures on boxes where `curl` is not installed (e.g. Ubuntu 24.04)

## [0.4.0] - 2026-03-03

### Changed
- `lager install` deploys box code via HTTPS git clone instead of SSH, removing the need for GitHub deploy keys
- `lager update` automatically migrates existing boxes from SSH to HTTPS remote URLs

### Improved
- Open-source release: repository is publicly accessible, enabling installation and updates without GitHub credentials

## [0.3.27] - 2026-02-19

### Added
- `lager-mcp` console script entry point for easier MCP server setup with AI assistants

## [0.3.26] - 2026-02-19

### Added
- Full Model Context Protocol (MCP) server with 165+ tools across 21 modules
- 254 unit tests and 64 integration tests for MCP coverage

## [0.3.25] - 2026-02-18

### Added
- Full FT232H USB cable support for SPI, I2C, and GPIO
- GPIO hold mode

### Fixed
- SPI config persistence between CLI commands
- LabJack T7 auto-CS reliability
- FT232H USB resource cleanup

## [0.3.24] - 2026-02-17

### Added
- CLI update notifications checking PyPI in background with 24-hour cache

### Fixed
- Duplicate SPI channel in LabJack T7 instrument query

## [0.3.23] - 2026-02-16

### Added
- LabJack pin conflict detection for multi-subsystem usage
- Major documentation overhaul for I2C, SPI, power supply, scope, ADC, GPI, GPO

## [0.3.22] - 2026-02-16

### Changed
- Removed LabJack T7 pin conflict restrictions; dynamic configuration at transaction time

## [0.3.21] - 2026-02-15

### Fixed
- `lager update` version file write timing with retry logic

## [0.3.20] - 2026-02-13

### Added
- Aardvark GPIO support
- SPI chip select (CS) control for Aardvark and LabJack
- GPI direction configuration
- Natural sorting for CLI list output

## [0.3.19] - 2026-02-09

### Added
- I2C protocol support (Aardvark, LabJack T7)
- FT232H adapter support
- Joulescope JS220 watt meter
- Net TUI enhancements with rename/delete

### Fixed
- SPI and Aardvark reliability improvements

## [0.3.18] - 2026-01-30

### Added
- JLinkScript storage with debug nets via `lager nets set-script`

### Fixed
- Power supply VISA session staleness
- Increased `lager update` SSH timeout

## [0.3.17] - 2026-01-29

### Added
- SPI communication support via LabJack T7 (modes 0-3, configurable frequency and word size)

### Fixed
- GPIO handle closing SPI connections
- LabJack SPI 800kHz workaround

## [0.3.16] - 2026-01-26

### Added
- J-Link script file support for custom initialization
- Expanded ARM device support to 70+ families

### Fixed
- "Resource Busy" USB errors
- Debug reset reliability for Cortex-M33

## [0.3.15] - 2026-01-20

### Fixed
- `lager update` command status synchronization
- `lager update --all` box synchronization

## [0.3.14] - 2026-01-20

### Improved
- Enhanced error messages with actionable guidance
- Input validation for numeric, address, and package parameters
- Connection error handling with platform-specific hints

## [0.3.13] - 2026-01-16

### Added
- Interactive Lager Terminal (REPL) with tab completion and command history
- `lager update --all` and `--needs-update` flags
- Live box status with version checks

## [0.3.12] - 2026-01-15

### Fixed
- Keysight E36233A Supply TUI support
- Dispatcher import error
- SCPI measurement commands and negative zero display

## [0.3.11] - 2026-01-15

### Fixed
- Supply TUI VISA resource lock on force-close
- Keysight E36200 output state preservation

## [0.3.10] - 2026-01-15

### Added
- Lager Terminal integrated into CLI (run `lager` with no args)

## [0.3.9] - 2026-01-15

### Fixed
- Supply TUI import error after dispatcher refactoring

## [0.3.8] - 2026-01-15

### Added
- Global VISA connection manager preventing "Resource busy" errors
- TestResult schema for structured test data
- Power supply/battery drivers return numeric values

## [0.3.7] - 2026-01-15

### Fixed
- Keysight E36233A incorrectly identified as E36313A

## [0.3.6] - 2026-01-15

### Added
- Enhanced `lager boxes sync` with version comparison

### Fixed
- `lager update` sudoers issues
- Container startup timeout increased to 5 minutes

## [0.3.5] - 2026-01-09

### Changed
- `lager install` works without lager repository

## [0.3.4] - 2026-01-07

### Added
- Custom SSH username support via `--user` flag for install/uninstall

## [0.3.3] - 2026-01-07

### Changed
- PyPI package includes deployment scripts; `lager install` works from PyPI

## [0.3.2] - 2026-01-07

### Added
- `--box` flag for install/uninstall commands

### Changed
- Uninstall default preserves `/etc/lager`

## [0.3.1] - 2026-01-05

### Changed
- Major codebase restructure: CLI commands reorganized into logical groups
- Box code reorganized by domain (power, io, measurement, protocols, automation)

### Added
- Logitech C930e webcam support

### Removed
- Legacy OpenOCD code (J-Link is now the only debug backend)
- Backward compatibility import stubs

## [0.2.36] - 2025-12-18

### Changed
- Terminology restructure: "gateway/DUT" renamed to "box" throughout codebase
- Directory flattened from `gateway/lager/lager/` to `box/lager/`

## [0.2.35] - 2025-12-17

### Changed
- 14 Python API function renamings for consistency

### Fixed
- ARM/robot serial port hangs and position polling
- Battery API mapper

## [0.2.33] - 2025-12-15

### Changed
- `lager hello` displays actual hostname instead of container ID

### Added
- Release Notes section in documentation

### Fixed
- Eload Net API and multi-channel USB caching

## [0.2.32] - 2025-12-11

### Added
- J-Link debugger auto-installed during deployment
- Flexible deployment with custom usernames

## [0.2.31] - 2025-12-10

### Added
- PicoScope and Rigol oscilloscope support with voltage measurements, cursor modes, autoscale

### Fixed
- Device proxy enum handling and channel parameter bugs

## [0.2.30] - 2025-12-08

### Added
- MCC USB-202 DAQ support for ADC, DAC, GPIO
- Improved `lager update` interface

## [0.2.29] - 2025-12-06

### Fixed
- `lager python download`, `lager exec`, and `lager devenv` commands

## [0.2.28] - 2025-12-05

### Fixed
- `lager exec` command execution

## [0.2.27] - 2025-12-05

### Fixed
- Minor bug fixes and stability improvements

## [0.2.26] - 2025-12-05

### Improved
- Webcam interface with enhanced controls and reduced zoom latency

### Fixed
- `lager exec` and net.py issues

## [0.2.25] - 2025-12-04

### Changed
- Updated `lager devenv` command functionality

## [0.2.24] - 2025-12-04

### Added
- `lager boxes add-all` command for bulk box management

### Fixed
- UART data corruption
- NetType.Analog for Rigol scopes

## [0.2.23] - 2025-12-02

### Fixed
- Multi-channel USB resource sharing for Keysight devices

## [0.2.22] - 2025-12-02

### Added
- Hardware invocation service for Device proxy pattern
- Keysight power supply support in `lager python` scripts

## [0.2.21] - 2025-12-02

### Added
- Phidget thermocouple expanded to 4 channels
- Keysight device support in Python scripts

## [0.2.20] - 2025-11-26

### Added
- Nets working in `lager python` scripts
- UART support in Python
- Oscilloscope web UI with HTTP server and WebSocket

### Fixed
- Python file command, udev rules, LabJack timeout hangs

## [0.2.19] - 2025-11-24

### Added
- `lager binaries` command for managing binary files
- Progress bar and verbose flag for `lager update`

## [0.2.18] - 2025-11-24

### Added
- Automatic security configuration in `lager update`
- Updated Keysight E36300 support

## [0.2.17] - 2025-11-21

### Added
- Concurrent CLI commands while Supply TUI is running

### Fixed
- UART `--line-ending` flag and communication issues

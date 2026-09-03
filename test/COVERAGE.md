# Test Coverage

This document tracks what test coverage exists across all Lager features. It covers the four
test suites: local unit tests, Python API tests, bash integration tests, and MCP tests.

**Counts here are checked against disk**, by `tools/check_coverage_counts.py` in the required
`static-checks` job. It checks three things:

- the gated run-counts below;
- the per-section file counts in every `(-- N files)` header;
- the per-file inventory tables themselves.

Every test file must have a row in its section's table, and a row naming a deleted file fails
the check. If you add or remove a test file, update the counts and the table in the same change.
`--fix` rewrites counts and drops rows for deleted files; a new file's row you write yourself,
with a real description.

## What runs in CI

Two workflows run on `pull_request`: `unit-tests.yml` and `static-checks.yml`. The rest are push-,
schedule-, or dispatch-triggered and need the bench.

| Workflow | Trigger | Runner | Gates a PR |
|---|---|---|:---:|
| `unit-tests.yml` | `pull_request`, push to `main`, dispatch | GitHub-hosted `ubuntu-latest` | **Yes** |
| `static-checks.yml` | `pull_request`, push to `main`, dispatch | GitHub-hosted `ubuntu-latest` | Reports (see below) |
| `rust-checks.yml` | `pull_request`, push to `main`, dispatch -- **path-filtered** to `box/oscilloscope-daemon/**` | GitHub-hosted `ubuntu-latest` | Reports (see below) |
| `integration-tests.yml` | push to `main`, `workflow_call`, dispatch | self-hosted `lager-bench` | No |
| `update-regression.yml` (Bench: Box Lifecycle) | `workflow_call`, dispatch | self-hosted `lager-bench` | No |
| `nightly-bench.yml` | nightly schedule, dispatch | orchestrator | No |

`nightly-bench.yml` is the only workflow with a schedule. It reaches the other two bench
workflows through `workflow_call`, so neither of those carries a `schedule` trigger of its own.

A job only *blocks* a merge once its status context is listed in branch ruleset 14535039.
Sixteen contexts are: the six `unit (...)` jobs, `static-checks`, the four `compat (...)` jobs,
`packaging`, `mcp-extra`, `rust (oscilloscope-daemon)`, and the two `xplat (...)` jobs. The
`coverage` and `pip-audit` contexts are not, and neither is `CodeQL`.

`unit-tests.yml` runs six matrix jobs, each in its own pytest process, on Python 3.11:

| Job (status context) | Path | Tests |
|---|---|---:|
| `unit (cli)` | `test/unit/cli/` + `cli/tests/` | 1863 (+2 xfailed) |
| `unit (box)` | `test/unit/box/` | 2081 |
| `unit (measurement)` | `test/unit/measurement/` | 105 |
| `unit (blufi)` | `test/unit/blufi/` | 89 |
| `unit (mcp)` | `test/mcp/unit/` | 181 |
| `unit (root)` | `test/unit/test_*.py`, `test/test_*.py` | 179 (+1 skipped) |
| | **Total gated** | **4498** |

Each suite gets its own job, because the suites need incompatible `sys.modules` states for the
name `lager`. `test/unit/measurement/conftest.py` registers a placeholder whose `__init__` never
runs, which skips the heavy box deps. `test/unit/box/conftest.py` imports the real package
instead. The two cannot share a process.

`unit-tests.yml` also runs a **`compat (pyX.Y)`** job covering the other versions `cli/setup.py`
advertises. It runs all six suites sequentially, one process per version.

`cli/setup.py` declares `python_requires=">=3.10"` and lists 3.10 through 3.14. Coverage today:

| Version | Status |
|---|---|
| 3.10 | `compat (py3.10)` |
| 3.11 | the six `unit (...)` jobs |
| 3.12 | `compat (py3.12)` |
| 3.13 | `compat (py3.13)` |
| 3.14 | `compat (py3.14)` |

**Every advertised version is now covered.** 3.13 and 3.14 were previously absent because
`box/lager/python/service.py` imported `cgi`, which PEP 594 removed in 3.13. Pytest failed to
collect the box suite there, and the box's python service did not start on 3.13 at all.
That file now parses multipart with `werkzeug.sansio.multipart` and `cgi` no longer appears
anywhere in the tree.

`static-checks.yml` covers what pytest cannot reach:

| Check | Scope | Baseline when added |
|---|---|---|
| `bash -n` | 56 shell scripts under `test/ tools/ box/ cli/deployment/` | clean |
| `shellcheck -S warning`, excluding `SC2034,SC2320,SC2155,SC2164,SC2046` | the 45 under `test/ tools/` only -- `box/` and `cli/deployment/` are syntax-checked but not linted | clean. Pinned to `shellcheck-py==0.11.0.1`, not the runner image's binary. See below for what the exclusions cost. |
| `compileall` | every `.py` in `cli/ box/ test/ tools/` | clean |
| `pytest --collect-only` | `test/mcp/integration/` | 8 tests collect |
| `ruff --select E9,F63,F7,F82` | `cli/ box/ test/ tools/`, vendored excluded | clean (default ruleset would be ~6300) |
| `coverage` | all six unit suites | ~38%, reporting only, no threshold |

### Rust: `rust-checks.yml`

`box/oscilloscope-daemon` is Rust, and until this workflow **no job in this repo referenced
cargo**. That crate is not a side project. `docker/start-services.sh` launches it on box boot
whenever the binary is present. `daemon/src/main.rs` opens QUIC/WebTransport listeners on
8082-8084, which is network-reachable runtime code on customer hardware.

The gap was not theoretical. Dependabot PR #172 bumped 20 crates across 22 breaking-version
boundaries and showed a **green tick from twelve checks**. It then failed to compile in 74
places, because all twelve checks were Python. It broke two ways independently:

- `bindgen` 0.69 -> 0.72 made the generated PicoScope FFI bindings unparseable (70 errors)
- `tungstenite` 0.20 -> 0.30 changed `Message::Text` to take `Utf8Bytes` instead of `String`
  (4 errors, at `daemon/src/websocket/handlers.rs:324,385,390` and
  `daemon/src/webtransport/handlers.rs:297`) -- source edits Dependabot cannot make

| Check | Baseline when added |
|---|---|
| `cargo check --workspace --all-targets --locked` | clean |
| `cargo metadata --locked` (lockfile in sync with manifests) | clean |
| `cargo clippy -A clippy::all -W clippy::correctness -D warnings` | 0 findings (full default ruleset is 22) |
| `cargo audit` | 0 vulnerabilities, 3 allowed warnings |
| `cargo fmt --check` | **48 files differ -- reported to the job summary, not gated** |

`cargo check` rather than `cargo build`: it type-checks the workspace without linking, which is
what catches API breaks without resolving every link-time symbol.

It still needs the **PicoScope SDK** on the runner. `daemon/build.rs` runs bindgen against
`/opt/picoscope/include/libps2000/ps2000.h` unconditionally, and no feature flag skips it.
Without the headers the build script panics, and nothing downstream is checked. The first run of
this workflow failed exactly there (`wrapper.h:2:10: fatal error: 'ps2000.h' file not found`).
The job installs `libps2000` from PicoTech's Debian repo, the same one `build_daemon.sh`
documents for setting up a box, and asserts the header exists before continuing.

That makes the job depend on an external apt host. If `labs.picotech.com` proves flaky, split the
job so the SDK-free crates (`cli`, `protocol`, `wtransport_test`) keep gating while the daemon
check degrades to advisory.

The toolchain is pinned to 1.95.0, for the same reason `shellcheck` is pinned in
`static-checks.yml`. The clippy and audit baselines were measured against a known version. A
move of `stable` will turn this red with no change in the repo.

**`cargo audit` is not redundant with Dependabot.** Dependabot reads the GitHub Advisory
Database; RustSec advisories reach it only once imported. Adding this check surfaced
`RUSTSEC-2026-0204` (invalid pointer dereference in `crossbeam-epoch` 0.9.18) at a moment when
Dependabot reported **zero** open alerts. It is fixed in the same change that added the workflow,
so the step starts clean.

Three advisory *warnings* remain and do not fail the build, because none of them has a fixed
version to move to. They are `rustls-pemfile` unmaintained (`RUSTSEC-2025-0134`, two versions in
the graph) and `anyhow` unsound `Error::downcast_mut` (`RUSTSEC-2026-0190`). Add
`--deny warnings` once they clear.

The `pull_request` trigger is **not path-filtered**. The job detects rust changes itself,
through a merge-base diff of `box/oscilloscope-daemon/` and the workflow file. It succeeds
through a skip when nothing rust-side changed, so it always reports, and it IS a required
context. A Python-only PR pays ~20 seconds of checkout+detect.

The job also runs `cargo test --workspace`. That is trivially green until the workspace gains
its first test, at which point the gate exists with no workflow change. On push to main the job
also runs a release-profile compile smoke, and uploads the binary as an artifact. That is NOT a
ship artifact: `build_daemon.sh` still hand-builds the deployed daemon against the box's own
OS.

### What CI does NOT run

| Area | Size | Why not |
|---|---|---|
| `test/api/` | 82 scripts | Needs real hardware. The bench workflows invoke 10 by name; the other 72 execute nowhere -- though all are now syntax-checked. |
| `test/integration/` | 38 bash scripts | Needs a real box and instruments. **8 execute:** `communication/jlink_script.sh` nightly via `integration-tests.yml`, plus 7 weekly via `bench-extended.yml` -- 5 infrastructure suites (`deployment`, `devenv`, `nets`, `box_config`, `generic`) and 2 power suites (`power/supply.sh`, `power/battery.sh`). The other 30 are syntax-checked and shellchecked but never executed. |
| `test/mcp/integration/` | 1 file | Needs two live boxes. Import-checked only. |
| `test/manual/` | 2 bash scripts | Operator-driven. Syntax-checked only. |
| `cli/tests/test_box_lager_imports.py` | 1 file | Excluded via `cli/tests/conftest.py`: it is a printed report with no `assert` statements, so under pytest its 16 functions pass unconditionally. Still useful run directly. |

Known gaps in the gate itself, in rough priority order:

- **Operating systems.** CI is `ubuntu-latest` only, and six `cli/` modules branch on platform.
  The `termios`/`tty` case is now fixed and guarded. `websocket_client.py` matches the pattern
  that `cli/status.py` already used, and `test/unit/cli/test_import_surface.py` simulates the
  missing module with a `meta_path` finder, so the guard is exercised on Linux. The remaining
  platform branches are still unexercised -- a real fix needs a `windows-latest` job.
- **No type checking, and no dependency scanning for Python.** There is no
  mypy/pyright/bandit/pip-audit config, so nothing in a PR run inspects Python dependencies.
  Dependabot covers the *alerting* half (`.github/dependabot.yml`), but it runs on GitHub's
  schedule rather than in the gate. A PR that introduces a vulnerable Python dependency still
  goes green, and someone catches it afterwards, if at all. **Rust is now covered in-gate** by
  `rust-checks.yml`; the Python equivalent is the remaining half.
- **A merged cargo bump does not patch a deployed box.** No workflow builds
  `box/oscilloscope-daemon`, and `box.Dockerfile` does not copy the binary: it is built by hand
  (`./build_daemon.sh`) and mounted from the host at runtime. Fixing a Rust advisory in the
  lockfile is therefore a source-only fix until someone rebuilds and redistributes the daemon.
- **Ruff is errors-only.** It gates real bugs, not style. That floor was chosen because it was
  already clean, and is meant to ratchet up.
- **Five shellcheck codes are excluded from the gate** -- 90 findings, all in `test/integration/`
  and `test/manual/`, which no workflow executes. Detailed below; the ~13 captured-then-ignored
  values in that set are the part that matters.

### The excluded shellcheck codes

`shellcheck` moved from `-S error` (0 findings) to `-S warning` minus five codes. Of the 181
findings `-S warning` reported, 91 are fixed:

| Code | Fixed | What it was |
|---|---:|---|
| `SC2069` | 76 | `cmd 2>&1 >/dev/null` sent stderr to the terminal instead of discarding it. Swapped to `>/dev/null 2>&1`; exit status was never affected, so no test outcome changes. |
| `SC2034` | 33 | `for i in ...` loops whose body never reads the counter, renamed to `for _`. |

**A raw finding count is not a backlog size.** Shellcheck reports `SC2034` once per variable name
per scope. A file with five unread `i` loops therefore reports one finding, until you fix it and
the next one surfaces. Fixing 33 loop counters moved the reported count only 45 -> 30, over five
passes. The original "181" was an undercount.

The remaining 90 are excluded by name in `static-checks.yml`. They are excluded there rather than
with inline `# shellcheck disable` comments, because a disable directive **cannot be scoped to a
single variable**. It silences that code for the rest of the file, so ninety inline suppressions
will blind 20 files to everything after them.

| Code | Count | Why still open |
|---|---:|---|
| `SC2320` | 39 | `$?` reads `echo`/`printf`'s status, not the command's. Fixing turns silently-passing checks into real ones. |
| `SC2034` | ~17 | Intentional constants only (BMP280/BME280 register maps, `TEST_DELAY`, a color fallback). The ~13 captured-then-ignored values that used to share this bucket are FIXED -- see below. |
| `SC2155` | 16 | `local x=$(cmd)` masks the command's return value. |
| `SC2164` | 4 | `cd` without `\|\| exit`. |
| `SC2046` | 1 | Unquoted command substitution. |

**The captured-then-ignored values are fixed.** Each was a bench test that computed something and
never checked it. Each one now asserts. Where the capture was setup for logic that was never
written, the change deletes it instead:

- `jlink_script.sh` Test 1.3 asserts `SCRIPT_EXISTS`. Previously **both branches of its `if`
  called `track_test "pass"`**, so the test had no way to report a failure.
- `debug.sh` gates Tests 14.3/14.4's lenient no-RTT arms on `RTT_SUPPORTED`. A refused RTT
  connection is a real failure when the firmware probe showed RTT working, and a warning
  otherwise.
- `sensors/thermocouple.sh` 11.1 asserts the `OUTPUT_ORIG` baseline read before drawing any
  case-sensitivity conclusion; `infrastructure/generic.sh` 12.5 uses `DEFAULTS_START` as a
  stale-state guard.
- The `OUTPUT1`/`OUTPUT2` keep-cs captures in both `spi_*_manual.sh` suites are checked for
  non-empty, error-free output (calibration bytes vary by chip, so no exact value is assertable).
- `BACKUP_LAGER_FILE`, `TEST_NET_NAME2`, `BACKUP_FILE` were setup for backup/second-net logic
  that was never written: deleted.

The newly honest checks can legitimately fail on the bench where the old ones cannot, and that
is the point. `SC2034` stays excluded for the intentional constants; retiring it entirely means
per-file disables for those, tracked as a ratchet follow-up.

## Coverage by Domain

Hardware suites only. Several domains marked "No" below do have hardware-free unit coverage in
`test/unit/` -- see the unit inventory.

| Domain | Python API | Bash Integration | MCP |
|--------|:----------:|:----------------:|:---:|
| **Power Supply** | Yes | Yes | Yes |
| **Battery** | Yes | Yes | Yes |
| **Solar** | Yes | Yes | Yes |
| **ELoad** | Yes | Yes | Yes |
| **I2C** | Yes (5 files, 3 backends) | Yes (4 files) | Yes |
| **SPI** | Yes (13 files, 3 backends) | Yes (6 files) | Yes |
| **UART** | Yes | Yes | Yes |
| **BLE** | Yes (4 files) | No | Yes |
| **BluFi** | Yes | No | Yes |
| **WiFi** | Yes | Yes | Yes |
| **USB Hub** | Yes (8 files) | Yes (3 files) | Yes |
| **Debug/J-Link** | Yes | Yes (2 files) | Yes |
| **ADC** | Yes (3 files) | Yes | Partial |
| **DAC** | Yes (3 files) | Yes | Partial |
| **GPIO (GPI/GPO)** | Yes (7 files) | Yes (4 files) | Partial |
| **Scope** | Yes (5 files) | No | Yes |
| **Logic Analyzer** | No | Yes | Yes |
| **Thermocouple** | Yes (3 files) | Yes | Partial |
| **Watt Meter** | Yes (2 files) | No | Partial |
| **Energy/Joulescope** | Yes (2 files) | No | Partial |
| **Robotic Arm** | Yes | Yes | Yes |
| **Webcam** | Yes | No | Yes |
| **Rotation Encoder** | Yes | No | No |
| **Actuate** | Yes | No | No |
| **Boxes (list/add/del)** | No | Yes | Yes |
| **Nets (list/add/del)** | Yes | Yes | Yes |
| **Defaults** | No | No | Yes |
| **Binaries** | Yes | No | Yes |
| **Pip** | No | No | Yes |
| **Python cmd** | No | Yes | Yes |
| **DevEnv** | No | Yes | No |
| **Logs** | No | No | Yes |
| **SSH** | No | No | No |
| **Exec** | No | No | No |
| **Install/Uninstall** | No | No | No |
| **Update** | No | Partial | No |
| **Hello/Status** | No | No | Partial |

## Device Coverage

| Category | Device | Python API | Bash Integration | MCP |
|----------|--------|:----------:|:----------------:|:---:|
| **Power Supply** | Rigol DP821 | 2 files | `supply.sh` | Yes |
| **Power Supply** | Keithley 2281S | 2 files | — | Yes |
| **Power Supply** | Keysight E36xxx | — | `keysight_supply.sh` | — |
| **Power Supply** | Multi-channel (generic) | — | `multichannel_supply.sh` | — |
| **Battery Simulator** | Keithley 2281S | 2 files | `battery.sh` | Yes |
| **Solar Simulator** | EA PSB series | 1 file | `solar.sh` | Yes |
| **Electronic Load** | Rigol DL3021 | 1 file | `eload.sh` | Yes |
| **I2C** | Aardvark I2C/SPI adapter | 2 files | `i2c_aardvark.sh` | Yes |
| **I2C** | LabJack T7 | 2 files | `i2c_labjack.sh` | Yes |
| **I2C** | FTDI FT232H | 1 file | `i2c_ft232h.sh` | Yes |
| **SPI** | Aardvark I2C/SPI adapter | 4 files | 2 files | Yes |
| **SPI** | LabJack T7 | 3 files | 2 files | Yes |
| **SPI** | FTDI FT232H | 3 files | `spi_ft232h.sh` | Yes |
| **GPIO / ADC / DAC** | LabJack T7 | 8 files | `labjack.sh` | Yes |
| **GPIO** | FTDI FT232H | 2 files | `gpio_ft232h.sh` | Yes |
| **GPIO** | Aardvark I2C/SPI adapter | 1 file | 2 files | Yes |
| **Oscilloscope** | Rigol MSO5000 series | 5 files | — | Yes |
| **Logic Analyzer** | Rigol MSO5000 (embedded) | — | `logic.sh` | Yes |
| **USB Hub** | Acroname USBHub3+ | 7 files | `acroname.sh` | Yes |
| **USB Hub** | Yepkit YKUSH | — | `ykush.sh` | — |
| **USB Hub** | Plugable UD-CAM (RTS5411) | 1 file | — | — |
| **Debug Probe** | Segger J-Link | 1 file | `debug.sh`, `jlink_script.sh` | Yes |
| **Energy Analyzer** | Joulescope JS220 | 3 files | — | Yes |
| **Power Profiler** | Nordic PPK2 | 1 file | — | Yes |
| **Watt Meter** | Yoctopuce Watt | 2 files | — | Yes |
| **Thermocouple** | Phidget temperature hub | 3 files | `thermocouple.sh` | Yes |
| **Webcam** | Logitech BRIO / C930e | 1 file | — | Yes |
| **Robotic Arm** | Rotrics Dexarm | 1 file | `arm.sh` | Yes |

The Acroname, YKUSH, and Plugable USB-hub drivers also have hardware-free unit coverage
(`test/unit/box/test_acroname_driver.py`, `test_ykush_driver.py`,
`test_plugable_driver.py`).

## Coverage Gaps

### CLI modules with no gated unit test

Ranked by risk. These are `cli/` modules that no test in the PR gate exercises at all.

| Module | Lines | Why it matters |
|---|---:|---|
| `cli/commands/development/debug/commands.py` | 1475 | The whole `lager debug` group -- flash, erase, reset, RTT state. No tests anywhere. |
| `cli/commands/box/boxes.py` | 773 | `lager boxes` registration, listing, lock/unlock persistence. |
| `cli/commands/measurement/scope.py` + `cli/impl/measurement/scope*.py` | 3221 | Trigger/timebase argument parsing plus a streaming state machine. |
| `cli/commands/measurement/logic.py` | 530 | Logic analyzer; 21 subcommands. |
| `cli/status.py` | 405 | Status/state rendering. Now import-tested (`test_import_surface.py`); the rendering itself is still uncovered. |
| `cli/commands/utility/defaults.py` | 398 | Reads and writes user config defaults -- persistence with no guard. |
| `cli/core/net_storage.py` | 239 | Net definition persistence. |
| `cli/commands/utility/logs.py` | 232 | No tests. |
| `cli/core/ssh_utils.py` | 203 | SSH invocation and argument building. |
| `cli/core/net_group.py` | 200 | Net-scoped click group base class. |
| `cli/context/core.py` | 160 | `LagerContext` construction. |
| `cli/simple_hdlc.py` | 156 | Frame parser / state machine. |
| `cli/update_check.py` | 138 | Background update-check thread. |
| `cli/terminal/**` | ~800 | The whole interactive REPL. |

Sixteen of roughly forty-nine top-level command groups registered in `cli/main.py` have no gated
test. They are `debug`, `defaults`, `webcam`, `scope`, `logic`, `hello`, `boxes`, `box`,
`box-config`, `dut`, `instruments`, `ssh`, `ssh-setup`, `authorize`, `logs`, and `terminal`.
`login`, `logout` and `whoami` are now covered by `test/unit/cli/test_login_commands.py`.

### Defects found by writing the Phase-F tests

Writing coverage for previously-untested modules surfaced five defects. The test-only change
that found them fixes none of them. A test pins each one, so it cannot regress further, and
nobody can "fix" it without the test noticing.

| Where | Defect | How it is pinned |
|---|---|---|
| `cli/core/param_types.py` `CanFrameType.convert` | Tests `'#' in value` **before** `'##' in value`, so a CAN-FD frame always takes the CAN-2.0 branch and dies with `ValueError: too many values to unpack`. The `'##'` branch is unreachable. | `xfail(strict=True)` + a test pinning the exact failure mode |
| `cli/core/param_types.py` `parse_canfd` | Passes `flags=` to the `CanFrame` namedtuple, which has no `flags` field -> `TypeError`, even called directly. So CAN-FD is broken twice over. | same |
| `cli/core/param_types.py` `ADCChannelType.convert` | `'-1'` contains `'-'`, so it takes the range branch and raises a raw `ValueError` from `int('')`. The `value < 0` guard below it is **unreachable dead code**. | test asserts the actual `ValueError`, not the intended `BadParameter` |
| `cli/core/matchers.py` `EndsWithMatcher.feed` | When a chunk ends on `\n`, `split()` leaves a trailing `b''` that is still emitted with its own newline -- so every chunk landing on a line boundary appends a blank line to the user's output. | test pins the exact write sequence |
| `cli/core/matchers.py` `iter_streams` | Line 87 is `elif V1ParseStates.Content:` -- missing `parse_state ==`, so it evaluates an always-truthy enum member. Correct today only because the branches above it are exhaustive; a sixth state would route here silently. | test asserts the source line, and fails once it is fixed |

The three `param_types` defects are all in **export-only** code. `cli/core/__init__.py`
re-exports the CAN types and `ADCChannelType`, but no command uses them, and `cli/main.py`
registers no `canbus` group. They are latent, not user-facing. The two
`matchers` defects are on the live `lager python` output path; the blank-line one is cosmetic and
the `iter_streams` one is currently benign.

Five other param types (`EnvVarType`, `PortForwardType`, `MemoryAddressType`, `HexArrayType`,
`BinfileType`) *are* on live paths -- `lager devenv`, `lager python --env/--port`, `lager debug`
-- and are now covered.

### Undertested -- a test exists, but thin relative to risk

| Module | Lines | Gated coverage | Gap |
|---|---:|---|---|
| `cli/commands/utility/update.py` | 2440 | 14 tests | Only version-ref resolution and the probe. Rollback, staging, service restart untested. |
| `cli/gateway_auth.py` | 376 | 27 tests | Refresh path plus the `cli/tests/` suite. `handle_gateway_denial`, `gateway_response_hook`, `auth_headers_for_box` remain thin. |
| `cli/config.py` | 435 | 63 tests | Cache, the configparser round-trip and legacy-key migration, `read_lager_json`/`write_lager_json`, `expand_devenv_path` and `get_debug_script_for_net` are covered. `get_includes_from_config` and `_find_config_files` are not. |
| `cli/commands/utility/install.py` | 575 | indirect | Only `install_wheel` is exercised. |
| `cli/commands/utility/uninstall.py` | 837 | 13 tests | Spec parsing plus the teardown's lock lifecycle. The privileged sudo session, the `--all` extras and `--dry-run` inspection are untested. |
| `cli/commands/communication/*.py` | — | 1 each | Smoke-only: asserts each posts to `:9000`. `spi.py` (700) and `i2c.py` (551) have no protocol or argument-parsing coverage. |
| `cli/commands/measurement/*.py` | — | 1 each | Same `:9000` smoke pattern. |

### Hardware suite gaps

| Gap | Details |
|-----|---------|
| **Logic Analyzer** | No Python API test. 21 CLI subcommands, bash and MCP coverage only. |
| **SSH / Exec / Install** | No coverage in any hardware suite. |
| **Watt / Energy** | No bash integration tests. |
| **Scope** | No bash integration test. 39 CLI subcommands. |
| **BLE** | No bash integration test. |
| **Box Management** | No Python API tests for boxes, status, or hello. |
| **Rotation / Actuate** | Python API only. |
| **Webcam** | No bash integration test. |

## Coverage Strengths

- **Box-side logic**: 74 files / 1554 tests. They cover the box HTTP handlers, debug and J-Link
  paths, locking, net persistence, and device drivers. All are hardware-free and gated on every PR.
- **Communication protocols**: I2C and SPI have 18+ Python API files across three hardware
  backends (Aardvark, LabJack, FT232H) with full 3-suite coverage.
- **Power management**: Supply, Battery, Solar, and ELoad all have full 3-suite coverage, with
  tolerance checks, boundary tests, and safety teardown. Device-specific suites cover the
  Rigol DP821 and the Keithley 2281S.
- **I/O domain**: 17 Python API tests covering ADC, DAC, GPIO, and PWM with real value assertions.
  `test_LabJack_T7.py` is an 11-group suite with env-var configuration, preflight, DAC boundary
  enforcement, stability analysis, and optional loopback.
- **MCP server**: 166 gated unit tests across 11 files, plus an integration suite requiring
  hardware.
- **Regression discipline**: a large share of the box and CLI unit tests are named for the defect
  they pin (`test_uart_bridge_reconnect.py`, `test_dispatcher_channel_resolution.py`,
  `test_gdbserver_zombie_status.py`, `test_jlink_error_masking.py`). Each one documents the
  failure in its module docstring.

## Test File Inventory

```
test/
├── api/                  # Python API tests (82 files, run on box via `lager python`)
│   ├── communication/    # 30 files: I2C, SPI, UART, BLE, BluFi, WiFi, debug
│   ├── io/               # 17 files: ADC, DAC, GPIO, PWM, pin conflict, USB-202
│   ├── peripherals/      #  9 files: scope, arm, webcam, rotation, actuate
│   ├── power/            #  7 files: supply (3), battery (2), solar, eload
│   ├── sensors/          #  9 files: thermocouple, watt, energy, joulescope, PPK2
│   ├── usb/              #  8 files: USB hub enable/disable/toggle/stress, Acroname
│   └── utility/          #  2 files: binaries, net listing
├── integration/          # Bash integration tests (38 files, run from host via harness.sh)
│   ├── communication/    # 15 files: I2C, SPI, UART, WiFi, debug, J-Link
│   ├── infrastructure/   #  7 files: boxes, nets, deployment, devenv, python, generic
│   ├── power/            #  6 files: supply, battery, solar, eload, keysight, multichannel
│   ├── io/               #  4 files: LabJack, GPIO (Aardvark, FT232H)
│   ├── usb/              #  3 files: USB, Ykush, Acroname
│   ├── measurement/      #  1 file: logic analyzer
│   ├── sensors/          #  1 file: thermocouple
│   └── peripherals/      #  1 file: robotic arm
├── mcp/                  # MCP server tests (pytest)
│   ├── unit/             # 11 files: mocked, no hardware -- GATED
│   └── integration/      #  1 file: live hardware required
├── unit/                 # Local unit tests (135 files) -- ALL GATED
│   ├── box/              # 74 files: box-side Python unit tests
│   ├── cli/              # 50 files: CLI Python unit tests
│   ├── measurement/      #  4 files: Joulescope / PPK2 / watt unit tests
│   ├── blufi/            #  2 files: BluFi protocol unit tests
│   └── test_*.py         #  5 files: root-level unit tests
├── manual/               #  2 bash scripts: operator-driven, not automated
├── assets/               # Fixture data (note: assets/firmware/ holds only a README)
└── framework/            # Test utilities
    ├── harness.sh        # Bash test framework (sourced by all 38 integration scripts)
    ├── colors.sh         # Bash color utilities
    ├── fixtures.py       # Pytest fixtures with auto-cleanup
    └── test_utils.py     # Python test helpers

test/test_*.py            #  2 files: run by the `unit (root)` job
cli/tests/                #  7 files: 6 pytest suites (GATED via `unit (cli)`),
                          #           plus 1 standalone report script
```

### Local Unit Tests (`test/unit/` -- 185 files)

#### Box Unit Tests (`test/unit/box/` -- 100 files)

`conftest.py` in this directory imports the real `lager` package once, before any test module is
imported. It also stubs the two third-party modules that are neither guarded nor installed
(`flask_socketio`, `pygdbmi`). Without it the suite depends on alphabetical collection order.

| File | What it tests |
|------|---------------|
| `test_acroname_driver.py` | Acroname USB hub driver: contention (bounded session hold, cross-process lock), latency (discovery cache, scan-free warm opens, cycle timing log), and exit cleanup (a parked handle is closed at interpreter shutdown, including through a real subprocess exit) |
| `test_authorized_keys_sync.py` | `start_box.sh` authorized_keys marker-block rebuild (revocation, no duplicates, foreign keys preserved) and its single-instance lock |
| `test_battery_model_authoring.py` | Battery model authoring (create/export of 2281S memory slots), against hardware-verified ground truth |
| `test_bench_quiesce.py` | The quiesce registry that makes a starting job wait for the previous one's teardown, and the arithmetic tying its bounds to the reap they must cover |
| `test_battery_model_catalog.py` | Read-only battery model catalog; the 2281S has no `:BATT:MODel:CATalog?` query |
| `test_bench_power_on_blocks_match.py` | The three bench workflows' power-on `run:` blocks are byte-identical, and none of them names the Keithley 2281S unconditionally |
| `test_binaries_store.py` | `lager.binaries.store` plus the `:9000` `/binaries/*` and `/download-file` handlers |
| `test_box_config.py` | box_config v1 schema validation rules and idempotency hash |
| `test_box_config_addverb_idempotency.py` | mount-add/apt-add/udev-add upsert behavior for provisioning re-runs |
| `test_box_config_cli.py` | `lager box-config` CLI: mount prep, readiness polling, rollback on bounce failure |
| `test_box_dut_cli.py` | `lager dut` CLI detached-list regression fix |
| `test_box_http_server_capabilities.py` | /status capabilities block advertises netCommand based on route registration |
| `test_box_level_command_handlers.py` | Box-level `POST /ble\|wifi\|blufi/command` handlers driving the box's own radios |
| `test_breakpoint_pause.py` | `lager.pause()` interactive breakpoint: timeout handling and resume signaling |
| `test_cleanup_watchdog.py` | Cleanup grace as an *idle* budget: a teardown making progress keeps its deadline pushed out, a wedged one is still cut off, and blocking on an instrument counts as progress |
| `test_custom_devices_assign.py` | `lager.devices.assign` and the `/custom-devices/*` handlers behind `lager nets assign` |
| `test_custom_store.py` | Custom-device JSON persistence: USB cable to catalog instrument mapping |
| `test_da1469x_loader.py` | DA1469x ELF symbol reading, loader path resolution, flash/erase/timeout paths |
| `test_debug_connect_ports.py` | `/debug/connect` port overrides are coerced and range-checked at the boundary, because they are used to build the debug backend's command line: a non-integer is refused rather than forwarded. A quoted number still works |
| `test_debug_defmt_rtt.py` | Defmt RTT decoding wrapper threading and piping logic, plus the down-channel `write()` that makes a decoding session bi-directional — including the late write that must not reopen the telnet port it just released |
| `test_debug_status_target_attached.py` | `/debug/status` must report `gdbserver_running` and `target_attached` separately, keep `connected` pinned to its old server-liveness meaning for older clients, and preserve the tri-state -- None (older box, refused probe, timeout) is not False. Also pins the log-scrape/probe split: the cheap path always runs, the wire read is opt-in |
| `test_debug_erase_verdict.py` | `/debug/erase` must not answer 200 for a J-Link session that never attached, and the verdict predicate `_attach_failed` must stay stricter than the flash-retry predicate `_connect_failed` |
| `test_debug_net_da1469x.py` | `DebugNet.flash()` / `.erase()` dispatch DA1469x targets on OpenOCD through the RAM-resident flash_loader rather than `program` / `flash_erase_all`, with absolute XIP addresses translated to flash-relative offsets; non-DA1469x OpenOCD and the J-Link backend stay on their existing paths; loader failures name the step that failed, a flash dying after its erase warns the board may be left blank, and a down daemon still routes through `_self_heal` |
| `test_debug_net_self_heal.py` | DebugNet self-heal retry and session endpoints |
| `test_debug_net_user_scripts.py` | User-script/slot helpers: OpenOCD/J-Link base64 fields and serial in debug_net.py |
| `test_debug_rtt_reconnect.py` | J-Link RTT reader reconnect-aware socket handling across J-Link restart |
| `test_debug_script_root.py` | `debug/jlink.py` carries its own copy of the debug runtime root because it must stay standalone-importable (three tests load it by path so the suite need not import pyvisa); this pins the copy to `probes.RUNTIME_DIR` and fails if an import that would break the standalone load comes back |
| `test_detect_and_configure_rtt.py` | RTT control-block RAM scan doesn't leave core halted in all-stop mode |
| `test_device_lock.py` | Cross-process advisory fcntl lock preventing USB-TMC pyvisa race |
| `test_diagnose_jlink_parse.py` | Box-side J-Link diagnose parsers, pinned with captured JLinkExe text |
| `test_dispatcher_channel_resolution.py` | `resolve_channel`: v0.32.0 regression where int()-only parsing broke named adc/dac channels |
| `test_download_file_headers.py` | `GET /download-file` names the attachment after the path it resolved, with the three characters a header value cannot carry reduced -- a filename may legally contain all of them. Spaces and parentheses survive, so the download keeps a name the user recognises |
| `test_ftdi_driver_addressing.py` | The FTDI GPIO/I2C/SPI drivers addressed by part and channel: existing single-channel FT232H URLs are byte-identical, an FT2232H opens at all (it was advertised but unreachable), I2C/SPI refuse the FT4232H's non-MPSSE C/D while GPIO accepts them, ACBUS pins are refused on a part with no ACBUS, and the GPIO state cache keys on interface so two channels of one chip stop clobbering each other |
| `test_ftdi_url.py` | `lager.util.ftdi_url`: PID to pyftdi product, interface letter/index parsing, and the base-0/base-1 split between OpenOCD's `ftdi channel` and pyftdi's URL — asserted against `probes.parse_device_field` so `@B` cannot come to mean different channels on the two paths |
| `test_firewall_port_allowlist.py` | The deployed `secure_box_firewall.sh` allowlist must match the ports `box/start_box.sh` publishes, parsed from both files rather than duplicated -- including the conditionally-appended `9000` an array-literal read would miss. The two had drifted three times behind a keep-in-sync comment |
| `test_gdb_controller_leak.py` | GdbController close on failed attempts to prevent fd leak |
| `test_gdbserver_zombie_status.py` | Defunct/zombie gdbserver detection that a bare `os.kill(pid, 0)` check passes |
| `test_hardware_service_fail_fast.py` | `/invoke` fail-fast locking and hang recovery: per-device and per-address locks answer `device-busy` rather than queueing behind a wedged `open_resource`, and a hung driver call expires into `invoke-timeout` plus a supervised restart |
| `test_hardware_service_retry.py` | Close-then-recreate retry path for concurrent Keithley resource collisions |
| `test_host_ops.py` | apt_install and sysctl_apply SSH execution branches |
| `test_hub_lock_fail_fast.py` | The same treatment on the USB hub path: bounded waits on the module-level and per-hub locks (`hub-busy`), a per-operation deadline (`hub-op-timeout`), the restart that follows, and the state sweep's per-hub sub-budget (clamp + `hub-skipped`) |
| `test_jlink_commander_use_poll.py` | JLinkExe spawned with use_poll=True to avoid fd >= 1024 select() failure |
| `test_jlink_error_masking.py` | Three debug-path defects that masked on-bench J-Link failures |
| `test_jlink_memrd_reset_halt.py` | DA1469x reset+halt-before-read gating, regression guard, env-var opt-out |
| `test_jlink_multi.py` | Multi-probe start_jlink_gdbserver with per-probe serial/port/RTT configuration |
| `test_jlink_multi_gdbserver_select.py` | Multi-probe GDB slot dispatch |
| `test_jlink_script_attach_retry.py` | A user `.JLinkScript` defining `InitTarget()` replaces J-Link's built-in, which is what brings the DAP up on a blank or protected part; the attach retries once without the script rather than wedging the net |
| `test_jlink_script_scoping.py` | J-Link scripts are per net: an operation with no net gets none, a net never inherits another's script, and a session's script is cleared when it ends |
| `test_jlink_uncached_verify.py` | DA1469x opt-in uncached QSPI post-program verify to detect false XIP failures |
| `test_lager_package_identity.py` | Guards this suite's conftest invariant: `lager` must be the real on-disk package with its `__init__` executed, not a placeholder |
| `test_labjack_batch_read.py` | `POST /labjack/batch_read`: locks on the same device identity `/invoke` does, and writes nothing to the instrument |
| `test_load_box_secrets.py` | `load_box_secrets()` returns `{}` on every failure, which makes an unreadable secrets file indistinguishable from a box with none configured -- pins that distinction |
| `test_lock_state.py` | lock_state.py single source of truth for box-side lock behavior |
| `test_logic_net_type.py` | `lager logic`'s workers must resolve nets under `NetType.from_role(LOGIC_ROLE)`; `Net.get` matches on type equality, so a mismatch is a silent no-op rather than an error |
| `test_rigol_mso5000_la.py` | The MSO5000 driver's logic-analyzer surface: the SCPI each method emits, D0 surviving a falsy-channel check, out-of-range channels and bad pods refused, every accepted spelling of the display size, and the uncrossed `LogicDisplaySize`. Walks net.py's Logic branches and the logic mapper to assert every name they call exists on the driver -- the check whose absence let ten undefined methods ship |
| `test_net_ready.py` | `wait_for_net`: polling a net through the re-enumerate / hardware-service-restart window, deadline honoured without overshoot, probe selection, and a progress callback that cannot break the wait |
| `test_monitor_state.py` | SupplyNet/KeithleyBattery single-call monitor-state helpers reducing lock contention |
| `test_mount_prep.py` | Mount preparation SSH operations via mocked runner |
| `test_net_command_handler.py` | Generic POST /net/command Flask handler dispatch by role and error handling |
| `test_net_save_uart_identity.py` | `usb_identity_for_net_record`: durable USB identity snapshot at UART net save time |
| `test_mapper_range_checks.py` | Tree-wide guard: no `LO > x > HI` range check in `box/` or `cli/`, a shape that is always false so the `raise` under it is unreachable; plus both ends of the seven inverted bounds fixed in the Rigol MSO5000 and Keithley mappers |
| `test_nets_display.py` | `lager nets` table no-truncation for long UART pins and VISA addresses |
| `test_nets_safety_limits_endpoint.py` | `/nets/safety-limits`: reading and writing a net's voltage/current ceilings |
| `test_nets_state_endpoint.py` | `GET /nets/state`: wedged-instrument resilience, per-instrument probing, LabJack cross-role batch routing through hardware_service (no USB contention), I2C bus scan, and the request deadline handed to the USB batch as a per-hub budget |
| `test_openocd_dispatch.py` | OpenOCD interface .cfg dispatch and user-cfg override behavior |
| `test_prebuilt_image.py` | The pre-built-image block in `box/start_box.sh`: a mutable tag refused before any docker call, the OCI version label asserted and an unlabelled image rejected, `--platform` pinned, the pull sent through a throwaway docker config, and every miss falling back to the local build rather than failing the deploy |
| `test_probes_paths.py` | Per-probe pid/log paths are built from a serial read from a permissive field of a client-supplied VISA address: whatever that field carries, the path stays inside `/tmp`, while an ordinary serial keeps the byte-identical filename it had before so an upgrade cannot orphan a running gdbserver; slot assignment still matches on the raw serial, and the unused `/pip` endpoint stays removed |
| `test_probes_visa_parsing.py` | VISA address parsing for empty-serial FTDI probes |
| `test_process_dir_containment.py` | A detached job's registry directory is named after a request-supplied id: the UUID parse each handler runs is pinned as what actually rejects a bad one, and the containment check beside each join pins where the result is allowed to land, so reordering or dropping a parse cannot silently widen it |
| `test_python_kill.py` | `/python/kill` signals every PID in a job, once per process group, and shares one grace window across the whole set; real forked children, not mocks |
| `test_python_timeout.py` | `--timeout` builds an enforceable deadline: `/usr/bin/timeout --kill-after`, the ceiling applied out loud, and a detached job wrapped only when a deadline was asked for and never capped -- the ceiling tracks the CLI's streaming read timeout, which nothing reads on a detached run; real SIGTERM-ignoring children prove the old argv could not stop them and the new one can; plus a guard that the box ceiling fits inside the CLI's HTTP read timeout |
| `test_python_detach_start.py` | A detached launch answers its client before the work that made it slow -- unpacking, pip, the quiesce gate -- and registers the job on disk first, so a reattach racing the launch opens a file that exists; a job that dies before it ever had a pid reports through its own log as stderr plus an exit marker and reaches a terminal status, never stuck at 'starting' |
| `test_python_service_framing.py` | JSON responses are self-delimiting rather than relying on the socket closing, with the length counted in encoded bytes; streaming responses declare close because they cannot carry a length; and the HTTP/1.0 default both rest on is pinned so raising it cannot silently hang every streamed run |
| `test_detached_job_lock.py` | The box holds a detached run's box lock for exactly that job's lifetime: it heartbeats only the holder the CLI handed over, releases without forcing, and stops rather than fights when the lock stops being that holder's |
| `test_python_service_breakpoint.py` | Breakpoint endpoints on box python/service.py POST routes |
| `test_python_service_multipart.py` | `parse_multipart` after the move off `cgi.FieldStorage`: byte-exact binary fields, repeated names, and the `.py`/`.zip` to BytesIO rule |
| `test_python_service_nets_list.py` | GET /nets/list handler returning saved net array or empty on missing/invalid JSON |
| `test_render_docker_args.py` | Sourceable bash output preserves docker-run args through array expansion |
| `test_render_packages.py` | pip/cargo/npm renderers preserve only their own config fields and soft-fail gracefully |
| `test_rtt_handlers.py` | Bi-directional RTT over the `/rtt` WebSocket namespace: read loop, J-Link banner stripping, shutdown cleanup, and the three ways a held RTT port is freed — a departed client (which the loop's own heartbeat cannot detect), a wedged reader, and the port-keyed guard that keeps two channels of one net independent |
| `test_safety_interlock.py` | Per-net voltage and current ceilings enforced on instrument commands |
| `test_script_backend_sniff.py` | `sniff_script_backend` routes a debug-script override by format so `DebugNet.connect(script=...)` works on both backends: extension beats content, every declared extension and marker is asserted individually because a base64 blob has no filename to fall back on, and an ambiguous file abstains rather than picking a side. Also pins the two J-Link forms the marker list misses (`InitTarget(void)`, `JLINK_ExecCommand`) — safe, because abstaining raises rather than guessing, but it is why `jlink_script=` exists |
| `test_secret_file_ownership.py` | The ownership block extracted verbatim from `box/start_box.sh`: mode 0600 grants the OWNER alone, so a secrets file owned by the host login user locks the container runtime out of its own secrets |
| `test_serial_id_cables.py` | tty enumeration and resolution via fake /sys tree lookup |
| `test_store_path_containment.py` | A binary name, a device-lock key and a DFU staging file are each named after something off the wire: the existing reduction is pinned as what rejects, and the containment check beside each join pins where the result lands, so widening a reduction cannot silently widen the directory. Also pins that names with spaces, `+` and parentheses still work, since the CLI forwards the basename of any local file |
| `test_ssh_runner.py` | SSH key selection and auth fallback logic |
| `test_ssh_setup.py` | `lager ssh-setup` command and SSH key provisioning with TTY passthrough |
| `test_stream_disconnect.py` | `peer_is_connected` and the idle tick that let the box notice a vanished client in under a second instead of waiting for the script's next write |
| `test_stream_teardown.py` | `lager python` child reaped when the client disconnects mid-run, instead of orphaning at 100% CPU holding a device flock |
| `test_sudoers_contract.py` | The `/etc/sudoers.d/` ownership contract: Lager writes exactly three files there, never globs and never touches the directory itself, and every writer — including the shell copy in `setup_and_deploy_box.sh` — emits the banner telling an operator those files are regenerated wholesale. Also pins the recorded escalation posture: the box login user is root-equivalent by design, and no source may claim a scoped entry confines it |
| `test_supply_command_handler.py` | `POST /supply/command` handler, covering v0.32.0 hardware-found regressions |
| `test_uart_bridge_reconnect.py` | UARTBridge re-enumeration healing after an adapter changes its /dev/tty node |
| `test_uart_session_cleanup.py` | Websocket UART read loop heals in place instead of stopping on a failed read |
| `test_usb_devices_dfu.py` | `GET /usb/devices` sysfs enumeration and `POST /usb/dfu` list/download/detach argument building |
| `test_usb_scanner_custom.py` | Custom-device surfacing in box HTTP scanner GET /instruments/list. Also the SuperSpeed companion dedupe: one physical dock lists as one instrument, and a missing bus root pairs nothing rather than pairing everything. Also what the Dexarm handshake -- the one scan step that WRITES to hardware -- is allowed to touch: every channel of a multi-interface chip and every saved uart net's tty reach the exclusion set, a foreign or unresolvable VID:PID is never opened at all, a port held by another process is skipped, and `LAGER_ARM_PROBE` off/force widen or close the gate without ever dropping the exclusive open or the deasserted modem lines. |
| `test_usb_scanner_uart_fallback.py` | UART enumeration without USB serial by matching sysfs path; two identical adapters keep distinct ttys and the channel catalog stays unmutated |
| `test_webcam_detection.py` | sysfs-based webcam detection (`_by_camera`) against a fake sysfs tree |
| `test_plugable_driver.py` | Plugable RTS5411 dock driver: USB hub-class per-port power switching over pyusb -- ganged/no-switching hubs refused without touching a port, a disable NOT judged by device presence (the kernel cannot see a disconnect while a port is unpowered, so the sysfs node persists), cycle restoring power on every failure path and reporting re-enumeration, off-time range enforced before any transfer, SuperSpeed companion pairing refused when ambiguous, network-device and inter-hub-link guards, one-session batch reads, handle disposal on every path |
| `test_ykush_driver.py` | YKUSH USB hub driver: device-contention regression from an indefinitely cached handle |
| `test_automation_exports.py` | Static parse of `automation/__init__.py`'s lazy export table: no name guarded twice, every returned driver reachable under its own name, everything in `__all__` resolvable -- the copy-paste class of defect that made one driver answer to another's name |

#### CLI Unit Tests (`test/unit/cli/` -- 68 files)

| File | What it tests |
|------|---------------|
| `test_address_utils.py` | IPv4/IPv6/Tailscale/hostname validation rejecting schemes, ports, and paths |
| `test_battery_tui.py` | BatteryTUI render output, command parsing, and worker thread offloading |
| `test_binaries_9000.py` | `lager binaries add/list/remove` and `download_file` migrated to the box HTTP server on `:9000` |
| `test_box_command_error.py` | `box_command_error`: a 404 that means "net or instrument not found" must not also tell the user their box image is out of date |
| `test_box_lock_helpers.py` | Lock holder resolution, acquire/release/heartbeat, `LockSession.dissolve`, format_lock_user CI support, `lock_scope`/`_lock_held_by_self` identity matching across all four lock-path comparisons (check, pre-acquire probe, `previous_user`, and the conflict branch that decides whether to wait), and the `_check_box_lock` refusal path |
| `test_box_request_failure_messages.py` | `echo_box_request_failure`: distinguishing a slow box-side op from a dead box |
| `test_box_ssh_identity.py` | Admin commands offer the `lager_box` key with keyless fallback (probe, pool, install/uninstall); key registration under `/etc/lager/authorized_keys.d`, de-registration on `uninstall --all`, and install's password-fallback removal. Also that `-i` does not cost the operator ssh's own defaults: `lager ssh` names `lager_box` first and then each default identity file present, in ssh's order, and passes no `-i` at all when no `lager_box` key exists |
| `test_configure_docker_dns.py` | `configure_docker_dns`: daemon.json `dns` entries must be bare IPs or Docker refuses to start |
| `test_configure_docker_dns_rollback.py` | Rollback behavior of `configure_docker_dns.sh` when the DNS optimization fails |
| `test_deploy_box_image_ref.py` | `setup_and_deploy_box.sh` and `_box_image_ref_for_version` agree on which versions have a published image, computed in one conditional so the two cannot drift; plus the anonymous GHCR digest resolution and the `LAGER_BOX_IMAGE` handoff to `start_box.sh` |
| `test_deployed_ref.py` | `/etc/lager/ref` records which ref produced the box's code (`<ref>@<sha>`), so a branch deploy is distinguishable from the release tag it shares a version number with; the release-tag predicate is pinned against `resolve_version_ref` so the two cannot drift, and a box reporting no ref renders exactly as before |
| `test_debug_auto_connect_gate.py` | `_auto_connect_if_needed` gates on the target answering, not on a live gdbserver: a confirmed attachment skips the connect, an absent target forces a reconnect rather than proceeding, and an inconclusive answer falls back to server liveness so a working session is never torn down. Covers `_is_connected` and `_target_attached`, which had no direct tests |
| `test_debug_flash_erase_reconnect.py` | `lager debug flash`'s default erase step: no reconnect between `/debug/erase` and `/debug/flash`, a failing `/debug/connect` cannot abort the flash, and the verdict of both `flash` and `erase` follows the programmer's own output rather than reporting "Flashed!" / "Erase complete!" unconditionally |
| `test_debug_service_client_auth.py` | Gateway auth on the debug service client |
| `test_devenv_config_commands.py` | `lager devenv mount` / `env`: editing project-local `.lager` volumes and environment keys |
| `test_devenv_terminal_docker_args.py` | `docker run` args for `devenv terminal` and `exec`; regression for the `--group` bare-flag bug |
| `test_docker_install_diagnosis.py` | The Docker install step names the command that failed and its exit status, instead of one generic error for an eight-command `&&` chain -- the chain is rebuilt the way bash builds it and EXECUTED under `bash` and `sh`, so the `\$`/`\"` escaping is covered rather than just matched as text; `ssh_t`'s stderr filter is synchronous, so the real error cannot land after the caller's generic line (the async form lost the ordering in 26 of 200 runs); its one run-scoped capture file survives a Ctrl-C and reports a TMPDIR it cannot write; and the printed recovery instructions match the chain they replace, including `systemctl enable` |
| `test_docker_start_limit.py` | The installer must not trip docker.service's `StartLimitBurst=3`: one service start per step, `reset-failed` before every restart, and `start-limit-hit` diagnosed as itself rather than a bad daemon.json |
| `test_diagnose_classify.py` | `lager diagnose` classification decision tree for one-line user diagnosis |
| `test_diagnose_classify_jlink.py` | `lager diagnose` J-Link classification from `/diagnose/usb` + `/diagnose/jlink` payloads |
| `test_diagnose_classify_usbhub.py` | `lager diagnose` USB hub classification from `/diagnose/usbhub`, including the wedged hub that sysfs and lsof both call healthy Also that an unsupported hub vendor gets its own permanent-state classification instead of the transient BUSY one, which told people to rerun when idle for a condition that never changes. |
| `test_error_mapping.py` | map_system_error errno mapping [16/19/110] to actionable headlines and actions |
| `test_gateway_auth_refresh.py` | Gateway-auth refresh margin scaling with token lifetime -- pins the refresh-storm fix |
| `test_gdbserver_interactive_rtt.py` | `gdbserver --rtt --interactive`: the flag is rejected without `--rtt`, the streaming leg moves to the `/rtt` WebSocket, and plain `--rtt` still uses the HTTP stream |
| `test_net_9000_migration.py` | Tier-1 net CLI commands (adc, dac, gpi, gpo, spi, i2c, watt, energy, ...) driving the box `:9000` API |
| `test_net_tui_assign.py` | Custom-device assignment TUI helpers; per-device uart tty preference over the shared channel map |
| `test_net_tui_labjack_pins.py` | TUI LabJack pin dialog (prefill/revert/legacy-channel preservation) + combined name+pin editor behind the Add-row pencil, dismissable notices |
| `test_net_tui_uart_guard.py` | UART net save validation rejecting bare interface indices and empty pins |
| `test_nets_add_labjack_pins.py` | LabJack I2C/SPI arbitrary pin selection via --sda/--scl/--cs/--sck/--mosi/--miso |
| `test_nets_add_roles.py` | Role-token normalization converting legacy supply/batt to power-supply/battery |
| `test_nets_assign.py` | `lager nets assign` flow with custom-device backend and net creation |
| `test_nets_channel_display.py` | `lager nets` Channel column rule for uart nets carrying a durable `live_path` |
| `test_nets_debug_scripts.py` | Smart `lager nets set-script` auto-detection and probe/file reconciliation |
| `test_nets_multi_device.py` | Two devices of one model: address ambiguity, not model name, decides whether nets can be created |
| `test_nets_state_display.py` | `lager nets state` State column rendering, including the dash shown for a net whose state is unknown |
| `test_nets_tui_startup.py` | Nets TUI startup regressions: mixed net types, empty state, unsaved placeholders |
| `test_performance_improvements.py` | Config caching, connection pooling |
| `test_python_auto_lock.py` | `lager python` auto-lock wrapper idempotency, atexit, and heartbeat thread |
| `test_python_breakpoint_session.py` | Breakpoint client request shapes for continue_python/breakpoint_status endpoints |
| `test_python_stop_signals.py` | `lager python` stop handlers cover SIGINT, SIGTERM and SIGHUP on both registration paths, and restore every one; asserts real signal dispositions rather than recorded calls |
| `test_python_detach_lock.py` | The CLI hands a detached run's lock to the box only when it freshly acquired that lock -- never a resumed reservation -- and arms the lapse TTL only once the box confirms it is heartbeating, so an older box that ignores the handoff keeps today's eternal hold instead of letting the lock lapse under a running job |
| `test_python_exit_codes.py` | `normalize_exit_code` maps a signal death (`-9`) onto the 128+N convention `SIGKILL_EXIT_CODE` is written in, so a timeout kill reports 137 rather than 247, and never returns a negative code to `sys.exit` |
| `test_resolve_box_locked.py` | `resolve_box_locked`: acquires an ephemeral lock on resolution, stashes the release on the context, passes through under `LAGER_AUTO_LOCK_DISABLE`, and reports `already_ours` for a lock we already hold. Pins the holder via `get_lock_holder` and forbids real HTTP, so the result cannot depend on whether it runs on a laptop or a CI runner |
| `test_empty_box_name.py` | An explicit `--box ""` (or whitespace-only) is refused rather than silently resolving to the DEFAULT box, in BOTH `resolve_and_validate_box` and `resolve_and_validate_box_with_name` -- they duplicate the resolution logic, so a guard in one would leave the other's callers still defaulting. Also pins the half that must not change: `None` still means "not given" and falls back to the default |
| `test_ssh.py` | SSH ensure_lager_box_keypair and key_auth_works helpers |
| `test_supply_tui.py` | SupplyTUI render output, command parsing, worker threads, connection failure |
| `test_uart_ws_status_events.py` | CLI handling of box-side `uart_status` events when a UART device re-enumerates |
| `test_update_deps_preview.py` | `lager update --check`'s build-cache line never promises a cached build the rebuild gate would override — a pending layout flatten is a certain rebuild, and an unmeasurable build hash is reported as unknown rather than as a valid cache |
| `test_update_flatten.py` | `lager update` sparse-checkout flatten: deletions propagate, root entries preserved, and the docker-build hash covers the source tree |
| `test_update_probe.py` | `lager update` probe script modprobe/usbtmc detection and output parsing |
| `test_control_flow_exits.py` | `ctx.exit()` survives the broad handler of its own try block: `lager update --check` exits 2 (not 1) with no traceback, plus the `tools/check_control_flow_handlers.py` gate and its own detection cases |
| `test_update_secret_ownership.py` | `lager update`'s secret-file ownership repair, run as real shell against a throwaway directory with a recording `sudo` stub |
| `test_usb_command_errors.py` | `lager usb <net> <command>` error wiring: a 404 for a missing device must not be reported as an out-of-date box image |
| `test_usb_cycle_command.py` | `lager usb <net> cycle|recover` wiring: off-time reaches the box, no client-side default that could drift from the box's, and the client budget outlasts the longest legal cycle |
| `test_version_skew.py` | Version skew warning when CLI minor > box minor with per-process caching |
| `test_watt_subcommands.py` | `lager watt` NetGroup reading power/current/voltage/all over the box API |
| `test_ws_diagnose.py` | WebSocket failure message generation pointing to instrument vs. box based on health |
| `test_box_lock_command.py` | `lager boxes lock`/`unlock` command layer: the no-expiry reservation body (`holder_type`/`ttl_seconds`), exit codes on 409/403, `--force`, and the Docker-root warning |
| `test_config_roundtrip.py` | `cli/config.py` JSON<->ConfigParser round-trip, legacy-key migration, `read`/`write_lager_json`, `expand_devenv_path`, `get_debug_script_for_net` |
| `test_impl_host_importable.py` | Every `cli/impl/*` module must import with `box/` off `sys.path` and `lager` blocked -- they ship in the wheel but the box tree does not, so a module-level `import lager` breaks them on any pip install |
| `test_import_surface.py` | Import guards: `cli/status.py` needs pymongo's `bson.decode`, and `termios`/`tty` must stay optional (simulated via a `meta_path` finder) |
| `test_impl_script_dispatch.py` | Every `run_backend`/`get_impl_path` call site names an impl script that exists on disk, against a two-sided `KNOWN_MISSING` baseline (now empty, #261); plus `get_impl_path` subdir/root resolution, its raise-on-missing behavior, and that the formerly-dead `lager logic` subcommands reach the backend |
| `test_instrument_role_tables.py` | The three instrument role tables -- the box's `SUPPORTED_USB` and `CHANNEL_MAPS`, the CLI's `INSTRUMENT_NET_MAP` -- agree for every instrument; a channel advertised for a role the CLI will not create a net for is a silently unusable capability |
| `test_logic_dispatch_actions.py` | Every action `lager logic` sends is one the impl script it targets actually handles -- the contract that broke in #261, which a file-existence check cannot see (the pulse-width pair named an existing file with an action it does not register) |
| `test_login_commands.py` | `lager login`/`logout`/`whoami`: display-name fallback, MFA prompt wiring, logout URL rstrip, and the four `whoami` session states |
| `test_matchers.py` | Test-output matchers and the v1 stream framing parser; markers split across chunk boundaries must still set the exit code |
| `test_param_types.py` | Every custom click ParamType, valid and invalid, incl. the five on live command paths |
| `test_safe_unpickle.py` | Deserialization allowlist: refused globals must not be imported as a side effect of refusing them |

#### Measurement Unit Tests (`test/unit/measurement/` -- 4 files)

| File | What it tests |
|------|---------------|
| `test_joulescope_cache.py` | JS220 close-vs-instance-cache coherence; the warm `/net/command` path bug |
| `test_joulescope_serial.py` | JS220 location parsing and serial-number device matching on the warm path |
| `test_ppk2_unit.py` | PPK2 pure logic: location parsing, dispatcher routing, singleton caching, read math |
| `test_watt_reads.py` | Watt-meter current/voltage/all reads, the shared SI formatter, and the averaging window |

#### BluFi Unit Tests (`test/unit/blufi/` -- 2 files)

| File | What it tests |
|------|---------------|
| `test_blufi_unit.py` | BluFi protocol parsing (696-line pytest suite) |
| `test_blufi_scan.py` | `BlufiClient.scan()` BLE advertisement presence checks |

#### Root Unit Tests (`test/unit/test_*.py` -- 11 files)

| File | What it tests |
|------|---------------|
| `test_bench_cleanup_timeouts.py` | Tree-wide guard: every `if: always()` step on a `self-hosted` bench job carries `timeout-minutes` and `continue-on-error`, and the bench jobs are still serialized on one non-cancelling concurrency group |
| `test_bench_schedule_check.py` | `tools/bench_schedule_check.py`: the nightly cadence signals kept distinct -- a missed night (gap), a dead cron (stale), and a schedule drifting later (lateness vs the cron parsed from `nightly-bench.yml`), which spacing alone cannot see |
| `test_bench_watchdog_env.py` | Pins `bench-watchdog.yml`'s `env:` block against the names `tools/bench_schedule_check.py` reads: the workflow must set no threshold at all (a second copy is how the gap threshold came to override 36 with 26 and alarm on a late night), and every threshold the tool reads must carry a default |
| `test_coverage_checker.py` | `tools/check_coverage_counts.py`: platform-gated rows are not drift (and `--fix` must not rewrite them), the anchored summary parse `FORCE_COLOR` defeated, and a missing `pytest-timeout` reported as the missing plugin rather than as a failing suite |
| `test_group_usage.py` | Usage-line formatting for CLI command groups (CommandFirstUsageMixin / LagerGroup) |
| `test_install_wheel.py` | install-wheel command: wheel filename to package name parsing |
| `test_no_global_os_path_patches.py` | Tree-wide guard: no test may patch `os.path` (process-global; on Python >= 3.14 it also rewrites every `pathlib.Path.exists()`) — patch the module's seam or use a real temp path |
| `test_pdf_pages.py` | `tools/pdf_pages.py`: PNG and text extraction (skips without pymupdf, which is AGPL) |
| `test_supply_settle.py` | The DP821 suite's `_wait_for_regulation`: replays the captured 0.17 A enable transient to pin that agreeing reads inside a plateau are not a settle, that a genuine steady load settles at once and is left for the caller to judge, and that a wired channel names its fixture on failure |
| `test_uninstall_spec.py` | Pins `lager uninstall`'s removal spec to what `install` / `box-config apply` actually create; lock dissolves when the teardown removes the lock server |
| `test_update_version_ref.py` | Version reference resolution for git checkouts (semver tags vs. named branches) |

#### Repo-Root Unit Tests (`test/test_*.py` -- 2 files)

| File | What it tests |
|------|---------------|
| `test_errors.py` | `cli/errors.py` taxonomy, plus main/box_storage/config error paths |
| `test_format_lock_user.py` | `box_storage.format_lock_user` rendering of lock holder identities |

#### In-Package CLI Tests (`cli/tests/` -- 7 files)

Gated as part of the `unit (cli)` job.

| File | What it tests | Gated |
|------|---------------|:---:|
| `test_box_storage.py` | `box_storage.py` project-level `.lager` merging behavior | Yes |
| `test_gateway_auth.py` | `gateway_auth.py` bearer-token auth for boxes behind an authenticating gateway | Yes |
| `test_update_gate.py` | Update rebuild gate: probe parsing, build-hash mismatch, early-exit verdict | Yes |
| `test_gateway_callsites.py` | Gateway-auth discovery across every box-talking call site: record the mapping on a discovery 401, retry once with a held token, surface genuine denials, and keep rendering the other boxes' rows | Yes |
| `test_host_cli.py` | Host-OS CLI install helpers shared by `lager install` and `lager update`: the reconcile decision table, `--check` labels, exit codes, the probe snippet under a real shell, and the drift guard pinning the deploy scripts' mirror | Yes |
| `test_io_imports.py` | The `lager.io.*` import surface and re-export identity; asserts the removed root-level aliases stay removed | Yes |
| `test_box_lager_imports.py` | Import-verification report across the box package. **No assert statements** -- excluded by `cli/tests/conftest.py`; run it directly | No |

### MCP Tests (`test/mcp/`)

#### Unit Tests (`test/mcp/unit/` -- 11 files)

| File | What it tests |
|------|---------------|
| `test_tool_registration.py` | MCP server registers exactly the expected discovery/planning tools |
| `test_api_reference.py` | `api_reference` driver introspection: the class named in `_DRIVER_CLASSES` must be the user-facing one an agent's `lager python` script actually calls |
| `test_bench_loader.py` | Bench loader: raw net descriptors to typed network objects |
| `test_box_tools.py` | box_manage MCP tool: health checks and reload operations |
| `test_capability_graph.py` | Capability graph builder from bench resources |
| `test_control_tools.py` | `lager.mcp.tools.control`: scoped box-control tools gated by `LAGER_MCP_ALLOW_CONTROL` |
| `test_dut_context.py` | DUT context: schema types, net metadata, DUT slot parsing, context-aware tools |
| `test_exec_tools.py` | `lager.mcp.tools.exec`: box_exec/read_file/write_file/list_dir, gated by `LAGER_MCP_ALLOW_EXEC` |
| `test_heuristic_engine.py` | Heuristic engine: requirement inference and suitability assessment |
| `test_schemas.py` | MCP schema model validation (BenchDefinition, NetDescriptor, CapabilityGraph) |
| `test_server_state_reload.py` | Auto-reload of bench state when bench.json or saved_nets.json change |

#### Integration Tests (`test/mcp/integration/` -- 1 file)

| File | What it tests |
|------|---------------|
| `test_agent_loop.py` | End-to-end agent workflow: discovery, suitability, `lager python` execution, verify |

### Python API Tests (`test/api/` -- 82 files)

These are **standalone scripts, not pytest** (see `test/CONVENTIONS.md`): each defines `main()`
and runs on a box via `lager python`. None of them run in the PR gate.

#### Power (7 files)

| File | What it tests |
|------|---------------|
| `test_supply_comprehensive.py` | Voltage/current set, readback, enable/disable, OVP/OCP, limits |
| `test_supply_Rigol_DP821.py` | Live measurements, output mode, voltage sweep, stability, OVP/OCP state, rapid cycling |
| `test_supply_Keithley_2281S.py` | Setpoint vs. measured accuracy, power consistency, protection limits, monitor state |
| `test_battery_Keithley_2281S.py` | Battery simulator: mode entry, SOC/VOC/capacity/ESR, terminal voltage, protection lifecycle |
| `test_battery_comprehensive.py` | SOC, VOC, capacity, mode, enable/disable, OVP/OCP, clear |
| `test_eload_comprehensive.py` | CC, CV, CR, CP modes, enable/disable, state verification |
| `test_solar_comprehensive.py` | Set, stop, irradiance, resistance, temperature, VOC, MPP |

#### Communication (30 files)

- I2C, across three backends: `test_i2c_aardvark.py`, `test_i2c_aardvark_api.py`,
  `test_i2c_labjack.py`, `test_i2c_labjack_api.py`, `test_i2c_ft232h.py`.
- SPI, across three backends and both CS modes: 13 files, including
  `test_spi_dead_zone_clamp.py` and `test_spi_write_readback.py`.
- UART: `test_uart_comprehensive.py`.
- BLE: 4 files.
- BluFi: `test_blufi_comprehensive.py`.
- WiFi: `test_wifi_comprehensive.py`, `test_wifi_new_methods.py`.
- J-Link: `test_debug_comprehensive.py`.
- GPI level wait: `test_wait_for_level.py`, 15 sub-tests.

#### I/O (17 files)

- ADC: `test_adc_multiple.py`, `test_adc_continuous.py`.
- DAC: `test_dac_output.py`, `test_dac_ramp.py`, `test_dac_adc_loopback.py`.
- GPIO: `test_gpio_output.py`, `test_gpio_input.py`, `test_gpio_multiple.py`,
  `test_gpio_pulse.py`, `test_gpio_ft232h.py`, `test_gpio_ft232h_api.py`,
  `test_gpio_aardvark_api.py`.
- Also `test_io_comprehensive.py`, `test_pin_conflict.py`, `test_pwm_measurement.py`,
  `test_LabJack_T7.py` (11-group suite), and `test_usb202.py` (MCC USB-202 DAQ).

#### Sensors (9 files)

Thermocouple (single, multiple, monitor), watt profile, multi-sensor lifecycle, energy analysis
and statistics, `test_joulescope.py` (254 assertions), and `test_ppk2.py`.

#### Peripherals (9 files)

Scope (basic, measurements, multichannel, scales, trigger), robotic arm, webcam, rotation encoder,
linear actuator.

#### USB (8 files)

`test_Acroname.py` plus enable/disable, multi-port, net API, power-cycle timing, stress, and
toggle tests.

#### Utility (2 files)

`test_custom_binaries.py` (binary listing, not-found handling) and `test_list_nets.py`.

### Bash Integration Tests (`test/integration/` -- 38 files)

Run from the host against a real box; nothing in CI validates them, not even syntax.

| Directory | Files | Contents |
|---|---:|---|
| `communication/` | 15 | `i2c*.sh` (4 backends), `spi*.sh` (6), `uart.sh`, `wifi.sh`, `debug.sh`, `jlink_script.sh` |
| `infrastructure/` | 7 | `generic.sh`, `boxes_config.sh`, `nets.sh`, `deployment.sh`, `devenv.sh`, `python.sh` |
| `power/` | 6 | `supply.sh`, `battery.sh`, `solar.sh`, `eload.sh`, `keysight_supply.sh`, `multichannel_supply.sh` |
| `io/` | 4 | `labjack.sh`, `gpio_aardvark.sh`, `gpio_aardvark_loopback.sh`, `gpio_ft232h.sh` |
| `usb/` | 3 | `usb.sh`, `ykush.sh`, `acroname.sh` |
| `measurement/` | 1 | `logic.sh` |
| `sensors/` | 1 | `thermocouple.sh` |
| `peripherals/` | 1 | `arm.sh` |

### Test Framework (`test/framework/`)

| File | What it provides |
|------|------------------|
| `harness.sh` | Bash test framework: `init_harness`, `track_test`, `print_summary` |
| `colors.sh` | Terminal color utilities for test output |
| `fixtures.py` | Reusable pytest fixtures with hardware auto-cleanup |
| `test_utils.py` | Python helpers: cache, connectivity, formatting |

## How to Run

### Unit tests (no hardware)

Run each suite as its own pytest invocation, exactly as CI does. `PYTHONPATH` must include the
repo root and `box/`. `--import-mode=importlib` keeps same-named modules in different suites from
a collision, and `-c /dev/null` stops `test/mcp` from shadowing the `mcp` PyPI package.

```bash
export PYTHONPATH="$PWD:$PWD/box"
PYTEST="pytest -v --import-mode=importlib -c /dev/null --timeout=60"

$PYTEST test/unit/cli/ cli/tests/
$PYTEST test/unit/box/
$PYTEST test/unit/measurement/
$PYTEST test/unit/blufi/
$PYTEST test/mcp/unit/
$PYTEST test/unit/test_*.py test/test_*.py
```

Do **not** run `pytest test/unit/` as a single command: `test/unit/box/` and
`test/unit/measurement/` install incompatible `lager` packages into `sys.modules` and cannot share
a process. Running them together fails with a message from `test/unit/box/conftest.py` saying so.

Dependencies: `pip install -e cli/` plus `pip install -r test/requirements-unit.txt`.

That file's ten entries carry major-version caps so an upstream major cannot turn a required
context red with no change in this repo. The **floors stay deliberately low**, because the same
file feeds the compat matrix and pip resolves differently per interpreter. `numpy` lands on
2.2.6 for 3.10, but on 2.4.6 for 3.11 and 2.5.1 for 3.12. A floor pinned to whatever 3.11
resolved will break `compat (py3.10)`. Verify any floor change against the oldest version in the
matrix:

```bash
pip install --dry-run --ignore-installed --only-binary=:all: \
    --python-version 3.10 --target /tmp/x -r test/requirements-unit.txt
```

### Hardware suites

```bash
# Python API tests (on real hardware, via the box)
lager python test/api/power/test_supply_comprehensive.py --box <YOUR-BOX>

# Bash integration tests (from host)
./test/integration/power/supply.sh <BOX> <NET>

# MCP integration tests (requires two boxes)
pytest test/mcp/integration/ -v --import-mode=importlib -c /dev/null \
    --box1 <YOUR-BOX> --box3 <YOUR-BOX>
```

<!-- Copyright 2024-2026 Lager Data -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

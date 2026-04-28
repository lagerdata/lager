# Changelog

All notable changes to the Lager platform are documented here. For detailed release notes, see [docs.lagerdata.com](https://docs.lagerdata.com).

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
- Every MCP tool call is now wired through an `@audited` decorator that records the call via `audit.log_tool_call`, so control planes (Stout) can rely on a consistent audit trail.
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

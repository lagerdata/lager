# Changelog

All notable changes to the Lager platform are documented here. For detailed release notes, see [docs.lagerdata.com](https://docs.lagerdata.com).

## [0.12.0] - 2026-03-20

### Added
- **Command-in-progress lock** — When a `lager` command is running on a box, other users are blocked with a "Command in progress" error. Same user is allowed through. Locks auto-expire after 30 minutes to handle crashed CLI processes
- **User lock (`lager boxes lock/unlock`)** — Explicitly lock a box so only you can run commands on it. Other users see a lock error until you unlock
- `--force-command` global flag to bypass command-in-progress locks
- `lager boxes` list now shows a "busy" column when any box has a command in progress
- `lager python --kill`, `--kill-all`, and `--reattach` skip lock checks (management operations)
- `lager python --detach` releases the command lock immediately after detaching

### Changed
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

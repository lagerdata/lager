# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Lager is a hardware test automation platform with a client-server architecture. The **CLI** (Python/Click) runs on developer machines and sends commands. The **Box** (Python + Rust) runs on dedicated Linux hardware inside Docker, controlling test equipment. The **Factory** is a Flask web dashboard for interactive test execution. Communication between CLI and box happens over Tailscale VPN or direct IP.

**Current version:** 0.4.0 (defined in `cli/__init__.py`)
**Python:** 3.10+ (supports 3.10–3.14)
**Rust:** 1.85+ (edition 2021) for the oscilloscope daemon

## Repository Layout

```
cli/                    # Python Click CLI (pip install lager-cli)
├── main.py             # Entry point — registers all commands via cli.add_command()
├── config.py           # Config management (~/.lager JSON file)
├── box_storage.py      # Saved box management (BOXES section of config)
├── context/            # LagerContext, session types, error handlers
├── core/               # Shared utilities (net_helpers, param_types, matchers, ssh_utils)
├── commands/           # Click command modules grouped by domain
├── impl/               # Backend scripts uploaded and executed on box
├── mcp/                # MCP server (FastMCP) for AI tool integration
├── terminal/           # Interactive terminal REPL
├── deployment/         # Box deployment and firewall scripts
└── setup.py            # Package config, entry points, dependencies

box/
├── lager/              # Python libraries running on box hardware
│   ├── __init__.py     # Public API: Net, NetType, Interface, Transport, OutputEncoders
│   ├── core.py         # Enums, output encoding, exception hook, firmware file classes
│   ├── constants.py    # Config paths (/etc/lager/*), ports, timeouts
│   ├── exceptions.py   # LagerBackendError hierarchy (per-domain subclasses)
│   ├── cache.py        # Thread-safe NetsCache singleton (mtime-based reload)
│   ├── box_http_server.py  # Flask+SocketIO server (port 5000)
│   ├── dispatchers/    # BaseDispatcher ABC + helpers
│   ├── nets/           # Net class, Device HTTP proxy, mappers
│   ├── power/          # supply/, battery/, solar/, eload/ (each: dispatcher + drivers)
│   ├── io/             # adc/, dac/, gpio/ (LabJack T7, USB-202, FT232H, Aardvark)
│   ├── measurement/    # scope/, thermocouple/, watt/, energy_analyzer/
│   ├── protocols/      # uart/, i2c/, spi/, ble/, wifi/
│   ├── automation/     # usb_hub/, arm/, webcam/
│   ├── http_handlers/  # Flask route handlers (supply, battery, uart, dashboard)
│   └── instrument_wrappers/  # Low-level VISA/device definitions
├── oscilloscope-daemon/  # Rust async streaming daemon (4-crate workspace)
└── start_box.sh        # Docker container orchestration

factory/
├── webapp/             # Flask app (port 5001)
│   ├── run.py          # Entry point
│   ├── Dockerfile      # python:3.12-slim container
│   └── boxes.json      # Box connection config
├── docker-compose.yml  # Container orchestration (host network mode)
└── deploy_factory.sh   # Deployment script

test/                   # See "Testing" section below
docs/                   # Mintlify documentation (npm run dev / npm run build)
```

## Build & Development Commands

```bash
# CLI development install (editable)
cd cli && pip install -e .
# With MCP support
cd cli && pip install -e ".[mcp]"

# Verify
lager --version
lager --help

# Oscilloscope daemon (Rust)
cd box/oscilloscope-daemon && cargo build --release

# Factory (local development)
cd factory/webapp && pip install -r requirements.txt && FLASK_DEBUG=1 python run.py

# Factory (Docker on box)
cd factory && docker compose up -d --build

# Documentation dev server
cd docs && npm install && npm run dev
```

## Testing

There are five test suites with different requirements:

```bash
# 1. CLI unit tests (no hardware needed)
pytest test/unit/ -v

# 2. MCP unit tests (no hardware needed)
#    MUST use --import-mode=importlib -c /dev/null to prevent test/mcp
#    from shadowing the mcp PyPI package
python -m pytest test/mcp/unit/ -v --import-mode=importlib -c /dev/null

# 3. MCP integration tests (requires two hardware boxes)
python -m pytest test/mcp/integration/ -v --import-mode=importlib -c /dev/null \
    --box1 <BOX> --box3 <BOX>

# 4. Factory/dashboard tests (no hardware needed)
python -m pytest test/factory/ -v

# 5. Bash integration tests (requires hardware box + net)
./test/integration/power/supply.sh <BOX_IP_OR_NAME> <NET_NAME>

# 6. Python API tests (requires hardware — runs ON the box via lager python)
lager python test/api/power/test_supply_comprehensive.py --box <BOX_NAME>
```

### Test Organization

```
test/
├── api/            # Python API tests (standalone scripts, NOT pytest)
├── integration/    # Bash CLI integration tests
├── unit/           # CLI unit tests (pytest, no hardware)
├── mcp/
│   ├── unit/       # MCP tool tests (pytest, mocked subprocess)
│   └── integration/  # MCP tests against real hardware
├── factory/        # Factory webapp tests (pytest)
├── framework/      # Shared infrastructure
│   ├── harness.sh  # Bash test framework (init_harness, track_test, print_summary, etc.)
│   ├── colors.sh   # ANSI color codes for test output
│   ├── test_utils.py  # Python helpers (connectivity checks, assertions, timing)
│   └── fixtures.py # Pytest fixtures (auto-disable hardware on teardown)
└── assets/         # Test firmware files
```

### MCP Test Markers and Options

The MCP conftest provides pytest markers (`@pytest.mark.power`, `@pytest.mark.battery`, etc.) and CLI options (`--box1`, `--box3`). The `mock_subprocess` fixture patches `cli.mcp.server.subprocess.run`. Use `assert_lager_called_with(mock_run, *args)` to verify CLI command construction.

### Factory Test Setup

Factory tests use a conftest that patches the `lager_config` module with test box data and provides fixtures: `app` (Flask test app), `client` (test client), `tmp_data_dir` (temporary data directory), and `mock_box_manager`.

## Architecture Details

### Three-Layer CLI Command Pattern

Every CLI command follows a three-layer architecture:

1. **Command layer** (`cli/commands/<domain>/<command>.py`): Click decorators, argument parsing, user-facing validation. Uses `@click.command()`, `@click.pass_context`, `@click.option("--box")`.

2. **Helper layer** (`cli/core/net_helpers.py`): Shared utilities that prevent duplication across ~30 net-based commands. Key functions:
   - `resolve_box(ctx, box)` — resolve box name/IP with validation
   - `run_backend(ctx, box, impl_script, action, **params)` — execute impl script on box via `LAGER_COMMAND_DATA` env var
   - `run_net_py(ctx, box, *args)` — query nets from box
   - `list_nets_by_role(ctx, box, role)` — filter nets
   - `validate_positive_float()`, `parse_value_with_negatives()` — input validation

3. **Backend layer** (`cli/impl/<domain>/<script>.py`): Scripts uploaded to and executed on the box. They read `LAGER_COMMAND_DATA` (JSON) from environment, import the appropriate dispatcher, and call the action.

```
User → lager supply my-net voltage 3.3
     → cli/commands/power/supply.py (Click parsing)
     → cli/core/net_helpers.py (resolve box, validate)
     → cli/impl/power/supply.py (runs on box, calls dispatcher)
     → box/lager/power/supply/dispatcher.py (routes to driver)
     → box/lager/power/supply/rigol_dp800.py (VISA commands to hardware)
```

### Context and Session System

`LagerContext` (`cli/context/core.py`) is stored in Click's `ctx.obj` and carries config defaults, debug flag, style function, and interpreter selection. Created by `setup_context()` in `main.py`.

Session types in `cli/context/session.py`:
- **DirectHTTPSession** — Primary session type for HTTP communication with box
- **DirectIPSession** — SSH + docker exec for remote script execution, handles module zipping and PYTHONPATH setup

### Configuration

Config file: `~/.lager` (JSON format). Env overrides: `LAGER_CONFIG_FILE_DIR`, `LAGER_CONFIG_FILE_NAME`. Key sections:
- `DEFAULTS` — default box (`gateway_id`), default nets per type
- `BOXES` — saved box entries: `{"name": {"ip": "...", "user": "lagerdata", "version": "main"}}`
- `DEVENV`, `DEBUG` — development environment and debug settings

Config is cached at module level with mtime-based invalidation (`cli/config.py`).

### Box-Side Dispatcher Pattern

Each hardware domain uses a dispatcher that routes operations to the correct instrument driver:

```
saved_nets.json (net config)
    → NetsCache.find_by_name() [O(1) lookup]
    → BaseDispatcher._find_net() [validate role]
    → _choose_driver() [regex match instrument string → driver class]
    → _make_driver() [create or retrieve from class-level cache]
    → Driver instance (e.g., RigolDP800, Keithley2281S, KeysightE36000)
```

`BaseDispatcher` (`box/lager/dispatchers/base.py`) provides:
- Per-class driver cache (not shared across dispatcher types)
- `_resolve_net_and_driver(netname)` → `(driver, channel)` tuple
- Connection alive checks before returning cached drivers
- Abstract `_choose_driver()` and `_make_error()` for subclasses

Each domain module (e.g., `box/lager/power/supply/__init__.py`) uses lazy-loading to defer dispatcher import until first use.

### Net Abstraction

`Net` class (`box/lager/nets/net.py`, ~853 lines) is the core hardware abstraction. Nets represent physical test points on PCBs and are persisted in `/etc/lager/saved_nets.json`.

```python
from lager import Net, NetType
supply = Net.get("power-rail", type=NetType.PowerSupply)
supply.voltage(3.3)
supply.enable()
```

`Net.get_from_saved_json()` performs instrument detection via string matching to select the correct driver class (e.g., `keithley.*2281s` → Keithley2281S). It returns a mapper object that delegates to either a direct driver or an HTTP proxy (`Device` class in `nets/device.py`).

The `Device` class proxies calls to the Rust hardware service at `localhost:8080` via HTTP POST to `/invoke`.

### Box Service Topology

All services run inside a Docker container on the box:

| Service | Port | Purpose |
|---------|------|---------|
| Flask+SocketIO (Python) | 5000 | Script execution, WebSocket streaming, REST API |
| Hardware Service (Rust) | 8080 | Instrument control via VISA/device proxying |
| Debug Service | 8765 | GDB/J-Link embedded debugging |
| Oscilloscope Daemon (Rust) | 8082–8085 | WebSocket + WebTransport oscilloscope streaming |
| Factory Dashboard | 5001 | Web-based test runner (separate container) |

The oscilloscope daemon uses `tokio` async runtime with crossbeam queues for streaming. It supports PicoScope 2000/2000a and Rigol MSO5000 via feature flags in Cargo.toml.

### Box Configuration Files

All stored in `/etc/lager/`:
- `saved_nets.json` — user-created net definitions
- `available_instruments.json` — hardware inventory
- `box_id` — unique box identifier
- `version` — software version
- `webcam_streams.json` — active webcam mappings
- `org_secrets.json` — organization credentials (optional)

### Exception Hierarchy

`box/lager/exceptions.py` defines `LagerBackendError(message, device=None, backend=None)` as the base, with per-domain subclasses: `SupplyBackendError`, `BatteryBackendError`, `SolarBackendError`, `ELoadBackendError`, `USBBackendError`, `ThermocoupleBackendError`, `WattBackendError`, `ADCBackendError`, `DACBackendError`, `GPIOBackendError`, `UARTBackendError`, `SPIBackendError`, `I2CBackendError`, `EnergyAnalyzerBackendError`. Also: `LibraryMissingError`, `DeviceNotFoundError`, `DeviceLockError`, `PortStateError`.

### MCP Server

The MCP server (`cli/mcp/`) exposes CLI functionality to AI assistants via the Model Context Protocol. Uses FastMCP from the `mcp` package. Entry point: `lager-mcp` (or `python -m cli.mcp`).

Tools are organized in `cli/mcp/tools/` (20+ modules mirroring CLI domains). Each tool function calls `run_lager(*args)` which invokes the `lager` CLI as a subprocess and returns stdout/stderr.

### Custom Click Parameter Types

`cli/core/param_types.py` defines specialized Click `ParamType` subclasses: `MemoryAddressType`, `HexParamType`, `HexArrayType`, `VarAssignmentType`, `EnvVarType`, `BinfileType`, `CanFrameType`, `CanFilterType`, `ADCChannelType`, `CanbusRange`, `PortForwardType`.

### Output Streaming Protocol

The V1 protocol (`cli/core/utils.py`) is a binary streaming format for Python script output: `<fileno> <length> <content>`. Stream types: `EXIT` (exit code), `STDOUT`, `STDERR`, `OUTPUT` (structured data). The `OutputHandler` class parses this with decoders for raw, pickle, JSON, and YAML.

## Code Conventions

### Python and Bash Style
- **Python:** PEP 8. No explicit linter config in repo — use standard PEP 8 tooling.
- **Bash:** ShellCheck for linting.
- **License:** Apache 2.0. All new files must include: `# Copyright 2024-2026 Lager Data LLC` and `# SPDX-License-Identifier: Apache-2.0`.

### Import Rules
- Always use top-level public imports: `from lager import Net, NetType`
- Never use internal paths: ~~`from lager.nets.net import Net`~~, ~~`from lager.measurement.watt.joulescope_js220 import JoulescopeJS220`~~
- Non-net features (no Net abstraction) can use module imports: `from lager.binaries import run_custom_binary`

### Adding a New CLI Command

1. Create command module in `cli/commands/<domain>/<command>.py` using Click decorators
2. Create backend impl script in `cli/impl/<domain>/<script>.py` that reads `LAGER_COMMAND_DATA` from env
3. Register command in `cli/main.py` via `cli.add_command(<command>)`
4. Use helpers from `cli/core/net_helpers.py` (resolve_box, run_backend, validate_net, etc.)
5. Add tests in appropriate test directories

### Adding a New Box Hardware Driver

1. Create driver in `box/lager/<domain>/<subdomain>/<driver>.py` implementing the domain's abstract base (e.g., `SupplyNet` ABC)
2. Add instrument regex pattern to the domain dispatcher's `_choose_driver()` method
3. Override `_make_driver()` in dispatcher if the driver has a non-standard constructor signature
4. Add HTTP handlers in `box/lager/http_handlers/` if WebSocket/REST endpoints are needed
5. Export from `box/lager/__init__.py` if it's part of the public API

### Writing Python API Tests

Python API tests are standalone scripts (NOT pytest), run on the box via `lager python`:
- Entry point: `def main()` called from `if __name__ == "__main__": main()`
- Print-based results with `PASS:` / `FAIL:` prefix
- Exit with `sys.exit(1)` on failure
- Include a docstring with the exact `lager python` run command
- Use `from lager import Net, NetType` for hardware access

### Writing Bash Integration Tests

Source the framework and use its functions — never redefine them:
```bash
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"
init_harness
# ... start_section, track_test/track_test_msg, run_and_track ...
print_summary
exit_with_status
```

Key framework functions: `init_harness`, `start_section "name"`, `track_test "pass"/"fail"/"exclude"`, `track_test_msg "status" "msg"`, `skip_test "name" "reason"`, `run_and_track "desc" "cmd"`, `run_expect_fail "desc" "cmd" "pattern"`, `print_summary`, `exit_with_status`, `register_box_from_ip "input"`.

### Writing MCP Tool Tests

MCP unit tests use the `mock_subprocess` fixture and `assert_lager_called_with` helper:
```python
from cli.mcp.tools.power import lager_supply_voltage
lager_supply_voltage(box="X", net="psu1", voltage=3.3)
assert_lager_called_with(mock_subprocess, "supply", "psu1", "voltage", "3.3", "--yes", "--box", "X")
```

### PR Checklist

From the PR template: code follows style guidelines, copyright headers on new files, CHANGELOG.md updated for user-facing changes, documentation updated if applicable.

## Deployment

```bash
# Deploy box software (recommended)
lager install --ip <BOX_IP>

# Or run deployment script directly
cli/deployment/scripts/setup_and_deploy_box.sh <BOX_IP> [--user lagerdata] [--branch main]

# Deploy factory dashboard to a box
factory/deploy_factory.sh <BOX_IP>

# Configure box firewall (default-deny with VPN-only access to Lager ports)
cli/deployment/security/secure_box_firewall.sh
```

## Versioning and Releases

Follows semantic versioning. Version is in `cli/__init__.py`. Release process documented in `RELEASE_PROCESS.md`: update version → update CHANGELOG.md (Keep a Changelog format) → create release notes MDX in `docs/source/release-notes/` → update `docs/docs.json` navigation → tag → build → upload to PyPI.

Entry points: `lager` → `cli.main:cli`, `lager-mcp` → `cli.mcp.server:main`.

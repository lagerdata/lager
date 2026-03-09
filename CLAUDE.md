# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Lager is a hardware test automation monorepo. The CLI runs on developer machines and sends commands to Box hardware (dedicated Linux devices) that control test instruments (power supplies, oscilloscopes, ADC/DAC, etc.). Communication happens over HTTP (direct IP, Tailscale VPN, or local network).

## Repository Structure

```
cli/        # Python Click-based CLI (pip install lager-cli)
box/        # Box-side Python libraries + services (runs on hardware)
factory/    # Flask factory test dashboard
test/       # Integration, API, MCP, and unit tests
docs/       # Mintlify documentation
```

## Commands

### CLI Development

```bash
# Install CLI in editable mode (run from repo root or cli/)
cd cli && pip install -e .
# Install with MCP support
cd cli && pip install -e ".[mcp]"

lager --version
lager --help
```

### Running Tests

```bash
# Unit tests (no hardware required)
cd test && pytest unit/

# MCP unit tests (no hardware required)
python -m pytest test/mcp/unit/ -v --import-mode=importlib -c /dev/null

# MCP integration tests (requires two boxes)
python -m pytest test/mcp/integration/ -v --import-mode=importlib -c /dev/null \
    --box1 <BOX> --box3 <BOX>

# Factory webapp tests
python -m pytest test/factory/ -v

# Python API tests (run on box via lager python)
lager python test/api/power/test_supply_comprehensive.py --box <BOX>

# Bash integration tests (requires hardware)
./test/integration/power/supply.sh <BOX> <NET>
```

**Always use `--import-mode=importlib -c /dev/null`** for MCP tests to prevent `test/mcp` from shadowing the `mcp` PyPI package.

### Oscilloscope Daemon (Rust)

```bash
cd box/oscilloscope-daemon
cargo build --release   # requires Rust 1.85+ (edition 2024)
```

## Architecture

### Command Execution Flow

1. **CLI command** (`cli/commands/<category>/`) parses user input
2. Calls `resolve_box()` to get the box IP from name or config
3. Calls `run_backend()` which uploads an **impl script** (`cli/impl/<category>/`) to the box via HTTP
4. The impl script runs inside a Docker container (`python`) on the box
5. Output streams back to the CLI in real-time

### Two Session Types

- **`DirectHTTPSession`** (`cli/context/session.py`): Default. Posts to `http://<box-ip>:5000`. Works over Tailscale, VPN, or local network.
- **`DirectIPSession`**: Uses SSH + `docker exec` to run scripts. Falls back for some operations.

### The `impl/` Pattern

Every hardware command has two parts:
- **`cli/commands/<category>/foo.py`** — Click command, parses args, calls `run_backend()`
- **`cli/impl/<category>/foo.py`** — Execution script that runs on the box inside Docker

`run_backend()` passes parameters via the `LAGER_COMMAND_DATA` env var as JSON: `{"action": "...", "params": {...}}`.

### Net Abstraction

"Nets" represent named test points. On the box side, `box/lager/nets/net.py` is the core abstraction. The box HTTP server (`box/lager/box_http_server.py`) routes CLI requests to hardware drivers via dispatchers.

### Key Helper: `cli/core/net_helpers.py`

All net-based commands use these shared helpers to avoid duplication:
- `resolve_box(ctx, box)` — resolves box name → IP
- `require_netname(ctx, command_name)` — gets net from context or raises
- `run_backend(ctx, box, "foo.py", "action", **params)` — execute impl script
- `run_backend_with_env(...)` — same but with extra env vars and timeout
- `list_nets_by_role(ctx, box, role)` — query box for nets by type

### Adding a New CLI Command

1. Create `cli/commands/<category>/mycommand.py` with Click group/commands
2. Create `cli/impl/<category>/mycommand.py` as the box-side execution script
3. Register in `cli/main.py` with `cli.add_command(mycommand)`

### MCP Server (`cli/mcp/`)

Wraps the CLI as an MCP (Model Context Protocol) server so AI tools can control hardware. Each domain has a tool file in `cli/mcp/tools/` that calls `run_lager(*args)` to invoke the CLI.

```bash
# Add to Claude Code
claude mcp add --transport stdio lager -- lager-mcp

# Test with MCP inspector
mcp dev cli/mcp/server.py
```

### Box-Side Architecture (`box/lager/`)

- **`box_http_server.py`** — Flask+WebSocket server (port 5000) receiving CLI requests
- **`nets/`** — Net class, device proxy, multiplexer management
- **`dispatchers/`** — `BaseDispatcher` pattern routes commands to hardware drivers
- **`http_handlers/`** — Flask route handlers organized by domain
- **`cache.py`** — Thread-safe `NetsCache` singleton for O(1) net lookups
- Domain subdirs: `power/`, `io/`, `measurement/`, `protocols/`, `automation/`, `debug/`

### Config (`~/.lager`)

JSON file storing box registry (`DEFAULTS.gateway_id`), default nets per type (`net_<role>`), and devenv settings. Managed by `cli/config.py`.

```bash
lager boxes add --name my-box --ip <IP>
lager defaults add --box my-box
```

## Test Conventions

### Python API Tests (`test/api/`)

Standalone scripts (not pytest) uploaded and run on the box via `lager python`. Use print-based `PASS:` / `FAIL:` output, `sys.exit(1)` on failure. Entry point must be `def main()`.

Always import via public API — **never** use internal paths:
```python
from lager import Net, NetType  # correct
# from lager.nets.net import Net  # WRONG
```

### Bash Integration Tests (`test/integration/`)

Use the shared test framework:
```bash
source "${SCRIPT_DIR}/../../framework/harness.sh"
init_harness
track_test "pass"   # or "fail"
print_summary && exit_with_status
```

### MCP Tests (`test/mcp/`)

- **`unit/`** — pytest with mocked subprocess calls (~254 tests)
- **`integration/`** — pytest with real hardware (~64 tests)

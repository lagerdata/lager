# Lager

Monorepo for the Lager Data platform - a hardware test automation system that enables remote control of test equipment and embedded devices.

## Overview

Lager provides a client-server architecture where:
- **CLI** runs on developer machines and sends commands
- **Box** (dedicated Linux hardware) connects to and controls test equipment
- **Communication** happens over Tailscale VPN or direct network

## Repository Structure

```
lager/
├── cli/                    # Command-line interface (includes deployment scripts)
├── box/                    # Box hardware control software
├── test/                   # Integration and API tests
└── docs/                   # Documentation (Mintlify docs + deployment reference)
```

---

## CLI (`cli/`)

The CLI is a Python Click-based command-line tool installed via `pip install lager-cli`.

### Directory Structure

```
cli/
├── main.py                 # Entry point - registers all commands
├── config.py               # Configuration management (~/.lager)
├── box_storage.py          # Box/instrument storage utilities
│
├── core/                   # Shared utilities (consolidated)
│   ├── net_helpers.py      # Net command helpers (resolve_box, run_net_py, etc.)
│   ├── param_types.py      # Custom Click parameter types
│   ├── utils.py            # General utilities
│   ├── ssh_utils.py        # SSH connection utilities
│   ├── matchers.py         # Pattern matching utilities
│   └── net_storage.py      # Net storage operations
│
├── context/                # Session and authentication management
│   ├── core.py             # LagerContext class
│   ├── session.py          # DirectIPSession, LagerSession, DirectHTTPSession
│   ├── error_handlers.py   # Docker, CANbus error handling
│   └── ci_detection.py     # CI environment detection
│
├── commands/               # Command modules (grouped by domain)
│   ├── power/              # Power equipment commands
│   │   ├── supply.py       # Power supply control
│   │   ├── battery.py      # Battery simulator control
│   │   ├── solar.py        # Solar simulator control
│   │   └── eload.py        # Electronic load control
│   │
│   ├── measurement/        # Measurement commands
│   │   ├── adc.py          # Analog-to-digital converter
│   │   ├── dac.py          # Digital-to-analog converter
│   │   ├── gpi.py          # General purpose input
│   │   ├── gpo.py          # General purpose output
│   │   ├── scope.py        # Oscilloscope commands
│   │   ├── logic.py        # Logic analyzer
│   │   ├── thermocouple.py # Temperature measurement
│   │   └── watt.py         # Power measurement
│   │
│   ├── communication/      # Communication commands
│   │   ├── uart.py         # Serial communication
│   │   ├── i2c.py          # I2C communication
│   │   ├── spi.py          # SPI communication
│   │   ├── ble.py          # Bluetooth Low Energy
│   │   ├── blufi.py        # BluFi provisioning
│   │   ├── wifi.py         # WiFi configuration
│   │   └── usb.py          # USB hub control
│   │
│   ├── development/        # Development commands
│   │   ├── debug/          # Embedded debugging (GDB, flash, etc.)
│   │   ├── arm.py          # Robotic arm control
│   │   ├── python.py       # Remote Python execution
│   │   └── devenv.py       # Development environment
│   │
│   ├── box/                # Box management commands
│   │   ├── hello.py        # Connectivity test
│   │   ├── status/         # Box status
│   │   ├── boxes.py        # Box registration
│   │   ├── instruments.py  # Instrument listing
│   │   ├── nets.py         # Net management
│   │   └── ssh.py          # SSH access
│   │
│   └── utility/            # Utility commands
│       ├── defaults.py     # Default settings
│       ├── update.py       # Box software updates
│       ├── pip.py          # Package management
│       └── webcam.py       # Webcam streaming
│
├── impl/                   # Implementation scripts (run on box)
│   ├── power/              # supply.py, battery.py, solar.py, eload.py
│   ├── measurement/        # adc.py, dac.py, scope.py, etc.
│   ├── communication/      # uart.py, ble.py, wifi.py
│   └── device/             # usb.py, arm.py, hello.py, webcam.py
│
└── vendor/                 # Vendored third-party libraries
    ├── PyCRC/              # CRC calculation
    └── (elftools at cli/elftools/)
```

### Command Groups

| Group | Commands | Description |
|-------|----------|-------------|
| **Power** | `supply`, `battery`, `solar`, `eload` | Control power equipment |
| **Measurement** | `adc`, `dac`, `gpi`, `gpo`, `scope`, `logic`, `thermocouple`, `watt`, `energy` | Read sensors and instruments |
| **Communication** | `uart`, `i2c`, `spi`, `ble`, `blufi`, `wifi`, `usb` | Device communication |
| **Development** | `debug`, `arm`, `python`, `devenv`, `terminal` | Embedded development |
| **Box** | `hello`, `status`, `boxes`, `instruments`, `nets`, `ssh` | Box management |
| **Utility** | `defaults`, `update`, `pip`, `webcam`, `exec`, `logs`, `binaries`, `install`, `install-wheel`, `uninstall` | Utilities |

---

## Box (`box/lager/`)

Python libraries and services running on box hardware that control test equipment.

### Directory Structure

```
box/lager/
├── __init__.py             # Main exports (Net, NetType, etc.)
├── core.py                 # Core utilities (Interface, Transport)
├── cache.py                # Thread-safe NetsCache singleton
├── constants.py            # Centralized configuration constants
├── exceptions.py           # Unified exception hierarchy
├── box_http_server.py      # Main Flask+WebSocket server
│
├── mcp/                    # MCP server for AI agent integration
│   ├── server.py           # FastMCP server (port 8100)
│   ├── tools/              # Tool implementations (power, debug, spi, etc.)
│   ├── resources/          # Bench discovery resources
│   ├── schemas/            # Pydantic models (bench, nets, scenarios)
│   └── engine/             # Scenario runner, bench loader, capability graph
│
├── dispatchers/            # Shared dispatcher infrastructure
│   ├── base.py             # BaseDispatcher abstract class
│   └── helpers.py          # Shared helper functions
│
├── power/                  # Power equipment control
│   ├── supply/             # Power supplies (Rigol, Keithley, Keysight)
│   │   ├── dispatcher.py   # Routes commands to drivers
│   │   ├── supply_net.py   # Abstract interface
│   │   └── *.py            # Driver implementations
│   ├── battery/            # Battery simulators (Keithley 2281S)
│   ├── solar/              # Solar simulators (EA PSI/EL)
│   └── eload/              # Electronic loads (Rigol DL3021)
│
├── io/                     # I/O hardware (LabJack T7)
│   ├── adc/                # Analog input
│   ├── dac/                # Analog output
│   └── gpio/               # Digital I/O
│
├── measurement/            # Measurement devices
│   ├── thermocouple/       # Temperature (Phidget)
│   ├── watt/               # Power meter (Yocto-Watt)
│   └── scope/              # Oscilloscope (Rigol MSO5000)
│
├── protocols/              # Communication protocols
│   ├── uart/               # Serial communication
│   ├── i2c/                # I2C communication
│   ├── spi/                # SPI communication
│   ├── ble/                # Bluetooth Low Energy
│   └── wifi/               # WiFi management
│
├── automation/             # Automation hardware
│   ├── arm/                # Robotic arm (Rotrics)
│   ├── usb_hub/            # USB hubs (Acroname, YKUSH)
│   └── webcam/             # Camera streaming
│
├── nets/                   # Core net framework
│   ├── net.py              # Net class - hardware abstraction
│   ├── device.py           # Device proxy (HTTP to hardware)
│   ├── mux.py              # Multiplexer management
│   └── mappers/            # Net-to-device mappers
│
├── http_handlers/          # HTTP/WebSocket handlers
│   ├── app.py              # Flask app factory
│   ├── uart.py             # UART streaming handlers
│   ├── supply.py           # Supply monitoring handlers
│   ├── battery.py          # Battery monitoring handlers
│   └── state.py            # Shared state management
│
├── debug/                  # Embedded debugging
│   ├── api.py              # Debug API
│   └── service.py          # GDB/J-Link integration
│
└── instrument_wrappers/    # Instrument enums and defines
    └── *.py                # Per-vendor definitions
```

### Key Design Patterns

#### Dispatcher Pattern
Each hardware domain uses a dispatcher that routes commands to the appropriate driver:

```python
# Example: box/lager/power/supply/dispatcher.py
from lager.dispatchers import BaseDispatcher
from lager.dispatchers.helpers import find_saved_net, resolve_address

def voltage(netname: str, value: float = None):
    net = find_saved_net(netname, SupplyBackendError)
    driver = _choose_driver(net)
    return driver.voltage(value)
```

#### Net Abstraction
"Nets" represent physical test points on PCBs:

```python
from lager import Net, NetType

# Get a net and control it
supply = Net.get("power-rail", type=NetType.PowerSupply)
supply.voltage(3.3)
supply.enable()
```

#### Caching Layer
Thread-safe singleton cache for net lookups:

```python
from lager.cache import get_nets_cache

cache = get_nets_cache()
net = cache.find_by_name("my-net")  # O(1) lookup
```

---

## Tests (`test/`)

Organized test suite matching the CLI/box domain structure.

### Directory Structure

```
test/
├── framework/              # Shared test infrastructure
│   ├── harness.sh          # Bash test framework
│   ├── colors.sh           # Terminal color definitions
│   ├── test_utils.py       # Python test utilities
│   └── fixtures.py         # Reusable pytest fixtures
│
├── assets/                 # Test assets
│   ├── firmware/           # Test firmware (*.elf, *.hex)
│   └── data/               # Test data files
│
├── integration/            # Bash integration tests
│   ├── power/              # supply.sh, battery.sh, solar.sh, eload.sh
│   ├── io/                 # labjack.sh
│   ├── usb/                # usb.sh, acroname.sh, ykush.sh
│   ├── communication/      # uart.sh, debug.sh
│   ├── sensors/            # thermocouple.sh
│   └── infrastructure/     # deployment.sh, generic.sh, nets.sh
│
├── api/                    # Python API tests
│   ├── power/              # test_supply_*.py, test_battery_*.py
│   ├── io/                 # test_adc_*.py, test_dac_*.py, test_gpio_*.py
│   ├── usb/                # test_usb_*.py
│   ├── communication/      # test_uart_*.py, test_ble_*.py
│   ├── sensors/            # test_thermocouple_*.py, test_watt_*.py
│   └── peripherals/        # test_arm_*.py, test_scope_*.py
│
└── unit/                   # Unit tests
    └── cli/                # CLI unit tests
```

### Running Tests

```bash
# Integration tests (requires hardware)
cd test/integration/power
./supply.sh <box-ip> <net-name>

# Python API tests
lager python test/api/power/test_supply_comprehensive.py --box <box-name>

# Unit tests (no hardware)
pytest unit/
```

---

## Deployment (`cli/deployment/`)

Box deployment and security automation, packaged with the CLI.

### Directory Structure

```
cli/deployment/
├── scripts/                # Deployment scripts
│   ├── setup_and_deploy_box.sh    # Main deployment
│   ├── setup_ssh_key.sh           # SSH key setup
│   └── convert_to_sparse_checkout.sh  # Fix directory structure
└── security/               # Security scripts
    └── secure_box_firewall.sh     # UFW configuration
```

Additional deployment docs (cloud-init, process guides) live in `docs/reference/deployment/`.

### Deploying a Box

```bash
# Using the CLI (recommended)
lager install --ip <BOX_IP>

# Or run the script directly
cli/deployment/scripts/setup_and_deploy_box.sh <BOX_IP>
```

---

## Quick Start

### Installation

```bash
# Install CLI
pip install lager-cli

# Add a box
lager boxes add --name my-box --ip <BOX_IP>

# Test connectivity
lager hello --box my-box
```

### Common Commands

```bash
# Power supply control
lager supply my-net voltage 3.3 --box my-box
lager supply my-net enable --box my-box

# Read ADC
lager adc my-net --box my-box

# Flash firmware
lager debug my-net flash --hex firmware.hex --box my-box

# Open UART terminal
lager uart --box my-box --baudrate 115200
```

### Python API

```python
from lager import Net, NetType

# Connect to a power supply net
supply = Net.get("power-rail", type=NetType.PowerSupply)
supply.voltage(3.3)
supply.enable()

# Read temperature
tc = Net.get("temp-sensor", type=NetType.Thermocouple)
print(f"Temperature: {tc.read()}°C")
```

---

## MCP Server (AI Agent Integration)

The Lager box includes an **MCP (Model Context Protocol) server** that lets AI coding agents control hardware directly. Any MCP-compatible client — Cursor, Claude Code, Claude Desktop, or custom agents — can connect over HTTP and run hardware operations without writing CLI commands.

### How It Works

```
AI Agent (Cursor, Claude Code, etc.)
    |  MCP (streamable-http)
    v
Lager MCP Server (on-box, port 8100)
    |  direct lager.Net API
    v
Hardware (power supplies, debug probes, GPIO, protocols, etc.)
```

The server runs inside the box's Docker container alongside existing services. All hardware operations execute directly on-box — no CLI subprocess calls, no round trips back to the agent.

### Connecting Your Agent

Add the Lager box as an MCP server in your client's configuration. Replace `<box-ip>` with the box's IP address (Tailscale IP, LAN IP, etc.).

**Cursor** — create or edit `.cursor/mcp.json` in your project:
```json
{
  "mcpServers": {
    "lager": {
      "url": "http://<box-ip>:8100/mcp"
    }
  }
}
```

**Claude Code** (CLI):
```bash
claude mcp add --transport http lager http://<box-ip>:8100/mcp
```

**Claude Desktop** — edit `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "lager": {
      "url": "http://<box-ip>:8100/mcp"
    }
  }
}
```

**Python (programmatic)**:
```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async with streamablehttp_client("http://<box-ip>:8100/mcp") as (read, write, _):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool("discover_bench", {})
```

### Recommended Workflow

1. **Discover hardware** — the agent calls `discover_bench()` to see available instruments, nets, and capabilities.
2. **Run scenarios** — use `run_scenario()` with a multi-step test plan. All steps execute on-box in one round trip, keeping latency low.
3. **Debug interactively** — fine-grained tools (`supply_set_voltage`, `debug_flash`, `spi_transfer`, etc.) are available for one-off operations.

### Verifying the Server

```bash
# Quick connectivity check from any machine
curl http://<box-ip>:8100/mcp

# Check logs on the box
docker exec lager tail -20 /tmp/lager-mcp-server.log
```

### Available Tools

| Category | Tools |
|----------|-------|
| **Discovery** | `discover_bench`, `assess_suitability` |
| **Scenarios** | `run_scenario` (multi-step, one round trip), `run_hil_program` (escape hatch) |
| **Power** | `supply_set_voltage`, `supply_enable`, `supply_measure`, `battery_*`, `eload_*`, `solar_*` |
| **Debug** | `debug_flash`, `debug_reset`, `debug_connect`, `debug_gdbserver`, `rtt_write`, `rtt_read` |
| **Communication** | `spi_transfer`, `i2c_scan`, `i2c_read`, `uart_send`, `uart_read` |
| **Measurement** | `adc_read`, `dac_set`, `gpio_read`, `gpio_set`, `watt_read`, `thermocouple_read` |
| **Scope** | `scope_enable`, `scope_measure`, `scope_trigger_edge`, `picoscope_capture` |
| **Other** | `usb_enable`, `webcam_start`, `ble_scan`, `wifi_status`, `router_info`, `flash_firmware` |

---

## Development

### CLI Development

```bash
cd cli
pip install -e .
lager --help
```

### Box Development

```bash
lager install --ip <box-ip>
```

### Oscilloscope Daemon (Rust)

The `box/oscilloscope-daemon/` directory contains a Rust-based WebSocket/WebTransport server for real-time oscilloscope streaming. Building it requires **Rust 1.85+** (edition 2024).

```bash
cd box/oscilloscope-daemon
cargo build --release
```

### Running Tests

```bash
# Unit tests
cd test
pytest unit/

# Integration tests (requires box)
./integration/power/supply.sh <box-ip> <net-name>
```

---

## Supported Hardware

| Category | Devices |
|----------|---------|
| **Power Supplies** | Rigol DP800, Keithley 2200/2280, Keysight E36200/E36300 |
| **Battery Simulators** | Keithley 2281S |
| **Solar Simulators** | EA PSI/EL series |
| **Electronic Loads** | Rigol DL3021 |
| **Oscilloscopes** | Rigol MSO5000 series, PicoScope 2000/2000a |
| **I/O** | LabJack T7 (ADC/DAC/GPIO) |
| **Temperature** | Phidget thermocouples |
| **Power Meters** | Yocto-Watt |
| **USB Hubs** | Acroname, YKUSH |
| **Debug Probes** | J-Link, CMSIS-DAP, ST-Link (via pyOCD) |
| **Robot Arms** | Rotrics Dexarm |

---

## Documentation

Full documentation available at [docs.lagerdata.com](https://docs.lagerdata.com)

- [CLI Reference](https://docs.lagerdata.com/reference/cli)
- [Python API Reference](https://docs.lagerdata.com/reference/python)
- [Getting Started Guide](https://docs.lagerdata.com/essentials/quickstart)

---

## License

Apache License 2.0 - Lager Data LLC. See [LICENSE](LICENSE) for details.

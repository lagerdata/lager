# Lager Test Suite

Comprehensive test suite for validating Lager CLI commands, Python API, and MCP server.

## Directory Structure

```
test/
├── api/                        # Python API tests (run on box via `lager python`)
│   ├── power/                  # test_supply_*.py, test_battery_*.py, test_eload_*.py
│   ├── io/                     # test_adc_*.py, test_dac_*.py, test_gpio_*.py, test_pin_conflict.py
│   ├── communication/          # test_i2c_*.py, test_spi_*.py, test_uart_*.py, test_ble_*.py
│   ├── sensors/                # test_joulescope.py, test_energy_*.py, test_watt_*.py, test_thermocouple_*.py
│   ├── peripherals/            # test_arm_*.py, test_scope_*.py, test_webcam_*.py
│   ├── usb/                    # test_usb_*.py
│   └── utility/                # test_custom_binaries.py, test_list_nets.py
│
├── integration/                # Bash CLI integration tests
│   ├── power/                  # supply.sh, battery.sh, solar.sh, eload.sh, keysight_supply.sh, multichannel_supply.sh
│   ├── io/                     # labjack.sh, gpio_aardvark.sh, gpio_ft232h.sh
│   ├── communication/          # uart.sh, debug.sh, jlink_script.sh, i2c.sh, spi.sh
│   ├── sensors/                # thermocouple.sh
│   ├── usb/                    # usb.sh, acroname.sh, ykush.sh
│   ├── peripherals/            # arm.sh
│   └── infrastructure/         # deployment.sh, generic.sh, python.sh, nets.sh
│
├── unit/                       # Local unit tests (pytest, no hardware) -- ALL GATED ON PRs
│   ├── box/                    # 55 files: box-side unit tests
│   ├── cli/                    # 33 files: CLI command unit tests
│   ├── measurement/            #  4 files: Joulescope / PPK2 / watt
│   ├── blufi/                  #  2 files: BluFi protocol
│   └── test_*.py               #  5 files: root-level unit tests
│
├── mcp/                        # MCP server tests (pytest)
│   ├── unit/                   # 11 files, 166 tests: mocked, no hardware -- GATED
│   └── integration/            #  1 file: real hardware required
│
├── framework/                  # Shared test infrastructure
│   ├── colors.sh               # Color definitions (GREEN, RED, YELLOW, etc.)
│   ├── harness.sh              # Test harness (init_harness, track_test, print_summary)
│   ├── test_utils.py           # Python test utilities
│   └── fixtures.py             # Shared pytest fixtures
│
├── assets/                     # Test data (firmware files, etc.)
├── CONVENTIONS.md              # How to write and run tests
└── README.md                   # This file
```

## Running Tests

### Python API tests (run on box)

```bash
lager python test/api/power/test_supply_comprehensive.py --box <YOUR-BOX>
lager python test/api/communication/test_i2c_aardvark.py --box <YOUR-BOX>
lager python test/api/sensors/test_joulescope.py --box <YOUR-BOX> -- watt1
```

### Bash CLI integration tests (run from host)

```bash
./test/integration/power/supply.sh <BOX> <NET>
./test/integration/communication/i2c.sh
./test/integration/peripherals/arm.sh <BOX> <NET>
./test/integration/infrastructure/deployment.sh <BOX_IP>
```

### Unit tests (no hardware)

These are what CI runs on every pull request. Always use these flags, and run each suite as its
own pytest process -- `test/unit/box/` and `test/unit/measurement/` need incompatible `lager`
packages in `sys.modules` and cannot share one. See `COVERAGE.md` for the details.

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

### MCP integration tests (real hardware)

```bash
python -m pytest test/mcp/integration/ -v --import-mode=importlib -c /dev/null \
    --box1 <YOUR-BOX> --box3 <YOUR-BOX>
```

## Test Categories

| Category | Directory | Type | Hardware |
|----------|-----------|------|----------|
| Python API | `api/` | `lager python` scripts | Yes |
| CLI integration | `integration/` | Bash scripts | Yes |
| CLI unit | `unit/` | pytest | No |
| MCP unit | `mcp/unit/` | pytest (mocked) | No |
| MCP integration | `mcp/integration/` | pytest | Yes |

## Writing New Tests

See [CONVENTIONS.md](CONVENTIONS.md) for detailed guidelines on:

- Directory structure and where to place tests
- Import rules (`from lager import Net, NetType` -- never internal paths)
- Python API test format (standalone scripts with `def main()`)
- Bash integration test format (source framework, use `track_test`)
- Naming conventions
- How to run each test type

<!-- Copyright 2024-2026 Lager Data -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

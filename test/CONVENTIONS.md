# Test Conventions

This document defines how to write and run tests in the Lager test suite. All new tests should follow these conventions.

## Directory Structure

```
test/
├── api/                  # Python API tests (run on box via `lager python`)
│   ├── power/            # Power supply, battery, solar, eload
│   ├── io/               # ADC, DAC, GPIO, pin conflicts
│   ├── communication/    # I2C, SPI, UART, BLE
│   ├── sensors/          # Joulescope, thermocouple, watt meter
│   ├── peripherals/      # Robotic arm, webcam
│   ├── usb/              # USB hub control
│   └── utility/          # Non-Net features (binaries, net listing)
│
├── integration/          # Bash integration tests (run from host machine)
│   ├── power/            # supply.sh, battery.sh, solar.sh, eload.sh
│   ├── io/               # labjack.sh
│   ├── usb/              # usb.sh, acroname.sh, ykush.sh
│   ├── communication/    # uart.sh, debug.sh, jlink_script.sh
│   ├── sensors/          # thermocouple.sh
│   ├── peripherals/      # arm.sh
│   └── infrastructure/   # deployment.sh, generic.sh, python.sh, nets.sh
│
├── unit/                 # CLI unit tests (pytest, no hardware)
│   └── cli/              # CLI command tests
│
├── mcp/                  # MCP server tests (pytest, unit + integration)
│   ├── unit/             # Mocked subprocess tests (~254 tests)
│   └── integration/      # Real hardware tests (~64 tests)
│
├── framework/            # Shared test infrastructure
│   ├── colors.sh         # Color definitions for bash tests
│   ├── harness.sh        # Bash test harness functions
│   ├── test_utils.py     # Python test utilities
│   └── fixtures.py       # Shared pytest fixtures
│
└── assets/               # Test data (firmware files, etc.)
```

## Import Rules

### Net-related features (REQUIRED)

Always use the top-level public import:

```python
from lager import Net, NetType
```

**Never** use internal paths:

```python
# WRONG - do not use
from lager.nets.net import Net, NetType
from lager.measurement.watt.joulescope_js220 import JoulescopeJS220
from lager.measurement.energy_analyzer.joulescope_energy import JoulescopeEnergyAnalyzer
```

Joulescope features are net-based. Access them through the Net abstraction:

```python
watt = Net.get("net_name", type=NetType.WattMeter)
energy = Net.get("net_name", type=NetType.EnergyAnalyzer)
```

### Non-Net features

Module imports are acceptable for features that don't go through the Net abstraction:

```python
from lager.binaries import run_custom_binary, list_binaries
from lager.ble import Central
```

## Python API Test Format

Python API tests are standalone scripts uploaded and executed on the box via `lager python`. They are **not** pytest tests.

### Template

```python
"""
Description of what this test verifies.

Run via:
    lager python test/api/<domain>/test_<feature>.py --box <BOX>
"""

import sys
from lager import Net, NetType


def main():
    print("=== Feature Test ===\n")

    # Test 1: Description
    print("Test 1: Description")
    net = Net.get("net_name", type=NetType.SomeType)
    result = net.some_method()
    if expected_condition(result):
        print("  PASS: Description of success")
    else:
        print("  FAIL: Description of failure")
        sys.exit(1)

    print("\n=== Feature Test Complete ===")


if __name__ == "__main__":
    main()
```

### Key rules

- Entry point is `def main()` called from `if __name__ == "__main__": main()`
- Print-based results: `PASS:` / `FAIL:` prefix for test outcomes
- Exit with `sys.exit(1)` on failure
- No pytest imports or decorators
- Include a docstring with the `lager python` run command

### Running

```bash
lager python test/api/power/test_supply_comprehensive.py --box <YOUR-BOX>
lager python test/api/communication/test_i2c_aardvark.py --box <YOUR-BOX>
```

## Bash Integration Test Format

Bash integration tests use the shared framework in `test/framework/`.

### Template

```bash
#!/bin/bash
# Description of what this test suite covers

set +e  # DON'T exit on error - we want to track failures

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

# Initialize the test harness
init_harness

# Parse arguments
if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX> <NET>"
  exit 1
fi

BOX_INPUT="$1"
NET="$2"

register_box_from_ip "$BOX_INPUT"
print_script_header "MY TEST SUITE" "$BOX" "$NET"

# ============================================================================
# SECTION 1: BASIC COMMANDS
# ============================================================================
print_section_header "SECTION 1: BASIC COMMANDS"
start_section "Basic Commands"

echo "Test 1.1: Help output"
lager mycommand --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: Basic operation"
OUTPUT=$(lager mycommand $NET --box $BOX 2>&1)
if echo "$OUTPUT" | grep -q "expected"; then
  track_test_msg "pass" "Basic operation OK"
else
  track_test_msg "fail" "Basic operation failed"
fi
echo ""

# Cleanup
lager mycommand $NET disable --box $BOX >/dev/null 2>&1 || true

# Summary and exit
print_summary
exit_with_status
```

### Key rules

- Source `colors.sh` and `harness.sh` from framework
- Call `init_harness` before any tests
- Use `start_section` for each test group
- Use `track_test "pass"` / `track_test "fail"` or `track_test_msg` for results
- End with `print_summary` and `exit_with_status`
- Do **not** redefine framework functions (colors, tracking, summary)

### Framework functions

| Function | Purpose |
|----------|---------|
| `init_harness` | Initialize tracking arrays |
| `start_section "name"` | Begin a new test section |
| `track_test "pass"/"fail"/"exclude"` | Record result (auto-prints [PASS]/[FAIL]/[SKIP]) |
| `track_test_msg "pass" "message"` | Record result with description |
| `skip_test "name" "reason"` | Skip a test with reason |
| `run_and_track "desc" "cmd"` | Run command, auto-track result |
| `run_expect_fail "desc" "cmd" "pattern"` | Expect failure matching pattern |
| `print_summary` | Print summary table |
| `exit_with_status` | Exit 0 if all passed, 1 otherwise |
| `get_timestamp_ms` | Cross-platform millisecond timestamp |
| `register_box_from_ip "input"` | Register IP as temporary box |
| `print_section_header "title"` | Print section separator |
| `print_script_header "title" "box" "net"` | Print test suite header |

### Running

```bash
./test/integration/power/supply.sh <BOX> <NET>
./test/integration/communication/i2c.sh
./test/integration/peripherals/arm.sh <BOX> <NET>
```

## MCP Tests

MCP tests use pytest with mocked subprocess calls (unit) or real hardware (integration).

```bash
# Unit tests (no hardware needed)
python -m pytest test/mcp/unit/ -v --import-mode=importlib -c /dev/null

# Integration tests (requires two hardware boxes)
python -m pytest test/mcp/integration/ -v --import-mode=importlib -c /dev/null \
    --box1 <YOUR-BOX> --box3 <YOUR-BOX>
```

Always use `--import-mode=importlib -c /dev/null` to prevent `test/mcp` from shadowing the `mcp` PyPI package.

## Naming Conventions

| Type | Pattern | Example |
|------|---------|---------|
| Python API test | `test_<feature>.py` | `test_supply_comprehensive.py` |
| Bash integration test | `<feature>.sh` | `supply.sh` |
| MCP unit test | `test_<module>.py` | `test_power_tools.py` |
| CLI unit test | `test_<module>.py` | `test_commands.py` |
| Test directory | lowercase, domain-based | `power/`, `io/`, `sensors/` |

## Adding a New Test

1. Determine the type: Python API (`api/`), bash integration (`integration/`), MCP (`mcp/`), or CLI unit (`unit/`)
2. Place it in the correct domain subdirectory
3. Follow the template for that type
4. For Python API tests: use `from lager import Net, NetType` (not internal paths)
5. For bash tests: source the framework, don't redefine harness functions
6. Include a docstring/comment with the exact run command

<!-- Copyright 2024-2026 Lager Data LLC -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

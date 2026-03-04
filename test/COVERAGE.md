# Test Coverage

This document tracks what test coverage exists across all Lager features and the three main test suites: Python API tests, bash integration tests, and MCP tests.

## Coverage by Domain

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
| **USB Hub** | Yes (7 files) | Yes (3 files) | Yes |
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

## Coverage Gaps

### High Priority

| Gap | Details |
|-----|---------|
| **Logic Analyzer** | No Python API test. Has 21 CLI subcommands but only bash and MCP coverage. |
| **SSH / Exec / Install** | Zero test coverage in any suite. |

### Medium Priority

| Gap | Details |
|-----|---------|
| **Watt / Energy** | No bash integration tests. Python API and partial MCP only. |
| **Scope** | No bash integration test. Has 39 CLI subcommands. |
| **BLE** | No bash integration test. Python API and MCP only. |
| **Box Management** | No Python API tests for boxes, status, or hello commands. |

### Low Priority

| Gap | Details |
|-----|---------|
| **Utility Commands** | Defaults, Logs, Pip have MCP-only coverage. |
| **Rotation / Actuate** | Python API tests only; no bash or MCP tests. |
| **Webcam** | No bash integration test. |

## Coverage Strengths

- **Communication protocols**: I2C and SPI have 18+ test files across three hardware backends (Aardvark, LabJack, FT232H) with full 3-suite coverage.
- **Power management**: Supply, Battery, Solar, and ELoad all have full 3-suite coverage with tolerance checks, boundary tests, and safety teardown.
- **I/O domain**: 17 Python API tests covering ADC, DAC, GPIO, and PWM with real value assertions and safety teardown. 3 FT232H/Aardvark API tests are gold standard with 100+ assertions each.
- **MCP server**: 384 unit tests (mocked, no hardware) plus 64+ integration tests covering 165+ tools across 25 unit and 11 integration test files.

## Test File Inventory

```
test/
├── api/                  # Python API tests (76 files, run on box via `lager python`)
│   ├── communication/    # 28 files: I2C, SPI, UART, BLE, BluFi, WiFi, debug
│   ├── io/               # 17 files: ADC, DAC, GPIO, PWM, pin conflict
│   ├── peripherals/      # 9 files: scope, arm, webcam, rotation, actuate
│   ├── power/            # 4 files: supply, battery, solar, eload
│   ├── sensors/          # 9 files: thermocouple, watt, energy, joulescope
│   ├── usb/              # 7 files: USB hub enable/disable/toggle/stress
│   └── utility/          # 2 files: binaries, net listing
├── integration/          # Bash integration tests (36 files, run from host via harness.sh)
│   ├── communication/    # 14 files: I2C, SPI, UART, WiFi, debug, J-Link
│   ├── io/               # 4 files: LabJack, GPIO (Aardvark, FT232H)
│   ├── power/            # 6 files: supply, battery, solar, eload, keysight, multichannel
│   ├── usb/              # 3 files: USB, Ykush, Acroname
│   ├── infrastructure/   # 6 files: boxes, nets, deployment, devenv, python, generic
│   ├── measurement/      # 1 file: logic analyzer
│   ├── sensors/          # 1 file: thermocouple
│   └── peripherals/      # 1 file: robotic arm
├── mcp/                  # MCP server tests (pytest)
│   ├── unit/             # 25 test files: mocked subprocess, no hardware
│   └── integration/      # 11 test files: live hardware required
├── unit/                 # Local unit tests
│   ├── blufi/            # 1 file: BluFi protocol unit tests
│   └── cli/              # 2 files: Keithley locking, performance
└── framework/            # Test utilities
    ├── harness.sh        # Bash test framework
    ├── colors.sh         # Bash color utilities
    ├── fixtures.py       # Pytest fixtures with auto-cleanup
    └── test_utils.py     # Python test helpers
```

### Python API Tests (`test/api/`)

#### Power (4 files)

| File | What it tests |
|------|---------------|
| `test_supply_comprehensive.py` | Voltage/current set, readback, enable/disable, OVP/OCP, limits |
| `test_battery_comprehensive.py` | SOC, VOC, capacity, mode, enable/disable, OVP/OCP, clear |
| `test_eload_comprehensive.py` | CC, CV, CR, CP modes, enable/disable, state verification |
| `test_solar_comprehensive.py` | Set, stop, irradiance, resistance, temperature, VOC, MPP |

#### Communication (28 files)

| File | What it tests |
|------|---------------|
| `test_i2c_aardvark.py` | Aardvark I2C scan, read, write, config |
| `test_i2c_aardvark_api.py` | Aardvark I2C edge cases (100+ assertions) |
| `test_i2c_ft232h.py` | FT232H I2C scan, read, write |
| `test_i2c_labjack.py` | LabJack I2C scan, read, write, config |
| `test_i2c_labjack_api.py` | LabJack I2C edge cases (100+ assertions) |
| `test_spi_aardvark.py` | Aardvark SPI transfer, config (100+ assertions) |
| `test_spi_aardvark_auto.py` | Aardvark SPI auto-CS, chip ID verification |
| `test_spi_aardvark_incremental_config.py` | Aardvark SPI config mutation safety |
| `test_spi_aardvark_manual.py` | Aardvark SPI manual CS, calibration |
| `test_spi_api.py` | SPI API type assertions |
| `test_spi_dead_zone_clamp.py` | LabJack SPI throttle dead zone clamping |
| `test_spi_ft232h.py` | FT232H SPI BMP280 multi-register reads |
| `test_spi_ft232h_auto.py` | FT232H SPI auto-CS, chip ID + readback |
| `test_spi_ft232h_manual_cs.py` | FT232H SPI manual CS behavior |
| `test_spi_labjack.py` | LabJack SPI BMP280 assertions |
| `test_spi_labjack_auto.py` | LabJack SPI auto-CS, teardown |
| `test_spi_labjack_manual.py` | LabJack SPI manual CS cleanup |
| `test_spi_write_readback.py` | SPI register write/readback verification |
| `test_uart_comprehensive.py` | UART loopback, baud rates, data patterns |
| `test_ble_basic.py` | BLE scan, basic device discovery |
| `test_ble_client.py` | BLE client connection to a real device |
| `test_ble_comprehensive.py` | BLE scan, connect, services, characteristics |
| `test_ble_with_real_devices.py` | BLE interaction with real peripherals |
| `test_blufi_comprehensive.py` | BluFi provisioning protocol |
| `test_debug_comprehensive.py` | J-Link flash, reset, erase, memory read |
| `test_wait_for_level.py` | GPI level wait with 15 sub-tests |
| `test_wait_for_level_simple.py` | GPI level wait (simplified) |
| `test_wifi_comprehensive.py` | WiFi scan, connect, status, delete |

#### I/O (17 files)

| File | What it tests |
|------|---------------|
| `test_adc_single.py` | Single ADC read, type + range check |
| `test_adc_multiple.py` | Multi-channel ADC reads (8 channels) |
| `test_adc_continuous.py` | Continuous ADC sampling, stability |
| `test_dac_output.py` | DAC voltage output, readback tolerance |
| `test_dac_ramp.py` | DAC voltage ramp, monotonic increase |
| `test_dac_adc_loopback.py` | DAC-to-ADC loopback verification |
| `test_gpio_output.py` | GPIO HIGH/LOW output verification |
| `test_gpio_input.py` | GPIO input read per-channel |
| `test_gpio_multiple.py` | Multi-pin GPIO verification |
| `test_gpio_pulse.py` | GPIO pulse output, post-pulse state |
| `test_gpio_ft232h.py` | FT232H GPIO (100+ assertions) |
| `test_gpio_ft232h_api.py` | FT232H GPIO API edge cases (100+ assertions) |
| `test_gpio_aardvark_api.py` | Aardvark GPIO API edge cases (100+ assertions) |
| `test_io_comprehensive.py` | Combined ADC + DAC + GPIO tests |
| `test_pin_conflict.py` | Pin conflict detection |
| `test_pwm_measurement.py` | PWM frequency, Vpp, duty cycle |
| `diag_aardvark_gpio_adc.py` | Diagnostic tool (not a test) |

#### Sensors (9 files)

| File | What it tests |
|------|---------------|
| `test_thermocouple_single.py` | Single thermocouple read, range -40 to 125C |
| `test_thermocouple_multiple.py` | Multi-channel thermocouple, cross-channel delta |
| `test_thermocouple_monitor.py` | Continuous thermocouple monitoring, stability |
| `test_watt_meter.py` | Watt meter per-sample type + range |
| `test_watt_profile.py` | Watt profile: min/mean/max validation |
| `test_sensors_comprehensive.py` | Multi-sensor enable/disable lifecycle |
| `test_energy_analyzer.py` | Energy analysis: duration, Wh/J cross-check |
| `test_energy_stats.py` | Energy statistics: per-section min/mean/max/std |
| `test_joulescope.py` | Joulescope driver (254 assertions) |

#### Peripherals (9 files)

| File | What it tests |
|------|---------------|
| `test_scope_basic.py` | Scope enable/start/stop/disable lifecycle |
| `test_scope_measurements.py` | Scope measurements (freq, Vpp, Vrms) |
| `test_scope_multichannel.py` | Multi-channel scope operations |
| `test_scope_scales.py` | Scope scale/timebase configuration |
| `test_scope_trigger.py` | Scope trigger edge/pulse/protocol |
| `test_arm_comprehensive.py` | Robotic arm position, move, home, enable/disable |
| `test_webcam_comprehensive.py` | Webcam start/stop, URL, active state |
| `test_rotation_encoder.py` | Rotation encoder position reads |
| `test_actuate.py` | Linear actuator control |

#### USB (7 files)

| File | What it tests |
|------|---------------|
| `test_usb_comprehensive.py` | USB hub port list, per-port cycle |
| `test_usb_enable_module.py` | USB enable via Net API |
| `test_usb_multiple.py` | Multi-port disable/enable |
| `test_usb_net_api.py` | USB Net.get API |
| `test_usb_power_cycle.py` | USB port power cycle timing |
| `test_usb_stress.py` | USB rapid toggle stress test |
| `test_usb_toggle.py` | USB double-toggle operation |

#### Utility (2 files)

| File | What it tests |
|------|---------------|
| `test_custom_binaries.py` | Binary listing, not-found error handling |
| `test_list_nets.py` | Net listing, name/role key validation |

### Bash Integration Tests (`test/integration/`)

#### Power (6 files)

| File | What it tests |
|------|---------------|
| `supply.sh` | Supply voltage, current, enable, OVP, OCP |
| `battery.sh` | Battery SOC, VOC, mode, capacity, enable |
| `solar.sh` | Solar set, stop, irradiance, resistance |
| `eload.sh` | ELoad CC, CV, CR, CP modes |
| `keysight_supply.sh` | Keysight supply variant |
| `multichannel_supply.sh` | Multi-channel supply variant |

#### Communication (14 files)

| File | What it tests |
|------|---------------|
| `i2c.sh` | I2C scan, read, write, config |
| `i2c_aardvark.sh` | Aardvark I2C backend |
| `i2c_labjack.sh` | LabJack I2C backend |
| `i2c_ft232h.sh` | FT232H I2C backend |
| `spi.sh` | SPI transfer, read, write, config |
| `spi_aardvark_auto.sh` | Aardvark SPI auto-CS |
| `spi_aardvark_manual.sh` | Aardvark SPI manual CS |
| `spi_labjack_auto.sh` | LabJack SPI auto-CS |
| `spi_labjack_manual.sh` | LabJack SPI manual CS |
| `spi_ft232h.sh` | FT232H SPI backend |
| `uart.sh` | UART loopback, baud rates |
| `wifi.sh` | WiFi scan, connect, status |
| `debug.sh` | J-Link flash, reset, erase |
| `jlink_script.sh` | J-Link script execution |

#### I/O (4 files)

| File | What it tests |
|------|---------------|
| `labjack.sh` | LabJack ADC, DAC, GPIO |
| `gpio_aardvark.sh` | Aardvark GPIO |
| `gpio_aardvark_loopback.sh` | Aardvark GPIO loopback |
| `gpio_ft232h.sh` | FT232H GPIO |

#### USB (3 files)

| File | What it tests |
|------|---------------|
| `usb.sh` | USB hub enable, disable, toggle |
| `ykush.sh` | Ykush USB hub |
| `acroname.sh` | Acroname USB hub |

#### Other (9 files)

| File | What it tests |
|------|---------------|
| `measurement/logic.sh` | Logic analyzer measurements, triggers |
| `sensors/thermocouple.sh` | Thermocouple reads |
| `peripherals/arm.sh` | Robotic arm position, move, home |
| `infrastructure/generic.sh` | Generic box operations |
| `infrastructure/boxes_config.sh` | Box add, delete, list |
| `infrastructure/nets.sh` | Net add, delete, rename |
| `infrastructure/deployment.sh` | Deployment scripts |
| `infrastructure/devenv.sh` | Development environment setup |
| `infrastructure/python.sh` | Python command execution |

### MCP Tests (`test/mcp/`)

#### Unit Tests (25 files, ~384 tests)

| File | What it tests |
|------|---------------|
| `test_tool_registration.py` | Module imports, tool count >= 160, name uniqueness |
| `test_run_lager.py` | Subprocess wrapper error handling |
| `test_power_tools.py` | Supply voltage, current, enable, OVP/OCP |
| `test_battery_tools.py` | Battery SOC, VOC, mode, capacity, OVP/OCP |
| `test_solar_tools.py` | Solar set, stop, irradiance, resistance |
| `test_eload_tools.py` | ELoad CC, CV, CR, CP |
| `test_measurement_tools.py` | ADC, DAC, GPI, GPO, thermocouple, watt |
| `test_scope_tools.py` | Scope enable, measure, trigger, stream |
| `test_logic_tools.py` | Logic analyzer enable, measure |
| `test_i2c_tools.py` | I2C scan, read, write, config |
| `test_spi_tools.py` | SPI transfer, read, write, config |
| `test_uart_tools.py` | UART list_nets, serial_port |
| `test_ble_tools.py` | BLE scan, info, connect, disconnect |
| `test_blufi_tools.py` | BluFi scan, connect, provision |
| `test_wifi_tools.py` | WiFi status, scan, connect, delete |
| `test_usb_tools.py` | USB enable, disable, toggle |
| `test_debug_tools.py` | Debug flash, reset, erase, gdbserver |
| `test_arm_tools.py` | Arm position, move, home, enable |
| `test_webcam_tools.py` | Webcam start, stop, URL |
| `test_box_tools.py` | Status, hello, list_nets, boxes, nets |
| `test_defaults_tools.py` | Defaults show, set, delete |
| `test_python_tools.py` | Python run, kill |
| `test_pip_tools.py` | Pip list, install, uninstall, apply |
| `test_binaries_tools.py` | Binaries list, add, remove |
| `test_logs_tools.py` | Logs clean, size, docker |

#### Integration Tests (11 files, ~64 tests)

| File | What it tests |
|------|---------------|
| `test_server_lifecycle.py` | Tool count >= 160, unique names, callable |
| `test_box_live.py` | Hello greeting, net listing |
| `test_power_live.py` | Supply voltage set/read, enable/disable |
| `test_battery_live.py` | Battery SOC, VOC, OVP/OCP, clear |
| `test_solar_live.py` | Solar set, stop, irradiance, temperature |
| `test_eload_live.py` | ELoad CC, CV, CR, CP with safety teardown |
| `test_i2c_live.py` | I2C list_nets, scan |
| `test_spi_live.py` | SPI list_nets, config, transfer |
| `test_usb_live.py` | USB enable, disable, toggle |
| `test_measurement_live.py` | ADC read, DAC write, GPO set |
| `test_defaults_live.py` | Defaults set, show, delete |

### Other Tests

#### Unit Tests (`test/unit/` -- 3 files)

| File | What it tests |
|------|---------------|
| `blufi/test_blufi_unit.py` | BluFi protocol parsing (696-line pytest suite) |
| `cli/test_keithley_locking.py` | VISA lock state diagnostic |
| `cli/test_performance_improvements.py` | Config caching, connection pooling |

#### Test Framework (`test/framework/` -- 4 files)

| File | What it provides |
|------|------------------|
| `harness.sh` | Bash test framework: `init_harness`, `track_test`, `print_summary` |
| `colors.sh` | Terminal color utilities for test output |
| `fixtures.py` | Reusable pytest fixtures with hardware auto-cleanup |
| `test_utils.py` | Python helpers: cache, connectivity, formatting |

## How to Run

```bash
# Python API tests (on real hardware)
lager python test/api/power/test_supply_comprehensive.py --box <YOUR-BOX>
lager python test/api/communication/test_i2c_aardvark_api.py --box <YOUR-BOX>

# Bash integration tests (from host)
./test/integration/power/supply.sh <BOX> <NET>
./test/integration/communication/i2c.sh

# MCP unit tests (no hardware)
python -m pytest test/mcp/unit/ -v --import-mode=importlib -c /dev/null

# MCP integration tests (requires hardware)
python -m pytest test/mcp/integration/ -v --import-mode=importlib -c /dev/null \
    --box1 <YOUR-BOX> --box3 <YOUR-BOX>

```

<!-- Copyright 2024-2026 Lager Data LLC -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

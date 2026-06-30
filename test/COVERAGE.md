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
| **Debug Probe** | Segger J-Link | 1 file | `debug.sh`, `jlink_script.sh` | Yes |
| **Energy Analyzer** | Joulescope JS220 | 3 files | — | Yes |
| **Power Profiler** | Nordic PPK2 | 1 file | — | Yes |
| **Watt Meter** | Yoctopuce Watt | 2 files | — | Yes |
| **Thermocouple** | Phidget temperature hub | 3 files | `thermocouple.sh` | Yes |
| **Webcam** | Logitech BRIO / C930e | 1 file | — | Yes |
| **Robotic Arm** | Rotrics Dexarm | 1 file | `arm.sh` | Yes |

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
- **Power management**: Supply, Battery, Solar, and ELoad all have full 3-suite coverage with tolerance checks, boundary tests, and safety teardown. Power supply has device-specific suites for the Rigol DP821 (`test_supply_Rigol_DP821.py`: live measurements, output modes, voltage sweeps, measurement stability, per-channel OVP/OCP) and the Keithley 2281S (`test_supply_Keithley_2281S.py`: setpoint accuracy, power consistency, embedded voltages, protection limits, monitor state; `test_battery_Keithley_2281S.py`: mode switching, SOC/VOC/capacity/ESR parameters, terminal voltage, current/ESR measurements, protection lifecycle).
- **I/O domain**: 18 Python API tests covering ADC, DAC, GPIO, and PWM with real value assertions and safety teardown. `test_LabJack_T7.py` is a comprehensive 11-group suite with env var configuration, preflight check, DAC boundary enforcement, stability analysis, optional loopback, and rapid stress testing. 3 FT232H/Aardvark API tests are gold standard with 100+ assertions each.
- **MCP server**: 384 unit tests (mocked, no hardware) plus 64+ integration tests covering 165+ tools across 25 unit and 11 integration test files.

## Test File Inventory

```
test/
├── api/                  # Python API tests (79 files, run on box via `lager python`)
│   ├── communication/    # 28 files: I2C, SPI, UART, BLE, BluFi, WiFi, debug
│   ├── io/               # 17 files: ADC, DAC, GPIO, PWM, pin conflict, USB-202
│   ├── peripherals/      # 9 files: scope, arm, webcam, rotation, actuate
│   ├── power/            # 7 files: supply (3 files), battery (2 files), solar, eload
│   ├── sensors/          # 9 files: thermocouple, watt, energy, joulescope, PPK2
│   ├── usb/              # 7 files: USB hub enable/disable/toggle/stress, Acroname
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
│   ├── unit/             # 8 test files: mocked, no hardware
│   └── integration/      # 1 test file: live hardware required
├── unit/                 # Local unit tests (72 files)
│   ├── box/              # 42 files: box-side Python unit tests
│   ├── blufi/            # 1 file: BluFi protocol unit tests
│   ├── cli/              # 24 files: CLI Python unit tests
│   └── measurement/      # 1 file: PPK2 watt/energy unit tests
└── framework/            # Test utilities
    ├── harness.sh        # Bash test framework
    ├── colors.sh         # Bash color utilities
    ├── fixtures.py       # Pytest fixtures with auto-cleanup
    └── test_utils.py     # Python test helpers
```

### Python API Tests (`test/api/`)

#### Power (7 files)

| File | What it tests |
|------|---------------|
| `test_supply_comprehensive.py` | Voltage/current set, readback, enable/disable, OVP/OCP, limits |
| `test_supply_Rigol_DP821.py` | Live measurements, output mode, voltage sweep across embedded rail voltages (channel-filtered), measurement stability, OVP/OCP state management, rapid cycling; channel limits configurable via `CHANNEL_MAX_VOLTAGE` / `CHANNEL_MAX_CURRENT` env vars |
| `test_supply_Keithley_2281S.py` | Keithley 2281S as power supply: live measurements, setpoint vs. measured accuracy, power consistency, output state, output mode, embedded voltages, measurement stability, current limit readback, protection limits, rapid cycling, channel limits, monitor state |
| `test_battery_Keithley_2281S.py` | Keithley 2281S as battery simulator: mode entry, static/dynamic mode, SOC/VOC/voltage-full-empty/capacity/ESR/battery-model parameters, enable/disable, terminal voltage, current/ESR measurement, protection limits+clearing, monitor state, print_state, rapid cycling |
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
| `test_wifi_comprehensive.py` | WiFi scan, connect, status, delete |
| `test_wifi_new_methods.py` | Standalone WiFi functions: scan_wifi, connect_to_wifi, get_wifi_status, disconnect_wifi; validates status.py bugfix (interface_interface → current_interface) |

#### I/O (17 files)

| File | What it tests |
|------|---------------|
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
| `test_LabJack_T7.py` | Comprehensive LabJack T7 suite: 11 groups — ADC (single, multi-channel, stability), DAC (output/readback, ramp, boundary enforcement), GPIO (output, input, pulse), optional DAC→ADC loopback, rapid stress |
| `test_usb202.py` | MCC USB-202 DAQ: ADC reads on 8 channels (±10V range), DAC output sweep on 2 channels (0-5V), GPIO output/readback on 8 digital I/O pins; optional cross-instrument accuracy tests (supply-driven ADC, LabJack-verified DAC output, GPIO loopback) enabled via env vars |

#### Sensors (9 files)

| File | What it tests |
|------|---------------|
| `test_thermocouple_single.py` | Single thermocouple read, range -40 to 125C |
| `test_thermocouple_multiple.py` | Multi-channel thermocouple, cross-channel delta |
| `test_thermocouple_monitor.py` | Continuous thermocouple monitoring, stability |
| `test_watt_profile.py` | Watt profile: min/mean/max validation |
| `test_sensors_comprehensive.py` | Multi-sensor enable/disable lifecycle |
| `test_energy_analyzer.py` | Energy analysis: duration, Wh/J cross-check |
| `test_energy_stats.py` | Energy statistics: per-section min/mean/max/std |
| `test_joulescope.py` | Joulescope driver (254 assertions) |
| `test_ppk2.py` | Nordic PPK2: WattMeter read/read_current/read_voltage/read_all, EnergyAnalyzer read_energy/read_stats (keys, types, ordering, cross-method consistency, stability) |

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
| `test_Acroname.py` | Acroname USB hub: Net.get() API, get_config() structure, string repr, enable/disable, toggle, power cycle timing, rapid cycling, multi-port control |
| `test_usb_comprehensive.py` | USB hub port list, per-port cycle |
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

#### Unit Tests (8 files)

| File | What it tests |
|------|---------------|
| `test_tool_registration.py` | MCP server registers exactly the expected discovery/planning tools (7 tools total, no I/O) |
| `test_bench_loader.py` | Bench loader: raw net descriptors → typed network objects (power supplies, SPI, ADC, etc.) |
| `test_box_tools.py` | box_manage MCP tool: health checks and reload operations |
| `test_capability_graph.py` | Capability graph builder: constructs available test capabilities from bench resources |
| `test_dut_context.py` | DUT context: schema types, net metadata loading, DUT slot parsing, context-aware tools |
| `test_heuristic_engine.py` | Heuristic engine: requirement inference and suitability assessment of test capabilities |
| `test_schemas.py` | MCP schema model validation (BenchDefinition, NetDescriptor, CapabilityGraph, safety constraints) |
| `test_server_state_reload.py` | Auto-reload of bench state when bench.json or saved_nets.json change (mtime-based detection) |

#### Integration Tests (1 file)

| File | What it tests |
|------|---------------|
| `test_agent_loop.py` | End-to-end agent workflow: discovery → suitability → lager python execution → verify |

### Other Tests

#### Unit Tests (`test/unit/` -- 66 files)

##### Box Unit Tests (`test/unit/box/` -- 42 files)

| File | What it tests |
|------|---------------|
| `test_authorize_key_rate_limit.py` | SSH /authorize-key fixed-window rate limiter: per-IP counting and pruning |
| `test_box_authorize.py` | `lager authorize` command and SSH key provisioning with TTY passthrough |
| `test_box_config.py` | box_config v1 schema validation rules and idempotency hash |
| `test_box_config_addverb_idempotency.py` | mount-add/apt-add/udev-add upsert behavior for provisioning re-runs |
| `test_box_config_cli.py` | `lager box config` CLI: mount prep, readiness polling, rollback on bounce failure |
| `test_box_dut_cli.py` | `lager box dut` CLI detached-list regression fix |
| `test_box_http_server_capabilities.py` | /status capabilities block advertises netCommand based on route registration |
| `test_breakpoint_pause.py` | `lager.pause()` interactive breakpoint: timeout handling and resume signaling |
| `test_custom_devices_impl.py` | Custom-device backend for `lager nets assign` list/assign/remove operations |
| `test_custom_store.py` | Custom-device JSON persistence: USB cable → catalog instrument mapping |
| `test_da1469x_loader.py` | DA1469x ELF symbol reading, loader path resolution, flash/erase/timeout paths |
| `test_debug_defmt_rtt.py` | Defmt RTT decoding wrapper threading and piping logic |
| `test_debug_net_self_heal.py` | DebugNet self-heal retry and session endpoints |
| `test_debug_net_user_scripts.py` | User-script/slot helpers: OpenOCD/J-Link base64 fields and serial in debug_net.py |
| `test_debug_rtt_reconnect.py` | J-Link RTT reader reconnect-aware socket handling across J-Link restart |
| `test_detect_and_configure_rtt.py` | RTT control-block RAM scan doesn't leave core halted in all-stop mode |
| `test_device_lock.py` | Cross-process advisory fcntl lock preventing USB-TMC pyvisa race |
| `test_diagnose_jlink_parse.py` | Box-side J-Link diagnose parsers: `_parse_emu_list`, `_serial_in_emu_list`, `_parse_connect_output`; pinned with captured JLinkExe text, no hardware |
| `test_gdb_controller_leak.py` | GdbController close on failed attempts to prevent fd leak |
| `test_hardware_service_retry.py` | Close-then-recreate retry path for concurrent Keithley resource collisions |
| `test_host_ops.py` | apt_install and sysctl_apply SSH execution branches |
| `test_jlink_commander_use_poll.py` | JLinkExe spawned with use_poll=True to avoid fd >= 1024 select() failure |
| `test_jlink_multi.py` | Multi-probe start_jlink_gdbserver with per-probe serial/port/RTT configuration |
| `test_jlink_multi_gdbserver_select.py` | Multi-probe GDB slot dispatch |
| `test_jlink_memrd_reset_halt.py` | DA1469x reset+halt-before-read in `jlink.py`: gate tests for r/h-before-mem8, non-DA1469x regression guard, env-var opt-out, `reset_halt=` override |
| `test_jlink_uncached_verify.py` | DA1469x opt-in uncached QSPI post-program verify to detect false XIP failures |
| `test_lock_state.py` | lock_state.py single source of truth for box-side lock behavior |
| `test_monitor_state.py` | SupplyNet/KeithleyBattery single-call monitor-state helpers reducing per-device lock contention |
| `test_mount_prep.py` | Mount preparation SSH operations via mocked runner |
| `test_nets_display.py` | `lager nets` table no-truncation for long UART pins and VISA addresses |
| `test_net_command_handler.py` | Generic POST /net/command Flask handler dispatch by role and error handling |
| `test_openocd_dispatch.py` | OpenOCD interface .cfg dispatch and user-cfg override behavior |
| `test_probes_visa_parsing.py` | VISA address parsing for empty-serial FTDI probes |
| `test_python_service_breakpoint.py` | Breakpoint endpoints on box python/service.py POST routes |
| `test_python_service_nets_list.py` | GET /nets/list handler returning saved net array or empty on missing/invalid JSON |
| `test_query_instruments_custom.py` | Custom-device surfacing in query_instruments.py cable assignment |
| `test_render_docker_args.py` | Sourceable bash output preserves docker-run args through array expansion |
| `test_render_packages.py` | pip/cargo/npm renderers preserve only their own config fields and soft-fail gracefully |
| `test_serial_id_cables.py` | tty enumeration and resolution via fake /sys tree lookup |
| `test_ssh_runner.py` | SSH key selection and auth fallback logic |
| `test_usb_scanner_custom.py` | Custom-device surfacing in box HTTP scanner GET /instruments/list |
| `test_usb_scanner_uart_fallback.py` | UART enumeration without USB serial by matching sysfs path |

##### CLI Unit Tests (`test/unit/cli/` -- 24 files)

| File | What it tests |
|------|---------------|
| `test_address_utils.py` | IPv4/IPv6/Tailscale/hostname validation rejecting schemes, ports, and paths |
| `test_battery_tui.py` | BatteryTUI render output, command parsing, and worker thread offloading |
| `test_box_lock_helpers.py` | Lock holder resolution, acquire/release/heartbeat, and format_lock_user CI support |
| `test_devenv_config_commands.py` | `lager devenv mount` and `lager devenv env` subcommands: editing project-local `.lager` volumes/environment keys |
| `test_devenv_terminal_docker_args.py` | `docker run` args for `lager devenv terminal` and `lager exec`: `-v`/`-e`/`--passenv`, `.lager` config keys, user:group handling; regression for `--group` bare-flag bug |
| `test_diagnose_classify.py` | `lager diagnose` classification decision tree for one-line user diagnosis |
| `test_diagnose_classify_jlink.py` | `lager diagnose` J-Link classification: turns `/diagnose/usb` + `/diagnose/jlink` payloads into user-actionable one-line diagnosis (sibling of `test_diagnose_classify.py`) |
| `test_error_mapping.py` | map_system_error errno mapping [16/19/110] to actionable headlines and actions |
| `test_nets_add_labjack_pins.py` | LabJack I2C/SPI arbitrary pin selection via --sda/--scl/--cs/--sck/--mosi/--miso |
| `test_nets_add_roles.py` | Role-token normalization converting legacy supply/batt to power-supply/battery |
| `test_nets_assign.py` | `lager nets assign` flow with custom-device backend and net creation |
| `test_nets_debug_scripts.py` | Smart `lager nets set-script` auto-detection and probe/file reconciliation |
| `test_net_tui_assign.py` | Custom-device assignment TUI helpers (_assign_payload, _cable_ident, _run_custom_devices) |
| `test_net_tui_labjack_pins.py` | TUI LabJack pin dialog preserving legacy channels or persisting custom params |
| `test_net_tui_uart_guard.py` | UART net save validation rejecting bare interface indices and empty pins |
| `test_nets_tui_startup.py` | Nets TUI startup regressions: tree-building with mixed net types, empty-state rendering, unsaved-placeholder rendering |
| `test_performance_improvements.py` | Config caching, connection pooling |
| `test_python_auto_lock.py` | `lager python` auto-lock wrapper idempotency, atexit, and heartbeat thread |
| `test_python_breakpoint_session.py` | Breakpoint client request shapes for continue_python/breakpoint_status endpoints |
| `test_ssh.py` | SSH ensure_lager_box_keypair and key_auth_works helpers |
| `test_supply_tui.py` | SupplyTUI render output, command parsing, worker thread offloading, connection failure |
| `test_update_probe.py` | `lager box update` probe script modprobe/usbtmc detection and output parsing |
| `test_version_skew.py` | Version skew warning when CLI minor > box minor with per-process caching |
| `test_ws_diagnose.py` | WebSocket failure message generation pointing to instrument vs. box based on health |

##### BluFi Unit Tests (`test/unit/blufi/` -- 1 file)

| File | What it tests |
|------|---------------|
| `blufi/test_blufi_unit.py` | BluFi protocol parsing (696-line pytest suite) |

##### Measurement Unit Tests (`test/unit/measurement/` -- 1 file)

| File | What it tests |
|------|---------------|
| `measurement/test_ppk2_unit.py` | PPK2 pure-logic: _parse_location, dispatcher routing, singleton caching, read math (current/voltage/power/raw), energy calculations, error handling — no hardware |

##### Root Unit Tests (`test/unit/` -- 4 files)

| File | What it tests |
|------|---------------|
| `test_group_usage.py` | Usage-line formatting for CLI command groups (CommandFirstUsageMixin / LagerGroup) |
| `test_install_wheel.py` | install-wheel command: wheel filename → package name parsing |
| `test_pdf_pages.py` | pdf_pages.py helper: PNG and text extraction from PDF pages (pymupdf) |
| `test_update_version_ref.py` | Version reference resolution for git checkouts (semver tags vs. named branches) |

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

<!-- Copyright 2024-2026 Lager Data -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

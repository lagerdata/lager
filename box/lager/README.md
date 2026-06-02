# Box Python

Python libraries and services that run on Lager box hardware to interface with test equipment and embedded devices.

## Overview

This component provides the core Python functionality for box devices, including hardware control, communication protocols, and test instrument interfaces. It serves as the execution environment for CLI implementation scripts and provides direct hardware access.

## Key Modules

### I/O (io/)
- **`io/adc/`** - Analog-to-digital converter control (LabJack T7)
- **`io/dac/`** - Digital-to-analog converter control (LabJack T7)
- **`io/gpio/`** - General-purpose I/O control (LabJack T7)

### Power (power/)
- **`power/supply/`** - Power supply control (Keithley, Keysight E36200/E36300, Rigol DP800)
- **`power/battery/`** - Battery emulation (Keithley)
- **`power/solar/`** - Solar panel simulation (EA power supplies)
- **`power/eload/`** - Electronic load control

### Measurement (measurement/)
- **`measurement/scope/`** - Oscilloscope control (Rigol MSO5000)
- **`measurement/thermocouple/`** - Temperature measurement (Phidget sensors)
- **`measurement/watt/`** - Power measurement (Yoctopuce watt meters)

### Communication Protocols (protocols/)
- **`protocols/i2c/`** - I2C communication (Aardvark, LabJack T7)
- **`protocols/spi/`** - SPI communication (LabJack T7)
- **`protocols/uart/`** - Serial/UART communication
- **`protocols/ble/`** - Bluetooth Low Energy client
- **`protocols/wifi/`** - WiFi management and provisioning

### Automation (automation/)
- **`automation/arm/`** - Robotic arm control (Rotrics)
- **`automation/usb_hub/`** - USB hub management (Acroname)
- **`automation/webcam/`** - Webcam capture and streaming

### Development Tools
- **`debug/`** - Embedded debugging interfaces (J-Link, OpenOCD, GDB)
- **`blufi/`** - ESP32 WiFi provisioning via BLE (BluFi protocol)

### Core Infrastructure
- **`nets/`** - Net/device/mux framework (hardware abstraction layer)
- **`instrument_wrappers/`** - Low-level instrument drivers (Keithley, Keysight, Rigol, EA)
- **`dispatchers/`** - Command routing (BaseDispatcher pattern)
- **`http_handlers/`** - Flask HTTP + WebSocket route handlers
- **`exec/`** - Remote command execution
- **`python/`** - User script executor (`lager python`)
- **`binaries/`** - Firmware binary management
- **`scripts/`** - Utility scripts

## Usage

### Running Box Services

```bash
# Run box Python services
./run.sh
```

### Docker Support

```bash
# Build box Python container
docker build -f docker/box.Dockerfile -t lager-box .
```

## Dependencies

Key Python packages are installed in the Dockerfile. See `docker/box.Dockerfile` for the complete list.

## Architecture

Box Python services:
1. Receive commands from CLI via HTTP/WebSocket APIs
2. Translate commands to hardware-specific protocols
3. Execute operations on connected test equipment
4. Stream results back to CLI

The modular dispatcher pattern (`dispatchers/`) allows dynamic hardware selection based on net configuration.

## Configuration

Box configuration is stored in:
- `/etc/lager/` - System-wide settings and saved net parameters

## TUIs are laptop-only

`lager battery <net> tui` and `lager supply <net> tui` are CLI commands
that run on the developer's laptop and connect to the box over a SocketIO
WebSocket on port 9000. They do NOT open USB instruments directly — the
WebSocket handler reuses hw_service's shared session pool so the TUI can
run concurrently with other CLI commands without competing for the USB
device.

Running TUIs **on the box itself** (e.g. by SSH'ing in and launching a
local textual app that opens pyvisa) is not supported and will likely
re-trigger the 2026-05-26 incident: a second pyvisa-py client
racing the hw_service for libusb's interface-0 claim, producing the
[Errno 16] Resource busy symptom. The 0.20.0 OS-level device_lock
helps detect this, but the right answer is: always launch TUIs from
the laptop CLI.

## Cross-process device locks

USB-TMC instrument drivers (Keithley, Rigol, Keysight) use an fcntl-based
`DeviceLockManager` (see `lager/util/device_lock.py`) to serialize the
`open_resource()` call across box-side processes. Lockfiles live under
`/tmp/lager_device_locks/` keyed by VISA address. The lock is held only
across the open itself — subsequent SCPI calls serialize via
hw_service's in-process per-address lock. EA solar/supply drivers
preserve their pre-0.20 lockdir at `/tmp/lager_ea_locks/`.

## Related Components

- **CLI** (`cli/`) - Command-line interface that communicates with this box

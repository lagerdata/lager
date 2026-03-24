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

## Related Components

- **CLI** (`cli/`) - Command-line interface that communicates with this box

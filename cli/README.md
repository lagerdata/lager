# Lager CLI

A powerful command-line interface for controlling embedded hardware, test equipment, and development boards through Lager Data box devices.

[![PyPI version](https://badge.fury.io/py/lager-cli.svg)](https://badge.fury.io/py/lager-cli)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)

## Features

### Hardware Control
- **Power Management**: Control power supplies, battery simulators, solar simulators, and electronic loads
- **I/O Operations**: ADC/DAC, GPIO, thermocouple sensors
- **Test Instruments**: Oscilloscopes, logic analyzers, multimeters, signal generators

### Embedded Development
- **Debugging**: ARM Cortex-M debugging with J-Link, CMSIS-DAP, ST-Link support
- **Firmware Flashing**: Flash firmware via debug probes
- **Serial Communication**: UART terminal with test framework integration
- **Robotics**: Robot arm control for automated testing

### Wireless & Connectivity
- **Bluetooth LE**: Scan, connect, and interact with BLE devices
- **USB**: Programmable USB hub control
- **Webcam**: Video streaming from box devices

## Installation

Install the Lager CLI using pip:

```bash
pip install lager-cli
```

Or upgrade to the latest version:

```bash
pip install -U lager-cli
```

## Quick Start

1. **Configure your box**:
   ```bash
   lager defaults set box <your-box-id>
   ```

2. **Test connectivity**:
   ```bash
   lager hello
   ```

3. **List available instruments**:
   ```bash
   lager instruments
   ```

4. **Control a power supply**:
   ```bash
   lager supply <net> voltage 3.3 --box <box-id>
   ```

## Core Commands

### Power Supply Control
```bash
# Set voltage and enable output
lager supply <net> voltage 3.3 --yes

# Set current limit
lager supply <net> current 0.5

# Check power state
lager supply <net> state
```

### ADC/DAC Operations
```bash
# Read ADC voltage
lager adc <net>

# Set DAC output
lager dac <net> 1.8
```

### Embedded Debugging
```bash
# Connect to debug probe
lager debug <net> connect --box <box>

# Flash firmware (auto-connects if needed)
lager debug <net> flash --hex firmware.hex --box <box>

# Reset and halt target
lager debug <net> reset --halt --box <box>

# Stream RTT logs
lager debug <net> rtt --box <box>

# Read memory
lager debug <net> memrd 0x08000000 256 --box <box>
```

### Oscilloscope & Logic Analyzer
```bash
# Measure frequency on scope channel
lager scope <net> measure freq

# Configure edge trigger
lager logic <net> trigger edge --slope rising --level 1.8
```

### Battery & Solar Simulation
```bash
# Set battery state of charge
lager battery <net> soc 80

# Configure solar irradiance
lager solar <net> irradiance 1000
```

### Serial Communication
```bash
# Connect to UART
lager uart --baudrate 115200

# Interactive mode with test runner
lager uart -i --test-runner unity

# Show the tty path for a UART net
lager uart <net> serial-port

# Create a UART net for an adapter without a USB serial number (store /dev path directly)
lager nets add <net> uart /dev/ttyUSB0 <label> --box <box>
# Warning: tty names can change after reboot; prefer device-serial mode when available
```

### Bluetooth LE
```bash
# Scan for BLE devices
lager ble scan --timeout 5.0

# Connect to device
lager ble connect <address>
```

## Configuration

### Box Setup

The CLI connects to boxes via:
- **Direct IP**: Using Tailscale or VPN IP addresses

Create a `.lager` file in your project directory:

```json
{
  "boxes": {
    "my-box": "box-abc123",
    "local-box": "<BOX_IP>"
  }
}
```

### Direct IP Access

For direct IP connections, ensure SSH key authentication is configured:

```bash
# Configure SSH key for a box
ssh-copy-id lagerdata@<box-ip>

# Then connect via the CLI
lager ssh --box <box-ip>
```

### Environment Variables

- `LAGER_BOX`: Default box ID or IP address
- `LAGER_DEBUG`: Enable debug output
- `LAGER_COMMAND_DATA`: Command data (used internally)

## Net Management

Lager uses "nets" to represent physical test points or signals on your PCB:

```bash
# List all configured nets
lager nets

# Create a new power supply net
lager nets add VDD_3V3 supply 1 USB0::0x1AB1::0x0E11::DP8C0000001

# Auto-discover and create all nets
lager nets add-all

# Interactive TUI for net management
lager nets tui
```

## Advanced Features

### Remote Python Execution
```bash
# Run a Python script on the box
lager python my_script.py --box <box-id>

# Run with port forwarding
lager python --port 5000:5000/tcp server.py
```

### Development Environment
```bash
# Create a development environment
lager devenv create --image python:3.10

# Open interactive terminal
lager devenv terminal
```

### Package Management
```bash
# Add a package to the box's declarative config
lager box-config pip add numpy

# Apply the change (restarts the lager container, runs pip install)
lager box-config apply
```

## Supported Hardware

### Debug Probes
- SEGGER J-Link
- ARM CMSIS-DAP
- ST-Link v2/v3
- Xilinx XDS110

### Power Supplies
- Rigol DP800 series
- Keysight E36200/E36300 series
- Keithley 2200/2280 series

### Battery Simulators
- Keithley 2281S

### Solar Simulators
- EA PSI/EL series (two-quadrant)

### Oscilloscopes
- Rigol MSO5000 series

### I/O Hardware
- LabJack T7 (ADC/DAC/GPIO)
- MCC USB-202 (ADC/DAC/GPIO)

### USB Hubs
- Acroname USBHub3+
- YKUSH

### Robotics
- Rotrics Dexarm

### Temperature
- Phidget Thermocouples

## Target Microcontrollers

Supports debugging and flashing for:
- STM32 (F0/F1/F2/F3/F4/F7/G0/G4/H7/L0/L1/L4/WB/WL series)
- Nordic nRF52/nRF91
- Atmel/Microchip SAM D/E/4S/70
- Texas Instruments CC32xx
- NXP i.MX RT, LPC54xx/55xx
- Silicon Labs EFM32
- Microchip PIC32MM

## Authentication & Access

The CLI authenticates to boxes via VPN access (Tailscale or similar). Access control is managed by your VPN permissions - if you have VPN access to a box, you can control it with the CLI.

### Prerequisites

1. **VPN Access**: Connect to your organization's VPN (Tailscale, etc.)
2. **SSH Keys**: Configure SSH key authentication for direct box access:
   ```bash
   ssh-copy-id lagerdata@<box-ip>
   ```
3. **SSH to Box**: Use the CLI to connect:
   ```bash
   lager ssh --box <box-ip-or-name>
   ```

### Verify Connectivity

```bash
# Test box connectivity
lager hello --box <box-id-or-ip>

# Check box status
lager boxes
```

## Documentation

For detailed documentation, visit: [https://docs.lagerdata.com](https://docs.lagerdata.com)

### Command Help

Every command has built-in help:

```bash
lager --help                 # Show all commands
lager supply --help          # Show supply command options
lager debug --help           # Show debug command options
```

## Examples

### Automated Test Script

```bash
#!/bin/bash

BOX="my-box"

# Configure power supply
lager supply VDD voltage 3.3 --box $BOX --yes

# Flash firmware
lager debug DEBUG_SWD flash --hex build/firmware.hex --box $BOX

# Reset and start
lager debug DEBUG_SWD reset --box $BOX

# Monitor UART output
lager uart --baudrate 115200 --test-runner unity --box $BOX

# Read sensor values
voltage=$(lager adc SENSOR_OUT --box $BOX)
temp=$(lager thermocouple TEMP1 --box $BOX)

echo "Voltage: $voltage V"
echo "Temperature: $temp °C"

# Disable power
lager supply VDD disable --box $BOX
```

### Interactive Python Control

```python
# example_test.py - Run on box with: lager python example_test.py
from lager import Net, NetType

# Access hardware through net abstraction
supply_net = Net("VDD_3V3", NetType.SUPPLY)
adc_net = Net("VOUT", NetType.ADC)

# Set power supply voltage and enable
supply_net.set_voltage(3.3)
supply_net.enable()

import time
time.sleep(0.1)

# Measure voltage
voltage = adc_net.read()
print(f"Output voltage: {voltage:.3f} V")

# Disable supply
supply_net.disable()
```

## Troubleshooting

### Connection Issues

```bash
# Test box connectivity
lager hello --box <box-id>

# Check box status
lager hello --box <box-id>
```

### Permission Errors

For Tailscale/direct IP connections, ensure SSH keys are configured:

```bash
# Set up SSH keys
ssh-copy-id lagerdata@<box-ip>

# Test SSH access
lager ssh --box <box-ip-or-name>
```

### Debug Probe Not Found

Verify J-Link GDB Server is installed on the box:

```bash
# Download J-Link to /tmp/ on your local machine
# Visit: https://www.segger.com/downloads/jlink/
# Download: JLink_Linux_V794a_x86_64.tgz to /tmp/

# Deploy box (J-Link will be installed automatically)
lager install --ip <box-ip>
```

### Authentication Issues

If you encounter connection issues:

1. **Verify VPN connection**: Ensure you're connected to the correct VPN
2. **Check SSH keys**: Verify SSH key authentication is configured
   ```bash
   ssh-copy-id lagerdata@<box-ip>
   ```
3. **Test SSH access**: Try connecting to the box
   ```bash
   lager ssh --box <box-ip-or-name>
   ```
4. **Test connectivity**: Use `lager hello` to verify the box is reachable
   ```bash
   lager hello --box <box-ip-or-name>
   ```

## Contributing

We welcome contributions! Please see [CONTRIBUTING.md](../CONTRIBUTING.md) for more information.

## Support

- **Documentation**: https://docs.lagerdata.com
- **Issues**: [GitHub Issues](https://github.com/lagerdata/lager/issues)

## License

Apache License 2.0 - Copyright (c) Lager Data

## Testing

Comprehensive test suites are available in the `test/` directory:

```bash
# Hardware-dependent tests (require instruments)
./test/integration/power/supply.sh <BOX> <NET>
./test/integration/power/battery.sh <BOX> <NET>
./test/integration/debug/debug.sh <BOX> <NET> <HEXFILE> <ELFFILE>
./test/integration/io/labjack.sh <BOX> <NET>
```

See `test/README.md` for test format and how to write new tests.

## Changelog

### Recent Updates
- Renamed test scripts for clarity (`test_*_commands.sh` → `*.sh`)
- Unified box deployment script (`setup_and_deploy_box.sh`)
- Added comprehensive test documentation (`test/README.md`)
- Enhanced debug command with RTT streaming and memory operations
- Improved error handling and validation across all commands

See full changelog in the [releases](https://github.com/lagerdata/lager/releases).

# Lager Debug Module

Consolidated debug functionality for embedded systems development. This module provides J-Link debug operations.

## Architecture

### Module Structure

```
lager/debug/
├── __init__.py          # Public API exports
├── api.py               # High-level debug operations
├── jlink.py             # J-Link commander interface
├── gdb.py               # GDB integration
├── mappings.py          # Status checking utilities
├── process.py           # Process management for debug servers
├── probes.py            # Per-probe USB-serial → slot/port/path helpers
├── service.py           # HTTP service for debug operations (port 8765)
└── README.md            # This file
```

## Multiple concurrent J-Link probes

A single Lager Box can run up to four J-Link probes concurrently — each `lager debug <net>` command targets the probe whose USB serial appears in the net's `address` field. The runtime resolves serial → slot → ports on every request, so no allocation state survives between calls.

- **Per-net routing.** Each debug net's `address` is a VISA resource string of the form `USB0::0x1366::0x<product>::<serial>::INSTR`. The `<serial>` segment selects the J-Link probe via `JLinkGDBServerCLExe -select USB=<serial>` and `JLinkExe -SelectEmuBySN <serial>`. Nets without a parseable `address` fall back to the legacy single-probe paths (`/tmp/jlink_gdbserver.pid`, bare `-select USB`).
- **Slot allocation.** Slots are assigned by sorted order of all debug-net serials in `saved_nets.json`. Slot N → GDB port `2331 + N`, RTT base port `9090 + 2*N` (two RTT channels per probe). Helpers live in `box/lager/debug/probes.py`. Slot 0 is also the legacy single-probe path.
- **Per-probe state on disk.** Each running gdbserver writes `/tmp/jlink_gdbserver_<serial>.pid` and `/tmp/jlink_gdbserver_<serial>.log`; `/tmp` is tmpfs so these vanish on container restart.
- **Port range published by Docker.** `box/start_box.sh` publishes `2331-2334:2331-2334` for GDB and `9090-9097:9090-9097` for RTT. Boxes that have been hardened with `box/lager/scripts/secure_box_firewall.sh` must re-run that script after upgrade so UFW admits the new ranges.

## Usage

### Python API

```python
from lager.debug import connect, flash_device, reset_device, disconnect

# Connect to target via J-Link
status = connect(
    interface='third-party',
    speed='4000',
    device='NRF52840_XXAA',
    transport='SWD'
)

# Flash firmware
files = (['firmware.hex'], [], [])  # (hexfiles, binfiles, elffiles)
for output in flash_device(files, verify=True):
    print(output)

# Reset device
for output in reset_device(halt=False):
    print(output)

# Disconnect
disconnect()
```

## API Reference

### Core Functions

#### `connect(interface, speed, device, transport, **kwargs)`
Connect to a debug target via J-Link.

**Parameters:**
- `interface`: Debug interface (must be 'third-party' for J-Link)
- `speed`: Interface speed in kHz or 'adaptive'
- `device`: J-Link device name (e.g., 'NRF52840_XXAA', 'STM32F4x')
- `transport`: Protocol ('SWD' or 'JTAG')
- `force`: Force connection if already connected (optional)
- `ignore_if_connected`: Return success if already connected (optional)
- `mcu`: MCU identifier for multi-device support (optional)

**Returns:** Status dictionary

#### `disconnect(mcu=None)`
Disconnect from debug target.

**Parameters:**
- `mcu`: MCU identifier (optional)

**Returns:** Status dictionary

#### `reset_device(halt=False, mcu=None)`
Reset connected device.

**Parameters:**
- `halt`: Whether to halt after reset
- `mcu`: MCU identifier (optional)

**Returns:** Generator yielding output

#### `flash_device(files, preverify=False, verify=True, run_after=False, mcu=None)`
Flash firmware to device.

**Parameters:**
- `files`: Tuple of `(hexfiles, binfiles, elffiles)`
  - `hexfiles`: List of hex file paths
  - `binfiles`: List of `(filepath, address)` tuples
  - `elffiles`: List of ELF file paths
- `preverify`: Verify before flashing (optional)
- `verify`: Verify after flashing (optional)
- `run_after`: Reset and run after flashing (optional)
- `mcu`: MCU identifier (optional)

**Returns:** Generator yielding output

#### `erase_flash(start_addr, length, mcu=None)`
Erase flash memory.

**Parameters:**
- `start_addr`: Starting address
- `length`: Number of bytes to erase
- `mcu`: MCU identifier (optional)

**Returns:** Generator yielding output

#### `chip_erase(device)`
Perform full chip erase (device-specific).

**Parameters:**
- `device`: Device name

**Returns:** Result dictionary

### Status Functions

#### `get_jlink_status()`
Check if J-Link GDB server is running.

**Returns:** Dictionary with `running`, `cmdline`, `logfile` keys

### GDB Functions

#### `get_arch(device)`
Get ARM architecture for device.

**Parameters:**
- `device`: Device name

**Returns:** Architecture string ('armv6-m', 'armv7e-m', 'armv8-m.main')

#### `get_controller(device=None, host='127.0.0.1', port=2331)`
Create configured GDB controller.

**Parameters:**
- `device`: Device name (optional, reads from environment if not provided)
- `host`: GDB server host
- `port`: GDB server port

**Returns:** GdbController instance

### Exceptions

- `DebugError` - Base exception for debug operations
- `JLinkStartError` - J-Link failed to start
- `JLinkAlreadyRunningError` - J-Link is already running
- `JLinkNotRunning` - J-Link is not running when required
- `DebuggerNotConnectedError` - GDB not connected to target

## Supported Devices

The module supports J-Link devices:

- **Nordic**: NRF52832_XXAA, NRF52840_XXAA, NRF9160_XXAA
- **STM32**: STM32F4x, STM32F7x, STM32G4x, STM32L4x, STM32H7x
- **SAMD**: AT91SAMD21G18, ATSAME54P20A
- **Renesas**: R7FA0E105, R7FA0E107
- **RP2040**: RP2040_M0_0
- **NXP**: Various i.MX RT and LPC series

## Dependencies

- `pygdbmi` - GDB/MI interface
- `pexpect` - J-Link commander interaction
- System tools: `JLinkGDBServer`, `JLinkExe`, `gdb-multiarch`

## Testing

```bash
# Run from box/lager directory
cd box/lager

# Test imports
python -c "from lager.debug import connect, disconnect, reset_device"
```

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](../../LICENSE) for details.

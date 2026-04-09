# Lager Box

Software that runs on Lager box hardware -- dedicated Linux devices that bridge CLI commands to physical test equipment, embedded targets, and lab instruments.

## Directory Layout

```
box/
  lager/                  # Python application (runs inside Docker)
    automation/           # USB hub, robotic arm, webcam drivers
    binaries/             # Firmware binary management
    blufi/                # ESP32 WiFi provisioning via BLE
    debug/                # Embedded debug interfaces (J-Link, OpenOCD)
    dispatchers/          # Command routing (BaseDispatcher pattern)
    docker/               # Dockerfile and container support files
    exec/                 # Remote command execution
    http_handlers/        # Flask HTTP + WebSocket route handlers
    instrument_wrappers/  # Low-level instrument drivers (Keithley, Keysight, Rigol, EA)
    io/                   # ADC, DAC, GPIO (LabJack T7)
    mcp/                  # MCP server for AI agent integration (port 8100)
    measurement/          # Oscilloscope, thermocouple, watt meter
    nets/                 # Net/device/mux framework (hardware abstraction)
    power/                # Power supply, battery emulator, solar sim, eload
    protocols/            # UART, BLE, WiFi, I2C, SPI
    python/               # User script executor (lager python)
    scripts/              # Utility scripts
    testing/              # On-box test helpers
  oscilloscope-daemon/    # Rust daemon for PicoScope streaming
  third_party/            # Third-party tool integration
  udev_rules/             # udev rules for USB device permissions
  start_box.sh            # Host-side script that launches the Docker container
```

## Building

The box application runs inside a Docker container. To build:

```bash
cd lager
docker build -f docker/box.Dockerfile -t lager-box .
```

The Dockerfile downloads proprietary SDKs (LabJack LJM, Acroname BrainStem) at build time from their official sources.

## Deployment

Boxes are deployed via `lager update` from the CLI. The flow is:

1. Push code to the repository
2. Run `lager update --box <name> --version <branch>` from the CLI
3. The box pulls the update, rebuilds the container, and restarts

`start_box.sh` on the host launches the Docker container with the appropriate device mounts, network configuration, and environment variables.

## Architecture

Each box runs multiple services inside a single Docker container:

| Service | Port | Purpose |
|---------|------|---------|
| Python Execution | 5000 | User script execution (`lager python`) |
| Hardware Invocation | 8080 | Device method proxy |
| **MCP Server** | **8100** | **AI agent integration (Cursor, Claude, etc.)** |
| Debug Service | 8765 | Embedded debugging (GDB, J-Link) |
| HTTP+WebSocket | 9000 | CLI hardware control (UART, supply, etc.) |

The **MCP server** (`mcp/server.py`) provides direct hardware access to AI coding agents via the Model Context Protocol. It uses the same `lager.Net` API as the CLI but executes everything on-box with no subprocess overhead. See the [main README](../README.md#mcp-server-ai-agent-integration) for agent setup instructions.

Hardware is accessed through **nets** — named references to physical connections defined in the box configuration. The dispatcher pattern routes commands to the correct driver based on the net's device type.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

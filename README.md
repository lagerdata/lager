# Lager

[![PR Gate: Unit Tests](https://github.com/lagerdata/lager/actions/workflows/unit-tests.yml/badge.svg)](https://github.com/lagerdata/lager/actions/workflows/unit-tests.yml)
[![PyPI](https://img.shields.io/pypi/v/lager-cli)](https://pypi.org/project/lager-cli/)
[![Python](https://img.shields.io/pypi/pyversions/lager-cli)](https://pypi.org/project/lager-cli/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**Hardware test automation.** Drive real instruments and embedded targets — power
supplies, oscilloscopes, debug probes, I2C/SPI/UART, USB hubs — from your laptop
or from CI, over the network.

```bash
pip install lager-cli
lager supply supply1 voltage 3.3 --box my-box
lager debug debug1 flash --hex firmware.hex --box my-box
```

---

## Why Lager

- **The bench stops being a place you have to be.** Instruments connect to a
  Linux *box* on the bench; the CLI runs anywhere and talks to it over Tailscale
  or your LAN.
- **The same commands work in CI.** Anything you can type, a pipeline can run.
  In a pipeline, pass `--yes` to commands that ask for confirmation.
- **One vocabulary across vendors.** `lager supply <net> voltage 3.3` is the same
  command whether the rail is behind a Rigol, a Keysight, a Keithley, or an EA.
- **Tests can run on the box.** Ship a Python script to the hardware with
  `lager python` and use the on-box `Net` API directly, with no network round
  trip per operation.

## Installation

Requires **Python 3.10+** on the machine running the CLI.

```bash
pip install lager-cli
```

You also need a **box**: a dedicated Linux machine on the bench, physically
connected to your instruments. To set one up:

```bash
lager install --ip <BOX_IP> --user <BOX_SSH_USER>
```

See [Setting Up a Lager Box](https://docs.lagerdata.com/source/getting-started/setting-up-a-lager-box)
for the full walkthrough.

## Quick Start

```bash
# Register a box you can reach (--user is the account you SSH in as)
lager boxes add --name my-box --ip <BOX_IP> --user <BOX_SSH_USER>

# Confirm the CLI can talk to it
lager hello --box my-box

# See what is attached, and what nets are defined
lager instruments --box my-box
lager nets --box my-box

# Drive a power rail
lager supply supply1 voltage 3.3 --box my-box
lager supply supply1 enable --box my-box
lager supply supply1 state --box my-box

# Read an ADC net
lager adc temp_sensor --box my-box

# Flash a target and open a serial console
lager debug debug1 flash --hex firmware.hex --box my-box
lager uart uart1 --baudrate 115200 --box my-box
```

A **net** is a named test point — a rail, a bus, a probe — mapped to a physical
instrument channel in the box's configuration. Commands address nets, not
instruments, which is why swapping a supply does not change your scripts.

## Python API

Scripts sent with `lager python` run *on the box* and talk to hardware directly:

```python
from lager import Net, NetType

psu = Net.get("VDD", type=NetType.PowerSupply)
psu.set_voltage(3.3)
psu.enable()

tc = Net.get("BOARD_TEMP", type=NetType.Thermocouple)
print(f"Temperature: {tc.read()} C")
```

```bash
lager python my_test.py --box my-box
```

For Rust, the [`lager-net` crate](https://docs.lagerdata.com/source/reference/rust/overview)
drives the same nets from `cargo test` in your firmware project, over the box's
HTTP API.

## How It Works

```
your laptop / CI runner
    |  lager CLI
    |  HTTP over Tailscale VPN or direct network
    v
Lager box  (dedicated Linux machine on the bench)
    |  USB / VISA / SCPI / SWD / serial
    v
instruments and targets
```

- **CLI** — a Python Click application (`pip install lager-cli`) that sends
  commands and streams results back.
- **Box** — services on bench hardware that own the instrument connections and
  expose them over HTTP and WebSocket.
- **Nets** — the naming layer that maps a test point to whatever instrument
  currently drives it.

For the detailed component and directory layout, see
[CONTRIBUTING.md](CONTRIBUTING.md) and the
[architecture guide](https://docs.lagerdata.com/source/getting-started/architecture).

## Supported Hardware

| Category | Vendors |
|----------|---------|
| Power supplies | Rigol, Keysight, EA Elektro-Automatik, Keithley |
| Battery simulators | Keithley |
| Solar simulators | EA Elektro-Automatik |
| Electronic loads | Rigol |
| Oscilloscopes and logic analyzers | Rigol, Pico Technology |
| Power and energy measurement | Yoctopuce, Joulescope, Nordic Semiconductor (PPK2) |
| Temperature | Phidgets |
| I2C, SPI, ADC, DAC, and GPIO | LabJack (T7, U3), Total Phase Aardvark, FTDI FT232H, Measurement Computing USB-202 |
| Debug probes | SEGGER J-Link, ST-Link, Raspberry Pi Debug Probe, FTDI (FT232H, FT2232H, FT4232H), CMSIS-DAP |
| USB hubs | Acroname, Yepkit YKUSH, Plugable |
| Serial adapters | Prolific, Silicon Labs, FTDI, Espressif |
| Wireless | Bluetooth Low Energy |
| Cameras | Logitech |
| Network routers | MikroTik |
| Robot arms | Rotrics |

Exact model numbers, channel counts, and the command each maps to are in the
[Supported Instruments reference](https://docs.lagerdata.com/source/supported-instruments/supported-instruments),
which is the authoritative list.

## AI Agents (MCP)

Every box runs an [MCP](https://modelcontextprotocol.io) server on port 8100. An
MCP-compatible agent uses it to discover the bench and the device under test, and
to plan hardware tests:

```json
{
  "mcpServers": {
    "lager": {
      "url": "http://<box-ip>:8100/mcp"
    }
  }
}
```

By default the server is read-only. Its tools describe the bench and plan tests
(`discover_dut`, `discover_bench`, `plan_firmware_test`, `assess_suitability` and
others). The agent runs each test with `lager python`. Two opt-in environment
variables, `LAGER_MCP_ALLOW_CONTROL` and `LAGER_MCP_ALLOW_EXEC`, add tools that
drive hardware or run commands on the box.

The MCP server performs no authentication. Keep port 8100 on a trusted network.
Do not forward it through a gateway or a public proxy. A box started with
`--no-publish` does not publish port 8100 on the host. The
[MCP reference](https://docs.lagerdata.com/source/reference/mcp/overview) tells
you how to reach the server on such a box.

## Documentation

Full documentation: **[docs.lagerdata.com](https://docs.lagerdata.com)**

- [Getting Started](https://docs.lagerdata.com/source/getting-started/overview)
- [CLI Reference](https://docs.lagerdata.com/source/reference/cli/overview)
- [Python API Reference](https://docs.lagerdata.com/source/reference/python/overview)
- [Rust API Reference](https://docs.lagerdata.com/source/reference/rust/overview)
- [AI Agents (MCP)](https://docs.lagerdata.com/source/reference/mcp/overview)
- [Supported Instruments](https://docs.lagerdata.com/source/supported-instruments/supported-instruments)
- [Troubleshooting](https://docs.lagerdata.com/source/getting-started/troubleshooting)

Release history is in [CHANGELOG.md](CHANGELOG.md). Releases before 0.40.0 are
in [docs/changelog/](docs/changelog/).

## Contributing

Bug reports, feature requests, and pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the repository layout, how to run the
test suites, and what CI checks on a PR.

Unit tests need no hardware:

```bash
export PYTHONPATH="$PWD:$PWD/box"
pytest -v --import-mode=importlib -c /dev/null --timeout=60 test/unit/cli/ cli/tests/
```

## Security

To report a vulnerability, see [SECURITY.md](SECURITY.md). Please do not open a
public issue for security reports.

## License

Apache License 2.0 — Lager Data. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

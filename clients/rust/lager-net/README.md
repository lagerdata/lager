# lager-net

An instrument-agnostic, **net-name-based** Rust client for the [Lager](https://github.com/lagerdata/lager) box — the Rust analog of the Python `lager` SDK's net API.

A binary started with `lager rust` runs inside the box container but cannot `import lager`, so it talks to the box over localhost HTTP. `lager-net` wraps that so your code names a *net*, not a driver or a SCPI command:

```rust
use lager_net::{Supply, Adc, Gpio};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    Supply::get("supply2").set_voltage(3.3)?;
    Supply::get("supply2").enable()?;

    let v = Adc::get("adc1").read()?;       // ergonomic alias for .input()
    println!("adc1 = {v:.4} V");

    Gpio::get("gpio1").set_high()?;
    Ok(())
}
```

The box infers each net's role from its saved configuration and runs the exact same code path as `lager python` (`Net.get(name, role).<method>(...)`), so behavior matches the Python SDK.

## API

Method names mirror the Python SDK, with ergonomic aliases layered on top:

| Type | Python-parity methods | Aliases |
|---|---|---|
| `Adc` | `input()` | `read()` |
| `Dac` | `output(v)`, `get_voltage()` | `set_voltage(v)`, `read()` |
| `Gpio` | `input()`, `output(level)`, `wait_for_level(level, timeout)` | `set_high()`, `set_low()`, `set(bool)`, `read()` |
| `Supply` | `set_voltage`, `set_current`, `voltage`, `current`, `power`, `enable`, `disable`, `set_ovp`, `set_ocp`, `get_ovp_limit`, `get_ocp_limit`, `is_ovp`, `is_ocp`, `clear_ovp`, `clear_ocp` | `measure()` |
| `Battery` | `set_soc`, `soc`, `set_voc`, `voc`, `terminal_voltage`, `current`, `esr`, `set_capacity`, `set_current_limit`, `enable`, `disable` | |
| `I2c` | `config`, `scan`, `read`, `write`, `write_read` | |
| `Spi` | `config`, `read`, `read_write`, `transfer`, `write` | |
| `Usb` | `enable`, `disable`, `toggle` | |

`Net::get(name).command(action, params)` is a generic escape hatch; `client::invoke(...)` reaches the low-level driver proxy for anything the role API doesn't cover.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `LAGER_BOX_HTTP` | `http://localhost:9000` | Box HTTP base URL |
| `LAGER_HARDWARE_HTTP` | `http://localhost:8080` | Low-level `/invoke` proxy base |

## Building for the box

The crate is dependency-light (`ureq` without TLS + `serde_json`) so it static-links cleanly for musl. Cross-compile and run via `lager rust`:

```bash
cargo zigbuild --release --target x86_64-unknown-linux-musl --example supply
lager rust target/x86_64-unknown-linux-musl/release/examples/supply --box PRD-1
```

(Use `aarch64-unknown-linux-musl` for an aarch64 box.)

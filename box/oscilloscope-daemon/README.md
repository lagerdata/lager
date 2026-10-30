# oscilloscope-daemon

Rust daemon that drives one oscilloscope and serves it over one WebSocket: commands as JSON
text, captures as binary LSCP frames. A box with several scopes runs one daemon per scope.

## Crates

| Crate | Contents |
| --- | --- |
| `daemon` | The `Oscilloscope` trait, the drivers, the hardware thread and the server |
| `protocol` | Wire types shared with clients, including `ScopeCapabilities` and `DriverFamily` |

## Drivers

| Scope | Module | Selected by |
| --- | --- | --- |
| PicoScope 2000 (legacy API) and modern families | `oscilloscope::pico` | Default, when `LAGER_SCOPE_SCPI` is unset |
| Rigol MSO5000 | `oscilloscope::rigol` | `LAGER_SCOPE_SCPI` set; `*IDN?` names an MSO5000. Live view from the `LAGER_SCOPE_VIDEO` capture card |
| Rigol DS1000Z (DS1054Z, DS1074Z, DS1104Z) | `oscilloscope::rigol::ds1000z` | `LAGER_SCOPE_SCPI` set; `*IDN?` names a DS1xxxZ/MSO1xxxZ. Live view from the 1200-point screen record, no capture card |

`LAGER_SCOPE_LOG` sets the log filter (default `info`).

## Checks

Run from this directory:

```bash
cargo check --workspace --all-targets --locked
cargo test --workspace --locked
cargo audit
```

# `lager nets` — Maintainer Notes

This directory implements the `lager nets …` CLI surface and the interactive
"Net Manager" TUI:

- `nets.py` — Click command group: `nets add`, `add-all`, `delete`,
  `rename`, `add-batch`, `set-script`, `remove-script`, etc. Also hosts the
  default `list` view.
  - The `set-script` / `show-script` / `remove-script` trio is
    backend-agnostic: it auto-detects J-Link vs. OpenOCD from the probe's
    VID (via the duplicated `_DEBUG_*_VIDS` tables — keep in sync with
    `box/lager/debug/probes.py`) and the file's extension/content, and
    enforces mutual exclusivity (`jlink_script` XOR `openocd_config`) on
    every save. `--backend jlink|openocd` is the explicit override when
    the auto-detection signals conflict.
- `net_tui.py` — Textual-based interactive manager (`lager nets tui`).

End-user documentation lives at
[`docs/source/reference/cli/nets.mdx`](../../../docs/source/reference/cli/nets.mdx);
this README is for people editing the Python side.

## The big picture

A **net** is a stable, user-named handle for one role on one channel of one
instrument. Net records persist on the box in `saved_nets.json` and look
roughly like:

```json
{
  "name": "supply1",
  "role": "supply",
  "instrument": "Rigol_DP811",
  "address": "TCPIP::192.168.1.100::INSTR",
  "pin": "1",
  "channel": "1"
}
```

The CLI offers nets to the user based on what the box's USB scanner sees
(`box/lager/http_handlers/usb_scanner.py`, served over `GET
:9000/instruments/list`). The scanner returns each
instrument's `channels` map (`{role: [channel1, channel2, …]}`); the CLI/TUI
turns that map into candidate nets, applies the constraint logic below,
prompts the user where needed, and saves the survivors.

## Channel & role constraints

We classify every supported instrument into one of three buckets. The buckets
have different rules, and **the rules are duplicated in both `nets.py` and
`net_tui.py`** because each surface enforces them independently — keep them
in sync.

### Bucket 1: Multi-channel instruments (default)

Instruments with physically independent outputs/inputs (e.g. Rigol DP831,
LabJack T7, Aardvark, MCC USB-202, Phidget thermocouple, Acroname USB hub).

**Rule:** at most one net per `(instrument, address, role, channel)` tuple.

Anything that's *not* listed in `_SINGLE_CHANNEL_INST` or
`_MODE_EXCLUSIVE_INST` lives here automatically.

### Bucket 2: `_SINGLE_CHANNEL_INST` — single channel, multiple modes

```python
_SINGLE_CHANNEL_INST = {
    "Keithley_2281S":   ("batt", "supply"),
    "EA_PSB_10060_60":  ("solar", "supply"),
    "EA_PSB_10080_60":  ("solar", "supply"),
}
```

These instruments have **one physical channel** that can run in one of
several firmware modes (Keithley 2281S: battery-simulator vs supply; EA PSB:
solar-array simulator vs straight supply). The role *tuple* documents which
modes are available, but the box can only be in one mode at a time.

**Rule: warn once, don't block.** A saved net on the chip no longer hides
the other role — the add flows offer it (default-unselected in the TUI)
together with `dual_role_notice()`, an informational notice explaining the
mode switch. Selecting *two* fresh roles for one chip in a single batch is
still rejected as a conflict, since that's almost never intentional.

**Why a warning is enough:** the drivers already make alternating use safe.
Every write path re-asserts its own entry mode before touching the
instrument (querying the current mode and switching only when needed, with
auto-reentry on mode errors), and reads are passive. So two saved nets on
one chip work — driving one simply ends whatever the other mode was doing,
which is exactly what the notice says.

Enforced in:
- `nets.py::create_cmd` / `create_all_cmd` — notice emitted to stderr, add proceeds
- `net_tui.py::AddScreen._get_addable_nets` — rows kept, notice attached
- `net_tui.py::AddScreen.on_button_pressed` — same-batch conflict detector
  (selected rows only; saved nets don't count)
- `net_tui.py::is_single_channel_taken` — back-compat helper, role arg
  ignored; now feeds the notice rather than hiding rows

### Bucket 3: `_MODE_EXCLUSIVE_INST` — hardware-multiplexed mode chips

```python
_MODE_EXCLUSIVE_INST = {"FTDI_FT232H"}
```

These chips have a single channel that's hardware-multiplexed across roles
(FT232H: MPSSE for SPI/I²C/GPIO/JTAG-SWD via libftdi *or* async-serial via
`ftdi_sio`, but never both at once).

**Rule: hard block.** At most one net per `(instrument, address)`. Once any
role is saved on the chip, every other role disappears from the add list
(with an `already has a net.` warning); the user must delete the existing
net to switch modes.

**Why stricter than Bucket 2?** No driver-side auto-switching exists here:
the MPSSE-vs-UART mode is fixed for the lifetime of an open (libftdi claims
the chip away from `ftdi_sio` and vice versa), so a second role's net
genuinely cannot work alongside the first — a warning would just defer the
failure to run time. We also expect the set to grow with future MPSSE-style
adapters; keeping it separate keeps the intent visible at the definition
site.

`add-all` treats Bucket 3 specially: it refuses to auto-pick a role on
behalf of the user (since there's no defensible default), and prints a
warning telling them to use `lager nets add` or the TUI to choose explicitly.

### Multi-channel FTDI debug adapters

FT2232H / FT4232H have multiple **MPSSE-capable channels** (A and B on the
2232H, A–D on the 4232H with only A/B capable of JTAG-SWD). These are *not*
mode-exclusive — each channel is independent.

To bind a debug net to a specific FTDI interface, the user appends an
`@A`/`@B`/`@C`/`@D` (or `@0`–`@3`) suffix to the device-type field. The
suffix is parsed by `box/lager/debug/probes.py::parse_device_field` and
forwarded to OpenOCD as `ftdi channel <N>`.

Dedup of debug candidates therefore keys on `(instrument, address,
@suffix)`, not just `(instrument, address)`. See `_debug_channel_suffix`
in both `nets.py` and `net_tui.py`.

UART nets on multi-channel FTDIs are distinguished by their tty path:
the scanner enumerates every `/dev/ttyUSB<N>` bound to the chip's USB
serial and emits one channel per interface.

## Adding a new instrument

1. Add the VID/PID/role-list entry to `SUPPORTED_USB` in
   `box/lager/http_handlers/usb_scanner.py` (the box-side scan — the only
   scanner; the CLI reads it over `GET :9000/instruments/list`).

2. Add the `CHANNEL_MAPS` entry in the same file. For roles where the
   user has to pick a target string (e.g. debug nets — the device name),
   use the `"DEVICE_TYPE"` placeholder; the CLI/TUI replaces it via prompt.

3. Add the instrument to `INSTRUMENT_NET_MAP` in `nets.py` so the role
   validation knows about it.

4. Decide which bucket the instrument lives in:
   - Multi-channel → nothing else to do; default rule applies.
   - Single-channel, software-switched modes → add to `_SINGLE_CHANNEL_INST`.
   - Hardware-mode-switched → add to `_MODE_EXCLUSIVE_INST`.

5. If it's a debug probe, also touch:
   - `box/lager/debug/probes.py` — add the VID to `_OPENOCD_VIDS` or
     `_JLINK_VIDS` so `resolve_backend()` picks the right backend.
   - `box/lager/debug/openocd.py::interface_config_for_address` — return
     the right `interface/*.cfg` for the VID.
   - `box/udev_rules/99-instrument.rules` — grant USB permissions inside
     the container.

   > For a one-off "let a tool open this device" permission fix (e.g.
   > `dfu-util` on a generic bootloader vid:pid), a user doesn't need a
   > release: `lager box-config udev add <vid>:<pid> [--usbtmc]` followed by
   > `lager box-config apply` installs a host udev rule to
   > `/etc/udev/rules.d/99-lager-user.rules`. The shipped
   > `99-instrument.rules` remains the place for first-class supported
   > instruments (it pairs with the `SUPPORTED_USB`/`CHANNEL_MAPS` tables
   > above); per-user device permissions live in the box config instead.

6. If it's a multi-channel FTDI debug adapter, make sure `CHANNEL_MAPS`
   advertises the right placeholders (`["DEVICE_TYPE@A", "DEVICE_TYPE@B"]`)
   so the user gets one prompt per usable interface.

## Why the rules live in two files

`cli/commands/box/nets.py` runs in the user's local Python (the CLI host).
`box/lager/...` runs inside the Docker container on the Lager Box. Both
sides need to know the supported-instrument table because:
- The CLI shows the user what nets *could* be created before contacting
  the box (so users get fast feedback).
- The box validates net records on save (so a malformed record from a stale
  CLI doesn't poison the cache).

We've accepted the duplication in exchange for the latency win and the
isolation guarantee. If you change one, change the other.

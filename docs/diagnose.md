# `lager diagnose`

`lager diagnose <net> --box <box> [--type <role>]` is a single-shot
diagnosis for a misbehaving instrument net. It collapses the manual
debug workflow used on the JUL-7 2026-05-26 incident (`lsof`, `dmesg`,
bare `pyvisa` probes, hardware-service introspection) into one CLI call
that returns an actionable classification.

Introduced in **lager 0.20.0**.

## Usage

```
lager diagnose <net> --box <box>
lager diagnose <net> --box <box> --type <role>
```

- `<net>` — name of the net to diagnose (e.g. `battery1`, `supply1`).
- `--box` — box name or IP (or use the default box).
- `--type` — optional; defaults to `auto`, which fetches the net's role
  from the box's saved nets. Use an explicit role
  (`battery | power-supply | scope | usb | adc | ...`) to override or to
  diagnose a net that isn't in saved nets.

The command queries three endpoints in parallel and prints sections for
each, followed by a one-line classification with the next step.

## Output sections

### `USB (host-side)`
From `GET /diagnose/usb?address=<visa>` on box port 9000. Reports:
- `enumerated` — does the device show up on the host's USB bus?
- `sysfs` — kernel sysfs path (e.g. `/sys/bus/usb/devices/1-4`).
- `device` — `/dev/bus/usb/BBB/DDD` path used by `lsof`/`fuser`.
- `usbtmc` — whether the `usbtmc` kernel module is currently loaded
  (and therefore racing libusb for interface 0).
- `lsof` — comma-separated `command(pid)` list of processes holding the
  USB device file.
- `dmesg tail` — last few USB / usbtmc kernel messages.

### `VISA (instrument-side)`
From `GET /diagnose/visa?address=<visa>` on box port 9000. Opens a
*fresh* `pyvisa` session and queries `*IDN?` with a 2s timeout. Skips
the open (with a clear note) if hardware-service already holds a shared
session for this address — collisions would either hang or return
garbage. Reports:
- `idn` — the IDN string if the instrument answered.
- `elapsed` — wall-clock ms.
- `error` / `error_class` — classified as `busy` | `nodev` | `timeout`
  | `other` when an open or query failed.
- `skipped` — set when hw_service holds the address.

### `Dispatcher (hw_service in-process)`
From `GET /diagnose/dispatcher?address=<visa>` on hardware-service
port 8080. Reports the in-process state for this address:
- `cached_session` — whether the shared `pyvisa` session pool has it.
- `cached_drivers` — driver instances cached against this address.
- `shared_pool` — total pool size.

## Classifications

The decision tree, in order (first match wins):

| Color | Headline | Trigger |
|---|---|---|
| red | `HOST-SIDE: usbtmc kernel module loaded` | `usb.usbtmc_loaded == True` |
| red | `HOST-SIDE: USB device claimed by multiple processes` | `visa.error_class == 'busy'` AND `len(usb.lsof) >= 2` |
| red | `HOST-SIDE: USB device busy` | `visa.error_class == 'busy'` AND only one holder |
| yellow | `TRANSIENT: device disappeared` | `visa.error_class == 'nodev'` |
| red | `INSTRUMENT WEDGED` | `visa.error_class == 'timeout'` |
| red | `NOT ENUMERATED` | `usb.enumerated == False` |
| green | `HEALTHY` (with IDN) | `visa.idn` returned |
| green | `HEALTHY (shared session)` | `visa.skipped` AND `dispatcher.cached_drivers` |
| yellow | `NOT USB-TMC` | VISA error matches vendor-SDK patterns |
| yellow | `UNCLEAR` | fallback |

## Sample sessions

### Healthy Keithley
```
$ lager diagnose battery1 --box PRD-1
lager diagnose — PRD-1 → battery1
  NetType: battery    address: USB0::0x05E6::0x2281::4518305::INSTR

== USB (host-side) ==
   enumerated:   True
   sysfs:        /sys/bus/usb/devices/1-4
   device:       /dev/bus/usb/001/033
   usb-tmc class: yes
   usbtmc kmod:  not loaded (good)
   lsof:         no holders

== VISA (instrument-side) ==
   idn:         KEITHLEY INSTRUMENTS,MODEL 2281S-20-6,4518305,01.08b
   elapsed:     429 ms

== Dispatcher (hw_service in-process) ==
   cached_session:  False
   shared_pool:     0 entry/entries

Classification: HEALTHY — IDN: KEITHLEY INSTRUMENTS,MODEL 2281S-20-6,4518305,01.08b
```

### Wedged firmware (mains-cycle required)
The case software cannot fix — surfaces clearly so the user stops
trying software-only recoveries.
```
Classification: INSTRUMENT WEDGED: device enumerates and accepts session open,
but won't respond to *IDN?. The instrument firmware is stuck — a mains-side
power-cycle of the instrument itself is required. Software can't fix this.
```

### Non-pyvisa instrument
LabJack (LJM SDK), Picoscope (Pico SDK), Acroname (BrainStem) etc. don't
go through pyvisa — `lager diagnose` recognizes this and points at the
role-specific command instead of returning misleading "UNCLEAR".
```
Classification: NOT USB-TMC: this instrument uses a vendor SDK
(LabJack/LJM, Picoscope/Pico SDK, Acroname/BrainStem, etc.), not pyvisa.
`lager diagnose` only covers USB-TMC instruments today; for this net,
check `lager <role> <netname> ...` directly.
```

## Backwards compatibility

Against a pre-0.20 box, each endpoint returns 404 and the CLI prints
`(endpoint not on this box; box may be on lager < 0.20)` for that
section. The remaining sections still run — `lager diagnose` is useful
against an older box, just less informative.

## See also

- `lager box hello` — basic box-side connectivity + version.
- `lager nets` — list saved nets and their VISA addresses.
- The 0.20.0 [CHANGELOG entry](../CHANGELOG.md) explains the JUL-7
  incident that drove this command.

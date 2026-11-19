# Lager macOS Port — Phase 0 Audit and Plan

Status: **draft for architecture review**. No implementation has started.
Audited at `v0.64.0` (`a6f7899b`) on branch `ad/macosport`.

Conventions used in this document:

- **[V]** verified by reading the code or running a command during this audit.
- **[A]** assumption or outside knowledge that has not been validated. Every macOS SDK
  availability claim is **[A]** until it is run on real hardware.

---

## 0. Summary

1. **The Box already has an instrument-level RPC seam.** Most instrument calls go through
   `POST :8080/invoke` (`box/lager/hardware_service.py`) as
   `{device, function, args, kwargs, net_info}`, issued by the `Device` proxy
   (`box/lager/nets/device.py:146`). The port can move that seam across the container
   boundary instead of inventing a new contract.
2. **That seam does not cover everything.** Four other things own hardware today: the debug
   service (`:8765`), parts of the public HTTP server (`:9000`: UART, RTT, USB hubs, BLE,
   DFU, discovery), the Rust oscilloscope daemon, and **user scripts run by `lager python`**,
   which open LabJack/FTDI/Aardvark/etc. directly in a child process inside the container.
3. **Recommendation: `lager-host` is the existing Python hardware code running natively on
   macOS, plus a native build of the Rust oscilloscope daemon**, not a Rust rewrite. Almost
   every driver is Python on top of pyvisa-py, pyserial, pyusb, pyftdi, or a vendor Python
   SDK. Rewriting them would discard the working code the brief says to keep.
4. **`lager install` has no local mode.** It requires `--box` or `--ip` and drives a remote
   Linux host over SSH. Decided: local and remote installs must both work, identically,
   for Linux and macOS targets. Three of those four cells are new (section 4.2).
5. **The Box image is `linux/amd64` only.** Apple Silicon needs a `linux/arm64` image.
6. **Port 5000 is taken on stock macOS** by the AirPlay receiver (confirmed on the audit
   machine). The CLI hardcodes `:5000` for `lager python`. This needs a decision.
7. **The Box HTTP API is unauthenticated today, by design** (`SECURITY.md`). The new
   Box-to-agent link will be authenticated; the public API keeps its current semantics.
8. **No automated hardware testing currently runs.** The CI bench named `MASTER` was
   retired on 2026-11-02 (`test/COVERAGE.md`) and all five `Bench:` workflows are disabled
   in GitHub [V]. Linux benches exist and can be used for manual verification (confirmed
   by the project owner); what is missing is the automated run.

---

## 0.1 Governing rule: do not break Linux

**Priority, set by the project owner:** the working Linux Box must not regress. Where
protecting Linux and getting macOS right conflict, Linux wins and the bug is accepted on
macOS. Every change that can affect an existing Linux Box is stated explicitly.

Rules for all phases:

1. **Additive by default.** macOS work goes in new files and directories. Existing Linux
   code is not moved, renamed, or restructured to make room for it. Duplication is
   preferred over a shared refactor.
2. **Linux behaviour is the default path.** New behaviour is reached only through an
   internal switch that is off unless the macOS installer sets it. With the switch off,
   execution follows the same code as before the change.
3. **Edits to existing files are minimal and guarded**, and each one is entered in the
   Linux impact ledger below before it is committed.
4. **Separate commits.** A commit that touches a file on the Linux runtime or install
   path contains nothing else and says so in its message.
5. **Evidence, stated honestly.** Each ledger entry records how it was checked. Unit
   suites are run before and after. Automated hardware runs are disabled, so an entry
   stays marked "not verified on Linux hardware" until it is run by hand on one of the
   existing Linux benches.
6. **Enforced.** A check compares files changed against `main` with the ledger and fails
   on any existing `box/` or `cli/` file that is changed but not listed.

### Linux impact ledger

Anticipated entries for later phases. The enforced record of what has actually been
changed is `tools/linux_impact_ledger.txt`, checked by `tools/check_linux_impact.py`.
**Phase 1 changed no existing `box/` or `cli/` file**, so that ledger is empty. Three
entries first planned for Phase 1 (`nets/device.py`, `constants.py`,
`hardware_service.py`) were avoided altogether; see section 7, Phase 1 status.

| Existing file | Change | Effect on a Linux Box | Risk | Phase |
|---|---|---|---|---|
| `box/lager/http_handlers/usb_scanner.py` | On macOS-backed boxes, ask the agent for the device list | None: sysfs scan is untouched and remains the default | Low | 2 |
| `box/lager/dispatchers/base.py` | Return a remote driver when the backend is remote | None when the switch is off | Medium: shared by every role | 3 |
| `box/lager/debug/*`, UART/RTT/hub/BLE handlers | Remote branch added beside the existing local code | None when the switch is off | Medium | 3 |
| `box/lager/docker/box.Dockerfile` | Copy `hostlink/` and `start-services-hostlink.sh` into the image; arm64 build support | The Linux image gains files it never runs. amd64 build must otherwise produce the same contents; verified by comparing installed packages before and after | Medium | 2 |
| `.github/workflows/box-image-*.yml` | Add an arm64 build | amd64 publish job unchanged | Low | 2 |
| `cli/commands/utility/install.py`, `update.py`, `uninstall.py` | No-flag means local; target OS probe before provisioning | Remote Linux: one extra read-only probe, then the same script as today. No-flag previously errored, so no working behaviour changes | Medium | 2 |
| `cli/context/session.py`, `cli/box_storage.py` | Script-service port read from the box's saved record, written at install time. No per-command probing | None: a record with no port field means 5000, with no extra network request | Low | 2 |
| `cli/deployment/scripts/setup_and_deploy_box.sh` | Needed only for Linux **local** install: route its ~99 inline `ssh` calls through one runner | **Highest Linux risk in the plan.** It rewrites the remote Linux install path | High | Last; see below |
| `box/oscilloscope-daemon` (`pico/loader.rs`, `pico/usb.rs`) | macOS library names and presence check | Linux code paths kept under `cfg(target_os = "linux")`, unchanged | Low | 3 |

Not touched by the port: `box/udev_rules/`, `box/modprobe_d/`,
`box/lager/docker/start-services.sh`, and every existing driver module. `box/start_box.sh`
is changed once, by the pigpio removal (Q5), which is separate from the port.

**Linux local install is sequenced last and on its own.** It is the one requirement that
cannot be met additively without duplicating the 2,600-line deploy script. It is done as
a separate, individually reviewed change after the macOS cells work, and it needs a real
remote Linux install run before it merges.

---

## 1. Current architecture

### 1.1 Processes inside the Box container [V]

Started by `box/lager/docker/start-services.sh`, each in a restart loop.

| Service | Entry point | Port | Hardware it owns |
|---|---|---|---|
| Python execution | `python3 -m lager.python.service` | 5000 | None itself; **spawns user scripts that open hardware directly** |
| Hardware service | `box/lager/hardware_service.py` | 8080 | Driver cache, shared pyvisa session pool, per-device and per-VISA-address locks, watchdog self-restart |
| Debug service | `python3 -m lager.debug.service` | 8765 | Spawns `JLinkGDBServerCLExe`, `JLinkExe`, `openocd`, `gdb-multiarch`; GDB/RTT/telnet ports per probe slot |
| Public HTTP + Socket.IO | `box/lager/box_http_server.py` | 9000 | UART and RTT sessions, USB hub control, BLE, BluFi, WiFi, DFU, USB discovery, scope relay |
| MCP server | `python3 -m lager.mcp` | 8100 | None directly |
| Oscilloscope daemon (Rust) | `/usr/local/bin/oscilloscope-daemon` | 8085 + Unix socket | PicoScope via `dlopen` of `libps*.so`; Rigol via `rusb`; HDMI capture via V4L2 |

Additional on-demand processes: one oscilloscope daemon per Rigol address
(`box/lager/measurement/scope/rigol_daemons.py`), one webcam streamer per stream
(`box/lager/automation/webcam/service.py:1208`).

### 1.2 Call paths [V]

```text
CLI / lager-rs ──HTTP :9000──▶ box_http_server ──Device proxy──▶ :8080 /invoke ──▶ driver ──▶ USB
                                    │
                                    ├── in-process: UART, RTT, USB hubs, BLE, DFU, discovery
                                    └── WebSocket relay ──▶ oscilloscope daemon :8085

CLI ──HTTP :8765──▶ debug service ──▶ JLink / OpenOCD subprocesses ──▶ USB
CLI ──raw TCP──▶ GDB 2331+, RTT 9090+, OpenOCD 4444+/6666+

CLI ──HTTP :5000 /python──▶ user script (child process in container)
                                    ├── Net API ──Device proxy──▶ :8080   (supplies, battery, eload, scope)
                                    └── Net API ──in-process driver──▶ USB (LabJack, FT232H, Aardvark,
                                                                           USB-202, Joulescope, PPK2,
                                                                           Phidget, DexArm)
```

The second script path is confirmed by `hardware_service.py:1022`
(`_release_direct_usb_claims`): the hardware service closes its cached drivers before a
script starts so the script's child process can claim the same USB device.

### 1.3 Existing abstraction boundaries [V]

| Boundary | Location | Use for the port |
|---|---|---|
| `Device` proxy → `/invoke` | `box/lager/nets/device.py:146`, hardcoded `http://localhost:8080` at line 179 | Primary RPC seam. Already JSON, already has structured errors (503 busy, 504 timeout, 403 safety) and lock timeouts |
| `*_hs.py` adapters | `box/lager/{adc,dac,gpio,i2c,spi,scope,arm,hsm,watt,energy,thermocouple,dmm,solar}_hs.py` | Per-role factories loaded by the hardware service; role map at `http_handlers/net_command.py:133` |
| `BaseDispatcher` | `box/lager/dispatchers/base.py:13`, `_choose_driver` | Single point where a role picks a driver class. Natural place to substitute a remote driver for script-side in-process paths |
| Debug service HTTP API | `box/lager/debug/service.py`, client `debug/service_client.py` | Already a separate process with a JSON API |
| Oscilloscope daemon protocol | `box/oscilloscope-daemon/protocol`, client `measurement/scope/daemon_client.py:90` | Already a network protocol (JSON + binary LSCP frames); URL already overridable by `LAGER_SCOPE_DATA_URL` |

### 1.4 Clients [V]

- `cli/`: Python CLI. HTTP to `:9000`, `:5000`, `:8080`, `:8765`; Socket.IO to `:9000`; raw
  TCP to debug ports; SSH only for install, update, uninstall, box-config.
- `lager-rs/`: separate repository, gitignored here. Pure HTTP client of `:9000` and
  `:8765`. No hardware access. Unaffected if the public API is preserved.

---

## 2. Hardware dependency inventory and Linux-specific code

### 2.1 Linux mechanisms that must not exist in the macOS container [V]

| Mechanism | Where |
|---|---|
| `--privileged`, `-v /dev:/dev`, `-v /sys/bus/usb`, `-v /sys/devices`, `-v /var/run/dbus`, `-v /opt/SEGGER`, `-v /opt/picoscope/lib` | `box/start_box.sh:1554-1586`; mirrored in `box/lager/box_config/config.py:49` |
| sysfs USB enumeration | `box/lager/http_handlers/usb_scanner.py:812` (`scan_usb`), `util/usb_sysfs.py`, `util/ftdi_usb.py`, `http_handlers/usb.py:180`, `http_handlers/diagnose.py:69-177`, `mcp/tools/control.py:59`, `automation/usb_hub/plugable.py:106`, Rust `pico/usb.rs:22` |
| sysfs tty and video identity | `devices/serial_id.py:45`, `protocols/uart/uart_bridge.py:133`, `devices/video_id.py:30`, Rust `rigol/video.rs:264` |
| `/dev/ttyUSB*`, `/dev/ttyACM*`, `/dev/serial/by-id`, `/dev/videoN` | `power/supply/ea.py:49-157`, `power/solar/ea.py:77-130`, `power/supply/rigol_dp700.py`, `protocols/uart/uart_net.py:45`, webcam service |
| `/proc/<pid>/...` process inspection | `debug/api.py:1188`, `debug/service.py:1112`, `debug/mappings.py:102`, `measurement/scope/rigol_daemons.py:138-210`, `exec/quiesce.py` |
| `fcntl.flock` cross-process device locks under `/tmp` | `util/device_lock.py:66`, `debug/probe_lock.py`, `measurement/thermocouple/phidget.py:67`, `lock_state.py:66` |
| udev rules, usbtmc unbind, modprobe blacklist | `box/udev_rules/99-instrument.rules`, `box/modprobe_d/blacklist-usbtmc.conf` |
| BlueZ over D-Bus | `protocols/ble/*`, `http_handlers/ble.py:406` (uses bleak BlueZ internals) |
| V4L2 | `automation/webcam/controls.py:65` (`v4l2-ctl`), Rust `v4l` crate |
| Linux WiFi tools | `protocols/wifi/*` (`iwlist`, `nmcli`, `wpa_supplicant`, `dhclient`) |
| Linux x86-64 vendor binaries | LabJack LJM x64 zip (`box.Dockerfile:122`), SEGGER `.deb`, PicoTech `.so` files |

### 2.2 Host-side install assumptions [V]

`cli/deployment/scripts/setup_and_deploy_box.sh` (2635 lines) and `box/start_box.sh` assume
apt, dpkg, systemd, sudoers.d, udevadm, ufw, `getent`, `flock`, `/proc/meminfo`,
`find -printf`, `grep -Po`, `sha256sum`, `setsid`, uid 33 ownership of `/etc/lager`, and a
hardcoded `/home/lagerdata/.ssh` mount. None of this applies to a Mac.

---

## 3. Proposed RPC boundary, protocol, and agent design

### 3.1 Agent implementation language

**Recommendation: Python-first agent, with the existing Rust oscilloscope daemon built
natively for macOS.** The brief prefers Rust but asks for the choice that maximises reuse.

| Option | Reuse | Cost | Verdict |
|---|---|---|---|
| Rust agent, drivers rewritten | Near zero | Reimplement ~25 backends and several vendor SDK bindings; re-verify each on hardware | Rejected |
| Rust front end supervising Python workers | High | Two runtimes, two IPC hops, more packaging | Possible later if a single signed Rust binary becomes a requirement |
| **Python agent reusing `box/lager` driver, dispatcher, and service code; Rust scope daemon alongside** | Highest | Bundle a Python runtime and native libraries | **Recommended** |

`lager-host` is delivered as one signed, self-contained bundle (embedded CPython, wheels,
`libusb`, `hidapi`, vendor dylibs, the scope daemon binary). Users see one binary name.

### 3.2 Process model on macOS

```text
Mac host                                              Docker Desktop / OrbStack VM
┌──────────────────────────────────────────┐          ┌───────────────────────────────────┐
│ lager-host (launchd LaunchAgent)         │          │ lager Box container (linux/arm64) │
│  ├ RPC front: auth, version, sessions    │◀──TCP────│  box_http_server :9000            │
│  ├ hardware service (existing /invoke)   │  token   │  python service  :5000            │
│  ├ debug service (JLink/OpenOCD procs)   │          │  mcp :8100                        │
│  ├ stream hub: UART, RTT, scope, webcam  │          │  hostlink client (new)            │
│  ├ discovery: IOKit/libusb/serial ports  │          │  no /dev, no /sys, no --privileged│
│  └ oscilloscope-daemon (native arm64)    │          └───────────────────────────────────┘
└──────────────────────────────────────────┘
```

The container remains the only public face of the Box, so `:9000`, `:5000`, `:8765`, and
the debug port ranges keep their current meaning. The container forwards to the agent.

### 3.3 Code layering

Maps to the four layers the brief requires.

| Layer | Contents | Platform |
|---|---|---|
| 1. RPC and session core | New `box/lager/hostlink/`: protocol, auth, version negotiation, client, server, session registry, stream framing, error mapping | Platform-independent; reusable by a future Windows agent |
| 2. Instrument logic | Existing drivers, dispatchers, `*_hs.py`, hardware service core, debug service core | Platform-independent after layer 3 is factored out |
| 3. OS hardware access | New modules under `box/lager/host/` (never a `box/lager/platform/` package: the hardware service and the HTTP server are started as scripts from `box/lager/`, so a directory of that name would shadow Python's own `platform` module on a Linux Box): USB enumeration, serial port identity, video identity, process inspection, vendor library and binary lookup, device locks | macOS implementation is new. The Linux implementation is the existing code, **left where it is** (section 0.1); consolidating it behind a common interface is deferred until after the port |
| 4. Service installation | `cli/` install path for launchd; existing SSH path for Linux | Per OS |

Backend selection is one internal switch, `LAGER_HARDWARE_BACKEND=local|remote`, set by
the installer in the container environment. Linux keeps `local`. User code never sees it.

### 3.4 Protocol

Keep HTTP/JSON for request/response and WebSocket for streams, because that is what the
existing seams already speak. Add what the brief requires and the current seam lacks.

| Requirement | Design |
|---|---|
| Version negotiation | `POST /v1/handshake` exchanges `{protocol: [min, max], agent_version, box_version, capabilities}`. Mismatch returns a structured error that `lager install`/`lager update` can act on |
| Authentication | 256-bit random token generated at install, stored mode 0600 on the host, mounted read-only into the container. Sent as a bearer header; constant-time compare. Agent listens on loopback only |
| Structured errors | Extend the existing `/invoke` error body to `{code, message, details, retryable, device_id}`. Map back to the same exception types the Linux path raises so error text is identical |
| Timeouts | Keep `lock_timeout_s` and the per-call deadline already in `hardware_service.py:95-110`; propagate the caller's deadline in a header |
| Cancellation | `POST /v1/cancel/{request_id}` plus cancel on client disconnect. Calls stuck in native code keep today's behaviour: watchdog restart of the worker |
| Concurrency and exclusivity | Existing per-device and per-address locks move to the agent unchanged. Add session leases with heartbeat so a dead container releases its devices |
| Streaming | WebSocket channels: `uart/{session}`, `rtt/{session}`, `scope/{net}` (existing LSCP frames passed through), `webcam/{stream}` |
| Batching | `POST /v1/batch` runs an ordered list of `/invoke` calls under one lock acquisition. `labjack/batch_read` already sets the precedent |
| Recovery | Agent is stateless toward the container apart from leases. Container reconnects with backoff and re-handshakes. Agent restart drops sessions; the client reopens on next use, as it does today after a hardware service self-restart |

### 3.5 What moves to the agent

| Today | On macOS |
|---|---|
| `hardware_service.py` `/invoke`, caches, locks | Runs in the agent. `Device` proxy targets the agent instead of `localhost:8080` |
| `debug/service.py` and its subprocesses | Runs in the agent. Container proxies `:8765` and relays the GDB/RTT/OpenOCD TCP ranges |
| UART, RTT, USB hub, DFU, BLE, discovery handlers in `box_http_server.py` | Handler stays in the container for the public API; the hardware half becomes an agent call or stream |
| Oscilloscope daemon(s) | Built for `aarch64-apple-darwin`, supervised by the agent; `daemon_url_for` points at the agent |
| Script-side in-process drivers | `BaseDispatcher._choose_driver` returns a remote driver proxy when the backend is `remote`. The few non-dispatcher paths (`io/labjack_handle.py`, `rotation.py`, `actuate.py`) get the same treatment |
| `_release_direct_usb_claims` hand-off | Not needed on macOS: the agent is the only owner |

### 3.6 Contract hole that cannot be closed by design

A user script that bypasses the Lager API and calls `serial.Serial(...)`, `pyvisa`, or
`usb.core` directly works on Linux today because the container has `/dev`. It cannot work
on macOS under the "container has no USB" rule. The same applies to custom driver bundles
in `/etc/lager/drivers` and to Linux customer binaries that touch hardware. This needs a
product decision (section 8, Q6).

This is not only an edge case. Direct serial access is a documented feature [V]:
`docs/source/reference/python/serial.mdx` ("Native pyserial support") shows
`serial.Serial('/dev/ttyUSB1', 115200)`, and `UARTNet.get_path()` / `connect()`
(`box/lager/protocols/uart/uart_net.py`) return a `/dev/ttyUSB0` path and a real pyserial
object. Six `test/api` scripts import `serial`, `pyftdi`, or `bleak` directly.

| Direct access | Can it be made to work on macOS? | How |
|---|---|---|
| pyserial | Yes | Agent serves each port over RFC 2217, which pyserial speaks natively **[A]**. `uart.connect()` returns that object. A shim in the container's Python maps `serial.Serial('/dev/ttyUSB1')` to the same client so hardcoded paths keep working, including baud changes and DTR/RTS |
| pyvisa | Yes | pyvisa backends are pluggable; a proxy backend in the container forwards sessions to the agent |
| Custom driver bundles (`/etc/lager/drivers`) | Yes | Run the bundle in the agent instead of the container |
| pyusb, hidapi, pyftdi, vendor SDKs called directly | Not without raw USB forwarding, which the brief rules out | Use the Lager net APIs |
| bleak called directly | No | Use the Lager BLE API |
| Linux customer binaries that open devices | No | Cannot run natively on macOS |

---

## 4. Installer and networking

### 4.1 `lager install` today [V]

- `cli/commands/utility/install.py:405`: errors unless `--box` or `--ip` is given.
- Always targets a remote host over SSH. No OS or architecture check.
- Box sources reach the target by `git clone` on the target; the wheel ships only `cli/`.
- `lager update` and `lager uninstall` are SSH-only as well.

### 4.2 Install matrix: local or remote, Linux or macOS (decided)

**Decision (Q1):** where Lager is installed and which operating system it runs on are
independent, and all four combinations must behave identically to a user: same
`lager install`, same CLI commands, same `lager python`.

- `lager install` with neither `--box` nor `--ip` installs onto the machine it is run on.
- `lager install --box/--ip` installs onto a remote machine over SSH.
- The target's operating system is detected, never specified by the user.
- `lager update` and `lager uninstall` follow the same rules.

| | Linux target | macOS target (arm64) |
|---|---|---|
| **Remote (SSH)** | Exists today | New: macOS provisioner run over SSH |
| **Local** | New: existing provisioning run on the local machine | New: macOS provisioner run locally (section 4.3) |

Windows is reserved in the dispatcher and reports "not yet supported", per the brief.

What the remote macOS cell adds, beyond the local one:

| Topic | Consequence |
|---|---|
| Target OS detection | Probe `uname -s` / `uname -m` over SSH before choosing a provisioner; today the script assumes Linux |
| SSH on the Mac | Remote Login must be enabled on the target, the same class of prerequisite as `sshd` on a Linux box |
| Non-interactive shell | `docker` is often not on `PATH` in an SSH session on macOS; the provisioner must locate it explicitly **[A]** |
| launchd from SSH | The LaunchAgent is bootstrapped into the logged-in user's GUI domain; a Mac nobody is logged into cannot run the agent or Docker Desktop/OrbStack **[A]** |
| Privacy permissions | Bluetooth and camera prompts cannot be answered over SSH; they need one click at the Mac's screen, or those features stay unavailable **[A]** |
| Port 5000 from another machine | AirPlay answers on 5000 on the remote Mac, so the CLI must identify the Lager service on a port, not merely find the port open [V for the conflict] |
| Inbound connections | Published container ports must be reachable from the LAN; the macOS application firewall may block or prompt **[A]** |
| Sleep | A Mac used as a remote box must not sleep; changing that setting needs admin rights, so the installer detects and reports it |
| Box-config host options | apt packages, sysctl, and udev rules have no macOS meaning and are rejected with a clear message |
| Agent exposure | Unchanged: loopback only. Remote users reach hardware only through the Box's public ports, as on Linux |

Structure: one target abstraction with two transports, `LocalTarget` and `SshTarget`,
and one provisioner per target OS. The command picks the transport from the flags and
the provisioner from the target's OS.

Findings that size the Linux work [V]:

- `cli/commands/utility/update.py` already passes an `ssh_runner` into its steps, so a
  local runner can be substituted.
- `cli/deployment/scripts/setup_and_deploy_box.sh` contains about 99 inline `ssh`
  invocations and 7 `scp`/`rsync` calls with no wrapper, and several steps exist only
  because the target is remote (key provisioning, connection multiplexing, ping).
  A local Linux install needs these routed through a single runner function, or the
  steps reimplemented in Python. Recommendation: introduce the runner function in the
  script first, with no behaviour change for the SSH path, then add the local runner.
- Steps that need root (`sudoers.d`, udev rules, apt, docker group) will prompt for
  `sudo` locally instead of over `ssh -t`.
- The box is registered in `~/.lager` at `127.0.0.1` after a local install.

### 4.3 macOS local path

When the local machine is a Mac, the CLI runs the macOS provisioner instead of the
Linux deploy script.

| Step | Detail |
|---|---|
| 1. Detect | `platform.machine() == "arm64"`, macOS version, `docker info` to distinguish Docker Desktop from OrbStack; actionable error if the daemon is not running |
| 2. Install agent | Download the `lager-host` bundle for this CLI version from the release, verify SHA-256 pinned in the CLI, unpack to `~/Library/Application Support/Lager/host/<version>/` |
| 3. launchd | Per-user LaunchAgent `~/Library/LaunchAgents/com.lagerdata.lager-host.plist`, `RunAtLoad` + `KeepAlive`. No admin rights |
| 4. Connectivity | Generate token and pick a free loopback port; create `/etc/lager` with one `sudo`; write `/etc/lager/host_agent.json` |
| 5. Box | Pull `ghcr.io/lagerdata/lager-box` for `linux/arm64` by digest; `docker run --restart always`, no `--privileged`, no device mounts, config directory mounted at `/etc/lager` |
| 6. Verify | Handshake from inside the container to the agent, then `lager hello` |
| 7. Register | Add the box to `~/.lager` at `127.0.0.1` |

Design notes:

- **LaunchAgent rather than LaunchDaemon.** No admin prompt, and USB, Bluetooth, and camera
  access on macOS are tied to a logged-in user session. Docker Desktop and OrbStack also
  start at login, so reboot recovery has the same precondition either way.
- **Config location (decided).** The installer creates `/etc/lager` on the Mac with one
  `sudo`, owned by the installing user, and mounts it into the container at the same
  path. No Box code changes. Whether Docker Desktop shares that path by default is still
  to be confirmed **[A]**.
- **File locks across the VM boundary.** `flock` over virtiofs between the Mac and the
  container is not something to rely on **[A]**. Each lock file gets a single owning side.
- **Launcher.** `box/start_box.sh` is GNU-specific and runs host `python3` renderers. The
  macOS path builds its `docker run` in the CLI rather than porting that script.
- **Idempotence, upgrade, uninstall.** Versioned agent directories with an atomic symlink
  switch; `lager update` replaces agent and image together and re-handshakes;
  `lager uninstall` boots out the LaunchAgent, removes the bundle, container, and image,
  and keeps config unless `--purge-config`.
- **Signing.** Developer ID signature and notarisation for release bundles; unsigned
  bundles allowed when installing from a source checkout. Bluetooth and camera access
  need usage-description strings, which implies an app-bundle layout **[A]**.
- **CI gate.** `xplat-smoke.yml` imports every `cli` module on macOS and Windows, so new
  install code must import cleanly on all three platforms.

### 4.4 Networking

| Topic | Finding or plan |
|---|---|
| Container to host | Use `host.docker.internal`. Both runtimes are expected to route it to services bound on the Mac's loopback **[A]**. Phase 1 tests this on each runtime independently before anything depends on it |
| Fallback | If loopback is not reachable on a runtime, bind the agent to that runtime's host-gateway address only. Token auth stays mandatory in both cases |
| Agent exposure | Never bound to a LAN interface. Other local users on the Mac can reach loopback, which is why the token is required |
| Unix socket alternative | Rejected: host sockets do not cross the VM file-sharing boundary reliably **[A]** |
| **Port 5000** | Held by the AirPlay receiver on the audit Mac [V]. `cli/context/session.py:490` hardcodes `:5000`. The Box already publishes `8301→5000` as a legacy alias (`start_box.sh:1426`). See Q3 |
| Other published ports | `8080` and the debug ranges stay published by the container and are relayed to the agent, so the box address means the same thing as on Linux |
| Host network mode | `box_config` `network-mode: host` is Linux-only; rejected with a clear message on macOS |
| Public API auth | Unchanged (unauthenticated, per `SECURITY.md`). Docker Desktop publishes ports on all interfaces by default **[A]**, which matches Linux behaviour |

### 4.5 Box image

`box-image-publish.yml:201` and `box-image-pr.yml:65` build `linux/amd64` only, and
`box.Dockerfile:122` downloads the x64 LabJack installer. Plan: publish a multi-arch
manifest; the arm64 variant skips or substitutes hardware SDK layers, which the macOS
container does not use. Running the amd64 image under emulation is rejected as a default
because of start-up time and build-in-container steps.

---

## 5. Instrument compatibility matrix

"Linux path" is **[V]**. "macOS outlook" is **[A]** in every row: nothing here is claimed
as supported until it passes on physical hardware.

| Backend | Linux path | macOS outlook | Move to agent | Streaming | Hardware to verify | Risk |
|---|---|---|---|---|---|---|
| Rigol DP8xx, DL3021; Keysight E36xxx; Keithley 2281S; Siglent SPD/SDM (USBTMC) | pyvisa-py → pyusb → libusb; kernel `usbtmc` unbound by udev | Expected to work; macOS has no kernel USBTMC driver to detach | `/invoke` unchanged | No | One of each family | Low |
| Same instruments over LAN | pyvisa-py TCPIP | Could run from the container directly; route through the agent anyway for one code path | `/invoke` | No | LAN-capable unit | Low |
| Rigol DP711/712, EA PSB (serial) | pyvisa ASRL on `/dev/ttyUSB*`, `/dev/serial/by-id` | Port naming and identity lookup must be reimplemented (`/dev/cu.*`) | `/invoke` + platform layer | No | DP711 + Prolific cable; EA PSB | Medium |
| UART nets | pyserial, sysfs identity, Socket.IO `/uart` | pyserial works; identity via IOKit; driver availability per chip (FTDI, CP210x, PL2303) to confirm | Stream channel | Yes | One adapter per chipset | Medium |
| LabJack T7 | `labjack-ljm` → `libLabJackM.so` | LJM has a macOS installer; arm64 and no-admin install to confirm | `/invoke` + remote dispatcher | No (batch read) | T7 | Medium |
| LabJack U3 | LabJackPython → Exodriver; ctypes `libusb-1.0.so.0` | Exodriver builds on macOS; hardcoded soname must change | Same | No | U3 | Medium |
| MCC USB-202 | `uldaq` → `libuldaq.so` built from source | Vendor lists macOS; arm64 build to confirm | Same | No | USB-202 | Medium |
| FT232H/FT2232H/FT4232H GPIO/I2C/SPI | pyftdi over libusb; sysfs staleness workaround | Apple's FTDI serial driver may hold the interface; detaching on macOS is restricted | Same | No | FT232H | **High** |
| Aardvark | `aardvark_py` bundled native lib | arm64 macOS binary to confirm | Same | No | Aardvark | Medium |
| J-Link | `JLinkGDBServerCLExe`, `JLinkExe` from `/opt/SEGGER` | SEGGER ships macOS builds; installer is a `.pkg` (admin) and licence terms affect bundling | Debug service | RTT | J-Link + DUT | Medium |
| ST-Link, CMSIS-DAP, Picoprobe, FTDI probes | Debian `openocd`, `gdb-multiarch` | Bundle OpenOCD and an Arm GDB for arm64 macOS; `gdb-multiarch` does not exist there | Debug service | RTT | ST-Link + DUT | Medium |
| PicoScope | Rust daemon `dlopen`s `libps*.so` from `/opt/picoscope/lib`; sysfs presence check | Needs macOS dylibs for arm64; loader paths, sonames, and presence check are Linux-only in code [V] | Native daemon | Yes (LSCP) | PicoScope 2000 and one `a`-API model | **High** |
| Rigol MSO5000 / DS1000Z | Rust daemon, `rusb` USBTMC or TCP 5555 | `rusb` with vendored libusb should build on macOS | Native daemon | Yes | MSO5000 or DS1000Z | Medium |
| MSO5000 live view (HDMI capture) | V4L2 via `v4l` crate, Linux-only target dependency [V] | Needs an AVFoundation implementation | Native daemon | Yes | Capture card | **High**; defer |
| Webcams | OpenCV on `/dev/videoN`, `v4l2-ctl` controls | OpenCV can use AVFoundation; controls and stable identity differ; camera permission for a background agent | Stream channel | Yes | Logitech BRIO/C930e | **High** |
| Acroname hubs | `brainstem` SDK | macOS arm64 wheel to confirm | Agent | No | USBHub3+ | Medium |
| YKUSH / YKUSH3 | `pykush` over HID | hidapi works on macOS | Agent | No | YKUSH3 | Low |
| Plugable hub | Raw pyusb hub-class control transfers; sysfs topology | Control transfers to a system-owned hub and topology mapping to confirm | Agent | No | Plugable hub | **High** |
| Joulescope JS220 | `joulescope` SDK, streaming callback | Vendor supports macOS | `/invoke`; statistics stream stays in agent | Internal | JS220 | Medium |
| Nordic PPK2 | `ppk2-api` over serial | pyserial path | `/invoke` | No | PPK2 | Low |
| Yocto-Watt | `yoctopuce` | Library ships macOS binaries | `/invoke` | No | Yocto-Watt | Low |
| Phidget thermocouple, encoder | `Phidget22` → `libphidget22` | Vendor macOS library; install method to confirm | `/invoke` | No | Phidget TC | Medium |
| YubiHSM 2 | `yubihsm[usb]` over pyusb | Expected to work | `/invoke` | No | YubiHSM 2 | Low |
| Rotrics DexArm | pyserial; handshake probe on unknown ttys | pyserial path; probe must not poke unrelated Mac serial ports | `/invoke` | No | DexArm | Medium |
| DFU | `dfu-util` subprocess | Bundle `dfu-util` | Agent | No | DFU-capable DUT | Low |
| BLE, BluFi | bleak on BlueZ via D-Bus; uses BlueZ internals | bleak has a CoreBluetooth backend, but macOS hides MAC addresses, so address-based APIs cannot behave identically; needs Bluetooth permission | Agent | Notifications | BLE peripheral | **High** |
| WiFi | Linux radio tools on the Box | Would control the Mac's own WiFi | — | — | — | Propose unsupported on macOS (Q5) |
| Asus / MikroTik routers | HTTP | No host hardware; stays in container | None | No | — | None |
| pigpio | Separate container | Not applicable on a Mac | None | — | — | Propose unsupported |

---

## 6. Conformance testing strategy

### 6.1 What exists [V]

| Suite | Count | Runs | Suitability |
|---|---|---|---|
| `test/unit/*` | ~10,000 tests | pytest, mocked, Linux CI only | Regression guard for Linux; not evidence of hardware compatibility |
| `test/integration/*` | 42 bash scripts | From the operator machine through the CLI | Best conformance base: depends only on the public CLI |
| `test/api/*` | 87 scripts | Inside the Box via `lager python` | Good conformance base and exercises the script path; `test/framework/test_utils.py:41` hardcodes `localhost:8080` and must use the public API instead |
| `lager-rs/tests/hardware.rs` | 17 ignored tests | Against a live box | Rust interface check |

### 6.2 Plan

1. **Conformance manifest.** One file listing each case with its instrument category,
   required nets, and required wiring, replacing per-script positional arguments and
   environment defaults. The same manifest drives both operating systems.
2. **Three tiers, kept separate in reporting:**
   - *Mocked*: new unit tests for `hostlink` (auth, version mismatch, error mapping,
     reconnect, cancellation) and for remote-driver substitution.
   - *Loopback agent*: the agent running with a fake instrument backend, exercising real
     container-to-host networking on Docker Desktop and on OrbStack.
   - *Physical*: the manifest run against real benches. Only this tier updates the
     supported-instrument list.
3. **Install matrix:** every lifecycle and conformance case runs in all four cells
   (Linux/macOS × local/remote).
4. **Lifecycle cases:** unplug and replug, agent kill during an operation, container
   restart, Docker restart, host reboot, concurrent CLI and script access to one device,
   repeated install and uninstall, upgrade with protocol mismatch.
5. **Performance:** `/invoke` round-trip latency, UART throughput and latency, scope
   frame rate (existing `box/oscilloscope-daemon/tests/bench` and `BASELINE.md`), RTT
   throughput, each compared between Linux and macOS.
6. **Linux regression:** existing unit suites stay green, with a recorded baseline taken
   before the first edit; the `local` backend is the default and existing files change
   only as listed in the Linux impact ledger (section 0.1).
7. **CI:** add a macOS job for agent unit tests and the loopback tier. Physical runs use
   the existing Linux benches by hand; automating them again, and adding a Mac bench, is Q7.

---

## 7. Phased, file-level implementation plan

### Phase 1 — vertical slice without physical hardware

Hardware-in-the-loop testing is deferred to Phase 2 (decided), so this phase proves the
architecture against a simulated instrument and makes no compatibility claim.

Goal: one power supply net controlled on a Mac through CLI, public HTTP API, and a
`lager python` script, with no USB in the container. The instrument is a fake SCPI
supply served over TCP on the Mac, driven by the real, unmodified supply driver through
pyvisa-py, so everything except the USB transport is exercised.

| Change | Files |
|---|---|
| New RPC core: handshake, token auth, client, error mapping | `box/lager/hostlink/` (new) |
| Backend switch and agent endpoint in the proxy | `box/lager/nets/device.py`, `box/lager/nets/constants.py`, `box/lager/constants.py` |
| Agent entry point that runs the hardware service natively behind the RPC front | `box/lager/host/` (new), `box/lager/hardware_service.py` (bind address, auth hook, config root) |
| Config root overridable | `box/lager/constants.py` and its importers |
| Minimal macOS discovery for USBTMC devices | `box/lager/host/darwin_usb.py` (new); guarded branch in `box/lager/http_handlers/usb_scanner.py`. Existing sysfs code is not moved |
| Ledger check and unit-suite baseline before any existing file is edited | `tools/check_linux_impact.py` (new) |
| arm64 dev image, hardware layers optional | `box/lager/docker/box.Dockerfile` |
| Dev launcher (not yet `lager install`) | `tools/` script to start agent and container |
| Fake SCPI supply over TCP | `test/framework/` (new) |
| Tests | `test/unit/box/test_hostlink_*.py` (new); loopback tier |

Exit criteria: supply set/read/enable from CLI and from a script on the Mac against the
fake instrument; instrument loss gives the same error text as the Linux path; agent kill
and container restart both recover; connectivity proven separately on Docker Desktop and
OrbStack. USB discovery code is written but counts as unverified until Phase 2.

#### Phase 1 status (2026-11-15)

Built and run on macOS 15.7.4 arm64 with Docker Desktop 28.1.1.

**Design change from the table above: no existing file was edited.** Instead of pointing
the `Device` proxy at the agent, the macOS container runs a relay on `localhost:8080`, the
port the hardware service normally holds. Every existing caller keeps working unchanged
and the relay forwards to the agent. The agent imports the existing hardware service and
wraps it. (It first repointed `/etc/lager` paths after import; that was replaced by using
`/etc/lager` itself on the Mac, see finding 3.)

| Piece | Files (all new) |
|---|---|
| Link core: protocol, token, endpoint record, relay | `box/lager/hostlink/` |
| Native agent: supervisor, worker, macOS USB listing | `box/lager/host/` |
| Container start script without hardware services | `box/lager/docker/start-services-hostlink.sh` |
| Simulated SPD1168X supply over TCP | `test/framework/fake_scpi_supply.py` |
| Dev launcher | `tools/macos_dev_box.sh` |
| Linux impact ledger and check | `tools/linux_impact_ledger.txt`, `tools/check_linux_impact.py` |
| Tests (47 link, 15 macOS USB enumeration, 12 network reconnect) | `test/unit/box/test_hostlink.py`, `test_host_darwin_usb.py`, `test_hardware_service_network_reconnect.py` |

Exit criteria:

| Criterion | Result |
|---|---|
| Supply set, read, enable, disable from the CLI | Pass, through the real `lager` CLI against the simulated supply |
| Same net from a script using the Net API | Pass when run inside the container. **Not run through `lager python`**: the CLI hardcodes port 5000, which AirPlay holds; that is the Phase 2 port work |
| Container has no hardware access | Pass: not privileged, no devices, no `/dev` or `/sys` mounts |
| Container reaches an agent bound to loopback | Pass on Docker Desktop via `host.docker.internal` |
| Same on OrbStack | Pass on OrbStack 29.8.2 (2026-11-16): CLI set, enable and state, a Net API script in the container, recovery from a worker crash and a container restart |
| Token and version handshake | Pass: no token gives 401, mismatch gives a "run `lager update`" error |
| Worker crash, agent stop, container restart | Pass: each recovers without manual steps; while the agent is down the CLI reports one clear error |
| Instrument-loss error text equals the Linux code path | Pass: the same sequence against the unwrapped hardware service gives identical text |
| Unit suites unchanged | Pass: box suite 5,312 passed before, 5,386 after (the 74 new tests). One test fails on macOS before and after (`test_sudoers_contract`, unrelated) |

Limits of this result:

- No USB device was involved. Nothing here is evidence of instrument compatibility.
- The container ran the published `linux/amd64` image under emulation with this
  checkout's `box/lager` mounted over it. A native arm64 image is Phase 2.
- `lager nets add` could not add the simulated supply, because it only accepts
  instruments found by the USB scan. The net was added with `lager nets add-batch`.

Findings to carry forward:

0. **Installer must not depend on the keychain.** With `"credsStore": "osxkeychain"` in
   `~/.docker/config.json`, an anonymous `docker pull` on OrbStack failed with "error
   getting credentials ... User canceled the operation". Pulling with an empty Docker
   config worked. The installer has to pull the public image without the credential
   helper, above all over SSH, where nobody can answer a keychain prompt. Installing
   OrbStack also switched the active Docker context to it, so the installer must choose
   its runtime explicitly rather than trust the current context.

1. **Fixed (Linux-affecting, in the ledger):** after a socket-connected instrument
   dropped and returned, the hardware service kept failing with "Broken pipe" until it
   was restarted. Reproduced against the unwrapped Linux code path, so the link did not
   cause it. `hardware_service.py` now treats a dropped connection on a TCPIP address as
   a dead session and reopens it. USB, serial and addressless devices do not take the
   new path. Remaining limitation: a write-only command sent as the first call after a
   drop is lost without an error, because TCP accepts the write before it learns the
   peer is gone.
2. **Fixed on the agent side, unverified on hardware:** the hardware service's
   self-restart for a wedged USB session only fires when the device is still listed in
   sysfs, which macOS lacks, so the recovery could never trigger. The agent now answers
   that question from the I/O Registry (`box/lager/host/darwin_usb.py`) by swapping one
   function on `lager.util.self_restart`; the Linux file is not edited. Two things are
   still unproven until a real instrument is unplugged and replugged: that the registry
   keys match what real devices report, and that restarting the worker actually clears a
   wedged USB session on macOS.
3. **Config directory (decided):** `lager install` on macOS creates `/etc/lager` once with
   `sudo`, owned by the installing user, so the agent and the container use the same
   path a Linux Box does. This replaces the Phase 1 approach of repointing `/etc/lager`
   paths in Box modules after import, which missed paths written inside functions. It
   costs one password prompt, which a Linux install already has.
   **Done 2026-11-16:** the repointing code is deleted and the supply slice was re-run on
   Docker Desktop 28.1.1 and OrbStack 29.8.2 with `/etc/lager` shared into the container
   (read, write and atomic rename as uid 33, directory mode 0700 on the Mac). Two things
   the installer must do, both found here:
   - Mount `/private/etc/lager`, not `/etc/lager`. `/etc` is a symlink on macOS and
     OrbStack does not follow it for a bind mount: the container silently gets an empty
     directory. Docker Desktop accepts either.
   - `chmod 700` the directory. The endpoint record is mode 0644 so the container user
     can read it, and the directory is what keeps the token from other local users.
     Neither runtime enforces file ownership across the share, so 0700 on the Mac does
     not lock the container out.
4. **Token visibility:** the endpoint record is mounted into the container, so scripts
   running there can read the link token. This matches the trust the container already
   has on Linux, where it holds the hardware directly.

#### Hardware results (2026-11-16)

First runs on real instruments, macOS 15 arm64, OrbStack. Bench: Keysight E36312A,
PicoScope 2000 (USB id 0ce9:1016) and LabJack U3 direct to the Mac; J-Link + nRF52840
behind port 0 of an Acroname USBHub3p.

| Path | Result |
|---|---|
| Keysight E36312A over USBTMC, CLI -> container -> agent | Works: state read and setpoint change. Output not yet enabled (load unknown) |
| `lager instruments` | Reports nothing: the scan still runs in the container and reads the VM's sysfs. Nets had to be added by hand |
| `lager debug flash` (J-Link, nRF52840) | Works with the debug service run natively on the Mac |
| `lager debug status` / `disconnect` | Work |
| `lager debug memrd` / `reset` | Fail: the service looks for the GDB server in `/proc/<pid>/cmdline`, which macOS lacks |
| Scope daemon build | Builds natively, unmodified, with Rust 1.97 (needs >= 1.89) |
| PicoScope open | Not working yet, see below |

What the debug service needs on macOS, all found here:
- J-Link Commander stalls at "Connecting to J-Link via USB" unless started with
  `-NoGui 1`: it waits on a dialog. A wrapper script placed first in the tool search
  path fixes it without editing `jlink.py`.
- The tool paths are Linux ones; macOS has `/Applications/SEGGER/JLink/`.
- `pygdbmi` and `pexpect` must be in the agent's Python environment.
- About seven places read `/proc/<pid>/cmdline` or `/proc/<pid>/stat`.
- The service did not stop on SIGTERM while a J-Link call was stalled.

What the scope daemon needs on macOS:
- PicoTech's macOS headers keep the shared headers (`PicoStatus.h`) in a separate
  `shared/` directory; the build expects them beside each family's header.
- The standalone PicoSDK framework is x86_64 only. The PicoScope 7 application carries
  arm64 drivers, and the loader now looks there first on Apple silicon (macOS-only code,
  compiled out on Linux). What the installer should require is undecided.
- With the drivers loaded, every family still answers "not found", and so does the
  vendor driver called directly, outside Lager. The scope's I/O Registry entry is
  `!registered, !matched`, so macOS has not finished attaching it. Unresolved; the next
  step is a physical replug.

#### Hardware results, second pass (2026-11-16)

The debug service and scope daemon now run under the agent and are reached through the
container, not directly.

| Path | Result |
|---|---|
| Keysight E36312A: voltage, current limit, enable | Works (3.3 V / 0.1 A read back as 3.299 V) |
| `lager debug` flash, memrd, reset, status, disconnect | Work through container -> host |
| PicoScope 2405A: enable, scale, timebase, start, measure | Work. 958 Hz on A and 247 Hz on D from the test firmware |
| `lager debug gdbserver --rtt --interactive \| defmt-print` | Works: decoded heartbeat lines on every attach |

How it is wired:
- The agent supervises three children: hardware worker, debug service (port 48765) and,
  when given a binary, the scope daemon (48085). All loopback.
- The container forwards 8765 and 8085 to them as plain TCP
  (`box/lager/hostlink/tcp_forward.py`). These two are not token-protected, unlike the
  hardware service.
- Debug-net calls made inside the container (the `/rtt` Socket.IO handler, and scripts
  using `Net.get(..., NetType.Debug)`) go to the agent through
  `box/lager/hostlink/debug_proxy.py`; RTT is bridged over a loopback TCP port. This is
  switched on by `LAGER_HOSTLINK=1`, set only by the macOS start script. The switch is
  one block in `box/lager/nets/debug_net.py`, in the ledger.

Found on the way:
- The PicoScope "not found" was macOS waiting for the user to allow the accessory.
- The debug service's SIGTERM handler deadlocks (it calls `server.shutdown()` from the
  serving thread). Docker hides this with its kill after ten seconds. The agent replaces
  the handler and kills any child that outlives a stop request.
- RTT detection starts `gdb-multiarch`, which macOS does not have; `arm-none-eabi-gdb`
  is substituted.
- GDB 18 took about six seconds over every connect to J-Link, so the Box code's
  non-stop fallback missed the refusal and the first RTT attach outlasted the CLI's
  wait. Cause: GDB 17 and later send `qExecAndArgs`, which J-Link's GDB server never
  answers, and GDB waits out three two-second timeouts. The agent sends
  `set remote fetch-exec-and-args-packet off` before connecting; the connect now takes
  0.2 s. This would also affect a Linux Box whose `gdb-multiarch` is 17 or newer.
- The agent's worker kept reusing a GDB whose server had since been restarted by the
  debug service, a separate process. Every read then failed, the RTT block was "not
  found" and the resume went nowhere. The agent now checks a cached GDB still has a
  thread and reconnects if not. **Suspected on Linux too, not verified:** the Box's
  `/rtt` handler and debug service are also separate processes with the same cache. It
  is hidden when the control block sits at a 4 KB-aligned address, because J-Link then
  finds it unaided.
- **RTT piped to defmt-print works:** three attaches in a row, including the first after
  a restart, decode the firmware's heartbeat lines, and the count carries on across
  attaches, so attaching does not reset the target.
- An earlier note here said the core was left halted after the RAM scan. That was
  wrong. A diagnostic command of mine (`monitor memU32 <addr> <n>`, which writes) had
  overwritten a byte of the firmware's RTT header; a reset cleared it.
- The GDB port (2331) is only reachable on the Mac itself, not through the container.
- Port 5000 was not needed by any command above.
- Unit tests for the new files: `test/unit/box/test_host_debug.py`.

#### Hardware results, third pass (2026-11-16)

| Path | Result |
|---|---|
| Acroname USBHub3p: state, enable, disable, toggle, cycle, `lager nets state` | Work through container -> host |
| Cycle confirms the device came back | Works ("device re-enumerated"), from the I/O Registry |
| J-Link unplugged and replugged by the hub | `lager debug` reconnects, also when a debug command was tried while it was off |

How it is wired: the same shape as the debug net. The container's hub dispatcher returns
a stand-in (`box/lager/hostlink/usb_proxy.py`) when `LAGER_HOSTLINK=1`; the agent runs the
real driver (`box/lager/host/usb_rpc.py`). The switch is one check in
`box/lager/automation/usb_hub/dispatcher.py`, in the ledger. Driver errors cross the link
as the same class with the same fields, so the Box's handlers answer as on Linux.

Found on the way:
- BrainStem's PyPI wheel works natively on Apple Silicon. The agent environment needs it.
- The hub drivers confirm a cycle from sysfs. The agent replaces that reader with one
  over the I/O Registry, using the location ID as the stable per-port name.
- Two toggles back to back (about one second off) once left the J-Link listed on USB but
  unopenable, by SEGGER's own tools as well. A cycle with three seconds off cleared it.
  Not reproduced with `cycle` or with two seconds off.
- **Open:** `lager nets state` reports USB instruments as "not on the USB bus", because
  that check reads sysfs inside the container.
- **Open:** YKUSH and Plugable hubs go through the same stand-in but were not on the bench.

#### `lager python` on the Mac box (2026-11-16)

- The CLI built `http://<ip>:5000` for `lager python`. It now asks
  `cli/box_storage.get_python_service_port(ip)`, which is 5000 unless the saved Box
  records `"python_port"`. Both files are in the ledger. Nothing writes the key yet: the
  macOS installer will record 8301. `lager boxes add` does not preserve it.
- With a Box record `{"ip": "127.0.0.1", "python_port": 8301}`, a script run by
  `lager python` read the Keysight, the Acroname port and target memory, read raw RTT,
  and read decoded lines with `rtt_defmt` (defmt-print runs in the container, as on Linux).
- A debug error now crosses the link as its own class: a second `connect()` raises
  `lager.debug.api.JLinkAlreadyRunningError` in the script, as on a Linux Box.

### Phase 2 — hardware in the loop, then installer

First, repeat the Phase 1 exit criteria on a physical USBTMC supply, including unplug and
replug, and fix what the simulation hid. Then the installer:

| Change | Files |
|---|---|
| Target abstraction (`LocalTarget`, `SshTarget`) and per-OS provisioner dispatch; no-flag means local | `cli/commands/utility/install.py`, `update.py`, `uninstall.py`; new `cli/deployment/target.py` |
| macOS provisioner, runnable through either transport | New `cli/deployment/macos/` |
| Target OS probe before provisioning | `cli/commands/utility/install.py`, `cli/commands/box/_ssh.py` |
| Linux local install: single runner function in the deploy script, then a local runner. **Done last, as its own change** (section 0.1) | `cli/deployment/scripts/setup_and_deploy_box.sh`, `cli/commands/utility/update.py` |
| Windows local install: recognised, reports not yet supported | `cli/commands/utility/install.py` |
| launchd plist, bundle fetch and verification | `cli/deployment/macos/` |
| Container launch without `start_box.sh` | `cli/deployment/macos/` |
| Multi-arch image publish | `.github/workflows/box-image-publish.yml`, `box-image-pr.yml` |
| Agent bundle build | New workflow; packaging spec |
| Port 5000 resolution | `cli/context/session.py`, `cli/box_storage.py` (per Q3) |
| Diagnostics | `cli/commands/box/diagnose.py`, `box/lager/http_handlers/diagnose.py` |

### Phase 3 — instrument migration, in dependency order

1. Remaining `/invoke` backends that are pure pyvisa or pyserial.
2. Platform layer for serial identity; UART stream channel.
3. Remote driver substitution in `box/lager/dispatchers/base.py` and the non-dispatcher
   handles, covering script-side LabJack, FTDI, Aardvark, USB-202, Joulescope, PPK2,
   Phidget, DexArm.
4. Debug service on the agent; container relays for `:8765` and the TCP port ranges;
   `/proc` usage replaced through the platform layer.
5. Oscilloscope daemon for macOS: loader paths and sonames (`pico/loader.rs`), presence
   check (`pico/usb.rs`), `v4l` already target-gated.
6. USB hubs, DFU, HSM, watt and energy meters.
7. High-risk group last: BLE and BluFi, webcams, HDMI capture.

### Phase 4 — cross-platform conformance

Manifest, lifecycle and performance cases, equivalent Linux and macOS benches.

### Phase 5 — production distribution

Signing, notarisation, upgrade and rollback, uninstall, documentation
(`docs/source/getting-started`, translations), release process changes in
`RELEASE_PROCESS.md`.

---

## 8. Risks, blockers, and decisions needed

### 8.1 Decisions

| # | Question | Recommendation |
|---|---|---|
| Q1 | How are local and remote installs selected? | **Decided:** no `--box`/`--ip` means local; with them, remote over SSH. Both work for Linux and macOS targets and must behave identically (section 4.2) |
| Q2 | Which instruments are physically available? | **Answered:** J-Link, Acroname hubs, power supplies, PicoScopes, YKUSH, LabJacks. Phase 1 uses a USBTMC power supply; these also cover the debug, hub, scope-daemon, and script-side driver paths in Phase 3 |
| Q3 | Port 5000 conflicts with AirPlay on macOS | **Decided:** macOS boxes use the existing `8301` alias. `lager install` saves the port in the box's `~/.lager` record; the CLI reads it from there and never probes per command. A Mac box added by hand without the field is probed once, on first failure, and the result is saved |
| Q4 | Agent language | **Decided:** Python-first, Rust scope daemon alongside. Constraint: installing `lager-host` is invisible. `lager install` fetches, installs, and starts it with no separate command, download, or prompt beyond what a Linux install already asks (a `sudo` password where a vendor tool needs it) |
| Q5 | Out of scope on macOS: WiFi control, pigpio, `network-mode: host` | **Decided:** WiFi control and `network-mode: host` return a clear "not supported on macOS" error. pigpio was vestigial and its live plumbing is removed on every platform (Linux-affecting, in the ledger): `box/start_box.sh` no longer passes `PIGPIO_ADDR` to the container and scripts no longer receive it. `lager update`, `lager uninstall` and the deploy script still remove a container named `pigpio`, so boxes installed long ago are cleaned up |
| Q10 | Windows local install | **Decided:** recognised, reports "not yet supported" |
| Q6 | Scripts that touch hardware directly | **Decided:** pyserial, pyvisa, and custom driver bundles are made to work (section 3.6). Raw USB libraries and direct `bleak` are not; a script that imports them on a macOS box gets an immediate error naming the Lager API to use |
| Q7 | Bench hardware | In progress on the owner's side. Hardware-in-the-loop starts in Phase 2. Re-enabling automated bench runs is a separate workstream (decided). Still needed before Phase 4 and before any instrument is advertised as supported |
| Q8 | Minimum macOS version | 14 or later, to be confirmed against vendor SDK requirements |
| Q9 | Third-party tools that need admin installers or have redistribution limits (SEGGER, PicoTech, LabJack, Phidget) | Detect and guide rather than bundle where licences require it; this is the main threat to "no manual steps" |

### 8.2 Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `host.docker.internal` behaves differently between runtimes or versions | Install fails on one runtime | Phase 1 tests both; fallback bind strategy |
| macOS privacy permissions for Bluetooth and camera in a background agent | BLE and webcam unusable or prompt unexpectedly | App-bundle layout with usage strings; schedule these last |
| Apple serial drivers holding FTDI and other interfaces | FT232H GPIO/I2C/SPI and some probes blocked | Early spike in Phase 3; may need a documented limitation |
| BLE addresses are not MAC addresses on macOS | "Indistinguishable" cannot hold for address-based BLE APIs | Decide on an identity mapping or a documented difference |
| Vendor SDK gaps on arm64 macOS | Instrument unsupported | Matrix tracks each; no claim without hardware pass |
| Extra network hop on latency-sensitive paths | Slower GPIO waits, scope streaming, RTT | Keep timing-critical sequences in the agent; batch endpoint; measure |
| Shared config directory across the VM boundary | Lock or atomic-rename misbehaviour | Single owning side per file; test under both runtimes |
| Any edit to code a Linux Box runs | Linux regressions | Section 0.1: additive changes, default-off switch, impact ledger, separate commits, enforced check |
| No automated Linux hardware runs today | Regressions caught only when someone runs a bench by hand | Manual run on an existing Linux bench for each Medium or High ledger entry; re-enabling automation is Q7 |

### 8.3 Smallest practical vertical slice

One power supply, end to end, on one Mac. Phase 1 runs it against a simulated instrument
over TCP; Phase 2 repeats it on a physical USBTMC supply:

`lager supply <net> voltage 3.3` → container `:9000` → `Device` proxy → authenticated
RPC → native agent → `hardware_service` → pyvisa-py → libusb → instrument, and the same
net driven from a `lager python` script.

It exercises discovery, the dominant RPC path, authentication, version handshake, error
propagation, session cleanup, and container-to-host networking on both runtimes, while
touching the fewest files.

#### USB discovery on the Mac box (2026-11-17)

`lager instruments` listed nothing and `lager nets state` reported USB
instruments as "not on the USB bus", because both read Linux's sysfs inside
the container. Both now ask the agent when the container runs with
`LAGER_HOSTLINK=1`.

- **Presence** (`self_restart.enumerated_usb_ids`): the agent answers from the
  I/O Registry (`GET /v1/usb/ids`).
- **Instrument scan** (`usb_scanner.list_instruments`): the agent runs the
  Box's own scanner, unedited, against a sysfs-shaped tree it writes from the
  I/O Registry before each scan (`lager.host.darwin_scan`,
  `GET /v1/instruments/list`). A device's directory name is derived from its
  location ID (`0x01114400` is `1-1.1.4.4`), so port-addressed devices get
  addresses of the same form as on Linux.
- **Linux-affecting:** one default-off check in each of
  `box/lager/util/self_restart.py` and `box/lager/http_handlers/usb_scanner.py`
  (both in the ledger). Not run on Linux hardware.

Verified on the bench: `lager instruments` lists the Acroname, J-Link,
Keysight, LabJack U3 and PicoScope; `lager nets state` shows the Keysight's
live state.

**Open:**
- The tree has no `/sys/class/tty` or `/sys/class/video4linux`, so serial
  adapters and cameras are not listed.
- The PicoScope reports no USB serial on macOS, so its scanned address has an
  empty serial where a net created earlier carries the unit's serial.
- A LabJack U3 is addressed as `port-1-1.3`; whether its driver can resolve
  that on macOS is not yet tested.

#### Serial ports on the Mac box (2026-11-17)

A USB serial adapter on the Mac is usable from the container under the name
a Linux Box gives it.

- **Names.** The agent gives each Mac port (`/dev/cu.usbserial-0001`) a Linux
  name (`/dev/ttyUSB0`; `ttyACM` for CDC devices), lowest free number first.
  Nets, scripts and CLI output only ever show the Linux name.
- **Lookups.** The agent writes the ports into its sysfs-shaped tree as
  `/sys/class/tty` entries, and the Box's own `serial_id` and UART-bridge
  lookups run against it unedited. The container asks the agent
  (`POST /v1/serial/lookup`).
- **Opening.** The agent opens the device exclusively and serves it to one
  client over RFC 2217 on a loopback port (`POST /v1/serial/open`). In the
  container `serial.Serial("/dev/ttyUSB0")` returns pySerial's RFC 2217
  client pointed at it, so data, line settings, modem lines and break cross.
  A startup hook (`hostlink/site/sitecustomize.py`, on PYTHONPATH only in the
  macOS start script) installs this in every Python process, so a user script
  that opens a port with pySerial directly works too.
- **Errors match Linux.** A second open reports the locked-port error with
  errno 11; an absent port reports "No such file or directory"; an adapter
  unplugged mid-session is classified device-gone by the Box's reconnect
  logic.
- **Linux-affecting:** one default-off block at the end of
  `box/lager/devices/serial_id.py` and one default-off check in
  `box/lager/protocols/uart/uart_bridge.py` (both in the ledger). Not run on
  Linux hardware.

Verified on the bench with a CP2102 adapter (TX looped to RX) behind the
Acroname: `lager instruments` lists it with `/dev/ttyUSB0`; `lager nets add`
records its USB identity; `lager uart -i` echoes typed text; a `lager python`
script loops data through `Net.connect()` and through raw
`serial.Serial('/dev/ttyUSB0')`; baud changes, a second open, and powering
the adapter off and on through the hub behave as described above.

**Open:**
- Only pySerial can open a host port. A non-Python tool in the container
  (picocom, a vendor flasher) finds no `/dev/ttyUSB0`.
- The RFC 2217 port is unauthenticated on the Mac's loopback for as long as
  a port is open, like the 8765 and 8085 forwards.
- `/dev/serial/by-id` and `by-path` names are not provided.
- A net with neither a USB identity nor a USB serial reconnects by checking
  that its path exists, which is never true in the container.
- Multi-interface chips (FT2232H, FT4232H) are covered by tests only.
- A Prolific PL2303G adapter (067b:23a3, in Lager's catalog) gets no port on
  macOS without Prolific's driver; the installer should say so.

#### LabJack U3 on the Mac box (2026-11-17)

- **Libraries.** The agent needs LabJackPython (pip) and LabJack's Exodriver
  (`liblabjackusb.dylib`), built from LabJack's GitHub source against
  Homebrew's libusb. Decision: the installer builds it from that source, as it
  fetches the Pico drivers from Pico. The agent finds it through
  `DYLD_FALLBACK_LIBRARY_PATH`, which must be set by `env` immediately before
  Python: macOS drops `DYLD_*` variables when it starts any system binary
  (the shell, `nohup`). The launchd job will need the same.
- **CLI.** `lager adc`, `lager dac`, `lager gpi`/`gpo` and `lager nets state`
  work, and the port-pinned address (`port-1-1.3`) resolves on macOS.
- **Scripts.** `Net.get` for a gpio, adc or dac net opened the device over USB
  from the script. With `LAGER_HOSTLINK=1` it now returns a stand-in
  (`lager.hostlink.io_proxy`) that calls the hardware service's adapter for
  the role, the route the CLI takes. **Linux-affecting:** one default-off
  check in `box/lager/nets/net.py` (in the ledger). Not run on Linux hardware.
- **Difference from Linux:** a failure in a script is a `DeviceError` with the
  driver's message, not the driver's own exception class.

Verified on the bench with a U3-HV, nothing wired: ADC read, DAC set and read
back, GPIO high/low read back, and `wait_for_level`, from the CLI and from a
`lager python` script.

**Open:** thermocouple nets, and GPIO/ADC/DAC nets on the FT232H, USB-202 and
YKUSH3 adapters, are covered by the same stand-ins but none was on the bench.

#### SPI and I2C in scripts (2026-11-17)

- **CLI.** `lager spi` and `lager i2c` already ran in the hardware service
  (`spi_hs`, `i2c_hs`), so they worked on the Mac box with no change.
- **Scripts.** `SPINet` and `I2CNet` ask their dispatcher for the driver, and
  the dispatcher opened the adapter over USB from the script. With
  `LAGER_HOSTLINK=1`, `Net.get` for an spi or i2c net first calls
  `lager.hostlink.bus_proxy.install()`, which replaces each dispatcher's
  `_resolve_net_and_driver` in that script's process with one that returns a
  stand-in driver calling `spi_hs` / `i2c_hs`. `SPINet`, `I2CNet` and the
  dispatcher files are not edited. **Linux-affecting:** one more default-off
  check in `box/lager/nets/net.py` (same ledger entry). Not run on Linux
  hardware.
- Failures arrive as `SPIBackendError` / `I2CBackendError`, as on Linux.
- The script runner drops the hardware service's drivers before each script
  (`/cache/release_direct_usb`, relayed to the agent), so a script starts from
  the saved configuration, as its own driver would on Linux.

Verified on the bench with a U3-HV, MOSI (FIO6) jumpered to MISO (FIO7): SPI
transfer, read and read_write return the bytes sent, from the CLI and from a
script, including after `config(mode=3, bit_order="lsb")`. Nothing is wired to
the I2C pins: config and scan return, and a read reports the driver's no-ACK
message. No real SPI or I2C slave has been read.

**Differences from Linux, open:**
- A script that only imports a dispatcher and never calls `Net.get` for an
  spi or i2c net is not handed off.
- An invalid argument that makes the driver fail to open (for example
  `config(mode=9)`) is treated by the hardware service as a device it could
  not open: the message gains a "restarting" prefix and the agent's worker
  restarts (about two seconds). On Linux the script just gets the error.
- Aardvark and FTDI adapters need their own libraries on the agent; untested.

#### `lager install` on this Mac (2026-11-17)

`lager install` with neither `--box` nor `--ip`, run on an Apple silicon Mac,
now provisions a Box on that Mac (`cli/deployment/macos_box.py`, wired in by
`cli/commands/utility/_local_install.py`). `lager update` and
`lager uninstall` follow, and a saved Box at a loopback address is routed the
same way when the CLI runs on macOS. On Linux and Windows the no-target form
exits with "not yet supported".

What it does, each step safe to repeat:

1. Finds a running Docker Desktop or OrbStack (PATH, then known locations
   and sockets).
2. Creates `/etc/lager` with one `sudo` if it is missing.
3. Takes the Box source from the checkout the CLI runs from, or clones the
   release tag. The agent and the container run the same tree.
4. Builds the agent's Python environment under
   `~/Library/Application Support/Lager/host/` and installs its packages.
   Vendor driver packages are installed one at a time; a failure is reported
   and does not stop the install.
5. Builds LabJack's USB library from LabJack's GitHub source (needs Homebrew
   libusb) and the oscilloscope daemon (needs Rust >= 1.89 and PicoTech's
   PicoSDK). Each is skipped with a stated reason when a prerequisite is
   missing. Nothing is installed or updated on the user's behalf.
6. Installs and starts the per-user launchd job `com.lagerdata.lager-host`.
7. Starts the container `lager` (`--restart always`, no privileges, no
   devices), waits for the handshake, writes `/etc/lager/version` and `ref`.
8. Saves the Box at `127.0.0.1` with `python_port: 8301`.

Verified on the bench Mac: install from nothing (31 s with the image already
pulled), install again, `lager update --box`, `lager uninstall --box` (agent
files, launchd job, container and saved Box gone; `/etc/lager` kept), then
install again. Through the installed Box: `lager hello`, `lager instruments`,
Keysight state, J-Link status, PicoScope frequency measurement, LabJack ADC,
SPI loopback script, UART loopback script, Acroname port state.

**Linux-affecting (all in the ledger, none run against a Linux Box):**
`cli/commands/utility/install.py`, `uninstall.py` and `update.py` each gain
a branch for "no target" or "loopback on macOS"; `cli/box_storage.py`
`add_box` takes an optional `python_port`; `cli/commands/box/boxes.py`
carries it across `lager boxes edit`.

**Decisions made here, open to change:**
- A local install publishes the Box's ports on `127.0.0.1` only. A Linux Box
  publishes on every interface; a laptop should not offer an unauthenticated
  Box to whatever network it is on. A remote macOS install will need the LAN.
- With `--yes` and no saved Box at `127.0.0.1`, the Box is saved as `local`.

**Open:**
- The Box image is `linux/amd64` only and runs emulated. The container
  always mounts the source tree over the image's copy.
- macOS asks once to allow the agent's Python (a new program to macOS)
  when it first runs. The installer does not announce this yet.
- After several agent restarts in a row the Keysight answered
  "Query INTERRUPTED" and then timed out until the agent was restarted once
  more. Seen once; not reproduced.
- `lager update --check` is not available for a local Box.
- `--fork`, `--skip-*`, `--pull`/`--no-pull` and `--timeout` are ignored or
  refused by the local path. Uninstall's `--all`, `--keep-docker-images` and
  `--include-control-plane` are ignored: the image is always kept.
- Local Linux install and remote macOS install: sections below.
- No release bundle, signing or notarisation; the agent runs from source.
- J-Link, PicoSDK/PicoScope 7, Rust and Homebrew libusb are detected, not
  installed.

### `lager install --ip <a Mac>` (2026-11-17)

`cli/commands/utility/_remote_macos.py` (CLI side) and
`cli/deployment/macos_remote.py` (runs on the Mac). Both new.

1. `lager install --box/--ip` checks SSH as it always has, then asks the
   target `uname -s` once. Only the exact answer `Darwin` goes to the macOS
   path. Any other answer, an error or a timeout continues down the Linux
   path unchanged.
2. The CLI pipes `macos_box.py` and `macos_remote.py` to
   `~/Library/Caches/com.lagerdata.installer` on the Mac. From a checkout it
   also pipes the tracked files under `box/` and `cli/__init__.py` to
   `~/Library/Application Support/Lager/src/shipped`, because a development
   version exists nowhere the Mac could download it from. A released CLI
   lets the Mac clone the release tag, as a local install does.
3. It runs the runner there over `ssh -t` (sudo may ask for the Mac's
   password) with Homebrew's Python 3, or `/usr/bin/python3` when the command
   line developer tools are installed. The runner uses the standard library
   only (checked under Python 3.9) and executes the same `macos_box.install`
   as a local install, with the ports published on `0.0.0.0`.
4. The outcome comes back as JSON in a file the CLI reads with a second,
   non-interactive SSH command; an error keeps its problem, cause and fixes.
5. The Box is always saved, with `python_port: 8301` and
   `"platform": "macos"`. With no name given it is saved as `mac-<address>`.
   `lager update` and `lager uninstall` recognise the Mac from that record
   and make no extra SSH call for any Box.

**Verified over real SSH (2026-11-17), from the bench Mac to its own LAN
address:** the project owner ran `lager install --ip <address> --user <user>`
in a terminal, which set up the `lager_box` key with one password prompt,
detected the Mac and installed. Then, non-interactively: `lager update --box`,
`lager uninstall --dry-run`, `lager uninstall` (agent files, launchd job and
container gone), and `lager install --box` from nothing (42 s with the image
present; the LabJack library and the oscilloscope daemon were built in the
SSH session's bare environment). Through the LAN address afterwards:
`lager hello`, `lager instruments` (all six bench devices) and a
`lager python` script on port 8301. launchd `bootstrap gui/<uid>` works from
an SSH session while the user is logged in at the desktop. With no terminal
on the CLI side `ssh -t` prints "Pseudo-terminal will not be allocated" and
carries on.
**Not verified:** a second Mac (the one offered was not reachable on the
network), `sudo` over `ssh -t` (`/etc/lager` already existed), the image
pull with the keychain locked (the image was already present), and reaching
the Box from a different machine.

**Linux-affecting (in the ledger, none run against a Linux Box):**
`install.py` makes one more SSH connection (`uname -s`) on every remote
install; `uninstall.py` and `update.py` read the saved-Boxes file once more;
`box_storage.add_box` takes an optional `platform`; `boxes.py` carries it
across `lager boxes edit`.

**Decisions made here, open to change:**
- A remote Mac's Box is published on every interface, like a Linux Box.
- A remote Mac is always saved, even with `--yes` and no `--box` (a Linux
  Box is not): without the record no later command knows the script port.

**Open:**
- `lager uninstall` / `lager update` with `--ip` for a Mac that is not saved
  go down the Linux path and fail there.
- Other commands that SSH to a Box and expect Linux (`lager ssh` is fine;
  `lager logs`, `lager status`, box-config, control-plane commands) are not
  audited against a Mac.
- The host agent's own listeners (debug service, GDB and RFC 2217 ports)
  bind to loopback on the Mac; from another machine only what the container
  relays is reachable.
- An update replaces the copied source in place before the new container
  starts; an update that fails midway leaves the old container on a
  replaced tree.
- The agent needs a logged-in desktop session on the Mac (launchd `gui`
  domain). A headless Mac that nobody has logged in to is not supported.
- The Mac's Docker default context decides which runtime gets the container
  (Docker Desktop on the bench Mac, which also has OrbStack). Nothing checks
  that an earlier install used the other one.

### `lager install` on a Linux machine (2026-11-17)

A bare `lager install` or `lager uninstall` on Linux no longer exits with
"not yet supported". It checks that `127.0.0.1` port 22 answers and then
continues into the existing SSH path with `ip=127.0.0.1` and the user running
the CLI (or `--user`). There is one Linux deploy path, whichever machine the
CLI runs on; `setup_and_deploy_box.sh` and everything after the target is
chosen are untouched. Windows still gets "not yet supported".

**Verified:** in a throwaway `python:3.11-slim` Linux container: with no SSH
server the command names the fix (`openssh-server`); with one it announces
`over SSH as <user>@127.0.0.1`, accepts the host key and reaches the key
set-up step. **Not verified:** a full install on a real Linux machine. The
deploy needs Docker, systemd and sudo, which the container does not have.
Run a bare `lager install` on one Linux bench before release.

**Linux-affecting (in the ledger):** only the no-target case in `install.py`
and `uninstall.py`, which until this branch was an error.

**Open:**
- The user installed for is the one running the CLI, not `lagerdata`; the
  deploy script takes any user, but this combination has not been run.
- With `--yes` and no `--box` the Box is not saved, as for any Linux Box;
  it is then reached with `--box 127.0.0.1`.
- A bare `lager update` is unchanged: it updates the default Box.

### Leftover agent processes (2026-11-18)

**Bug found on the bench Mac:** the agent's hardware worker restarted every
two seconds overnight (17,574 times) with "Port ... is in use by another
program". A worker from an agent started the previous day had survived the
agent that started it, and two uninstall/reinstall cycles, and still held
the agent's port. It kept answering hardware requests from a source tree
that no longer existed, which is why `lager instruments` looked healthy
after the reinstalls. A supervisor that is killed outright
(`launchctl kickstart -k`, a crash) cannot stop its children.

**Fix (`box/lager/host/orphans.py`, new; macOS agent only, no Linux file
touched):** each Python child watches its parent and exits when it changes;
a starting supervisor ends agent children whose parent is launchd, asking
first and killing after five seconds. **Verified live:** the day-old
leftover was killed on the next agent start and the worker bound its port;
after two further `kickstart -k` there were no leftovers and no bind errors.

**Open:** the earlier "Keysight answered Query INTERRUPTED until one more
restart" was seen while that leftover was running and may have been two
workers talking to one instrument; not re-tested (the bench was not
attached on 2026-11-18).

### Commands that SSH into a Box, against a Mac (2026-11-18)

Audited against the Box installed on the bench Mac (`--box local`). Before:
`lager ssh`, `lager dut`, `lager logs` and the SSH half of `lager box-config`
all tried `ssh lagerdata@127.0.0.1` and failed on the host key or the user.

**Change:** `cli/commands/box/_macos_target.py` (new), asked first by the
shared runner `default_ssh_runner` and by `lager ssh`.
- The Box on this Mac (loopback address, macOS CLI): the command runs in a
  local shell. No SSH, no Remote Login, no `lagerdata` user.
- Any Mac Box, local or saved with `"platform": "macos"`: a command that
  manages a Linux host (apt, dpkg, sysctl, udev, systemd, journalctl, ufw,
  sudoers, `start_box.sh`) is refused by name and never run.
- Every other Box: unchanged; a test pins the identical `ssh` argv.

**Verified on the bench Mac:** `lager ssh --box local -- <cmd>` (output and
exit status), `lager dut show`, `lager box-config init/status/show/reset`
and `apt add`, `lager logs size`. `lager box-config apply` stops at the first
host step with "not supported for a Box on macOS".

**Linux-affecting (in the ledger):** `cli/commands/box/_ssh.py` (one call at
the top of `default_ssh_runner`; one more read of the saved-Boxes file per
SSH command) and `cli/commands/box/ssh.py`.

**Bug fixed in the 2026-11-17 remote-install commit:** the `uname -s` probe
bound `subprocess.run` when its module was imported, so tests that replace
`subprocess.run` did not replace it there, and existing install tests ran a
real `ssh lagerdata@10.0.0.1`. That failed instantly on one network and hung
the suite on another. The probe now looks `subprocess.run` up when called,
carries `ConnectTimeout=10`, reads no stdin and treats any failure as "not a
Mac".

**Open:**
- `lager box-config apply` cannot restart the container on a Mac, so config
  that needs a restart (mounts, env, volumes, pip/npm/cargo packages) can be
  edited but not applied. Needs a macOS restart path.
- `lager logs docker` reports "needs sudo" on a Mac; Docker's log files are
  inside the runtime's VM there.
- After a refusal some commands still print a Linux "manual fix" line.
- A remote Mac runs these commands over SSH as before; only the refusals
  apply. Not exercised against a remote Mac.
- A laptop Box saved by LAN address stops resolving when the laptop changes
  network (seen: the bench Mac moved from 10.101.x to 192.168.x overnight).

### Requirement: full parity (2026-11-18)

The project owner: "my customers will use all instruments on the mac. this
port needs to make the mac version indistinguishable from the linux
version".

This replaces "the bench's instruments work" as the bar. Every net type,
instrument family, script API and CLI command of the Linux Box is in scope,
and the port is not done while any is unbuilt or unverified. It is still
subordinate to section 0.1: the Linux Box must not regress.

Three exceptions were confirmed by the owner on 2026-11-18, after setting
this bar: WiFi control stays "not supported" on macOS; raw USB, pyftdi and
bleak used directly in a script keep failing loudly; host network mode
stays refused. Everything else is in scope.

Hardware the owner can put on a bench for verification, beyond the current
bench: YKUSH, LabJack T7, Keysight supplies, OpenOCD probes, Rotrics arm,
4-port Acroname.

A parity matrix (every net type and instrument family, how a script and the
CLI reach it, and its macOS state) is being compiled and will be added
here; it is the list this work is tracked against from now on.

### Parity matrix (compiled 2026-11-18)

Compiled by reading the code, not by running it; rows not marked "bench"
have never been exercised on a Mac. "CLI" is a `lager <command>`; "script"
is `Net.get(...)` or a module-level helper inside `lager python`.

States: **bench** = verified on the bench Mac; **built** = hand-off exists,
not verified; **likely** = no hand-off needed (the object already calls the
hardware service) but never run on a Mac; **gap** = does not work yet.

| Area | Instruments | CLI | Script |
|---|---|---|---|
| Power supply | Keysight E36000 | bench | bench |
| Power supply | Rigol DP800, Siglent SPD1000X, Keithley 2281S | likely | likely |
| Power supply | Rigol DP700 (serial), EA PSB | gap: Linux tty names opened in the agent | same |
| Battery, e-load, DMM | Keithley 2281S, Rigol DL3021, Siglent SDM3065X | likely | likely |
| Solar | EA PSB | gap (tty names) | gap: helpers build the driver in the script |
| Scope | PicoScope 2000 | bench | bench |
| Scope, logic | Rigol MSO5000 / DS1000Z | gap: per-scope daemon is started from the container; `/proc`, binary path | gap |
| GPIO, ADC, DAC | LabJack U3 | bench | bench |
| GPIO, ADC, DAC | LabJack T7, U6, MCC USB-202, FT232H, Aardvark, YKUSH3 | built | built |
| SPI, I2C | LabJack U3 | bench (SPI loopback; I2C no slave) | bench |
| SPI, I2C | LabJack T7, Aardvark, FT232H | built | built |
| Debug | J-Link | bench | bench (connect, reset, halt, flash, erase, read, status, RTT) |
| Debug | OpenOCD probes (ST-Link, CMSIS-DAP, FTDI, picoprobe) | gap: fixed Linux paths | gap |
| Debug | remaining `DebugNet` methods in scripts | n/a | gap: not on the stand-in |
| USB hub | Acroname | bench | bench |
| USB hub | YKUSH | built | built |
| USB hub | Plugable | gap: driver reads sysfs | gap |
| UART | USB-serial by `ttyUSBn`/`ttyACMn` | bench | bench |
| UART | pinned by `/dev/serial/by-id` or `by-path` | gap | gap |
| Thermocouple | Phidget | likely | built |
| Rotation | Phidget encoder | no command exists | built |
| Watt meter | Yocto-Watt, Joulescope JS220, Nordic PPK2 | likely | built |
| Energy analyzer | Joulescope JS220, Nordic PPK2 | likely | built |
| HSM | YubiHSM 2 | likely | built |
| Arm, actuate | Rotrics Dexarm | gap (tty names, port discovery) | built, same gap in the agent |
| Webcam, HDMI capture | UVC cameras, capture cards | gap | gap |
| BLE, BluFi | host Bluetooth | gap | gap |
| WiFi (`lager wifi`) | host WiFi | not supported on macOS (owner's decision); must say so plainly | same |
| Routers | MikroTik, ASUS | likely (network only) | likely |
| DFU, `/usb/devices`, `lager diagnose` | host USB | gap: run in the container | n/a |
| Module-level helpers | `lager.power.supply.voltage(...)`, `lager.io.adc.read(...)` and the like, for supply, battery, e-load, solar, ADC, DAC, GPIO, watt, thermocouple, energy, HSM | n/a | built (driver made in the agent through the dispatcher base); SPI/I2C and USB hub bench |
| Helpers that bypass the dispatchers | `lager.core.read_adc`, the DAC legacy backend | n/a | gap |
| GPIO/ADC/DAC from `LAGER_MUXES` (legacy net definition) | — | n/a | gap |
| pyvisa used directly in a script | any | n/a | gap |
| raw USB, pyftdi, bleak used directly in a script | any | n/a | fails loudly (owner's decision) |
| Custom driver bundles, customer binaries | user code | reach hardware only through nets that have a hand-off | same |
| MCP tools | `debug_probe_status`, hub USB listing | gap: read container sysfs | n/a |
| Box management | install, update, uninstall, ssh, dut, box-config edit | bench | n/a |
| Box management | box-config apply, update --check, logs docker | gap | n/a |

Also unverified for every non-bench row: that the vendor library the driver
needs has a macOS build the installer provides (LJM, libuldaq, libphidget22,
aardvark, hidapi/pykush, joulescope, yoctopuce, OpenOCD, dfu-util).

Not macOS-specific, found on the way: `Waveform` and `PowerSupply2Q` nets
cannot be constructed from saved nets on Linux either.

Order of work: (1) script stand-ins for the roles that already have a
hardware-service adapter (thermocouple, watt, energy, HSM) and the
module-level helpers; (2) tty-name translation inside the agent (DP700, EA,
Dexarm, by-id pins); (3) OpenOCD, Rigol scope daemons, Plugable hub;
(4) DFU, USB listing, diagnose, MCP tools; (5) pyvisa and raw USB from
scripts; (6) cameras; (7) BLE/BluFi and WiFi; (8) box-config apply, arm64
image, packaging.

### A script's driver object, run in the agent (2026-11-18)

`box/lager/host/net_rpc.py`, `box/lager/hostlink/net_proxy.py`,
`box/lager/hostlink/wire.py` (all new). Instead of one hand-written stand-in
per instrument, the object `Net.get` would have returned on Linux is made in
the agent and the script holds a stand-in that forwards every method call
and attribute read. The script therefore sees the driver's whole interface.
- Values keep their types (bytes, tuples, sets, non-string keys; numpy
  becomes plain numbers and lists). Errors are raised as the driver's own
  class when it can be imported in the container.
- A value with no JSON form (an HSM session, a generator) stays in the
  agent and comes back as a stand-in of its own.
- An object lives until the script closes or drops it; all are released
  when the script runner asks for USB claims to be released, before each
  script.
- Not carried: a callback passed as an argument; `isinstance` against the
  driver class.

Used by `Net.get` for thermocouple, watt-meter, energy-analyzer, hsm,
rotation, actuate and arm nets (one more default-off branch in
`box/lager/nets/net.py`, in the ledger). **Verified:** unit tests against a
fake driver; live on the bench Mac only that a script in the container
reaches the agent's route and gets the Box's own `InvalidNetError` for an
unknown net. **Not verified:** any of these seven instrument types on
hardware; none is on the bench.

**Module-level helpers (2026-11-18).** All eleven Box dispatchers get their
driver from `_make_driver`, so one default-off hook in
`BaseDispatcher.__init__` (`box/lager/dispatchers/base.py`, in the ledger)
hands that to the agent: the agent instantiates the same dispatcher class
and runs its real `_make_driver`, and the helper gets a stand-in for the
result. The agent refuses any class that is not a `BaseDispatcher` subclass
in the `lager` package. Unit tested; not run on hardware.


# Changelog

All notable changes to the Lager platform are documented here. For detailed release notes, see [docs.lagerdata.com](https://docs.lagerdata.com).

Releases 0.2.17 through 0.39.1 are in [docs/changelog/0.2-0.39.md](docs/changelog/0.2-0.39.md).

Write one bullet per change, in one to three sentences: what changed for a user, and the command or API it affects. Put diagnosis, measurements, and test detail in the pull request and the release notes.

## [Unreleased]

<!-- Keep this heading. A branch cut before a release and merged after it
     files its entry here; without it the entry lands inside the released
     section below, with no merge conflict to catch it. -->

### Added

- **The reference docs cover what shipped from v0.40.0 through v0.47.0.** Each
  claim was checked against the v0.47.0 source rather than the release notes.
  New or expanded sections:
  - `lager nets state`, which shipped in 0.34.0 and appeared on no page: state
    strings, the shared 8 s budget, reason codes and their remedy lines, and the
    `--json` fields.
  - The LabJack U3 across the adc, dac, gpi, gpo, spi, i2c, nets, instruments and
    supported-instruments pages: pins and the SPI pin order, the 50-byte transfer
    limit, approximate clock rates, the 0.04-4.95 V DAC range, external I2C
    pull-ups, the refused chip-select options, and the LJM `"ANY"` limitation.
  - `lager box-config`: the `apply` exit codes, every refusal of the host-network
    pre-flight, `network-mode show` output, switching back, `pip import-legacy`,
    and `LAGER_DISABLE_UART_SERVICE`.
  - DA1469x flash and erase on both debug backends; `DebugNet.connect()` errors
    and backend-specific parameters; `erase` and `flash` failure output and exit
    codes.
  - `lager uart --sessions` and `--force`; `lager usb cycle` verdicts per hub;
    `lager webcam snapshot`; `lager exec` inside a CI job and
    `LAGER_CI_OVERRIDE`; robot-arm detection and `LAGER_ARM_PROBE`.
  - `/etc/lager/ref`, `update --check` exit codes, the install deploy timeout,
    the pre-built image rules for install and update, the host CLI, how a
    command recognizes its own lock, `setup_battery()`, and where the box
    services write their logs.

### Fixed

- **Four pages said the host firewall limits the Lager ports to the VPN.** On the
  default network, Docker publishes those ports ahead of the host firewall, as
  `SECURITY.md` states. The install, update, setup and architecture pages now say
  so and link the Security Model. `update.mdx` also documented a firewall step
  and a script invocation that do not exist.
- **The Logic Analyzer pages said the feature was not available.** The commands
  run. Both pages, and the scope page, now carry one callout for the MSO5000
  triggers and bus decoders that still fail (#418), plus exit codes and valid
  ranges. Examples that cannot run are removed.
- **Examples that could not run are corrected.** `lager debug flash --bin` takes
  `FILE,ADDRESS`, not `ADDRESS FILE`. `lager python --kill` needs the process ID.
  `lager uart` takes a net name, not `/dev/ttyUSB0`. `lager exec` needs `--`
  before extra arguments that start with a dash. `UARTNet.connect()` ignores
  `timeout`. `lager nets add` examples used channel `0` where the box lists
  `I2C0`, `SPI0` or `FIO4-FIO5`.
- **Stale claims are corrected.**
  - pyOCD was listed as a debug backend; J-Link and OpenOCD are the only two.
  - Webcam ports start at 8086, not 8081.
  - `lager usb` commands are case-sensitive, and the `cycle` timing and hub hold
    apply to Plugable docks only.
  - A LabJack U3 supports SPI and I2C.
  - A second role on a Keithley or EA instrument is a notice, not a block, and
    two Acroname hubs of one model both work.
  - The `.lager` defaults are JSON in `~/.lager`, not INI in the project.
  - `lager hello`, `lager boxes`, `lager defaults list`, `lager instruments`,
    `lager logs` and `lager binaries list` sample output matches the CLI.
  - The box runs one `lager` container, not `controller`.
  - The MCP server is read-only only while both opt-in gates are off.
  - Top-level `lager` options are `--version`, `--debug` and `--colorize`;
    `--box` belongs to each command.

## [0.47.0] - 2026-09-10

### Added

- **`lager webcam NET snapshot`** saves one JPEG frame from a running
  stream. The frame comes back over the box's command endpoint, so scripts
  and assistants that only need to look at the bench never have to reach
  the stream's own port or parse the multipart feed. The streamer also
  serves `GET /snapshot` directly for anything already talking to it.
- **Stream origin is recorded.** A webcam stream now remembers which
  surface started it (`cli`, `api`, or another tool) and who that surface
  attributed it to; `lager webcam url` prints it, and other tools sharing
  the box can label streams they did not start.

- **`lager spi` and `lager i2c` now work against a LabJack U3.** A U3 can now
  host `spi` and `i2c` nets, which means it covers every role the T7 does.

  As with `adc`/`dac`/`gpio`, this is a second driver stack rather than an
  extension of the existing one. The T7 reaches both protocols through named
  Modbus registers over LJM, and LJM does not talk to the U3 at all. A U3 has
  one low-level extended command for each -- `0xF8/0x3A` for SPI and
  `0xF8/0x3B` for I2C -- reached through LabJackPython over the Exodriver, both
  already in the box image. They need U3 hardware version 1.21 or greater. The
  T7 paths are untouched and the two families can share a box.

  Differences from the T7 that are visible to a user, and deliberate:

  - **The pins are `FIO4`-`FIO7`, not `FIO0`-`FIO3`.** SPI is `CS=FIO4`,
    `CLK=FIO5`, `MISO=FIO6`, `MOSI=FIO7`; I2C is `SDA=FIO6`, `SCL=FIO7`. Those
    are LabJackPython's own defaults and what LabJack's wiring diagrams show.
    Note the SPI span runs CS/CLK/**MISO**/**MOSI** -- MISO before MOSI, the
    opposite of the T7's span. The two spans overlap because every usable `FIO`
    on a U3-HV lives in that one four-pin block; the pin conflict tracker warns
    if a single script drives both. A net asking for `FIO0`-`FIO3` is now
    refused when it is created, naming the pins that work, rather than being
    accepted and failing at first use on hardware.
  - **A transfer is at most 50 bytes**, and an I2C transaction at most 50 bytes
    out and 52 back. These are the commands' own limits and are not the T7's 56.
  - **Clock speed is approximate, and is never rounded up.** Neither part takes
    a frequency: both take a delay count. SPI reaches about 5.4 kHz to 71 kHz
    and I2C about 10 kHz to 150 kHz; a request outside the reachable range is
    clamped with a warning naming what was used, as on the T7. `config` reports
    the rate the hardware settled on rather than the one requested, and names
    the request alongside it when the two differ.

    The SPI figures are measured, not taken from the datasheet. LabJack
    publishes `Frequency = 1e6 / (10 + 10 * (256 - SPIClockFactor))` with 0
    meaning the maximum, but a U3-HV on hardware 1.30 / firmware 1.24 does not
    behave that way: the byte is a plain delay count, 0 fastest and higher
    values monotonically slower. The published formula makes a wire value of 1
    the *slowest* setting, which would put a 50-byte transfer at about a
    second; it measures around 6 ms. The driver therefore models the period as
    affine in the wire byte, the same shape the I2C delay count already used.
    Those two constants come from timing transfers on one unit, so they
    describe effective bit rate rather than the SCK pin rate.
  - **`cs_active="high"` and `keep_cs=True` are refused, not ignored.** The
    firmware's `AutoCS` drives CS low for the transfer and releases it at the
    end; there is no polarity bit and no hold bit. Both errors name the way to
    get the behaviour: `cs_mode="manual"` plus a `gpio` net driving CS, which
    holds it across as many transfers as you like.
  - **I2C needs external pull-up resistors.** A U3 has none at all -- 4.7k to
    VS on both `SDA` and `SCL` is the usual choice. Without them every address
    NAKs, so a scan returns nothing and looks exactly like an empty bus.

  Two details of the vendor library are load-bearing and easy to get backwards,
  so they are called out here for anyone reading the drivers:

  - **`u3.spi()` does not trim its own reply and `u3.i2c()` does.** An
    odd-length SPI transfer is padded with a byte and the response comes back
    at the padded length, so the driver trims it; handing the library's list
    straight back would grow a phantom trailing byte on every odd transfer.
    Its I2C sibling already trims, so doing the same thing there would lose a
    byte instead.
  - **I2C `AckArray` is numbered from the end of the transfer.** Bit 0 is the
    *last* data byte and the address byte is the highest bit, at index `n` for
    an `n`-byte write, so the address bit moves with the transfer length -- it
    is bit 0 only during a scan, which writes no data at all. A partially
    acknowledged write therefore produces a non-zero value, and a check written
    as "no ACK means zero" would call it a success and silently drop the bytes
    the device refused. Writes and reads now verify every acknowledgement and
    name the first byte a device rejected.

  Pin mode is handled the way the existing UD drivers handle it: the
  analog/digital mux is whole-device state, so it is set through the handle
  manager under its lock before every transaction and never written from inside
  a driver. Setting the SPI command's `DisableDirConfig` is not a substitute --
  that sets each line's direction, which is a different register.

- **The box container's docker network is now a per-box setting.** `lager
  box-config network-mode set host` runs the container with `--network host`
  instead of the default `lagernet`; `unset` returns it to the default. Nothing
  changes on a box that does not set it.

  This exists so the box's Bluetooth adapter is reachable from inside the
  container. Linux `AF_BLUETOOTH` sockets are scoped to a network namespace --
  the kernel registers that address family only in the initial one -- so `hci0`
  is invisible on `lagernet` however the container is privileged, and raw-HCI
  tooling cannot run there. Confirmed on a box with the shipping image: the
  same image with the same `--privileged`, differing only in `--network`,
  answers `hciconfig -a` with "Address family not supported by protocol" on
  `lagernet` and reports the adapter up on `host`. `lager ble` is unaffected in
  either mode, because bleak reaches the host's `bluetoothd` over the mounted
  D-Bus socket rather than opening a Bluetooth socket of its own.

  Two consequences of `host` are worth stating, since neither follows from the
  command. Published ports are not published on host networking, so the host
  firewall governs the box's ports where Docker's forwarding rules previously
  bypassed it. `secure_box_firewall.sh` allows those ports **per interface** --
  `lo`, `docker0`, `tailscale0` when present, and one named with
  `--corporate-vpn` -- and denies them elsewhere, so a box reached over any
  other route stops answering the moment its ports stop being published. And
  the container shares the host's `bluetoothd`, so anything wanting exclusive
  control of the adapter contends with it.

  Because both of those can strand a box, **`apply` checks before it switches
  and refuses when the switch would cut the operator off.** It reads the
  interface the operator's own connection arrives on -- from the live SSH
  connection, so it holds for any VPN rather than only the one the firewall
  script knows by name -- and confirms the control-plane ports are admitted
  there. It reads the rules in the order ufw applies them, so an allow listed
  behind a deny for the same port does not count. It never opens a port
  itself: whether Lager's control plane belongs on a LAN is a security
  decision, not a side effect of a Bluetooth feature. A refused apply changes
  nothing; `--skip-host-network-check` overrides it.

  **Only `apply` makes the switch.** `start_box.sh` renders the config on every
  container start, so without this a refused `apply` left `host` in the config
  for the next `lager update` to apply unchecked. Any other start now keeps the
  network the last successful `apply` recorded and prints that the switch is
  pending, while a return to `lagernet` needs no check and happens from any
  start. `apply --skip-restart` refuses a pending switch to `host`.

  The check reads ufw with `sudo -n ufw status`, which Lager does not grant. On
  a box where sudo asks for a password, `apply` refuses, names that command, and
  prints the one sudoers line that lets the check run, to add with
  `sudo visudo -f` to a sudoers file of the operator's own.

  The commands it prints use `ufw insert`, not a plain `ufw allow`.
  `secure_box_firewall.sh` writes its per-interface allows first and a blanket
  `deny <port>/tcp` last, and ufw matches the first rule that applies, so an
  appended allow sits behind that deny and never takes effect. Position 1 is
  ahead of it whatever else the box carries. Each insert is preceded by a
  delete, because ufw skips a rule it already holds and would otherwise
  silently no-op for anyone who had already tried appending one.

  It also refuses on a box fronted by a port-publishing gateway, where the
  container would bind ports the gateway already holds and fail to start. A
  port counts as taken only when something other than the lager container holds
  it: the container publishes 5000 and 9000 on any ordinary box, and `apply`
  stops it before starting the replacement, so its own ports are not a
  conflict.

  The recovery path is in-band. `network-mode set|show|unset` and every step of
  `apply` fall back to SSH when the HTTP path cannot reach the box, so a box
  that has already been switched can still be switched back. The post-bounce
  readiness check asks the box from inside the container rather than over a
  route the switch may just have closed, and polls for the same deadline the
  network check uses. Without those the applied-hash was never stamped, and
  every later `apply` re-bounced the container indefinitely.

  The default is unchanged, and the setting is stored so that it does not
  disturb boxes that never use it: a config sitting at the default writes no
  key, so its hash is byte-for-byte what it was before this release. Without
  that, every box in the fleet would report configuration drift and take one
  pointless container restart on its next `apply`. A box whose lager predates
  the verb is told to run `lager update` rather than shown the dispatcher's
  raw `unknown command`.

### Changed

- **Webcam links work on access-gated boxes.** A gateway can now front the
  stream ports, so `lager webcam start` / `url` print a link that carries
  your sign-in token instead of warning that the stream is unreachable.
  The link stays valid for roughly the token's lifetime (the CLI says how
  many minutes); `lager webcam url` mints a fresh one.
- **Lock holders recorded as `origin:id:name:email`** by other services now
  display as the name in `lager boxes` instead of `name:email` run together.
- `lager defaults add --user` help now says to use the same name you use in
  other tools that lock boxes, so a box locked from either side is
  recognised by both.
- **`lager box-config apply` now exits 3 when the container is up but running
  the previous config.** `start_box.sh` has always separated that from a failed
  bounce, but `apply` collapsed eleven distinct outcomes into exit 1, so a
  script could not tell "the box is fine, your config did not land" from "the
  bounce failed and the container may be down". The three codes are now `0`
  applied, `3` up on the previous config, `1` everything else. Anything that
  tested only for non-zero is unaffected.

### Fixed

- **`lager webcam <net> start` printed a stop command that did not run.** The
  hint at the end of a successful start read `lager webcam stop <net>`, which
  click refuses with `Got unexpected extra argument` — the net name belongs
  before the subcommand, as every other webcam example has it. It also printed
  the box's IP rather than the `--box` label the user typed, as did the
  matching hint after `start-all`. The line a first-time user copies is now
  `lager webcam <net> stop --box <label>`, and a test replays each printed hint
  through the command group so the text and the parser cannot drift again.
- **One dropped debug read no longer aborts a DA1469x flash.** Every step of
  the RAM-resident flash_loader path -- loader boot, ping, erase and each
  program chunk -- waits by polling a word of target RAM through the debug AP
  while the CPU is running, which is exactly where a marginal SWD link drops a
  reply and OpenOCD answers with nothing to parse. The poll loop retried a word
  that read back *wrong* but gave up on a word that failed to read at all, so a
  single dropped reply ended the flash with `OpenOCD mdw 0x... returned no
  values:` while seconds of its own deadline went unused. On one HIL bench with
  known-marginal probe wiring this was ~12% of flash attempts and the only
  remaining source of failure in an overnight suite. A failed read is now
  retried to the deadline like any other, and no timeout was lengthened to do
  it. A link that never answers still fails, and says the read failed rather
  than reporting a last value it never read.
- **Every "add a box" hint printed a command that could not run.** `lager boxes
  add` has required `--user` since 0.29.0, but the hints the CLI prints when it
  cannot find a box still read `lager boxes add --name X --ip Y` -- copy one and
  click rejects it as a missing option. All five hint sites now carry `--user`,
  as do the README, the `lager` file and ssh-setup references, and the MCP guide
  and discovery text an assistant reads to learn the command.
- **The box installer's offer to register the box never worked.** After a
  successful deploy, `setup_and_deploy_box.sh` offers to add the box to the
  `.lager` file in the current directory. That call omitted the required
  `--user` and sent its own error to `/dev/null`, so it failed on every box
  since 0.29.0 and reported only "Failed to add to .lager - you may need to add
  manually". It now passes the login user the deploy already knows, and lets a
  real error through instead of swallowing it.
- **Integration suites carried two competing settings for the box login user.**
  The scripts that register a temporary box when handed an IP address read one
  variable, while the raw `ssh` calls in the same file read another with a
  different default, so exporting a user changed one and not the other. They now
  share a single `SSH_USER`, defaulting to `lagerdata` as it did before 0.29.0
  removed the implicit default, declared once in the test harness.

## [0.46.2] - 2026-09-08

### Changed

- **The OpenOCD flash and erase decision now lives in one module.** 0.46.0 gave
  `DebugNet.flash()` / `.erase()` the DA1469x QSPI flash-loader path the HTTP
  debug service already had, but as a second copy of it: `service.py` and
  `debug_net.py` each decided for themselves whether a target needs the
  RAM-resident flash_loader rather than OpenOCD's `program` / bank erase. A
  second copy is how the Net API came to be missing the path in the first
  place, and the bench that found it passed every by-hand check while its
  automated runs left boards blank. Both paths now route through
  `lager.debug.openocd_flash`, and one predicate,
  `lager.debug.probes.is_da1469x()`, replaces the five inline device-string
  tests that were spread across them.

  Three changes are visible from outside. A loader failure on either path now
  names the step that failed instead of surfacing an OpenOCD tcl traceback, and
  a flash that dies after its erase stage says the board may be left blank. An
  absolute XIP address outside the DA1469x flash window is refused before any
  I/O rather than part-way through. And OpenOCD's generic `program` and erase
  now refuse a DA1469x by name — naming the missing QSPI driver and the module
  that does work — so a caller that bypasses the dispatch gets a sentence
  instead of a ten-second stall and a `startup.tcl` traceback.

  Callers keep passing absolute XIP addresses (`0x16000000`), as on the J-Link
  path. Non-DA1469x OpenOCD targets and the J-Link backend are unchanged. A
  parity test drives both entry points through the real dispatch and asserts
  they issue identical loader calls, and an AST scan of `box/lager` fails the
  build if either grows a private copy.

### Fixed

- **`lager exec` runs the command in place again when the CI job is already
  inside the devenv image.** `exec` used to choose between two runners: `docker
  run` on a developer's machine, and running the command directly under
  container-based CI, on the reasoning that such a job is already in the image
  and has neither a Docker binary nor a socket to start another one with. The
  second runner was lost when the command moved from `cli/exec/commands.py` to
  `cli/commands/utility/exec_.py`; `is_container_ci()` survived the move as
  exported dead code that nothing called. Every `lager exec` in a job container
  has since failed with `Docker is not installed or not in PATH`, which is the
  documented way to run a build under GitHub Actions, GitLab CI, Drone, and
  Bitbucket Pipelines. Nothing caught it because neither runner had a test.

  The command now runs in the job's working directory -- not `mount_dir`, which
  only means something when there is a bind-mount to name. `--env` and the
  `environment` key are applied to it; `--passenv` is satisfied by ordinary
  inheritance. Options that need a container to start (`--mount`, `--volume`,
  `--user`, `--group`) each produce a warning rather than being dropped in
  silence, while the equivalent `.lager` keys stay quiet under `--verbose`,
  since one config file is shared between a developer's machine and CI and
  carrying them is normal. `LAGER_CI_OVERRIDE` still forces the Docker path.

  A Jenkins agent and a bare `CI=true` runner are hosts, not job containers, and
  keep starting a container as before. Anyone on 0.4x whose CI sets the
  container-CI variables *and* has a working Docker will switch from a container
  to in-place execution; that is the pre-migration behavior returning, but it is
  a behavior change on an already-shipped version, not only a fix for people
  still on the older CLI.

## [0.46.1] - 2026-09-08

### Fixed

- **A LabJack U3 no longer offers `FIO0`-`FIO3` as `gpio` channels.** Those four
  pins are the U3-HV's fixed high-voltage analog inputs, wired through the
  high-voltage front end rather than to the digital fabric; no configuration
  bit makes them digital. They were advertised anyway, so `lager nets add`
  accepted a `gpio` net on `FIO0` without complaint and the net then failed at
  first use, on hardware, with the driver's `PIN_CONFIGURED_FOR_DIGITAL` error.
  `lager nets add-all` was worse: it enumerates the advertised list, so every
  box with a U3 gained four nets that could never be driven.

  The scanner reads a USB descriptor, and a U3-LV — whose `FIO0`-`FIO3` really
  are flexible — reports the same product id (`0cd5:0003`). Only an open handle
  knows the variant (`u3.U3.isHV`), and by then the net exists. The whole family
  is therefore treated as a U3-HV. Of the two ways to be wrong, advertising a
  pin that cannot work is the worse one; omitting it costs a U3-LV owner four
  digital lines, and those four pins remain readable as an `adc` net on
  `AIN0`-`AIN3`. The sixteen usable digital lines are `FIO4`-`FIO7`,
  `EIO0`-`EIO7` and `CIO0`-`CIO3`.

  This is deliberately not a setting. An environment variable or a `--force`
  flag would be a knob whose only correct value depends on a variant the
  scanner cannot detect, so it moves the guess to the user without handing them
  anything to decide it with.

  The channel table is box-side, and `nets add`, `add-all`, `instruments` and
  the net TUI all read it over `GET /instruments/list`, so one change closes
  all four. **A 0.46.1 CLI against a 0.46.0 box still sees the old list** —
  update the box to get the fix.

- **A rejected channel now names the ones that work.** `lager nets add` reported
  only that a channel was invalid, which for a U3 user meant a dead end. It now
  lists the valid channels for that role and instrument, and points a `FIO0`-`FIO3`
  attempt at `AIN0`-`AIN3`. `lager nets add-batch` gained the same channel check,
  reporting every bad record at once rather than the first. It stays permissive
  where it has to: a device the scan does not find is not validated, so batches
  that provision absent or bare-IP-addressed hardware behave as before.

  Two paths still accept any channel: the box's `PUT /nets/<name>` and its
  legacy `:5000` twin store what they are given, for every instrument. Nets
  created before this release also survive untouched — they still list, and
  still fail at use with the driver's message, which remains the check that
  sees the real device.

## [0.46.0] - 2026-09-04

### Added

- **`lager uart --sessions` and `lager uart <net> --force`.** A held UART net
  had no recovery path: the error named the conflict and stopped there.
  `--sessions` lists which nets are held, whether each holder's client is still
  connected, and whether its reader is running; `--force` releases the holder
  before connecting. Backed by `GET /uart/sessions` and
  `DELETE /uart/sessions/<netname>` on the box, and modelled on the box lock's
  existing `lager boxes unlock --force`. The "already in use" error now names
  the take-over command, including the `--box` the user typed.

  Against a box too old to serve the endpoints, `--sessions` says so and
  `--force` warns rather than failing obscurely; the routes are additive, so a
  current box keeps working with an older CLI.

- **The box serves its net and box metadata over HTTP, so the control plane can
  sync it.** New `GET|PUT /nets/<name>/metadata` (a net's `purpose` / `notes` /
  `tags`) and `GET|PUT /box-metadata` (the box's own description, stored in
  `/etc/lager/box_metadata.json`) on the box HTTP server, plus
  `netMetadataSync` / `boxMetadataSync` in the `/status` capabilities block and
  the metadata itself on each entry of the `/status` `nets` array.

  The control plane has had the other half of this since May and gates its
  pushes on those two capability flags, so with nothing advertising them it
  skipped every push silently — no error, no log, no sign in the dashboard.
  A description typed into the dashboard was written to its own database and
  went no further, and `lager nets describe` on the box was invisible to it.
  Only `tags` moved at all, and only upward, because it is the one field name
  the two sides still had in common.

  The endpoint speaks today's vocabulary: `purpose` / `notes` / `tags`, the
  fields `lager nets describe` writes and the MCP server reads. It rejects the
  pre-0.24.0 `description` / `dut_connection` / `test_hints` names rather than
  storing keys nothing reads back. Metadata is merged, so a caller that knows
  only about prose cannot drop a `jlink_script` or a `safety_limits` ceiling
  the way a whole-record `PUT /nets/<name>` would; every record sharing a name
  is updated, because the MCP bench loader builds one descriptor per record and
  leaving a sibling behind would make which metadata an agent sees depend on
  file order.

  A field that `bench.json` overrides through `net_overrides` is reported back
  in `shadowed_by_override`. The bench loader applies those *after* reading
  `saved_nets.json`, so a write under one lands on disk and never reaches an
  agent; answering a bare success there would tell the caller a value synced
  when it did not.

- **`lager adc`, `dac`, `gpi` and `gpo` now work against a LabJack U3.** The
  box speaks to the T-series through LJM, which does not support the U3/U6 at
  all, so this is a second driver stack rather than an extension of the
  existing one: the Exodriver (`liblabjackusb`) plus LabJackPython's `u3`
  module, both added to the box image. The T7 path is untouched and the two
  can share a box -- they are distinguished by USB product id.

  A U3 pin is analog *or* digital depending on a whole-device bitmask, which
  has no T-series equivalent: `AIN5` and `FIO5` are the same physical line. The
  mode is therefore owned by the handle manager and set under its lock before
  every read or write, rather than by each driver -- a driver writing the mask
  itself would flip another net's pin and produce a plausible number instead of
  an error. On a U3-HV, `FIO0`-`FIO3` are fixed high-voltage analog inputs and
  are rejected for GPIO with an explicit message.

  Not yet supported on the U3: SPI and I2C, which on the T7 are driven through
  firmware registers that the U3 does not have. `lager instruments` advertises
  only the roles that have a driver behind them.

  Two differences from the T7 are deliberate and visible. The DAC range is
  0.04-4.95 V, not 0-5 V, and out-of-range values are refused rather than
  silently clamped by the hardware. And a U3 DAC has no readback at all, so
  reading one reports the value this process last wrote and errors if there
  is none -- reporting 0 V would be indistinguishable from a real measurement.

  Validated on a U3-HV (firmware 1.24) with DAC0 looped back to AIN0, DAC1 to
  AIN1 and FIO4 to FIO5: all 16 AIN channels, all 16 usable DIO, both DACs
  linear to within 20 mV across the full range, and the whole CLI path from
  `lager dac` through to `lager adc` reading the result back over the jumper.
  Two defects that only hardware could show were fixed in the process:

  - **The pin mux is written with `configIO`, not `configU3`.** They are
    different state -- `configU3` carries the power-up defaults, `configIO`
    the live mux -- and a mask written to `configU3` is accepted, reads back
    through `configU3` as though it worked, and leaves the pin in its old
    mode. Every flexible channel (`AIN4`-`AIN15`) failed with
    `PIN_CONFIGURED_FOR_DIGITAL`, and the memo then cached the ineffective
    write so the retry never happened.
  - **One device is one cache entry however it is named.** Entries are keyed
    by the serial the device reports, not the one the caller asked for. A U3
    reports no USB serial, so the scanner writes an empty serial slot and its
    nets resolve to "first found", while a record carrying the real serial
    names the same device a second way. Keying on the request made those two
    entries, and the second open then raced the claim the process already
    held and failed outright, in both orders, until `close_all`.
    A `None` request means "first found", which names one specific device
    rather than any device, so a box with two U3s still opens the free one
    instead of binding a serial-less net to whichever device another net
    happened to open.

  Both are covered by tests that fail if the fix is reverted. The `u3` test
  double now models `configU3` and `configIO` as separate state and raises
  `PIN_CONFIGURED_FOR_DIGITAL` for a flexible channel left digital -- without
  that split the whole suite passed with the mux bug in place.

  On a box carrying both families, note that LJM does not merely ignore a U3:
  asked for device type `"ANY"` it fails outright with
  `LJME_U3_NOT_SUPPORTED_BY_LJM` (1243) rather than skipping the device it
  does not support, so `ljm.openS("ANY", ...)` and `ljm.listAllS("ANY", ...)`
  stop working as soon as a U3 is plugged in. Every T7 path here names the
  device type explicitly and is unaffected; `read_adc(kind=...)` and the
  handle manager now say so where the default lives. The Exodriver build is
  pinned to `v2.7.0` rather than tracking upstream's default branch, which is
  currently ahead of its own newest tag.

### Changed

- **A second role on a dual-role instrument is now a notice, not a block.**
  Chips like the Keithley 2281S (battery or supply), the EA PSB pair (solar
  or supply) and the Rigol DP711 hid every remaining add row once any net
  was saved on them, treating a deliberate alternating-use setup — one
  battery net, one supply net, driven at different points in a test — as
  impossible. The drivers already make that setup safe: every write path
  re-asserts its own entry mode before touching the instrument, so driving
  one net simply ends whatever the other mode was doing.

  The TUI Add screen now shows the remaining role's row (unselected by
  default) with an informational notice explaining the mode switch, and
  `lager nets add` / `add-all` emit the same notice to stderr and proceed
  instead of refusing. Selecting both roles of a fresh chip in one batch is
  still rejected. The FT232H keeps its hard block: its MPSSE-vs-UART mode
  is fixed per open with no driver-side switching, so a second role there
  genuinely cannot work.

  Getting `add-all` there uncovered that it never reached these chips at
  all: its scanner-duplicate detector keyed offered channels per device
  only, so two roles legitimately sharing one physical channel (the 2281S
  offers channel "1" as battery AND as power-supply; the FT232H offers
  channel "0" per MPSSE role) read as "the box offered the same channel
  twice" and the whole instrument was silently skipped with a warning
  blaming the scanner. The detector now keys per device and role. With the
  chips reachable, a fresh dual-role chip offering several roles is refused
  with the same pick-one guidance mode-exclusive chips already got, rather
  than double-booked.

### Fixed

- **A box without a pigpio container now gets the default pigpio address
  instead of an empty one.** `start_box.sh` detected the address with

      PIGPIO_ADDR=$(docker inspect ... pigpio 2>/dev/null | tr -d '\n' || echo "172.18.0.2")

  where `||` tests the pipeline, and the pipeline ends in `tr`, which exits 0
  whether or not `docker inspect` produced anything. The fallback was therefore
  unreachable, and the container was started with `--env PIGPIO_ADDR=` (empty).
  The Python side does not recover it either: `os.environ.get('PIGPIO_ADDR',
  '172.18.0.2')` returns the empty string for a variable that is set and empty,
  so a default was applied at neither end. Measured on a box with no pigpio
  container: the value reaching the container was empty.

  The result is now validated as an address rather than merely non-failing,
  which also covers the second way the detection returns a non-address -- a
  pigpio container that exists but is not attached to `lagernet`, where the
  template renders the literal `<no value>`.

- **A UART net is no longer held indefinitely after its interactive client
  goes away.** `lager uart <net> -i` could leave the net stuck reporting
  "already in use by another session" on every subsequent invocation, with no
  cure short of restarting the box's container.

  The box reclaims a UART session by asking whether its read loop is still
  making progress, via a `last_activity` heartbeat. But that heartbeat is
  written *by the read loop itself*, so it proves the loop is iterating — never
  that anyone is still listening. A client that goes away leaves a perfectly
  healthy loop refreshing it forever: emits into an empty Socket.IO room are a
  silent no-op, so the session kept its per-net/per-device guard and its
  exclusive `flock` on the tty, and the 30s staleness bound was unreachable by
  construction. The 0.31.14 reclaim addressed a *wedged* reader and could not
  see this case.

  The specific way this became permanent: `start_uart` and `disconnect` are
  dispatched on different threads, so a client that opens a session and closes
  it again straight away can have its disconnect handler run first, find
  nothing registered, and return. `start_uart` then registers a session with no
  client and no cleanup path left. Reproduced on real hardware: still held 246s
  later with no
  sign of clearing, and the box log shows the disconnect landing 100ms before
  the registration it was supposed to clean up.

  The read loop now also asks the connection manager directly whether its
  client is still on the `/uart` namespace, and exits on the first iteration
  where it is not. Measured on the same box, the same race now releases the
  net in ~2ms. The check is fail-open: a socketio whose manager cannot be
  introspected reports "still there", so an unknown answer can never tear down
  a live session, and it is deliberately not consulted during a device
  re-enumeration, where a session is expected to sit and heal. This is the same
  fix RTT received in 0.36.0; the two now match.

  It does **not** shorten the case of a client that stops answering without
  closing its socket (host suspended, VPN dropped). The connection manager
  still reports that sid connected until engine.io's ping timeout, so the net
  stays reserved for ~85s regardless — measured 92s before, 89s after. Use
  `lager uart <net> --force` for that. (The equivalent RTT note in 0.36.0 says
  this check collapses the 85s window. It does not; that description is being
  corrected here rather than repeated.)

  Two CLI-side contributors to the same stranding are fixed with it. `stop_uart`
  was sent only on the normal exit path, so Ctrl+C skipped it and left the
  release to the socket.io disconnect alone — and teardown restored the terminal
  *before* disconnecting, so the user saw what looked like a returned prompt
  while the disconnect was still pending and would reasonably Ctrl+C the "hung"
  process, killing it. Teardown now runs on every exit path, socket first, and
  survives a second Ctrl+C. It also sends `stop_uart` whenever `start_uart` went
  out rather than only when the session came up, so a session the box registers
  just after the client stops waiting is not orphaned from birth; that wait grew
  from 5s to 15s, since the box will sit through a re-enumeration for up to 60s.

- **Editing a net's details in the Net-Manager TUI no longer discards the rest
  of the record.** The Edit Details dialog built a fresh record from the five
  fields it displayed plus the three it edits and sent that to
  `PUT /nets/<name>`, which replaces a net wholesale — so writing a description
  silently dropped `jlink_script`, `openocd_config`, `safety_limits`,
  `usb_identity`, `params`, `device_path` and `channel_key`. A debug net lost
  its script and a supply lost its ceiling, at the moment somebody documented
  it. The dialog now fetches the stored record and mutates it in place, which
  is what `lager nets describe` has always done.

- **`DebugNet.flash()` / `.erase()` now take the DA1469x QSPI flash-loader
  path on OpenOCD, matching the CLI.** On a DA1469x target behind an OpenOCD
  probe, `lager debug <net> flash` worked but the same operation through the
  Python Net API (`Net.get(..., NetType.Debug).flash(bin, 0x16000000)`) died
  with a bare `** Programming Failed **`, and `.erase()` silently never touched
  the external QSPI NOR. The DA1469x special case — mainline OpenOCD has no
  QSPI flash driver for the family, so the RAM-resident Apache Mynewt
  flash_loader must be driven instead of `program`/`flash_erase_all` — existed
  only in the HTTP service path (`service.py`), not in `debug_net.py`. Both
  methods now dispatch through the same `da1469x_loader` helpers with the same
  family predicate, so callers keep passing absolute XIP addresses exactly as
  on the J-Link path. Loader failures now raise a message naming the loader
  step that failed instead of a raw OpenOCD tcl traceback, and a flash that
  dies after its erase says the board may be left blank. Non-DA1469x OpenOCD
  targets and the J-Link backend are unchanged.

- **Lateness alone filed a new `bench-alert` issue every night.**
  `bench_schedule_check.py` appended its lateness finding to the same
  `problems` list as the gap and stale checks, and any non-empty `problems`
  exits 1 and fires `bench_alert.sh`. That script searches only for an **open**
  issue carrying the label, so the recovery job closing one on a green night
  guaranteed the next watchdog run created another rather than reopening it.
  With the mean delay sitting around 4.1h against a 3h threshold since a regime
  change on 2026-08-27, the steady state was one new issue per day for a
  condition that will still be true tomorrow -- and a `bench-alert` issue that
  is usually open for the boring reason is one nobody reads on the night it is
  open for a real one.

  The delay is GitHub's scheduled-event queue. Nothing in this repository can
  bound it, and `nightly-bench.yml` already says so. Lateness is therefore
  reported rather than alerted: `check_lateness()` returns warnings instead of
  problems, the tool writes `warnings.txt` and still exits 0, and the watchdog
  puts the trend in the run summary on every run while folding it into the
  alert body whenever something else fires -- which is when a reader needs it,
  because "the night is 5h late" is what makes a missed night ambiguous.

  Raising the threshold was the alternative and is worse: it silences the
  signal on exactly the nights it was built to catch, and re-mutes itself as
  the queue degrades further. The measurement is kept; only the paging is
  dropped. `TestLatenessIsReportOnly` fails if lateness reaches the problems
  list again.

- **The `authorized_keys` probe withdrew the operator's own SSH identities, so
  it could not answer for the boxes it exists to repair.**
  `key_installed_on_box` asks a box directly whether `lager_box` is in its
  `authorized_keys`, and offered that key with a lone `-i` so the query could
  authenticate at all. But `-i` replaces ssh's built-in identity list rather
  than adding to it -- the same defect fixed for `lager ssh` in v0.45.1 -- so
  naming `lager_box` withdrew `id_rsa`, `id_ed25519` and the rest. On a box
  authorized with one of those and not yet with `lager_box`, ssh had nothing
  usable to offer and the probe returned `None`, meaning "could not ask".

  Both callers read `None` as "do not fail". `lager update` prints `SSH key
  installed successfully! Future connections will not require a password.` on
  it, and `lager ssh-setup`'s post-install verification skips its error. So
  the check written to catch a silent no-op was itself silently skipped, on
  exactly the boxes being repaired.

  The probe now offers `lager_box` first and then each of ssh's default
  identity files that exists, through a `widened_identity_args()` helper that
  `lager ssh` shares -- one definition, so the two cannot disagree about what
  `-i` does. `probe_box_identity` is deliberately unchanged: it sets
  `IdentitiesOnly` on its keyed attempt because it has to isolate whether that
  particular key is the one being accepted.

- **`setup_battery(soc=0)` set nothing and said nothing.** The Keithley battery
  mapper guarded `soc` with `!= None` and then again with a bare truthiness
  test. `0` is falsy, so a state of charge of 0 fell through both the range
  check and `set_soc`: no exception, no log line, no return value, and the
  simulation kept whatever charge it already had.

  0 is the interesting end of the range for a discharge test, and it sits
  inside the range the neighbouring error message advertises -- so the message
  said 0 was acceptable while the code discarded it. Every other parameter on
  `setup_battery` (`voltage_full`, `voltage_empty`, `current_limit`, `voc`,
  `capacity`, `sim_mode`, `model`) is guarded by `!= None` alone, which is why
  the extra test read as belt-and-braces rather than as a behaviour change. The
  driver underneath always handled 0 correctly; only the mapper dropped it.

  This is a behaviour change, not a cleanup: `set_soc(0)` is now called where it
  previously was not.

- **The supply suite's bench-fixture note printed on passing assertions.** The
  unloaded-current checks in `test_supply_Rigol_DP821.py` annotate a failure
  with the fixture wired to the channel under test, so a red night does not
  cost the next reader a re-derivation of which channel goes where. `_record`
  takes one detail string and prints it on both outcomes, so the note went out
  on passes too -- three lines per CH2 run, on every nightly, explaining a
  fixture that was not causing anything.

  The note is now built only for the failing branch. Both call sites already
  compute the verdict on the line above, so this is a condition at the call
  rather than a change to what the note says. The note itself is kept: the wire
  cost three rounds of triage before anyone wrote it down, and the assertion
  that trips on it needs to name it.

- **`lager usb <net> cycle` reported "no device on this port" on ports that had
  one.** The message is box-side and the CLI echoes it verbatim. `USBNet.cycle`
  ended `return None` unconditionally and only the Plugable driver overrode it,
  so on an Acroname or YKUSH hub the box answered `None` for every cycle
  whatever was plugged in -- not an edge case on those drivers, but the only
  behaviour they had. Cycling four ports on a bench printed it every time while
  the devices behind them demonstrably re-enumerated, taking new USB device
  numbers across the window.

  It reads as an authoritative statement that the hub sees nothing attached,
  and it was taken that way during a hardware fault: it produced a written
  conclusion that an instrument "is not even asserting its USB data-line
  pullup", which nothing supported. During a fault a false "no device here" is
  close to the most expensive thing a tool can say, because it points the
  investigation at the device rather than at the tool.

  `cycle` now answers from the kernel's own USB topology, so every driver gets
  a real verdict without implementing one. The bus is sampled before the port
  is cut and again while it is dark: whatever left the bus in between is what
  that port carries, which is the only moment the question has an unambiguous
  answer. A device that returns reports `device re-enumerated`; one that does
  not reports the timeout; and "no device on this port" is now claimed only
  when the bus was actually readable -- a box that cannot read its own topology
  says so instead.

  Two consequences. A successful `cycle` now takes as long as the device needs
  to come back, up to 5s on top of the off-time, where it used to return
  immediately. And the MCP `power_cycle_hub` tool no longer pays a blind
  4-second sleep on every call: it waits only when nothing can be observed.

- **`lager logic <net> trigger spi` failed on a call to a method that did not
  exist.** The mapper's `set_trigger_data` reads the configured data width when
  the caller does not pass one, and the name it called -- `get_trigger_spi_width`
  -- was defined nowhere. The mapper's `__getattr__` forwarded it to the Device
  proxy, where it resolved locally and then 404'd on the box as
  `Function not found: get_trigger_spi_width`. It was the last remaining
  failure in the logic suite.

  The whole SPI trigger surface behind it was missing the same way, so the
  driver gains all of it: the three sources, the three trigger levels, the
  clock slope, the framing condition and its chip-select idle level, the
  framing timeout, the data width and the data value, plus the acquisition
  trigger status the settings object reports. Nineteen methods, each removed
  from `test/unit/box/mapper_undefined_baseline.txt` in the same change --
  that guard is two-sided and fails if an implemented name is left behind.

  **Every query was confirmed against the instrument rather than against the
  programming guide.** That distinction is load-bearing: a node can be
  accepted, report `0,"No error"`, and still never answer, in which case the
  read times out and no respelling helps. Four plausible spellings behave
  exactly that way on an MSO5074 and are deliberately not used. The three
  level nodes were told apart by writing distinct values and reading them
  back, rather than inferred from their names.

  The logic suite gains four checks driving the new nodes. `trigger spi` with
  no arguments passed while ten of these were undefined, which is how the gap
  stayed invisible until a hardware run hit it.

- **A LabJack that is not a T7 no longer lands on the T7's code paths.** Three
  places asked "is this net a LabJack?" with a substring test and then assumed
  LJM. Each failed quietly rather than loudly: a DAC net would be handed the T7
  driver and, on a box with both models, write `DAC0` on the wrong instrument
  and report success; `/nets/state` would batch-read it through LJM register
  names; and the shared device lock collapsed both models onto one key for any
  net saved without an address, which LabJack nets routinely are. The T7, a
  bare `t7`, and an empty instrument keep exactly the behaviour and the lock
  identity they had.

## [0.45.1] - 2026-09-02

### Changed

- **`secure_box_firewall.sh` no longer claims an outcome it cannot deliver.**
  The script ended a successful run with `[OK] External access blocked for
  Lager services`, and `SECURITY.md` says the opposite: UFW governs traffic to
  the host, and it does not filter the ports the box's containers publish,
  because Docker installs its forwarding rules ahead of the host chain. Both
  statements described the same deployment and the script's was the wrong one.
  An operator reads that line last, so it is the impression they keep, and the
  policy correcting it lives in a file they have no reason to open at that
  moment.

  The script now reports what it configured rather than what it achieved --
  `[OK] Host firewall configured for Lager services` -- and closes with a note
  stating the limit and pointing at the Security Model section of
  `SECURITY.md`. The same overstatement is corrected in the script's own
  Security Model header, in the progress line that announced it was "blocking"
  Lager services from external networks, and in the deployment reference.

  No rule the script writes changes. Whether a rule then governs a
  container-published port is a separate question, tracked separately.

### Fixed

- **`lager ssh` refused boxes that a plain `ssh` reached.** When
  `~/.ssh/lager_box` exists, `lager ssh` passes it with `-i` so a box that
  authorizes only that key connects without a password. But `-i` does not add
  to ssh's identity list; it replaces it. ssh's own defaults -- `id_rsa`,
  `id_ecdsa`, `id_ed25519` and their `-sk` variants -- were no longer offered,
  so a box authorized with one of them and not with `lager_box` answered
  `Permission denied (publickey)` even though `ssh user@box` worked. A stale
  or never-installed `lager_box` key thus locked `lager ssh` out of every box
  the user had set up with `ssh-copy-id`, and the only cure was deleting the
  key.

  `lager ssh` now offers `lager_box` first and then each of ssh's default
  identity files that exists on the machine, in the order ssh would have tried
  them, so both kinds of box connect. Nothing else about the session changes:
  `~/.ssh/config` identities and agent keys remain on offer, and with no
  `lager_box` key present the command still passes no `-i` at all. The
  non-interactive commands (`install`, `uninstall`, `box-config`) already
  probed with the key and retried without it, and are unchanged.
  `test/unit/cli/test_box_ssh_identity.py` pins the order.

- **The instrument scan wrote G-code into serial ports it did not own,
  including live DUT consoles.** A `lager uart` session would periodically
  receive the literal text `M105`, which the DUT echoed and answered with
  `Error: Unknown command: M105`. `M105` is the handshake the box uses to find
  a Dexarm robot arm: `_by_handshake` globbed every `/dev/ttyUSB*` and
  `/dev/ttyACM*` and wrote to each one not in an exclusion set. It opened
  without `exclusive=`, so pyserial skipped its flock branch entirely and
  opened straight through the lock a live session was holding -- flock is
  advisory and only arbitrates between processes that both take it.

  The exclusion set had three holes of its own. It was an allowlist of
  *recognized* hardware, built from `scan_usb`, which only emits entries for
  VID:PIDs in `SUPPORTED_USB` -- so a CH340, an FT230X, a vendor CDC bridge or
  any other unlisted USB-serial chip was invisible to it and got written to.
  It read `tty_path`, the primary interface only, leaving channels B/C/D of an
  FT2232H/FT4232H unprotected while channel A was excluded. And it never
  consulted saved nets at all, so a net's own console port was not excluded
  unless the scan happened to recognize its adapter.

  The handshake is now gated on the Dexarm's `0483:5740` -- a pair the code
  already hardcoded when synthesizing the arm's address, but never used to
  decide whom to write to -- resolved through the existing sysfs cable
  enumeration and failing closed, so a tty whose identity cannot be
  established is not written to. The exclusion set gains every tty of every
  multi-interface chip and every tty owned by a saved `uart` net, including
  legacy nets pinned to a bare `/dev/tty*` that carry no durable USB identity.
  Ports are opened with `exclusive=True` and skipped when held. What keeps the
  probe away from a board wired for DTR/RTS auto-reset is that gate rather than
  the line state: in the default mode a port that is not already a Dexarm is
  never opened at all, which is strictly stronger than opening it with a line
  held low. DTR is asserted, because a CDC-ACM device gates its transmitter on
  it and the arm is silent without it; RTS is not needed and stays low.
  `LAGER_ARM_PROBE` sets `auto` (default), `off`, or `force`; `force` widens the
  VID:PID gate only and keeps every other guard.

  The exclusion set is durable, not a function of who happens to be connected:
  a saved uart net's ttys are excluded whether or not a session is open on them,
  so the port a DUT console lives on is protected while it sits idle as well as
  while it is in use. The exclusive open is a second layer under that, not the
  thing carrying it -- which matters because the failure being fixed here is a
  scan that ran between sessions as readily as during one.

  Bench-validated on a four-channel FTDI (`0403:6011`) whose uart net sits on
  interface 2, alongside an unrelated CDC device absent from `SUPPORTED_USB`:
  six stray `M105` arrived in a live session across five scans before, none
  across ten after, with every one of the adapter's four ttys reported as owned
  and the unrecognized CDC device refused by the identity gate. The instrument
  list was unchanged. Separately validated against a real arm: it is still
  detected, and `lager arm <net> position` still answers over a saved arm net.

- **An attached Dexarm was not detected at all, for two further reasons.** Found
  while validating the above against real hardware. The arm answered the
  handshake and was then discarded, because `_get_serial_by_port` resolves the
  USB serial with `udevadm info`, which reads the udev runtime database -- a
  container without `/run/udev` mounted has none, so udevadm returns the
  device's `P:`/`M:` lines and no `E:` properties at all, and every
  `ID_SERIAL*` key the code looks for is simply absent. It now falls back to
  the serial sysfs already carries, which the cable enumeration has read
  anyway. Separately, the probe slept a fixed 10ms and then did a
  non-blocking `read_all()`; the arm replies in more than that, so the read
  raced hardware that was about to answer. It now blocks on `read_until`,
  bounded by the port's existing one-second timeout.

  Two supporting fixes. The probe's `@with_timeout(seconds=10)` is
  SIGALRM-based and a documented no-op off the main thread, so under the
  threaded HTTP server -- the path that actually serves `/instruments/list` --
  it had no overall cap at all; the loop now enforces the same budget
  directly. And both `/instruments/list` handlers now log the requester: the
  scan is never cached, so every request is a full re-probe, and this is the
  only record of which caller caused a given write.

  `test/unit/box/test_usb_scanner_custom.py` pins all of it, including that a
  real Dexarm is still detected -- the gate must not filter out the hardware
  it exists to find.

- **The bench watchdog alarmed on a late night as though it were a missed
  one.** `bench-watchdog.yml` carried its own copy of the thresholds
  `tools/bench_schedule_check.py` reads, and the two drifted when the tool was
  rewritten from interval-based to lateness-based checking. Two defects fell
  out. `SCHEDULE_GAP_ALERT_HOURS` was set to 26, overriding the tool's 36 -- the
  gap check exists to catch a night that certainly did not run, which is why its
  threshold sits well above the 24h nominal, and at 26h it fired on a 26.7h
  night that had merely started late. `SCHEDULE_DRIFT_WARN_HOURS` was still set
  and read by nothing, the check it configured having been replaced by one
  reading `SCHEDULE_LATENESS_WARN_HOURS`; two further names the tool reads were
  never set and ran on defaults.

  Every threshold now lives in the tool alone, where each is documented with its
  reasoning, and `test/unit/test_bench_watchdog_env.py` fails if one reappears
  in the workflow. Nothing could catch this before: the tool's tests pass
  thresholds explicitly and never read the workflow, and neither `zizmor` nor
  `actionlint` checks that an `env:` name is one the consuming script reads.

- **Seven range checks in the instrument mappers rejected nothing.** Each was
  written with its bounds inverted -- `if 4 > bits > 32:` -- which Python
  chains into `4 > bits and bits > 32`. No number satisfies both, so the
  branch was dead and the `raise` under it unreachable, while the message
  promised a range. An out-of-range width passed validation and reached the
  instrument; what happens there is not established, because the write may be
  clamped, rejected silently, or accepted into a state the caller did not ask
  for.

  Corrected to `not (LO <= x <= HI)`, the form `rigol_mso5000.py`'s
  cursor-position checks already use, at: the UART trigger data width, the I2C
  trigger address width and data byte width, the SPI trigger data width, the
  UART and SPI bus data widths, and the Keithley battery state-of-charge.
  Three of the messages said only "is not a valid value" and now name the
  range, as their siblings already did.

  `test/unit/box/test_mapper_range_checks.py` pins both halves: every site
  rejects at each end and still accepts a valid value, and a tree-wide scan
  fails on this shape anywhere in `box/` or `cli/` -- including in a
  validation nobody has written yet.

- **`tools/check_coverage_counts.py` reported a failing test suite when the
  real problem was a missing pytest plugin.** The checker runs each suite with
  `--timeout=60`, which needs `pytest-timeout`, and nothing a contributor can
  install declares it -- `test/requirements-unit.txt` names neither pytest nor
  the plugin, and CI installs the pin inline. On an environment built from the
  repo's own files every suite therefore died at argument parsing, and the
  checker printed `suite FAILED:` followed by a suite name. That suite passes
  when run by hand. Four fresh environments hit it in one day, each costing a
  few minutes to work out that the tests were never the problem.

  pytest rejects an unrecognized argument before collecting anything, exits 4,
  and writes the reason to stderr -- which the checker captured and discarded,
  so the misleading headline had an empty stdout tail underneath it. The
  checker now recognizes that case and names the plugin and the install
  command instead, and both of its failure paths print stderr as well as
  stdout, so a failure explained only there still reaches the reader.

## [0.45.0] - 2026-09-01

### Changed

- **`SECURITY.md` gains a Threat Model, and stops recommending the host
  firewall as the boundary.** The policy told operators to verify UFW as the
  control restricting access to a box. UFW governs the host, but it does not
  filter the ports the box's containers publish -- Docker installs its
  forwarding rules ahead of the host chain -- so a published service port is
  reachable from anywhere that can route to the box whatever `ufw status`
  reports. The guidance now says to treat network reachability as the boundary
  and put the box on a VPN or isolated LAN.

  The new Threat Model section records what is deliberate rather than
  overlooked: that `POST /python` and the breakpoint console run user code
  because that is the product, that box error strings are the diagnostic
  surface and are not genericised, that paths built from client-supplied names
  are contained at each join, and that a path *received* as a parameter is
  checked on entry even though static analysis cannot credit it. It exists so
  an accepted finding has a written reason rather than a bare dismissal.

- **Whether the bench has a Keithley 2281S is now a repository variable.** Its
  USB device port stopped presenting on 2026-08-31: the instrument powers up and
  boots, and its AC relay switches audibly, but it never reaches the USB bus. That
  is not the bench -- two hub ports across two segments were tried, the cable was
  swapped, and the same hub cold-booted three other devices on demand during the
  same session.

  While it was listed unconditionally, every nightly failed at instrument power-on
  and skipped every suite, including those for the Rigol DP821, which is healthy.
  `KEITHLEY_PRESENT` now gates its entry in the expected inventory in all three
  bench workflows, and every suite that drives the instrument, so one variable
  restores it the day it comes back rather than several edits in several places
  that can drift apart. It is currently unset, which is what a bench without that
  instrument should say.

- **The undefined-method guard now covers the whole MSO5000 mapper, not just
  its logic surface.** The previous version walked `net.py`'s Logic branches and
  filtered mapper calls to names containing `la`, which is why it did not see
  `get_trigger_spi_width` -- called by the analog mapper, and found only when a
  hardware run hit `Function not found: get_trigger_spi_width`.

  Widened to every mapper class, the walk finds **115** names that no driver
  defines, across the trigger-settings and bus-decode surfaces. Each is a
  command that fails at runtime and cannot fail earlier, because the mapper's
  `__getattr__` forwards any name to the box and the only real check there is a
  bare `hasattr`.

  They are recorded in `test/unit/box/mapper_undefined_baseline.txt` so the
  widened guard can land without turning a required context red. The check is
  two-sided: a new undefined name fails it, and so does a baselined name that
  has since been implemented but not removed, so the list ratchets down and
  cannot rot. Both directions are exercised by the suite.

- **The supply suite now captures evidence when its unloaded-current check
  fails, instead of only reporting the number that failed.** That check has gone
  red twice on a channel which measures a clean `0.0000 A` whenever anyone looks,
  and the previous attempt at a fix -- waiting for the output to reach regulation
  rather than sleeping a fixed interval -- did not hold. It failed again
  afterwards at both assertion sites.

  A hand-run probe did not reproduce it either: five cold enables on that
  channel, 189 samples, every one exactly `0.0 A`, with the regulation wait
  returning `0.0` each time. That is worth recording on its own, because it is
  much stronger support for "the channel draws nothing in steady state" than the
  sweep the bench README cites. **Raising the threshold for this channel would
  therefore hide a real anomaly rather than accommodate a known load.**

  So this change deliberately does not adjust the assertion, the threshold or
  the settle. The cause is not known, one fix has already been shipped against a
  cause that turned out to be wrong, and the event is too rare to chase by hand
  at roughly one failure in three runs. Instead, a failure now prints:

  - ten consecutive `current()` reads with timestamps. Each is a fresh
    `:MEAS:CURR?`, so a run of byte-identical values is a register that is not
    being reacquired.
  - `voltage()`, `power()` and `state()`.
  - one `measure()` call, which is a single `:MEAS:ALL?` acquisition. If that
    triple disagrees with the `current()` reads beside it, the two paths are
    seeing different samples.

  That distinction is the open question. The failing runs report a
  self-consistent `5.0 V / 0.24 A / 1.2 W` triple across four reads inside 22 ms,
  and `0.0000` three seconds later, which a live measurement does not easily
  explain and a stale one does.

  The capture is best-effort throughout: every read is guarded and nothing in it
  raises, because a diagnostic that can fail the suite it is diagnosing is worse
  than no diagnostic. Both a latched-reading stub and a stub whose every call
  raises are exercised against it.

### Fixed

- **The supply suite read an unloaded channel while it was still discharging,
  and called the transient a steady load.** The nightly's Rigol DP821 CH2 check
  had been failing intermittently since 2026-08-14 with readings of 0.11 A,
  0.18 A and 0.24 A against a 0.1 A limit, on a channel that measures a clean
  0.0000 A once settled -- 96 samples across four setpoints, no spread.

  The settle helper waited for the output to reach its setpoint and then for
  the current readback to stop moving, and it treated two consecutive reads
  agreeing to within 5 mA as stopped. The failure-time capture added last
  release fired for the first time and showed why that is a different claim:
  the reading held 0.17 A across two reads 60 ms apart and was 0.0 by 130 ms,
  so the pair agreed with each other while the output was still discharging
  into the ADC input wired to that channel. `power()` already read 0.0 and the
  atomic `measure()` reported 0.00 at the same moment, so the two readback
  paths disagreed by an entire transient. A pause is not a settle.

  The reading must now hold across four consecutive samples, a window longer
  than any plateau measured on this bench, and a reading that pauses and then
  moves restarts the count. The threshold is unchanged: at 0.1 A the assertion
  was always right, and raising it would have hidden a real anomaly rather than
  accommodated a known load. A genuine steady draw reads the same value at
  every sample, settles at once, and still fails the assertion -- which the
  helper must never decide for its caller, and which is now pinned by tests
  that replay the captured transient.

  The current-limit readback check got the same treatment; it had a flat 0.2 s
  sleep against a readback its own assertion reads. And if the check ever trips
  again on a channel declared as wired, the failure now names the fixture
  instead of leaving the next reader to re-derive it.

- **Both bench fixes above reached one of the three workflows that needed them,
  and the nightly stayed red.** The nightly runs Box Lifecycle first and the
  instrument sweep second, as separate workflows. The Keithley gate and the relay
  retry were applied to the sweep only, so the lifecycle job still waited for an
  instrument that is off the bench, still failed at power-on after 180 seconds,
  and still skipped the sweep that carried the fix. A run dispatched against the
  very commit that added the gate failed with `Did not enumerate within 180s of
  relay power-on: Keithley_2281S`. The weekly extended bench had the same two
  gaps and would have failed the same way on its next Saturday.

  All three workflows now run a byte-identical power-on block. That was already
  the stated intent -- one of them carried a comment asking the next reader to
  keep the copies in sync -- but nothing checked it, which is precisely how a fix
  came to be written, reviewed, merged and still absent from the job that runs
  first. `test/unit/box/test_bench_power_on_blocks_match.py` now compares the
  three and fails `unit (box)` on any drift, and separately refuses to let any of
  them name the Keithley unconditionally.

- **The bench watchdog reported green while the nightly schedule decayed, and
  again on a night that did not run.** It asked one question -- is the newest
  scheduled run more than 26h old? -- sampled whenever its own six-hourly cron
  happened to fire. Nominal spacing is 24h, so that left two hours of headroom
  checked at four arbitrary offsets a day, and GitHub's scheduled-event queue
  spends it.

  Spacing turns out to be the wrong measure. Across the ten scheduled runs to
  2026-09-01 the intervals ranged 17.9h to 33.6h and averaged 24.47h against a
  nominal 24h -- almost no signal -- because a night that starts late shortens
  the next interval and the average repairs itself. Measured against the cron
  instead, the same ten runs read 0.4h, 0.5h, 0.6h and 0.6h late, and then
  4.6h, 4.8h, 4.6h, 7.3h, 10.2h and 10.9h. The schedule turned on 2026-08-27
  and nobody saw it for four days.

  The watchdog now checks three things that mean different things: an interval
  large enough that a night was certainly skipped, nothing scheduled arriving
  at all, and the mean delay against the cron -- which is the leading indicator,
  because once a night is hours late, late and missed stop being
  distinguishable until it either arrives or does not. The cron is read from
  `nightly-bench.yml` rather than copied, so the two cannot drift.

  The arithmetic moved to `tools/bench_schedule_check.py` with
  `test/unit/test_bench_schedule_check.py` behind it. It was wrong for as long
  as it was inline YAML with nothing able to test it, and one of the new tests
  pins the specific blindness: a schedule that slips a fixed amount and then
  holds is spaced at exactly 24h while every run is hours late.

  `nightly-bench.yml`'s own comment also implied the queue delay was bounded at
  the 78 minutes once observed. It is not, and it now says so.

- **A hardware-service self-restart could fail the whole nightly at its first
  command.** The service exits by design when it finds an orphaned USB claim, so
  the supervisor can respawn it with a clean USB context, and it is unavailable
  for about four seconds while that happens. The two relay writes that open the
  bench run were bare under `set -e`, so the night's first hardware command was a
  coin flip against that window -- run 33318035290 lost it, lifecycle failed,
  integration was skipped, and the night produced no instrument coverage at all.
  The enumeration loop directly below those calls already tolerated transients for
  the same reason; now the writes do too, in all three bench workflows. The relay
  level latches in LabJack hardware, so a retry is idempotent.

- **The Architecture page said three things about the box that are not true, and
  a user hitting `[Errno 16] Resource busy` had nothing to read.** Drawing the
  box-internals diagram in Mermaid last time forced explicit arrows, and the
  arrows asserted a topology nobody had checked against the source. Checking it
  now:

  - **`NetsCache` is per-process, not per-box.** It is a singleton keyed on a
    class attribute and a `threading.Lock`, so the guarantee is one per
    interpreter. The box API, the hardware service, the debug and MCP services,
    and every `lager python` subprocess each hold their own copy of
    `saved_nets.json`. The diagram drew one, inside `:8080`. Each copy
    invalidates on mtime, so they converge on their own -- but each pays its own
    first read, two can briefly disagree, and a restarted service comes back
    cold. That is now a note rather than more boxes in an already busy diagram.
  - **Dispatchers do not run in the hardware service.** `:8080` imports no
    dispatcher; it resolves a driver module by name, instantiates it, and calls
    the method. Net resolution happens in the *caller's* process -- the box API's
    handlers, or the user script -- which is also where its `NetsCache` copy
    lives. The diagram put dispatchers behind `:8080`, and the CLI sequence
    diagram had the box API posting to the hardware service before the net was
    resolved, which is backwards.
  - **A user script does not drive every instrument in-process.** Supplies,
    scopes, battery simulators, e-loads and solar simulators are all reached
    through the same `/invoke` proxy the box API uses, so the hardware service
    keeps sole ownership of the VISA session. Only the direct-USB drivers
    (LabJack, USB-202, FT232H, Aardvark, Joulescope, PPK2) are constructed in the
    subprocess, and the execution service releases the hardware service's claims
    on those before spawning -- while deliberately leaving the VISA sessions
    open, because tearing them down is what produced `[Errno 16] Resource busy`
    in the first place.

  The last correction supersedes a claim from the previous pass: the calls
  between services are `9000 -> 8080` **and** `subprocess -> 8080`, not `9000 ->
  8080` alone.

  Troubleshooting gains an entry for `[Errno 16] Resource busy`. The string
  appeared nowhere in the published docs except release notes, so the one failure
  the box's own source comments call out by name had no page a reader could land
  on. It says which paths cannot produce it, which can, and points at
  `lager diagnose`, whose VISA section already reports a held shared session
  correctly.

- **`lager logic <net> disable` asked the scope a question it never answers.**
  `Net.disable` polls all sixteen digital channels through
  `is_la_channel_enabled`, which queried `:LA:DIGital<n>:DISPlay?`. That is the
  symmetrical-looking partner of the write the enable path uses, and on an
  MSO5204 it is not answerable: the read blocks for the full VISA timeout and
  `:SYSTem:ERRor?` afterwards reports `0,"No error"`, so the instrument accepted
  the header and produced no response. Sixteen channels meant sixteen timeouts
  before the command failed.

  Measured on hardware, the rest of the subtree behaves the same way --
  `:LA:DIGital0:POSition?` also hangs -- while `:LA:STATe?`, `:LA:ACTive?`,
  `:LA:SIZE?` and `:LA:POD<n>:THReshold?` all answer in about 20 ms. Treat
  `:LA:DIGital<n>:` as write-only.

  The state is readable, just not there: `:LA:DISPlay? D<n>` returns it in 20 ms,
  and that is what the method asks now. The automatic "switch the analyzer off
  once the last channel goes" behaviour is kept rather than dropped, and the
  sixteen-query loop costs about a third of a second.

- **`lager logic <net> enable` and `disable` called ten driver methods that
  were not defined anywhere, so they failed on every instrument.** The issue
  found three. Walking the call sites found seven on `Net.enable` / `Net.disable`
  (`is_la_enabled`, `enable_la`, `enable_la_channel`, `set_la_active_channel`,
  `disable_la_channel`, `is_la_channel_enabled`, `disable_la`) and three more
  the logic mapper calls (`set_la_threshold`, `set_la_display_position`,
  `set_enabled_channel_size`). The MSO5000 driver defined eighty methods and
  none of them was logic-analyzer related.

  All ten are implemented, plus three read-backs, against `:LA:STATe`,
  `:LA:DIGital<n>:DISPlay`, `:LA:ACTive`, `:LA:POD<n>:THReshold` and `:LA:SIZE`.
  A digital channel index is range-checked to D0-D15, and deliberately does not
  use the `channel or self.channel` idiom the analog methods use: D0 is valid and
  falsy, so `or` would silently redirect a request for D0 to the net's channel.

  **Nothing local could have caught this, which is the more interesting half.**
  A net's device sits behind two chained catch-all `__getattr__` methods, so
  every attribute appears to exist on the caller's side and the call still fails
  on the box, where the check is a bare `hasattr` on the driver. `hasattr` guards
  written against that device therefore cannot be False. The new tests do not
  mock the device: they read `net.py`'s Logic branches and parse the mapper to
  recover the names actually called, then assert each one resolves on the driver.
  A guard test asserts the walkers matched something, so they cannot pass by
  finding nothing.

- **`LogicDisplaySize.Medium` set the display to large, and `Large` set it to
  medium.** The two enum members had their command strings crossed. The second
  element of each pair is the abbreviated form the instrument echoes, so reads
  were wrong in the same direction as writes.

- **`lager logic` reported success after a box-side failure.** The worker
  functions returned nothing, so an error from the box printed a traceback and
  the command still exited 0. They now report the failure on stderr and exit
  non-zero. Two related holes closed with it: an unknown net name fell through to
  success without printing anything, and four `hasattr` guards that can never be
  False read as safety checks while doing nothing.

  **Not yet validated on hardware.** Every SCPI string here is written from the
  programming guide and has not been sent to an instrument. The unit tests pin
  what each method emits, not what the instrument does with it.

- **`POST /debug/connect` now validates the port overrides it accepts, and
  answers `400` rather than `500` for a malformed request.** `gdb_port`,
  `swo_port`, `telnet_port` and `rtt_telnet_port` were forwarded from the
  request body exactly as they arrived, and they are used to build the debug
  backend's command line. Each is now coerced with `int()` and range-checked to
  1-65535 at the boundary, and a request carrying anything else is refused with
  a message naming the field. An absent key still means "no override" and takes
  the slot allocator's value; a numeric string still works. An explicit `null`
  is now named at the boundary instead of failing later inside port arithmetic,
  and `true`/`false` no longer coerce to port 1. The status code matters
  because the CLI keys its "update the box" hint on the shape of a failure, so
  a bad request must not look like a broken box.

- **A probe serial the debug backend cannot bind to is now refused instead of
  used.** The VISA parser accepts any run of non-colon characters in the serial
  slot, so a value arriving there is not necessarily a serial. The admissible
  set — letters, digits, dot, underscore, hyphen — now lives in
  `debug/probes.py` as `BINDABLE_SERIAL_RE`, and both backends check it before
  building a command line. `/debug/connect` checks it at its boundary too, so a
  malformed request is answered `400` before anything is stopped or started,
  with the backend checks left as the backstop for other callers. Every serial
  in the field already satisfies this, so no live probe changes behaviour, and
  the pidfile and logfile helpers have named their files after the same set
  since 0.43.0.

- **`GET /download-file` builds its `Content-Disposition` from the path it
  resolved.** The header previously interpolated the raw query parameter. It
  now uses the basename of the allowlist-checked path, with the three
  characters a header value cannot carry — carriage return, line feed and
  double quote — reduced to `_`; a filename may legally contain all three.
  Spaces and everything else are untouched, so a download keeps the name the
  user recognises. The `:9000` twin already delegated escaping to Flask and is
  unchanged.

- **A detached job's registry directory is now checked to be inside the
  registry at the point it is built.** `/python/attach`, `/python/continue`,
  `/python/breakpoint` and `/python/kill` name that directory after a
  `lager_process_id` taken from the request body. Each already parses it as a
  UUID first and that remains the real defence, but the join itself lived in a
  shared helper, so nothing local to those handlers said where the result was
  allowed to land. Each now joins under `PROCESS_REGISTRY_DIR` and refuses a
  result outside it, and `process_dir_for` carries the same check for the
  callers that build an id rather than receiving one. No path changes for a
  valid id.

  The repetition is deliberate, for the reason `box/lager/util/paths.py`
  records: a containment check is only credited to the function that performs
  the join, so folding these four back into the helper would leave the code
  correct and the analysis blind.

- **A net's J-Link script and OpenOCD cfg are now checked to be inside the
  debug runtime directory at every point one is built, opened or removed.**
  Both are named after a net name that comes from user config. `_net_slug`
  reduces it and remains the real defence, but the joins lived in
  `script_path_for_net` / `config_path_for_net`, so none of the functions that
  actually write, read or delete those files said where the result was allowed
  to land.

  The root moves to `debug/probes.py` as `RUNTIME_DIR` — the lowest layer the
  debug modules share — and the two path templates split into that root plus a
  basename, so the check has a constant to name. Builders, the two `clear_*`
  functions and every site in `debug/service.py` now join under it and refuse a
  result outside it. `debug/jlink.py` and `debug/gdbserver.py` gained the same
  check on the script path they accept; `chip_erase` and `flash_device` did
  too, where previously any path with a `None` default was accepted.

  No path changes for any valid net name.

- **`debug/jlink.py` keeps its own copy of that root, and a test now pins it.**
  Three tests load that module standalone so the box suite need not import
  pyvisa and the hardware drivers to check argv assembly, which means the
  module cannot import the shared constant — not from `.probes`, and not from
  `lager.*` either, since that executes `lager/__init__.py`. The duplication is
  deliberate; `test_debug_script_root.py` fails if the two drift, and also if
  someone reintroduces an import that would break the standalone load.

- **Paths named after a client-supplied value are now checked to be inside
  their own directory at the point they are built.** Three places do this: a
  binary name on `/binaries/add` and `/binaries/remove`, a VISA address used as
  a device-lock key, and the firmware staged for a DFU run.

  `_validate_name`, the lock-key slug and the DFU suffix pattern each stay as
  they are and remain the real defence. What was missing is local: the joins sat
  in helpers, so the functions that open, chmod or remove those files said
  nothing about where the result was allowed to land. Each now joins under its
  own root and refuses a result outside it, and the DFU staging file is checked
  against the directory `tempfile` actually used before it is removed.

  No path changes for any valid input. A binary name with a space, a `+` or
  parentheses still works, which the new tests pin -- the accepted set has to
  stay wider than the check's own character needs, because the CLI forwards the
  basename of whatever local file it was given.

- **`serial_id` checks the sysfs path it builds from a caller-supplied tty.**
  `identity_for_tty` is reachable from a net-save payload, and reduces its
  argument with `basename(realpath(...))` before joining it under
  `/sys/class/tty`. That reduction is what prevents an escape; the containment
  check now states it where the join happens. The helpers it calls walk outward
  into `/sys/devices` by design, so the check belongs at that entry point rather
  than in each of them.

- **A failed `/invoke` no longer returns the box's stack trace to the caller.**
  Three error paths on the hardware service put `traceback.format_exc()` in the
  response body alongside the message. The message stays -- it is the diagnosis
  the CLI shows, and `cli/core/net_helpers.py` renders it verbatim -- but the
  trace now goes only to the box log, which is where the adjacent
  `logger.error` was already sending it. Nothing rendered the `details` field
  at a user: `nets/device.py` documents it as log-only and its `__str__`
  returns just the message. These were the only three places in `box/` where a
  trace reached a response.

## [0.44.0] - 2026-08-28

### Added

- **A release-note template, and a partial convention to hold it.**
  `docs/source/release-notes/_template.mdx` carries the section order and the
  STYLE.md rules that apply by hand, because the release-notes archive is
  deliberately outside the gate: a note records what shipped on a date, and
  editing one makes the archive disagree with itself. `tools/check_docs.py` now
  treats an underscore-prefixed page as a partial and exempts it from the nav
  and release-notes checks, which is Mintlify's own convention and the one case
  where "not in docs.json" is the intent rather than the defect.

- **The Net-Manager Add screen now makes LabJack I2C/SPI default pins
  obviously changeable.** Users read `Ch: FIO4-FIO5` on an available I2C net
  as a fixed assignment and didn't discover the pin-picker dialog behind the
  Add button. Three changes, all in the TUI:
  - The Add screen carries a hint line saying the pins can be changed.
    Warnings and the hint render as compact single-line notices in one
    block with a `✕ Dismiss` button, so they can't crowd the net list out
    of view.
  - A LabJack I2C/SPI row's `[✎]` button now opens a combined editor —
    name field plus the pin dropdowns — instead of the rename-only dialog,
    so the net can be fully configured while selecting nets instead of
    only after pressing Add. Nets edited this way aren't re-prompted for
    pins during the add; rename validation is shared with the plain
    rename dialog, which all other net types keep.
  - The pin dialog now prefills with the net's current selection rather
    than always the defaults, and reverting a customized net back to the
    defaults restores the original scanner record (legacy channel string,
    no params) byte-for-byte.

- **The Rust API reference now mirrors the Python one, page for page.** The Rust tab
  was five pages against Python's twenty-seven, and a single 76-line `net-types.mdx`
  table row was the whole counterpart to Python's twenty-four per-instrument pages.
  A reader got one line where the Python reader got a method reference.

  The tab is now 31 pages in the same six-group taxonomy the Python and CLI tabs
  already use: a page per net type (supply, battery, solar, eload, watt, energy,
  scope, adc, thermocouple, gpio, dac, i2c, spi, usb, uart, ble, wifi, blufi, router,
  arm, webcam), the debug surface split into debug, rtt and dfu, and new client,
  errors and async pages. `debug-and-uart.mdx` is retired into `debug`, `rtt` and
  `uart` with a redirect.

  Every page documents the timeout budget, box-version floor and gotchas for its net
  type, none of which were published anywhere before. **All 149 Rust examples across
  the 31 pages are compiled against `lager-net` 0.4.0**, and the API was exercised
  against real hardware on a box running 0.43.0 first, so the return shapes and error
  strings are observed rather than transcribed. Two examples were wrong and were
  caught by that compile pass: `tokio::try_join!` over inline handle constructors does
  not borrow-check, and `std::fs::read(..)?` cannot convert into `lager::Error`.

  Behavior worth calling out, all verified on hardware and previously undocumented:
  `flash()` on a `.bin` infers the STM32 base `0x08000000` and **returns `Ok(())`
  while writing nothing useful** on any other family, so `flash_bin()` is mandatory
  there; `erase()` drops the debugger connection, so a following `read_memory()` fails
  until you reconnect; a per-net safety ceiling caps `set_voltage`/`set_current` but
  **not** `set_ovp`/`set_ocp`; a `bleCommand`/`wifiCommand` capability flag means the
  route is registered, not that the box has BlueZ or `nmcli`; and `state()` returning
  `Ok` does not mean the instrument answered -- check the `error` field.

  `RttStream` is documented as yielding raw HTTP chunked-transfer framing rather than
  clean payload, with interactive RTT recommended instead. Tracked upstream as
  lagerdata/lager-rs#5.

  The Python overview now links across to the Rust SDK, which nothing in the Python
  tab did before.

- **A Python API page for `NetType.Router`.** A router net drives a MikroTik
  access point over its REST API, and its methods include the bench's only
  network fault-injection tooling -- `block_internet`, `block_dns`,
  `block_port`, bandwidth limits and DHCP control -- which is how a test asserts
  what firmware does when the network degrades rather than disappears. None of
  it was documented, and `NetType.Router` appeared nowhere in the docs.

- **`tools/check_docs.py` gates docs against the shipping CLI**, wired into
  `static-checks.yml`. It fails on a dangling nav entry, an unpublished page, a
  release with no notes, a command with no page (or a page for a hidden
  command), and an options table naming a flag no click param declares.

- **A guide for running Lager from CI.** Covers non-interactive sign-in,
  registering the box, and the two checks worth failing a pipeline on: that the
  box is reachable, and that it is running the commit under test. `lager python`
  runs the script on the box against the box's own checkout, so a bench result
  from a stale box is not evidence about the commit that triggered it.

- **`mint broken-links` runs in the static-checks gate**, with anchor checking
  on. Three cross-references had shipped missing the `/source` prefix that every
  published URL carries, each found by hand. Scoped to `docs/source/` -- the
  working notes under `docs/reference/` are not published and carry stale
  relative paths a reader can never follow.

### Changed

- **The Architecture page draws its diagrams, and four of its claims about the
  box were wrong.** Five hand-drawn ASCII block diagrams are Mermaid now, which
  Mintlify renders natively with zoom and pan and themes for dark mode. Several
  had drifted out of alignment: borders that do not close, arrow columns landing
  between the boxes beneath them, and a step list that runs 1 to 10 and then
  jumps to 14.

  Redrawing the internals meant checking them against the source, which is where
  the wrong claims surfaced:

  - `lager supply <net> voltage <v>` posts to `:9000/supply/command`. The page
    described it as a script upload to `:5000`, and named an impl script,
    `cli/impl/power/supply.py`, that does not exist.
  - `:5000` is a `ThreadingHTTPServer`. Flask serves `:9000` and `:8080`.
  - `:8080` is published to the host alongside `:5000`, `:8100` and `:8765`, and
    unpublished only by `--no-publish`, which unpublishes all of them. The port
    table called it container-internal.
  - The old diagram's arrows implied a request chain
    `9000 -> 5000 -> 8765 -> 8100 -> 8080`. The services are peer processes under
    one start script. The only call between them is `9000 -> 8080`, through the
    Device proxy.

  The first three contradicted the `:9000` / `:5000` section the page had just
  gained, which describes `:9000` as the box API and `:5000` as the older
  script-upload path.

  The `saved_nets.json` record is a highlighted `json` block rather than ASCII
  art, and the `--no-publish` caveat is a `Note` so it is harder to skim past
  than the port table it qualifies. The host file listing stays a code block:
  Mintlify's `Tree` component renders nothing at all in the version that builds
  these docs, in every documented spelling, with no error.

- **The prose gate is now a required context, and it can see three rules it
  could not see before.** `tools/check_ste.py` reported zero across the corpus
  while three of its own rules were partly blind, so the zero was a statement
  about the checker as much as about the prose. Each gap was found by a
  conversion batch running against the tool, not by reading it:

  - The `tense` pattern admitted no adverb but `not` between the auxiliary and
    the participle, so `is currently outputting` and `is actually presenting`
    sat in pages that reported clean. Any adverb now counts. The same rule
    treated every `-ing` word as a participle, which would have fired on `is
    nothing` the first time anyone wrote it; the common non-participles are
    excluded.
  - `clean_inline()` ran per source line, so an inline code span opened on one
    line and closed on the next never collapsed to `CODE` and its literal words
    counted as prose. That was the whole of a 33-word `length` violation in
    `usb.mdx` that was not one. Cleaning now happens once, on the joined
    paragraph.
  - `LagerError` and `BoxError` were absent from the emitter set, and their
    `cause=` and `suggestion=` text was never read at all. They print at a user
    exactly as `click.echo` does. They carried 11 banned modals across six
    files.

  Widening the checker surfaced 26 violations in text that had just merged as
  clean, across `cli/errors.py`, `gateway_auth.py`, `config.py`, `_ssh.py`,
  `nets.py`, `battery.py`, `install.py`, `python.py`, `debug/commands.py` and
  one troubleshooting page. All 26 are rewritten. `tools/ste_baseline.json`
  stays empty.

  With the checker honest, the CI step drops `continue-on-error: true`. A
  required context that cannot see three of its rules is worse than no context,
  because it converts "nobody checked" into "the check passed".

- **The CI guide is rewritten around a runner installed on the Lager Box.** The
  published page assumed the runner is a separate machine that reaches the box
  across the network, so every job paid for `pip install`, `lager login` and
  `lager boxes add`, and the box IP became a repository secret. A runner on the
  box removes all three: its label is the box name, the job needs no Lager
  secrets, and a self-hosted runner takes one job at a time, which serializes
  that bench with no `concurrency:` block.

  The page covers both arrangements and recommends the second. It adds material
  the old page had no equivalent for: a firmware build that runs off the bench
  with a `github.sha`-pinned checkout, a flash step that checks its own result
  because a programmer can report a fatal error and still exit 0, a 0/1/2
  exit-code contract that separates a device failure from an infrastructure one,
  a retry wrapper that power-cycles the probe and the DUT between attempts,
  cleanup that runs on cancel rather than only on failure, a matrix generated
  from checked-in bench files, and a gate job that catches a test which is not
  applicable on any bench.

  Every flag, environment variable, exit code and default is verified by walking
  the live click tree rather than by reading the published docs. That found one
  error in the source draft: `lager nets list` does not exist. `lager boxes
  list` does, and bare `lager nets` is the listing form.

- **`architecture.mdx` hands CI to the CI page, and explains why a box answers
  on two HTTP ports.** Its CI section described the separate-host arrangement as
  the only one and carried a second workflow example beside the real one; two CI
  examples in two pages is what let them drift, so one page owns CI now.

  The page gains a short section on the `:9000` / `:5000` split, which the port
  table listed without explaining. `:9000` is the box API and takes all net
  data-plane traffic; `:5000` is the older script-upload path that `lager python`
  still uses; lock state answers on both and the CLI reads `:9000`. The
  consequence a reader can act on was written down nowhere: a box that publishes
  only `:9000` answers `lager nets` and `lager hello`, and fails `lager python`.
  Closes #383.

- **The Python API reference is converted to Simplified Technical English.** All
  60 gated violations across the 14 affected pages of
  `docs/source/reference/python/` are fixed: 43 sentences over the 25-word
  reference cap, 9 unapproved modals, and 8 perfect or progressive tenses. Those
  14 entries leave `tools/ste_baseline.json`, and no budget in the file rises.

  Where a sentence carried four or more coordinate facts it became a vertical
  list, not a shorter sentence -- the three-valued return of `cycle()`, the RTT
  reader's reconnect rules per backend, and the two guarantees `cycle()` gives
  over a hand-rolled `disable`/`sleep`/`enable`. STE prescribes a list past two
  items, and in reference text it states the contract more precisely than the
  running prose did.

  One defect no check could see: `debug.mdx` read `materialised`. The
  American-spelling rule carries no budget, but that stem is absent from the
  checker's word list, so only reading the page finds it.

- **CLI failure messages now say what happened, not what could not happen.**
  `Could not connect to the box` names an outcome that did not occur, and leaves
  the reader to guess which of a dozen causes applied. STYLE.md rule 6 asks for
  the event instead. Every budgeted `modals` and `tense` violation under `cli/`
  is gone -- 108 modals (59 `could`, 41 `may`, 7 `would`, 1 `should`) and 33
  progressive or perfect verb forms, across 130 message and `help=` strings in
  25 files. `tools/ste_baseline.json` drops those 25 entries.

  Each rewrite was read out of its own branch rather than swapped for a synonym.
  The handler already dispatched on `Connection refused`, on a `ReadTimeout`, on
  an `OSError`, so the sentence now carries that. `Could not connect to
  {ssh_host} within 15 seconds` became `The box at {ssh_host} did not answer
  within 15 seconds`, and `Could not determine update state` became `The update
  state is unknown`.

  Two sentences changed more than their wording, because reading the branch
  showed the old one was false. `lager boxes` summarized its failures as `N
  boxes could not be reached`, but that counter also counts a box with no stored
  IP, a bad response, invalid JSON, an old box, and any HTTP error -- boxes that
  answered. It now reads `N boxes did not report a version`, which is true of
  every branch that increments it, and the Status column already names the
  specific reason per box. `lager debug memrd` warned that a start address `may
  be invalid for 32-bit system` on a guard that also fires when only
  `start + length` overflows; it now describes the range.

  Two bodies of text are deliberately untouched. `_CONNECT_FAILURE_SIGNATURES`
  holds three `Could not ...` entries that are match targets for the
  programmer's own output, kept in step with the box, where a reword changes an
  exit code rather than a sentence. `cli/errors.py` carries the same kind of
  text through `LagerError`, which is not in the checker's emitter set and so
  carries no budget; it needs its own pass.

  The `--check`, `--dry-run` and `--user` rows in `docs/source/reference/` move
  with the `help=` strings they mirror, so the published option tables cannot
  drift from `--help`.

- **User-facing prose now follows ASD-STE100, enforced in CI.**
  `docs/STYLE.md` adopts Simplified Technical English: fourteen rules covering
  sentence and paragraph length, active voice, simple tenses only, one
  instruction per sentence with the condition first, the approved modals
  `can`/`will`/`must`, American spelling, one term with one meaning, noun-cluster
  limits, and the shape of a safety warning. STE's Writing Rules are adopted in
  full; its Dictionary is not reproduced, because the approved-word list is a
  licensed ASD specification that a public repository cannot carry. A project
  Technical Names table stands in its place, which is what STE itself expects.
  `tools/check_ste.py` enforces the measurable rules against the published pages,
  the root prose files, and every `help=` string and message the CLI prints.

  The corpus carried two spellings of the product's own name -- `Lager Box` 329
  times and `Lagerbox` 119, on the same pages -- plus a British/American split
  on `behaviour` and `recognised`. Both are now single-valued across `cli/` and
  `docs/`, and both rules carry no budget, so neither can come back. The
  `Lagerbox` spelling was mostly in CLI help and message strings rather than in
  the docs, which is why the sweep spans both trees: the docs quote CLI output
  in sample blocks, so changing one without the other would leave the samples
  wrong.

  The ten `getting-started/` pages are converted, taking that section from 106
  violations to zero on all eight checked rules. The remaining sections carry a per-file budget in
  `tools/ste_baseline.json` that ratchets down as each later batch lands; the
  CI step stays `continue-on-error` until the last one, because a required
  context that nothing can turn red is worse than no context at all.

- **Sixteen `reference/cli/` pages are converted to Simplified Technical English.**
  `battery` through `lager-file` go from 62 violations to zero on all seven gated
  rules: 38 sentences over the 25-word cap, 16 modals outside `can`/`will`/`must`,
  and 8 perfect or progressive forms. Long sentences are split at the clause break
  rather than trimmed, so the articles and `that` clauses STE keeps are still
  there. `tools/ste_baseline.json` ratchets from 103 files to 87 and from 496
  budgeted violations to 440, because all sixteen pages leave the budget entirely.

  Two message strings in `cli/commands/development/debug/commands.py` are
  rewritten with them. The baseline predated the reconnect path
  `_auto_connect_if_needed` gained, so the recorded budget for that file sat one
  modal and one tense below what the file actually carried. Rewriting the two
  strings holds that budget where it was rather than raising it, which the
  ratchet does not allow.

- **Fourteen `lager` command reference pages are converted to Simplified
  Technical English.** `locking`, `login`, `nets`, `python`, `router`, `scope`,
  `ssh`, `ssh-setup`, `supply`, `uninstall`, `update`, `usb`, `watt` and
  `webcam` go from 79 budgeted violations to zero on all seven gated rules: 54
  sentences over the 25-word reference cap, 13 uses of a modal STE does not
  approve, and 12 perfect or progressive verbs. `tools/ste_baseline.json` drops
  all fourteen files, taking the corpus budget from 380 violations across 73
  files to 301 across 59.

  Several of the long sentences were vertical lists that lost their formatting.
  The longest ran to 54 words, and one carried three semicolon-joined clauses;
  those are now lists or separate sentences rather than shorter run-ons.

  Two fixes sat on lines that `tools/check_docs.py` reads as the page's
  assertion that a flag exists -- `--cs`/`--sck`/`--mosi`/`--miso` on `nets`,
  and `--check` on `update`. Those lines keep every `--flag` token and only
  their description text changed, so the flag check still sees the same set of
  declarations. The `nets` caveat that made its line too long moved into the
  paragraph below it, which already describes how pins behave.

- **The Rust API reference is converted to Simplified Technical English.** All 31 pages
  under `docs/source/reference/rust/` now report zero on `terms`, `spelling`, `modals`,
  `length`, `tense`, `conjunction` and `paragraph`, down from 80 violations across 24 of
  them (49 length, 17 tense, 14 modals). These are reference pages rather than
  procedure, so the sentence cap is STE's 25-word descriptive limit and not the 20-word
  procedural one.

  Rust type, trait and method names are Technical Names under rule 7 and are always
  approved, so `DebugNet`, `RttStream`, `NetType` and every method signature read as
  they did. The `lager-net` version pins are untouched. Four sentences that were really
  lists -- the net-type catalogue, the `NetRecord` fields, and the BluFi and debug
  timeout budgets -- became bullet lists under rule 14 rather than tables, so every item
  is still measured by the checker.

  Four modals took no substitute, because `can`, `will` and `must` would each have
  stated something false. A URL captured on one network *sometimes* resolves from
  another, and a recommendation to mark hardware-only tests `#[ignore]` is not an API
  requirement that anything enforces. Rule 6's own worked example replaces such a modal
  with what actually happens, and that is what these do.

  Two defects here were invisible to `tools/check_ste.py` rather than reported by it.
  Its tense pattern allows only `not` between the auxiliary and the participle, so
  `is currently outputting` in `dac.mdx` and `is actually presenting` in `battery.mdx`
  sat in pages that reported clean; both are now simple present. Its `clean_inline()`
  runs per source line, so an inline code span hard-wrapped across a newline is never
  collapsed and its literal words count as prose -- which was the whole of a 33-word
  violation in `usb.mdx`. Reflowing that span onto one line clears it with no change to
  the prose, and the error message the crate emits stays character-for-character
  identical. The same latent wrap in `dfu.mdx` is reflowed as well.

  `passive` stays report-only per rule 4, and the CI step stays `continue-on-error`
  until the last batch lands.

- **The root prose files and the MCP and supported-instruments pages now read as
  Simplified Technical English.** `README.md`, `CONTRIBUTING.md`,
  `RELEASE_PROCESS.md`, `docs/README.md`, `test/README.md`,
  `test/CONVENTIONS.md`, `test/COVERAGE.md`, the two `reference/mcp/` pages and
  `supported-instruments.mdx` go from 82 violations to zero on every gated rule:
  62 sentences over the 25-word cap, 11 uses of `should`/`may`/`could`/`would`
  where `can`, `will` or `must` is meant, and 9 perfect or progressive tenses.
  Five run-on inventories that had lost their list formatting are vertical lists
  again. `tools/ste_baseline.json` drops from 35 files and 221 budgeted violations to
  25 and 139. Two British spellings the checker's word list does not carry,
  `labelled` and `analyses`, are also corrected.

  No instrument name, model number or address string changed:
  `supported-instruments.mdx` is what the CLI is checked against, so a rename
  there would make the docs disagree with what the CLI prints. The counts in
  `test/COVERAGE.md` are machine-checked and untouched; only its prose moved.

- **21 `Dexarm` methods and both `Wifi` methods gained docstrings.**
  Introspection uses a docstring's first line as a method's description, so an
  undocumented driver method reaches an agent as a name with no explanation.

- **The Release Notes navigation was a single flat list of 158 entries.**
  Grouped into five version ranges.

- **Bench wiring fixtures are documented.** A permanent wire from DP821 CH2's
  output to a USB-202 ADC input existed for a check whose repository variables
  were never set, so it had never run and nothing recorded that the channel had
  anything attached. The supply suite asserts that channel is unloaded, so the
  wire presented as an intermittent per-channel instrument fault. The bench
  README now carries a fixture table, on the principle that an undeclared wire
  reads as a hardware failure.

### Fixed

- **Per-probe runtime file paths are now built from a validated serial.** The
  pid and log files for a debug probe are named after its USB serial, which is
  read from a field of a net's VISA address that is permissive about what it
  accepts. That value is now reduced to characters that cannot alter the shape
  of a path, through one shared helper rather than the near-copies of the idea
  that had grown up separately, and each site then checks that its own join
  stayed in its own directory. The sysfs lookup in `diagnose` that reads the
  same field gets the same treatment.

  The check is repeated at each site rather than shared, which is worth knowing
  before someone tidies it away: a static analyser recognises a path guard only
  inside the function that builds the path, so folding those lines into a
  helper leaves the code correct and the analysis blind. The helper's docstring
  records that, because the tidier version is the tempting one.

  An ordinary alphanumeric serial produces the byte-identical filename it did
  before, so a box that upgrades while a debug session is live still finds its
  running gdbserver; a test pins that. Probe identity is unaffected -- slot
  assignment still matches on the raw serial, because this is a path concern
  and not an identity one.

- **Removed an unused `/pip` endpoint from the box python service.** Its only
  caller addressed a port and path the box has never served, so both halves
  were dead code. Boxes should be updated to a release containing this change.

- **A bench check stopped retrying past a box bounce, because the prose pass
  reworded the message it greps for.** `test/integration/infrastructure/box_config.sh`
  decides whether `lager box config validate` failed to reach the box by matching
  `could not connect to the box|may be offline` in its output. The Simplified
  Technical English pass rewrote both strings in `cli/errors.py` to "The connection
  to the box failed" and "The box is offline", so the match could no longer
  succeed.

  Nothing went red. That suite does not run in CI, so the change shipped through
  22 green checks. The failure mode is worse than an error: the retry never fires,
  and the test then records a verdict about a box that was still coming back from
  the section-3 bounce -- the defect class the comment above that function cites
  issue #283 for.

  The pattern now matches both wordings. A probe that asks whether the box was
  reachable only becomes more reliable by accepting more spellings, and an older
  CLI still emits the original pair. The comment says who has to update it next.

- **A box install failed at the firewall step once the port allowlist held
  ranges.** `secure_box_firewall.sh` writes its per-interface allow rules as
  `ufw allow in on <iface> to any port <port>`, naming no protocol. ufw refuses
  a port range spelled that way -- `Must specify 'tcp' or 'udp' with multiple
  ports` -- so the first range aborted the script under `set -e` and
  `lager install` exited 1 having configured nothing. The allowlist held only
  single ports when those rules were written, which is why the omission went
  unnoticed until the debug port ranges were added to it.

  The rules name `proto tcp` now, on all four interfaces rather than only the
  one that reports first. TCP is what `start_box.sh` publishes, and a test pins
  both halves of that so neither can move alone.

  The script disables and resets ufw before writing the new rules, so a failure
  anywhere in between left the box with the firewall off and no rules at all,
  reported as nothing more specific than `Deployment failed!`. It now restores a
  deny-incoming policy with SSH allowed, prints which half is configured and
  which is not, and keeps the failing exit code.

- **A `lager python` connection error printed the literal `{box_ip}`.** The hint
  that follows `Connection refused by box` was a plain string rather than an
  f-string, so it told the reader to run `ssh lagerdata@{box_ip} "docker ps"`
  with the braces intact. It interpolates now.

- **A debug command no longer proceeds against a target that is not there.**
  `/debug/status` reported a single `connected` boolean that meant "the
  gdbserver process is alive", and `_auto_connect_if_needed` returned on it
  without touching the target. On a box where the server outlives the part,
  `flash`, `reset`, `erase`, `memrd` and the RTT paths all ran believing they
  were connected. #344 fixed the erase verdict at one call site by reading the
  programmer's output; this is the cause underneath it.

  The endpoint now reports `gdbserver_running` and `target_attached`
  separately, and `lager debug <net> status` prints both. `connected` stays,
  pinned to its old meaning -- a live server -- so an older CLI against a newer
  box behaves exactly as it did rather than silently changing what the field
  means.

  `target_attached` is a tri-state, and the third value carries weight. A box
  older than this change, a probe refused because a debugger already holds the
  session, or a probe that timed out all yield "could not establish", which is
  not the same as "absent" -- reading it as absent would tear down sessions
  that were working. The CLI falls back to server liveness there, and `status`
  prints `Unknown`.

  Reading the target costs a GDB round trip, and `/debug/status` is called by
  every debug subcommand, so the wire read is opt-in per request. The free
  check -- the server's own logfile, using the same predicate #344 established
  -- always runs.

- **`connect()`'s target verification checked the wrong thing and was never
  read.** It issued `monitor version` and accepted any console reply as proof,
  but that is the gdbserver answering about itself, which it does with no part
  attached. The value was also discarded: nothing read `target_verified`, and
  `/debug/connect` does not route through the function that sets it. It now
  uses the same predicate `/debug/status` reports, so "attached" has one
  definition instead of two.

- **The Python API reference documented a key `status()` does not return.** It
  showed `status.get('connected')`; the method returns `running`.

- **`docs/package.json` ran `mint build`, a subcommand the Mintlify CLI no longer
  has.** `docs/vercel.json` pointed its `buildCommand` at that script and expected
  the output in `.mintlify`. Nothing consumed either file: docs.lagerdata.com is
  built and served by Mintlify's own hosted platform, which deploys from `main`
  through the Mintlify GitHub app. `vercel.json` is deleted, and the scripts are
  now the commands that actually work -- `dev`, `validate` and `broken-links` --
  each pinned to the same mint version `static-checks.yml` pins, so a local run
  and CI cannot disagree. Closes #374.

- **The scope's `SUPPORTED_USB` key was spelled `Rigol_MS05204`, with a zero where
  the letter O belongs.** The instrument is the MSO5204, as
  `rigol_mso5000_defines.py` and the docs both have it, and the misspelling was
  user-visible in `lager instruments` and `lager nets list`. The key is renamed in
  all three tables that carry it (`SUPPORTED_USB`, `CHANNEL_MAPS`,
  `INSTRUMENT_NET_MAP`).

  The instrument name is persisted verbatim in every saved net record, so boxes
  provisioned before this change still hold the old string. `canonical_instrument()`
  maps it to the new spelling at each of the exact-key lookups that consume a saved
  value, so those records keep working with no migration of `saved_nets.json`. The
  distinction matters: a saved net whose instrument no longer matches a table key
  does not fail loudly, it silently loses whatever restriction that key carried.
  Closes #373.

- **`lager://reference/Router` returned zero methods, and so did `Logic`, `Arm`,
  `Webcam` and `Wifi`.** `api_reference.py` introspects a driver class per
  NetType so the agent-facing reference stays in lock-step with the real
  drivers, but a NetType absent from the map is never introspected at all --
  and nothing checked the map in that direction, so ten of `NetType`'s
  twenty-four members had no entry.

  `Router` was the expensive one: `MikroTikRouter` has 37 public methods
  including the bench's only network fault-injection tooling
  (`block_internet`, `block_dns`, `block_port`, bandwidth limits, DHCP
  control), which is how a test asserts what firmware does when the network
  degrades rather than disappears. None of it was visible to an agent.

  Curated entries are added for `Router`, `Arm`, `Webcam`, `Wifi`, `Analog` and
  `Logic`. `Analog` and `Logic` are hand-written for the same reason `Debug`
  is: `Net.get()` returns a bare `Net` proxying to the instrument over RPC, so
  introspecting the mapper would replace the curated list with nine
  undocumented local helpers. The raw saved-net roles are added to the alias
  map too -- `plan_firmware_test` looks entries up by role, so without them the
  new entries would have been reachable only through the resource URI.

  A guard test now asserts every `NetType` either has an entry or appears in an
  explicit exclusion list with a stated reason, which is the check that was
  missing. Verified against MCP Python SDK 2.1.1: `lager://reference/Router`
  returns 37 methods, and `lager://guide/api-quick-reference` renders all six
  new types. Closes #372.

- **Legacy double-booked nets no longer block unrelated adds in the
  Net-Manager TUI.** Boxes that still hold two saved nets on a
  single-channel instrument (e.g. a `battery` and a `power-supply` net on
  the same Keithley 2281S, saved before the one-net-per-chip rule) made
  the Add screen reject every selection with "Only one net may be added
  per Keithley_2281S…", even pure GPIO adds. Both the single-channel and
  mode-exclusive conflict checks now only fire for instruments the current
  selection actually touches.

- **Six CLI messages told the user to run `lager box update`, which does not
  exist.** The `lager box` group carries only `config` and `dut`; the `update`
  spelling was removed in favor of top-level `lager update`, and two comments in
  the source say so. The messages were never updated, so a version-skew warning,
  a lock-support warning, a `diagnose` verdict, a download-file error and an
  `/etc/lager` permission error each handed the reader a command that errors out.
  Three unit tests asserted on the dead spelling and pinned it in place. All six
  messages and all three assertions now name `lager update`.

- **The firewall allowlist that provisioning deploys now matches the ports the
  box publishes.** Two copies of `secure_box_firewall.sh` had drifted, and the
  one carrying the correct debug port ranges was the copy nothing deploys --
  absent from the box image, absent from the wheel, referenced only by a README
  telling operators to run it. The deployed copy admitted `5000 8301 8765 5001`:
  it omitted the GDB/SWO, OpenOCD telnet, OpenOCD TCL and RTT ranges, the MCP
  and hardware-service ports and the box HTTP API, and admitted `5001`, which
  nothing serves. Two previous release notes described this same allowlist being
  brought in line; both changed only the copy that is never deployed.

  There is now one copy. `test/unit/box/test_firewall_port_allowlist.py` parses
  the publish list out of `box/start_box.sh` and the allowlist out of the script
  and fails if they diverge, including the conditionally-published `9000` arm
  that an array-literal read would miss. The script's `--help` renders the array
  rather than restating it, since both its help text and its header comment had
  gone stale against the array in their own file.

  Note the scope. This corrects which ports the allowlist names. It does not
  change how the host firewall treats a container-published port, which is
  tracked separately.

- **`docs/reference/gateway-auth-contract.md` states where MCP stands.** The
  contract defined the box surface as `:9000` and `:8765` and never mentioned
  `:8100`, leaving whether the gateway should front it as an open question
  rather than a decision. It is now recorded as in-fabric only, with the
  reasoning -- the MCP server authenticates nothing itself, deliberately
  disables DNS-rebinding protection, and its opt-in gates extend it to hardware
  control and arbitrary command execution.

- **A second device of the same model no longer disables that whole instrument
  family.** Four call sites -- `nets add`, `nets add-all`, the net TUI and
  `lager instruments` -- each carried their own copy of a hardcoded model list
  and each did something different with it. `nets add` refused with an error,
  `add-all` skipped the family in silence, the TUI computed per-device keys and
  then discarded them, and `lager instruments` hid the devices from its own
  table, so the addresses needed to create their nets could not even be read.

  Whether two devices can coexist is a property of the address, not the model.
  Most instruments carry a unique serial, so two of them get two addresses and
  both stay drivable; a hub that reports no serial is already topology-addressed
  by the scanner for exactly this reason. The check is now "do two present
  devices report the same address", which is right for a model nobody has
  considered yet and stops being wrong for a model the moment the scanner learns
  to address it.

  Two Acronames now yield sixteen usb nets instead of none. A second LabJack T7
  is still refused, because it reports no serial and is not topology-addressed,
  so two of them enumerate as the same string and a net could not say which one
  it meant -- but the message now says that, rather than "unplug extras".

  The silent-skip path was the dangerous one: `delete-all` + `add-all` is the
  documented recovery procedure, and on a bench whose instrument AC power is
  switched by LabJack GPIO nets, skipping the LabJack family takes the bench's
  power control with it and says nothing.

- **`lager arm`'s reference page was un-runnable as written.** `--x/--y/--z` and
  `--dx/--dy/--dz` were documented as positional arguments, so every motion
  example on the page failed. Same for `set-acceleration`.

- **`lager update` documented three options that do not exist.** `--all` and
  `--skip-restart` were removed in v0.18.2 and `--check-jlink` never shipped,
  but the page carried them for eighteen releases along with a walkthrough and
  sample output for a multi-box mode that no longer exists.

- **The MCP page understated the tool surface.** It stated the tools are
  read-only, which holds only while both opt-in gates are off.
  `LAGER_MCP_ALLOW_CONTROL` adds `power_cycle_hub`, which drives hardware;
  `LAGER_MCP_ALLOW_EXEC` adds `box_exec`, `read_file`, `write_file` and
  `list_dir`, exposing arbitrary command execution and file writes to any agent
  that can reach the MCP port. Neither variable was named anywhere in the docs.

- **The Rust pages pinned `lager-net` to a version two breaking releases old.**
  Four sites pinned `"0.2"`; the published crate is 0.4.0, and cargo does not
  resolve `"0.2"` to 0.4.x.

- **Options that shipped but appeared on no page** are now documented: `--json`
  on `adc`, `dac`, `gpi`, `gpo`, `thermocouple` and the `eload`/`energy`
  subcommands; `--volume` on `exec`; `--email`/`--password` on `login`.

- **Reference pages that failed a strict MDX build or linked nowhere.** An
  unclosed callout in the supported-instruments page, and three cross-references
  missing the `/source` path prefix.

- **The `lager wifi` reference page is removed.** The command is `hidden=True`,
  so publishing a page advertised something the CLI conceals.

- **The supply suites wait for the output to reach regulation instead of
  sleeping a fixed interval.** A Rigol DP821 channel does not step to its
  setpoint, and its current readback lags its voltage. Measured on a channel
  wired to an ADC input: 0.25 s after `enable()` reads 2.0 V against a 5 V
  setpoint, and 0.5 s reads 4.5 V, still climbing -- while the current register
  still held a charge transient after the voltage had arrived, reporting
  `V=5.0` and `I=0.24 A` together. The unloaded-current assertion sampled
  exactly that window and failed intermittently on a channel that measures a
  clean 0.0000 A once settled.

  The settle now waits for the ramp to finish and then for the current readback
  to stop changing. Deliberately not for it to fall below any threshold -- that
  would assert the very thing the caller is about to test, so a genuine steady
  load still fails. Waiting on voltage alone is insufficient (the current lags
  it) and waiting on current alone is worse (before the ramp starts it reads
  0.000 and looks settled immediately), so both conditions apply, in order. An
  unsupported query falls back to a plain sleep rather than taking a hardware
  suite down.

  `MAX_UNLOADED_CURRENT` is unchanged at 0.1 A. It was never the problem: both
  channels satisfy it comfortably once the output has settled, and a
  range-relative per-channel bound is unnecessary -- a 1.5 s-settle sweep across
  1/2/5/7 V read exactly 0.0000 A on both channels at every setpoint, 96
  samples with zero spread.

  The same fixed-settle exposure in the USB-202 supply-into-ADC check is fixed
  the same way; it would otherwise have started failing on tolerance the first
  time that check was enabled.

- **The box no longer advertises host URLs it does not publish.** `start_box.sh`
  printed its entire `Services running:` summary unconditionally, including the
  MCP line's `http://<box-ip>:8100/mcp`, on a box started with `--no-publish`.
  That mode publishes none of the container's service ports: the container joins
  `lagernet` either way, but `PORT_PUBLISH_ARGS` is empty, so a reverse proxy on
  that network owns the host ports and nothing is listening on the host at 8100.
  Every `<box-ip>:<port>` in that banner was therefore wrong for precisely the
  deployment it was describing. The summary now states which mode the box is in,
  and the MCP line points at the lagernet address rather than the box IP.

  The MCP server itself was never at fault and needed no change -- it binds
  `0.0.0.0:8100` inside the container in both modes and answers normally on
  lagernet. This was only ever a question of reachability, and of six
  documentation sites asserting a reachability that a proxied box does not have:
  the module docstring in `box/lager/mcp/server.py`, the agent-facing run guide
  in `box/lager/mcp/resources/guide.py` (which told agents to identify the box by
  the IP they connected on, illustrated with the published form), the box service
  table in `box/README.md`, the MCP section of the top-level `README.md`, the
  connection example in the MCP reference, and the port table in the architecture
  guide, whose "Exposed" column described the published case as though it were
  the only one.

- **`LAGER_DISABLE_UART_SERVICE` now actually frees port 9000.** The flag exists
  so a box can leave 9000 to another service. `start-services.sh` honoured it and
  declined to launch `box_http_server.py`, but `start_box.sh` published
  `-p 9000:9000` unconditionally, and docker-proxy binds a published port whether
  or not anything listens behind it. The port therefore stayed occupied and the
  flag delivered none of what it exists for. `start_box.sh` now reads the same
  value out of `BOX_CONFIG_ENV`, with the same `1|true|yes` rule
  `start-services.sh` uses, and declines to publish the port; the startup banner
  stops promising 9000 in that case.

  The integration check could not have caught this -- it only `pgrep`s for the
  process inside the container, which was already correct. It now also asserts
  the host port is free.

- **`lager box config apply` says what the container-side package steps did.**
  `_bounce_container_rc` captures `start_box.sh`'s transcript and re-emits lines
  only when the run exits 3, keeping only `[ERROR]`-prefixed ones, so a
  successful apply printed nothing whatsoever about pip, cargo or npm. A step
  that installed three crates and a step that found none to install were
  indistinguishable from the CLI, which is how a suspected silent no-op survived
  three rounds of triage.

  Each step now reports what it did or why it did nothing, and apply relays those
  lines on success, bounded and de-duplicated the way the error relay already is.
  Worth naming the asymmetry this closes: apt and sysctl are applied host-side by
  the CLI before the bounce and print their failures directly with a repair hint,
  while pip, cargo and npm run inside `start_box.sh` and reached the operator only
  as an exit code.

- **The cargo integration check asserted a path that does not exist on the box.**
  `CARGO_HOME` is `/opt/rust/cargo`, set in `box.Dockerfile` and backed by the
  `lager-cargo` volume, so `cargo install` writes there and never to
  `$HOME/.cargo`. `/home/www-data/.cargo` is absent entirely, so the assertion on
  `/home/www-data/.cargo/bin/` could not pass for any crate, installed or not. It
  now checks the real path, drops the login shell that `start_box.sh`'s own cargo
  loop documents as dropping `/opt/rust/cargo/bin` from `PATH`, and prints the
  directory listing on failure so a future run can tell "cargo did not install
  it" from "we looked in the wrong place".

- **`lager logic measure` / `trigger` / `cursor` can resolve a logic net again.**
  All sixteen actions failed with `Error: Invalid Net: <net>` against a real
  logic net. `cli/impl/measurement/scope.py` is the consolidated worker for both
  the scope and logic families -- its dispatch tables already register every
  action either sends -- but its two net-resolution helpers were hardcoded to
  `NetType.Analog`, and `Net.get` matches on type equality, so a net whose role
  is `logic` could never resolve there however it was addressed.

  The role is known unambiguously at the CLI layer, which validates the net
  against it before dispatching, so it is now passed down in the command
  envelope and the worker resolves under the type that role maps to. A CLI that
  predates the key keeps working: the worker defaults to `scope`, which is the
  behavior it had previously.

  A second, independent path to the same dead end is fixed with it:
  `get_net_info` filtered saved nets on `role == "scope"`, so it returned `None`
  for a logic net, which made `is_rigol()` and `is_picoscope()` both false and
  the basic-op dispatchers report `not found or not a scope net`.

  This is the same defect as the one `cli/impl/power/enable_disable.py` was
  fixed for, one layer over. `lager logic` had been dispatching to two workers
  holding two contradictory type constants; they now agree, and
  `test/unit/box/test_logic_net_type.py` pins both.

  Note the Rigol mapper needed no work: every measurement and trigger method
  already branches on the net's type and maps a logic net to `D0`-`D15`. Only
  the lookup was wrong.

- **The thermocouple page published at `/reference/cli/tc`** while the command
  is `lager thermocouple`. Renamed, with a redirect from the old path.

## [0.43.0] - 2026-08-25

### Added

- **`DebugNet.halt()` stops the target where it is, without a reset.** OpenOCD
  only. `reset(halt=True)` runs OpenOCD's `reset halt`, which pulses nRESET and
  re-enters through the reset vector; on a part executing in place out of QSPI
  that re-runs the bootloader rather than stopping on the image just
  programmed. `halt()` issues a bare `halt`, so XIP is left holding what was
  written. It is the operation the self-heal path already assumed existed when
  it documented why a DA1469x must not be auto-reattached unhalted.

  J-Link has no standalone halt-in-place primitive -- `reset_device` and
  `gdb_reset` both reset first -- so on that backend the call raises and names
  the halt-first `.JLinkScript` as the supported route.

- **`connect()` accepts `halt`, `openocd_config` and `jlink_script`.** `halt`
  was pinned to `False` on the OpenOCD path even though the underlying
  gdbserver call has always taken it; it is documented as reset-then-halt, with
  a pointer to `halt()` for the other meaning. The two script kwargs are the
  unambiguous per-backend forms of `script`, for a base64 blob that carries no
  filename to classify.

- **FTDI GPIO, I2C and SPI nets can address a specific channel on a
  multi-channel adapter.** A net may now carry `params.interface`, taking
  `A`-`D` or `0`-`3` -- the same vocabulary debug nets already accept as the
  `@A` suffix on their device field. Previously all three drivers hardcoded
  `ftdi://ftdi:232h[:serial]/1`, so interface A was the only channel any of
  them could ever open, and a board wiring comms to one channel and control
  lines to another could not be driven at all.

  Which channels are legal is not uniform, and is enforced per net rather than
  per instrument. I2C and SPI are MPSSE protocols, and on an FT4232H only
  channels A and B have an MPSSE engine; asking for C or D now fails at net
  construction naming the channel, instead of somewhere inside pyftdi. GPIO
  runs as asynchronous bitbang, needs no MPSSE, and works on all four -- which
  is what makes an FT4232H's C and D usable for control lines.

  Two things that only surface once a second channel is reachable are handled
  with it: the GPIO state cache now keys on interface as well as device, so
  AD0 on channel A and AD0 on channel B stop sharing an entry and clobbering
  each other between CLI invocations; and ACBUS pins (8-15) are refused on the
  FT4232H, whose channels are 8 bits wide with no ACBUS at all.

  `FTDI_FT4232H` accordingly gains the `spi`, `i2c` and `gpio` roles its
  siblings already advertised.

### Changed

- **`DebugNet.connect(script=...)` documents that an OpenOCD override must be
  a complete cfg.** The launch line still carries lager's own
  `ftdi channel <N>` for a net with a probe channel, and that command is not
  recognized unless a cfg has selected the ftdi adapter driver -- so a
  fragment holding only, say, `adapter speed 1000` dies at startup with
  `invalid command name "ftdi"`. The docstring implied a small standalone cfg
  would do.

### Fixed

- **`/etc/lager/ref` was never written when the box was already up to date.**
  `lager update` records which ref produced the box's code so `lager hello`
  can distinguish a branch deploy from the release tag it shares a version
  number with. That write sat on the pulled path only: a run that found the
  box already at the target version exited several hundred lines earlier, so
  it never happened.

  That is the case the file matters most in -- a re-run against a box whose
  ref file is missing or stale is exactly when someone is trying to find out
  what the box is running. And because the documented way to confirm a branch
  deploy took is that `lager hello` names a ref, its absence reported failure
  for a deploy that had succeeded. `/etc/lager/version` had the identical bug
  on the identical branch and was fixed once already; the two writes are now
  pinned together by a test so a third such file cannot repeat it.

  Note the file is written by the CLI doing the deploying, not by the box, so
  a box deployed by a CLI predating this feature has no ref file however many
  times it is updated. Put the host CLI on the newer version first.

- **"SSH key not configured for this box" on a box where the key was
  installed and working.** Two independent defects, both in reading a probe
  that deliberately has three outcomes -- installed, not installed, and
  couldn't tell.

  The probe greps the box's `authorized_keys` rather than inferring
  installation from a successful login, but it did not offer the lager key to
  the SSH that carries the query. On a machine whose default identities the
  box does not accept, and where the key is not loaded in the agent, nothing
  usable was offered, so the probe could not connect and honestly answered
  "couldn't tell" about a box it was perfectly able to answer for. It now
  passes the key with `-i`, which widens the identities tried rather than
  narrowing them -- any other working credential is still accepted.

  That is the whole of the original defect, and fixing the probe fixes it:
  a box that has the key now answers "installed" rather than "couldn't
  tell". The pre-flight gate in `lager update` accordingly requires a
  confirmed key -- `is True`, explicitly, rather than the bare truthiness it
  used before, which silently meant the same thing while reading as though
  no decision had been made.

  Reading "couldn't tell" as good enough was tried and is worse than the bug.
  A box with no key cannot authenticate at all, so on a real fleet absence
  arrives as "couldn't tell" far more often than as a definite no -- a
  definite no needs some other identity to log in and the key search to then
  miss. Waving it through reported "SSH key works" about a box that never
  authenticated, dropped `--check` from exit 2 to 1, replaced the actionable
  message with a bare permission-denied several steps later, and removed the
  only path that offers to install a key. "Couldn't tell" and "not installed"
  differ in what can be claimed, not in whether a usable key exists, so both
  now take the setup path -- and when the box could not be reached, the
  output says that rather than asserting the key is absent.

- **A box deployed from a branch now says so.** After
  `lager update --version <branch>`, `/etc/lager/version` was left unchanged
  and `lager hello` reported the same version string as before the deploy, so
  a box running a branch was indistinguishable from one on the release tag by
  any means the CLI offered. The only on-box trace was `/etc/lager/build-hash`,
  which is opaque and surfaced nowhere.

  The idempotence guard in `write_box_version_file` was not the bug. A branch
  whose `__version__` has not been bumped past the last release serializes to
  a string identical to the release tag's, so the guard correctly saw
  unchanged content. The bug is that the file records a version *number*,
  which carries no information about which ref produced it -- v0.36.2 and
  main-thirteen-commits-later are the same bytes.

  `lager update` and `lager install` now write `/etc/lager/ref` as
  `<ref>@<sha>` (`main@85c1b64`), the box reports it from `/status`, and
  `lager hello` prints it flagged when it is not a release tag:

  ```
  Version: 0.36.2 (main@85c1b64 -- not a release build)
  ```

  `lager boxes` names the ref in the version column for the same reason, since
  across a fleet that is how a box gets left on a branch and someone else runs
  a test against it believing it is on the release. The SHA matters as much as
  the branch name: "main" alone is not reproducible once main moves.

  A sibling file rather than a third field in `/etc/lager/version`, because
  four readers parse that file with `split('|', 1)` -- box_http_server's
  `/status`, the python service's `_read_box_version`, `mcp/config.py` and
  `mcp/engine/bench_loader.py` -- and a third field would have landed inside
  `updater_version` on every one of them.

  Boxes that predate the file report no ref and read exactly as they did
  before, rather than gaining an empty parenthetical they cannot fill.

- **`DebugNet.connect(script=...)` was ignored under the OpenOCD backend.** No
  error, no warning, no log line: the script was written to disk and then never
  read, because only the J-Link path passes it downstream. A caller passing a
  per-run attach script in-process -- the way a CI job avoids mutating shared
  box state with `lager nets set-script` -- got a run that silently used
  whatever attach sequence the net already had.

  `script` now works on both backends. A `.JLinkScript` is executed by the
  J-Link DLL and an OpenOCD `.cfg` is TCL read by the daemon, so the same file
  cannot serve both; the override is classified by extension, then by content,
  exactly as `lager nets set-script` already classifies one, and routed to
  whichever backend it is for. A script handed to the wrong backend now raises
  naming both formats, as does one that cannot be classified -- neither is
  routed on a guess. Invalid input (a missing path that is not valid base64)
  is still ignored, as before.

  Per-connect overrides are written to a per-net path rather than the box-wide
  cfg that the net record and the HTTP debug service share, so one session's
  override cannot reach another net, and `disconnect` clears it -- the same
  scoping J-Link scripts received in v0.38.0.

- **`gpio`, `i2c` and `spi` nets on an FT2232H could not be opened.** The
  instrument has advertised all three roles for as long as the role table has
  existed, so `lager nets add` accepted them; but the drivers addressed the
  device as `ftdi://ftdi:232h:...`, and `232h` is the product selector for the
  FT232H (PID 6014). It does not match an FT2232H (6010), so every such net
  failed to find its device. The part is now selected from the PID already
  present in the net's own address, which had been parsed and discarded.

- **An FTDI net whose address was written as a full `ftdi://` URL had it
  silently discarded.** The address was recognized as "not a serial number"
  and then dropped, with the hardcoded URL rebuilt over the top -- so a user
  who spelled out exactly which device and channel they wanted got interface A
  of the first FT232H instead. Such an address is now used verbatim.

## [0.42.0] - 2026-08-25

### Added

- **The weekly bench run now exercises the `lager supply` and `lager battery`
  CLI surfaces against real instruments.** `test/integration/power/supply.sh`
  (59 checks) and `power/battery.sh` (86) predate the bench having CI at all
  and had never run inside it, so a regression in either surface would have
  shown up only when someone happened to run one by hand.

  Serving them means `Bench: Extended` is no longer a dark-bench workflow: it
  gains the relay-net self-heal and AC-relay power steps from `Bench:
  Integration Tests`. The bench is energized for the two power suites only and
  returns to dark before the infrastructure suites, so those still run under
  the conditions their baselines were measured in.

  The relay steps are `continue-on-error` here rather than the hard gate they
  are in the nightly. There, failing fast is right because every suite needs an
  instrument; here five suites need none and have been running green weekly, so
  a dead relay must not take them with it. A relay failure still fails the job,
  through the same aggregation gate that already covers every suite.

### Fixed

- **`lager debug <net> erase` no longer reports "Erase complete!" when nothing
  was erased.** With the probe enumerated but the target unreachable over SWD
  -- unplugged, unpowered, or held in reset -- the command printed
  `Erase complete!` in green and exited 0 over a part it had never touched.
  The only hint anything was wrong was a yellow `Failed to reconnect after
  erase` warning printed *after* success had already been reported.

  This is the defect fixed for `flash` in v0.34.0, on the command next to it.
  `/debug/erase` answers 200 whether or not the probe ever attached -- the
  box's `chip_erase()` is a generator that yields J-Link Commander's stdout and
  carries no success channel, exactly like `flash_device()` -- and the CLI
  printed its success line without ever reading that text. Short of an HTTP
  error the command could not fail. The OpenOCD paths were already strict
  (`Da1469xLoaderError` and `OpenOcdRpcError` both surface as 500), so this was
  the J-Link backend only.

  Both halves now check. The box refuses to answer 200 for a session whose
  output shows it never attached, and the CLI takes its own verdict from the
  programmer's output, so a current CLI reports the failure correctly against a
  box that has not been updated yet.

  `flash` erases by default, and that pre-erase step discarded the box's reply
  entirely and printed `Erase complete!` unconditionally. It now takes the same
  verdict, and stops before programming a part that was never reached.

  As with `flash`, output matching nothing keeps its existing meaning, so an
  older box or a backend we have not characterised is never newly reported as
  failing.

  Confirmed against hardware: a J-Link Plus on an nRF5340, board unpowered,
  probe still enumerated. The box answered HTTP 200 with
  `status: erase_complete` for a session whose own output read
  `Error occurred: Could not connect to the target device.` -- twice, because
  `chip_erase()` runs `connect` then `erase` and both failed. Nothing was
  erased. A successful erase on the same bench still reports success, and its
  output carries a `CPUID register:` line one careless substring match away
  from a failure signature, which is why matching is whole-line.

  One deliberate asymmetry, because the two are easy to conflate: the predicate
  that decides a *verdict* is stricter than the one that triggers the flash
  path's *retry*. `Could not read CPUID register` drives the retry and does not
  decide the verdict, because J-Link emits it per access port while scanning
  and it does not on its own establish that the session never attached. As a
  reason to try again that costs one attempt; as a reason to call an operation
  failed it would report completed work as broken. Every captured failure
  prints it alongside `Could not connect to target.`, which both predicates
  match, so nothing is lost.

- **`lager install`'s deploy budget is now configurable, and the lock TTL
  follows it.** The deployment step was killed after a hardcoded 30 minutes,
  a literal in both the `subprocess.run` call and the message that reported
  it, with nothing reading an override. That budget covers the cold container
  build, which is the longest step by far -- roughly 14 minutes on ordinary
  box hardware, so the default was about a 2x margin. The margin disappears on
  anything slower: an emulated x86-64 guest, a low-power mini PC, a throttled
  VM, a cold apt cache. A healthy build then exceeded the limit and was cut off
  mid-build, after the previous container had already been removed, leaving the
  box with nothing running and the operator no way to retry with more time.

  `--timeout <seconds>` and `LAGER_INSTALL_TIMEOUT` now set it, flag winning
  over environment over the 1800-second default; `0` removes the bound
  entirely. A negative environment value falls back to the default rather than
  clamping to 0, because 0 means *unbounded* here -- clamping would turn a typo
  into an install with no deadline at all.

  The auto-lock TTL is derived from the resolved timeout rather than being a
  second literal sized against the first. It was 3600 precisely because the
  deploy timeout was 1800, and the comment said so; left fixed, a
  `--timeout 5400` install would have had its own lock reaped mid-deploy. It
  now tracks the budget and keeps 3600 as a floor. An unbounded deploy takes no
  TTL, since no finite one can outlast it.

  The timeout message now names the override, states that a re-run is safe and
  reuses whatever layers the interrupted build cached, and says the budget is
  not a verdict on the box -- the build may well have been progressing normally.

  The published documentation told operators to expect "up to 30 minutes",
  which was exactly the point at which the tool gave up. The documented
  expectation and the hard failure threshold are no longer the same number.

- **`lager python --detach` now returns as soon as the box has accepted the
  job, instead of after everything that makes a job slow to start.** Every step
  before the process was spawned ran inside the HTTP request: unpacking the
  module, `pip install -r requirements.txt` with no bound on it, the quiesce
  gate that can wait 69 seconds for a previous job's teardown, and the
  direct-USB handoff. A detached launch of a module carrying a
  `requirements.txt` therefore blocked the CLI on its 320-second read timeout
  before it could see the response saying the job had detached -- the one thing
  `-d` exists to avoid.

  The box now answers the client first and does all of that on one background
  thread. The job's registry entry -- `meta.json` and `output.log` -- is written
  before the response, so a `--reattach` issued immediately opens a file that
  exists rather than getting a 500, and `meta.json` gained a `starting` state to
  say so.

  **A failure to start is now reported through the job rather than to the
  launch.** A broken `requirements.txt` used to come back as an HTTP 422 the
  user saw at once, after the wait. It now happens after the response, so the
  pip transcript is written into the job's own log as stderr followed by an exit
  marker and `meta.json` reaches `failed` with return code 1 -- the same code
  the attached path reports for the same failure. `lager python --reattach <id>`
  shows the pip output and exits 1. That is a real loss of immediacy at launch
  time, and it is inherent: any wait long enough to catch a pip failure is the
  wait `-d` exists to avoid.

  Also fixed here: a detached run whose request carried no `LAGER_PROCESS_ID`
  registered itself under the literal directory `/tmp/lager_processes/None`. The
  box now mints an id and injects it into the child's environment, without which
  `--kill <id>` could never have found the job -- a job is located by reading
  `LAGER_PROCESS_ID` out of `/proc/*/environ`, not by its directory name.

- **`--timeout` now applies to `--detach`, without the box ceiling.** A detached
  job was never wrapped in `/usr/bin/timeout`, on the stated grounds that the
  wrapper would become the group leader `_signal_targets` reasons about. That is
  a true statement of fact but not a reason: `start_new_session` makes the
  wrapper a process-group leader whose child inherits its group, which is
  exactly the arrangement `_signal_targets` was written for and exactly what the
  attached path already does. A detached job is now wrapped whenever a deadline
  was actually asked for -- and only then, so the default `-d` path keeps the
  process tree it always had.

  `MAX_TIMEOUT` is deliberately not applied to it. That ceiling exists because
  the CLI's streaming read timeout is 320 seconds, and nothing streams a
  detached job; capping `-d --timeout 3600` to 300 would cut short exactly the
  long run `-d` exists for.

- **A detached run no longer holds the box lock forever.** The lock was acquired
  with `ttl_seconds: null` because the CLI's heartbeat thread dies with the CLI,
  and released by hand. That is workable while the job runs and a trap when it
  does not: a detached job that failed to start left the box locked with nothing
  running on it.

  The box now holds that lock for exactly as long as the job it launched --
  heartbeating while it runs, releasing when it ends however it ends. It can
  only ever touch the holder the CLI handed over, and only a lock the CLI
  freshly acquired is offered, so a `lager boxes lock` reservation the run
  merely resumed is never handed over and never released. The CLI arms the
  lapse TTL only once the box confirms it has taken over, so a newer CLI against
  a box too old to know about the handoff keeps today's eternal hold instead of
  letting the lock lapse under a running job.

- **The box's JSON responses now carry a `Content-Length`.** They were delimited
  by the socket closing, which worked only because nothing in `box/` sets
  `protocol_version` and `BaseHTTPRequestHandler` therefore defaults to
  HTTP/1.0 -- an invariant nothing stated and nothing tested, while
  `parse_multipart`'s own comment assumed the opposite. Streaming responses
  cannot carry a length and now say `Connection: close` instead of relying on
  that default. There is a test pinning `protocol_version`, because raising it
  to HTTP/1.1 would leave every streamed run waiting for a body end that never
  comes.

- **Four output-state checks in the supply and battery suites could never have
  passed.** `supply.sh` tests 3.3 and 3.5 and `battery.sh` tests 4.2 and 4.4
  matched the output of `lager supply|battery <net> state` against
  `disabled`, `output ... off` and `enabled: ... on`. The command reports
  channel state as `Channel <n>: OFF` / `Channel <n>: ON` and carries none of
  those words, so all four failed against a supply that was working correctly.
  They now match the shipped format, anchored on the channel field --
  `battery state` also prints a `Mode:` field carrying ON/OFF, and that must
  not be what decides the check.

  Found by running the suites for the first time. Fixed rather than
  baselined: a baseline of 4 here would have recorded "matches a format the
  CLI has never emitted" as the expected state.

## [0.41.0] - 2026-08-24

### Added

- **`lager update --version` and `lager install --version` accept a commit
  SHA.** A full 40-character SHA resolves to that exact commit; a release tag
  and a branch behave as before. Only the full 40 is accepted, because a short
  hex prefix cannot be told apart from a branch name.

  This exists because a branch is not a stable target. `--version main` is
  re-resolved against `origin/main` every time it is evaluated, so two
  invocations minutes apart can mean two different commits -- which is fine for
  "bring me up to date" and wrong for "is this box running the code I am
  testing". CI now pins both halves of a bench run to one commit and asks the
  second question properly.

  A commit has no pre-built image (only release tags are published), so a SHA
  target builds on the box, exactly as a branch does. The box must be able to
  reach the commit: it has to be on some branch or tag on the remote, and a
  commit that only ever existed in a pull-request ref, or that was force-pushed
  away, is refused with that reason rather than "not a tag or branch".

### Changed

- **The bench no longer runs on every push to `main`.** `Bench: Integration
  Tests` had a push trigger but no deploy step -- it only ever probed the box
  with `lager update --check`, and the one job that deploys is reached through
  the nightly chain or a dispatch. A push-triggered run could therefore only
  test whatever the previous night left on the box; the guard that catches this
  found it 1, 2, 3, 4 and 8 commits behind across a single afternoon, and
  correctly refused every one.

  Deploying on each push instead would cost roughly seven hours a day of a
  bench there is one of: a 48-minute suite against about nine pushes, all
  serialized, with merges queueing behind each other. So the bench runs
  nightly and on demand, and the trigger that could only produce refusals is
  gone. Bench-testing a specific commit is a `workflow_dispatch` after updating
  the box to it -- see `.github/workflows/README.md`.

- **Both bench jobs pin to the run's own commit instead of to `main`.** The
  lifecycle job deployed `main` unconditionally while the guard compared
  against `main` re-resolved at probe time, so any merge landing between the
  two made the box read as stale when it was running exactly the commit under
  test. Both now use `github.sha`, which is fixed for the life of a run, and in
  the nightly chain they share one run so the value cannot move between them.
  The N-1 -> current upgrade regression the lifecycle job exists for is
  unchanged; only its target is now named exactly.

- **The bench lifecycle's recovery step says when it leaves the bench without
  box software.** Recovery exists to turn a transient failure into "red run,
  healthy bench", and its `|| true` guards keep it from adding a second failure
  to an already-red run. But `|| true` alone also left it unable to report its
  own failure: when a broken sudoers file made `lager install` fail, recovery's
  reinstall failed the same way, the guard swallowed it, and the step reported
  success while the box had no container at all. The exit codes stay non-fatal;
  `lager hello` is now the verdict, and a bench that is still not answering
  raises an error annotation naming the command that fixes it.

### Fixed

- **`lager install` now completes on a box running `sudo-rs`.** Ubuntu switched
  the default `sudo` to `sudo-rs` in 25.10 and 26.04 LTS ships it as the
  default, and `sudo-rs` rejects wildcards in command arguments by design.
  Nineteen of the rules Lager wrote to `/etc/sudoers.d/lagerdata-udev` used
  one, so `visudo -c` failed and the install aborted at step 2 of 9, before
  anything was deployed -- on a configuration the documentation calls
  supported.

  Every rule now names its arguments exactly. The staged rule and modprobe
  files are granted by filename rather than by `/tmp/*.rules`, which is also a
  narrower grant: the glob matched any file an attacker could stage in a
  world-writable directory. The mode and owner wildcards are replaced by the
  values actually applied, which made the list *shorter* -- the `chown -R`
  call sites pass three arguments and so never matched a two-argument
  `chown * /etc/lager` rule in the first place, and `chown` on
  `/etc/lager/version` has no call site at all. The login user's gid, the one
  genuinely dynamic value, is resolved on the box. The firewall script's
  trailing wildcard is replaced by the exact invocation, since its only
  argument form (`--corporate-vpn <iface>`) is known when the file is written.

  Two things found while fixing it. The file was installed *before*
  `visudo -c` ran, so a validation failure left the box with a broken
  `/etc/sudoers.d` -- worse than not having tried. It is now staged, validated
  with `visudo -c -f`, and installed only on success. And the manual-fix text
  `lager box config apply` prints on a box missing the grant taught the same
  globbed rules, handing an operator a file their own `visudo` would reject on
  exactly the release where they were most likely to need it.

  A contract test pins every rule as wildcard-free, and pins that the literal
  grants still match the commands the CLI actually runs -- byte-for-byte, since
  sudo compares the command line verbatim and a trailing-slash difference is a
  silent "a password is required". That test is the only thing that can catch
  this class of bug: every box we can reach carries a blanket
  `(ALL) NOPASSWD: ALL`, so the narrow grants are never exercised on hardware
  and a broken one is invisible to a green bench run.

- **`DEBIAN_FRONTEND` and `NEEDRESTART_SUSPEND` now reach `apt` during
  install.** They were passed as `sudo VAR=value apt-get ...`, and sudo's
  default `env_reset` discards assignments made on its own command line unless
  the authorising sudoers rule carries `SETENV`. During install no Lager
  sudoers file exists yet -- apt runs under the operator's own rights -- so
  both variables were dropped without a word, and `needrestart` ran during
  installation, which is precisely what setting them was meant to prevent. Its
  service-restart scan appearing in the log is the proof they never arrived.

  The nine install-path invocations now use `sudo env VAR=value apt-get ...`,
  which runs `env` as root and lets it set the variables, so nothing depends on
  sudoers policy.

  Deliberately **not** applied to the two call sites that run on a provisioned
  box (`lager update`'s venv prerequisite and `lager box config apply`'s
  package install). Those rely on `NOPASSWD: SETENV: /usr/bin/apt-get`, which
  authorises *apt-get*; under `sudo env` the command run as root is
  `/usr/bin/env`, the rule stops matching, and `sudo -n` is refused. Keeping
  `sudo VAR=` there is also the narrower grant, since permitting `/usr/bin/env`
  would permit every binary. Both halves are pinned by tests, because applying
  either shape uniformly silently breaks the other's call sites.



- **`lager --box ""` no longer silently runs against your default box.** An
  empty string is falsy, so an explicitly empty `--box` fell into the "no box
  given" branch and resolved to whatever the default was -- the caller named
  one box and got another, with nothing in the output saying so. It is now
  refused, matching `lager boxes add --name ""`, which already rejected an
  empty name. Both box resolvers are guarded: they duplicate the resolution
  logic rather than one delegating to the other, so a guard in only one would
  have left the other's callers still defaulting. Omitting `--box` is
  unchanged and still uses the default box.

## [0.40.0] - 2026-08-21

### Added

- **A Getting Started guide covering box setup end to end.** Nine new
  pages under `docs/source/getting-started/`, including setting up a Lager
  Box, adding a first box, instruments, nets, a first test, a glossary and
  troubleshooting -- and the sudo-rs behavior an operator hits on Ubuntu
  25.10 and newer.

- **`lager install` now uses the pre-built box image for a release tag, taking a
  fresh install from about 14 minutes to about 2.** Building the box image is by
  far the slowest part of an install, and an install always pays the full cold
  cost: the deployment prunes the builder cache before it starts, so there is
  never a warm layer cache to reuse. `lager update --pull` already avoided that
  build for release tags; install was the one path that could not.

  On by default here, unlike `lager update`. The reason update's pull stays
  opt-in -- it loses to a warm layer cache on a code-only update -- cannot apply
  to an install, whose cache is always cold. `--no-pull` forces a local build,
  and `LAGER_BOX_IMAGE_PULL=0` does the same for a whole shell.

  Only release tags publish an image, so `--version main` and other branch
  targets still build on the box and now say so, with the time a release tag
  would have cost instead.

  The image is verified the same way `lager update` verifies it: resolved to an
  immutable digest and pulled by digest rather than by tag, pulled anonymously
  through a throwaway docker config so a box's unrelated registry credentials
  cannot deny it, pinned to the box's architecture, and required to carry an
  `org.opencontainers.image.version` label naming the exact tag requested. An
  unlabelled or mismatched image is discarded rather than trusted. Every miss
  falls back to the local build -- a slow install that works beats a fast one
  that does not. The digest that was used is recorded in
  `/etc/lager/image-source`, which `lager install` previously did not write at
  all.

  The decision about which versions have a published image is made in the same
  conditional that already resolves a semver pin to a release tag, so "what has
  an image" cannot drift from "what has a tag". A test pins that agreement
  against the client's own answer.

### Changed

- **ShellCheck now covers the same files as the `bash -n` syntax check.** The
  two steps had drifted to different scopes, and the gap held the shell with
  the most to get wrong: the 1600-line box provisioning script, the firewall
  script and the box start scripts all run as root, and none of them were
  linted. All eleven newly covered files pass at the existing severity with no
  new exclusions.

- **The box's MCP server is ported to MCP Python SDK v2, and the `mcp`
  dependency is uncapped.** v0.33.1 pinned `mcp` below 2.0 in both the box
  image and the CLI's optional `mcp` extra, because SDK 2.0 renamed `FastMCP`
  to `MCPServer` and moved transport configuration (`host`, `port`,
  `transport_security`) off `mcp.settings` onto `run()` /
  `streamable_http_app()`. `box/lager/mcp/server.py` now uses the 2.x API and
  both constraints move to `>=2.0.0,<3` -- a floor, not just a wider ceiling,
  since the server can no longer import under 1.x. Boxes pick this up on the
  next `lager update`.

  Two behaviors worth knowing about, neither visible in the tool surface:

  - SDK 2.0 removed the ambient `mcp.get_context()`, so the per-request
    context is now injected as a tool parameter and handed down explicitly.
    `discover_bench` and `discover_dut` still echo the address you connected
    on into their `lager python ... --box <addr>` hint; that address is read
    from the request `Host` header, falling back to the socket peer, exactly
    as before.
  - DNS-rebinding protection now auto-arms inside `streamable_http_app()`
    unless transport security is passed explicitly. The server passes it, so
    a box reached at its LAN address keeps answering rather than returning
    `421 Invalid Host header`.

  The MCP endpoint is unchanged: `http://<box-ip>:8100/mcp`.

- **`mcp` is now a direct test requirement instead of arriving under
  `fastmcp`.** Nothing in the tree imports `fastmcp`; it was listed only as a
  way to pull the SDK in, and its own dependency chain capped `mcp` below 2.0
  -- which silently decided which SDK major every unit suite ran against, and
  would have made the test requirements unresolvable against the widened
  extra. `test/requirements-unit.txt` names `mcp` itself.

### Fixed

- **The deploy script's ssh wrapper no longer leaves a temp file behind on
  Ctrl-C, and no longer fails a deploy over one it cannot create.** `ssh_t`
  captures ssh's own stderr so it can filter it in order, and it allocated
  that capture file per call -- which put two new ways to fail on a path that
  runs a dozen times per deploy. The `rm -f` sat after the ssh call, so an
  interrupt of a script that runs for half an hour skipped it and left the
  file in `TMPDIR` for good. And a `TMPDIR` that could not be written aborted
  the deploy at the assignment under `set -e`, before ssh ran, leaving
  `mktemp`'s own message and nothing else. There is now one capture file per
  run, created where the failure can be explained and removed by the exit
  trap, which does cover Ctrl-C. The filter also reads it as text: a single
  NUL byte in the stream made `grep` call the file binary and print
  `Binary file ... matches` in place of the error line the operator needed.

- **A failed Docker install no longer points at a line that may not be there.**
  The step named the failing command unconditionally, but the ssh session can
  fail on its own -- a connect timeout, a rejected host key, a dropped
  multiplexed connection -- and two links of the chain are deliberately
  unwrapped because a failure there is not fatal. In any of those cases no
  `[lager] STEP FAILED` line is printed, and the operator was sent looking for
  one. The wording is now conditional and says what it means if the line is
  absent.

- **The boot-enable check tells "could not reach the box" apart from "the unit
  is disabled".** `systemctl is-enabled` answers 0 for enabled and 1 for
  disabled or masked, while ssh answers 255 when it never reached the box at
  all. Folding those together stated a fact about the unit from an exit code
  that never got near it.

- **A failed Docker install step now names the command that failed.** The step
  ran eight commands as one `&&` chain behind a single
  `[ERROR] Failed to install Docker`; four of them print nothing on success, so
  a failure in any of those left a transcript that simply stopped, with no
  command named and nothing to act on. Each link now reports its own label and
  exit status, the failure message points at that line, and it offers
  `systemctl status docker` / `journalctl -xeu docker.service` when the
  packages landed and the daemon is the likely cause.

- **`ssh_t` no longer prints ssh's own errors out of order.** The
  "connection closed" filter ran in a process substitution, which bash does not
  wait for, so a real diagnostic ("Permission denied", "Connection refused")
  could land after the caller had already printed its generic failure -- 26 of
  200 runs against a stub ssh that fails immediately, 0 of 200 after. The
  filter now runs over captured output, in order.

- **The printed manual-recovery commands are equivalent to the step they
  replace again.** `systemctl enable docker` was missing from them, and a
  re-run skips the whole install block once `command -v docker` succeeds, so a
  box recovered by hand worked until its next reboot and then came up with no
  docker daemon. The `enable` in the container step also announced success
  whether or not it worked; it now checks `systemctl is-enabled` and warns when
  the unit is not enabled. `systemctl enable docker` is granted on both
  `/bin` and `/usr/bin` in the generated sudoers, matching `restart` and
  `reset-failed`.

- **A deliberate `ctx.exit()` is no longer reported as a crash, and no longer
  has its exit code rewritten to 1.** `click.exceptions.Exit` subclasses
  `RuntimeError`, so a broad `except Exception` caught every intentional exit
  raised inside its own `try` block: the command printed a Python traceback for
  a designed exit, rendered the exception payload as the message (`Error: 2`),
  and then exited 1.

  The visible case was `lager update --check` against a box whose SSH key is not
  set up. That path asks for 2 -- "the probe could not run" -- and delivered 1,
  which in `--check`'s vocabulary means "an update is available": a claim the
  command was in no position to make, having never reached the box. Anything
  branching on the code got the wrong answer, and
  `integration-tests.yml`'s `rc -gt 1` branch was unreachable.

  It was not one call site. Eleven `try` blocks across seven files had the same
  shape, and two were doing visible damage of their own:
  `lager binaries remove <nonexistent>` printed "binary not found", had its
  `ctx.exit(1)` cancelled outright by an `except Exception: pass`, and continued
  on into the removal; `lager uart` treated the `ctx.exit()` that ends a session
  as a connection error, retried the whole session, and rewrote the session's
  exit code to 1 -- worked around until now by comparing `str(last_error) != "0"`
  against `str(Exit(0))`, which is now deleted. All eleven re-raise `Exit` and
  `Abort` ahead of the broad handler, as `cli/commands/box/ssh.py` already did.

  `tools/check_control_flow_handlers.py` runs in the `static-checks` gate to keep
  the shape from coming back. It is ordering-aware -- a handler placed after
  `except Exception` never runs -- and its own detection cases are tested, since
  a gate that cannot fail is not a gate.

  `lager update`'s traceback is now printed only under `--verbose`, and to
  stderr rather than stdout, where it had been corrupting piped output.

- **A CI job is no longer refused by its own box lock.** Auto-lock acquires with
  `get_lock_holder()`, which under CI is a per-process identity ending in the
  pid, but the pre-command check compared the stored holder against
  `get_lager_user()`. Those two strings can never be equal in CI, so every
  command after the first was refused by the lock the first one had just taken,
  and the error named the running job as the culprit. It was invisible on a
  developer machine, where `get_lock_holder()` falls back to `get_lager_user()`
  and both sides of the comparison are the same string.

  The check now compares lock *scope* -- the holder with its per-process pid
  removed -- so consecutive commands in one job match, while two jobs of one
  run, and two runs of one workflow, stay distinct as before. It still accepts a
  plain user, because `lager boxes lock` and the bash test harness record one;
  fixing only the CI identity would have broken those. `LAGER_LOCK_HOLDER` now
  works end to end, having previously been unable to satisfy the comparison at
  all.

  The same comparison appears four times on the lock path, and all four now
  compare scope: the pre-command check, the pre-acquire probe, the
  `previous_user` classification after an acquire, and the conflict branch that
  decides whether to wait. Fixing only the first would have been worse than the
  original bug -- the command would stop being refused and instead block on the
  wait loop for `LAGER_LOCK_WAIT`, 1800s under CI, on a lock it already held.

- **The host CLI installs to `~/.lager_venv`, because `~/.lager/venv` could
  never work.** `~/.lager` is the CLI's own global config file and box registry
  (`config.DEFAULT_CONFIG_FILE_NAME`, and the path `box_storage` reads). The
  host-CLI feature shipped in v0.32.6 put its venv *inside* that same name, so
  the two collided -- one wanting a file, the other a directory -- and whichever
  was created first made the other impossible:

  - Config file first: `python3 -m venv "$HOME/.lager/venv"` fails with
    `[Errno 20] Not a directory`, permanently. Every update reported
    `venv creation failed (is the python3-venv package installed?)` on hosts
    where that package was installed and working -- the message named a cause
    nothing had checked.
  - Host CLI first: the venv works, and the CLI on that host can no longer read
    or write its config or box registry (`IsADirectoryError`). That is worse
    than not installing it, because the CLI runs but cannot keep state -- and
    the whole point of the feature is that someone SSH-ing in gets a working,
    version-matched CLI.

  Measured across the fleet: every box was in one state or the other, so the
  feature had not worked anywhere since it shipped. `~/.lager_venv` follows the
  convention the rest of the host-side state already uses
  (`~/.lager_update_check`, `~/.lager_gateway_auth`).

  Boxes carrying the old venv are migrated on their next install or update: the
  stale `~/.lager/venv` is removed and `~/.lager` is retired with `rmdir` --
  guarded on the path being a directory, and `rmdir` rather than a recursive
  delete, so it is inert wherever `~/.lager` is the config file and cannot take
  anything else with it.

  The exit-42 message now points at the command's own error instead of guessing,
  and that error is printed whether or not `--verbose` is set. A failed host-CLI
  step is also repeated in the end-of-run summary, next to
  `<box> updated to version ...`; previously the only mention was one yellow
  line inside a 19-step progress render that then printed `Complete!`, so a box
  looked fully updated while a shipped feature was silently absent.

  Two pieces of drift surfaced while pinning the two implementations together:
  `setup_and_deploy_box.sh` created the `~/.local/bin/lager-mcp` symlink that
  `_host_cli` deliberately removes, and its exit-41 text had diverged from the
  module's. The drift guard now compares each exit-code message exactly rather
  than by substring, which is what let them diverge unnoticed.

- **A command that dispatches to a missing helper script now says so, instead of
  raising a `ValueError` traceback that looks like a box problem.**
  `get_impl_path()` searched `cli/impl/{power,measurement,communication,device}/`
  with `os.path.exists`, then fell through to the root `impl/` directory and
  returned that path **without checking it existed**. A caller asking for a
  script that is not in the tree received a well-formed path to a file that is
  not there.

  Nothing failed at that point. The dead path travelled on to
  `run_python_internal`, which raised a bare
  `ValueError: Could not find runnable ...` -- and by then the box had been
  resolved and the net validated over the network, so the traceback read as a
  box or connectivity fault rather than a missing file. Sixteen `lager logic`
  subcommands (`measure`, `trigger` and `cursor`, dispatching to
  `measurement.py`, `trigger.py` and `cursor.py`) had been failing exactly that
  way, with nothing pointing at the reason.

  `get_impl_path()` now checks the root fallback like every other candidate and
  raises a `LagerError` naming the script, with the searched directories under
  `--debug`. The root fallback still resolves -- `cli/impl/box_config.py` lives
  there -- so this is a missing check, not a removed code path.

  `test/unit/cli/test_impl_script_dispatch.py` walks every `run_backend` and
  `get_impl_path` call site in `cli/` and asserts each script name resolves.
  Nothing could see this before: the scripts are read off disk and uploaded to
  the box rather than imported, so no import test, linter or type checker
  resolves the filename strings that couple a command to its implementation.
  The three scripts `lager logic` needs are recorded in a two-sided
  `KNOWN_MISSING` baseline (#261) -- a new unresolvable dispatch fails, and so
  does a listed name that starts resolving, so the baseline can only shrink.
- **A Rigol MSO5204's logic channel can now have a net.** `lager instruments`
  advertised `logic: 1` on the scope, but `lager nets add <name> logic ...` was
  refused: the instrument's role list said `scope` only, while its channel map
  listed both. `lager nets add` is the only gate on role -- the box stores what
  it is given -- so the CLI's narrower copy made `lager logic` unusable on the
  one instrument in the fleet that does logic capture.

  The roles are written down three times (the box's `SUPPORTED_USB` and
  `CHANNEL_MAPS`, and the CLI's `INSTRUMENT_NET_MAP`), which `nets.py` already
  flagged as duplication. This was the only instrument where they disagreed;
  `test_instrument_role_tables.py` now asserts all three agree for every
  instrument, so the next omission fails a gate instead of quietly removing a
  capability.

- **`lager python --timeout` now stops the script.** The option was never being
  dropped -- it reached the box and was applied as `/usr/bin/timeout N` -- but
  GNU `timeout` sends SIGTERM at the deadline and nothing more. A script that
  does not return from SIGTERM therefore did not stop: one blocked in an
  uninterruptible call (a pyvisa, libusb or serial read, which is the normal
  case on a box) or one that installs its own handler. `--timeout 3` against a
  30-second script was measured still running 17 minutes later, ended by a CI
  step timeout rather than by the timeout it was given.

  The wrapper now carries `--kill-after`, so the deadline escalates to SIGKILL
  after `CLEANUP_GRACE_S` -- the same escalation `_signal_and_reap` already
  applied wherever else a job is stopped. Verified on a box: a script that
  installs a SIGTERM handler and sleeps 30 seconds now returns in 9 with
  `--timeout 3`, and one that honours SIGTERM still exits 124 at its deadline,
  unchanged.

- **A script killed by its timeout reports 137 instead of 247.** Fixing the
  above exposed a second defect immediately behind it. GNU `timeout` puts itself
  in its child's process group, so the SIGKILL it sends at the end of the grace
  window kills the wrapper too; the box reports Python's `Popen.wait()` value,
  which is `-9` for a signal death rather than the 137 a shell would show. The
  CLI passed that straight to `sys.exit`, so the caller saw 247 (256-9) and no
  explanation -- and `SIGKILL_EXIT_CODE = 137` matched nothing, leaving
  "Script forcibly killed due to timeout." unreachable, as it had been for the
  whole life of the feature.

  Box-reported codes are now mapped onto the 128+N convention those constants
  are written in. `-1` is passed through: it is `FAILED_TO_RETRIEVE_EXIT_CODE`,
  and also what `terminate_process` returns when it had to kill something, and
  also SIGHUP death -- already indistinguishable on the wire, and mapping it
  would invent a signal nobody sent.

- **`--timeout` above the box's ceiling says so instead of quietly running
  shorter.** Values over `MAX_TIMEOUT` were reduced by a bare `min()`, so a job
  asking for 600 seconds ran 300 with nothing said -- which reads as the timeout
  firing early rather than as a ceiling being applied. The ceiling is unchanged
  and now logged. `--timeout` is also a no-op with `--detach`, now stated in
  `--help` and logged rather than left to be discovered.

  A regression test pins the box ceiling below the CLI's HTTP read timeout. The
  two sit either side of the wheel boundary with 15 seconds between them, and a
  deadline the client stops waiting for reports a connection error rather than a
  timeout.

- **The sixteen `lager logic measure` / `trigger` / `cursor` subcommands work
  again.** They dispatched to `measurement.py`, `trigger.py` and `cursor.py`,
  which had been consolidated into `scope.py` -- the actions themselves never
  moved, and `scope.py` has handled all three families for PicoScope and Rigol
  the whole time. Only the three helpers in `logic.py` were left naming the old
  files, so every one of these subcommands failed after resolving a box and
  validating a net over the network, which made a local dispatch fault read as a
  box or connectivity problem.

  Two of them were wrong in a second way: `measure pw-pos` and `measure pw-neg`
  sent `measure_pw_pos`/`measure_pw_neg`, but `scope.py` registers those actions
  as `measure_pulse_width_pos`/`measure_pulse_width_neg` -- the names
  `lager scope` has always sent. Repointing the file alone would have left these
  two broken, in a way a file-existence check cannot see.

  `test_logic_dispatch_actions.py` now asserts the real contract: every action a
  command sends is one the script it targets actually handles. The previous
  check was that the script *file* existed, which the pulse-width pair satisfied
  while still being undeliverable.

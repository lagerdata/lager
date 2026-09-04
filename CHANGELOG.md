# Changelog

All notable changes to the Lager platform are documented here. For detailed release notes, see [docs.lagerdata.com](https://docs.lagerdata.com).

## [Unreleased]

<!-- Keep this heading. A branch cut before a release and merged after it
     files its entry here; without it the entry lands inside the released
     section below, with no merge conflict to catch it. -->

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
  script knows by name -- confirms the control-plane ports are admitted there,
  and prints the exact per-interface `ufw allow` command when they are not. It
  never opens a port itself: whether Lager's control plane belongs on a LAN is
  a security decision, not a side effect of a Bluetooth feature. It also
  refuses on a box fronted by a port-publishing gateway, where the container
  would bind ports the gateway already holds and fail to start. A refused
  apply changes nothing; `--skip-host-network-check` overrides it.

  The recovery path is in-band. `network-mode set|show|unset` and the
  post-bounce steps of `apply` fall back to SSH when the HTTP path cannot reach
  the box, and the post-bounce readiness check asks the box on loopback rather
  than over a route the switch may just have closed. Without that last part the
  applied-hash was never stamped, and every later `apply` re-bounced the
  container indefinitely.

  The default is unchanged, and the setting is stored so that it does not
  disturb boxes that never use it: a config sitting at the default writes no
  key, so its hash is byte-for-byte what it was before this release. Without
  that, every box in the fleet would report configuration drift and take one
  pointless container restart on its next `apply`. A box whose lager predates
  the verb is told to run `lager update` rather than shown the dispatcher's
  raw `unknown command`.

### Changed

- **`lager box-config apply` now exits 3 when the container is up but running
  the previous config.** `start_box.sh` has always separated that from a failed
  bounce, but `apply` collapsed eleven distinct outcomes into exit 1, so a
  script could not tell "the box is fine, your config did not land" from "the
  bounce failed and the container may be down". The three codes are now `0`
  applied, `3` up on the previous config, `1` everything else. Anything that
  tested only for non-zero is unaffected.

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

## [0.39.1] - 2026-08-19

### Fixed

- **`lager supply <net> tui` now reaches the box when `--box` is a saved name.**
  The subcommand resolved the box name to its address, then passed the *raw*
  `--box` value to the net-listing validation, so the listing was requested from
  a host literally named after the saved box name -- which resolves only by
  accident, if that name also happens to be a real DNS name. Where it does not,
  every listing failed, came back empty, and the command reported `'BATT' is not
  a power supply net` while `lager nets` listed that exact net. Only the TUI
  subcommand was affected; every other `supply` subcommand already resolved
  first. Passing an IP always worked, and so did a name DNS happened to resolve,
  which is why the bug looked intermittent. Present since 0.32.0, when net
  listing moved to the `:9000` HTTP API and the previously harmless argument
  became the URL host.

- **A net check against an unreachable box no longer reports the net as
  missing.** `fetch_nets` returns `[]` both when a box has no nets and when it
  cannot be reached, and `validate_net_exists` read that as "not found" -- then
  advised creating a net that already existed on the box. It now distinguishes
  the two and reports the connectivity failure instead.

## [0.39.0] - 2026-08-19

### Added

- **`lager update --pull` now pulls anonymously, so a box's own registry
  credentials cannot disable it.** A box that ever authenticated to `ghcr.io`
  for something unrelated sent those credentials on the pull; GHCR evaluated
  them against the `lager-box` repository rather than falling back to
  anonymous, and answered `denied: denied` for a package anyone can read. The
  update recovered -- the denial is classified and falls back to a local build
  -- but the pull silently stopped helping that box for a reason unrelated to
  the image. The pull now runs through a throwaway empty docker config, so it
  behaves identically on every box. Verified on a box by planting a
  deliberately wrong `ghcr.io` credential and confirming the pull still
  succeeded.

- **A box that cannot build is now told it can still pull.** When Docker >= 23
  has no buildx plugin, `lager update` fails the BuildKit preflight and tells
  the operator to install the plugin. That is the slower fix and sometimes not
  one they can apply, while a pull needs no buildx at all -- so the error now
  also points at `--pull` for targets that have a published image.

- **`lager usb <net> cycle` power-cycles a port** — off, wait, on — for every
  supported hub. `--off-time` sets how long the port stays unpowered (default
  1s, range 0.5-10s); the default is set above the slowest cold boot measured on
  real hardware, because too *short* an off time is the failure that matters: the
  DUT's rails do not fully discharge and it warm-starts while appearing to have
  been reset. Drivers that can hold the hub for the whole sequence do, so no
  other caller can switch the port while it is dark, and power is restored on
  every failure path.

- **`lager usb <net> recover` re-powers a port left dark** by an interrupted
  command. On a Plugable dock this re-asserts power on every port of that dock,
  which is the situation it exists for: something died partway through and it is
  not obvious what is off.

- **Plugable RTS5411 USB docks are supported as switchable-USB-power
  instruments** (`lager usb <net> enable|disable|toggle|state|cycle|recover`),
  adding a third option alongside Acroname and Yepkit hubs. Plugable ships no
  SDK, so control is the standard USB hub class per-port power switching over
  pyusb rather than a vendor library. Notes:
  - Matches VID:PID `2230:5411`, which Plugable reuses across its RTS5411 dock
    line, so the instrument is named `Plugable_USB_Hub` rather than for one
    model. Developed and validated against a UD-CAM.
  - **Only the four external Type-A sockets switch VBUS.** A dock cascades two
    identical hubs and the upstream one is entirely internal (billboard, audio
    codec, ethernet); its ports do not appear to switch power. Both hubs
    advertise per-port switching identically, so the descriptor cannot tell them
    apart — only the topology can, and only the external tier is ever exposed.
  - These hubs report no serial number and a dock enumerates two of them, so the
    saved address carries a USB topology path (`...::port-1-1.4::INSTR`) instead
    of a serial. That pins the net to a physical box port: re-cabling the dock
    breaks the net loudly rather than silently driving different hardware.
  - A hub advertising ganged or absent power switching is refused outright
    rather than degrading to a silent no-op that would cut every port at once.
  - Ports whose downstream subtree carries a network device are refused by
    default (override with `params.allow_network`), since cutting one can drop
    the box off the network.
  - SuperSpeed-linked docks, where VBUS drops only when both virtual halves are
    switched, are handled in code but have **not** been validated against
    hardware; anything ambiguous is refused rather than half-switched.
  - Requires the udev rule for vendor `2230`; without it libusb cannot open the
    hub. Shipped in `99-instrument.rules`, or add it ahead of a release with
    `lager box-config udev add 2230:5411`.

### Fixed

- **Disabling a USB hub port no longer reports a false failure and undo itself.**
  The Plugable driver confirmed a power-down by waiting for the device to
  disappear from sysfs. It never can: with the port unpowered the hub raises no
  change bit, so the kernel never polls the port and never processes the
  disconnect — the device node, its `lsusb` entry, and any `/dev/ttyUSB*` all
  persist for the whole off window, and the kernel logs `USB disconnect` only
  when power comes *back*. So every successful `disable` was reported as a hub
  with no VBUS power switch, and then powered back on. The port's own
  `PORT_POWER` bit is the only thing observable while a port is off, so it is now
  the only thing checked; proof that power really drops belongs to `cycle`,
  which watches for the port to re-enumerate after power returns.

  The same reasoning applies to anything scripted around these commands: **do not
  test for a device's absence to decide whether a port is off.** `disable` now
  says so in its own output when the port had a device on it, and `cycle`
  reports whether the device came back, so neither needs checking by hand. To
  confirm a device really lost power, compare its USB device number either side
  of a cycle — a device cannot be renumbered without leaving the bus.

- **`lager.automation.PlugableUSBNet` is importable, and
  `lager.automation.YKUSHUSBNet` is the YKUSH driver again.** A copy-pasted
  branch in the lazy export table guarded the Plugable driver with the YKUSH
  name, so the YKUSH name returned the wrong class and the Plugable name raised
  `AttributeError`. Both names now resolve to their own driver.

- **`lager diagnose` no longer reports a non-Acroname USB hub as wedged.** Hub
  diagnostics are BrainStem-specific but every usb-role net reached them, so a
  hub from another vendor was opened with the wrong driver and the failure
  reported as an electrical fault. Such a hub now classifies as `NOT SUPPORTED`
  — a permanent property of the vendor, kept distinct from the transient `BUSY`
  state so nobody is told to "rerun when it is idle" for a condition that will
  never change — and the bus facts, which are accurate, are still reported.
  Addresses carrying a topology path instead of a serial also resolve to their
  sysfs device rather than reporting the hub as not enumerated.

## [0.38.0] - 2026-08-18

### Added

- **`lager update --pull` fetches a pre-built box image instead of building it
  on the box.** Release tags are published to `ghcr.io/lagerdata/lager-box` by
  the tag-publish workflow, so updating to `vX.Y.Z` can replace a multi-minute
  `docker build` with a pull. Opt-in for now (`--pull`, or
  `LAGER_BOX_IMAGE_PULL=1`); `--no-pull` disables it once it becomes the
  default.

  The image is pulled by resolved digest rather than by tag, for the box's own
  architecture, and is rejected unless it carries an
  `org.opencontainers.image.version` label matching the requested tag. The
  digest is recorded in `/etc/lager/image-source`, which `/etc/lager/build-hash`
  could not express: that hash is computed from the box's own tree and reads
  the same whether the image was built locally or pulled.

  Every failure -- branch target, unpublished tag, unreachable registry,
  wrong architecture, missing or mismatched label -- falls back to the local
  build that has always run. The pull happens *before* the containers stop, so
  a box keeps serving through the download and a miss costs nothing but the
  time it took to notice.

### Changed

- **Every hardware-interacting CLI command now takes the box lock, not just
  `lager python` and the admin commands.** Toggling a GPIO, driving a supply,
  flashing over SWD or opening a UART could previously collide with a running
  test or another operator with nothing to stop it, which was the most common
  source of unexplained failures on a shared bench. Measurement (`gpi`, `gpo`,
  `adc`, `dac`, `thermocouple`, `watt`, `energy`, `scope`, `logic`),
  communication (`spi`, `i2c`, `uart`, `wifi`, `ble`, `blufi`, `usb`,
  `router`), power (`supply`, `battery`, `eload`, `solar`) and development
  (`debug`, `arm`, `webcam`) all acquire an ephemeral TTL+heartbeat lock as
  they resolve the box.

  Read-only paths are deliberately untouched: `lager supply --box X` with no
  subcommand, `lager boxes list`, and the bare net listings still resolve
  without locking, so inspecting a bench never blocks anyone.

  This is the behavior reverted in v0.13.4, brought back on the infrastructure
  that made it safe. The three failures that forced that revert each have an
  answer now: locks are released by an `atexit` hook on any exit path and
  reaped by TTL on SIGKILL, so a supply command cannot strand one; only
  hardware-interacting subcommands acquire, so a long-running command no longer
  blocks status queries; and every ephemeral lock carries a 1800s TTL with a
  60s heartbeat, so a detached process that dies is reaped rather than leaving
  the bench held. `LAGER_AUTO_LOCK_DISABLE=1` remains the escape hatch.

### Fixed

- **A box whose Docker lacks the buildx plugin can now be updated.** The
  BuildKit preflight rejected such a box before doing anything else. That is
  still correct when the update has to build, but a pulled image needs no
  buildx, so the preflight now runs only on the path that actually builds.

- **`lager logic <NET> enable|disable|start|start-single|stop` did nothing, and
  reported success doing it.** The box-side worker resolved the net with
  `Net.get(netname, NetType.Analog)`, but the CLI validates the net as role
  `logic` before dispatching, and `NetType.from_role('logic')` is
  `NetType.Logic`. Both of `Net.get`'s lookup paths match on type equality, so
  the mismatch did not raise -- it returned `None`, the worker's
  `if target_net:` guard went false, and the command exited 0 having touched
  no instrument.

  Confirmed on a box with a Rigol MSO logic net configured: the net lists under
  `lager logic`, which is only possible when its role is `logic`, and the
  enable was a no-op. All six workers now resolve `NetType.Logic`.

  A unit test pins the worker's net type to `NetType.from_role(LOGIC_ROLE)`,
  importing the role from the CLI module that validates it, so the two sides
  cannot drift apart again without failing CI. Nothing covered this before:
  `cli/impl/*` scripts are uploaded and executed on the box rather than
  imported, so no unit suite exercised them.

- **A bench run against a stale box reported a result for code the box was not
  running.** `lager python` ships the script to the box and executes it there,
  so anything under `box/` runs from the box's checkout rather than the
  runner's. Only `update-regression.yml`'s lifecycle job updates the box, and
  only `nightly-bench.yml` chains lifecycle into integration — so a
  push-triggered `integration-tests.yml` run tested whatever the last nightly
  left behind, while producing output shaped exactly like a run that tested
  the commit. The run's own probe said so, in a step marked
  `continue-on-error` that nothing read: three days of triage once concluded a
  merged box-side fix had failed, from a run reporting the box 3 commits
  behind.

  That step is now a hard gate, and it compares against the ref being tested
  rather than the CLI's default of `main`. The exit code could not carry this
  — `--check` exits 1 for any pending change, including a deps rebuild or
  host-CLI drift — so it reads the `Code:` line, whose vocabulary is closed.
  Both the box's state and what triggered the run now go to the job summary,
  on every run rather than only on failure.

  Bench-testing a branch consequently needs `lager update --box <box>
  --version <branch>` first. That was always true; it now fails loudly instead
  of silently reporting results about `main`.

- **A weekly `Bench: Extended` failure filed itself under the nightly's alert
  issue, titled "Nightly bench is failing", and nothing could close it.**
  `tools/bench_alert.sh` dedupes by **label**, not by title, so passing a
  different title would not have separated the two — an Extended failure
  appended a comment to whichever issue the nightly was currently using, and a
  reader of a Saturday `17 14 * * 6` run was sent to the wrong workflow, the
  wrong schedule and the wrong triage order. Extended deliberately has no
  recovery job, so it could extend that issue but never resolve it, while a
  green nightly would close an issue describing a live Extended failure.

  Extended now writes its own `bench-alert-extended` label and its own title,
  which is what actually separates the streams. Its no-recovery design is
  unchanged and now safe to state plainly: the issue body says it does not
  self-close.

- **`.github/workflows/README.md` had no "triage order" section, though every
  bench alert body told the reader to go and read it.** The pointer had never
  resolved. That section now exists, and leads with the check that was
  missing entirely — whether the box contains the change under test at all.

  Added alongside it: a concurrency section documenting the shared
  `hardware-ci-<box>` group and why `nightly-bench.yml` deliberately declares
  none, and the fact that **dispatching displaces a run already queued**.
  GitHub holds at most one pending run per group, so with one executing and
  one waiting, a third arrival silently cancels the one in the middle;
  `cancel-in-progress: false` only ever protected the executing run. There is
  no setting for queue depth, so the remedy is to check the run list before
  dispatching — and each bench workflow now records its trigger, ref and
  commit in the job summary, so a displaced run can be identified afterwards.


## [0.37.2] - 2026-08-17

### Added

- **Release tags now publish a pre-built box image to GHCR.** A new
  `Release: Publish Box Image` workflow (`.github/workflows/box-image-publish.yml`)
  fires on every `v*` tag, builds `box/lager/docker/box.Dockerfile`, and pushes
  `ghcr.io/lagerdata/lager-box:vX.Y.Z` (plus a bare `:X.Y.Z` alias). It is also
  dispatchable against an existing tag, so images can be backfilled.

  **Nothing consumes these images yet.** `lager update` still builds on the box
  on every path, and this changes no box behavior at all. The publisher ships
  first on purpose: it is the half that has to exist before a pull can be
  tested against anything real, and it is inert with respect to the fleet until
  a client asks for it. Making the GHCR package public is a separate, manual,
  one-time step — until then even a deliberate pull would fail.

  amd64 only, matching the x64-only LabJack and nrfutil downloads already
  hardcoded in the Dockerfile. Retention is manual for now.

### Changed

- **Cold box image builds spend less time installing things nothing uses.**
  Three changes to `box.Dockerfile`, all of them about build time rather than
  behavior:

  Node and npm now come from the official upstream tarball, verified against
  its `SHASUMS256.txt` before extraction, instead of Debian's `nodejs npm`
  meta-packages — which pull in roughly 400 `node-*` packages the box never
  touches. `start_box.sh` needs npm only to install the packages named in
  `box_config.npm_packages`, which the tarball provides.

  `cryptography` moves from 38.0.4 to 43.0.3. The old pin has no cp312 wheel,
  so every cold pip layer compiled it from Rust source; 43.0.3 ships a
  manylinux wheel for the image's Python. The BluFi cipher, its only consumer
  in this tree, switches from `algorithms.AES128` to `algorithms.AES` — stable
  across both versions and byte-identical for BluFi's fixed 16-byte key.

  `flex` and `bison` move into the uldaq layer, the only stage whose
  `autoreconf` needs them. `ccache` and `ninja-build` are dropped outright:
  nothing in this repo invokes either, and no build here was wired to use
  them.

  **A box carrying globally-installed npm packages should be updated once with
  `--force`.** Node's major version moves from 18 to 20, and the
  `lager-npm-global` volume holding those packages survives an ordinary image
  rebuild — only `--force` wipes it. Any package with a compiled native module
  needs reinstalling under the new ABI.

- **The update progress bar names what the container build is currently
  doing** — `Building container... [pip install ...]` — instead of holding one
  unchanging label for the several minutes a cold build takes. Parsed from
  BuildKit's own step output; `--verbose` is unchanged.

- **A USB hub disconnect that fails is now recorded rather than discarded.**
  `AcronameUSBNet._close_hub` caught every exception from `hub.disconnect()`
  and dropped it, which made `HubSessionPool.drain`'s existing
  `logger.exception` for a failed close unreachable: the exception was gone
  before the drain could see it. The handler stays best-effort — a failed
  disconnect must not propagate out of teardown, where every caller is either
  finishing successfully or already unwinding — but it now logs what it
  caught.

  Also adds an opt-in exit trace behind `LAGER_HUB_EXIT_DEBUG`, for the
  intermittent abort a `lager python` script takes after an Acroname
  operation. When set it reports, per exit, which sessions were parked, which
  teardowns were already in flight, the outcome and duration of each close,
  and the total time in the hook. Unset by default, so nothing changes for
  anyone not chasing that bug.

  It writes to stderr rather than through `logging`, for reasons specific to
  where it runs: by `atexit` time logging may already be torn down and
  emitting can raise, and anything escaping the exit hook turns a passing
  script into a failing one — the exact outcome the hook exists to prevent.
  A `lager python` script also configures no handlers, so `logging.lastResort`
  would drop these lines for being below WARNING, which as timings they are.
  Every write is individually guarded.

### Fixed

- **`lager update --check` still promised a cached build when the ref it was
  about to check out changed the image recipe.** This is the second half of the
  `--check` estimate fix whose flatten-aware half shipped in 0.37.1, and the
  more common of the two cases. The probe measured the Dockerfile,
  requirements and box source in the box's *current* working tree — which on a
  box a long way behind its target still matched `/etc/lager/build-hash`
  exactly. The preview printed `Estimated: ~90s (cached build)`; the pull then
  landed a different Dockerfile and the update took the full six minutes.

  `--check` now reads those same build inputs at the target ref, straight out
  of the box's git object database via `git cat-file` / `git show` — no
  checkout, no mutation, and one extra SSH round-trip on the `--check` path
  only. The snippet is composed to emit a byte-identical digest to the
  working-tree hasher for an identical tree, so the two are comparable by
  construction rather than by coincidence; tests execute both under `sh`
  against a fixture repo and assert the digests agree.

  When the target ref can be measured it replaces the working tree as the
  basis for the whole preview, which also turns the old
  `unknown until pull (older ref may differ)` guess on rollbacks and branch
  switches into a measured answer. When it cannot be measured — a sparse
  checkout, an odd ref — the preview says so rather than falling back to the
  pre-pull tree and calling the cache valid. A pending flatten still forces a
  rebuild whatever the target digest says.

- **`lager install` and `lager uninstall` now offer the SSH key Lager itself
  installs.** `~/.ssh/lager_box` is generated by `lager ssh-setup` (and by
  `lager install`) and appended to the box's `authorized_keys`. It is not one
  of ssh's default identity filenames, so a bare `ssh` never tries it: install,
  uninstall, the connection pool, and every `ssh`/`scp` in
  `setup_and_deploy_box.sh` were instead relying on the operator's
  `~/.ssh/config` naming an identity for the box. Where that entry is missing —
  or names a key that has since been removed from the box — all of them fail
  with `Permission denied (publickey)` while `lager ssh`, which passes `-i`,
  keeps working on the same box from the same machine. Each of those paths now
  passes `-i ~/.ssh/lager_box`.

  Two things make this hard to see. SSH connection multiplexing means a
  connection that rides an existing master socket does not authenticate at
  all, so a bare `ssh` appears to work for as long as `ControlPersist` keeps
  the socket alive after any lager command; and a long command that holds the
  connection and then closes it makes the next `ssh` authenticate for real,
  which reads as "that command broke SSH" when the key had been absent for
  days. Reproduce the true state with `-o ControlPath=none`.

  The key is offered, not forced. `-i` *replaces* ssh's built-in default
  identity list rather than adding to it, so a box authorized only by a key
  the operator installed with `ssh-copy-id` would be locked out by an
  unconditional `-i`. A single probe settles which identity to use per command
  — the key first, ssh's own defaults if the box rejects it — and only an
  authentication rejection triggers the second attempt, so an unreachable box
  still fails once rather than twice. A box that has never had `lager
  ssh-setup` run against it is unaffected.

- **`lager ssh-setup` and `lager update` could not tell whether the key was
  installed, so they never reinstalled a deleted one.** Both decided by
  logging in with the key — but a login proves only that *some* identity
  worked, and `-i` does not restrict ssh to the key it names. Even with
  `IdentitiesOnly=yes` (which excludes the agent and ssh's default
  filenames), any `IdentityFile` declared in `ssh_config` is still offered,
  and a `Host *` block naming one — a common fleet-management layout —
  supplies a credential for every host. On such a machine the probe cannot
  return false, so a box whose `lager_box` line had been deleted reported
  healthy and neither command put it back. Confirmed on hardware: a box that
  passed the probe rejected the key outright under `ssh -F /dev/null`.

  Both now ask the box directly, grepping `authorized_keys` for the public
  key's base64 blob — the blob rather than the comment, because comments
  drift between the name a key was generated with and the name a key manager
  renders it under. Three outcomes, not two: installed, absent, or "could not
  ask", so an unreachable box no longer reads as an uninstalled key. The old
  `key_auth_works` helper is gone rather than left in place, because a
  function whose name says it tests one key and whose behavior accepts any
  is a trap for the next caller.

  `ssh-copy-id` is now invoked with `-f`. Its own "already installed?" filter
  is the same login test with the same blind spot — on the box above it
  reported "All keys were skipped because they already exist on the remote
  system" and installed nothing. Skipping that filter is safe because the
  check above it is exact, and re-running still installs nothing, because
  the command no longer reaches `ssh-copy-id` when the key is present.

  `setup_and_deploy_box.sh` had the same defect in its own SSH pre-flight,
  with the worst consequence of the three: it fell back to a bare
  `ssh <box> "echo test"` when the keyed attempt failed, and on a machine
  with a `Host *` IdentityFile that always succeeds — so it printed
  "Passwordless SSH already configured" and skipped generating, copying and
  registering the key entirely. Observed on hardware: an install that ran to
  completion, reported success, and left the box with no `lager_box` key at
  all. It now asks the box the same way the CLI does.

- **The `lager_box` key is now registered on the box, not just appended.**
  `lager ssh-setup`, `lager update`, and `lager install` all installed the key
  by appending it to `~/.ssh/authorized_keys` — outside every key manager's
  marker block. `start_box.sh` preserves such lines against its own rebuild,
  which reads as safe and is not: it cannot preserve them against a *different*
  key manager, and one that rebuilds `authorized_keys` from its own source and
  keeps only its own marked region takes the line with it. `start_box.sh` then
  re-creates its block from `/etc/lager/authorized_keys.d/` alone, so a key
  that was never registered there does not come back.

  On a box that also sets `PasswordAuthentication no`, that is unrecoverable
  from the CLI: `lager ssh-setup` reinstalls the key with `ssh-copy-id`, which
  needs a password the box will not accept. All three paths now also write the
  public half to `/etc/lager/authorized_keys.d/lager-box-<user>-<host>.pub`,
  which is the sync's own source of truth and survives any rebuild. One file
  per operator machine, rewritten in place, so re-running after regenerating
  the keypair replaces the entry instead of leaving the superseded key
  authorized. `lager uninstall --all` removes the registration before
  stripping the `authorized_keys` line, so `--keep-config` cannot republish a
  key that was just revoked.

  The write is attempted unprivileged first and falls back to `sudo -n`,
  which covers the two common cases: an unmanaged box's key directory is
  already writable by the box user, and a fleet whose box user has broad
  NOPASSWD is covered by the fallback. **Lager installs no new sudoers grant
  for this.** A key manager that hardens the box makes that directory
  root-owned on purpose — a writable key directory lets any user on the box
  authorize any key — and a fleet that also scopes sudo tightly should decide
  for itself who may write there. Where both attempts fail, `lager ssh-setup`
  prints the exact grant to add through that fleet's own provisioning, scoped
  to the `lager-box-*.pub` filename shape (a sudoers wildcard does not match
  `/`, so it cannot reach outside the directory), and says plainly not to
  widen the directory instead.

  Registration failure is a warning, not an error: the key is installed and
  working by then, and re-running `lager ssh-setup` retries it without a
  prompt. Boxes provisioned before this change are repaired by one
  `lager ssh-setup` or `lager update`.

- **`lager install` no longer offers password authentication when key auth
  fails.** A box with `PasswordAuthentication no` never receives the password
  it asks for, so `Password authentication failed` was a claim about something
  that never happened — and it sent operators looking for a password problem
  when the box had simply never authorized their key. The branch is replaced
  by an error naming the fix: `lager ssh-setup --box <ip>`, which installs the
  key with one password prompt, after which install runs unattended. That is
  one extra command on a brand-new box, in exchange for install no longer
  prompting for the same password at each of its later steps.

- **`lager install` sets the SSH key up itself instead of sending you to
  another command.** When no identity this machine holds is authorized, it
  now offers to install `lager_box` inline — one box-password prompt, after
  which the rest of the install runs unattended — rather than stopping with
  "run `lager ssh-setup` first". The password it would have asked for is the
  same one install needs anyway, so making it two commands bought nothing.
  Declining still exits with that error, and `lager ssh-setup` is unchanged:
  it remains the fix when any *other* command hits an unauthorized box, and
  the repair path for a key that a rebuild removed. The shared body now lives
  in one place, so the two cannot drift.

- **`lager uninstall` no longer claims you will need a password afterwards.**
  It removes `lager_box` and nothing else, so "the next SSH connection to this
  box will require a password" was a claim about every credential made from a
  fact about one — untrue for any operator with a key of their own, or on a
  box whose keys another manager renders. It now says which key was removed
  and that others are untouched.

- **`lager install` no longer writes a `Host` block into `~/.ssh/config`.**
  `setup_and_deploy_box.sh` used to add a per-IP entry naming
  `IdentityFile ~/.ssh/lager_box`. That file is commonly generated by
  something else (a config manager, an ssh-config tool), and the next rebuild
  deletes the block — taking with it the only thing telling ssh which identity
  to present, so boxes that worked yesterday start failing today for no
  visible reason. The block was also wider than it needed to be: it set
  `StrictHostKeyChecking no` and `UserKnownHostsFile=/dev/null`, permanently
  disabling host-key verification for that box, and `ProxyCommand none`,
  breaking access through a jump host. Passing `-i` per command has neither
  problem. Blocks written by earlier installs are left alone — Lager does not
  edit that file.

## [0.37.1] - 2026-08-17

### Changed

- **Lager's sudoers files now say on the box that Lager rewrites them.** Lager
  writes three files under `/etc/sudoers.d/` — `lagerdata-udev`,
  `lager-box-config`, and `lager-bench-json` — and regenerates each one in full
  on every run, so the current grant shape is always what lands. That is
  deliberate and unchanged. What was missing was any sign of it on the box: an
  operator or a box-management platform that added a `NOPASSWD` grant *inside*
  one of those files got it silently erased by the next `lager install`, with
  nothing to explain where it went.

  Each of the three now opens with a comment header naming the command that
  rewrites it and pointing operator grants at a separate file. Lager has never
  read, edited, or removed a file under `/etc/sudoers.d/` that it did not
  write — there is no glob, no directory-level operation, and uninstall's
  `rm -f` names those same three paths — so a grant kept in its own file (say
  `/etc/sudoers.d/zz-local`) survives every Lager run. The headers are sudoers
  comments; `visudo -c` validation is unchanged and still gates every write.
  `test/unit/box/test_sudoers_contract.py` pins both halves.

  **No existing box is touched, and no box prompts for a sudo password.** New
  installs get the header; a box that already has these files keeps its
  banner-less copies until it is reinstalled. Reaching provisioned boxes would
  mean bumping the marker that lets `lager update` skip the rewrite, and that
  rewrite needs an interactive sudo password — `sudo tee` into
  `/etc/sudoers.d/` matches no grant Lager installs — so it would charge every
  box a prompt to deliver five comment lines, and would fail on every run for
  boxes updated by a script with no terminal. The header is comments and both
  generations grant identical commands, so a box on either is correct. The
  marker stays at `.boxcfg-sudoers-v2`; bump it for a rule change, not a
  comment.

- **Documented that the box login user is root-equivalent by design.**
  Provisioning a box requires root, and three of the grants Lager must install
  to do it are each a full path to root: `apt-get` runs arbitrary commands as
  root through its own configuration, deploying udev rules is root by
  construction (udev executes `RUN+=` as root), and the firewall grant
  installs a script from world-writable `/tmp` to a root-owned path and then
  runs it.

  Nothing about that changes here — it is the same posture Lager has always
  had, now written down where the rules are defined, in the deployment script,
  and in the `lager install` reference. The grants are still spelled out as
  specific commands, which keeps the blast radius small and the file readable;
  what they are not is a privilege boundary, and two places in the tree
  previously said they were, claiming a compromised account "cannot escalate
  to root" via the path-scoped entries. That was true of those entries alone
  and false of the file that grants `apt-get` one line above them. Treat
  anyone holding the box login account's SSH key as holding root on that box.

### Fixed

- **A short `lager python` script that touched an Acroname USB hub net aborted
  at interpreter shutdown, after its own logic had already succeeded.** The
  script printed its results, called `sys.exit(0)`, and then died: czmq
  reported a dozen dangling ZeroMQ sockets and failed an assertion in
  `zsock_set_sndtimeo`, the box-side `python3` took SIGABRT, and the CLI
  reported exit 250.

  0.37.0 introduced the bounded hub session hold: after a one-shot operation
  the BrainStem handle stays open for a 2.5s idle window so a burst of
  commands pays one connect, and a timer closes it when the window ends. That
  timer is a daemon thread on purpose — a parked hub must never be what keeps
  a script's interpreter alive — so a script that finishes inside the window
  exits with the handle still open. The vendor SDK's sockets were then live
  when CPython finalised, which is the state czmq aborts on. Every other
  stateful driver on the box registers exit cleanup; the session pool was the
  one that did not.

  A pool now registers an `atexit` drain for itself the first time it parks
  anything, so the handle is closed and the hub's cross-process lock released
  while the interpreter is still whole. The drain closes inline rather than
  under the usual watchdog deadline: a worker thread cannot be created during
  interpreter shutdown at all on Python 3.12 and later, and a watchdog timeout
  fires the hang hook, which for a box service ends in `os._exit(70)` — a
  clean exit failed by its own cleanup. It never raises, so a hub that will
  not close cannot turn a passing script into a failing one, and it never
  touches a handle a wedged thread still owns.

  Closing what is parked is only half of it, because a disconnect is not
  instant — it measures around two seconds on real hardware. Once the idle
  timer has fired there is nothing left parked to close, while the disconnect
  it started is still running on a daemon thread; a script exiting in that gap
  had finalisation kill the teardown midway and produced exactly the same
  abort. On a 2.5s window that gap is about as wide as the window itself, so
  any test idling a few seconds after its last hub operation hit it. Exit now
  also waits for a teardown already in flight, bounded well above a healthy
  disconnect and far below the driver's own deadline — long enough never to
  abandon one that was about to succeed, short enough never to wait out a
  wedged hub.

  A script that parked a session consequently takes about a second longer to
  exit, which is the disconnect it was previously skipping. That is a cost
  only against the aborting build, not against the driver this replaced:
  measured end to end on an eight-step hardware suite, the fixed build
  completes in 77.9s where the pre-0.37.0 open-close-per-operation driver took
  92.6s, because reusing a session saves considerably more than closing one
  costs.

  Only the clean-shutdown path was ever affected. `os._exit`, SIGKILL and the
  box's `timeout` wrapper all skip finalisation, so czmq never runs and the
  kernel reclaims the descriptor and the lock.

- **Every successful `lager update` that rebuilt the container warned that its
  own lock was failing.** `Warning: update lock heartbeat has failed 5 times in
  a row (5m); the box may be unreachable.` — printed while nothing was wrong.
  Update stops the `lager` container in Step 8 and rebuilds it in Step 9, and
  that container is the process serving the `:9000` lock API the command's own
  lock lives in. Renewals across that window cannot succeed, and the failures
  accumulated into a warning about a box that was busy doing exactly what it
  had been asked to do.

  `lager install` already declares this outage via `LockSession.suspended()`.
  Update could not: its window spans some 350 lines of branchy logic, so it
  takes the lock through the imperative `auto_lock_acquire_for_command`, whose
  release callable offered no way to say so. That callable now carries
  `suspend()` / `resume()` (and a `suspended()` context manager), and update
  brackets the container rebuild with them — resuming only once
  `wait_for_box_ready` confirms `/health` on 9000 answers again.

  No lock semantics change. While the server is down a renewal cannot succeed
  whether or not it is attempted, so the lock was already riding its TTL
  either way; only the misreporting stops. A real heartbeat failure outside
  the rebuild window still warns.

  **That warning had a second, independent cause, also fixed here.** The
  heartbeat picks its threshold from the lock's TTL: with one, it warns once
  the unrenewed window reaches half the TTL and names the real risk ("has not
  renewed for 15m of its 30m TTL; the lock will expire if this continues");
  without one, it falls back to five consecutive failures and blames the box
  ("the box may be unreachable"). `auto_lock_around_command` passes the TTL.
  `auto_lock_acquire_for_command` did not — so every command using the
  imperative variant took the fallback branch, and a five-minute rebuild
  window tripped it even though the lock had twenty-five minutes of margin
  left. Suppressing exactly that case is what the TTL threshold was built for;
  the omission defeated it. Both variants now pass the TTL the lock is
  actually living under: ours when we took the lock, the existing lock's when
  we resumed one.

- **`lager update --check` promised a cached build immediately before a
  ten-minute rebuild.** On a box still using the `box/` subdir layout the
  preview printed `Deps: cache valid (no rebuild)` and
  `Estimated: ~90s (cached build)`; the run then flattened the tree and did a
  full clean rebuild. Two things were wrong, and they pulled in the same
  direction:

  The preview ignored a pending flatten entirely, though `_rebuild_gate_verdict`
  treats it as a definite rebuild trigger. It is not merely possible: the
  flatten moves every source file, and the build hash is taken over
  `sha256sum` output — which prints each path beside its digest — so a moved
  file necessarily changes it.

  Separately, `_build_hash_mismatch` returns `False` both for "measured, and
  unchanged" and for "could not measure", and the preview rendered both as
  `cache valid`. An unmeasurable hash is now reported as unknown. It still
  does not predict a rebuild, because the gate does not treat it as one
  either — saying it might would over-estimate as badly as the old text
  under-estimated.

  The decision moved into `_deps_preview()` so the property that matters is
  testable directly: the preview may never promise a cached build when the
  gate would rebuild. `--check`'s exit code now accounts for a pending
  flatten too, so a box needing one no longer reports `Nothing to do`.

- **The sudoers bootstrap snippet printed by `lager box-config mount` wrote a
  strict subset of the file it was overwriting.** It teed a single
  `NOPASSWD: /bin/mkdir, /bin/chown` line over `/etc/sudoers.d/lager-box-config`,
  so an operator who pasted it fixed mount auto-prep and silently dropped the
  `apt-get`, `sysctl`, `tee`, `rm`, and `cp` grants that `lager box-config
  apply` needs — and wrote no marker file, so the next update re-bootstrapped
  and prompted for the sudo password again. It now prints the same content
  `lager install` and `lager update` write, which already covers mkdir/chown.

- **A bump of the box-config sudoers marker could not have taken effect.**
  `lager install` and `lager update` hardcoded `/etc/lager/.boxcfg-sudoers-v2`
  in the probes that decide whether to re-bootstrap, rather than reading the
  constant the bootstrap command writes. Changing the constant would have
  moved the file Lager wrote without moving the file it looked for, so the
  rewrite would have been skipped on every box. All three call sites now use
  the constant, and a test fails on any new hardcoded copy.

## [0.37.0] - 2026-08-14

### Removed

- **The vendored `pyelftools` tree, whose ELF and DWARF parsing could not be
  imported in any environment.** `cli/elftools/` was copied in without its
  `construct/lib/` subpackage, so 34 of the tree's 45 modules raised
  `ModuleNotFoundError` everywhere -- pip installs and dev checkouts alike,
  since the missing directory was absent from the repo itself. That covers
  every module that parses anything: `elf/elffile.py`, `elf/structs.py`,
  `dwarf/dwarfinfo.py` and the whole `construct/` package. The eleven that
  still imported are constants tables and compatibility shims, which parse
  nothing on their own. Nothing detected any of it: the tree was excluded
  from ruff and from coverage, it had no tests, and `compileall` checks
  syntax without resolving imports.

  Its only consumer was `cli/commands/development/debug/gdb.py`, which was
  itself unreachable -- `gdb` appears in neither `list_commands` nor
  `get_command` of the debug group, so there has been no `lager debug gdb`
  command to invoke. Both are removed, along with the ruff and coverage
  exclusions that were hiding the tree from static analysis.

  This deletes 13,241 lines and changes no behavior that worked. The vendored
  copy was pyelftools 0.27 with no local modifications and no DWARF5 line
  program support, so restoring the missing subpackage would have revived a
  path that modern toolchains break anyway. Should ELF or DWARF parsing be
  needed again, depend on `pyelftools` from PyPI. `NOTICE` drops the
  `pyelftools` and `construct` attributions accordingly, leaving `PyCRC` in
  `cli/vendor/PyCRC/` as the only third-party code bundled with the CLI.
  Closes #240.

- **The `lager-mcp` console script, which exited immediately on every pip
  install.** It targeted `lager.mcp.server` -- box-side code under `box/`
  that the `lager-cli` wheel has never shipped -- so running it produced
  `ModuleNotFoundError: No module named 'lager'`. Installing the `mcp` extra
  did not help: that extra supplies the PyPI `mcp` SDK, not the `lager`
  package.

  This is a removal rather than a repair because the console script was never
  how the MCP server runs. The box starts it in-container as
  `python3 -m lager.mcp` and serves it on port 8100, which is what the MCP
  documentation describes and what clients connect to.

  `lager install` / `lager update` previously symlinked the script into
  `~/.local/bin`, so boxes deployed from an older ref carry a link to a script
  that cannot work. The install command now removes that link instead of
  creating it, so those boxes self-heal on the next deploy. The `mcp` extra is
  unchanged. The packaging gate now asserts the script is absent from the
  built wheel, so a console script that cannot resolve fails CI rather than
  reaching users. Closes #242.

### Fixed

- **Interactive USB hub commands went from ~150 ms to 7-9 s per command; a
  burst of commands now pays one hub connect, and a slow open path is no
  longer silent.** The cross-process contention fix (0.32.x) moved the
  Acroname driver to open -> operate -> disconnect per operation. The
  BrainStem open is native code costing whole seconds, and the discovery
  cache meant to make warm opens scan-free had three shapes where a "warm"
  open still ran a full USB discovery scan on every command, silently: a
  connect that succeeded via the `discoverAndConnect` fallback cached no
  spec, so every later open scanned again; an SDK without `connectFromSpec`
  scanned on every open while logging a healthy cache hit; and the scan's
  serial match compared the address's parsed int against the SDK's
  `serial_number` with a raw `==`, so a type mismatch failed every match.
  Successful opens logged nothing, so none of this was visible from a box
  log.

  Three changes, in `box/lager/automation/usb_hub/`:

  - *Timing instrumentation.* Every completed hub cycle (Acroname and YKUSH)
    now logs a per-phase breakdown -- lock wait, open (and which open path
    ran: session reuse, cached spec, cached-but-scanning, full discovery),
    the operation, close -- at DEBUG, escalating to INFO when the cycle
    exceeds 2 s. The open-path label is the evidence that separates a healthy
    fast path from one silently paying discovery.
  - *Scan-free warm opens.* The spec serial match is normalized on both
    sides, and regression tests pin that a warm open performs a single
    `connectFromSpec` with no discovery scan -- including from a fresh
    controller instance, which is what every HTTP request constructs.
  - *Bounded session reuse.* After a one-shot operation (enable / disable /
    toggle / state), the driver parks the open connection and the
    cross-process lock for a short idle window (2.5 s); operations on the
    same hub inside the window reuse the handle, and an idle timer
    disconnects and releases afterwards. Another process's worst-case wait
    is the window plus one operation -- never an indefinite pin, which was
    the original contention bug. The 1 Hz state-poll path may ride an
    existing session but never creates or extends one, so polling cannot
    keep a hub claimed. A held handle that went stale (hub re-enumerated
    inside the window) is dropped and the operation retried once on a fresh
    open, inside the same deadline. All fail-fast bounds are preserved: lock
    timeouts, the whole-cycle operation deadline, and the hang path -- a
    wedged call inside a held session still answers 504, releases the flock
    for other processes, poisons only this process's per-hub lock, and
    schedules the supervisor respawn. Because the driver now briefly holds a
    USB handle between operations, `AcronameUSBNet` declares
    `holds_usb_context_between_ops = True` again, keeping the sysfs-gated
    self-restart reachable for an orphaned held handle.

  BrainStem behavior itself is not testable in CI; the timing log is what
  verifies the win on a real bench.

- **`lager terminal` told users to install one of its own dependencies by
  hand.** `prompt_toolkit` is imported at module scope by
  `cli/terminal/ui/repl.py`, `completer.py` and `themes.py`, but it was never
  in `install_requires`. `_launch_terminal()` catches the resulting
  `ImportError` and prints `Install with: pip install prompt_toolkit rich`
  before falling back to help output, so on a clean install the command
  degraded into a manual instruction rather than a traceback -- which is also
  why it survived this long. It is now declared as
  `prompt_toolkit >= 3.0, < 4`.

  The packaging gate added alongside it installs the built wheel into a clean
  venv and imports every `cli` module against
  `tools/packaging_import_baseline.txt`, so an undeclared import fails CI
  instead of reaching users.

- **Two `cli/impl` modules could not be imported from a pip-installed CLI.**
  `cli/impl/box_config.py` and `cli/impl/power/enable_disable.py` imported
  `lager.*` at module scope. That package lives under `box/` and is not in the
  `lager-cli` wheel, so both raised `ModuleNotFoundError` on any clean install
  -- they worked only where `box/` happened to be on `sys.path`, which is true
  in dev checkouts and on boxes and false everywhere else.

  These files are uploaded to the box and executed there, so the box-side
  behavior was never affected. But they also ship inside the wheel, because
  `get_impl_path()` resolves them from the installed package on disk, which
  makes them importable modules on the host. Both now defer the `lager` import
  into the function that needs it, matching what
  `cli/impl/measurement/scope.py` already did. `box_config.py`'s
  `/app/lager` path insert moved inside `__main__` for the same reason.

  A new unit test imports every `cli/impl` module in a subprocess with `box/`
  stripped from the path and the name `lager` blocked outright, so this cannot
  regress quietly between packaging runs. The two entries naming this defect
  are deleted from `tools/packaging_import_baseline.txt`, and because that
  baseline is two-sided, the gate now fails if either module regresses *or* if
  the entries were left behind. Closes #241.

- **The BluFi key exchange no longer depends on a key-agreement primitive that
  newer `cryptography` releases reject outright.** cryptography 50.0 deprecates
  finite-field Diffie-Hellman wholesale, and the BluFi handshake was built on
  `hazmat.primitives.asymmetric.dh` end to end. This was worse than a
  deprecation clock: on 50.0 the group BluFi uses is refused at parameter
  construction with `ValueError: Invalid DH parameters`, so key generation
  fails and provisioning cannot complete at all. The failure was invisible
  because the four unit tests covering the exchange caught that same
  `ValueError` and skipped — the suite reported green on exactly the
  configuration where the feature was broken.

  The box is the initiator and sends P, G and its own public key to the
  device, so it owns the group outright and the only compatibility constraint
  is byte-level. The exchange is now direct modular exponentiation over the
  same hardcoded parameters, which removes the dependency in both directions:
  it works on the runtime's pinned cryptography 38.0.4 and on 50.0 alike.

  Two byte-level invariants that the library used to supply implicitly are now
  explicit and pinned by known-answer tests captured from the previous
  implementation: the public key still goes out padded to 256 bytes, and the
  shared secret is still zero-padded to 128 bytes before it is hashed. The
  latter matters — about one exchange in 256 produces a secret with a leading
  zero byte, and hashing the unpadded form yields a different AES key from the
  device's, which would present as an occasional unreproducible provisioning
  failure rather than a clean break.

  The four skipped tests now run everywhere, and peer public keys are
  validated (0, 1 and p-1 are rejected) where the library used to do it. Note
  that CPython's `pow()` is not constant-time where the library's `exchange()`
  was; the parameters are Espressif's published BluFi constants and not a safe
  prime, so the handshake does not withstand an active attacker either way,
  but the timing characteristic is a real change. Completes #219.

- **A second BluFi security negotiation on one client no longer derives a key
  from two concatenated peer public keys.** The receive buffer for the peer's
  key was only ever appended to, and was not cleared with the rest of the
  security state. Not reachable today, since each request builds a fresh
  client, but it failed silently rather than loudly.

## [0.36.2] - 2026-08-12

### Fixed

- **`lager install` could take Docker down on a fresh box and then blame the
  config.** `docker.service` ships `StartLimitBurst=3` / `StartLimitInterval=60s`,
  and the installer started it four times in eleven seconds: the package
  postinst's own start, a `docker.socket` restart (which bounces the service
  too, since `docker.service` declares `Requires=docker.socket`), the service
  restart immediately after it, and finally the restart that applies
  `daemon.json`. systemd refused the fourth and latched the unit into
  `failed (start-limit-hit)`, where every further restart — including the
  installer's own retry — fails instantly without attempting a start. Docker
  itself was healthy throughout; every start systemd allowed to run reached
  full initialization.

  The installer now performs one service start per step: the `docker.socket`
  restart is kept only as a fallback for the stale-socket failure it was
  actually added for, so it costs a start only when the plain restart has
  already failed. Every restart is preceded by
  `systemctl reset-failed docker.service docker.socket`, which clears the
  counter — so a box that is already latched now recovers instead of staying
  wedged, including on the DNS rollback path, where a refused restart used to
  leave the old `daemon.json` restored and the daemon still dead.

- **A wedged Docker daemon was reported as a malformed `/etc/docker/daemon.json`.**
  That hint fired because writing `daemon.json` is the step before the restart
  that fails, not because the two are related — and it was wrong in the case
  actually observed, sending debugging down the wrong path. The installer now
  reads `systemctl show docker -p Result` first and, on `start-limit-hit`,
  names that cause and gives the remedy that works (`reset-failed`, then
  `start`). The `daemon.json` hint remains for every other failure.

- **`lager uninstall` warned about a lock heartbeat failure on every
  successful run.** The command holds the box auto-lock across all five of its
  steps so a concurrent `lager python` cannot be torn down mid-test — but Step
  1 removes the lager container, which is the process serving the `:9000` lock
  API the heartbeat renews against. Every heartbeat and the final release after
  that point were POSTs to a server the command itself had just deleted, so
  `Warning: uninstall lock heartbeat failed; relying on server TTL.` was
  guaranteed, and it read as a fault when it was a consequence of the uninstall
  working.

  The lock session now *dissolves* once the container is confirmed gone: the
  heartbeat stops and the release is skipped, because the lock state died with
  the container — there is nobody left to tell and nothing left to persist. If
  the container removal fails, nothing is dissolved: the server may still be
  up, and a heartbeat failure is real signal again. No other command's lock
  behavior changes.

- **`lager install` warned that it could not reach the box on installs that had
  just succeeded.** The connectivity check fired a single `lager hello` seconds
  after the container was started, racing the five services still coming up
  inside it. It failed on healthy boxes often enough that the warning was
  routinely ignored — which is the state in which it stops being able to report
  a real failure. It now retries for 30s, and when it does give up it says so
  in those terms and prints the `docker logs` command to run next.

- **The install summary told users to run `lager nets create`, which is not a
  command.** The nets commands are `add` / `add-all` / `add-batch`; the printed
  example also named the second argument `<net-type>` when what `add` takes is
  a role. Now `lager nets add <net-name> <role> <channel> <address>`.

- **Provisioning could stop and wait for a keypress nobody was there to
  press.** Every `apt-get` the installer runs on the box now sets
  `NEEDRESTART_SUSPEND=1` beside `DEBIAN_FRONTEND=noninteractive`. needrestart
  is an apt post-invoke hook that `DEBIAN_FRONTEND` does not reach, so on a box
  with a pending kernel upgrade it raised a full-screen dialog mid-install. The
  suspend is deliberate rather than `NEEDRESTART_MODE=a`: auto mode answers the
  prompt by restarting services itself, including docker, which would spend a
  start against the `StartLimitBurst` budget the fix above exists to protect.

- **The lock heartbeat warned on the first missed renewal, which made it
  meaningless.** Renewals are attempted every 60s against a 1800s TTL, so one
  failure has spent a thirtieth of the budget — but it printed
  `Warning: … heartbeat failed; relying on server TTL.` immediately. `lager
  install` replaces the container serving the `:9000` lock API, so *minutes*
  of failed renewals are expected mid-install and the lock outlives them
  comfortably; the line fired on installs that were fine. The warning now
  waits until the unrenewed window reaches half the TTL and reports how long
  it has actually been (`has not renewed for 15m of its 30m TTL`), which is
  the number that tells you whether to act. A successful renewal resets the
  window, so a box that keeps dropping out is reported each time it gets
  close rather than once per process. Eternal locks (`--detach`) have no
  deadline to measure against and fall back to a consecutive-failure count.

- **`lager install` warned that its lock was about to expire, every time.**
  Raising the warning threshold (above) was not enough on its own: install
  removes the lager container — the process serving the `:9000` lock API —
  and then rebuilds the image, which on a cold cache runs past fifteen
  minutes. No renewal can succeed for that entire window, so on a 1800s TTL
  the warning was still correct to fire near the end of a completely healthy
  install, and would fire earlier on any slower box.

  The threshold was the wrong instrument for an outage the installer causes
  and knows about, so install now declares it. The lock session grows a
  `suspended()` block, which stops attempting renewals and resets the failure
  window on the way out; install wraps the deploy script in it. Covering the
  window is handled where it belongs — install takes its lock with a TTL
  (1 hour) longer than the deploy script's own 30-minute timeout, so the
  lock survives on time rather than on renewals it cannot make. The cost is
  that an install killed outright — SIGKILL, power loss, anything that beats
  both the `finally` and the `atexit` release — leaves the box locked for up
  to an hour instead of half of one; `lager boxes unlock` clears it.

- **`lager uninstall --keep-config` left a lock nothing could ever clear.**
  Dissolving the session is right — the lock server is being deleted, so
  there is nobody to release to — but it leaves `/etc/lager/lock.json` saying
  `locked: true`, and `--keep-config` is the one path where that file
  survives the uninstall. A lock whose `ttl_seconds` is null is never reaped
  (the box's expiry check returns false outright on a null TTL), so
  reinstalling the box brought it up held by a holder that no longer existed,
  permanently. The lock state and its flock sidecar are now cleared as part
  of the privileged step; saved nets, which are the point of the flag, are
  untouched.

- **`lager uninstall --dry-run` could report a box as empty because it never
  managed to ask.** The query helper captured both streams with a 30s timeout
  and no allowance for a password prompt, so on a box without key auth every
  query stalled, hit the timeout, landed in a blanket `except Exception` and
  returned `None` — which the inventory renders identically to "the box does
  not have this". Queries on the password path now allow 120s, permit a
  single prompt, and leave stderr on the terminal; a query that does time out
  says so instead of being counted as an absence.

- **`lager uninstall` reported removing a box from `.lager` that was still
  there.** Writes deliberately touch only the global `~/.lager` — project
  boxes must not leak into global storage — but every read merges that file
  with each project `.lager` found walking up from the cwd. A box defined in
  both was deleted and still resolved, under a green "Removed" line, which
  sent debugging toward the box rather than the config. The command now says
  which file it wrote and names any project file that still defines the name.
  Relatedly, a box defined *only* in a project file no longer reports as "not
  found in .lager config", which was the wrong sentence for "found, but not
  somewhere this command may write".

### Changed

- **The installer no longer installs pyOCD, and no longer offers it as a
  fallback it cannot deliver.** The step pip-installed pyOCD onto the box
  *host*, outside the container where debugging actually runs, and nothing in
  the box code imports or invokes it — the debug subsystem drives J-Link and
  OpenOCD, and OpenOCD is already in the image. So when a J-Link install
  failed, every message that said "debug commands will use pyOCD (already
  installed)" — in the installer, in `lager update`, in `start_box.sh` and in
  the deployment docs — was describing a fallback that did not exist. They now
  name OpenOCD and say plainly what is lost: SEGGER probes, not debugging. Removing the step also drops a step from the install (9 → 8) and
  one that reported failure by grepping pip's stdout, which is how it managed
  to print "encountered issues" for installs that never ran.

- **The box image now pins the third-party MCC uldaq library it builds from
  source.** It was cloned unpinned from the default branch, so two boxes built
  a week apart could get different driver code with no change in this repo. It
  is pinned to `v1.2.1` — upstream's latest release, and what the unpinned
  clone was already producing — so this fixes current behavior in place rather
  than moving it.

  That build also emits about thirty copies of one `-Wstringop-overflow`
  warning, from a loop in upstream's `Usb9837x.cpp` that writes 16-byte
  registers past the end of a 64-byte stack struct. It is a real overrun, and
  it is **left unsuppressed on purpose**: lager's own drivers cannot reach it
  (only the USB-202 is driven), but the `uldaq` Python package is present on
  every box, so a user script with a DT9837 attached could. Silencing the
  warning would hide a memory-safety defect rather than close it. The
  reasoning is recorded in the Dockerfile so the noise is not mistaken for
  something nobody looked at.

## [0.36.1] - 2026-08-12

### Fixed

- **Two identical USB-UART adapters no longer collapse onto one tty.** The
  box's USB scanner handed every device of the same model a reference to one
  shared channel table, then wrote each device's tty list into it in place --
  so with two same-model adapters plugged in, both scan entries advertised
  whichever device happened to be enumerated last. Adding a net for the
  second adapter therefore recorded the first adapter's tty and USB identity
  under the second adapter's address, and the two nets silently drove the
  same physical port. The clobbered lists lived in the scanner's module
  state, so the corruption outlived the request and poisoned every later
  scan in the same server process.

  Scan entries now carry their own per-role channel lists, and the merge
  helper re-owns a record's lists before appending, so the catalog can never
  be edited through a scan result. The CLI's net-add flows (the TUI and
  `nets add` / `nets add-all`) additionally prefer the scanner's per-device
  `tty_paths` field -- which was always computed from the device's own
  serial -- over the shared channel map, so an updated CLI offers the right
  tty even against a box that predates this fix. A net saved with a
  cross-wired identity does not self-correct: re-add it after updating.
  (#213)

- **One slow USB hub no longer reports the rest of the bench as timed out.**
  `/nets/state` gives the whole request one budget, but every USB hub on the
  box was probed serially inside a single work unit -- so a hub that burned
  its driver timeout consumed the entire budget, and every hub after it came
  back `reason: "deadline"` while being perfectly healthy: a false diagnosis
  manufactured by the budget rather than observed.

  The request deadline is now handed to the USB batch probe, which
  sub-budgets it per hub: each hub's whole probe cycle -- the lock wait
  included -- is clamped to the time actually remaining, and a hub the
  budget cannot cover is skipped outright rather than probed into the
  deadline. Skipped nets say so, with their own reason ("not probed: slower
  instruments consumed the state budget") and a `hub-skipped` code the CLI
  footnotes with a remedy, so "the budget ran out before this hub's turn"
  and "this hub is slow" stop being the same message. The whole-request
  deadline and every one-shot USB command path are unchanged. (#205)

- **Every box now installs the same Acroname BrainStem SDK.** The box image
  installed `brainstem` unpinned in a build-cached layer, so which SDK a box
  ran depended on when that layer was last invalidated -- two boxes built
  weeks apart could differ, which made cross-box comparison unreliable when
  diagnosing hub behavior (the hub driver already degrades differently for
  SDKs without `discover.findAllModules`). The SDK is now pinned to 2.12.5,
  the version validated against real hubs, with the bump-deliberately
  rationale recorded next to the pin. `/diagnose/usbhub` additionally
  reports the installed version as `sdk_version`, so version skew between
  boxes is visible from the CLI rather than by shelling into containers.
  (#206)

### Changed

- **The BluFi AES import follows CFB into cryptography's decrepit home.**
  cryptography 50.0 deprecates importing CFB from
  `hazmat.primitives.ciphers.modes`: upstream moved it to
  `hazmat.decrepit.ciphers.modes` and calls the old path's removal imminent.
  The import now prefers the new location and falls back to the old one on
  installs that predate it (the box runtime pins cryptography 38.0.4). Same
  mode class either way, so ciphertext is byte-for-byte unchanged. The BluFi
  key exchange's FFDH deprecation -- the other half of #219 -- remains open
  and tracked there.

## [0.36.0] - 2026-08-12

### Fixed

- **A `lager python` script whose client vanishes now gets to finish its
  teardown.** When the CLI is hard-killed, the network drops, or a CI job is
  cancelled, the box previously SIGTERMed the script outright — no `finally`,
  no context managers, no `atexit` — and only noticed the dead client the next
  time the script wrote output, which for a quiet script could take many
  seconds. Disconnects are now detected sub-second even for silent scripts, the
  script is interrupted with SIGINT first, and a progress-aware watchdog gives
  cleanup work a grace window (extended while it is demonstrably making
  progress, hard-capped at one minute) before escalating to SIGTERM and
  SIGKILL. New jobs wait for a previous job's teardown to clear before starting.

- **`lager python` now survives more than Ctrl+C: SIGTERM and SIGHUP stop the
  job too.** A killed terminal or a supervisor's TERM (including a cancelled CI
  job) previously ended the client at its default disposition — the box-side
  script kept running and the box lock stayed held until someone noticed. All
  three stop signals now trigger the same remote stop and lock release, the
  handlers are restored afterwards so a second signal can always break out, and
  the reattach path installs them safely from worker threads as well.

- **Killing a `lager python` job now stops every process the job started, not
  whichever one a scan happened to find first.** All of a job's processes are
  collected before any is signalled, delivery is reduced to one signal per
  process group (so a graceful interrupt is not cut short by its own
  duplicate), and one grace window is shared by the whole job before anything
  is force-killed — a multi-process job no longer holds the kill request open
  while each process is resolved in turn. The kill request sent when the CLI
  is interrupted is also bounded by a network timeout, so an unreachable box
  surfaces as an error instead of a hang inside a signal handler.

## [0.35.0] - 2026-08-06

### Added

- **A net can now carry voltage and current ceilings that the box enforces, so
  a script bug cannot drive an instrument past what the hardware on the bench
  can survive.** A setpoint above a net's ceiling is refused before it reaches
  the instrument. Limits live on the saved-net record, so they follow the net
  rather than whichever instrument happens to be driving it. This is opt-in: a
  net with no limits configured is unrestricted, so existing benches behave
  exactly as they did.

  Enforcement is deliberately two-tier, and the difference matters when you
  decide what to rely on. The hard tier runs inside the box's hardware service —
  a separate process, and the only route to the instrument for the nets it
  covers — so a test script cannot talk around it. A second, advisory tier in
  the power dispatchers catches honest mistakes earlier but is defeatable by a
  script that imports a driver directly; it is a convenience, not a guarantee.

  Ceilings are always re-read from the box's saved nets by name and never taken
  from the request, so a caller on a shared box cannot raise its own limit. The
  inline `ovp=` and `ocp=` trip settings are checked too, since those would
  otherwise lift the instrument's own guard above the net ceiling. A net can
  refuse erase and flash outright with `allow_destructive: false`, and the box
  caps the call rate per net and method so a runaway loop stays bounded.

  Set them with `PUT /nets/<name>/safety-limits` on the box, which merges
  `max_voltage`, `max_current` and `allow_destructive` into the saved net and
  leaves every other field untouched. `max_power` is refused rather than stored:
  a single setter call establishes either voltage or current and never both, so
  a power ceiling could not be evaluated honestly, and a stored limit that
  nothing enforces is indistinguishable from an enforced one from the outside.
  Boxes carrying the route advertise it in the capabilities on `/status`, so a
  control plane can tell where a configured ceiling is genuinely enforced
  instead of inferring it from a version number.

- **RTT is now bi-directional, so firmware that reads commands over RTT can be
  driven from the CLI.** The probe's RTT connection was always full duplex, and
  the box's own Python API has exposed `write()` on it for as long as
  `dbg.rtt()` has existed — but the only remote transport was a one-way HTTP
  stream, so a remote caller could read the target's log output and had no way
  to answer it. Interactive firmware consoles were therefore reachable from a
  script running on the box and from nowhere else.

  `lager debug <net> gdbserver --rtt --interactive` now forwards stdin to the
  target's RTT down-channel while the up-channel streams as before. The
  up-channel stays raw bytes on stdout, so the established defmt pipeline is
  unchanged and composes with the new flag:

      lager debug <NET> gdbserver --rtt --interactive 2>/dev/null \
        | defmt-print -e app.elf

  What you type is echoed by your terminal rather than injected into stdout,
  so the byte stream reaching `defmt-print` is exactly what it was before.
  `--rtt-channel` selects a channel other than 0 for both directions.

  This requires the firmware to declare an RTT **down** buffer on that channel.
  `defmt-rtt` alone only sets up the up buffer; with no down buffer the target
  silently discards what it is sent, which looks like a host-side failure and
  is not one.

  Plain `--rtt` is untouched and still uses the HTTP stream, so nothing that
  reads RTT today changes behavior.

- **`rtt_defmt()` can now write, so a `lager python` script can drive
  interactive firmware and assert on its decoded reply.** Raw `dbg.rtt()` has
  always been bi-directional, but the decoding wrapper only exposed
  `read_line()` — and reaching for a second raw session alongside it does not
  work, because the RTT telnet port accepts a single client. Decoding a
  target's logs therefore meant giving up the ability to talk to it.

      with dbg.rtt_defmt(elf='build/app.elf') as logs:
          logs.write(b'self_test\n')
          line = logs.read_line(timeout=5.0)

  Decoding is one-way — `defmt-print` only ever sees the up-channel — so a
  write bypasses it and goes straight to the target's down-channel. As with the
  CLI flag above, the firmware must declare an RTT down buffer on that channel.

## [0.34.4] - 2026-08-05

### Fixed

- **The box no longer restarts its HTTP server for a USB hub it cannot
  reach.** A self-restart repairs exactly one thing: a USB handle the process
  orphaned across a re-enumeration, which only a fresh process can reopen. The
  Acroname driver keeps no such handle — it opens, operates and disconnects on
  every call — so there was never anything on that path for a restart to
  repair. It fired anyway, because the gate asks only whether the device is
  still in sysfs, and the kernel keeps a sysfs node for a device that is
  enumerated but not answering on the wire.

  Observed on a two-hub bench: a hub that would not open triggered the restart
  twice, and each respawned process failed identically 37s and 43s later.
  Nothing was fixed, and every other in-flight operation the service was
  holding — UART sessions, running scripts, hardware calls — was dropped to do
  it. Only the 60s cooldown kept it from repeating for as long as the hub
  stayed down. Drivers now declare whether they hold a USB context between
  operations; those that do not are skipped. The pyvisa and HID paths, where a
  session really does persist, are unchanged.

- **`lager diagnose` reported the wrong instrument on a bench with two of the
  same model.** The sysfs lookup matched on vendor and product id and returned
  the first hit. It now prefers an exact serial match, falling back to
  vid/pid so a device with an unreadable iSerial is not lost.

- **`lager diagnose`'s dmesg section has never worked.** It shelled out to
  `sudo -n dmesg` and the box image ships no `sudo`, so the field has read
  `(dmesg unavailable: ...)` on every box since it shipped. It now reads dmesg
  directly and, when the container cannot (the services run unprivileged and
  `dmesg_restrict` is set), says so and points at where the history actually
  lives.

### Changed

- **A USB hub that will not open now says what to do about it.** The bus
  cross-check already worked out which of three faults it was looking at —
  nothing from this vendor on the bus, our serial present but not answering,
  or other devices present and none of them ours — then flattened all three
  into one string with a breakdown of vendor return codes appended. The
  terminal showed a wall of `USBHub2x4/spec rc=7 (x3)` and no sentence saying
  whether to check a cable, a power switch, or the net's address.

  Null entries in `lager nets state` now carry a `reason_code` alongside
  `reason`, and the footnote adds one remedy line per affected group — red
  when the fault is hardware, yellow when the bench is more likely in a normal
  state. `--json` carries `live_state_reason_code`. The box always sends a
  complete, self-sufficient human `reason`, so an older CLI, or a newer one
  seeing a code it does not recognise, renders exactly as before.

  A hub the kernel has enumerated but that will not answer now logs at ERROR
  rather than WARNING. That case is always hardware and always worth acting
  on; a hub that is simply absent is a normal bench state.

- **An Acroname hub open is retried once when the bus says the hub is there.**
  A hub caught mid-re-enumeration is on the bus a beat before discovery will
  return it, so an operation landing in that window failed outright. The YKUSH
  driver has always retried once for this reason; this one did not. Gated on
  the bus cross-check, so a hub that is genuinely absent still costs exactly
  one attempt, and suppressed on the polling path so the whole-bench state
  sweep's timing is unchanged.

### Added

- **`lager diagnose` now covers USB hub nets.** They used to fall through to
  "NOT USB-TMC: check the role command yourself", which is no help when the
  question is why the hub will not answer. A new `/diagnose/usbhub` endpoint
  reports what sysfs and lsof structurally cannot: the vendor SDK's own view.
  A hub can be enumerated, held by nobody, and still invisible to BrainStem
  discovery — and in that state every host-side signal looks healthy.

  It also reports the device's `devnum` against the rest of the bench. The
  kernel assigns those monotonically per bus, so a device far above its
  neighbours has re-enumerated many times since they did. On the bench that
  prompted this, the failing hub sat at 93 while every other instrument was in
  the 60s; that one number was the most useful fact in the investigation and
  nothing surfaced it.

## [0.34.3] - 2026-08-04

### Fixed

- **A wedged USB hub no longer takes every later USB command down with it.** A
  hub operation that hung — native BrainStem or pykush code blocking forever
  against a hub whose USB link is wedged, typically after a re-enumeration —
  held the box's USB lock for the life of the process. Every subsequent
  `lager usb` command and every 1 Hz state poll queued behind it with no
  timeout of its own, and the box's self-restart recovery could not help
  because it only fired when an operation *raised*. A call that never returns
  raises nothing.

  Three bounds close that. `POST /usb/command` now waits at most 10s for the
  in-process hub lock and answers `503 hub-busy` rather than queueing; the
  per-hub lock inside the drivers honours the same timeout the cross-process
  lock already did, and reports a hub it cannot claim as unavailable instead
  of waiting forever; and each hub operation — the whole open → operate →
  close cycle, since the open is what hangs — runs under a 30s deadline. On
  expiry the caller gets `504 hub-op-timeout` and the service schedules the
  same supervised self-restart it already used for unreachable devices, which
  is the only thing that clears an orphaned USB context. The hung thread
  cannot be killed from Python; the supervisor respawn is the recovery.

- **The same treatment for `hardware_service`'s `/invoke`.** Per-device and
  per-VISA-address locks are acquired with an 8s timeout and answer
  `503 device-busy` instead of queueing forever behind a wedged
  `open_resource` (which blocks inside libusb before the 5s VISA I/O timeout
  is even applied) or a hung native driver call. The driver call itself runs
  under a 30s deadline; expiry answers `504 invoke-timeout` and schedules the
  self-restart. `POST /labjack/batch_read`, which takes the same lock, is
  bounded too — a busy LabJack reports its nets as unreadable rather than
  hanging a `/nets/state` sweep. A device whose operation hung deliberately
  keeps its lock: the abandoned thread still owns the session and the USB
  claim, so later requests get a fast "busy" until the service is respawned.
  Success responses and the existing stale-VISA-session retry are unchanged.

  One behavior change to be aware of: a request that arrives while the same
  physical device is more than 8s into another operation now reports
  `device-busy` instead of queueing. Callers already gave up at their own HTTP
  timeout in that situation (`Device.DEFAULT_TIMEOUT` is 10s); what changes is
  that they get a real error rather than a transport timeout.

- **`lager usb` waits long enough to hear the box's answer.** Its HTTP timeout
  was 30s, but the box's own bounds for `/usb/command` are additive — up to 10s
  queueing on the hub lock, then up to 30s in the operation deadline — so a
  wedged hub answered at ~40s and the CLI had already given up. That surfaced
  as "cannot reach box", which reads as a network fault and hides both the real
  diagnosis and the fact that the box had already scheduled its own recovery.
  Now 45s, above the box's worst case.

- **Secret files are now owned by the container user, not just locked down.**
  `/etc/lager/org_secrets.json` and `/etc/lager/secret_key` were tightened to
  mode 0600 at boot and on load. But 0600 grants the *owner* alone, and
  everything that reads these files runs as the container user — so a secrets
  file that had been copied onto a box by hand, and therefore belonged to the
  host login user, was locked away from the runtime by the very step meant to
  protect it. The box's boot script runs as that same user, so its `chmod`
  succeeded and the lockout was instant.

  Nothing failed loudly: the loader caught the permission error and returned an
  empty secret set, so every `LAGER_SECRET_*` variable simply vanished and
  scripts failed far from the cause.

  `lager update` now repairs ownership of both files automatically — it is the
  path that runs with real privilege, so ordinary users are fixed on their next
  update with nothing to run by hand. The box also repairs what it can at boot
  and prints an unmissable warning, with the exact `chown`/`chmod` to run, when
  it finds a secrets file the runtime cannot read. The loader reports a
  permission failure at error level with the same remediation instead of a
  low-visibility warning.

  The boot-time diagnostics used to be inverted: the old script warned when its
  `chmod` *failed*, which is the healthy case (the file already belongs to the
  container user, which is precisely why the host user cannot change it), and
  said nothing at all when the `chmod` succeeded — the case that creates the
  lockout.

## [0.34.2] - 2026-08-02

### Removed

- **The HTTP SSH key-authorization endpoint (`POST /authorize-key` on port
  9000) is gone**, along with its handler, rate limiter, and the
  `/tmp/lager-authorized-keys.d` staging directory it wrote to. The endpoint
  let any caller with the bearer token create an arbitrarily-labelled `.pub`
  file, and the keys it created could never be removed — the old sync only
  appended. It was also the first link in a privilege-escalation chain: a
  container-side file write became host SSH access, and from there host root
  via the privileged runtime container.

  **If you provision boxes through this endpoint, switch to writing
  `<name>.pub` into `/etc/lager/authorized_keys.d/` directly.** That directory
  is bind-mounted into the runtime container, so a control plane can still
  write it from inside the container before it has SSH access — the bootstrap
  path is unchanged, and keys still appear in `~/.ssh/authorized_keys` within
  about five seconds. No CLI or MCP command called this endpoint, so
  command-line workflows are unaffected. The `authorize_token` field in
  `/etc/lager/control_plane.json` now has no reader; the file is still used for
  auth-gateway enrollment and should not be deleted.

### Changed

- **`~/.ssh/authorized_keys` is now rebuilt from the key directory, not
  appended to.** `start_box.sh` owns only the region between its
  `# BEGIN LAGER MANAGED KEYS` / `# END LAGER MANAGED KEYS` markers and
  regenerates that region from `/etc/lager/authorized_keys.d` on every pass,
  writing a temp file and renaming so sshd never sees a partial file. Deleting
  a `.pub` now revokes the key, which was previously impossible, and the old
  check-then-append race can no longer duplicate lines — boxes have been found
  with five `authorized_keys` entries built from three key files.

  Keys installed by any other route — `lager ssh-setup`, `ssh-copy-id`,
  cloud-init — live outside the marked region and are preserved byte-for-byte;
  they are never revoked by this loop. Loose copies of a key that *is* in the
  key directory get adopted into the block, which collapses historical
  duplicates. Note what adoption implies: a key installed by another route
  that is *also* published through the key directory becomes managed, so
  deleting its `.pub` later removes it outright — including the copy that
  other route installed, which nobody intended the key directory to own. Use a
  distinct key per access path when the two must be revoked independently.

  A key whose `.pub` was already gone before this release stays put as an
  unmanaged line: it cannot be told apart from an operator's own key, so
  removing it is a manual step. A missing key directory is treated as
  ambiguous and revokes nothing; an empty one revokes the managed keys.

  Another system that manages this file must claim its own distinct marker
  pair. Two managers sharing one pair would each rebuild the other's region
  from its own source on every pass.

### Fixed

- **`start_box.sh` is single-instance.** Concurrent copies raced each other and
  accumulated across restarts — boxes have been found running ten-plus copies
  at once, some months old, each having burned hours of CPU, with their
  key-sync loops appending over each other. The script now takes a non-blocking
  `flock` for its lifetime and exits with a clear message if another copy holds
  it. The background key-sync poller closes the inherited lock descriptor, so a
  long-lived poller cannot pin the lock against later runs.

## [0.34.1] - 2026-08-02

### Fixed

- **A J-Link script no longer leaks onto debug operations that never asked for
  one.** The box kept a single script at `/tmp/lager_jlink_script.JLinkScript`
  and handed it to any operation that did not supply its own. Only `connect` and
  `flash` send a script, so `reset`, `erase`, `memrd` and the gdbserver path
  silently inherited whichever script was written last -- by a different net, by
  an earlier session, or by a test suite that had since finished.

  Scripts are now written per net (`/tmp/lager_jlink_script_<net>.JLinkScript`),
  an operation with no net gets no script rather than an ambient one, and a
  net's script is cleared when its debug session ends -- on the in-box Python
  API path as well as over HTTP. `reset`, `erase` and `memrd` take the script
  explicitly instead of resolving it from a shared location, so a net with a
  genuinely required custom `InitTarget` still gets it on every operation. An
  older box's shared file is removed when the debug service next starts.

  OpenOCD configs still use a shared path: the daemon reads its config once at
  startup, and it is one daemon per probe rather than per net.

  Also fixed in the J-Link CLI test suite: its teardown reported success without
  checking, so a suite that failed to clear its script left the bench poisoned
  and said nothing. It now verifies the removal and prints the manual command if
  it did not take. Its script-content checks read the per-net path too; they had
  been asserting against the old shared file, which no current box writes.

- **`lager nets state` now says *why* a net has no state.** `state: null` meant
  three unrelated things with no way to tell them apart from outside the box:
  the instrument had not answered before the request deadline, its probe
  failed, or the role has no probe at all. They need different remedies, and on
  a multi-hub bench the first two are what made one hub look intermittently
  broken.

  Null entries now carry a `reason` -- `"deadline"`, `"no probe for role"`, or
  `"unreadable: <detail>"`. Entries that have a state carry no `reason`, so its
  presence means "this is a null, and here is why". `lager nets state` prints
  the unexpected ones in a footnote after the table, grouped by reason, and
  `--json` carries them as `live_state_reason`. Roles with no probe are not
  listed: they are normal, and reporting them would put a footnote on every
  bench.

  The `deadline` note says explicitly that the budget is shared across every
  instrument, because "this instrument is slow" is the wrong thing to go and
  investigate.

  Three supporting fixes.

  **A USB hub that will not open now says so.** This is the path that turns a
  whole hub null, and it discarded its exception silently -- so a hub that
  could not be reached was indistinguishable from one that answered null, with
  nothing anywhere naming which hub or why. It now logs the hub key, how many
  nets it is failing, and the driver's own cause.

  An Acroname *port* that will not read is still recorded as null so it does
  not lose the other seven, but its error code is logged instead of discarded.
  A partial result is the one shape a deadline miss cannot produce, so it is
  what separates a per-port fault from a hub-wide one.

  And the deadline log line said "N/M instrument groups answered" while
  counting *nets* -- on a bench with many nets per instrument that reads as
  though the grouping had collapsed, and it sent a real diagnosis down the
  wrong path. It now counts nets and names which ones were dropped.

  A CLI newer than its box simply sees no `reason` and renders as before.

  This is diagnosis, not a behavior change: the shared request budget and the
  hub discovery path that loses the race are unchanged. `reason: "deadline"` is
  the evidence needed to size that work.

- **A J-Link script no longer leaves a just-erased target unattachable.**
  A user `.JLinkScript` replaces J-Link's built-in *per-device* `InitTarget()`,
  and the replacement is per function -- a script defining no `InitTarget()`
  leaves the built-in in place and is harmless. On an nRF5340 that built-in is
  what brings the DAP up on a blank part: measured after a chip erase it takes
  ~425 ms, against ~3 us for a user stub that just returns. Displaced, the
  attach that follows an erase failed with `Could not read CPUID register` and
  `Failed to power up DAP` -- and because `flash` erases by default, one
  scripted flash could leave the part blank and the net failing every later
  attach.

  `connect` now exhausts its speed ladder **with** the script and, only then,
  retries once without it. Dropping the script is the more surprising change of
  behavior, so it happens last and is reported rather than silently succeeding:
  the response carries `script_skipped` and the box logs that the target is not
  running the configured init. A target that is unreachable either way still
  fails, so the retry cannot turn a dead board into a pass.

  When the scriptless retry also fails, the error now names the script and the
  command to remove it. Previously it offered cabling advice and never mentioned
  the script, so a poisoned net presented as dead hardware.

  `lager nets set-script` warns when the script being attached defines
  `InitTarget()`, naming what it displaces. It is a warning, not a refusal --
  custom target init is a legitimate use -- but nothing else told you what you
  had taken over.

- **`lager update` no longer leaves a file on the box after it is deleted
  upstream.** The step that flattens the sparse-checkout layout (`box/` into
  the repository root) copied with `cp -rf box/* .`, which is additive: it
  overwrote changed files but never removed ones the new tree no longer
  contained. The copy then deleted the tracked source, leaving the flattened
  tree untracked — so neither `git checkout -f` nor `git reset --hard` had any
  authority over it, and nothing else in the update path could clean it up.

  A file deleted upstream therefore persisted on every box indefinitely and
  was copied into the runtime image by the next docker build. Boxes were found
  carrying a module deleted thirteen minor versions earlier, and the
  execute-capable MCP tools removed as a deliberate security reduction were
  still sitting in the image releases after the change that removed them.
  Nothing imported them, so they were inert rather than reachable, but a
  security-motivated deletion that still ships is not a deletion.

  Each top-level entry is now removed and then moved into place, so a subtree
  is rebuilt rather than merged into and deletions propagate on the first
  update. Entries at the repository root that `box/` does not provide are
  untouched, so `.git` and the sparse-checked-out `cli/` are never in range.
  No manual cleanup is needed: the first update carrying this fix removes the
  accumulated files and the image rebuilds without them.

- **A source-only change now invalidates the cached Docker image.** The build
  hash covered only `box.Dockerfile` and `requirements.txt`, so a pure-Python
  change left `must_wipe_image` false and correctness rested entirely on
  BuildKit's `COPY` cache. That held only incidentally — an early `COPY *.py`
  layer tended to change too, invalidating the layers after it — and it is the
  same class of silent staleness as the flatten bug above. Every file under
  `~/box/lager` now feeds the hash. `__pycache__` directories and `.pyc` files
  are excluded, since they are regenerated on the box and would otherwise
  force a rebuild on every run.

  Because the stored hash on an existing box was computed with the old
  formula, it cannot match the new one: **the first `lager update` after this
  release wipes the cached image and rebuilds it in full, once per box.** On
  Pi-class hardware that is a slow update. Subsequent updates only rebuild
  when something under `~/box/lager` actually changed.

## [0.34.0] - 2026-07-30

### Added

- **`lager nets state` reports live hardware state for every saved net**, via a
  new `GET /nets/state` box endpoint. Power supplies report channel/output/
  voltage/current, USB ports report enabled or disabled, GPIO reports level,
  ADC/DAC report volts, and so on; roles with no probe report nothing rather
  than guessing. `--json` emits the same data unformatted.

  It stays a separate subcommand rather than folding into `lager nets`, because
  it touches hardware: plain `lager nets` is a single `saved_nets.json` read
  (~0.31s measured, no instrument access), while this takes the same instrument
  locks a running `lager python` test holds.

  Boxes too old to have the endpoint report a clear upgrade hint instead of an
  HTTP error.

  Probing is per *instrument*, not per net. Every driver wraps each call in its
  own open/operate/close cycle under that instrument's lock -- correct for
  one-shot commands, but it means N nets on one device cost N full
  enumerate/connect/disconnect cycles, serialised, however wide the pool is
  (~2.4s for a single hub port read on real hardware, ~4.5s for a hub that is
  not currently discoverable). Nets are grouped by physical instrument so that
  cost is paid once per device, and the endpoint always answers within its
  deadline: a wedged instrument yields nulls for its own nets instead of a 500
  for the whole bench, and the request does not wait for it.

### Fixed

- **`lager nets state` no longer reconfigures the hardware it is reporting on.**
  Two paths mutated the instrument while answering a read-only query. GPIO
  state was read with an unconditional `input()`, which on a LabJack T7
  reconfigures the pin as an input -- guarded for `FIO` pins only, so `EIO`,
  `CIO` and `MIO` took the unguarded path and a pin holding a target's reset,
  boot-mode or enable line was silently released just by listing state. And the
  ADC path wrote `_RANGE`, `_NEGATIVE_CH`, `_RESOLUTION_INDEX` and
  `_SETTLING_US` before each read, so a deliberately configured differential
  pair or chosen resolution was reset to single-ended +/-10V mid-measurement.
  GPIO now reads the `DIO_DIRECTION` + `DIO_STATE` registers, which reports
  every pin in the DIO range without touching direction, and AIN channels are
  read exactly as configured.

- **The LabJack batch probe now serialises against concurrent `lager
  gpo`/`gpi`/`adc`/`dac` commands.** It locked on a hardcoded `"labjack:ANY"`
  while `/invoke` locks on the net's real device identity, so on any bench whose
  LabJack net carries an address the two took different lock objects and could
  interleave I/O on one shared LJM handle -- the exact contention the batch
  endpoint was introduced to remove. Both sides now key on the identity the
  caller resolves.

- **`lager debug <net> flash` no longer leaves the target blank when the
  post-erase reconnect fails.** `flash` erases by default, and it used to
  disconnect and reconnect the debugger between the erase and the flash. That
  reconnect sat inside the erase's own `try`, so when it failed the command
  printed `Flash erase failed`, exited 1, and never called `/debug/flash` -- on
  a part it had just wiped. When that connect fails -- it answers 500, "Failed
  to power up DAP" -- a plain `lager debug NET flash --hex fw.hex` left the
  target it was asked to program erased, and every later command on that net
  failed until someone restored a valid image by hand.

  The reconnect is now gone for every device family, because it was never
  load-bearing for either backend. On J-Link, `/debug/flash` runs its own
  `JLinkExe` session: `flash_device` stops the gdbserver on entry and
  re-establishes one after programming, so anything the CLI started here was
  torn down moments later. On OpenOCD it was actively harmful -- `/debug/erase`
  leaves the daemon running and `/debug/flash` programs over that same daemon,
  answering 400 when it is missing, so the disconnect removed the session the
  flash depended on. The DA1469x path already skipped the reconnect and is
  unchanged.

  `--force-reconnect` still requests a clean session before flashing, and still
  warns and continues rather than aborting if that reconnect fails. A failed
  erase is still fatal.

- **`lager debug <net> flash` no longer reports "Flashed!" when nothing was
  programmed.** `/debug/flash` answers 200 whether or not the probe ever
  attached -- the box's `flash_device` is a generator that yields the
  programmer's output and carries no success channel -- and the CLI printed
  that output and then reported success without inspecting it. Short of an HTTP
  error the command could not fail, so a run whose log read
  "ERROR: Could not connect to target" still finished with "Flashed!" and exit
  0, leaving the caller believing a blank part had been programmed. Because
  `flash` erases by default, that silence did not leave the previous image in
  place; it left nothing.

  The command now takes its verdict from the programmer's own output, and
  reports which line it failed on plus the fact that the part is now erased.
  Evidence of programming wins over a later connect error, since
  `flash_device` re-establishes a gdbserver *after* programming and that
  reconnect can fail on a part that was written correctly. Output that matches
  neither keeps its existing meaning, so an older box or a backend we have not
  characterised is never newly reported as failing.

## [0.33.1] - 2026-07-29

### Fixed

- **The box's MCP server no longer fails to start after `mcp` 2.0.0 was
  published.** The box image asked for `mcp>=1.0.0` with no upper bound, so
  any image built after the SDK's 2.0.0 release installed it. Version 2.0
  renamed `FastMCP` to `MCPServer` and moved transport configuration
  (`host`, `port`, `transport_security`) off `mcp.settings` and onto
  `run()` / `streamable_http_app()`; `box/lager/mcp/server.py` still uses
  the 1.x form, so the service raised on startup and nothing ever listened
  on port 8100. Agent clients pointed at `http://<box-ip>:8100/mcp` saw
  connection timeouts. Both the box image and the CLI's optional `mcp`
  extra now cap the dependency below 2.0 until the server is ported to the
  new API. Boxes pick the fix up on the next `lager update`.

## [0.33.0] - 2026-07-28

### Added

- **`GET /usb/devices` — generic USB bus enumeration on port 9000.** Walks
  `/sys/bus/usb/devices` and returns every device's vid/pid, iSerial,
  product, manufacturer, bus/dev numbers, devpath, class, and speed, with
  optional `vid`/`pid`/`serial` query filters. Pure non-blocking sysfs
  reads (a few ms, no exclusive device access), so clients can poll it
  while waiting for a DUT to re-enumerate after a hub power-cycle or DFU
  detach. Consumed by `lager-rs`'s new `usb_devices()`.

- **`POST /usb/dfu` — box-side dfu-util.** Actions: `list` (parsed
  `dfu-util -l` output as structured JSON), `download` (base64 firmware
  written to a temp file, with optional `-d vid:pid`, `-S serial`, `-a
  alt`, `-s` DfuSe address, `-R` reset), and `detach` (`-e`). Runs are
  serialized under their own lock (a long download never blocks hub-port
  commands), time-bounded (120s default, 600s cap), and a missing binary
  returns a clear `dfu-util-missing` error pointing at
  `lager box-config apt add dfu-util`. Consumed by `lager-rs`'s new
  `dfu()` handle.

### Changed

- **USB hub drivers now cache discovery metadata per physical hub.** The
  0.31.2 contention fix (open/operate/close per call so no process pins the
  hub's exclusive USB claim) made every Acroname operation re-run BrainStem
  discovery, and up to three scans when the class-order fallback ran. The
  drivers now cache discovery *metadata* — the hub's link Spec and winning
  hub class (Acroname), the resolved HID device path (YKUSH) — and connect
  directly from it. Connections themselves are still never cached: the
  never-pin-the-hub invariant is untouched, and a stale cache entry (hub
  re-enumerated) falls back to full discovery automatically.

  This removes redundant discovery scans but does not restore the ~80ms
  hub-port timings seen before 0.32.1. Measured on a USBHub3p, one hub-port
  operation costs ~2.1s: ~1.8s is the per-operation `disconnect()` required
  to leave the hub unclaimed, ~0.3s is discovery, and the port read itself
  is ~2ms. Where the hub class is identified correctly on the first
  attempt, the cache saves no measurable time. Reducing the disconnect cost
  is tracked separately.

  Also fixed the `usb.py` handler docstrings that still claimed the drivers
  cache hardware handles.

## [0.32.6] - 2026-07-28

### Added

- **`lager install` and `lager update` now install the lager CLI onto the
  box's host OS, version-matched to the deployed box code.** The box's sparse
  checkout gains the `cli/` directory, and both flows pip-install it from
  `~/box/cli` into a dedicated venv at `~/.lager/venv`, with `lager` (and
  `lager-mcp`) symlinked into `~/.local/bin` — so a self-hosted CI runner on
  the box can invoke `lager` locally, and the host CLI tracks the box as it is
  updated. The update path reinstalls whenever box code changed (a branch
  deploy can change code without bumping the version string), and the
  "already up to date" fast path still probes the host CLI cheaply and
  reconciles it when missing, broken, or mismatched — so existing boxes pick
  the CLI up on any routine update. `lager update --check` reports a
  `Host CLI:` line and counts a pending install toward its would-change exit
  code. The step is non-fatal and exit-code verified rather than
  pip-stdout-grepped: a host whose `python3` predates 3.10 (the CLI's floor)
  gets an honest warning and an otherwise successful run, and Debian/Ubuntu
  hosts missing `python3-venv` get it through the existing batched
  privileged-operation path (at most one sudo prompt per run). The dedicated
  venv sidesteps PEP 668 (`externally-managed-environment`), which a plain
  `pip3 install --user` hits on Ubuntu 23.04+.

### Fixed

- **`lager debug` subcommands now authenticate against access-controlled
  boxes.** The debug service client reached the box directly over HTTP and
  attached no Authorization header, so every box-contacting debug subcommand
  (status, health, memrd, reset, flash, erase, gdbserver, disconnect) failed
  against a gateway-gated box with "Stout authorization required", and no user
  action worked around it. All twelve client methods now resolve a bearer
  token per call — nothing is cached on the session, because a
  `gdbserver --rtt` client can issue its first and last request hours apart —
  and responses run through the gateway check before `raise_for_status`, so a
  gateway 401 surfaces as an actionable sign-in message instead of a bare
  `HTTPError`.

- **`pymongo` is now a declared dependency.** `lager status` does
  `from bson import decode`, which is pymongo's API; the similarly named PyPI
  `bson` package exports different functions, so installing "bson" to cure
  the `ImportError` would still fail. Declaring the right package removes the
  trap.

- **`lager uart` no longer fails to import on Windows.** `websocket_client.py`
  imported `termios`/`tty` unconditionally, making interactive UART mode a
  hard `ImportError` there. The import is now guarded, and interactive mode
  reports an honest "not supported on this platform" error instead of
  degrading into a silently broken line-buffered session.

### Changed

- **static-checks now enforces `test/COVERAGE.md`'s gated-test counts.**
  The file states its numbers are checked against disk, but nothing checked
  them and they drifted three times in one afternoon.
  `tools/check_coverage_counts.py` runs each suite and compares passed,
  skipped and xfailed separately. shellcheck also ratchets from `-S error` to
  `-S warning` (pinned via shellcheck-py), and the unit-test dependency file
  gains major-version caps so the compat matrix cannot break on an unrelated
  upstream major release.

### Security

- **quinn-proto bumped to 0.11.15 in the box oscilloscope daemon**, closing
  GHSA-4w2j-m93h-cj5j (high severity: remote memory exhaustion via unbounded
  out-of-order stream reassembly). The daemon opens QUIC listeners that start
  on box boot whenever the binary is present. Merging the bump does not by
  itself patch a deployed box — the daemon binary is built by hand via
  `build_daemon.sh` — which is recorded alongside the advisory so a closed
  alert is not mistaken for a patched fleet. A Dependabot config was added so
  future advisories produce fix PRs instead of sitting open in the security
  tab.

## [0.32.5] - 2026-07-27

### Added

- **A `Static Checks` workflow covering the parts of the tree pytest cannot
  reach.** `test/integration/` (38 bash scripts run against a real box),
  `test/manual/` and `test/framework/` had no validation of any kind, not even
  syntax; `test/api/` is 81 standalone scripts of which only 6 are ever
  invoked. The workflow runs `bash -n` and `shellcheck -S error` over all 42
  shell scripts, `compileall` over every Python file, `--collect-only` over the
  MCP integration suite, and `ruff` restricted to real errors
  (`E9,F63,F7,F82`). Each of those was already clean on the tree, so the floors
  are meaningful from day one and can ratchet upward. A separate report-only
  `coverage` job publishes per-module line coverage to the job summary, with no
  threshold.

- **Unit suites now run on Python 3.10 and 3.12 as well as 3.11.**
  `cli/setup.py` declares `python_requires=">=3.10"` while CI tested 3.11
  only. A separate `compat (pyX.Y)` job covers the rest -- separate rather
  than another dimension on the existing matrix, because that would rename
  the `unit (...)` contexts the branch ruleset requires and strand every open
  PR on checks that never report.

  It found two real problems on its first run. 3.13 and 3.14 are deliberately
  not in the matrix yet: `box/lager/python/service.py` imports `cgi`, which
  PEP 594 removed in 3.13, so the box suite cannot even be collected there --
  a forward-compatibility gap in the box's python service, tracked separately
  because migrating its multipart parsing off `cgi.FieldStorage` touches the
  file-upload path.

### Changed

- **The PR unit-test gate now runs 2291 tests, up from 1066.** Two bodies of
  hardware-free tests already in the repo were never executed by CI:
  `test/unit/box/` (56 files, 1142 tests — larger than all previously gated
  suites combined) and `cli/tests/` (auth, update-gate and box-storage
  coverage). Both are now gated. The box suite was excluded because roughly a
  dozen of its modules register a placeholder `lager` in `sys.modules` so they
  can load single box files without executing the heavy package `__init__`,
  while twenty-two module-level `from lager ... import` statements require
  that `__init__` to have run; only alphabetical collection order reconciled
  the two, so any `-k`, `--ignore`, explicit file list or newly added
  earlier-sorting file broke collection with `cannot import name ... from
  'lager' (unknown location)`. A new `test/unit/box/conftest.py` imports the
  real package once before any test module loads and asserts it resolved to
  the on-disk package, removing the ordering dependency. pytest is now pinned
  so the gate cannot go red from an unrelated upstream release.

### Fixed

- **First contact with a gated box now authenticates everywhere — `lager
  boxes` no longer reports `HTTP 401` forever.** The CLI only sends a bearer
  token to boxes already in its box→auth-server map, and that map is learned
  from a gateway's discovery 401 — but a dozen call sites attached the auth
  header without ever running the record-and-retry step, so first contact
  failed and never self-healed. Every CLI path that talks to a box (HTTP and
  WebSocket) now records the mapping, retries once with a held token, and
  surfaces genuine denials actionably. `lager boxes` renders a per-box
  verdict (`sign-in required`, `no access`, `auth server down`) with a
  `lager login` hint instead of a raw status code and never aborts the
  table on one gated box; the uart instrument listing no longer reports an
  auth denial as an empty instrument list; `run_pip` sends auth at all.
  SocketIO clients (supply/battery/uart), whose handshake exceptions hide
  response headers, discover via an HTTP probe and retry the handshake
  once. Plain boxes are untouched: no token is ever sent without the
  discovery header, and application 401/403s keep their behavior.
- **`lager debug` could not reach a box behind an authenticating gateway.**
  The debug service client talks to the box directly over
  `http://<box>:8765` and attached no `Authorization` header, so every
  box-contacting subcommand — `status`, `health`, `memrd`, `reset`, `flash`,
  `erase`, `gdbserver`, `disconnect` — failed with "Stout authorization
  required". No user action worked around it: the path sent no credential at
  all, so a correct `lager login` did not help. The client now resolves the
  bearer token per request and records-and-retries a first-contact denial in
  call, bringing this path into conformance with sections 6.2, 6.3 and 6.4 of
  the gateway auth contract, which names the debug service and its streaming
  RTT requests explicitly. The token is deliberately resolved per call rather
  than cached on the session: a `gdbserver --rtt` client issues its first and
  last request hours apart, and a cached header would replay an expired
  token. The gateway retry helper now forwards the caller's `stream` and
  `timeout`, without which replaying an RTT request would block forever
  buffering a body that never ends. Plain un-gated boxes are unaffected — no
  token is stored, sent, or looked up. Covered by 22 new unit tests; the path
  previously had none.

  Only the network-side client changed. `box/lager/debug/service_client.py`
  serves on-box `lager python` scripts over loopback, is always past the
  gateway, and is now documented as intentionally divergent.

- **The J-Link CLI suite's CI ratchet could read the wrong column.** It took
  the last field of the harness `TOTAL` row as the failure count, but the
  harness appends an `Excluded` column whenever any check is skipped — so on
  a run with skips the ratchet compared the skip count against its baseline
  and would have passed with an arbitrary number of real failures. It now
  addresses the column by position and derives the check total from the same
  row instead of hardcoding it.

- **`lock_state.acquire` rejected a valid holder type in test only.** The box
  unit suite asserted an unrecognized `holder_type` was coerced to
  `ephemeral`; the implementation deliberately preserves it verbatim, because
  other services write their own origin token and coercion would attach a TTL
  and let the reaper drop someone else's reservation. The test encoded the
  pre-consolidation contract and had never run in CI.

- **`cli/tests/test_io_imports.py` asserted import aliases that no longer
  exist.** `lager.adc` / `lager.dac` / `lager.gpio` were consolidated under
  `lager.io.*`; the test still required the old paths to import. It now covers
  the supported surface, verifies `lager.io` re-exports are the same objects
  as the submodules, and asserts the removed aliases stay removed.

- **`lager status` raised `NameError` instead of handling the failure on
  Python 3.10.** The websocket path catches `BaseExceptionGroup`, a builtin
  only from 3.11, with no guard -- so on a version the package claims to
  support, evaluating the except clause failed and masked the original
  exception. It now imports the `exceptiongroup` backport (already present via
  trio) below 3.11. Surfaced by adding ruff.

- **The measurement unit suite failed 21 tests on Python 3.10.** Its conftest
  loads box modules by path and registers them in `sys.modules`, but never
  bound each module as an attribute of its parent package -- something the
  real import system does. `unittest.mock.patch` resolves a dotted target by
  walking attributes, so patching `lager.measurement.watt.ppk2_watt.*` raised
  `AttributeError: module 'lager' has no attribute 'measurement'`. Python 3.11
  changed mock's lookup to fall back to an import, which hid the omission
  everywhere CI was looking. Found by the new compat job.

- Box unit tests no longer overwrite `simplejson` with the stdlib `json`
  module in `sys.modules`. Nine files did this at import time, process-wide and
  uncleaned, *after* the real module was already bound -- it changed nothing
  and simply waited to surprise the next reader. `simplejson` is a declared
  test dependency and is installed.

- Added `test/unit/box/test_lager_package_identity.py`, which asserts the
  invariant `test/unit/box/conftest.py` establishes: `lager` must resolve to
  the real in-repo package with its `__init__` executed. Re-stubbing it used to
  fail far from the cause with `(unknown location)`; it now fails by name.

## [0.32.4] - 2026-07-24

### Fixed

- **Gated-box sessions no longer die with a spurious "Box requires sign-in"
  mid-command.** Three hardening fixes in the CLI's gateway-auth refresh
  path: the refresh-ahead margin now scales with the token's issued
  lifetime (a fixed 60-second margin against a server issuing 60-second
  tokens turned every box request into a refresh round-trip — a refresh
  storm in which each refresh rotated the refresh-token family and any
  hiccup lost the whole session); a refresh that fails while the stored
  token is still valid now falls back to that token instead of
  hard-failing the command; and a refresh whose connection never reached
  the server is retried once (ambiguous failures such as read timeouts
  are deliberately not retried — replaying a refresh can trip the
  server's rotation replay detection). Found by the box-lifecycle CI's
  first supervised runs.

- **`lager install` and `lager uninstall` no longer remove containers and
  images they do not own.** The deploy script stopped and force-removed
  EVERY container on the box and pruned every unused image; uninstall's
  image cleanup did the same prune. On a box that also runs third-party
  containers (a management agent, a user's own services) this destroyed
  infrastructure the tooling cannot restore. Cleanup is now scoped to the
  containers lager creates (`lager`, `pigpio`, legacy `controller`), the
  lager image, and dangling layers. Found by the box-lifecycle CI's first
  supervised reinstall, which took down the box's gateway agent.

## [0.32.3] - 2026-07-22

### Fixed

- **`lager update` could report "already at version X" on a box that was
  never actually updated.** The update stops and removes the box's
  containers before rebuilding the image, so a failed build left the box
  with no services at all — and the retry's early-exit gate checked only
  source state (git in sync, docker-build-inputs hash matching), so it
  printed a green success and exited 0 on a dead box. Found via a field
  report of an update that failed during Docker image export; the retry
  claimed the box was up to date and the user had to restart it by hand
  over SSH. The gate now also requires the lager container to be RUNNING
  and the last successfully deployed version (`/etc/lager/version`) to
  match the tree — a box left dead, or left serving an older build by an
  update interrupted between pull and rebuild, falls through to a real
  rebuild and restart with an explanatory message. `lager update --check`
  surfaces both states ("Container: NOT RUNNING" / "running a STALE
  build") and exits 1. Hardware-validated end to end on a real box.

- **A failed rebuild no longer strands the box with no services.** Unless
  the cached image was wiped (`--force` / a build-inputs change), the
  update restarts the previous image, waits for `/health`, and states
  plainly that the update FAILED and was not applied (exit code stays 1).
  Every build failure also poisons the stored build-inputs hash with a
  sentinel so the next run always performs a clean rebuild instead of
  trusting stale state. New build-failure hint for BuildKit cache
  corruption ("failed to prepare extraction snapshot ... parent snapshot
  does not exist"): clear with `docker builder prune -af` and re-run.

## [0.32.2] - 2026-07-22

### Added

- **The first command against a freshly-gated box now just works.** The CLI
  learns a box's auth server from the box's first 401 response, so the very
  first command against a newly-guarded box used to fail with a "re-run this
  command" message. That request is now retried once, transparently: the
  box-to-auth-server mapping is recorded, the stored session token attached,
  and the request resent — the caller gets the authenticated response and
  never sees the round trip. A plain box never receives the token (only a
  gateway sends the discovery header), and genuine denials — revoked
  session, no access grant, auth server unreachable — still raise their
  actionable errors.

- **`lager whoami` — access-gateway sign-in status at a glance.** Shows which
  auth servers you're signed in to, as whom, and whether each session is
  active, auto-renewing, or expired (with the exact `lager login` command to
  fix it). It's the first thing to run when a box reports an authorization
  problem.

- **Clearer gateway auth errors, each linking to a new "Signing In" docs
  page.** "Signed in but not authorized", "requires sign-in", and "session
  rejected" are now distinct messages with their own fixes, and the docs
  page (reference/cli/login) walks through every gateway message and what
  to do about it.

- **The Rust crate gets a first-class "Rust API" tab on the docs site** —
  overview, net types, cargo-test guide, debug/UART, and auth — with the
  getting-started intro reframed so Python, Rust, and MCP read as equal
  automation paths.

### Changed

- **`lager box config` is now `lager box-config`, `lager box dut` is now
  `lager dut`, and `lager authorize` is now `lager ssh-setup`.** The `box`
  group is flattened to top level, and the SSH-key setup command no longer
  reads like authentication now that `lager login` exists — it installs this
  machine's SSH key on a box (one-time passwordless-SSH setup), which the
  new name says plainly. All three old spellings keep working as hidden
  aliases that print a DEPRECATED warning on stderr; they will be removed in
  a future release. Help text, error hints, docs pages, and docs navigation
  all follow the new names.

## [0.32.1] - 2026-07-21

### Fixed

- **`lager adc` / `lager dac` failed on every named channel.** The migrated
  dispatchers' channel resolver only accepted integers, but the scanner saves
  LabJack T7 and MCC USB-202 adc/dac nets with named pins (`AIN0`, `CH0`,
  `DAC0`) — so every read/write on those nets failed with "Invalid channel
  pin". Named pins now pass through to the drivers, which already parse them
  (the gpio dispatcher's long-standing behavior). Hardware-verified on both
  instrument families.

- **`lager supply <net> set` failed with "Unknown action: set_mode" for every
  supply model**, and **`--ocp`/`--ovp` on `supply voltage`/`supply current`
  were silently discarded** — the command reported success but the protection
  limits never reached the instrument. The handler now implements `set_mode`
  and forwards `ocp`/`ovp` to the drivers with hardware-limit validation; a
  protections-only call (e.g. `voltage --ovp 6` with no value) applies the
  protection instead of falling into the read path. Also, `clear-ocp` /
  `clear-ovp` now use the uniform driver wrappers, fixing a 502 on EA PSB
  supplies whose granular clear methods take no channel argument.

- **`set_model('discharge')` on the Keithley 2281S raised
  `BatteryBackendError` in 0.32.0**, breaking HIL flows that select discharge
  battery simulation over SCPI. Discharge is the instrument's always-available
  idle default, not a stored model — the firmware rejects every SCPI recall
  form for it and `:BATT:MOD:RCL?` never echoes it, so the stricter
  recall-then-verify introduced for empty-slot detection could only fail.
  The request is now treated as satisfied: nothing is sent (a rejected recall
  can corrupt the SCPI parser's input buffer) and the cached active model
  becomes DISCHARGE. Strict empty-slot detection for slots 1-9 and built-in
  model verification are unchanged.

- **A Joulescope JS220 could be lost until a container restart after a
  `lager python` claim handoff.** The box releases its USB claims before a
  user script runs; right after the script exits, the JS220 can be briefly
  unopenable (`jsdrv_open -4`) or missing from an in-process scan while it
  settles, and a re-enumeration could wedge the service's USB context
  outright. The open path now retries with a short backoff (mirroring the
  pyvisa Resource-busy retry), and the jsdrv failure signatures are wired
  into the service's self-restart wedge detection, so an enumerated-but-
  invisible device recovers with an automatic ~2s service respawn.

- **Hardware errors printed a raw Python dict containing the full box-side
  traceback**, and an internal proxy failure printed a literally empty
  "Hardware error: ". Driver errors now surface as their one-line message
  (the traceback stays in box logs), and connection failures name their
  cause.

- **A slow box-side operation was misreported as "cannot reach box".** The
  CLI's request funnels now distinguish a read timeout (box reachable,
  operation still running — e.g. first contact with a USB hub, which runs
  discovery that can exceed 10s) from a genuine network connection failure,
  and the USB commands get a 30s first-contact budget.

## [0.32.0] - 2026-07-20

### Added

- **CLI-to-box communication now runs on the box's :9000 hardware-service API.**
  Net commands (gpio, uart, watt, energy, battery, supply, usb, and more), plus
  ble, webcam, arm, wifi, router, blufi, box management, solar, net management,
  and binaries, all use dedicated HTTP handlers with in-process drivers —
  replacing the legacy :5000 script-upload model and its per-call subprocess
  spawn. Commands against a box running an older image now warn clearly
  ("run: lager box update") instead of degrading silently.

- **Instrument claims are coordinated with `lager python`.** The box releases
  its direct-USB claims (LabJack, FT232H, Aardvark, Joulescope/PPK2, Phidget,
  Dexarm) before a user script runs and re-claims afterward, so scripts that
  open instruments directly no longer fight the warm device cache.

- **`lager login` — authentication for gateway-fronted boxes.** Deployments
  that place an authenticating reverse proxy in front of a box are now fully
  supported: the CLI discovers the auth server from the box's 401 response,
  `lager login` stores a session (0600 on disk, transparent refresh, MFA
  supported), every CLI-to-box request attaches the session automatically, and
  denials explain exactly what to run. Boxes without a gateway are completely
  unaffected — no prompts, no stored tokens, no behavior change.

- **`start_box.sh --no-publish` for reverse-proxy deployments.** Runs the box
  container reachable only on the internal Docker network, and the chosen mode
  persists across restarts so an update can't republish ports out from under a
  proxy that owns them. `--publish` restores the default. Default behavior
  without either flag is unchanged.

### Fixed

- **Webcam start/url/stop commands crashed with a `TypeError`** after the
  :9000 migration (an internal parameter collision); all three work again, and
  `webcam start` on an access-gated box now notes that the stream URL is not
  directly reachable there.

- The CLI test suite's SIGPIPE crash and several pre-existing test failures.

## [0.31.16] - 2026-07-20

### Fixed

- **A failed debug connect now shows the real J-Link reason instead of a Python
  traceback.** `validate_speed()` returned the caller's value unchanged, so when
  a caller passed an integer speed, the connect-error message's
  `', '.join(speeds_to_try)` raised `TypeError: sequence item 0: expected str
  instance, int found` — which *replaced* the actual diagnosis (e.g. "Failed to
  power up DAP", "Cannot connect to target") in the console. It now returns a
  normalized string, as its docstring already promised, and the gdbserver argv
  stringifies the speed as well. This is the fix that matters: a debug failure
  used to lie about why it failed.

- **A leftover GDB server can no longer wedge the next connect on its port.**
  Cleanup before starting a J-Link GDB server was anchored on the probe serial
  (`-select USB=<serial>`), so a server left running under a different
  `-select` tag — e.g. a bare `-select USB` from a fallback path — kept holding
  the GDB port under `-stayrunning 1`. The two servers then collided ("Failed
  to open listener port 2331" on one, "Failed to power up DAP" on the other)
  and deadlocked the probe. The port itself is now swept before binding,
  matched on the exact `-port <n>` token so sibling probes on other ports are
  untouched.

- **The connect-failure message now includes the J-Link server's real log.**
  The failure path read `status.get('logfile')`, but `status` is only bound on
  the success path — so every failure printed "No log available" and hid the
  server's actual complaint. The server's on-disk logfile is now read back on
  failure (falling back to the log captured by the last start error).

- **An opaque "RTT auto-detection failed: 'LAGER_BOX_COMMANDS'" warning is now
  actionable.** `get_device()` read `LAGER_BOX_COMMANDS` unguarded and raised a
  bare `KeyError` when that variable is absent — the state when a script is
  exec'd into the box container directly rather than run through lager. It now
  raises a clear message explaining the variable is unset and the device must be
  passed explicitly.

## [0.31.15] - 2026-07-20

### Added

- **`BlufiClient.scan(timeout=10.0, name_prefix=None)` — BLE advertisement scan
  on the box Python API.** Returns nearby BLE devices as `{name, address,
  rssi}` dicts sorted by RSSI descending, with an optional exact-prefix name
  filter, so a test suite can confirm its target is advertising before
  attempting a BluFi connection. Previously the API offered no presence check:
  a suite that missed `connectByName`'s `False` return went on to drive a
  never-connected client and failed later with a confusing `'NoneType' object
  has no attribute 'write_gatt_char'`. An empty `scan(name_prefix="MyDevice-")`
  is now a clean, actionable failure. RSSI is read from `AdvertisementData`
  (`BLEDevice.rssi` is deprecated in bleak; boxes pin 0.22.2, which supports
  `return_adv`). The `lager ble scan` and `lager blufi scan` commands already
  provide the equivalent from the CLI.

## [0.31.14] - 2026-07-17

### Fixed

- **A UART net no longer stays stuck at "already in use by another session"
  after a session's read loop wedges on a disconnected serial adapter.** The
  box tracks live UART sessions in an in-memory registry guarded
  per-connection, per-net, and per-device; an entry was only removed by a clean
  stop, a socket disconnect, or the read thread's exit path. If the read thread
  wedged inside a blocking serial read (a USB-serial adapter that vanished or
  re-enumerated without raising a device-gone error), none of those ran, so the
  net stayed reserved with no live reader until the box restarted. Each session
  now carries a monotonic heartbeat; a new connection reclaims a holder whose
  read thread has died or whose heartbeat has aged past 30s, instead of
  refusing to start. A live or reconnecting session keeps its heartbeat fresh
  and is never reclaimed.

## [0.31.13] - 2026-07-16

### Fixed

- **Joulescope JS220 watt reads no longer fail with "is not connected" after
  the first read on the warm `/net/command` path.** The handler closes the net
  after every read to release the USB device, but `close()` left the
  per-serial driver singleton cached as initialized — every later construction
  got the dead handle back, and one net's close also broke a sibling net
  sharing the same physical JS220, until the box runtime restarted. `close()`
  now evicts the instance so the next read reopens the device, `clear_cache()`
  can no longer deadlock, the JS220/PPK2 energy analyzers re-acquire the
  shared watt driver if the other net closed it, and dispatcher driver caches
  drop closed instances via a health check. (The per-command `/python`
  executor path had masked all of this: a fresh process per read discarded the
  poisoned cache.)

- **Energy-analyzer reads work on nets addressed by a VISA resource string.**
  The energy dispatcher passes the net's VISA address
  (`USB0::0x16D0::0x10BA::<serial>::INSTR`) as the driver location, which was
  misparsed to serial `INSTR` — and even with the correct serial, the
  joulescope v1 API has no top-level `Device` class for the old re-wrap, so
  every such read failed with "Joulescope with serial 'INSTR' not found"
  listing unreadable object reprs. VISA USB resource strings now parse to the
  serial field, devices are matched via their `serial_number`/`device_path`
  attributes and opened directly, and the not-found error lists device serial
  numbers. A specified serial that matches nothing still errors instead of
  silently opening the first device, which would measure the wrong unit on a
  multi-Joulescope bench.

- **A warm-path energy read no longer blocks subsequent watt reads (and
  external tools) with `jsdrv IN_USE`.** Once the VISA fix let the in-process
  energy path actually open the JS220, it held the device's exclusive USB
  claim indefinitely. The energy handler now releases the device after every
  read, exactly like the watt handler, and the next read re-acquires it
  automatically.

## [0.31.12] - 2026-07-15

### Added

- **`lager battery <NET> model-create <SLOT> --csv <file> [--force]` — create a
  custom battery model from a CSV file.** Writes a voltage/resistance curve
  into a Keithley 2281S memory slot (1-9); previously custom models could only
  be authored at the instrument's front panel. The CSV has two columns
  (`voc,resistance`, header optional) ordered from empty battery to full, with
  exactly 11 or 101 data rows — 11-row files are interpolated to 101 points by
  the instrument (verified exactly linear). Files are validated client-side
  with line-numbered errors (row count, VOC non-decreasing, resistance
  non-increasing, value ranges) before anything reaches the box. Saving
  overwrites the slot, and the instrument has no way to delete a saved model —
  a slot can only be overwritten — so occupied slots are refused unless
  `--force`.
- **`lager battery <NET> model-export <SLOT> --csv <out>` — export a saved
  battery model's curve to CSV.** Read-only: writes the slot's 101
  `voc,resistance` points in the exact format `model-create` accepts, enabling
  the export → edit → create round-trip. Exporting reads the saved slot
  directly and never changes the active model. Exporting an empty slot is an
  error that points at `models`.

### Fixed

- **`lager battery <NET> model discharge` no longer fails with a misleading
  "slot appears to be empty" error.** Discharge mode is not selectable over
  SCPI on current 2281S firmware: the instrument rejects every recall form
  (numeric 0 is out of range — only slots 1-9 are valid recall arguments — and
  the DISCHARGE name and its quoted/abbreviated variants are syntax errors).
  The command now says so up front, pointing at the front panel and at
  `models`, and the model catalog no longer advertises a slot-0 DISCHARGE
  entry that was never actually loadable. A discharge selection made from the
  front panel still reads back as DISCHARGE.

## [0.31.11] - 2026-07-13

### Fixed

- **`lager box config apply` no longer reports success while applying nothing.**
  `apply` runs `start_box.sh` on the box as the login user, and its four
  box-config renderers *create* files in `/etc/lager` (`box_config.docker.sh`,
  `user_requirements.txt`, `cargo_packages.txt`, `npm_packages.txt`). Creating a
  file needs write permission on the **directory**, and `lager install` left
  `/etc/lager` owned by the container user only (`33:33`, mode `755`) — so every
  render failed with `EACCES`. Renders are soft-failed by design (the container
  must always come up), which turned this into a silent no-op: the install steps
  read files that were never written, so `apply` skipped them, stamped the
  applied-hash, and printed "Applied box config". Every `pip`/`cargo`/`npm`
  package, mount, volume, and env var added through `lager box config` was
  quietly dropped on any box whose last provisioning step was an install.
  `/etc/lager` is now `33:<box-user-group>` mode `2775` (setgid), so the
  container (owner) and `start_box.sh` (group) can both write it.

- **`/etc/lager` is no longer world-writable.** `lager update` granted the box
  user write access by running `chmod 777` on the directory, which also gave it
  to every other local account — enough to replace `box_config.json`,
  `saved_nets.json`, or the org secrets. It now gets the same owner/group/setgid
  treatment as above, which is what the two writers actually need and nothing
  more. This is also why the breakage above was invisible for so long: an
  updated box ended up world-writable and rendered fine, while a freshly
  *installed* box was owner-only and silently applied nothing — the two paths
  disagreed.

- **A box-config render failure is now loud, and no longer poisons the retry.**
  A failed render printed a one-line warning and a raw Python traceback. It now
  reports which file could not be written, why, and how to repair it.
  `start_box.sh` exits `3` — a new code meaning "the container is up, but the
  config on disk was not applied to it" — and `apply` neither stamps the
  applied-hash nor rolls back on it. Not stamping the hash is the load-bearing
  half: it previously did, so the retry after a fix short-circuited on "Config
  unchanged since last apply; skipping restart" and the config could never be
  applied. Rolling back is wrong here because the container is healthy and the
  fault is environmental — restoring the previous config and re-bouncing would
  fail identically. A genuine bounce failure (container possibly down) still
  rolls back exactly as before. A failed in-container `pip`/`cargo`/`npm`
  install now exits `3` for the same reason — those steps run *after*
  `docker run`, so they already reported "container is up but ... may be
  incomplete" while exiting `1` and triggering a rollback of a healthy
  container.

- **An uncaught exception in a script run inside the box container no longer
  buries the real error.** `lager_excepthook` read `LAGER_HOST_MODULE_FOLDER`
  with `os.environ[...]` in order to rewrite host paths out of tracebacks. That
  variable is set by `lager python`; anything else that execs a script into the
  container leaves it unset, so the exception *handler* raised `KeyError` —
  Python then printed "Error in sys.excepthook:" followed by a traceback from
  inside lager, and only after that the user's actual exception. A one-line
  `ModuleNotFoundError` arrived buried under a stack trace pointing at lager
  internals. With no host paths to rewrite, the traceback is now printed
  unchanged.
## [0.31.10] - 2026-07-13

### Added
- **`lager battery models` lists the battery models saved on the instrument** —
  occupied memory slots plus the firmware built-ins, all valid inputs to the
  existing `model` command. Read-only, and also exposed in the battery TUI and
  the box's `/battery/command` endpoint (`list_models`).
- **8 more Logitech webcams are detected and usable as webcam nets**: Logi 4K
  Pro, BRIO 4K Stream, C925e, C922 Pro, C920, C615, C270, and StreamCam join
  the existing BRIO/BRIO HD/C930e. Camera detection is now catalog-driven
  (adding a model is a table entry, not code) and maps each camera to its
  actual /dev/video capture node via sysfs, so mixed setups with different
  per-camera node counts resolve correctly.

### Fixed
- **Battery model readback reports the actual loaded model.** The driver read
  `:BATT:STAT?`, which reports charge/discharge status rather than the model,
  so `model`, `state`, and the TUI always showed "DISCHARGE" and successful
  loads raised a false "slot is empty" error. Readback and verification now
  use `:BATT:MOD:RCL?`, which also reliably detects the 2281S's silent
  empty-slot recall failures.
- **Built-in battery models are loadable over SCPI.** The 2281S rejects the
  manual's hyphenated names (`LI-ION4_2`) with a syntax error; the driver now
  sends the underscore spellings the firmware accepts and takes either form
  as input.
- **Battery driver errors are no longer masked as "[Errno 16] Resource busy".**
  The box endpoint returned driver errors as 5xx, tripping the CLI's legacy
  direct-USB fallback; they now come back as ordinary command failures so the
  real message is shown.

## [0.31.9] - 2026-07-13

### Fixed
- **UART reconnect can no longer land on a look-alike adapter with a clone
  serial.** Many USB-serial adapters ship with a non-unique programmed serial
  (e.g. several CP210x units all reading "0001"). If such a device dropped
  mid-session, the v0.31.5 reconnect could match a sibling adapter with the
  same serial while the real device was still off the bus — attaching the
  session to the wrong hardware. Identity resolution now treats a serial
  shared by multiple live devices as untrusted (the physical port must match,
  and it keeps retrying until the real device returns), and new identity
  snapshots record a bus-duplicated serial as null so the net is pinned to
  its physical port outright. Nets on clone-serial adapters that were
  enriched under v0.31.5 pick up the corrected snapshot on their next
  re-save.

## [0.31.8] - 2026-07-13

`lager uninstall` now actually removes what the modern `lager install` creates.
The `--all` cleanup had not kept pace with several releases of install changes,
and on boxes without broad passwordless sudo every privileged removal failed
silently while reporting "done".

### Fixed
- **`lager uninstall --all` removes the artifacts today's install creates.** The old
  udev glob (`lager-*.rules`) never matched the shipped `99-instrument.rules`, and
  the usbtmc modprobe blacklist, the `lager-box-config` sudoers file, the firewall
  helper script, the lager sysctl config, and the `lager` group were never removed
  at all. The removal list is now a single spec shared by the confirmation listing,
  `--dry-run`, the removal session, and the unit tests, so it cannot silently drift
  from what install creates again. Deliberately left in place: docker itself
  (packages, buildx, the daemon.json DNS entry) and pip/apt packages.
- **Privileged removals actually happen (and report honestly) on boxes without
  passwordless sudo.** Each sudo step used to run over BatchMode SSH with `|| true`,
  so on such boxes every one failed silently and printed "done" — a plain uninstall
  left `/etc/lager` behind while claiming success. All privileged steps now run in
  one interactive session (at most one sudo password prompt) with per-step
  OK/FAILED results, and failures are summarized at the end instead of hidden.
- **`--all` removes this machine's key from the box's `authorized_keys`.** The
  "deploy keys" cleanup only deleted box-side private keys that modern installs
  never create, while the actual access grant survived. The key is matched exactly
  by the local `~/.ssh/lager_box.pub` blob (falling back to the default key
  comment), and the next SSH connection needing a password is called out.
- **`--keep-config` is honored together with `--all`.** Previously `--all` deleted
  `/etc/lager` even when `--keep-config` asked to preserve saved nets.
- **`--dry-run` inspects the real artifact list** (correct udev filenames, modprobe
  blacklist, both sudoers files, sysctl config, firewall script, `lager` group,
  authorized_keys state) and no longer reports `/etc/lager` as "(not found)" on
  boxes where reading it via `sudo` under BatchMode fails.

## [0.31.7] - 2026-07-13

`lager install` can no longer leave a box with a Docker daemon that will not start,
and it stops rather than deploying into one that isn't running.

### Fixed
- **`lager install` no longer writes a DNS server Docker cannot parse into
  `/etc/docker/daemon.json`.** The "Configuring Docker DNS" step reads the box's
  upstream resolvers from systemd-resolved and merges them into `daemon.json`,
  but excluded only the `127.0.0.53` stub — every other value was trusted to be a
  usable IP. On a network that advertises DNS over IPv6 router advertisement,
  systemd-resolved records a link-local resolver carrying a zone id
  (`fe80::1%3`). Docker validates each `dns` entry as a bare IP address and
  **refuses to start** when one does not parse — it does not skip the entry or fall
  back to the valid resolvers beside it. Because `daemon.json` is persistent, the
  daemon then stayed down across reboots, and re-running the installer rewrote the
  file each time, undoing any manual repair. Resolvers are now validated before
  they are written: link-local, loopback, unspecified, multicast and unparseable
  values are dropped (a link-local address is unreachable from a container's
  network namespace regardless), and anything dropped is named in the install log.
- **A failed Docker DNS change is rolled back instead of left in place.** Pointing
  Docker at the box's resolvers is an optimization; it must not be able to leave the
  box worse off. `daemon.json` is now backed up before the change, and if Docker
  will not come back with the new config, the previous file is restored and Docker
  restarted on it. A box that cannot be improved is no longer a box that gets broken.
- **The installer stops when the box's Docker daemon is down, instead of running six
  more steps against it.** Every Docker command in the container step is
  best-effort (`|| true`), so a dead daemon left no trace until `start_box.sh` failed
  on `docker network create` with a bare "Cannot connect to the Docker daemon" —
  far from whatever actually stopped it, and pointing at the wrong thing. The step
  now checks the daemon first and fails with the commands needed to diagnose it.
  Likewise, a DNS step that fails now reports that the box kept its previous Docker
  configuration and that the install is continuing, rather than a bare warning.

### Changed
- The deployment script's Docker DNS logic moved out of an inline heredoc into
  `cli/deployment/scripts/configure_docker_dns.{sh,py}`, shipped to the box like
  `secure_box_firewall.sh` already is, and is now covered by unit tests.
- The install step counter reports `[N/8]`; there were eight steps and a total of
  seven, so the last one printed as `[8/7]`.

## [0.31.6] - 2026-07-13

Help pages now share one usage grammar across every net-style command, and the box
lock endpoint accepts reservation holder types from any service without
misclassifying them.

### Changed
- **Every net-style help page shows the same usage shape: positionals first, then
  `--box [BOX_NAME]`.** Click's stock leaf usage (`lager uart [OPTIONS] [NET_NAME]
  [ACTION]`) read backwards next to the net-group usage lines and the examples each
  page prints. Standalone net commands (`uart`, `adc`, `gpi`, `gpo`, `dac`,
  `thermocouple`), every subcommand of the net groups (`supply`, `scope`, `i2c`,
  `spi`, `debug`, `usb`, `nets`, ...), and the box-scoped `hello`/`instruments` now
  all render `lager <cmd> [NET_NAME] ... --box [BOX_NAME]`. `[OPTIONS]` is omitted —
  the Options section below the usage line lists every flag — and subcommands added
  to a net group inherit the format automatically.
- **`lager uart`'s `serial-port` action is documented and validated.** The bare
  `[ACTION]` metavar (whose only value is `serial-port`, printing the `/dev` path
  backing the net) no longer clutters the usage line; the help body documents it,
  and an invalid action fails with a proper Click error naming the valid value.
- **Box lock holder types are open-ended.** The box's lock endpoint whitelisted
  `holder_type` values and silently reclassified anything unrecognized as an
  auto-expiring `ephemeral` lock — which would give a reservation from a newer or
  third-party service a TTL and let the reaper drop it behind the holder's back. Any
  non-empty `holder_type` is now preserved verbatim; only `ephemeral`/`ci` keep
  auto-lock re-acquire semantics. `lager boxes` likewise recognizes any
  `<origin>:<id>:<email>` reservation string and displays just the email.
- Comments, docs, and historical changelog entries use neutral
  control-plane/web-dashboard terminology throughout.

## [0.31.5] - 2026-07-10

UART nets survive USB re-enumeration: live sessions heal in place when a device
re-enumerates (hub power-cycle, DUT reflash, replug), saved nets resolve by a
durable USB identity instead of a raw `/dev/tty*` number, and `lager nets`
shows where each UART device actually is right now.

### Fixed
- **UART nets survive USB re-enumeration.** When a UART adapter re-enumerated
  mid-session (hub power-cycle, DUT reflash, accidental replug), the box kept a
  stale open file descriptor on the vanished tty — which killed the stream AND
  pinned the old `/dev/ttyUSB*` number so the device came back under a new one —
  and the "session already active" guards then refused clean reconnects until
  the socket dropped. The box now closes the port and releases the session the
  moment a read fails, classifies device-gone errors (busy/locked ports are
  deliberately excluded), and transparently re-resolves and reopens the adapter
  with backoff (up to 60s): by USB serial when the adapter has one, otherwise by
  vendor/product + physical USB port + interface — so serial-less adapters
  (Prolific PL2303, FTDI chips with no programmed serial) and multi-port chips
  (FT4232H channels) heal in place. Applies to `lager uart` websocket sessions,
  the HTTP stream endpoint, and on-box monitor modes. The CLI shows
  `[reconnecting...]` / `[reconnected]` notices during the gap; older CLIs
  simply resume streaming.
- **New UART nets are saved with a durable USB identity.** Creating or re-saving
  a UART net (TUI or `lager nets add`) now records a `usb_identity` snapshot of
  the adapter alongside the existing `pin`, so the net keeps resolving across
  replugs and reboots even when it was created from a raw `/dev/ttyUSB*` path.
  Existing saved nets are untouched and keep working exactly as before —
  re-save a net once to upgrade it. `lager python` scripts using
  `UARTNet.get_path()` also stop returning a cached path whose node no longer
  exists.
- **`lager nets` shows where a UART device actually is.** The Channel column
  now displays the node the device owns right now (resolved live from its
  durable identity), so it stays truthful after a re-enumeration shuffles tty
  numbers instead of showing the stale stored path; unplugged devices are
  marked `(disconnected)`. The stored record is never modified by listing.

## [0.31.4] - 2026-07-10

LabJack I2C fixes: the requested bus frequency is now actually applied (previously every
request silently ran at maximum speed), and a bus wedged by a slave holding SDA low
(LabJack error 2720, `I2C_BUS_BUSY`) now recovers automatically — during both normal
transactions and address scans.

### Fixed
- **LabJack I2C nets honor the requested bus frequency.** The LabJack's
  `I2C_SPEED_THROTTLE` register counts *down* from 65536 toward slower speeds, but the
  old conversion assumed the opposite scale, produced invalid register values, and had
  been papered over by clamping every request to maximum speed (~450 kHz) — so
  `frequency_hz` in a net's params or `i2c.config(frequency_hz=...)` was silently
  ignored. The throttle is now computed correctly from the requested frequency, clamped
  to the firmware floor, and degrades to maximum speed only if the firmware rejects the
  value. Verified against the LabJack Modbus register map across the full range.
- **LabJack I2C auto-recovers from a wedged bus (error 2720).** A slave whose internal
  bus timeout fires mid-transaction — e.g. at very slow clock speeds — can hold SDA low,
  failing every subsequent transaction with `I2C_BUS_BUSY` until the box was power-cycled.
  Transactions now retry once with the firmware's bus-reset option enabled, clearing the
  stuck slave transparently.
- **LabJack I2C scan no longer returns empty on a wedged bus.** The address sweep
  swallowed per-probe errors, so a bus stuck in `BUS_BUSY` made every probe fail silently
  and the scan reported no devices. The sweep now enables the firmware bus reset as soon
  as one probe reports `BUS_BUSY` and keeps it on for the remainder of the sweep.

## [0.31.3] - 2026-07-10

`lager install` now reliably provisions instrument access on fresh boxes: udev
rules and the usbtmc blacklist install from the box's own checkout (so a
pip-installed CLI no longer silently skips them), and the box-config sudoers
rule names the box's actual login user instead of a hardcoded `lagerdata`.

### Fixed
- **`lager install` deploys instrument udev rules from every CLI install method.**
  The rules were copied from the host's repo checkout, which only exists for
  editable/source installs — a pip-installed `lager-cli` (the common case) has no
  `box/` directory, so installs completed with a scroll-by warning and fresh boxes
  came up with no instrument udev rules or usbtmc blacklist. Both now install from
  the box's own sparse checkout (`~/box/udev_rules`, `~/box/modprobe_d`) at exactly
  the deployed version, the `lager` group is created if missing, a failed deploy
  aborts the install instead of warning, and post-deployment verification checks
  the rules, group, and blacklist explicitly.
- **Fresh-box installs no longer fail at container start with "permission denied ...
  docker.sock".** When the install itself installs docker and adds the login user to
  the docker group, the group only takes effect on a new SSH login — but the whole
  run multiplexes over one master connection established before the change, so the
  container steps failed and the operator had to re-run the entire install. The
  script now detects the stale session, cycles the SSH master connection (no extra
  password prompt — the key is already installed by then), and continues. The docker
  install itself is also hardened for boxes where docker was ever removed: a stale
  docker.socket unit left loaded in systemd made the reinstalled service fail with
  "Device or resource busy"; the install now runs `systemctl daemon-reload` and
  restarts the socket before the daemon, recovers a dead daemon during the
  docker-access check, and if docker still isn't usable fails right there with
  diagnostics instead of dying later at the container step.
- **Box-config passwordless sudo works on boxes whose login user isn't `lagerdata`.**
  The `/etc/sudoers.d/lager-box-config` rule written by `lager install`/`lager update`
  hardcoded the `lagerdata` username, so on boxes with a different login user the
  grant never matched — install ended with "Sudoers file installed
  but `sudo -n apt-get` still fails" and `lager box config apply` required manual
  setup. The rule now names the box's actual login user (validated before being
  interpolated into sudoers content), already-provisioned wrong-user boxes re-bootstrap
  automatically on their next `lager update`, and the manual-fix snippets shown on
  failure name the right user too.
- **SSH key setup is no longer silently skipped for clients with connection
  multiplexing.** The install's "Passwordless SSH already configured" test used
  BatchMode, which blocks password prompts but still rides a live ControlMaster
  connection (the user's own, or one left by the CLI's password-authenticated
  connectivity check) — so the test false-positived, the key and SSH config entry
  were never installed, and every later BatchMode operation (`lager update`, box
  probes) failed with "Permission denied". The test now forces a genuinely fresh
  connection (`ControlPath=none`).
- **The end-of-install sudo prompt no longer times out on a slow (or absent) operator.**
  The sudoers bootstrap ran under a 120-second subprocess timeout that included the
  time a human takes to type the box's sudo password — at the end of a 15+ minute
  install, stepping away meant the bootstrap was killed mid-prompt and the box was
  left needing manual sudoers setup. Install now checks first whether the grant is
  already live and skips the prompt entirely (re-installs never ask), and genuine
  first-time bootstraps get a 10-minute window; `lager update`'s privileged session
  gets the same treatment.

## [0.31.2] - 2026-07-08

USB hubs are no longer pinned open by the Lager Box server: every YKUSH/Acroname
hub operation now opens a fresh handle, operates, and releases it under a per-hub
lock, so `lager python` scripts can drive USB nets without `OSError: open failed`.
Also includes driver and setup robustness fixes surfaced while bringing the shared
hardware bench under continuous integration.

### Fixed
- **USB-hub nets work from `lager python` scripts instead of raising `OSError: open failed`.**
  libusb access to a Yepkit YKUSH or Acroname hub is exclusive, and the drivers cached
  the open handle indefinitely — so after the first `lager usb` command the long-lived
  box server pinned the hub, every separate process (each `lager python` script runs in
  its own subprocess) failed to open it, and only a container restart recovered. Each
  operation is now a fresh open → operate → release cycle, serialized within and across
  processes by a per-hub lock; different hubs never block each other. Note: the
  per-operation reconnect adds roughly 2 seconds to each Acroname operation (YKUSH is
  unaffected at ~0.1 s); a follow-up may restore a short-lived cached session if this
  matters for your workflow.
- **Keithley 2281S measurement parsing.** Current/voltage reads that come back as
  multi-field or unit-suffixed responses are now parsed robustly instead of
  raising or returning an incorrect value.
- **VISA-resource net mapping.** Nets backed by a VISA resource now resolve to
  the correct instrument backend, fixing misrouted access to VISA-connected
  supplies/meters.
- **Windows-safe `lager update`.** SSH-key setup and the update flow no longer
  crash on Windows hosts (broadened error handling around `ssh-copy-id` and the
  container update steps).

## [0.31.1] - 2026-07-06

Power-supply and battery-simulator state reads are now fault-tolerant: the `/supply/command` and `/battery/command` HTTP endpoints always return a structured `state` object, and a single failing instrument query degrades that one field instead of dropping the whole readout. Also adds opt-in MCP box-control and command-execution tools for automated recovery workflows, and fixes a J-Link flash failure after a probe power-cycle mid-session.

### Added
- **Structured `state` object from the supply/battery HTTP command endpoints.** The `state` action on `/supply/command` and `/battery/command` now returns the same structured dict the WebSocket monitors emit, so HTTP-only clients can render a live readout by polling. The supply endpoint also gains `clear_ocp`/`clear_ovp` actions, matching the WebSocket handler and the battery endpoint.
- **Opt-in MCP box-control and command-execution tools.** The on-box MCP server (read-only by default) can now expose gated tools for probe/net status checks, USB-hub power-cycling, and command execution, enabling automated recovery workflows. These stay disabled unless explicitly enabled on the box.

### Fixed
- **One failing instrument query no longer blanks the whole supply/battery readout.** Every field in the monitor-state gather is guarded individually, so an unsupported SCPI query, a measurement overflow, or a transient bus error degrades that field to `null` instead of aborting the gather. The monitors report a clear error — and the TUI keeps its last good display — only when the instrument is entirely unreachable, and a power-cycled instrument still triggers stale-session recovery.
- **Flashing recovers after a debug probe power-cycles mid-session.** A J-Link GDB server left defunct by a flash that ran while the probe was down was previously treated as still running, so reconnects reused the dead server and the next flash failed. Zombie server processes are now detected and a clean server is restarted automatically.

## [0.31.0] - 2026-07-02

Richer Joulescope power measurement plus more robust box setup. The watt meter now reads current and voltage — not just power — with SI-scaled output so a small load no longer rounds to `0.000 W`; `--duration` averages over any window (gaplessly on the JS220 via its on-device accumulator); `--json` makes readings scriptable; and `lager energy` reads close the device cleanly on exit. On the setup side, `lager box dut edit`/`add-doc` now work against a Lager Box's www-data-owned `/etc/lager`, and `lager install` is more robust — it asks for the box password once, deploys its udev/modprobe rules correctly, and no longer clobbers the installed `lager` while flattening box code.

### Added
- **`lager watt <net> current|voltage|all`** — read current (A), voltage (V), or all three (current/voltage/power) from a watt-meter net, not just power. Backed by the Joulescope JS220 and Nordic PPK2; a Yocto-Watt (power only) reports a clear "not supported" message.
- **`--duration` averaging window on watt reads.** Average over a longer capture (e.g. `--duration 1.0`) for a lower-noise, higher-effective-resolution reading. On the Joulescope JS220, long windows (e.g. `--duration 60`) are measured gaplessly via the on-device charge accumulator (average current = Δcharge ÷ Δt) — constant memory, captures every transient, and scales to arbitrarily long windows.
- **`--json` output for `lager watt`.** Emit a machine-readable JSON object in base SI units (W/A/V) for HIL scripts.
- **`lager nets add` now accepts Joulescope JS220, Nordic PPK2, and Yocto-Watt.** These watt-meter/energy-analyzer instruments were missing from the CLI's instrument table, so creating a `watt-meter` or `energy-analyzer` net previously required the Workbench UI; they can now be added from the command line like any other instrument.

### Changed
- **`lager watt` output is SI-scaled.** Sub-milliwatt/-milliamp readings now display in µ/n units (e.g. `52.340 µW`) instead of rounding to `0.000 W`. Values too small for the nano prefix fall back to scientific notation (e.g. `3.000e-13 W`) rather than rounding to zero.
- **`lager install` prompts for the box password at most once.** SSH key setup now runs first, so the remaining install steps authenticate by key instead of re-prompting for the box password on each one (previously up to ~10 prompts on a fresh box).

### Fixed
- **`lager energy` reads no longer hang or crash on exit.** The reader now closes the Joulescope device when it finishes, so its USB streaming thread is torn down cleanly instead of leaving the process to hang (or segfault) after printing correct output.
- **`lager box dut edit` and `dut add-doc` work on a Lager Box's www-data-owned `/etc/lager`.** The CLI stages the updated `bench.json` in `/tmp` and installs it via a passwordless `sudo -n /bin/cp`/`chmod` fallback when the unprivileged move is denied, instead of failing with a bare "Permission denied"; the SSH banner is stripped from output and a clear message (with the exact sudoers snippet to add) is shown when the sudo grant is missing.
- **`lager install` deploys its udev and modprobe rules again.** An off-by-one in the script's source path (`../../box` → `../../../box`) resolved the rules directory to a nonexistent location, so the udev/modprobe install was silently skipped and instrument USB permissions were never applied.
- **`lager install` no longer clobbers the installed `lager` command while flattening box code.** The flatten step (`mv box/* .`) could die trying to overwrite `./lager`; it is now overwrite-safe.

## [0.30.0] - 2026-06-30

Adds first-class support for the SEGGER J-Link Base Compact and makes the bundled J-Link udev rule match by vendor ID, so every J-Link variant is granted device access and auto-detected instead of only three hard-coded product IDs. Also fixes a CLI scanner bug that silently dropped the standard J-Link.

### Added
- **J-Link Base Compact (`1366:1020`) is auto-detected as a debug net.** The box USB scanner now recognizes the Base Compact's product ID, so it scans, nets, and drives exactly like any other J-Link.

### Fixed
- **The shipped udev rules grant `GROUP="lager"` to every SEGGER J-Link by vendor ID.** Previously only PIDs `0x1024 / 0x0101 / 0x0503` were allow-listed, so a J-Link enumerating under any other PID (e.g. the Base Compact's `0x1020`) kept kernel-default `root:root` ownership and was unusable from the box user and inside the container — silently degrading debug/flash. The rule now matches `idVendor==0x1366` (all PIDs), mirroring the Acroname-hub rule in the same file.
- **The CLI USB scanner no longer drops the standard J-Link (`0x1024`).** A duplicate `"J-Link"` dictionary key in `query_instruments.py` silently overwrote the `0x1024` entry; the Base Compact now has its own key so both probes resolve.

## [0.29.0] - 2026-06-29

USB control gets multi-hub support and read-only state queries, `lager ssh` can run a one-off command on the box, and the Keithley 2281S gains two-quadrant (battery-sim) coverage. Power-supply/battery monitors and USB hubs now self-heal after a power-cycle instead of wedging. **Breaking:** `lager boxes add` now requires `--user`.

### Added
- **`lager usb <net> state`** — a read-only command that reports a USB net's current port state without changing it.
- **`lager ssh --box <box> -- <cmd>`** runs a single command on the box and returns its output, like `ssh user@host <cmd>`, instead of only opening an interactive shell.
- **Keithley 2281S battery-sim ESR setter.** Set the simulated internal resistance in battery-simulator mode via `:BATT:SIM:RES:OFFSet`.
- **Keithley 2281S signed sink-current read (two-quadrant).** Charger/sink testing now reads back negative current correctly instead of reporting 0.

### Changed
- **`lager boxes add` now requires `--user` (breaking).** The implicit `lagerdata` default has been removed; you must specify the box's login user explicitly.
- **`lager usb <net> toggle` reports the resulting state.** Toggling a port now prints whether it ended up on or off.

### Fixed
- **Acroname multi-hub boxes bind each net to its own hub by serial.** A box with more than one Acroname hub no longer addresses the wrong hub; each USB net is matched to its hub by serial number.
- **YKUSH recovers from a stale/transient handle** and self-restarts the hardware service on a power-cycle instead of failing until manual intervention.
- **The Keithley battery/supply TUI monitor self-heals after a power-cycle.** A non-intrusive liveness probe plus a sysfs-gated hardware-service self-restart (via the new shared `lager.util.self_restart`) recover a stale VISA session automatically.
- **`box_http_server` self-restarts to recover a wedged USB hub.**

### Docs
- Documented the DP711 crossover-cable requirement; added a `devenv` reference page and a J-Link section to `diagnose`; fixed stale `debug`/`boxes`/`pip` docs; removed dead pages (`pip`, `web-apps`, `docker-helper`).

## [0.28.5] - 2026-06-24

`lager update` brings up a brand-new box reliably. The BuildKit work in 0.28.4 made the box image require the Docker `buildx` plugin, which a stock `docker.io` install (e.g. Ubuntu) doesn't bundle — so a fresh box failed mid-build with a confusing "buildx component is missing or broken". Update now catches that up front, provisioning installs buildx, and a stale SSH control socket no longer masquerades as an auth failure.

### Fixed
- **`lager update` fails fast with a clear fix when the box lacks buildx.** The BuildKit preflight previously accepted any Docker >= 18.9 by version alone, so a modern `docker.io` box with no `buildx` plugin sailed through and then died minutes later in the build. The preflight now requires `buildx` to actually be present on Docker >= 23 (where `docker build` delegates to it) and points at the exact install command, instead of dead-ending in the build output. It also runs *before* the box's container is stopped, so a box that can't build the image is never taken offline by a doomed update.
- **A stale SSH control socket no longer breaks the update probe.** A control-master socket orphaned by an earlier interrupted run was silently reused by connection multiplexing, and the inherited broken state surfaced as "Permission denied (publickey,password)" on the state probe — even though the key authenticated fine on a fresh connection. Update now tears down any leftover master for the box before the first probe.
- **`lager update`'s SSH calls accept a first-seen host key.** The probe/build SSH calls ran in `BatchMode` without `StrictHostKeyChecking=accept-new` (unlike the key-setup phase), so a box not yet in `known_hosts` — or an environment where `known_hosts` can't be persisted — failed with "Host key verification failed". They now match the setup phase and auto-trust a first-seen key.
- **`lager update` no longer falsely re-prompts "SSH key not configured" on slow-connecting boxes.** The first key-auth probe used a 5-second connect timeout, which a cold Tailscale/VPN first hop can exceed — so an already-authorized box timed out, was treated as not set up, and re-ran the key-copy prompt on every update. The probe's connect timeout is now 15 seconds.

### Changed
- **Box provisioning installs the buildx plugin.** `setup_and_deploy_box.sh` now installs `docker-buildx` (falling back to `docker-buildx-plugin`, then to the official static buildx binary if neither distro package yields a working plugin) so a freshly provisioned box is BuildKit-ready and never reaches the update preflight error.

## [0.28.4] - 2026-06-22

`lager update` is faster and smoother: box image rebuilds reuse a build cache (a cold rebuild drops from ~20 minutes to a few minutes), a repeat update with nothing to change finishes in about a second instead of rebuilding, and an update asks for the sudo password at most once.

### Changed
- **Box image rebuilds are much faster.** The box Dockerfile now uses BuildKit cache mounts for the Rust (`defmt-print`) and pip layers, so a from-scratch rebuild reuses already-downloaded packages and compiled artifacts instead of redoing them — a cold, cache-invalidating rebuild drops from roughly 20 minutes to a few minutes, and a warm rebuild is seconds. `defmt-print` is now pinned to 1.1.0 so an unrelated upstream release can't silently trigger a multi-minute recompile. `lager update` also checks up front that the box's Docker supports BuildKit and fails fast with an upgrade hint if it doesn't.

### Fixed
- **A repeat `lager update` no longer rebuilds when nothing changed.** The box records a hash of its build inputs to decide whether a rebuild is needed, but that record was owned by the container user and the update (running as the login user) couldn't overwrite it — so it went stale and every update rebuilt the image and restarted the container (~30s). The record is now written reliably, so an unchanged box finishes in about a second.
- **`lager update` now asks for the sudo password at most once.** The udev, modprobe, sudoers, and box-config setup steps each used to open their own privileged session, so a box needing several of them could prompt for the password multiple times in a single update. They now run together in one session — at most one prompt, and none at all on a fully provisioned box.

## [0.28.3] - 2026-06-18

`lager diagnose` now covers debug nets. Pointed at a J-Link / debug net it localizes the fault across the whole probe stack and prints the specific fix, instead of dead-ending at "only covers USB-TMC instruments today".

### Added
- **`lager diagnose <debug-net>` diagnoses J-Link probes.** For a `debug` net it runs J-Link-aware checks via a new box endpoint and classifies the fault: probe not enumerated on USB, J-Link software missing on the box, probe claimed by another process or firmware-wedged, a wedged gdbserver, target unpowered (J-Link reports target voltage too low), target locked (readout/IDCODE/AP protection), wrong device/MCU name on the net, or no SWD/JTAG comms to a powered target — each with the concrete next action. When a debug session (gdbserver) is already running for the probe, diagnose reports from its log instead of disturbing the live session. OpenOCD/ST-Link debug nets get basic coverage (probe enumeration + gdbserver state). The USB-TMC path (power supplies, DMMs, scopes) is unchanged.

## [0.28.2] - 2026-06-17

`lager devenv` can now remember your container setup, and memory reads on DA1469x chips are fixed.

### Added
- **Save your devenv container setup in the project.** Container settings now live in the project's `.lager` file instead of needing to be retyped or kept in shell aliases. Use `devenv set`/`unset`/`show` for basic settings (image, shell, user, group, ports, and more), `devenv mount add`/`remove`/`list` for folders to share into the container, and `devenv env set`/`unset`/`list` for environment variables. `devenv terminal` and `lager exec` use these automatically, so they travel with the repo.
- **Add settings for a single run.** `devenv terminal` and `lager exec` take `-v HOST:CONTAINER` to share a folder; `devenv terminal` also takes `-e FOO=BAR` to set a variable and `--passenv NAME` to forward one from your shell. Paths can use `~` and `${PROJECT_ROOT}`, so saved settings work on any machine.
- **Preview a session without launching it.** `devenv terminal --info` prints the exact `docker` command it would run, then exits.
- **`lager debug memrd --no-reset`** skips the reset-and-halt before a DA1469x read — useful on a blank chip where you don't want to reboot it.

### Changed
- **More predictable devenv settings.** When a setting is given both on the command line and in `.lager`, the command line now wins. The container entrypoint can also be saved in `.lager`.

### Fixed
- **Reading memory from a running DA1469x now works.** Live firmware turns off the debug port, so reads used to fail. The box now resets and halts the chip first. This reboots the device under test — pass `--no-reset` to skip it.
- **Some memory reads returned wrong values** on certain chip registers; they now read correctly.
- **`devenv terminal --group` now works** — the group setting was being ignored before.

## [0.28.1] - 2026-06-15

Warm-path Workbench support for bus and energy instruments, plus UART hardening on the box.

### Added
- **`spi`, `i2c`, and `energy-analyzer` nets are served by the box's `/net/command` endpoint.** The web dashboard drives these roles, which previously round-tripped through the `/python` executor — paying an interpreter spawn + device re-open on every command. They now run in the long-lived box HTTP server like the existing gpio/adc/dac/eload/thermocouple/watt-meter roles, matching the dashboard's actions and params exactly (spi `transfer`/`read`, i2c `scan`/`read`/`write`/`transfer`, energy-analyzer `read_stats`/`read_energy`). Message formats match the previous `/python` fallback so the dashboard log is identical on either path, and energy-analyzer durations are clamped to 0.1–30s. Each net's transactions are serialized per netname so concurrent requests can't interleave on one device. Rollout is back-compatible: a box without this build returns 501 and the control plane falls back to `/python`, with no control-plane deploy needed.
- **`simple-websocket` added to the box image** so Flask-SocketIO (threading mode) can serve the WebSocket transport instead of long-polling only. Clients negotiate transport automatically.

### Fixed
- **`lager ssh` now offers the `lager authorize` key (`~/.ssh/lager_box`).** The key isn't one of ssh's default identity filenames, so `lager ssh` ran bare `ssh user@ip` and authorized boxes still dropped to a password prompt. It now passes `-i ~/.ssh/lager_box` when the key exists (mirroring the other SSH paths); without `IdentitiesOnly`, default keys and the password fallback still work for unauthorized boxes.
- **UART devices are opened exclusively and arbitrated against double-use.** A second opener — another dashboard socket.io session, or `lager uart` from the CLI while a Workbench session is live — now fails fast with a clear "device in use" message instead of silently interleaving reads on the same `/dev/tty*`. `start_uart` rejects a second session for a net (or for a different net mapping to the same device) before opening, and an exclusive-open failure is reported readably rather than as a raw errno.

## [0.28.0] - 2026-06-13

Two themes: `lager python` (and the box-mutating admin commands) now reserve the box automatically — with CI-aware identity, heartbeat keepalive, and TTL reap of stale locks — and the supply/battery TUIs work reliably on slow or mode-shared instruments (Keithley 2281S), with honest diagnostics when they can't.

### Added
- **Automatic box locking around `lager python`.** Every run acquires the box lock at start and releases it on exit, Ctrl+C, crash, or kill — with a server-side TTL + heartbeat reap as the backstop for hard kills. Holder identity is CI-aware (`ci:github:<repo>#<run>-<attempt>/<job>@<runner>:<pid>` and equivalents for Drone/GitLab/Bitbucket/Jenkins), so parallel CI matrix jobs queue against a shared box instead of colliding: collisions fail fast on dev machines (<1s) and wait up to `LAGER_LOCK_WAIT` in CI. `--detach` takes an eternal lock released via `lager boxes unlock`. `lager install`, `uninstall`, `update`, and `install-wheel` hold the lock across their destructive sections so a concurrent test is never killed mid-run. Escape hatches: `LAGER_AUTO_LOCK_DISABLE`, `LAGER_LOCK_WAIT`, `LAGER_LOCK_TTL`, `LAGER_LOCK_HEARTBEAT`, `LAGER_LOCK_HOLDER`.
- **Explicit reservations are inviolable.** `lager boxes lock` reservations are never reclassified, given a TTL, or released by any auto-locking command — on new or old box servers. (Both box HTTP servers now share one lock state machine, `lock_state.py`, with atomic file I/O; previously the two ports had divergent implementations.)

### Fixed
- **Supply TUI is usable on slow/mode-shared instruments (Keithley 2281S).** The box-side monitor gathered its display state via ~12 separate hardware-service calls per second, each taking the shared per-device lock — on a 2281S in battery mode this starved interactive commands into "Command timeout" and showed "Hardware service unreachable: " with no detail. Monitors (supply and battery) now gather the whole state in a single call per tick, poll adaptively (never occupying more than ~half of a slow device's time), and the Keithley supply monitor uses only non-intrusive queries, so it works regardless of the instrument's entry function.
- **WS/TUI errors say what actually failed.** Bare connection failures now name their cause (timeout vs refused vs device error) instead of ending in a colon, and a healthy box with a failed supply/battery session points at the instrument (offline/busy/slow, with the `lager instruments` and box-log checks) instead of blaming a "pre-0.20 box image".
- **`lager battery <net> tui` with a non-battery net exits 1** (was a stdout message with exit 0, invisible to scripts and CI).

### Changed
- **Dependency ceilings: `textual >= 3.2.0, < 9` and `python-socketio >= 5.10.0, < 6`.** The unpinned floors let every fresh install resolve whatever major shipped that day; textual 8.x removed APIs the TUI tests relied on, which is how TUI behavior drifted between machines and releases. Existing installs inside the range are unaffected.
- **`--force-command` remains removed** (gone since 0.13.4); the structured collision policy above replaces it, and `lager boxes unlock --force` stays the manual override.

## [0.27.1] - 2026-06-12

A quality-of-life pass: a one-command fix for SSH key authorization, untruncated `lager nets`/`lager instruments` output, and clearer `--help` usage lines.

### Added
- **`lager authorize --box [BOX]`.** Authorizes this machine's SSH key on a box in one step: it generates `~/.ssh/lager_box` if missing, copies it with `ssh-copy-id` (one box-password prompt), and verifies passwordless auth — re-running against an already-authorized box reports that and changes nothing. It replaces having to know the key path and the `ssh-copy-id` incantation by hand. The `Permission denied (publickey,password)` SSH error now points at this command, and shows the box's actual SSH user in the manual `ssh-copy-id` fallback instead of a hardcoded user.

### Fixed
- **`lager nets` and `lager instruments` no longer truncate output.** UART channel paths were cut to 10 characters (showing `/dev/ttyUS` instead of `/dev/ttyUSB0`) and the bracketed VISA/USB address to 45; both now display in full.
- **`lager box dut` and `lager box config` report SSH failures clearly.** Transport failures that previously printed a raw `SSH read failed: ...` line — or, in `box config`, were misread as a missing config snapshot — now route through the shared SSH error classifier, which names the cause and suggests `lager authorize`. This also closes a path where `lager box dut edit` could write back a `bench.json` missing its other keys after a failed read.

### Changed
- **Command `--help` usage lines read `COMMAND [OPTIONS]`** instead of the misleading `[OPTIONS] COMMAND [ARGS]...` on groups whose subcommands take no positional arguments. `lager nets` and `lager authorize` show `... --box [BOX_NAME]`, matching the net-style commands like `lager supply`.
- **SSH key provisioning is defined once.** `lager authorize` and `lager update` now share a single keypair-generation and key-probe implementation, so the key type and comment can't drift between the two paths.

## [0.27.0] - 2026-06-12

One theme: LabJack T7 i2c/spi pins are no longer hardcoded. Any DIO pin can be chosen per signal when adding a net — from the CLI or the Net TUI — and the TUI no longer freezes while talking to the box (a 0.25.0 regression).

### Added
- **Custom LabJack pin selection for i2c/spi nets.** `lager nets add` accepts `--sda/--scl` (i2c) and `--cs/--sck/--mosi/--miso` (spi); any DIO pin (FIO0-FIO7, EIO0-EIO7, CIO0-CIO3, MIO0-MIO2) or raw DIO number works, and omitting `--cs` selects 3-pin SPI with manual chip select. Custom selections persist via the net record's `params` dict — the format the box dispatchers already consume — plus a labeled `pin` summary (`SDA:EIO0 SCL:EIO1`). Accepting the defaults saves a record identical to the previous hardcoded flow. Pins already claimed by another saved LabJack net warn without blocking, matching the runtime PinRegistry policy.
- **Net TUI pin-picker dialog.** Adding a LabJack i2c/spi net opens a dialog with the historical defaults preselected (I2C: SDA=FIO4/SCL=FIO5; SPI: CS=FIO0/SCK=FIO1/MOSI=FIO2/MISO=FIO3). Duplicate pins block the save; pins claimed by saved nets warn live. `lager i2c`/`lager spi` display custom pins with their canonical names.

### Fixed
- **Net TUI buttons no longer need multiple clicks (0.25.0 regression).** Box round-trips ran synchronously inside button handlers and `on_mount`, freezing the event loop for seconds per call — worst right after launch and on Assign Device. All box I/O (assign flows, add/save, delete, rename, delete-all, edit details) now runs on worker threads with busy indicators and disabled controls while in flight, and startup no longer re-fetches data it already loaded.
- **`run_python_internal` works off the main thread.** It installed a SIGINT handler on every call, which `signal.signal()` forbids outside the main thread — every TUI worker-thread box call failed with "signal only works in main thread of the main interpreter". The handler is now only installed for interactive main-thread runs, and the TUI serializes box calls behind a lock so overlapping workers can't capture each other's output.

## [0.26.0] - 2026-06-11

Three themes: a security-hardening pass over the box services (rate-limited key authorization, persisted secrets with tight permissions, instrument device nodes scoped to a dedicated group), `lager box config` host-side operations that no longer dead-end on boxes with customer-managed SSH users (the dedicated lager_box key falls back to the user's own keys on auth failure, an unreachable box host is reported as exactly that, and the mount pre-flight runs late enough that mounts of apt-installed files work in a single `apply`), and two opt-in debug-stack additions for scripted J-Link workflows.

### Added
- **Per-connect J-Link script override — `DebugNet.connect(script=...)`.** Accepts a path on the box or a base64 blob; the bytes are copied to the shared script temp path so `flash`/`reset`/`read_memory` pick the new script up immediately. An already-running gdbserver only adopts it on relaunch (`force=True`). Invalid input is ignored and the net's saved script stays in effect. The OpenOCD backend ignores the argument.
- **Opt-in cache-coherent post-program verify for DA1469x QSPI images (experimental).** Set `LAGER_DA1469_UNCACHED_VERIFY=1` to read programmed `.bin` bytes back through the uncached QSPI mirror after a cache-controller flush: a matching image suppresses J-Link's stale-cache false "verification failed" report, a real mismatch is reported with its first differing address, and an inconclusive read-back leaves the original output untouched. `LAGER_DA1469_UNCACHED_VERIFY_BYTES` caps the compare (0 = whole file). Default off; flash output is byte-identical when unset. Experimental: unit-tested with regression parity, pending validation against live QSPI firmware.

### Security
- **`/authorize-key` is rate limited.** Bad-token attempts against the box key-authorization endpoint are limited per client IP (5 attempts per 60 s window, then HTTP 429); the window resets on a successful authorization.
- **The box web service `SECRET_KEY` persists across restarts.** Generated once and stored at `/etc/lager/secret_key` (mode 0600) instead of regenerated per boot, so sessions survive container restarts and the key never appears in process listings.
- **`org_secrets.json` is held at mode 0600.** The on-box secrets file is tightened to owner-only permissions at load time (with a warning when it had to be corrected), and the boot-time permission fix is best-effort so an unexpected owner can no longer abort container startup.
- **Instrument device nodes are scoped to a `lager` group.** The shipped udev rules grant `MODE="0660", GROUP="lager"` instead of world-writable 0666; `lager update` creates the group on the box host when missing and the container joins it via `--group-add`. User-added udev rules (`lager box config udev add`) default to the same scoping. Update a box before applying new user udev rules so the group exists.

### Fixed
- **The `~/.ssh/lager_box` key no longer locks out a user's own SSH key.** Passing `-i ~/.ssh/lager_box` replaces ssh's default identity list, so on boxes whose user was authorized via `ssh-copy-id` (customer-managed users), every `lager box config` host-side call failed with `Permission denied (publickey,password)` — even right after `ssh-copy-id` succeeded. The shared SSH runner now retries once without the key on an auth failure (and remembers the fallback per destination for the rest of the process), so default identities get their chance. Auth failure means the remote command never ran, so the retry cannot double-execute anything; timeouts and no-route errors are not retried.
- **`apply`/`mount add` no longer misreport an SSH transport failure as a host-path problem.** ssh's own exit code (255) during the mount pre-flight was read as "path missing", producing a wrong `Manual fix: sudo mkdir -p ...` and aborting the apply. An unreachable box host is now classified separately: the message names the user@ip and the actual fixes (`ssh-copy-id`, `lager update`, `--no-auto-prep`), `mount add` persists the mount (apply re-checks it), and `apply` warns and continues — could-not-verify is not verified-bad. Genuinely bad states (wrong-owner populated directory, sudo refused) still abort before the container restart.
- **A hung SSH connection no longer crashes the CLI with a traceback.** `lager box config` host-side calls (mount prep, apt, sysctl, udev, container bounce) ran ssh with a 60s timeout but never caught `subprocess.TimeoutExpired`, so a half-dead box or dropping link dumped a raw Python stack trace mid-apply. A timeout now surfaces as the same transport-failure result as any other SSH failure ("ssh timed out after Ns to user@ip") and is not retried.
- **An SSH connection that dies mid-prep is reported as an SSH failure, not a sudo failure.** The transport-failure classification only covered the first `stat` call in host-path prep; a connection dropping before the mkdir/find/chown step still produced the misleading raw-stderr message with a wrong manual fix. All prep SSH calls now classify ssh's rc 255 as `ssh_failed` with the ssh-copy-id/`lager update`/`--no-auto-prep` guidance.
- **Mount pre-flight now runs after the confirm prompt and after apt/sysctl/udev provisioning.** Previously it ran first, so a mount whose host path is installed by an apt package in the same config (e.g. `/usr/bin/dfu-util` from `dfu-util`) was seen as missing and pre-empted by a `sudo mkdir -p` directory at that path, breaking the package unpack — and the host was mutated before the operator confirmed the apply. The sudoers bootstrap snippet in prep failure messages also now names the box's actual SSH user instead of hard-coding `lagerdata`.

- **`lager debug ... gdbserver --rtt` no longer leaves the target halted on probes whose J-Link GDB server rejects non-stop mode.** The 0.24.0 all-stop fallback meant the RTT control-block RAM scan implicitly halted the core and nothing resumed it. The effective stop mode is now recorded on the controller and the core is resumed after the scan — only in the all-stop fallback; non-stop and OpenOCD paths are unchanged.
- **Leaked file handles closed in `zip_dir` and the gdb `--debugfile` read** — long-lived CLI processes no longer accumulate open descriptors from project packaging or debug-file uploads.
- **Bare `except:` clauses replaced with specific exceptions** across the CLI's debug tunnel helper, wifi scan parsing, session error handling, and download error paths — `KeyboardInterrupt`/`SystemExit` are no longer silently swallowed, and unexpected errors surface instead of being masked.

### Changed
- **`apply --skip-restart` no longer runs the mount pre-flight.** Mounts only take effect at the container restart this flag skips, and the eventual full `apply` re-checks them; running prep there mutated the host with no confirm prompt and before apt provisioning.

## [0.25.0] - 2026-06-11

One theme: instruments the box can't identify by USB enumeration become first-class. An RS-232-only bench instrument (first case: the Rigol DP711 power supply, reached through a generic Prolific USB-serial cable that enumerates as the cable, not the PSU) can now be assigned to its cable once — after which it scans, nets, and drives exactly like an auto-detected instrument.

### Added
- **`lager nets assign` — tell the box what's on the other end of a USB-serial cable.** `--list` shows assignable devices, current assignments, and unassigned cables; `lager nets assign Rigol_DP711 --serial <USB_SERIAL>` (or `--port <sysfs-path>` for serial-less clone cables) stores the assignment on the box, durable across reboots and replugs. `--baud` overrides the catalog default when the instrument's front panel differs; `--as-net [NAME]` creates the net in the same step. Assignments live in `/etc/lager/custom_devices.json`; the cable's vid/pid are captured from the live device, so assigning requires the cable plugged in, and ambiguous clone-cable serials are rejected with a pin-by-port hint.
- **Assign Device flow in the Net TUI** — the interactive twin of `nets assign`: pick a cable from the unassigned list, pick the instrument (with optional baud), and name its net in a follow-up dialog. Assignments can be removed from the same screen.
- **Rigol DP711 (DP700-series) support** — single-channel RS-232 supply driver with the DP800-compatible method surface, driven over a durable `serial://<vid>:<pid>/serial/<s>` (or `/port/<p>`) address that re-resolves to the live tty at open time, surviving tty renumbering, port moves, and replugs. Self-heals stale sessions; per-assignment baud override.
- **Generic POST `/net/command` endpoint** on the box HTTP server for Tier-1 instruments (GPIO, ADC, DAC, thermocouple, watt-meter, e-load), giving them the same warm in-process path the supply/battery endpoints use instead of a `lager python` subprocess per call. The `netCommand` capability is advertised in `/status` only when the route actually registers.

### Changed
- **Nets live and die with their assignment.** Removing (or replacing) a cable assignment deletes the saved nets bound to its address and reports them; pre-existing generic-UART nets on the cable are retired at assign time so a terminal session can never fight the instrument driver for one tty. A baud-only re-assign keeps existing nets.
- **The scanner reports assigned instruments, not their cables.** `lager instruments`, the TUI, and the box's `/instruments/list` show the catalog instrument (e.g. `Rigol_DP711`) at its durable `serial://` address; the cable's generic UART record is suppressed while assigned, and assigned ttys are excluded from the Dexarm G-code handshake probe.
- **`lager nets add`/`delete`/`add-batch` normalize the legacy role tokens** `supply` → `power-supply` and `batt` → `battery`. The short tokens were previously saved verbatim, producing nets that listed fine but could never be driven (every consumer — the instrument CLIs, the box dispatchers, `NetType.from_role` — matches the saved role string exactly). The tokens remain accepted as input aliases; `delete` reaches legacy nets saved under either spelling.

### Fixed
- **Manually-added supply/battery nets are driveable again** (see role-token normalization above) — and channel validation for supplies actually runs on `nets add` (it was silently skipped because the legacy token never matched the scanner's channel map).
- **`nets create` ghost exorcised**: the docs and four runtime error hints referenced `lager nets create`/`create-all`/`create-batch`, commands that don't exist — all renamed to the real `add`/`add-all`/`add-batch`, and the documented role vocabulary now matches what nets actually carry.
- **Backend JSON parsing tolerates doubled output for objects, not just arrays** — the duplicate-output recovery in `_parse_backend_json` misrouted doubled objects containing arrays; both CLI and TUI parsers now take the first complete JSON value via `raw_decode`.
- **DP700 driver reports a missing cable as `DeviceNotFoundError`** when unplugged mid-session, instead of a raw NoneType traceback.

## [0.24.0] - 2026-06-05

Two themes: the MCP server becomes a focused, read-only surface for *learning* a bench and its device-under-test (so an agent can author a correct `lager python` test), and the debug/RTT path becomes resilient enough to survive scripted flash → attach → reset loops without human babysitting.

### Added
- **DUT context for the MCP server — agents now understand the board, not just the bench.** New `DUTContext`, `SubSystem`, and `DocRef` schemas (`box/lager/mcp/schemas/dut.py`) capture a DUT's purpose, summary, MCU, key peripherals, and document references (schematics/datasheets by URL or synced `repo_path`). New `discover_dut()` and `cite_schematic()` tools and `lager://dut/overview.md` / `lager://dut/context` resources surface it; `discover_bench(net)` now returns the parent subsystem and relevant doc refs, and `plan_firmware_test` threads DUT context into every plan. Schematics are *referenced*, never stored — the agent fetches and analyzes them with its own tools.
- **`lager box dut show | edit | add-doc`** — author and inspect DUT context (subsystems, doc refs) stored in `/etc/lager/bench.json`. Includes a new CLI reference page (`docs/source/reference/cli/box-dut.mdx`).
- **`DebugNet.session()`** — a context manager that scopes connect-on-entry / guaranteed teardown so the safe connect/disconnect ordering is encoded once.
- **`DebugNet.rtt_defmt(elf=...)`** — opens an RTT session and pipes it through `defmt-print`, yielding decoded log lines instead of raw bytes, so on-box `lager python` tests can assert directly on defmt-encoded firmware logs. `defmt-print` is now baked into the box image.
- **MCP prompts** (`write_lager_test`, `explore_bench`, `assess_test_feasibility`) as client slash-command entry points, plus a new "AI Agents (MCP)" docs tab (server overview + DUT-context guide).
- **`tools/pdf_pages.py`** — a pymupdf-based helper that extracts text or renders PNGs for a specific 1-indexed page range, so agents render only the relevant schematic/datasheet pages rather than whole PDFs (guidance added to `lager://guide/workflow`; the box gains no PDF dependency).

### Changed
- **The MCP server is now a read-only discovery & planning surface.** Its job is to let an agent learn the bench and DUT well enough to write and run a test — execution lives in the test script run via `lager python … --box <box-ip>`. `discover_bench`/`discover_dut` echo the real address the client connected on and hand back a literal `lager python … --box <addr>` command; `discover_bench` now reports instrument channels, capabilities, firmware, and authored specs/ranges. Self-correcting affordances: an unknown net returns the available net names, and a no-match `get_test_example` returns the pattern catalog.
- **Reconnect-aware RTT + self-healing reset/read_memory on both backends.** The RTT reader (J-Link and OpenOCD) transparently re-attaches to the same RTT port after the socket drops (bounded by `reconnect_timeout`); it only ever re-attaches to an already-running server and never starts one. `DebugNet.reset`/`erase`/`read_memory` run through a backend-agnostic self-heal that reconnects only when no server is running, leaving a live server (and any attached RTT) untouched. A DA1469x guard never auto-starts an unhalted server (which would yield garbage QSPI-XIP reads).
- **Simplified agent-facing net metadata to `purpose` / `notes` / `tags`.** Replaces the overlapping `description` / `dut_connection` / `test_hints` fields; `lager nets describe` swaps `--description`/`--dut-connection`/`--hint` for `--purpose`/`--notes`/`--tag`, and the Net Manager TUI edit dialog is a clean Purpose / Notes / Tags form.
- **The MCP server auto-reloads bench config on change.** It snapshots the mtimes of `bench.json` / `saved_nets.json` / `box_id` and re-checks them per request, so edits via `lager box dut edit`/`add-doc` or `lager nets describe` are picked up on an agent's next request — no reload call or service restart needed. The loader now also warns on `subsystems[].nets` references that don't match a known net.

### Fixed
- **Debug connect no longer burns retries when the GDB remote rejects non-stop mode.** JLinkGDBServer rejects `set non-stop on`, which previously exhausted all connect retries and skipped target verification and RTT control-block auto-detection (with scary non-fatal warnings). The connect now detects the specific "does not support non-stop" rejection and retries once with a fresh all-stop controller; OpenOCD keeps non-stop, and any other target error still fails loudly.
- **`lager box dut add-doc` / `edit` now save `bench.json` without passwordless sudo.** Writes stage a temp file and `mv` it into the user-owned `/etc/lager` directory (chmod 644 for the www-data MCP container), falling back to `sudo -n mv` only for unusual ownership — fixing "sudo: a password is required" failures.
- **`lager box dut add-doc` no longer raises `KeyError` on a box whose `bench.json` has no DUT block.** The synthesized default `dut_slots` list is now attached to the payload before write-back so every return path leaves it writable.
- **`lager box dut edit` / `lager box config edit` now honor `$EDITOR`/`$VISUAL` flags.** The editor string is parsed with `shlex.split()` so configured flags (e.g. `subl -w`, `code -w`, `vim -p`) no longer resolve to a program named literally `"subl -w"` and fail with `FileNotFoundError`.

### Removed
- **The MCP server no longer exposes hardware I/O or mutation tools.** **BREAKING:** removed `quick_io`, `install_dependency`, `run_python`, and the `pip`/`logs`/`defaults`/`binaries` tools, the safety/preflight engine, the `run_lager` CLI passthrough, the audit subsystem, and the unused session stub. Per-bench safety constraints become advisory metadata (surfaced in `discover_bench`, not enforced). Agents now execute tests via `lager python path/to/test.py --box <box-ip>`, not over MCP.

## [0.23.0] - 2026-06-04

Self-service box provisioning: users can now grant USB device access by adding their own udev rules through `lager box config`, and there are first-class commands for erasing the config and getting a fresh container — no engineer-cut release required for a new device.

### Added
- **`lager box config udev add/list/remove` — user-editable host udev rules.** Grant a USB device read/write access from inside the container by vid:pid, e.g. `lager box config udev add 1209:0001 --box <BOX>`, then `lager box config apply`. This fixes the common case where a device node is owned by root so tools like `dfu-util` fail with "No DFU capable USB device available" (exit 74). Pass `--usbtmc` for SCPI/USBTMC instruments to also emit the driver-unbind rule (needed for PyVISA/libusb). Rules are stored in `box_config.json` (`udev_rules`) and installed host-side on `apply` to `/etc/udev/rules.d/99-lager-user.rules` with a `udevadm` reload+trigger, reusing the box's existing passwordless-sudo udev grant. Previously every new device required a Lager engineer to edit `box/udev_rules/99-instrument.rules` and cut a release.
- **`lager box config reset` — erase the box config to empty.** A single command that clears the config to an empty state (unlike `init`, which seeds the default `box-tools` volume). Pass `--apply` to also restart the container, so you get an erased config *and* a fresh container in one command — handy as a clean slate before a test run.
- **`lager box config restart` — restart the container without changing config.** A fresh container with the same config, useful for per-test isolation when you want a clean container between test runs.

## [0.22.2] - 2026-06-03

A multi-channel power-supply fix: commands now always act on the channel the net selects.

### Fixed
- **Multi-output power supplies (Keysight E363xx, Rigol DP800 series) now route every command to the selected channel.** `hardware_service` caches one driver instance per device address (so all of a supply's channels share a single USB/pyvisa session), but that shared instance stayed bound to whichever channel first opened it. Channel-less net operations — `voltage`/`current`/`enable`/`disable`/`state` — were therefore applied to that first channel instead of the one the command targeted. Most visibly on the **Keysight E36312A**, a voltage setpoint above 6V on CH2/CH3 (25V channels) was rejected because the write actually landed on CH1 (6V max), even though the limit check passed. The dispatcher now re-points the shared instance at the requesting net's channel before each call, under the per-address lock, via a new `set_active_channel()` hook on the affected drivers.

## [0.22.1] - 2026-06-02

Documentation and metadata cleanup: standardized how Lager Boxes are referred to across the tree.

### Changed
- **Standardized Lager Box references to placeholders across all in-repo text.** `--help` output, command docstrings, source comments, the CHANGELOG, release notes, and documentation now use the placeholder `<BOX>` (and `<box-ip>`) for box names and addresses; test fixtures use a neutral `test-box` token. Documentation/metadata only — no functional or API changes.

## [0.22.0] - 2026-06-01

This release makes release **tags** the single source of truth for pinning a box to a version. `lager update`/`lager install` now resolve a release-number pin to the matching `vX.Y.Z` tag instead of a same-named git branch, so the per-release version branches are no longer needed.

### Changed
- **`lager update --version X.Y.Z` / `lager install --version X.Y.Z` now resolve to the release tag `vX.Y.Z`.** Previously a bare release number was fetched as a git *branch* (`origin/X.Y.Z`), which required publishing a per-release branch next to every tag. A semver pin — with or without a leading `v`, including common pre-release suffixes (`-rc1`, `-beta2`, `-alpha`, `-preview`) — now resolves to the tag. Branch targets (`main`, `staging`, feature branches) are unchanged and still resolve to `origin/<name>`. This is backward compatible: existing `--version X.Y.Z` pins keep working, now via the tag. (`resolve_version_ref` in `cli/commands/utility/update.py`, mirrored in `cli/deployment/scripts/setup_and_deploy_box.sh`.)

### Fixed
- **Tag pins now fetch reliably on boxes that don't already have the tag.** A tag is fetched with an explicit refspec (`refs/tags/<tag>:refs/tags/<tag>`) so it becomes a local ref; `git fetch origin <tag>` alone only sets `FETCH_HEAD`, which previously left `lager update --check` reporting "update state unknown" and could block the checkout.

### Deprecated
- **Per-release version branches (`X.Y.Z`) are deprecated.** Releases no longer create them (removed from `RELEASE_PROCESS.md`); use the `vX.Y.Z` tag to pin. Existing version branches are recreatable from their tag if ever needed.

## [0.21.3] - 2026-05-29

This patch completes the 0.21.2 fix. That release stopped `lager nets tui` from fighting the `lager.pause()` stdin watcher, but the same root cause still degraded every other in-process caller that captures script output — most visibly `lager supply tui` and `lager battery tui`.

### Fixed
- **`lager supply tui`, `lager battery tui`, and net confirm prompts no longer drop keystrokes.** The 0.21.0 `lager.pause()` feature starts a daemon thread in `run_python_internal` that reads `stdin` (so Enter resumes a paused script). It only skipped that thread when the caller passed `watch_stdin_resume=False`, which 0.21.2 wired into `net_tui.py` only. Every other in-process caller that captures output via `redirect_stdout` — `cli/core/net_helpers.py:run_net_py` (behind `lager supply/battery/arm` and the measurement commands' net validation), plus the `_run_net_py` helpers in `webcam.py` and `debug/commands.py` — still leaked a daemon `stdin` reader on each call. For the power TUIs the leak came from the one pre-launch `validate_net` call, whose reader then raced Textual for the whole session; for `lager supply`/`battery`/`arm` it raced the immediately-following `click.confirm`, intermittently swallowing the first `y`/Enter.
- **The watcher is now gated structurally, not just by an opt-out flag.** `run_python_internal` only starts the stdin watcher when `sys.stdout is sys.__stdout__` — i.e. stdout has not been swapped out by `redirect_stdout`. Output-capture call sites (which all redirect stdout) can therefore never leak the reader again, even if a future one forgets the flag, while genuine foreground runs — including `lager python script.py | tee`, where stdout is piped but not reassigned — keep Enter-to-resume. The three capture helpers also pass `watch_stdin_resume=False` explicitly. Covered by new gating tests in `test/unit/cli/test_python_breakpoint_session.py`.

## [0.21.2] - 2026-05-29

This patch fixes a regression introduced in 0.21.0: the interactive `lager nets tui` would drop and mishandle keystrokes.

### Fixed
- **`lager nets tui` no longer fights the breakpoint watcher for your keystrokes.** The 0.21.0 `lager.pause()` feature starts a daemon thread inside `run_python_internal` that reads `stdin` to let you press Enter to resume a paused script. But `lager nets tui` is a Textual app that calls `run_python_internal` in-process for every backend action (scan, load, save, delete), so each call leaked a stdin-reading thread that raced the TUI's own input loop — producing dropped/erratic keypresses and unresponsive rename/edit dialogs. `run_python_internal` gains a `watch_stdin_resume` flag (default `True`, so `lager python` breakpoint resume is unchanged) and the TUI now passes `watch_stdin_resume=False`. `net_tui.py` is the only Textual caller, so no other command is affected.

## [0.21.1] - 2026-05-29

This release reworks the CLI's help and error output for newcomers: every command now shows the real `lager <type> [NET_NAME] [COMMAND] --box [BOX_NAME]` usage pattern with copy-pasteable examples, and the most common failures now print a clear problem-and-fix message instead of a raw Python traceback.

### Added
- **Actionable error messages.** Introduced a structured `LagerError` (problem + cause + suggested fixes) in `cli/errors.py`, with classifiers for connection failures, SSH/auth errors, USB-TMC system errnos (16/19/110), box-not-found, and net-not-specified. A top-level funnel in `main()` replaces raw Python tracebacks with friendly guidance; the full traceback is still available via `--debug` / `LAGER_DEBUG=1`. Wired into the highest-impact new-user paths: box selection, connection failures, bad `.lager` config, SSH/auth errors in `logs`/`install`, and net-not-specified in `i2c`/`spi`/`uart`. Adds `test/test_errors.py` (43 tests).

### Changed
- **Help is accurate, consistent, and scannable.** Replaced Click's misleading `[OPTIONS] COMMAND [ARGS]...` usage line with the real net pattern (`lager <type> [NET_NAME] [COMMAND] --box [BOX_NAME]`) via a shared `NetGroup`/`NetSubCommand`/`NetCommand` in `cli/core/net_group.py`, added a copy-pasteable Examples section to every net command, and now show `[NET_NAME]` consistently in every subcommand usage line. `lager --help` groups commands into categories (`SectionedGroup`) instead of a flat 40-item alphabetical list. Bracket placeholders (`[NET_NAME]`, `[COMMAND]`, `[BOX_NAME]`, `[IP_ADDRESS]`, ...) are now `[UPPER_SNAKE]` everywhere, short-helps are tidied, and `usb` is now a proper net group with `enable`/`disable`/`toggle` subcommands.

### Fixed
- **Fixed a broken `__main__` import and a wrong "defaults set" hint** surfaced while migrating the error paths.

## [0.21.0] - 2026-05-28

Interactive breakpoints for `lager python` scripts. A long-running test can now pause itself mid-run so the operator can inspect the bench with ad-hoc `lager` commands (or a live Python prompt) and then continue — the workflow customers asked for when debugging tests against a device in an unknown state. Before this, `lager python` scripts ran start-to-finish with `stdin` set to `DEVNULL` and there was no way to hold execution at a chosen point.

### Added
- **`lager.pause(label=None, *, timeout=None, interactive=False)`** — drop it anywhere in a `lager python` script and execution blocks at that line. Resume three ways: press **Enter** in the script's foreground terminal, run **`lager python --continue <id> --box <box>`** from any terminal, or let it **auto-resume after `timeout` seconds (default 300)**. A paused script holds no box-wide lock, so other `lager` commands keep working against the bench while it waits. Coordination is file-based under `/tmp/lager_processes/{id}/` (`breakpoint.json` describing the pause + a `resume` marker), polled by the script — deliberately chosen over adding a channel to the timing-sensitive `stream_process_output()` path (which has 50–120 ms UART budgets). The `id` is the existing client-generated `LAGER_PROCESS_ID`, so it is known for both foreground and detached runs.
- **`POST /python/continue` and `POST /python/breakpoint`** endpoints on the box Python service (port 5000): `continue` drops the `resume` marker for a process id (returns `{resumed: bool}`); `breakpoint` reports the current pause state. Both validate the id as a UUID.
- **`lager python --continue <id>` and `lager python --console <id>`** flags (alongside `--kill`/`--reattach`, lock-check skipped), plus a foreground stdin watcher so **Enter resumes** a paused run. New session helpers `continue_python()` / `breakpoint_status()` in `cli/context/session.py`.
- **Interactive console** via `pause(interactive=True)` + `lager python --console <id>` — a Python REPL bound to a socket (a free port in the already-exposed 8081–8090 range), seeded with the paused frame's globals + locals. Read any variable, evaluate expressions, or call the script's functions. **This is the way to inspect a device the script itself holds open** (e.g. a LabJack), since it runs *inside* the paused process. It operates on a snapshot — mutations in the console do not write back into the running script.
- **Configuration**: `timeout=` (per call) or `LAGER_BREAKPOINT_TIMEOUT` (env, set via `lager python --env`) override the auto-resume duration; `timeout=0` waits indefinitely; `LAGER_BREAKPOINTS=off|0|false` makes every `pause()` a no-op (and `pause()` outside `lager python` — no `LAGER_PROCESS_ID` — is a safe no-op).
- **Dedicated docs**: a Python API guide at `docs/source/reference/python/breakpoints.mdx` covering the API, the three resume paths, the console, configuration, and the device-ownership behavior observed during hardware validation.

### Changed
- **`PYTHONBREAKPOINT` now points at `lager.breakpoint.pause`** (was `remote_pdb.set_trace`, set in `box/lager/python/executor.py` and `box/start_box.sh`). `remote_pdb` was never installed in the box image, so the builtin `breakpoint()` raised `ImportError`; it now invokes the same interactive pause as `lager.pause()`. The dead `REMOTE_PDB_HOST` / `REMOTE_PDB_PORT` envs were dropped.
- **The breakpoint banner reports the script name you invoked** (from `LAGER_RUNNABLE`) instead of the box-side temp filename — `lager python` runs a single-file script from an opaque `tmpXXXX.py`, so the location previously read `tmpXXXX.py:NN`. Line numbers are unchanged (the temp file is a verbatim copy).

### Fixed
- **`box/lager/breakpoint.py` is now copied into the box image.** `box.Dockerfile` enumerates the top-level `lager/*.py` files by name in its `COPY`, so the new module was initially omitted and `lager/__init__.py`'s import of it failed the image build; it has been added to the manifest.
- **Console output (banner, exit message, syntax errors, tracebacks) is routed to the console socket** rather than the script's stderr, so it no longer leaks into the `lager python` terminal.

### Verified on hardware (live, not just unit-tested)
- Pause/resume via **Enter** and via `lager python --continue <id>`; **auto-resume** after the timeout.
- **Interactive console**: read the captured `readings` dict, a live in-process `read_adcs()` (succeeds where a separate `lager adc` returns `LJME_DEVICE_CURRENTLY_CLAIMED_BY_ANOTHER_PROCESS`, because the paused script owns the LabJack handle), and arbitrary Python.
- Reading `supply2` and `battery1` state from a second terminal while the script was paused.
- **Device-ownership behavior documented from this testing**: a paused script keeps every instrument it opened claimed for the duration of the pause — inspect script-held devices via `--console`, shared instruments from a second terminal; and a single process can hold only one net per physical instrument at a time (Rigol `supply2`/`supply3`; dual-role Keithley `supply1`/`battery1`).

## [0.20.1] - 2026-05-27

### Added
- **`--force` flag on `lager update`.** Bypasses the "already up to date" early-exit *and* forces a clean rebuild (wipes the cached image plus the `lager-cargo` / `lager-npm-global` volumes). The version file is written to `/etc/lager/version` *before* the container is started, so a box whose update failed at container start still reports the new version and reads as "up to date" — a normal `lager update` then refuses to act. `--force` is the recovery path for that state; the "Removing cached image…" status line now names the actual trigger (`--force` vs `build inputs changed`).

### Changed
- **`lager update` is the canonical box-update command again.** It is no longer deprecated and is the documented way to update a Lager Box. Internally both surfaces always shared one implementation; this just promotes the shorter top-level spelling and drops the deprecation notice.
- **Box updates reuse the Docker build cache instead of rebuilding from scratch every run.** The post-build cleanup changed from `docker image prune -af` (which deleted *all* unused images, including the layer cache, forcing a ~40-package pip reinstall + Rust toolchain rebuild on every update) to `docker image prune -f` (dangling only). Because `box.Dockerfile` copies source *after* the heavy apt/pip/rust/nrfutil layers, a code-only update now reuses those layers and finishes in ~30s instead of ~15min; full builds happen only when the Dockerfile or requirements actually change.
- **The container image is built once per update, not twice.** `lager update` builds the image in its own step (with full build-error reporting) and now invokes `start_box.sh` with `LAGER_SKIP_BUILD=1` so the box-side script skips its redundant second `docker build`. Standalone/deploy invocations of `start_box.sh` still build as before.

### Fixed
- **`lager update` survives a transient DNS/connection blip during `git fetch`.** The fetch step retries up to 3 times (3s/6s backoff) when the box hits a transient resolver/connection error (`Could not resolve host`, `Name or service not known`, connection timeouts) — common on boxes behind a flaky WiFi resolver. Non-transient failures (auth, missing branch) still fail fast with the original clear error.
- **Docker image builds can resolve external hosts on systemd-resolved boxes.** `cli/deployment/scripts/setup_and_deploy_box.sh` now writes `/etc/docker/daemon.json` pointing Docker's container DNS at the box's real uplink resolvers (discovered from `/run/systemd/resolve/resolv.conf`, with `1.1.1.1`/`8.8.8.8` fallbacks). Boxes whose `/etc/resolv.conf` only exposes the `127.0.0.53` stub previously left Docker falling back to `8.8.8.8` inside build containers; where that resolver was unreachable, `docker build` could not clone the GitHub-hosted pip dependencies and the image build failed mid-run — the root cause behind updates that hung for ~15 minutes and then reported a container-start timeout.

### Removed
- **`lager box update`.** Removed in favor of the canonical top-level `lager update`. Any scripts or automation calling `lager box update` should switch to `lager update`.

## [0.20.0] - 2026-05-26

This release is a direct response to the "battery net not responding" incident on 2026-05-26, where a Keithley 2281S misbehavior took ~2 hours of debugging across `lsof`, `dmesg`, bare `pyvisa` probes, and hardware-service introspection to root-cause. The biggest items below — `lager diagnose`, the `usbtmc` blacklist, automatic ENODEV recovery, and cross-process device locks — collectively eliminate the most common failure modes that drove that session, and surface the rest (e.g. wedged instrument firmware that only mains-power-cycling can fix) with a single one-line diagnosis.

### Added
- **`lager diagnose <net> --box <box> [--type <role>]` — single-shot net diagnosis.** Polls three box-side endpoints in parallel (USB enumeration + USB-TMC interface-class detection + holder detection via `/proc/*/fd/*` walk + `dmesg` + `lsmod` for usbtmc on port 9000, bare `pyvisa` `*IDN?` probe on port 9000, hardware-service in-process session cache on port 8080) and classifies the net into one actionable bucket with the next step the user should take: `HOST-SIDE: usbtmc kernel module loaded` (→ `lager box update`), `HOST-SIDE: USB device claimed by multiple processes` (→ names the PIDs), `HOST-SIDE: USB device busy` (→ `lager ssh` + `sudo lsof`), `TRANSIENT: device disappeared from USB` (→ hw service auto-recovers; if not, `docker restart lager`), `TRANSIENT: device enumerated as USB-TMC but pyvisa fresh probe couldn't reach it` (→ run any net command so hw_service caches a session, or `pkill -f box_http_server` to reset libusb state — narrow window after USB re-enumeration), `INSTRUMENT WEDGED` (→ mains-side power-cycle — the case software cannot fix), `NOT ENUMERATED` (→ power/cable/upstream-hub), `NOT USB-TMC` (LabJack/Picoscope/Acroname use vendor SDKs, not pyvisa — gated on the device's sysfs interface class so we don't misclassify a healthy USB-TMC instrument that's just briefly unreachable), or `HEALTHY` (with the IDN string). `--type` is optional and auto-detected from the box's saved nets via `NetType.from_role()`. Backwards-compatible against pre-0.20 boxes: each endpoint's 404 surfaces as "endpoint not on this box (pre-0.20 image)" and the command keeps running with the available bits.
- **`usbtmc` kernel-module blacklist shipped with the box image** at `/etc/modprobe.d/blacklist-usbtmc.conf`. Without this, the kernel auto-binds the `usbtmc` driver to USB-TMC-class instruments (Keithley 2281S, Keysight, Rigol scopes, etc.) and claims interface 0; pyvisa-py's libusb backend then can't `set_configuration()` and returns `[Errno 16] Resource busy`. The race re-arms on every module load / box reboot, so a one-shot `modprobe -r usbtmc` doesn't stick — the blacklist file is the only durable fix. Deployed by `setup_and_deploy_box.sh` (new boxes) and refreshed by `lager box update` (existing boxes), mirroring the `box/udev_rules/` shape. The deploy script also attempts `sudo modprobe -r usbtmc` after installing the file so the change takes effect without a reboot; if the module can't be unloaded because an instrument is currently in use, a "reboot recommended" notice is printed.
- **`lager update` verbose status block now includes `modprobe.d:`** alongside the existing `udev rules:` line, showing whether the source file is in sync, missing, or already current, plus whether `usbtmc` is currently loaded on the host.
- **Cross-process device locks for USB-TMC drivers** via the new `lager.util.device_lock` module (`box/lager/util/device_lock.py`). Generalizes the long-standing EA-solar/supply `DeviceLockManager` pattern (`fcntl.flock` on a lockfile under `/tmp/lager_device_locks/` keyed on VISA address) and adopts it in the Keithley battery + supply, Rigol DP800, Rigol DL3021 eload, Keysight E36000, and Rigol MSO5000 scope drivers. Held only across the `open_resource()` call itself — hardware_service serializes subsequent SCPI traffic via its in-process per-address lock — so the lock guards only against the specific failure mode where a second box-side `pyvisa` client (an ad-hoc `docker exec python3 -c "import pyvisa; ..."` debug session, a TUI launched directly on the box, an MCP tool taking a shortcut) races the hardware service for the libusb interface-0 claim. Fails open if the locking infrastructure itself errors (FS issue, perms), matching EA's long-standing behavior so a transient filesystem hiccup can't take legitimate work offline. EA drivers continue to use their pre-0.20 `/tmp/lager_ea_locks/` directory via a thin `_EaDeviceLockManager` subclass that preserves the existing exception hierarchy.
- **Version-skew warning** prints once per CLI session to stderr when the CLI's minor version is ahead of the box's minor version by one or more (same major), recommending `lager box update --box <name>`. The 2026-05-26 session started with CLI 0.19.2 talking to box 0.18.3 and the first error was opaque; this single line would have cut diagnosis time by hours. Hooked into `resolve_and_validate_box_with_name`, cached per-process by box IP so a tight command loop doesn't refetch, and fails open on any error (network timeout, JSON parse, missing `cli-version` endpoint, etc.) so a flaky network can never break a working command.
- **Actionable error messages for `[Errno 16/19/110]`** via the new `map_system_error()` / `format_system_error_for_user()` helpers in `cli/context/error_handlers.py`. Detection prefers explicit `[Errno N]` substring; falls back to message heuristics (`'resource busy'`, `'no such device'`, `'timed out'`, etc.) so wrapped exceptions still match. The three errnos map to: 16 EBUSY → "USB device busy — another process holds the libusb interface" with `Try: lager diagnose <net> --box <box>`; 19 ENODEV → "Instrument disappeared from USB (re-enumeration)" with `Hw service should auto-recover; if not: sudo docker restart lager`; 110 ETIMEDOUT → "Instrument did not respond to SCPI — firmware may be wedged" with `A mains-side power-cycle of the instrument is usually required`. Raw error remains available via `LAGER_DEBUG=1`. Wired into `cli/impl/power/battery.py` and `cli/impl/power/supply.py` — the two backends that surface the trio most often. Other backends (eload, solar, scope) keep printing raw for now; trivial follow-up to extend.
- **`lager diagnose` command-specific docs** at `docs/diagnose.md` covering the three endpoints' returned shapes, the classification decision tree, sample sessions for each classification, and the `--type` semantics.

### Fixed
- **`lager battery <net> ...` and `lager supply <net> ...` no longer return `[Errno 19] No such device` until `docker restart lager`** after a USB re-enumeration of the instrument (mains power-cycle, accidental unplug, USB hub port toggle). The hardware-service retry path was gated on `_VISA_SESSION_ERROR_KEYWORDS = ('session', 'closed', 'invalid')`, which did not match libusb's ENODEV signature — so the existing retry never fired and every subsequent `/invoke` failed against the stale file descriptor. Extended the keyword tuple with `'no such device'`, `'cannot find'`, `'errno 19'`, `'enodev'`; added an explicit `_is_enodev_error()` helper; and on ENODEV the `/invoke` retry now evicts every sibling `device_cache` entry on the same VISA address (so a Keithley 2281S supply call recovers automatically when battery just hit ENODEV — they share one physical USB device) and force-closes the shared `pyvisa` session pool entry regardless of `_SHARED_VISA_DEVICE_NAMES` membership (the cached `pyvisa` handle holds a stale fd after re-enumeration even for non-shared drivers). Plain stale-session errors keep the narrower per-caller eviction; the cascade is gated strictly behind `_is_enodev_error` so we don't over-evict on every minor `pyvisa` hiccup. Live-verified on the box's Keithley 2281S via a USB driver unbind/bind sequence.
- **`lager update` Step 5b (new) re-detects the `modprobe_d/` source dir post-pull.** The update probe runs at the start of the flow, before the `git pull`; on the very first deploy that introduces the directory (i.e. this 0.20.0 PR), the pre-pull probe correctly reports `MODPROBE_SRC_PATH` empty and the install step would short-circuit with "SKIPPED (source dir missing)" even though the dir exists post-pull. Re-detects via a fresh SSH round-trip against the canonical paths if the pre-pull probe came up empty.
- **`lager diagnose` host-side holder detection now works on the actual box image.** The original `/diagnose/usb` endpoint shelled out to `sudo lsof /dev/bus/usb/<device>` to find competing libusb claims, but neither `sudo` nor `lsof` ship in the lager container; the subprocess silently exited 127 and the endpoint always returned `lsof: []`. As a result the `HOST-SIDE: USB device claimed by multiple processes` and `HOST-SIDE: USB device busy` classifications **could never fire in production** — the very buckets `lager diagnose` was designed to surface from the 2026-05-26 incident. Replaced with a `/proc/*/fd/*` walk that reads `/proc/<pid>/comm` for the process name. No external tools, no permission gymnastics, scoped to the same container PID namespace as `box_http_server` (which is where rogue holders inside the lager container live).
- **`lager diagnose` classifier no longer misclassifies a healthy USB-TMC instrument as `NOT USB-TMC`** when pyvisa's fresh-probe path can't reach it (most common cause: a stale libusb context inside `box_http_server` after a USB re-enumeration; hw_service runs in a separate process and recovers transparently). `/diagnose/usb` now reads the device's sysfs interface descriptors (`bInterfaceClass`/`bInterfaceSubClass`) and surfaces `is_usbtmc: true` for class 0xFE / subclass 0x03 devices. The classifier disambiguates accordingly: enumerated USB-TMC + fresh-probe failure → new `TRANSIENT: device enumerated as USB-TMC but pyvisa probe couldn't reach it` bucket with a concrete recovery hint; enumerated non-USB-TMC (LabJack/Picoscope/Acroname) → existing `NOT USB-TMC` hint preserved.
- **`lager diagnose` VISA-side error mapping now catches all three libusb "device not reachable" message variants.** pyvisa-py emits `[Errno 19] No such device` (libusb's standard ENODEV after a re-enumeration), `[Errno 2] Entity not found` (authorized=0, mid-bind window, or a denied open), and `No device found.` (generic vendor-not-matched-or-stale path). All three now map to `error_class: nodev` so the classifier consistently returns `TRANSIENT` instead of falling through to `UNCLEAR`.
- **`lager diagnose` VISA section renders all five fields on endpoint-returned errors** (`idn:`, `elapsed:`, `error:`, `error_class:`, `skipped:`). The pre-fix `_print_section` helper short-circuited on any `error` key in the dict, collapsing the section to a single `error:` line and dropping the `error_class:` and `elapsed:` context the user needs to interpret the failure. Now distinguishes transport-layer errors (connect failure, HTTP 5xx) — which still short-circuit with a `transport error:` line — from endpoint-structured errors, which flow through the section's lambda renderer.
- **`lager diagnose` prints an actionable message when the box is unreachable** instead of wrapping the raw urllib3 traceback. The previous output read `Could not fetch net list from box: HTTPConnectionPool(host='<box-ip>', port=5000): Max retries exceeded with url: /nets/list (Caused by NewConnectionError("HTTPConnection(host='<box-ip>', port=5000): Failed to establish a new connection: [Errno 61] Connection refused"))`. Now reads: `Box '<BOX>' unreachable at <box-ip>:5000 (connection refused). The lager container may be stopped. Check with: lager ssh --box <BOX> -- "sudo docker ps"`. `requests.exceptions.ConnectionError` and `Timeout` are caught explicitly with tailored messages (refused vs timed out); other exceptions still fall through to the catch-all.
- **`/diagnose/visa` correctly consults hw_service's session pool across processes.** `box_http_server` (port 9000) and `hardware_service` (port 8080) are separate processes, but the original `/diagnose/visa` implementation imported `_visa_resources` from `lager.hardware_service` to check for a shared session — giving `box_http_server` its own empty copy of the dict, not hw_service's live state. The skip-if-shared-session check always returned False, the fresh probe always ran, and on a healthy box with a cached hw_service session it ALWAYS hit EBUSY at `set_configuration()` — surfacing as `HOST-SIDE: USB device busy` on every diagnose call against a perfectly healthy box. Replaced with an HTTP call to `localhost:8080/diagnose/dispatcher`, which returns the canonical live state.

### Improvements
- **TUI WebSocket-failure messages call out the specific next step instead of `WebSocket connection failed: Failed to connect to WebSocket server`.** `lager battery <net> tui` and `lager supply <net> tui` now probe `http://<box>:9000/health` on connect failure and emit one of: `Action: box is reachable on :9000 but the WebSocket handshake failed — the box may be on a pre-0.20 image; lager box update --box <name>` (200 response); `Action: services may be partially up; sudo docker restart lager` (non-200); `Action: timed out reaching <box>:9000; check Tailscale, then lager box hello` (connect-timeout); `Action: cannot reach <box>:9000 — lager container may not be running; sudo docker start lager` (connect-refused). Original WS error preserved in parentheses so debug info isn't lost. Lives in `cli/core/ws_diagnose.py` so future TUIs can reuse the same diagnostic.
- **Documented "TUIs are laptop-only"** in `box/lager/README.md`. Running TUIs directly on the box was the suspected culprit of that incident (a second `pyvisa-py` client competing with hardware-service for interface 0). The OS-level `device_lock` makes this case detect-and-fail-clean instead of silent EBUSY, but the right answer is still: always launch TUIs from the laptop CLI.
- **`lager diagnose` output labels clarified.** The header line now reads `NetType: <role>` instead of `resolved role: <role>` to align with the terminology used elsewhere in the CLI. The USB section now prints `usb-tmc class: yes/no` (newly surfaced from `/diagnose/usb`) so the user can see whether the classifier is treating the device as USB-TMC — and the existing kernel-module-status line is renamed from the ambiguous `usbtmc:` to `usbtmc kmod:` so the two related fields are visually distinct. Pre-0.20 boxes (which don't return `is_usbtmc`) render the field as `usb-tmc class: —` rather than guessing.

### Verified on hardware (live, not just unit-tested)
- Item 2 (ENODEV recovery): unbind/bind sequence on the Keithley while a state-polling loop ran from the laptop; loop kept getting 200s throughout (no `[Errno 19]` ever surfaced) and the Keithley's reported state values changed in real time, confirming hardware-service evicted and reopened transparently.
- Item 3 (cross-process lock): spawned a `multiprocessing.Process` inside the lager container that grabbed the `device_lock` for 3s, then timed a competing acquire from the parent process — bounced off `DeviceLockError` in 1.51s, exactly the configured 1.5s timeout window. Pre-0.20 this would have raced through libusb into `[Errno 16] Resource busy`.
- Item 5 (`lager diagnose`): `battery1` → HEALTHY (Keithley IDN); `supply1` → HEALTHY (same Keithley, sibling role); `adc1`/`usb1`/`scope1` (LabJack/Acroname/Picoscope) → NOT USB-TMC (with the vendor-SDK hint). `--type` explicit override matches auto-detect.
- Regression smoke: `lager adc adc1` → `-10.603 V`; `lager gpi gpio1` → `HIGH (1)`. The PR1 lock changes do not affect non-pyvisa drivers (LabJack uses LJM, Picoscope uses Pico SDK, neither goes through `device_lock`).
## [0.19.2] - 2026-05-25

### Changed
- **`--ip` now accepts DNS hostnames in addition to IP addresses** on `lager boxes add`, `lager boxes edit`, `lager install`, and `lager uninstall`. Lets a Lager Box sit behind a DNS name (e.g. `box.example.com`) or a Tailscale MagicDNS short name (e.g. `box-1.tailXYZ.ts.net`) instead of requiring the operator to look up and pin a numeric address. Validation is purely syntactic — IPv4/IPv6 (incl. Tailscale `100.x.x.x`) take the existing `ipaddress.ip_address` fast path; everything else is checked against RFC 1123 hostname rules (1–63 char alphanumeric/hyphen labels, ≤253 chars total, single-label allowed for MagicDNS), with actual resolution deferred to SSH/HTTP. The shared validator lives in the new `cli/address_utils.py` (covered by 34 unit tests in `test/unit/cli/test_address_utils.py`); the four call sites all share one error path that prints a "Valid formats:" cheatsheet on failure (`install` / `uninstall` previously printed only the bare error). Inputs that already carry a scheme, port, or path (e.g. `http://...`, `host:5000`, `host/api`) are rejected with a specific message instead of the previous generic "not a valid IP" — the rest of the CLI composes `http://{addr}:port/...` itself, so an embedded one of those would conflict.

## [0.19.1] - 2026-05-25

### Fixed
- **`lager debug ... flash` and the DA1469x flash loader now quote the firmware path before handing it to OpenOCD.** OpenOCD parses its TCL commands word-by-word, so a `program` or `load_image` argument with a space in it was being chopped into two TCL words and the underlying flash op either failed loudly with `wrong # args` or hit the wrong file. In practice the path comes from `tempfile.NamedTemporaryFile()` or the fixed `~/third_party/customer-binaries/openocd/flash-loaders/da1469x/` tree (no spaces), so the bug never bit in normal operation; the fix is defensive and aligns `box/lager/debug/openocd.py`'s `OpenOcdRpc.program()` and `OpenOcdRpc.load_image()` with the existing quoting pattern in `OpenOcdRpc.rtt_setup()`. Notable for operators who relocate the flash-loader tree via `LAGER_FLASH_LOADERS_DIR=/path/with spaces/`.
- **DA1469x flash_loader ELF parser now reports a clear error on a truncated symbol-table name instead of a Python `ValueError` traceback.** `box/lager/debug/da1469x_loader.py`'s ELF32 symbol walker used `bytes.index(b'\x00', ...)` to locate the null terminator for each name in the string table, which raised an unwrapped `ValueError` if the strtab itself was truncated. Switched to `bytes.find()` with an explicit error message that names the offending offset; `_resolve_loader_symbols()` still rewraps it as `Da1469xLoaderError` so the call site error type is unchanged.

## [0.19.0] - 2026-05-23

### Added
- **OpenOCD debug backend.** Non-SEGGER debug probes are now first-class peers of J-Link under `lager debug` — same `connect` / `gdbserver` / `flash` / `erase` / `reset` / `memrd` / RTT surface, same multi-probe slot allocator, same TUI. The box-side dispatcher in `box/lager/debug/probes.py:resolve_backend` routes by USB VID extracted from the debug net's VISA address; the auto-mapped OpenOCD probes are SEGGER-adjacent in scope: ST-Link V2 / V2-1 / V3 (`0483`), Raspberry Pi Debug Probe (RP2040 Picoprobe / CMSIS-DAP, `2e8a`), FTDI FT232H (`0403:6014` → `c232hm.cfg`) and FT2232H (`0403:6010` → `olimex-arm-usb-ocd-h.cfg`), ARM DAPLink / NXP MK20 CMSIS-DAP (`0d28`), Atmel EDBG/mEDBG (`03eb`), and Olimex ARM-USB-OCD-H (`15ba`). Anything else stays on J-Link to preserve existing-net behavior. The OpenOCD daemon, TCL/RPC client, and command implementations live in the new `box/lager/debug/openocd.py` (~1000 lines); `service.py`'s `handle_*` paths fan out to the right backend per request. Per-probe slot stride mirrors the J-Link layout so OpenOCD nets can run concurrently with J-Link nets on the same Lager Box.
- **DA1469x flash via the Apache Mynewt RAM-resident flash_loader (OpenOCD path).** Mainline OpenOCD has no QSPI flash driver for the Dialog/Renesas DA1469x family, so `program ... 0x16000000 verify reset` cannot touch external NOR — `lager debug SWD flash` against an FT4232H rig silently did nothing despite a green `Flashed!` log line. `box/lager/debug/da1469x_loader.py` ports the upstream GDB-script protocol (`flash.gdb` / `erase.gdb` / `flash_loader.gdb`) to pure OpenOCD TCL/RPC: brings the loader up in RAM at the convention path `/home/www-data/customer-binaries/openocd/flash-loaders/da1469x/flash_loader.elf{,.bin}` (override via `LAGER_FLASH_LOADERS_DIR`), seeds MSP/PC, disables QSPIC/MTB/MPU, sets a hardware breakpoint on `mynewt_main`, waits for `fl_state == 1`, then drives the `fl_cmd` / `fl_cmd_rc` / `fl_cmd_data` command struct in chunks and software-resets on success. Also includes an inline ELF32 symbol-table reader so the box doesn't need `pyelftools`. Wired into `service.py`'s `handle_flash` / `handle_erase` OpenOCD branches, gated on `'DA1469' in device_type.upper()`. The two loader artefacts ship under `~/third_party/customer-binaries/openocd/flash-loaders/da1469x/` (operator drops them in once per box; `start_box.sh` `mkdir -p`s the subtree on every container start so they survive `lager update`); a missing pair raises an actionable error pointing at the expected paths.
- **Concurrent multi-probe slots extended to OpenOCD.** `box/lager/debug/probes.py` adds OpenOCD telnet (`4444 + slot`) and OpenOCD TCL/RPC (`6666 + slot`) ports to the existing per-slot window, alongside the J-Link GDB stride (`2331 + 3·slot`) and shared RTT base (`9090 + 2·slot`). Slot 0 is still the legacy single-probe path (GDB 2331, RTT 9090, OpenOCD telnet 4444, OpenOCD TCL 6666); legacy nets without a parseable serial keep landing on slot 0. `start_box.sh` publishes `4444-4447` and `6666-6669` via `docker run -p`; `secure_box_firewall.sh`'s `LAGER_PORTS` admits the same windows so hardened boxes don't silently drop OpenOCD telnet/TCL traffic. The in-box `DebugNet` Python API now allocates probes through the same shared `NetsCache` slot pool as the HTTP debug service (`service._resolve_probe`), so a `Net.get(name, NetType.Debug).connect()` call inside a `lager python` script gets a distinct port window per probe instead of pinning every concurrent script to slot 0.
- **`--openocd-config` on `lager nets add` / `add-batch`.** Parallels the existing `--jlink-script` flag — the user's `.cfg` rides on the saved net (base64-encoded) and is materialised to `/tmp/lager_openocd_user.cfg` in the box-side `_build_openocd_command` before each `openocd` spawn. Required for FT4232H, which has no auto-detected interface cfg (the chip exposes four channels and Lager can't guess which one carries SWD without the user telling us); supported on every other adapter as an escape hatch for vendor-supplied configs.
- **Backend-agnostic `lager nets set-script` / `show-script` / `remove-script`.** Replaces the script-routing surface with a single trio that detects the target backend from the probe VID + the file's extension/content sniff and routes to `jlink_script` or `openocd_config` accordingly. Refuses with a clear "pass `--backend jlink|openocd`" message when probe and file signals disagree (instead of silently guessing); enforces mutual exclusivity on every write so a debug net carries either field but never both (the other is cleared with a yellow stderr notice). `SCRIPT_PATH='-'` reads from stdin. Legacy `--backend X` short-circuit is preserved for CI and scripts that already know which slot they want.
- **OpenOCD speed fallback ladder for `DebugNet.connect()`.** `connect_jlink` already walked `[requested, 4000, 1000, 500, 100]` kHz; OpenOCD's `adapter speed` is set once at daemon startup with no built-in retry, so a vendor cfg expecting 500 kHz against Lager's 4 MHz default would die silently at the first SWD transaction. The same ladder is now applied at the `DebugNet` layer for the OpenOCD branch, exposed as the pure helper `openocd_speed_ladder(requested)` (covered by 7 unit tests).
- **`--jlink-version` flag on `cli/deployment/scripts/setup_and_deploy_box.sh`.** Pin the on-box JLink/JLinkExe version at deploy time instead of taking whatever SEGGER ships at the moment of the box build; matches the `--lager-version` flag's shape. Deployment options table in `docs/reference/deployment/README.md` corrected to match the current flag set.
- **Docs: ST-Link, RP2040, and FTDI listed under Debug & Flashing.** `docs/source/reference/instruments/supported-instruments.mdx` calls out the OpenOCD-detected probes alongside the existing J-Link entries; `docs/source/reference/cli/nets.mdx` documents `--openocd-config`, the unified `set-script`/`show-script`/`remove-script` trio, and the OpenOCD RTT `chunk_size` knob.

### Changed
- **`DebugNet.connect()` is symmetric across backends.** OpenOCD now honors `force=False` (restart via the daemon's built-in `stop_openocd`) and `ignore_if_connected=False` (returns the existing `status()` instead of raising) with the same semantics as J-Link's `JLinkAlreadyRunningError` path. Neither flag with a running daemon raises `RuntimeError`.
- **`DebugNet.status()` always returns `{running, pid, backend, ...}`** regardless of which backend the probe routes to. Backend-specific extras (J-Link's `cmdline`, OpenOCD's daemon log path) pass through unchanged, but consumers writing portable code can now rely on the three guaranteed keys. Previously OpenOCD returned `{running, pid}` while J-Link returned a wider dict that could hand back either a `jlink_status` or `gdbserver_status` shape.
- **OpenOCD gdb / telnet / TCL ports bind to `0.0.0.0`.** OpenOCD ≥ 0.11 defaults `bindto` to `127.0.0.1`, so `docker run -p 2331-2342:2331-2342` was forwarding traffic to a listener that wasn't accepting it and off-box GDB clients timed out without an error. Now matches J-Link's all-interfaces default. TCL/RPC remains 127.0.0.1-only on the wire because the box-side service drives it locally; only when explicitly remapped does it open up.
- **Custom OpenOCD configs now load before adapter-dependent `-c` commands.** Previously Lager emitted `-c "adapter serial <s>"` and `-c "transport select swd"` *before* `-f <user.cfg>`, but those `-c` commands require an adapter driver that only gets set inside the user's cfg — OpenOCD bailed with "adapter driver is not configured" before the cfg ever loaded. The user cfg now occupies the same slot in the command line that the auto-detected `interface/*.cfg` would, and the auto `transport select` is suppressed when a user cfg is supplied (vendor cfgs almost always call it themselves at the top, and OpenOCD errors on a duplicate set).
- **Dropped the short-lived `set-openocd-config` / `show-openocd-config` / `remove-openocd-config` trio.** Now that `set-script` is backend-agnostic, the OpenOCD-specific aliases looked like a special escape hatch that J-Link doesn't need — exactly the asymmetry we wanted to avoid. They never shipped in a tagged release, so there's no installed CLI surface to break; existing scripts/CI calls migrate to `set-script --backend openocd ...`. The error-message hint in `box/lager/debug/openocd.py`'s "can't infer interface" path now points at the canonical `set-script` command too.
- **OpenOCD FTDI dispatch keys on VID/PID, not just VID.** The original "VID `0403` → some FTDI cfg" mapping fell over the moment a box had both an FT232H and an FT2232H plugged in. Now: FT232H (`0403:6014`) → `c232hm.cfg`, FT2232H (`0403:6010`) → `olimex-arm-usb-ocd-h.cfg`, FT4232H → no auto-config (`openocd_config` required, with an actionable error when missing). `_build_openocd_command` skips the auto-detected interface cfg whenever a `user_config_path` is supplied, since the two `.cfg` files would otherwise collide on adapter driver / `layout_init`. The unreachable `0x1209` (BlackMagic et al.) pseudo-mapping is dropped; users with open-hw probes whose VIDs aren't auto-mapped can still use the OpenOCD backend by setting `debug_backend: openocd` and supplying `openocd_config`.

### Fixed
- **DA1469x flash hung at chunk 4 (`0x18000`) on one box.** Two bugs in the freshly-ported pure-RPC loader path that didn't show up against the GDB-script reference flow:
  - `_fl_program` was reading `fl_cmd_rc` mid-loop the moment `fl_cmd` returned to 0. The upstream `fl_program` macro deliberately doesn't — `rc` is only checked once, after the post-loop `while fl_state != 1` poll, because the loader uses `fl_cmd_rc` as scratch during a chunk. An eager mid-loop read caught a transient address-shaped value (observed `rc=0x66A4E0`) before the loader restored `rc` to 1, raising a spurious "program failed" mid-flash.
  - `fl_cmd_data` is a *pointer var* that the upstream `apps/flash_loader` toggles between halves of a malloc'd buffer on every `LOAD_VERIFY` (`fl_rotate_databuf()`). The pure-RPC path was passing the static ELF symbol of the pointer var to `load_image`, dumping each chunk on top of loader BSS and corrupting `fl_cmd_data` itself. Chunks 0/1 limped through; chunk @ `0x10000`'s first 4 bytes formed an unmappable pointer and bus-faulted the M33, leaving `fl_cmd` latched at 5. Now reads the pointer with `mdw` before each chunk's `load_image`, matching the GDB-script flow. (The earlier per-chunk-unique tempfile + 50 ms inter-chunk sleep were workarounds for symptoms of this bug and were dropped.)
- **DA1469x `lager debug SWD flash --bin <file>,0x16000000` silently programmed offset 0.** `service.py:handle_flash`'s DA1469x branch was hardcoding `offset=0` and dropping the CLI's address argument. The CLI accepts absolute XIP addresses (matching the J-Link convention) but the loader's `fl_cmd_flash_addr` is flash-relative, so the new `xip_to_flash_offset` helper translates and bounds-checks against the SoC XIP window `[0x16000000, 0x18000000)` — flash-relative offsets passed in by mistake fail loudly with an actionable message instead of silently writing to the wrong chunk of NOR.
- **OpenOCD silent flash failures.** OpenOCD's TCL/RPC channel returns the `program` proc's stdout as plain text even when flash write/verify failed, so a bad flash looked successful to callers. `flash_image` / `flash_erase_all` / `flash_erase_range` now scan the response for `program_error` markers (`** Programming Failed **`, `** Verify Failed **`, etc.) and `Error:` lines and raise `OpenOcdRpcError` with the file path and raw output. Side effect: `Erase complete!` / `Flashed!` no longer print on rigs whose `target.cfg` declares no flash bank — those calls now fail fast with the underlying error.
- **In-box `DebugNet` Python API ignored user-supplied debug scripts.** `openocd_config` (base64-encoded content) was being looked up under the wrong key (`openocd_config_path`) and never decoded to disk; `jlink_script` was never forwarded to `connect_jlink`. Custom configs/scripts uploaded via `lager nets set-script` had no effect when scripts ran `Net.get(name, NetType.Debug).connect()` from within `lager python`. Both fields are now decoded to the same shared temp paths the HTTP debug service writes to (`/tmp/lager_jlink_script.JLinkScript` / `/tmp/lager_openocd_user.cfg`), so the J-Link `reset_device` / `read_memory` helpers that look the script up via `api._get_script_file()` pick it up unchanged. Explicit `*_path` fields still win when the file is already on the box.
- **`set-script` previously routed every upload to the `jlink_script` field.** OpenOCD configs uploaded via `lager nets set-script` were silently stored in the wrong slot and ignored at run time. Now detects backend from probe VID + file extension/content sniff and writes to the correct field; refuses ambiguous cases with a `--backend` hint instead of guessing.
- **Net Manager TUI's "script attached" indicator missed `openocd_config`-only nets.** `has_script` was computed from `jlink_script` alone, so debug nets carrying only an `openocd_config` (the new normal for FT4232H rigs) showed no indicator even though one was attached. Now checks both fields.
- **FTDIs without a programmed USB serial were broken end-to-end.** A FT4232H whose EEPROM was never burnt has no readable USB serial, so the box scanner emitted `USB0::0x0403::0x6011::::INSTR` (empty serial slot) and the chain fell apart in three places: (1) the static `CHANNEL_MAPS` UART fallback was `["0", "1", "2", "3"]`, those bare interface indices landed in the saved net's `pin` field via the TUI, and the box-side UART dispatcher (which reads `pin` as a USB serial) failed at first use with `UART bridge with serial 2 not found`; (2) the debug-probe regex `([^:]+)::INSTR` rejected the empty-serial address, so `resolve_backend()` silently fell back to J-Link for what was actually an OpenOCD-backed FT4232H — `lager debug gdbserver` came back as the canned "Failed to connect to debugger" checklist with no real cause; (3) `cli/commands/box/nets.py:show_cmd` labelled the overloaded `pin` field as "Channel:" regardless of role, hiding misconfigurations. Fixed end-to-end: `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py` grow a `_get_ttys_for_usb_device` helper that matches ttys by sysfs node instead of USB serial; `CHANNEL_MAPS` for FT2232H/FT4232H change from `["0","1","2","3"]` to `[]` so bare-index placeholders can never leak; the VISA regex relaxes to `([^:]*)`; `cli/commands/box/net_tui.py` gains a `_validate_uart_pin` guard that rejects the legacy `"0"/"1"/"2"/"3"` placeholders with an actionable EEPROM hint; `cli/commands/development/debug/commands.py` surfaces the box's structured `error` field on `gdbserver` connect failures instead of parsing it into a discarded local; `show_cmd` is now role-aware (`Pin/serial:` for UART, `Device:` for debug). Direct CLI paths (`lager nets add` / `add-all` / `add-batch`) remain unaffected so power users keep an escape hatch. A parity test pins `usb_scanner.py` and `query_instruments.py` so the two scanners can't silently drift on this contract again.
- **TUI Keithley net wizard let the user assign both `power-supply` and `battery` roles to the same net.** The 2281S's two entry functions are mutually exclusive in firmware (and even the v0.16.9 dual-role fix for the *separately-named* keithley supply / keithley battery nets requires distinct nets, not one net with both roles checked). The role selector in `cli/commands/box/net_tui.py` now treats `power-supply` and `battery` as mutually exclusive at the checkbox level for the Keithley device class, surfacing the constraint at create-time instead of at first SCPI command.
- **Unrelated openocd follow-ups: warn instead of silently dropping a `.lager` debug script attached to a non-debug net; dedup legacy UART nets that ended up with both a serial-keyed and a sysfs-keyed copy in `saved_nets.json`; and document the OpenOCD RTT `chunk_size` knob in `docs/source/reference/cli/nets.mdx`.**

## [0.18.5] - 2026-05-22

### Fixed
- **`/debug/erase` and `/debug/flash` started returning 500 (`ValueError: filedescriptor out of range in select()`) on long-running boxes.** The debug service is a long-lived process. `get_controller()` builds a fresh `gdb-multiarch` `GdbController` on every retry attempt — the retry loop exists for the J-Link/RTT startup-timing races that are routine during a flash/RTT session — but a *failed* attempt is never stored in `_gdb_controller_cache`, so `cleanup_controller()` could never reach it. Each failed attempt leaked the `gdb-multiarch` subprocess and the pipe fds to it. Once the debug service crossed 1024 open fds, every newly spawned `JLinkExe` child PTY landed at an fd ≥ `FD_SETSIZE` (1024), and `pexpect`'s `REPLWrapper` — used by the erase/flash `commander()` path — crashed in `select()`. (`/debug/connect` was unaffected: it spawns `JLinkGDBServer` via `subprocess`, not `pexpect`.) Failed-attempt controllers are now closed (`_discard_failed_controller`), and `commander()` spawns `JLinkExe` with `use_poll=True` so `pexpect` uses `poll()` — which has no `FD_SETSIZE` ceiling — instead of `select()`.

## [0.18.4] - 2026-05-20

### Fixed
- **`lager python` scripts no longer miss tight response deadlines under streaming back-pressure.** A running script's stdout/stderr were drained from their kernel pipes *inline* on the same generator that forwards bytes to the CLI over HTTP, so any stall on that socket (slow link, Nagle, retransmit) stopped pipe drainage. Once the 64 KiB pipe filled, the script blocked on its next `print()`. For scripts with tight timing budgets — e.g. a DA14695 ROM-bootloader handshake that must reply within 50–120 ms of each byte — this stretched response windows enough to fail ~90% of the time, even though the same script run directly on the host (no executor, no stream-forwarder) succeeded every time. Output is now drained on background threads into a bounded queue, decoupling the script from HTTP-write latency; stdout/stderr pipe buffers are enlarged to 1 MiB (`F_SETPIPE_SZ`); the interpreter runs with `-u` for guaranteed unbuffered I/O; and the unused stdin is closed (`DEVNULL`). Wire format and public API are unchanged.
- **Avoid a potential deadlock when launching a `lager python` script.** The per-script scheduling-priority boost (`os.setpriority(-10)`) was applied via a `preexec_fn`, which runs in the forked child between `fork()` and `exec()`. Python documents `preexec_fn` as unsafe in a multithreaded process — and the python execution service is a `ThreadingHTTPServer` — because the child can deadlock if another thread held an allocator/import lock at fork time. The boost is now applied from the parent on the child's PID, with identical effect and permission semantics (`CAP_SYS_NICE`) and no fork/exec window.

## [0.18.3] - 2026-05-15

### Added
- **`lager box update --version <older-ref>` rolls back.** The previous one-way `git rev-list HEAD..target --count` only counted commits the box was *behind* and treated any "ahead" state as in-sync, so downgrading a box that had pulled a newer ref required manual `git reset --hard` on the box. Now uses `git rev-list --left-right --count HEAD...target` to detect divergence in both directions; a pull fires when the box is ahead of the target as well as behind. An explicit second confirmation prompt (skippable via `--yes`) gates the destructive direction so a typo'd `--version` argument can't silently downgrade a box. `--check` reports "will roll back N commit(s) ahead of target" / "will switch (N ahead / M behind)".

### Improvements
- **Update flow batches read-only state into a single SSH probe.** Replaces ~11 individual `test`/`cat`/`git`/`diff`/`stat` round-trips (git-repo check, remote URL, layout, current commit, build-cache hashes, udev rule state, sudoers ownership, box-config sudoers state, `/etc/lager/version`) with one structured shell script that emits `LAGER_PROBE_<KEY>=<value>` lines parsed locally. Combined with merging fetch+rev-list, sparse-checkout+checkout+reset, flatten+verify, post-build directory setup, and verify+J-Link presence into single calls, a typical no-op `lager box update` goes from ~3-5s to ~1.6s.
- **Persist user-installed cargo crates and global npm packages across container recreation via Docker named volumes.** Adds `lager-cargo:/opt/rust/cargo` and `lager-npm-global:/home/www-data/.npm-global` mounts to `start_box.sh`'s `docker run`. Without these, every `lager box update` recreated the container from scratch and the post-run loops recompiled `cargo install` packages (e.g., `defmt-print`) from source, adding ~50-60s per update. With them, the second-and-onward run sees "already installed" and finishes in seconds. The CLI wipes both volumes alongside `docker rmi lager` whenever the build-hash changes, so a Dockerfile rustup/node bump can't leave a stale toolchain in the volume. Measured on one box: typical update 1:40 → 17s after the volumes seed.
- **Verbose output cleanup.** Probe results print as one tidy block instead of a dozen "Checking X... OK" lines; consistent step labels between progress bar and `--verbose`; noise lines dropped (e.g. "Checking remote URL" only prints when it actually migrates SSH→HTTPS); single label for the build step instead of two; `log_status` helper signature simplified.

### Fixed
- **Pull aborted on git ≥2.36 with `fatal: 'cli/__init__.py' is not a directory`.** Cone-mode sparse-checkout (default since git 2.36) rejects single-file patterns. The pre-batching version of the sparse-checkout add ran in a separate SSH call whose exit was never checked, so the failure was silently swallowed; the new batched pull script chained it with `&&`, which propagated the failure and aborted the whole pull. Now treats the `cli/__init__.py` add as best-effort to match the original behavior. Affects boxes running newer git (observed on one box at git 2.43.0; another at 2.34.1 was unaffected).

## [0.18.2] - 2026-05-13

### Added
- **`lager box update` — canonical update command.** Replaces the top-level `lager update` (now a hidden deprecation alias that still works for existing scripts and CI). Sits alongside `lager box config` under the `lager box` group.
- **`--check` / dry-run mode.** `lager box update --box X --check` reports the planned update without modifying the box: current vs target version, code/deps/container state, estimated duration. Exits 0 for no-op, 1 for would-update, 2 on error.
- **Auto Docker-cache invalidation.** Records sha256 of `Dockerfile` + `requirements.txt` at `/etc/lager/build-hash` after each build. The next update detects drift (Dockerfile or requirements changed) and triggers `docker rmi lager` before the rebuild, replacing the manual `--force` workflow. First-run-after-deploy bootstraps the hash silently without forcing a rebuild.
- **SSH ControlMaster multiplexing.** All update SSH calls reuse a single OpenSSH master connection via `cli/core/ssh_utils.SSHConnectionPool`. Per-command overhead ~300ms → ~10ms; consecutive no-op runs ~20s → ~1.6s.

### Changed
- **`lager update` hidden in `--help`** and prints a one-line deprecation notice on every invocation, nudging users toward `lager box update`. Same flag set, same behavior — old scripts keep working.
- **End-of-run output redesigned.** Single green summary line (`<BOX> updated to version 0.18.2 (main)` or `<BOX> is already at version 0.18.2 (main)` for no-op). The redundant Restart/Build status, the "Verify with:" hint, and the verbose Duration line are dropped — elapsed time appears on the progress bar itself.
- **Progress bar rewrite.** Bar width is computed from the live terminal columns with a 2-char right margin (was a fixed 30 chars and would wrap on 80-col terminals, producing stacked-line artifacts because `\r\033[2K` only clears the current row). Elapsed time moved to the left of the bar, padded to a fixed width. The 1-second re-render thread is gated on `sys.stdout.isatty()` so captured output (CI logs, pipes, redirects) gets one frame per step instead of dozens.

### Fixed
- **Cache-invalidation early-exit silently skipped rebuilds.** The hash mismatch check ran *after* the no-restart early-exit branch, so a corrupted `/etc/lager/build-hash` with code in sync took the no-op path and never rebuilt. Auto-invalidation now also fires on deps-only changes (Dockerfile/requirements moved, code unchanged) as intended.
- **Stale `/etc/lager/version` after early-exit.** The "already up to date" branch only updated the local `~/.lager` cache, leaving the on-box version file untouched, so the next `lager hello` would surface the stale value and users would re-run `lager update` thinking the previous one didn't take. The primary cause of the recurring "had to run `lager update` 2–3 times before it stuck" reports.
- **Post-restart `time.sleep(5)` race.** Replaced with a poll of `http://<box>:5000/health` (60s ceiling, exponential backoff). The 5s window was too short on slower boxes; subsequent commands raced against an unready service.
- **Flatten heuristic misfired on every run.** "Files at root + box/ absent" treated the post-flatten state as broken and wiped+refetched on every consecutive `lager update`, defeating the early-exit branch and forcing ~20s of pointless container churn each run.
- **Silent flatten failures producing broken images.** Verify `~/box/lager/box_http_server.py` + `box.Dockerfile` after the flatten step; abort cleanly if missing instead of building against an incomplete tree.
- **Swallowed git errors.** `git checkout` and `git reset --hard` failures used to print only "Failed to checkout version X" without git's underlying message. Now pass stderr through.
- **Flatten artifact blocked branch switch.** A prior flatten could clobber a root-level tracked file (e.g. `README.md`), making the working tree look modified to git. `git checkout -f` discards spurious modifications from flatten artifacts so the branch switch succeeds.

### Removed
- **`lager update --all`** and the multi-box loop it drove (~145 lines). Belongs in its own command if multi-box update returns as a feature.
- **`lager update --force`.** Obsoleted by auto cache-invalidation. The escape-hatch use case (force a rebuild when the hash heuristic misses something) is rare; `docker rmi lager && lager box update` is the manual workaround.
- **`lager update --skip-restart`.** Produced a half-update state ("pull code but don't restart") with no real workflow — `ssh lagerdata@<box> 'cd ~/box && git pull'` is clearer if that's what you want.

## [0.18.1] - 2026-05-13

### Fixed
- **J-Link GDB attach no longer halts the target CPU on ~15% of attaches.** Two compounding changes in `box/lager/debug/`: (1) `gdbserver.py` no longer passes `-ir` (init registers) to `JLinkGDBServer`. The `-ir` flag briefly halts the CPU to seed its register file, but Lager doesn't need that — RTT control-block initialization happens later via `SetRTTAddr` in `detect_and_configure_rtt()`. (2) `gdb.py` now puts GDB into non-stop async mode (`set pagination off`, `set target-async on`, `set non-stop on`) *before* `tar ext`. In GDB's default all-stop mode the inferior is implicitly halted for memory reads and monitor commands, which produced the residual ~15% halt rate after the `-ir` drop landed. The non-stop flag is locked once a target is attached, so the order matters. Bench-validated with no halts observed.

### Improvements
- **`lager usb enable | disable | toggle` ~2.6x faster (~3.9s saved per call on Tailscale-attached boxes).** Mirrors the supply/battery fast-path migration from 0.17.x: the CLI now POSTs to `/usb/command` on the box's port 9000 Flask server instead of uploading `cli/impl/device/usb.py` and spawning a fresh Python subprocess + brainstem/pykush imports on `:5000/python` for every call. The Acroname BrainStem singleton and YKUSH per-serial LRU already live inside the long-lived box-server process, so the speedup comes from skipping per-call subprocess+import cost. Implemented as `box/lager/http_handlers/usb.py` (new) + `register_usb_routes` wired up alongside the supply/battery registrations in `box_http_server.py`. The CLI falls back to the slow path on `ConnectionError`/`Timeout`/404, so a new CLI keeps working against older box images; real handler errors (missing net, port-state) exit fast and do *not* retry the slow path, since the slow path would just reproduce the same failure. Bench-validated on the box's Acroname 8-port over Tailscale: 6.10–6.57s (slow path via 404 fallback) → 2.18–2.66s (fast path post-deploy).

## [0.18.0] - 2026-05-12

### Added
- **`lager box config` — declarative per-box provisioning.** Replaces ad-hoc SSH-and-edit-files workflows with a single JSON manifest at `/etc/lager/box_config.json` that declares mounts, named volumes, container env vars, host apt packages, sysctl settings, in-container pip packages, cargo crates, and npm packages. `lager box config apply` reconciles a Lager Box to match. Idempotent — re-applying the same config is a no-op via SHA-256 hash comparison against the last-applied snapshot (`/etc/lager/box_config.applied_hash` + `box_config.applied.json`). Full operator command surface: `init`, `show`, `validate`, `diff`, `apply` (with `--dry-run` and `--yes`), `audit`, `status`, `edit` (opens `$EDITOR`/`nano`/`vi` and round-trips through shim validation), `copy --from --to`, `import FILE`, `export FILE`, `repair`. Multi-box fanout via `--box A,B,C` on `show` and `apply`. Every section has CRUD verbs (`mount add/remove/list`, `pip add/remove/list`, `apt add/remove/list`, `cargo add/remove/list`, `npm add/remove/list`, `sysctl set/unset/list`, `env set/unset/list`, `volume add/remove/list`).
- **npm support inside the container.** New `npm_packages` first-class field with scoped (`@types/node`) and versioned (`lodash@4.17.21`) package support. The container Dockerfile installs `nodejs npm` and sets `NPM_CONFIG_PREFIX=/home/www-data/.npm-global` (pre-created and chowned to `www-data`) so `npm install -g` works without root.
- **Rust toolchain baked into the container.** Dockerfile installs rustup into `/opt/rust` (owned by `www-data`) with `RUSTUP_HOME`, `CARGO_HOME`, and `PATH` set, so `cargo install` works from the post-bounce loop without needing the operator to install rust manually. `cargo_packages` accepts `name` or `name@version`.
- **Audit log of every config mutation.** `/etc/lager/box_config.audit.log` (JSONL, append-only) records `mount-add`, `apt-add`, `set-applied-hash`, etc. with ISO-8601 timestamps. `lager box config audit [--tail N] [--since 1h] [--verb apt-add] [--json]` host command surfaces it; filters compose for "what changed in the last hour" or "every apt operation ever."
- **Sudoers auto-bootstrap.** `lager install` (new boxes) and `lager update` (existing boxes) now install `/etc/sudoers.d/lager-box-config` with the narrow NOPASSWD grants `lager box config apply` needs (`/usr/bin/apt-get` with SETENV for DEBIAN_FRONTEND, path-scoped `tee` and `rm` for the sysctl conf, `/bin/mkdir` and `/bin/chown` for mount auto-prep, path-scoped `/bin/cp` for the rollback's snapshot restore). A marker file at `/etc/lager/.boxcfg-sudoers-v2` lets `lager update` skip re-bootstrapping when the current rule shape is already installed.
- **Automatic rollback on failed bounces.** When `lager box config apply`'s container restart fails (e.g., docker rejects a malformed mount), the previously applied snapshot is restored to `/etc/lager/box_config.json` via SSH `sudo cp` (the in-container shim is unreachable when the container is dead), sysctl is reverse-diffed back to the previous values, and a re-bounce brings the box up on the prior good config. `lager box config repair --box X` exposes the same recovery as a manual verb for situations where automatic rollback can't fire (e.g., operator hand-edited the JSON to invalid syntax outside the CLI).

### Changed
- **`lager update` container startup timeout** raised from 5 to 10 minutes (`cli/commands/utility/update.py`) so first-time docker builds with cargo + npm layers don't time out on slower boxes. Same headroom for `_bounce_container`'s SSH ceiling (300s → 900s), giving the apply path room for cargo crate compilation plus pip and npm install loops.
- **`lager box config show` reads as a tree.** Bold uppercase `HOST` / `CONTAINER` group headers with horizontal-rule underlines, bold section labels indented two spaces, and `├── /└── ` branches under each section. Mounts/volumes align around `->`; env/sysctl align around `=`; empty sections render as `(none)` leaves so operators discover what's configurable. The header carries a colored `[Up To Date]` / `[Unapplied Changes!]` marker driven by a `hash` vs `applied-hash` comparison.

### Fixed
- **SSH user resolution in `lager box config`.** `default_ssh_runner` was calling `get_box_user(box_ip)` even though that helper keys by box *name*, so every box with a stored custom SSH user silently fell back to `lagerdata`. Reverse-resolved via `get_box_name_by_ip` before the user lookup. Same runner now also uses `~/.ssh/lager_box` (the dedicated key `lager install`/`lager update` set up) via `-i`, matching the rest of the CLI's SSH conventions.
- **`DEBIAN_FRONTEND=noninteractive` silently dropped on apt-get.** Default Ubuntu sudoers' `env_reset` strips `DEBIAN_FRONTEND` set as a `sudo VAR=value cmd` argument unless `SETENV:` is granted. Packages with debconf prompts (`iptables-persistent` and friends) would hang on a prompt that never showed. The new sudoers rule grants `SETENV:` only on `/usr/bin/apt-get` so the variable propagates.
- **`cargo` not found inside the container during box-config apply.** `start_box.sh`'s cargo install loop used `bash -lc` (login shell), which re-sources `/etc/profile` and resets `PATH` — wiping the Dockerfile's `ENV PATH=/opt/rust/cargo/bin:...`. Switched to `bash -c` (non-login) so the docker ENV is honored. Same fix applied to the npm install loop.
- **Real exit codes from pip/cargo/npm install loops.** The previous `if ! cmd; then _rc=$?; ...; fi` pattern in `start_box.sh` captured `$?` *after* the `!` inversion, so `_rc` was always `0` even on real failures. Refactored to `if cmd; then : else _rc=$?; ... fi` so the script's `[ERROR] ... (rc=$_rc) for: ...` messages report accurate exit codes.
- **`lager box config edit` no longer rejects valid saves with non-zero editor exit.** Some vim plugins return `1` from `:wq` even when the save succeeded. The command now compares the tempfile contents before and after the editor exits — content changed AND non-zero exit means "user saved, proceed"; content unchanged AND non-zero means "abort." Bonus: `nano` is preferred over `vi` as the fallback editor when `$EDITOR` is unset.
- **Sudoers bootstrap detection no longer false-positive.** Previous detection used `sudo -n -l <cmd>` exit code to decide if a rule was present, but Ubuntu's default `%sudo` group grants `(ALL : ALL) ALL` (with-password) which `-l` reports as "permitted" regardless of NOPASSWD status. Replaced with a marker file at `/etc/lager/.boxcfg-sudoers-v2` written during bootstrap plus a functional `sudo -n DEBIAN_FRONTEND=... apt-get --version` probe. `lager update` now correctly re-bootstraps when the marker is missing.
- **Env values containing whitespace, `$`, backticks, or single quotes survive the bounce.** `render_docker_args.py` used to emit `--env 'KEY=hello world'` to stdout, which `start_box.sh` then interpolated unquoted into `docker run` — bash variable expansion does not re-parse quotes, so values got word-split and the literal quote characters leaked through. The renderer now writes a bash-sourceable file declaring `BOX_CONFIG_MOUNTS`, `BOX_CONFIG_ENV`, and `BOX_CONFIG_HOST_PATHS` arrays via `shlex.quote`; `start_box.sh` sources that file and uses `"${BOX_CONFIG_MOUNTS[@]}"` and `"${BOX_CONFIG_ENV[@]}"` so each element preserves its content verbatim.
- **NPM_CONFIG_PREFIX in the container's Dockerfile.** Default `npm install -g` writes to `/usr/local` (root-owned) and `~/.npm`. The container runs as `www-data` (uid 33), which has no permission for either. Pre-created `/home/www-data/.npm-global` and `/home/www-data/.npm` owned by www-data + set `NPM_CONFIG_PREFIX` and prepended `/home/www-data/.npm-global/bin` to `PATH` in the image so `npm install -g X` works non-root.

### Improvements
- **`apply` shows the pending diff inline before confirming.** When `--yes` is not passed, the confirm prompt is preceded by a per-field diff of what's about to change. Closes the most common pre-apply workflow ("run diff first, then apply") into a single command.
- **Tightened sudoers rule for apt/sysctl/apply.** `tee`, `rm`, and `sysctl --system` are path-locked to the exact files/flags `lager box config apply` invokes, so a compromised `lagerdata` account cannot escalate to root via those binaries. `apt-get` and `mkdir`/`chown` stay unscoped because the package list and host paths are user-defined.
- **flock against the in-container shim.** Two concurrent `lager box config X` invocations against the same box used to do read-modify-write on `box_config.json` and silently drop one mutation. The shim now `flock`s `/etc/lager/box_config.lock` around the whole dispatch.
- **Post-apply consistency check.** After the bounce + API-ready probe but before `set-applied-hash`, the apply path re-runs `validate` + `show` against the box. If either drifts from what was bounced (i.e., the JSON was hand-edited mid-apply), `applied-hash` is left untouched and the operator is told to re-run apply.
- **In-container shim hardening.** Dispatch table replacing a 60-line if/elif chain; a `_MIGRATIONS` scaffold ready for future schema bumps; `restore-applied` verb supports the host-side rollback path; per-mutation audit log entries.

### Internal
- Cleanup tasks B–G from `BOX_CONFIG_CLEANUP.md` all landed: redundant host-side validators deleted (validation is now box-side only), shim protocol verbs centralized in `cli/commands/box/_shim_verbs.py`, the seven `*_list_cmd` host commands collapsed into one shared helper, `_render_human` driven by a registry instead of seven copy-pasted blocks, shim dispatch consolidated, inline imports hoisted to module-top.
- Test coverage: 240+ unit tests across `test/unit/box/test_box_config.py`, `test_box_config_cli.py`, `test_render_docker_args.py`, `test_host_ops.py`, `test_mount_prep.py` covering schema validation, every CLI verb, every package-manager surface, the rollback path with snapshot existence/missing/cp-failure cases, the audit log with `--since`/`--verb` filters, the `apply` pre-confirm diff, multi-box fanout, env/sysctl/mount value-alignment in the tree renderer, and edge cases around the `assh` SSH wrapper. Verified end-to-end on a real Lager Box including the quoting regression test (env values with whitespace + `$`), the rollback path (intentional duplicate-mount-point bounce failure), and full package-manager round-trips across apt/pip/cargo/npm.

## [0.17.0] - 2026-05-05

### Added
- **Concurrent J-Link probes on a single Lager Box.** `box/lager/debug/service.py` now resolves each debug net's J-Link USB serial from its VISA address and allocates a deterministic per-probe slot (read from `saved_nets.json` via `NetsCache` in `_resolve_probe`). Slot N owns a three-port window: GDB `2331+3N`, SWO `2332+3N`, telnet `2333+3N`, plus RTT base `9090+2N`. The slot stride was widened from 1 to 3 because `JLinkGDBServer`'s default `-swoport`/`-telnetport` are `2332`/`2333` and a stride of 1 collided on those auxiliary ports; auxiliary ports are now passed explicitly so the defaults can't bite. The box service passes `-select USB=<serial>` to `JLinkGDBServer` and `-SelectEmuBySN <serial>` to `JLinkExe`, writes per-serial PID and log files, and narrows `pkill` so disconnecting probe A no longer tears down probe B. `start_box.sh` publishes the widened `2331-2342` Docker port range; `secure_box_firewall.sh` admits the same range. The CLI's `--gdb-port` default changed from `2331` to `None`, so the box's allocator is no longer clobbered on every connect; the CLI prints the effective `gdb_port` the box returned. Backwards compatible: nets without a parseable serial (legacy single probe) fall back to slot 0 / 2331 / 9090 / `/tmp/jlink_gdbserver.pid`. Includes a no-DUT smoke test (`test/unit/box/test_jlink_multi_gdbserver_select.py`) that drives `start_jlink_gdbserver` end-to-end with two distinct serials and asserts per-probe `-select USB=<sn>`, distinct ports, distinct log paths, and distinct PID files.
- **Detect the RIGOL DP811 power supply.** Both `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py` now classify USB serials starting with `DP8H` or `DP81` as `Rigol_DP811` (in addition to the existing `DP82`/`DP8G` → `Rigol_DP821` and `DP8B`/`DP83` → `Rigol_DP832` mappings). The DP811 shares VID:PID `1ab1:0e11` with the DP821/DP832, so it is added to the serial-disambiguated bucket in `_VIDPID_TO_NAME` to avoid being misclassified at scan time. `lager instruments` now lists DP811 supplies plugged into a Lager Box.
- **Multiple concurrent viewers per webcam stream.** Previously each `/stream` connection in `box/lager/automation/webcam/service.py` opened its own `cv2.VideoCapture` against `/dev/videoN`, which V4L2 serves exclusively — a second viewer either failed or got blank frames. The streamer subprocess now starts a single daemon capture thread on the first viewer that owns the device and broadcasts encoded JPEG frames to a shared buffer guarded by a `threading.Condition`. Each `/stream` handler waits on the condition for the next frame and writes it to its client, so any number of viewers can subscribe concurrently. Stop and re-start each webcam to pick up the regenerated streamer script.

### Fixed
- **`LabJackADC.input()` no longer inherits sticky AIN register state from a previous tool.** `box/lager/io/adc/labjack_t7.py:LabJackADC.input` previously called `ljm.eReadName` with zero AIN register configuration, inheriting whatever device-side state a previous tool left in `AIN_RANGE`, `AIN_NEGATIVE_CH`, `AIN_RESOLUTION_INDEX`, and `AIN_SETTLING_US`. T7 register state persists in device RAM until USB power-cycle, so if a previous tool left an AIN in differential mode with a floating negative channel, every read saturated at ~10.10 V regardless of actual signal — indistinguishable from a real wiring fault. Safe defaults (`RANGE=10.0`, `NEGATIVE_CH=199`, `RESOLUTION_INDEX=0`, `SETTLING_US=0`) are now written once per `(handle, channel)` tuple before the first `eReadName`, cached in a class-level set. Config-write failures are logged but do not raise.

### Improvements
- **Webcam capture forces MJPEG so two cameras can share a USB 2.0 bus.** Default OpenCV negotiation in `box/lager/automation/webcam/service.py` picked YUYV (uncompressed, ~150 Mbps at 640×480 30fps), which doesn't leave room for a second camera on the same bus — the kernel rejected `VIDIOC_STREAMON` with "Not enough bandwidth for altsetting". MJPEG is roughly 5× smaller and fits two cameras comfortably. The `FOURCC` is now set before width/height/fps so negotiation honors it.

## [0.16.10] - 2026-05-01

### Fixed
- **`lager debug connect` surfaces the real SEGGER error when J-Link cannot reach the target.** When J-Link's multi-speed retry loop in `box/lager/debug/api.py:connect_jlink` exhausted without ever reaching a target, `status['logfile']` could be set to `None` rather than absent, so `status.get('logfile', 'No log available')` returned `None` and the downstream `clean_logfile_content(None)` crashed with `AttributeError: 'NoneType' object has no attribute 'replace'` — masking the real SEGGER "Connecting to target failed" message that operators need to see in the dashboard. `connect_jlink` now coerces `None` to `'No log available'` at the call site, and `clean_logfile_content` returns `''` when given `None` as defense in depth.

### Internal
- Bumped seven transitive Rust dependencies in `box/oscilloscope-daemon/Cargo.lock` (`quinn-proto` → 0.11.14, `rustls-webpki` → 0.103.13, `time` → 0.3.47, `bytes` → 1.11.1, `tracing-subscriber` → 0.3.20, `rand` 0.8.6 and 0.9.4) to clear ten Dependabot security advisories on the daemon's QUIC/TLS stack. Lockfile-only change with no runtime effect on existing boxes until the daemon is rebuilt; verified with a full release build + libps2000 link on Picoscope hardware.

## [0.16.9] - 2026-04-29

### Fixed
- Resolves the **sequential half** of the Keithley 2281S dual-role known limitation from v0.16.7. When `supply1` (role `power-supply`) and `battery1` (role `battery`) are configured on the same physical Keithley 2281S, the box now opens exactly one pyvisa session per VISA address — shared by both driver classes — instead of opening two sessions and hitting `[Errno 16] Resource busy` on the second open. Scripts can alternate `lager supply <net>` and `lager battery <net>` commands against the same Keithley without restarting the box. Implemented in `box/lager/hardware_service.py` as a process-wide `_visa_resources` cache keyed by address, plus a `raw_resource=` kwarg on `box/lager/power/supply/keithley.py:create_device` and `box/lager/power/battery/keithley.py:create_device`. Both drivers track an `_owns_resource` flag so `close()` does not release the underlying USB claim when the session is shared. SCPI serialization moved to a per-address lock so supply and battery commands targeting the same Keithley serialize correctly. (Note: genuinely concurrent supply + battery operation against one Keithley is not supported by the instrument's firmware — its Power Supply and Battery Simulator entry functions are mutually exclusive — so configure one role per Keithley if you need both running at once. See Known Limitations in the release notes.) No behavior change for single-role drivers (Rigol DP821, Keysight E36xxx, EA PSB) — they continue to use the legacy per-driver-opens-its-own-session path.
- Resolves the **Keithley 2281S concurrent battery TUI + CLI known limitation** from v0.16.7. The retry path in `box/lager/hardware_service.py:/invoke` now calls `_close_device(old_device, cache_key)` *before* invoking `module.create_device(net_info)`, releasing the popped instance's USB claim so the new pyvisa session can open cleanly. Previously the popped `KeithleyBattery`/`Keithley2281S` instance stayed alive in the process and held the libusb claim, causing the recreated session's `pyvisa.ResourceManager().open_resource(addr)` to fail with `[Errno 16] Resource busy` (surfaced as `Could not open instrument at ...`). For drivers that share a pyvisa session (Keithley dual-role), the retry path also closes-and-reopens the shared resource instead of closing-and-reusing the same already-broken handle.
- **Keithley 2281S supply method-signature compatibility.** `box/lager/http_handlers/supply.py` is modeled on multi-channel drivers (Rigol DP800) and calls supply-driver methods with a `channel=` kwarg or positional channel. The Keithley 2281S supply driver follows the `SupplyNet` abstract (no `channel` parameter — the 2281S is single-channel), so the very first call into a Keithley supply hit a `TypeError` that the handler treated as hardware failure and triggered `/cache/clear`, closing the shared pyvisa session that this release's dual-role fix had just opened. `Keithley2281S.output_is_enabled` now accepts (and ignores) a `channel=None` kwarg, and six new public OCP/OVP wrapper methods (`set_overcurrent_protection_value`, `enable_overcurrent_protection`, `set_overvoltage_protection_value`, `enable_overvoltage_protection`, `clear_overcurrent_protection_trip`, `clear_overvoltage_protection_trip`) delegate to the existing private `_set_ocp` / `_set_ovp` and public `clear_ocp` / `clear_ovp` methods so the handler can call them without `AttributeError`. No new SCPI logic.
- **`lager battery <net> state` no longer falls through to a competing pyvisa session.** The battery CLI sends `action='print_state'` (the dispatcher function name), but `box/lager/http_handlers/battery.py:/battery/command` only recognized `action='state'` (matching the supply handler's name). The unrecognized action returned HTTP 400 and the CLI's `_run_backend` fell through to the python:5000 dispatcher path, which opened its own pyvisa session against the same Keithley and immediately collided with the shared session that hardware_service had just opened during the previous supply command — surfaced as `Could not open instrument at USB0::...: failed to set configuration [Errno 16] Resource busy`. `/battery/command` now accepts both `'state'` and `'print_state'`, so the CLI stays on the WebSocket → hardware_service path and reuses the shared pyvisa session this release introduces.
- **Removed the v0.16.5 `/cache/clear` band-aid from `lager python` script exit.** `cli/commands/development/python.py` previously POSTed `/cache/clear` to `hardware_service` on every script exit, Ctrl+C, and BrokenPipeError. v0.16.9 owns one persistent pyvisa session per VISA address inside hardware_service and shares it across CLI/TUI/script callers, so tearing the cache down on every script exit defeated the design and forced a re-open that often raced libusb's asynchronous release-interface (surfaced as `[Errno 16] Resource busy` on the next supply or battery command). The clears are removed; hardware_service's cache now persists for the container's lifetime as intended.
- **`hardware_service._get_or_open_visa_resource` retries on transient `Resource busy`.** When pyvisa-py + libusb returns `[Errno 16] Resource busy` on `open_resource()` — typically because a previous claim hasn't been fully released by the kernel — the open is now retried with an exponential backoff (`0.2, 0.5, 1.0, 2.0` seconds) before giving up. This makes the shared-session path resilient to libusb's async release-interface timing window without papering over genuine "device unplugged" or wiring failures.
- **`POST /cache/clear` no longer tears down shared pyvisa sessions.** The endpoint still closes cached driver wrappers and removes them from `device_cache` so a wedged driver can recover on the next `/invoke`, but the underlying per-VISA-address shared session that v0.16.9 introduced is now retained across calls — clearing it on every CLI script exit (which is what older `lager python` clients still do) defeated Phase 2's design and re-introduced the libusb release-interface race surfaced as `[Errno 16] Resource busy`. A new `POST /cache/clear_all` endpoint preserves the old behavior for the rare case (USB unplug/replug, manual debugging) where a full reset is genuinely required.
- **Cross-role concurrent use on a single Keithley 2281S now fails fast with a clear error instead of cryptic SCPI/Resource-busy traces.** The 2281S's Power Supply (`:ENTR:FUNC POW`) and Battery Simulator (`:ENTR:FUNC BATT`) entry functions are mutually exclusive in firmware — running a `lager supply <net> tui` against the same physical Keithley while simultaneously running a `lager battery <net>` command (or vice-versa) made the two clients fight over the entry function on every poll. The box now tracks the active monitoring sessions per role in `box/lager/http_handlers/state.py` (sessions store the resolved VISA address on start) and refuses an opposite-role command — both at `/supply/command` / `/battery/command` and at `start_supply_monitor` / `start_battery_monitor` — when the same address is already in active use, with a message that names the conflicting net and explains the hardware constraint. Sequential CLI cross-role workflows (which never populate the active-session dicts) are unaffected and continue to work via Phase 2's shared pyvisa session.

### Removed (was Known Limitations in v0.16.7)
- The two Keithley 2281S workarounds documented in v0.16.7 are no longer needed.

## [0.16.8] - 2026-04-28

### Added
- Recognize the SEGGER J-Link Flasher PRO (USB `1366:0105`) as a supported `debug` instrument. Added `J-Link_Flasher_Pro` to `SUPPORTED_USB` and `CHANNEL_MAPS` in both `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py`, so `lager instruments` now lists the device when it is plugged into a Lager Box.

## [0.16.7] - 2026-04-28

### Fixed
- `lager uart <net>` returned `404 — UART net not found` for every UART command, even when `lager nets` correctly listed the net. The v0.16.6 battery-handler consolidation (commit `f277402`) deleted the two-line `register_uart_routes(app)` / `register_uart_socketio(socketio)` block in `box/lager/box_http_server.py` as collateral damage. Imports stayed in place so the file still parsed; the Flask route just was never registered. Re-added the registration alongside supply and battery.
- `lager supply <net> state` (and any other one-shot supply or battery command) failed with `[Errno 16] Resource busy` immediately after exiting the TUI, succeeding only on the second invocation. Root cause: `/supply/command` and `/battery/command` returned 404 when no active WS session was found, forcing the CLI's `_run_backend` into a direct-pyvisa subprocess fallback (`cli/impl/power/supply.py` → dispatcher) that opened its own pyvisa session, conflicting with the still-cached session in `hardware_service.py`. Both endpoints now build a transient `Device` proxy via `resolve_net_proxy()` when no active WS session exists, routing through `hardware_service.py:/invoke` like the WS monitor already does. There is now exactly one pyvisa session per `(device_name, address)` regardless of TUI lifecycle. This completes v0.16.6's "VISA session ownership unified" promise.
- Concurrent TUI + CLI access on the same supply (e.g. `lager supply <net> tui` running while another terminal runs `lager supply <net> current`) no longer cascades `Resource busy` errors across subsequent commands. Previously a single transient kernel-level USB-claim collision matched the substring `'resource'` in `_is_visa_session_error()` and triggered the stale-session retry path, which popped the live cache entry and called `module.create_device()` on the same address — that second open hit `Resource busy` again because the original session was still alive in the same process, turning an isolated collision into a chain of failures. Removed `'resource'` from `_VISA_SESSION_ERROR_KEYWORDS` in `box/lager/hardware_service.py`; retry now fires only for genuine stale-session signals (`'session'`, `'closed'`, `'invalid'`). An isolated USB-busy collision is still possible on heavily-contended USB transfers but is now returned to the caller cleanly without disturbing the cache, so the next command immediately succeeds.

### Known Limitations
- Keithley 2281S configured with both a supply role (`power-supply`) and a battery role (`battery`) on the same physical USB device cannot be used concurrently (or sometimes even sequentially without restarting the box service). `box/lager/hardware_service.py` keys its driver cache by `(device_name, address)`, and the supply path uses `device_name="keithley"` while the battery path uses `device_name="keithley_battery"` — so the same USB device gets two distinct cache entries and two competing pyvisa sessions, the second of which fails with `[Errno 16] Resource busy`. Workaround: configure either the supply role or the battery role on the Keithley 2281S, not both. Proper fix (shared pyvisa Resource or merged driver class) is targeted for v0.16.8.
- Concurrent battery TUI + CLI on the Keithley 2281S can surface `[Errno 16] Resource busy`. Running `lager battery <net> tui` in one terminal while running `lager battery <net> state` (or any other one-shot battery CLI command against the same net) in another terminal can fail with `Resource busy`, even when only the battery role is configured on the Keithley (so this is distinct from the dual-role limitation above). The Bug-B retry-classification fix prevents this from cascading across subsequent commands, but does not eliminate the initial collision; the underlying contention appears to live in the Keithley pyvisa session itself rather than in `hardware_service.py`'s lock. Workaround: do not invoke battery CLI commands while a battery TUI is open against the Keithley 2281S — close the TUI first, or run TUI-only or CLI-only. Root-cause investigation tracked for v0.16.8.

## [0.16.6] - 2026-04-27

### Fixed
- `lager battery <net> tui` now works for the first time. The OLD WebSocket battery monitor in `box/lager/box_http_server.py` imported `_resolve_net_and_driver` from `lager.power.battery.dispatcher`, but the battery dispatcher (unlike the supply dispatcher) had no module-level wrapper of that name — every TUI launch crashed at module load with `ImportError: cannot import name '_resolve_net_and_driver' from 'lager.power.battery.dispatcher'`. Nobody had reported it because nobody had tested the battery TUI. Incidentally fixed by the VISA-ownership unification below; the battery monitor now also emits a `battery_driver_ready` event mirroring `supply_driver_ready` for client symmetry.
- Concurrent SCPI access on the same instrument now serializes correctly. Previously, two `/invoke` requests against the same cached driver in `box/lager/hardware_service.py` could race on the SCPI bus and produce `Query INTERRUPTED` pyvisa errors. Added a per-`(device_name, address)` `threading.Lock` that wraps the actual `func(*args, **kwargs)` call and the stale-VISA-session retry; lock is acquired per call (never held across calls). Multi-channel devices (e.g., Rigol DP821) correctly share one lock since they share one VISA session.

### Changed
- **VISA session ownership unified.** The supply and battery WebSocket monitor handlers (`box/lager/http_handlers/supply.py`, `box/lager/http_handlers/battery.py`) no longer open their own pyvisa sessions in monitor threads. They now hold a `Device` HTTP proxy (`box/lager/nets/device.py`) and route every per-tick driver call (and every TUI command) through `hardware_service.py:/invoke`. `hardware_service.py` (port 8080) is now the sole owner of pyvisa sessions per `(device_name, address)`. The v0.16.5 `POST /cache/clear` band-aid in the WS monitor is removed; the architectural fix replaces it.
- Battery handlers migrated from `box/lager/box_http_server.py` to `box/lager/http_handlers/battery.py`, mirroring the earlier supply migration. The duplicate copies in `box_http_server.py` (~670 lines: `/battery/command` HTTP route, four `/battery` WebSocket handlers, the `monitor_battery` thread) were deleted; `box_http_server.py` now imports and registers the modular handlers via `register_battery_routes` / `register_battery_socketio` / `cleanup_battery_sessions`.
- New shared helper `box/lager/dispatchers/helpers.py:resolve_net_proxy(netname, role, error_class)` returns `(device_module_name, net_info, channel)` for a saved net, mirroring the regex switches in `SupplyDispatcher._choose_driver` and `BatteryDispatcher._choose_driver`. Used by both monitor handlers to construct their `Device` proxies.
- `box/lager/power/supply/ea.py` now exposes a `create_device(net_info)` factory for `hardware_service.py:/invoke`, matching the other supply drivers.
- Two unit tests in `test/unit/cli/test_performance_improvements.py` (`test_config_parsing_cached`, `test_config_cache_invalidation_on_write`) had been silently failing since the `.lager` config format was migrated to JSON-only: they wrote INI/configparser tempfiles, but `cli/config.py:read_config_file` calls `json.load()` and `raise SystemExit(1)` on `JSONDecodeError`. Both tempfiles now write `{"LAGER": {...}}` JSON. Full unit suite now 141/141 passing (was 139/141). No CLI behavior change.

## [0.16.5] - 2026-04-27

### Fixed
- `lager supply <net> state` (and other read-only supply commands) would report `Enabled: OFF` immediately after a successful `lager supply <net> enable` on Keysight E36xxx supplies. The `KeysightE36000` constructor unconditionally called `disable_output()` as a "safe default" on every connect, so each fresh CLI invocation silently turned the output off before running its query. The disable is now gated behind the explicit `reset=True` flag (matching the existing OCP-reset block), so constructing a driver for a read or for `enable` no longer mutates output state.
- `lager supply <net> enable` on EA PSB supplies briefly dropped the output (~500ms) when the output was already on, because `EA.enable()` always ran `_clear_latched_events()` (which writes `OUTPut OFF` and waits 200ms) before turning the output back on. `enable()` is now idempotent: if `OUTPut?` reports the output is already on, it returns immediately without toggling. The off→on path that needs latched-protection clearing is unchanged.
- `lager supply <net> tui` would close after ~5 seconds with no visible error on Rigol DP821 supplies whenever a direct supply command (e.g. `lager supply <net> state`) had been run beforehand. The WebSocket monitor in `box/lager/http_handlers/supply.py` was opening its own pyvisa session via `_resolve_net_and_driver()`, conflicting with the cached VISA session held by `hardware_service.py` on port 8080 — instruments that don't tolerate concurrent USB sessions hung silently and `supply_driver_ready` never fired. The monitor thread now POSTs to `localhost:8080/cache/clear` before opening its session so the cached handle is released first. In the same path, `get_channel_limits()` and the session-store block now emit a visible `error` event on any failure (extending the pattern around `_resolve_net_and_driver`), so init failures no longer disappear into a silent 15-second timeout. The CLI captures the TUI's exit reason and prints it red to stderr after Textual's alt-screen tears down, so the message survives the screen restore.

## [0.16.4] - 2026-04-27

### Fixed
- `/instruments/list` no longer returns an empty list when served from a `ThreadingHTTPServer` worker thread. `scan_usb()`'s `with_timeout` decorator uses `signal.signal(SIGALRM, ...)`, which only works on the main thread; the resulting `ValueError` was silently swallowed by the handler, making boxes appear to have no instruments connected (e.g. LabJack T7) even when devices were plugged in. The scanner now falls back to a no-timeout direct call when not on the main thread; the inner serial/sysfs reads already have their own I/O timeouts. The CLI path was unaffected because `query_instruments.py` runs as a subprocess.

## [0.16.3] - 2026-04-24

### Added
- `lager boxes` (and `lager boxes list`) now shows a `user` column between `ip` and `version`, so boxes configured with a non-default SSH user are visible at a glance. Boxes added without `--user` display as `lagerdata` (the default).

## [0.16.2] - 2026-04-17

### Fixed
- Corrected the USB PID for the Keysight E36313A power supply (`2a8d:1202`) in `SUPPORTED_USB` for both `box/lager/http_handlers/usb_scanner.py` and `cli/impl/query_instruments.py`; previously the PID was a placeholder (`????`) so the device was never recognized. Added a matching udev rule in `box/udev_rules/99-instrument.rules` (`MODE=0666` plus `usbtmc` unbind on `bind`) so PyVISA can open the instrument directly via libusb.

## [0.16.1] - 2026-04-13

### Fixed
- `bench_loader` no longer crashes when `bench.json` or `saved_nets.json` contains explicit `null` values for list or dict fields (`test_hints`, `tags`, `aliases`, `params`, `net_overrides`, `dut_slots`, `interfaces`, `channels`). Previously, `dict.get(key, default)` only substituted the default when the key was absent, so a literal `"test_hints": null` would return `None` and break downstream iteration. All affected sites now use `dict.get(key) or default` so an explicit `null` is treated the same as an absent key.

## [0.16.0] - 2026-04-13

### Added
- Lager MCP (Model Context Protocol) server, running on the box on port 8100 (FastMCP, streamable-http). Allows AI agents to discover a Lager setup and understand how nets are wired to the DUT.
- Net metadata fields: `description`, `dut_connection`, `test_hints`, and `tags`. New CLI commands and TUI flows under `lager nets` for editing them.
- Capability graph and heuristic engine (`box/lager/mcp/engine/`) that map test types to the nets available on a bench.
- Auto-generated MCP API reference built from driver introspection at image build time. The Dockerfile build now fails fast on driver renames.
- Defensive `bench.json` parser so a single malformed entry can no longer break `discover_bench`.
- New integration test `test_agent_loop` and unit tests for the bench loader, capability graph, heuristic engine, safety preflight, and MCP schemas.

### Changed
- The MCP server has moved from the CLI (`cli/mcp/`) to the box (`box/lager/mcp/`). It is now started by `start-services.sh` inside the Docker container rather than running on the developer machine.
- Every MCP tool call is now wired through an `@audited` decorator that records the call via `audit.log_tool_call`, so downstream control planes can rely on a consistent audit trail.
- `quick_io` writes now go through a `preflight_check` that enforces voltage, current, and dangerous-action constraints before hitting hardware.
- MCP errors no longer return raw tracebacks to agents; `NetType()` inputs are validated against the enum.
- `plan_firmware_test` now uses a regex-based pattern split instead of the previous unsafe `get_pattern` split.

### Security
- The `run_lager` MCP passthrough tool is now gated behind the `LAGER_MCP_ALLOW_RUN_LAGER` environment flag and is **off by default**. Operators must opt in explicitly before agents can invoke arbitrary `lager` commands.

## [0.15.2] - 2026-04-08

### Added
- `lager install --version` now accepts a release tag (e.g. `v0.15.0`) in addition to a git branch, so a box can be installed at a pinned version directly. The deployment script detects tags and uses the bare ref for `git reset --hard` instead of the (non-existent) `origin/<tag>` ref.

### Changed
- `lager install --branch` has been replaced by `lager install --version`. The new flag accepts both branches and release tags.

### Fixed
- Reverted DA1469x post-flash reset to use J-Link Commander register writes (as in 0.15.0). The GDB-based reset introduced in 0.15.1 caused regressions on DA1469x targets; Commander remains the supported path.

## [0.15.1] - 2026-04-07

### Fixed
- DA1469x post-flash reset now uses GDB-based reset instead of J-Link Commander register writes, fixing unreliable behavior on DA1469x targets after flashing. The target is reset via `gdb_reset(halt=False)` and the GDB server is stopped so the application runs freely.

## [0.15.0] - 2026-04-02

### Added
- `lager boxes lock` now accepts a `--user` flag to lock as a specific username, useful when running inside a Docker container where the effective user would otherwise be `root`

### Changed
- `lager boxes` now shows a warning when any box is locked as `root`, with instructions to use `--user` or `lager defaults add --user`
- `LAGER_USER` environment variable is now the highest-priority source when determining the lager user for lock operations (before `~/.lager` config and the OS username)
- Lock output and error messages now display the user's email address when available. External tools that lock boxes using the `<tool>:<id>:<email>` lock format will have their email extracted and shown rather than the raw lock string
- `lager update` SSH operations now use `StrictHostKeyChecking=accept-new` to avoid host-key prompts on first connection to a new box
- `lager update` Docker rebuild step now correctly passes the explicit SSH key file when one is in use
- `lager update` stop/remove step now targets the `lager` and `pigpio` containers by name instead of stopping all running containers

### Removed
- `lager boxes connect` command

## [0.14.4] - 2026-03-31

### Changed
- `lager debug flash` now erases flash by default before programming, ensuring a clean boot state. Use `--no-erase` to skip. The `--erase` flag is retained for backwards compatibility.

## [0.14.3] - 2026-03-31

### Fixed
- Supply net current limit no longer gets automatically reset to 1A on TUI startup or any CLI command
- OVP value now correctly displays in `lager supply state` output

### Changed
- Release version branches are now named `X.Y.Z` (without the `v` prefix) to distinguish them from the `vX.Y.Z` tag

## [0.14.2] - 2026-03-30

### Fixed
- `lager debug erase` and `lager debug flash` now correctly pass the JLinkScript to J-Link during the connect step. Previously only `gdbserver` passed the script, causing erase/flash to fail on MCUs that require a JLinkScript to load the correct flash algorithm (e.g. DA1469x with external QSPI flash)
- For DA1469x targets, erase now uses address-range erase instead of chip erase, and no longer halts after erase when flashing
- Fixed crash in `flash_device` when RTT is subsequently run after flashing
- Improved J-Link process management: stale PID files are now cleaned up, and JLinkGDBServer is stopped before chip erase operations to ensure exclusive hardware access

## [0.14.1] - 2026-03-24

### Fixed
- `lager update --version v0.14.0` (and any version tag) now works correctly. Previously, version tags were incorrectly resolved as remote branch refs (`origin/v0.14.0`), causing the update to fail when git could not find the ref. Tags are now resolved directly.

## [0.14.0] - 2026-03-24

### Added
- `lager install-wheel` command to install a local Python wheel file on a Lager Box. Automatically uninstalls any previously installed version of the package before installing, so the version number does not need to be bumped on every rebuild. The package name is parsed from the wheel filename per the wheel specification.

## [0.13.4] - 2026-03-23

### Removed
- **Ephemeral command lock** — The automatic command-in-progress lock that fired on every CLI command has been removed. It had multiple corner cases: supply commands never released the lock, long-running commands (like `gdbserver`) blocked all other commands, etc.
- `--force-command` flag removed from all commands (no longer needed)

### Note
- User lock (`lager boxes lock/unlock`) is unchanged — use it to reserve a box for yourself

## [0.13.3] - 2026-03-21

### Fixed
- `lager python --detach` now correctly holds the command lock while the detached process runs. Previously the lock was released immediately, allowing other commands to run against a busy box

## [0.13.2] - 2026-03-21

### Changed
- Maintenance release: updated repository configuration

## [0.13.1] - 2026-03-20

### Fixed
- `lager ssh` now properly releases the command lock when the SSH session ends. Previously, `os.execvp` replaced the Python process, preventing cleanup handlers from running, which left boxes stuck in "busy" state until the 30-minute auto-expiry

## [0.13.0] - 2026-03-20

### Added
- `--force-command` is now a local flag on all subcommands that target a box, not just a global flag
- `--force-command` added to `hello`, `install`, `uninstall`, and `boxes connect` commands

### Changed
- `lager python --detach` now keeps the command lock until the detached process finishes on the box. The lock is automatically released when the script completes
- `acquire_command_lock_with_cleanup` now checks `ctx.obj.force_command` automatically, so all commands that acquire locks support `--force-command`

### Fixed
- Locking documentation updated to reflect current behavior

## [0.12.0] - 2026-03-20

### Added
- **Command-in-progress lock** — When a `lager` command is running on a box, all other commands are blocked with a "Command in progress" error, including from the same user. Locks auto-expire after 30 minutes to handle crashed CLI processes
- **User lock (`lager boxes lock/unlock`)** — Explicitly lock a box so only you can run commands on it. Other users see a lock error until you unlock. The user who locked it can still run commands
- `--force-command` global flag to bypass command-in-progress locks
- `lager boxes` list now shows "locked by" and "busy" columns when any box has a lock or command in progress
- `lager python --kill`, `--kill-all`, and `--reattach` skip lock checks (management operations)

### Changed
- Command lock is process-based — same user cannot stomp on their own commands unless using `--force-command`
- Hardcoded control plane URL, removed `--url` flag from `lager boxes connect`

## [0.11.0] - 2026-03-18

### Added
- Nordic PPK2 (Power Profiler Kit II) support as a watt-meter and energy-analyzer instrument
- `lager watt` reads instantaneous power from PPK2 nets
- `lager energy <net> read` integrates energy and charge over a configurable duration
- `lager energy <net> stats` computes current/voltage/power statistics (mean, min, max, std)
- PPK2 auto-detection via `lager instruments` and `lager nets add-all`
- Python API: `Net.get(name, type=NetType.WattMeter)` and `Net.get(name, type=NetType.EnergyAnalyzer)` for PPK2 nets
- Unit tests for PPK2 location parsing, dispatcher routing, singleton caching, and measurement math
- Integration test suite for PPK2 hardware validation (`test/api/sensors/test_ppk2.py`)
- Webcam start/stop HTTP endpoints for dashboard control

### Changed
- `lager energy` command now uses `lager energy <NETNAME> <subcommand> --options` argument order (consistent with other commands like `lager supply`)

### Fixed
- Webcam MJPEG stream 404 for dashboard `/stream/{netName}` requests
- `Net.get()` now falls back to `address` field when `location` is not set in saved net config

## [0.10.0] - 2026-03-17

### Added
- `lager router` command group for managing routers as Lager nets
- `lager router add-net` to register a router (MikroTik hAP or compatible) as a net on a Lager Box
- `lager router connect` to verify connectivity to a router net
- `lager router interfaces` and `lager router wireless-interfaces` to inspect network interfaces
- `lager router wireless-clients` to list connected wireless clients
- `lager router dhcp-leases` to list devices that have received IP addresses
- `lager router system-info` to query router resource usage
- `lager router reboot` to reboot a router net
- `lager router enable-interface` / `lager router disable-interface` for wireless interface control
- `lager router block-internet` to drop all forwarded traffic for network isolation testing
- `lager router reset` to restore a router to a clean baseline state (removes test firewall rules, bandwidth limits, and access list entries)
- `lager router run` for arbitrary REST API calls against the router

## [0.9.0] - 2026-03-16

### Added
- `disconnect_wifi()` standalone function for the Python WiFi API
- `lager boxes` now reads project-level `.lager` files, not just the global `~/.lager`
- WiFi Python API docs updated to use standalone functions

### Fixed
- `lager boxes` showing empty results in fresh Docker containers when boxes were defined in a project-level `.lager` file
- Typo in `wifi/status.py`

## [0.8.0] - 2026-03-12

### Added
- RTT RAM search parameters for Python API: `dbg.rtt(search_addr=, search_size=, chunk_size=)`
- RTT RAM search CLI flags: `--rtt-search-addr`, `--rtt-search-size`, `--rtt-chunk-size`
- Instruments and nets HTTP handlers on Lager Box

### Fixed
- PID file path mismatch: `status()` and `rtt()` now check both `/tmp/jlink.pid` and `/tmp/jlink_gdbserver.pid`
- `detect_and_configure_rtt()` now detects running debugger correctly (was always reporting "No debugger connection")
- `erase_flash()` and `read_memory()` now check both PID file paths

## [0.7.0] - 2026-03-10

### Added
- `lager devenv terminal --attach <container_name>` to attach to a running Docker container
- `lager devenv terminal --shell <path>` to override the shell when attaching
- Jobs WebSocket client in control plane heartbeat for receiving and executing job dispatch commands

### Changed
- Default control plane URL changed to the new control-plane API domain

## [0.6.0] - 2026-03-06

### Added
- `lager python --reattach <ID>` to stream output from detached processes (replays from start)
- `lager python --kill <ID>` to kill a specific detached process
- `lager python --kill-all` to kill all running `lager python` processes on a box
- Ctrl+D during `--reattach` detaches without killing the process
- 10 MB log cap for detached process output to prevent disk abuse
- Runtime warning when system-installed `lager` CLI shadows a virtual environment version

### Fixed
- Ctrl+C during `lager python` no longer breaks the Acroname USB hub (required box reboot before)
- `--detach` no longer hangs; returns immediately with process ID and reattach/kill hints
- `--kill` now actually kills the process (was silently doing nothing)
- `--kill <invalid-id>` shows a friendly error instead of a traceback
- Multi-user box provisioning: new users are always added to the docker group
- `start_box.sh` uses `$HOME` instead of hardcoded `/home/lagerdata` paths

### Changed
- `--kill` changed from a boolean flag to an option that takes a process ID
- Detached process output now shows box name instead of IP address

## [0.5.0] - 2026-03-05

### Added
- Control plane heartbeat client (`control_plane_client.py`) for WebSocket-based box status reporting
- `/status` endpoint on both Flask and Python HTTP servers returning box health, version, and nets
- `lager boxes connect` command to configure a box for control plane heartbeat reporting
- `websocket-client` dependency added to box Docker image

### Changed
- Refactored version file reading in `service.py` into reusable `_read_box_version()` helper
- `start-services.sh` starts control plane heartbeat when configured

## [0.4.2] - 2026-03-04

### Improved
- `lager install` and `lager uninstall` now provide detailed SSH error diagnostics (connection refused, no route to host, host key changes)
- `lager uninstall` supports `--dry-run` flag to preview what would be removed without making changes
- Deployment script uses SSH connection multiplexing for reliability over VPN connections
- Shared `host_in_known_hosts` utility extracted to `ssh_utils` for consistent host key handling across commands

## [0.4.1] - 2026-03-03

### Fixed
- `lager install` GitHub connectivity check now uses `git ls-remote` instead of `curl`, fixing deployment failures on boxes where `curl` is not installed (e.g. Ubuntu 24.04)

## [0.4.0] - 2026-03-03

### Changed
- `lager install` deploys box code via HTTPS git clone instead of SSH, removing the need for GitHub deploy keys
- `lager update` automatically migrates existing boxes from SSH to HTTPS remote URLs

### Improved
- Open-source release: repository is publicly accessible, enabling installation and updates without GitHub credentials

## [0.3.27] - 2026-02-19

### Added
- `lager-mcp` console script entry point for easier MCP server setup with AI assistants

## [0.3.26] - 2026-02-19

### Added
- Full Model Context Protocol (MCP) server with 165+ tools across 21 modules
- 254 unit tests and 64 integration tests for MCP coverage

## [0.3.25] - 2026-02-18

### Added
- Full FT232H USB cable support for SPI, I2C, and GPIO
- GPIO hold mode

### Fixed
- SPI config persistence between CLI commands
- LabJack T7 auto-CS reliability
- FT232H USB resource cleanup

## [0.3.24] - 2026-02-17

### Added
- CLI update notifications checking PyPI in background with 24-hour cache

### Fixed
- Duplicate SPI channel in LabJack T7 instrument query

## [0.3.23] - 2026-02-16

### Added
- LabJack pin conflict detection for multi-subsystem usage
- Major documentation overhaul for I2C, SPI, power supply, scope, ADC, GPI, GPO

## [0.3.22] - 2026-02-16

### Changed
- Removed LabJack T7 pin conflict restrictions; dynamic configuration at transaction time

## [0.3.21] - 2026-02-15

### Fixed
- `lager update` version file write timing with retry logic

## [0.3.20] - 2026-02-13

### Added
- Aardvark GPIO support
- SPI chip select (CS) control for Aardvark and LabJack
- GPI direction configuration
- Natural sorting for CLI list output

## [0.3.19] - 2026-02-09

### Added
- I2C protocol support (Aardvark, LabJack T7)
- FT232H adapter support
- Joulescope JS220 watt meter
- Net TUI enhancements with rename/delete

### Fixed
- SPI and Aardvark reliability improvements

## [0.3.18] - 2026-01-30

### Added
- JLinkScript storage with debug nets via `lager nets set-script`

### Fixed
- Power supply VISA session staleness
- Increased `lager update` SSH timeout

## [0.3.17] - 2026-01-29

### Added
- SPI communication support via LabJack T7 (modes 0-3, configurable frequency and word size)

### Fixed
- GPIO handle closing SPI connections
- LabJack SPI 800kHz workaround

## [0.3.16] - 2026-01-26

### Added
- J-Link script file support for custom initialization
- Expanded ARM device support to 70+ families

### Fixed
- "Resource Busy" USB errors
- Debug reset reliability for Cortex-M33

## [0.3.15] - 2026-01-20

### Fixed
- `lager update` command status synchronization
- `lager update --all` box synchronization

## [0.3.14] - 2026-01-20

### Improved
- Enhanced error messages with actionable guidance
- Input validation for numeric, address, and package parameters
- Connection error handling with platform-specific hints

## [0.3.13] - 2026-01-16

### Added
- Interactive Lager Terminal (REPL) with tab completion and command history
- `lager update --all` and `--needs-update` flags
- Live box status with version checks

## [0.3.12] - 2026-01-15

### Fixed
- Keysight E36233A Supply TUI support
- Dispatcher import error
- SCPI measurement commands and negative zero display

## [0.3.11] - 2026-01-15

### Fixed
- Supply TUI VISA resource lock on force-close
- Keysight E36200 output state preservation

## [0.3.10] - 2026-01-15

### Added
- Lager Terminal integrated into CLI (run `lager` with no args)

## [0.3.9] - 2026-01-15

### Fixed
- Supply TUI import error after dispatcher refactoring

## [0.3.8] - 2026-01-15

### Added
- Global VISA connection manager preventing "Resource busy" errors
- TestResult schema for structured test data
- Power supply/battery drivers return numeric values

## [0.3.7] - 2026-01-15

### Fixed
- Keysight E36233A incorrectly identified as E36313A

## [0.3.6] - 2026-01-15

### Added
- Enhanced `lager boxes sync` with version comparison

### Fixed
- `lager update` sudoers issues
- Container startup timeout increased to 5 minutes

## [0.3.5] - 2026-01-09

### Changed
- `lager install` works without lager repository

## [0.3.4] - 2026-01-07

### Added
- Custom SSH username support via `--user` flag for install/uninstall

## [0.3.3] - 2026-01-07

### Changed
- PyPI package includes deployment scripts; `lager install` works from PyPI

## [0.3.2] - 2026-01-07

### Added
- `--box` flag for install/uninstall commands

### Changed
- Uninstall default preserves `/etc/lager`

## [0.3.1] - 2026-01-05

### Changed
- Major codebase restructure: CLI commands reorganized into logical groups
- Box code reorganized by domain (power, io, measurement, protocols, automation)

### Added
- Logitech C930e webcam support

### Removed
- Legacy OpenOCD code (J-Link is now the only debug backend)
- Backward compatibility import stubs

## [0.2.36] - 2025-12-18

### Changed
- Terminology restructure: "gateway/DUT" renamed to "box" throughout codebase
- Directory flattened from `gateway/lager/lager/` to `box/lager/`

## [0.2.35] - 2025-12-17

### Changed
- 14 Python API function renamings for consistency

### Fixed
- ARM/robot serial port hangs and position polling
- Battery API mapper

## [0.2.33] - 2025-12-15

### Changed
- `lager hello` displays actual hostname instead of container ID

### Added
- Release Notes section in documentation

### Fixed
- Eload Net API and multi-channel USB caching

## [0.2.32] - 2025-12-11

### Added
- J-Link debugger auto-installed during deployment
- Flexible deployment with custom usernames

## [0.2.31] - 2025-12-10

### Added
- PicoScope and Rigol oscilloscope support with voltage measurements, cursor modes, autoscale

### Fixed
- Device proxy enum handling and channel parameter bugs

## [0.2.30] - 2025-12-08

### Added
- MCC USB-202 DAQ support for ADC, DAC, GPIO
- Improved `lager update` interface

## [0.2.29] - 2025-12-06

### Fixed
- `lager python download`, `lager exec`, and `lager devenv` commands

## [0.2.28] - 2025-12-05

### Fixed
- `lager exec` command execution

## [0.2.27] - 2025-12-05

### Fixed
- Minor bug fixes and stability improvements

## [0.2.26] - 2025-12-05

### Improved
- Webcam interface with enhanced controls and reduced zoom latency

### Fixed
- `lager exec` and net.py issues

## [0.2.25] - 2025-12-04

### Changed
- Updated `lager devenv` command functionality

## [0.2.24] - 2025-12-04

### Added
- `lager boxes add-all` command for bulk box management

### Fixed
- UART data corruption
- NetType.Analog for Rigol scopes

## [0.2.23] - 2025-12-02

### Fixed
- Multi-channel USB resource sharing for Keysight devices

## [0.2.22] - 2025-12-02

### Added
- Hardware invocation service for Device proxy pattern
- Keysight power supply support in `lager python` scripts

## [0.2.21] - 2025-12-02

### Added
- Phidget thermocouple expanded to 4 channels
- Keysight device support in Python scripts

## [0.2.20] - 2025-11-26

### Added
- Nets working in `lager python` scripts
- UART support in Python
- Oscilloscope web UI with HTTP server and WebSocket

### Fixed
- Python file command, udev rules, LabJack timeout hangs

## [0.2.19] - 2025-11-24

### Added
- `lager binaries` command for managing binary files
- Progress bar and verbose flag for `lager update`

## [0.2.18] - 2025-11-24

### Added
- Automatic security configuration in `lager update`
- Updated Keysight E36300 support

## [0.2.17] - 2025-11-21

### Added
- Concurrent CLI commands while Supply TUI is running

### Fixed
- UART `--line-ending` flag and communication issues

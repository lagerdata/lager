# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
OpenOCD backend for the debug net.

This module is the OpenOCD counterpart to ``debug/jlink.py`` +
``debug/gdbserver.py``. It runs a long-lived ``openocd`` process per probe
slot (binding to a specific USB serial via ``adapter serial <serial>``) and
dispatches all runtime commands (flash, erase, reset, memrd, RTT) through
OpenOCD's TCL/RPC port. That's the major UX difference from the J-Link path:
J-Link needs to free its USB handle for ``JLinkExe`` to flash/erase, but
OpenOCD does everything through the *running* gdbserver, so we never have
to bounce it.

Probe → interface config map: derived from (VID, PID). The target config
(``target/stm32f4x.cfg`` etc.) is derived from the net's ``device`` field;
users can override either via the ``openocd_config`` field on the net
(mirrors ``jlink_script``). When ``openocd_config`` is supplied, it
replaces the auto-detected interface cfg entirely — loading both would
let the two .cfg files' ``adapter driver`` / ``layout_init`` commands
collide unpredictably.
"""

import logging
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

from .probes import (
    openocd_pidfile,
    openocd_logfile,
    parse_probe_address,
    parse_device_field,
    is_ftdi_vid,
)

logger = logging.getLogger(__name__)

# Where OpenOCD lives in the lager container. Built-in scripts (interface/*.cfg,
# target/*.cfg) ship with the apt package and live under
# /usr/share/openocd/scripts. ``-s`` adds that to the search path so bare
# names like ``interface/stlink.cfg`` resolve.
OPENOCD_EXE_PATHS = [
    '/usr/bin/openocd',
    '/usr/local/bin/openocd',
]

OPENOCD_SCRIPT_DIRS = [
    '/usr/share/openocd/scripts',
    '/usr/local/share/openocd/scripts',
]


def get_openocd_exe_path():
    """Locate the ``openocd`` binary in the container, or None."""
    for path in OPENOCD_EXE_PATHS:
        if os.path.exists(path):
            return path
    return None


def get_openocd_script_dir():
    """Pick a usable OpenOCD scripts directory, or None if neither exists."""
    for path in OPENOCD_SCRIPT_DIRS:
        if os.path.isdir(path):
            return path
    return None


# --------------------------------------------------------------------------
# Probe → interface config map
# --------------------------------------------------------------------------

# FTDI chips ship across many products with very different pin layouts, so
# we can't pick a single .cfg from VID alone. The (VID, PID) → cfg table
# below covers the chips we have a confident default for; anything missing
# returns None and forces the user to supply ``openocd_config``.
#
# * FT232H (PID 6014): single MPSSE channel. The C232HM cable is the
#   FTDI reference design, and most generic FT232H breakouts (Adafruit,
#   custom) wire the pins the same way (ADBUS0=TCK, ADBUS1=TDI,
#   ADBUS2=TDO, ADBUS3=TMS). The stock c232hm.cfg works for the common
#   case; non-standard layouts override via ``openocd_config``.
# * FT2232H (PID 6010): dual MPSSE. The Olimex ARM-USB-OCD-H is a
#   widespread reference board for this chip and was the historical
#   default for all FTDI cables in this codebase — kept for back-compat.
# * FT4232H (PID 6011): quad MPSSE, used on boards with wildly different
#   wiring schemes. No safe default — force user to supply a cfg.
_FTDI_PID_TO_CFG = {
    '6014': 'interface/ftdi/c232hm.cfg',                   # FT232H
    '6010': 'interface/ftdi/olimex-arm-usb-ocd-h.cfg',     # FT2232H
}


def interface_config_for_address(address):
    """Return the OpenOCD ``-f interface/...`` config for a probe address.

    None means we can't infer it from (VID, PID) and the caller must rely
    on a user-supplied ``openocd_config``.
    """
    vid, pid, _serial = parse_probe_address(address)
    if not vid:
        return None
    # ST-Link family — single shared config handles v2/v2-1/v3.
    if vid == '0483':
        return 'interface/stlink.cfg'
    # Raspberry Pi Picoprobe (CMSIS-DAP firmware).
    if vid == '2e8a':
        return 'interface/cmsis-dap.cfg'
    # Atmel EDBG / CMSIS-DAP variants.
    if vid == '03eb':
        return 'interface/cmsis-dap.cfg'
    # NXP / ARM DAPLink-style.
    if vid == '0d28':
        return 'interface/cmsis-dap.cfg'
    # FTDI: dispatch by PID since the chip family determines the pinout.
    if vid == '0403':
        return _FTDI_PID_TO_CFG.get(pid)
    # Olimex ARM-USB-OCD-H uses its own VID but is electrically an FT2232H.
    if vid == '15ba':
        return 'interface/ftdi/olimex-arm-usb-ocd-h.cfg'
    return None


# --------------------------------------------------------------------------
# Device → target config map
# --------------------------------------------------------------------------

# Ordered list of (prefix-regex, target.cfg). Matches are case-insensitive
# against the J-Link-style device name stored in the net (channel/pin field).
# This is intentionally heuristic — users with exotic devices set
# ``openocd_config`` themselves.
_TARGET_PREFIX_MAP = [
    ('STM32F0', 'target/stm32f0x.cfg'),
    ('STM32F1', 'target/stm32f1x.cfg'),
    ('STM32F2', 'target/stm32f2x.cfg'),
    ('STM32F3', 'target/stm32f3x.cfg'),
    ('STM32F4', 'target/stm32f4x.cfg'),
    ('STM32F7', 'target/stm32f7x.cfg'),
    ('STM32H7', 'target/stm32h7x.cfg'),
    ('STM32G0', 'target/stm32g0x.cfg'),
    ('STM32G4', 'target/stm32g4x.cfg'),
    ('STM32L0', 'target/stm32l0.cfg'),
    ('STM32L1', 'target/stm32l1.cfg'),
    ('STM32L4', 'target/stm32l4x.cfg'),
    ('STM32L5', 'target/stm32l5x.cfg'),
    ('STM32U5', 'target/stm32u5x.cfg'),
    ('STM32WB', 'target/stm32wbx.cfg'),
    ('STM32WL', 'target/stm32wlx.cfg'),
    ('STM32C0', 'target/stm32c0x.cfg'),
    ('NRF51', 'target/nrf51.cfg'),
    ('NRF52', 'target/nrf52.cfg'),
    ('NRF53', 'target/nrf53.cfg'),
    ('NRF54', 'target/nrf54l.cfg'),
    ('NRF91', 'target/nrf91.cfg'),
    ('RP2040', 'target/rp2040.cfg'),
    ('RP2350', 'target/rp2350.cfg'),
    ('ATSAMD21', 'target/at91samdXX.cfg'),
    ('ATSAMD51', 'target/atsame5x.cfg'),
    ('ATSAME54', 'target/atsame5x.cfg'),
    ('LPC1', 'target/lpc1xxx.cfg'),
    ('LPC54', 'target/lpc54xxx.cfg'),
    ('LPC55', 'target/lpc55xx.cfg'),
    ('MIMXRT', 'target/imxrt500.cfg'),
    ('ESP32C3', 'target/esp32c3.cfg'),
    ('ESP32C6', 'target/esp32c6.cfg'),
    ('ESP32S2', 'target/esp32s2.cfg'),
    ('ESP32S3', 'target/esp32s3.cfg'),
    ('ESP32', 'target/esp32.cfg'),
]


def target_config_for_device(device):
    """Best-effort map a J-Link device name to an OpenOCD target.cfg.

    Returns None when we can't infer one — caller must rely on a custom
    ``openocd_config``.
    """
    if not device:
        return None
    dev_upper = str(device).upper()
    for prefix, target in _TARGET_PREFIX_MAP:
        if dev_upper.startswith(prefix):
            return target
    return None


# --------------------------------------------------------------------------
# OpenOCD lifecycle
# --------------------------------------------------------------------------

def get_openocd_status(serial=None):
    """Check whether an OpenOCD instance is running for *serial*.

    Returns ``{'running': bool, 'pid': int|None}``.
    """
    pidfile = openocd_pidfile(serial)
    if not os.path.exists(pidfile):
        return {'running': False, 'pid': None}
    try:
        with open(pidfile, 'r') as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return {'running': False, 'pid': None}
    try:
        os.kill(pid, 0)
        return {'running': True, 'pid': pid}
    except OSError:
        try:
            os.remove(pidfile)
        except OSError:
            pass
        return {'running': False, 'pid': None}


def _pkill_pattern(serial, tcl_port):
    """Pattern uniquely identifying *this* OpenOCD instance.

    Strategy: anchor on the explicit ``-c "tcl_port <N>"`` we always pass,
    which is per-slot and so unique across concurrent boxes. We avoid
    matching on the serial because users may supply OpenOCD configs that
    re-encode the serial differently.
    """
    return f'openocd.*tcl_port {tcl_port}'


def stop_openocd(serial=None, tcl_port=None):
    """Stop the OpenOCD process for *serial* (or legacy single-probe path).

    Mirrors ``debug/gdbserver.stop_jlink_gdbserver``: try PID file first,
    fall back to pkill on the tcl_port pattern for orphans.
    """
    pidfile = openocd_pidfile(serial)
    pid = None
    if os.path.exists(pidfile):
        try:
            with open(pidfile, 'r') as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            pid = None

    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Wait briefly for graceful shutdown.
        for _ in range(20):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.1)
        except ProcessLookupError:
            pass
        try:
            os.remove(pidfile)
        except OSError:
            pass

    # Orphan cleanup: a previous run may have failed to write the pidfile.
    if tcl_port is not None:
        pattern = _pkill_pattern(serial, tcl_port)
        try:
            check = subprocess.run(
                ['pgrep', '-f', pattern],
                capture_output=True, timeout=2.0, check=False,
            )
            if check.returncode == 0:
                subprocess.run(
                    ['pkill', '-TERM', '-f', pattern],
                    timeout=1.0, check=False,
                )
                time.sleep(0.3)
                subprocess.run(
                    ['pkill', '-KILL', '-f', pattern],
                    timeout=1.0, check=False,
                )
        except FileNotFoundError:
            # pgrep/pkill missing — best effort, nothing to do.
            pass


def _build_openocd_command(
    *, openocd_exe, scripts_dir, address, device, transport, speed, halt,
    gdb_port, telnet_port, tcl_port, rtt_telnet_port, user_config_path,
    log_file, probe_channel=None,
):
    """Assemble the ``openocd`` argv for a fresh gdbserver start.

    OpenOCD config files are evaluated top-to-bottom, and most target/interface
    scripts perform an implicit ``init``. We therefore:

    1. Override the default port numbers up front so they take effect when
       the targets later call ``$_TARGETNAME configure -gdb-port`` (no-op for
       most boards, but harmless).
    2. Load the interface bring-up — either the auto-detected
       ``interface/*.cfg`` or the user-supplied ``openocd_config``. Either
       way, this is what sets ``adapter driver`` so the ``-c`` commands
       that follow (``adapter serial``, ``ftdi channel``, ``transport
       select``) have a driver to talk to.
    3. Load the target config (auto-detected from the net's device field).
       Skipped when the user cfg defines its own target — pick a device
       string that doesn't match ``_TARGET_PREFIX_MAP`` to avoid double
       ``target create`` failures.
    4. Set adapter speed and (optionally) halt on reset.
    """
    vid, _pid, serial = parse_probe_address(address)
    interface_cfg = interface_config_for_address(address) if address else None
    target_cfg = target_config_for_device(device)

    cmd = [openocd_exe]
    if scripts_dir:
        cmd.extend(['-s', scripts_dir])

    # Ports first so anything ``init``-ed by later configs binds to the slot.
    cmd.extend([
        '-c', f'gdb_port {gdb_port}',
        '-c', f'telnet_port {telnet_port}',
        '-c', f'tcl_port {tcl_port}',
    ])

    # Bind all listening ports to 0.0.0.0 so off-box gdb / telnet clients
    # can reach them through the docker port forward (``-p 2331-2342:...``
    # in start_box.sh). OpenOCD ≥ 0.11 defaults ``bindto`` to ``127.0.0.1``
    # for security, which means the docker forward delivers traffic to the
    # container's veth interface and finds nothing listening — the on-laptop
    # gdb client times out without any error from OpenOCD itself. This
    # matches the J-Link path's default (``JLinkGDBServer`` binds all
    # interfaces unless ``-localhostonly 1`` is set, which we don't pass).
    # The TCL/RPC port (used by the box-side service via 127.0.0.1) is
    # unaffected by this change either way; ``bindto`` widens the listen
    # set, it doesn't restrict it.
    cmd.extend(['-c', 'bindto 0.0.0.0'])

    # When the user attaches a custom ``openocd_config``, treat it as the
    # sole source of interface configuration: don't also load the auto-detected
    # interface_cfg, since the two .cfg files would each call ``adapter driver
    # ftdi`` / ``layout_init`` and override each other unpredictably.
    #
    # We load the user cfg *first* — before the adapter-dependent ``-c``
    # commands below (``ftdi channel``, ``adapter serial``, ``transport
    # select``). Those commands all require an adapter driver to already be
    # configured, and for user-cfg probes the driver only gets set when the
    # cfg itself runs.
    if user_config_path:
        if not os.path.exists(user_config_path):
            raise FileNotFoundError(
                f'openocd_config path not found on box: {user_config_path}'
            )
        cmd.extend(['-f', user_config_path])
    elif interface_cfg:
        cmd.extend(['-f', interface_cfg])
    else:
        # Common ways to land here: user set ``debug_backend: openocd``
        # explicitly on a net whose (VID, PID) isn't in our auto-classify
        # map (Black Magic Probe at 0x1209, Glasgow at 0x20b7, FT4232H-based
        # custom boards, etc.) but didn't supply an ``openocd_config``. The
        # message has to be actionable because the failure is otherwise
        # opaque — OpenOCD never starts and the user just sees a generic
        # "OpenOCD exited" log.
        hint_vid = f'0x{vid}' if vid else '<unknown>'
        raise FileNotFoundError(
            f"Cannot infer OpenOCD interface for probe VID {hint_vid}. "
            f"Either change ``debug_backend`` to a probe whose VID is "
            f"in lager.debug.probes._OPENOCD_VIDS, or attach a custom "
            f"OpenOCD ``.cfg`` to the net via the ``openocd_config`` "
            f"field (CLI: ``lager nets set-script <net> <file.cfg>``)."
        )

    # Multi-channel FTDI: override the interface config's default channel so
    # users wiring the JTAG/SWD pins to interface B (or C/D on FT4232H) don't
    # have to ship a custom .cfg. Issued AFTER the interface config so the
    # ``ftdi`` command is recognised. No-op for non-FTDI probes.
    if probe_channel is not None and is_ftdi_vid(vid):
        try:
            ch = int(probe_channel)
            if 0 <= ch <= 3:
                cmd.extend(['-c', f'ftdi channel {ch}'])
        except (TypeError, ValueError):
            logger.warning(
                'Ignoring non-integer FTDI probe_channel: %r', probe_channel,
            )

    # Bind the adapter to a specific USB serial *after* the interface config
    # has selected the adapter driver, otherwise this command is unknown.
    if serial:
        cmd.extend(['-c', f'adapter serial {serial}'])

    # Most STLink configs assume hla_swd; honour the user's requested
    # transport when they passed SWD/JTAG explicitly.
    #
    # Skip when ``user_config_path`` is set: custom configs almost always
    # call ``transport select`` themselves at the top, and OpenOCD errors
    # ("Transport already selected") on a second invocation.
    if transport and not user_config_path:
        transport_lc = transport.lower()
        if interface_cfg and 'stlink' in interface_cfg:
            # ST-Link only supports hla_swd / hla_jtag (high-level).
            if transport_lc in ('swd', 'hla_swd'):
                cmd.extend(['-c', 'transport select hla_swd'])
            elif transport_lc in ('jtag', 'hla_jtag'):
                cmd.extend(['-c', 'transport select hla_jtag'])
        else:
            if transport_lc in ('swd', 'jtag'):
                cmd.extend(['-c', f'transport select {transport_lc}'])

    if target_cfg:
        cmd.extend(['-f', target_cfg])
    elif not user_config_path:
        raise FileNotFoundError(
            f'No OpenOCD target.cfg known for device {device!r} and no '
            f'user openocd_config supplied'
        )

    # When the user attached a custom ``openocd_config`` they've signalled
    # "I'm managing adapter setup myself" — appending ``adapter speed <N>``
    # *after* their cfg silently clobbers any ``adapter speed`` they set
    # (their cfg is sourced earlier in argv, so OpenOCD's last-write-wins
    # makes lager's default override theirs). This is the same gating used
    # for ``transport select`` above, for the same reason. CLI ``--speed``
    # still works against auto-detected interface cfgs, where lager is the
    # source of truth for adapter setup.
    if (
        speed
        and str(speed).lower() != 'adaptive'
        and not user_config_path
    ):
        try:
            cmd.extend(['-c', f'adapter speed {int(speed)}'])
        except ValueError:
            logger.warning('Ignoring non-numeric OpenOCD speed: %r', speed)

    # We expose a per-slot RTT control port; the actual ``rtt server start``
    # is issued lazily when a client requests RTT (so the firmware has had
    # time to initialise the RTT block in RAM). We stash the chosen port in
    # an OpenOCD variable so handlers can read it back.
    cmd.extend(['-c', f'set LAGER_RTT_PORT {rtt_telnet_port}'])

    # Reset behaviour: most target configs call ``reset_config`` themselves.
    # We just decide whether to halt at startup. ``init`` runs configured
    # ``$_TARGETNAME`` and applies all settings.
    cmd.extend(['-c', 'init'])
    if halt:
        cmd.extend(['-c', 'reset halt'])
    else:
        cmd.extend(['-c', 'reset run'])

    # Keep the process alive — OpenOCD without ``-c "shutdown"`` runs forever.
    # We do NOT pass ``-d3`` (debug logging) here; the user's openocd_config
    # can add that if they need it.
    return cmd


def start_openocd_gdbserver(
    *, device, address=None, speed='adaptive', transport='SWD', halt=False,
    gdb_port=2331, telnet_port=4444, tcl_port=6666, rtt_telnet_port=9090,
    serial=None, openocd_config=None, probe_channel=None,
):
    """Start an OpenOCD instance bound to a specific probe.

    Args mirror ``start_jlink_gdbserver`` where they overlap:

    * ``device``: J-Link-style target name (e.g. ``RP2040_M0_0``,
      ``STM32F4x``). Used to pick the OpenOCD ``target/*.cfg``. May carry an
      optional ``@<channel>`` suffix (``@A`` / ``@0``..``@3``) selecting the
      FTDI interface for multi-channel adapters; the suffix is stripped
      before target lookup and the channel is forwarded to OpenOCD via
      ``ftdi channel <N>``.
    * ``address``: VISA resource string for the probe (used to derive both
      the USB serial and the OpenOCD interface config).
    * ``speed``: ``adaptive`` or a kHz integer.
    * ``transport``: ``SWD`` or ``JTAG`` — applied via OpenOCD's
      ``transport select`` command.
    * ``halt``: when True, OpenOCD runs ``reset halt`` after init.
    * ``gdb_port`` / ``telnet_port`` / ``tcl_port`` / ``rtt_telnet_port``:
      per-slot ports allocated by ``probes.py``.
    * ``serial``: USB serial; passed to OpenOCD via ``adapter serial`` so
      multiple identical probes can coexist on one box.
    * ``openocd_config``: optional path to a user ``.cfg``/``.tcl`` file
      (mirrors ``jlink_script``). Applied *after* the standard interface +
      target configs so it can override anything.
    * ``probe_channel``: FTDI interface index (0..3) or None. Overrides any
      ``@channel`` suffix in ``device``. Ignored for non-FTDI probes.

    Returns ``{'pid', 'status', 'gdb_port', 'telnet_port', 'tcl_port',
                'rtt_telnet_port', 'serial'}``.
    """
    # Strip any ``@channel`` suffix from device so target_config_for_device
    # gets the bare MCU name. Explicit ``probe_channel`` kwarg wins over the
    # parsed suffix (lets callers override without re-parsing).
    parsed_target, parsed_channel = parse_device_field(device)
    device = parsed_target
    if probe_channel is None:
        probe_channel = parsed_channel
    openocd_exe = get_openocd_exe_path()
    if not openocd_exe:
        raise Exception(
            'openocd binary not found; install via `apt install openocd` in '
            'the lager container'
        )

    pidfile = openocd_pidfile(serial)
    logfile = openocd_logfile(serial)

    # Stop any prior instance on the same TCL port before starting fresh.
    stop_openocd(serial=serial, tcl_port=tcl_port)
    time.sleep(0.15)

    cmd = _build_openocd_command(
        openocd_exe=openocd_exe,
        scripts_dir=get_openocd_script_dir(),
        address=address,
        device=device,
        transport=transport,
        speed=speed,
        halt=halt,
        gdb_port=gdb_port,
        telnet_port=telnet_port,
        tcl_port=tcl_port,
        rtt_telnet_port=rtt_telnet_port,
        user_config_path=openocd_config,
        log_file=logfile,
        probe_channel=probe_channel,
    )

    logger.info('Starting OpenOCD: %s', ' '.join(cmd))

    with open(logfile, 'w') as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setpgrp,
        )

    with open(pidfile, 'w') as f:
        f.write(str(proc.pid))

    # OpenOCD startup includes probe enumeration + target reset; this is the
    # roughly the same time budget as JLinkGDBServer's initial connect.
    deadline = time.time() + 8.0
    last_err = None
    while time.time() < deadline:
        if proc.poll() is not None:
            try:
                with open(logfile, 'r') as f:
                    log_text = f.read()
            except OSError:
                log_text = '<logfile unavailable>'
            try:
                os.remove(pidfile)
            except OSError:
                pass
            raise Exception(
                f'OpenOCD exited with code {proc.returncode}:\n{log_text}'
            )
        # Poll the TCL port; once OpenOCD accepts connections we know init
        # finished cleanly.
        try:
            with socket.create_connection(('127.0.0.1', tcl_port), timeout=0.5) as s:
                s.close()
            return {
                'pid': proc.pid,
                'status': 'started',
                'gdb_port': gdb_port,
                'telnet_port': telnet_port,
                'tcl_port': tcl_port,
                'rtt_telnet_port': rtt_telnet_port,
                'serial': serial,
            }
        except (OSError, ConnectionRefusedError) as exc:
            last_err = exc
            time.sleep(0.2)

    # Timeout — surface the log content so callers can show a useful error.
    try:
        with open(logfile, 'r') as f:
            log_text = f.read()
    except OSError:
        log_text = '<logfile unavailable>'
    stop_openocd(serial=serial, tcl_port=tcl_port)
    raise Exception(
        f'OpenOCD failed to come up on tcl_port {tcl_port} '
        f'(last error: {last_err}):\n{log_text}'
    )


# --------------------------------------------------------------------------
# TCL/RPC client
# --------------------------------------------------------------------------

_TCL_TERMINATOR = b'\x1a'


class OpenOcdRpcError(Exception):
    """Raised when the OpenOCD TCL/RPC channel returns an error."""


class OpenOcdRpc:
    """Thin wrapper around OpenOCD's TCL/RPC port (default 6666).

    Each command is terminated by ``0x1a``; the response ends with the
    same byte. We open a fresh socket per command to keep the failure mode
    simple (no persistent connection state to babysit).
    """

    def __init__(self, host='127.0.0.1', port=6666, timeout=10.0):
        self.host = host
        self.port = port
        self.timeout = timeout

    def cmd(self, command, timeout=None):
        """Run *command* and return its stdout as a decoded string.

        Raises ``OpenOcdRpcError`` if the socket can't be reached.
        """
        deadline_timeout = timeout if timeout is not None else self.timeout
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=min(deadline_timeout, 5.0)
            ) as sock:
                sock.settimeout(deadline_timeout)
                payload = command.encode('utf-8') + _TCL_TERMINATOR
                sock.sendall(payload)
                chunks = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    if _TCL_TERMINATOR in chunk:
                        chunks.append(chunk.split(_TCL_TERMINATOR, 1)[0])
                        break
                    chunks.append(chunk)
        except (OSError, socket.timeout) as exc:
            raise OpenOcdRpcError(
                f'OpenOCD TCL RPC ({self.host}:{self.port}) error: {exc}'
            ) from exc
        return b''.join(chunks).decode('utf-8', errors='replace')

    # ---- Higher-level helpers ------------------------------------------------

    # OpenOCD's TCL/RPC channel does not surface TCL errors out-of-band — they
    # show up only as text in the response (lines starting with ``Error:`` or
    # the TCL backtrace form ``<file>:<line>: Error: ...``). Helpers below run
    # ``cmd()`` then call ``_check_for_tcl_error()`` to convert those into
    # ``OpenOcdRpcError`` so callers can't silently read past failures (the
    # bug class that made ``Flashed!`` print after a failed ``program``).
    _ERROR_LINE_RE = re.compile(r'(?m)^[^\n]*\bError:\s')

    @classmethod
    def _check_for_tcl_error(cls, out, *, command=None):
        """Raise ``OpenOcdRpcError`` if *out* contains an OpenOCD error line."""
        if cls._ERROR_LINE_RE.search(out):
            prefix = f'OpenOCD {command} failed' if command else 'OpenOCD command failed'
            raise OpenOcdRpcError(f'{prefix}:\n{out.rstrip()}')

    def cmd_checked(self, command, *, timeout=None, label=None):
        """Run *command* and raise ``OpenOcdRpcError`` on TCL ``Error:`` output.

        Use this for any command where the *response* — not just the socket —
        determines success. Returns the raw output on success.
        """
        out = self.cmd(command, timeout=timeout)
        self._check_for_tcl_error(out, command=label or command.split()[0])
        return out

    # ---- Reset / halt / resume ----------------------------------------------

    def reset(self, halt=False):
        return self.cmd_checked(
            'reset halt' if halt else 'reset run', timeout=30, label='reset',
        )

    def halt(self):
        return self.cmd_checked('halt', timeout=10, label='halt')

    def resume(self, address=None):
        cmd = 'resume' if address is None else f'resume {hex(address)}'
        return self.cmd_checked(cmd, timeout=10, label='resume')

    def wait_halt(self, timeout_ms=5000):
        """Block until the target halts (or *timeout_ms* expires).

        OpenOCD reports timeout as ``Error: timed out while waiting for target
        halted`` — surfaced as ``OpenOcdRpcError``.
        """
        # Add a generous margin to the socket timeout so the RPC layer doesn't
        # cut OpenOCD off mid-poll.
        sock_timeout = max(self.timeout, (timeout_ms / 1000.0) + 5.0)
        return self.cmd_checked(
            f'wait_halt {int(timeout_ms)}', timeout=sock_timeout, label='wait_halt',
        )

    def sleep_ms(self, ms):
        """OpenOCD ``sleep <ms>`` — host-side wait, target keeps running."""
        sock_timeout = max(self.timeout, (ms / 1000.0) + 5.0)
        return self.cmd_checked(
            f'sleep {int(ms)}', timeout=sock_timeout, label='sleep',
        )

    # ---- Memory access -------------------------------------------------------

    _MDW_VALUE_RE = re.compile(r'0x[0-9a-fA-F]+:\s*((?:[0-9a-fA-F]{8}\s*)+)')

    def mww(self, address, value):
        """Write a 32-bit word: ``mww <addr> <value>``."""
        return self.cmd_checked(
            f'mww {hex(int(address))} {hex(int(value) & 0xFFFFFFFF)}',
            timeout=10, label='mww',
        )

    def mwb(self, address, value):
        """Write a single byte: ``mwb <addr> <value>``."""
        return self.cmd_checked(
            f'mwb {hex(int(address))} {hex(int(value) & 0xFF)}',
            timeout=10, label='mwb',
        )

    def mdw(self, address, count=1):
        """Read *count* 32-bit words at *address*. Returns ``int`` for
        ``count == 1``, otherwise ``list[int]``.

        OpenOCD output looks like::

            0x20000000: 12345678
            0x20000000: 12345678 9abcdef0 fedcba98 76543210
        """
        out = self.cmd_checked(
            f'mdw {hex(int(address))} {int(count)}',
            timeout=15, label='mdw',
        )
        words = []
        for m in self._MDW_VALUE_RE.finditer(out):
            for token in m.group(1).split():
                words.append(int(token, 16))
        if not words:
            raise OpenOcdRpcError(
                f'OpenOCD mdw {hex(address)} returned no values:\n{out.rstrip()}'
            )
        if count == 1:
            return words[0]
        return words[:count]

    def load_image(self, file_path, address, fmt='bin'):
        """Load *file_path* into target memory at *address* via OpenOCD's
        ``load_image``. ``fmt`` is ``'bin'`` for raw binary; OpenOCD accepts
        ``elf``/``ihex``/``s19``/``mem``/``bin``.
        """
        return self.cmd_checked(
            f'load_image {file_path} {hex(int(address))} {fmt}',
            timeout=120, label='load_image',
        )

    # ---- Registers -----------------------------------------------------------

    _REG_VALUE_RE = re.compile(r':\s*(0x[0-9a-fA-F]+)')

    def reg_write(self, name, value):
        """Write *value* to the named register."""
        return self.cmd_checked(
            f'reg {name} {hex(int(value) & 0xFFFFFFFF)}',
            timeout=10, label='reg',
        )

    def reg_read(self, name):
        """Read the named register and return it as an ``int``.

        OpenOCD prints e.g. ``pc (/32): 0x12345678``.
        """
        out = self.cmd_checked(f'reg {name}', timeout=10, label='reg')
        m = self._REG_VALUE_RE.search(out)
        if not m:
            raise OpenOcdRpcError(
                f'OpenOCD reg {name} returned no value:\n{out.rstrip()}'
            )
        return int(m.group(1), 16)

    # ---- Breakpoints ---------------------------------------------------------

    def bp(self, address, length=4, hw=True):
        """Set a breakpoint at *address*. ``hw=True`` requests hardware."""
        suffix = ' hw' if hw else ''
        return self.cmd_checked(
            f'bp {hex(int(address))} {int(length)}{suffix}',
            timeout=10, label='bp',
        )

    def rbp(self, address):
        """Remove a breakpoint at *address*."""
        return self.cmd_checked(
            f'rbp {hex(int(address))}', timeout=10, label='rbp',
        )

    # ``bp`` (no args) lists active breakpoints, one per line, e.g.::
    #
    #     Breakpoint(IVA): 0x20001234, 0x4, hard
    #     Breakpoint: 0x20005678, 0x2, hard
    #
    # The lead-in word and the punctuation around the address vary slightly
    # between OpenOCD versions; the only reliable anchor is "Breakpoint"
    # followed (after some non-digit chars) by a hex address. Any line
    # without that combination is ignored.
    _BP_LIST_LINE_RE = re.compile(r'Breakpoint[^0-9]*0x([0-9a-fA-F]+)')

    def bp_list(self):
        """Return the list of active breakpoint addresses as ``list[int]``.

        Empty list when no breakpoints are set. Used by the DA1469x flash
        loader to clear out stale entries left behind when a previous
        bring-up hit ``wait_halt`` timeout — without this, the next attempt
        fails with ``Breakpoint at 0x... already exists`` because OpenOCD's
        internal bp table outlives ``reset halt``.
        """
        out = self.cmd_checked('bp', timeout=10, label='bp list')
        return [int(m.group(1), 16) for m in self._BP_LIST_LINE_RE.finditer(out)]

    # ---- Flash / program -----------------------------------------------------

    # OpenOCD's ``program_error`` proc (in startup.tcl) emits failure
    # markers of the form ``** <Something> Failed **`` (e.g.
    # ``** Programming Failed **``, ``** Verify Failed **``). ``program``
    # output legitimately contains echoes like ``** Programming Started **``,
    # so the asterisk markers are checked separately from the generic
    # ``Error:`` line scan to avoid false positives.
    _PROGRAM_FAILURE_RE = re.compile(r'\*\*\s.*Failed\s\*\*', re.IGNORECASE)

    def program(self, file_path, verify=True, reset_after=True, address=None):
        """``program`` runs flash erase + write + verify in one go.

        OpenOCD's ``program`` proc takes an optional address (for raw bin
        files) and the trailing ``verify``/``reset`` keywords.

        Raises ``OpenOcdRpcError`` if the response contains an OpenOCD
        ``program_error`` failure marker (``** ... Failed **``) or any
        TCL ``Error:`` line; otherwise returns the raw command output.
        """
        parts = ['program', file_path]
        if address is not None:
            parts.append(hex(address))
        if verify:
            parts.append('verify')
        if reset_after:
            parts.append('reset')
        out = self.cmd(' '.join(parts), timeout=300)
        if self._PROGRAM_FAILURE_RE.search(out):
            raise OpenOcdRpcError(
                f'OpenOCD program failed for {file_path}:\n{out.rstrip()}'
            )
        self._check_for_tcl_error(out, command=f'program {file_path}')
        return out

    def flash_erase_all(self):
        """Erase every sector of every flash bank — analog to JLink ``erase``.

        Some target configs declare multiple flash banks (STM32H7 in
        dual-bank mode, RP2350 with external + internal flash, some Cortex-M55s)
        and ``flash erase_sector 0 0 last`` only clears bank 0 — leaving
        stale code behind on the other banks. We enumerate every bank via
        ``flash banks`` and erase each in turn so the contract matches
        J-Link's ``chip_erase()`` ("the chip ends up blank").

        ``flash banks`` output looks like::

            #0 : stm32h7x.bank1 (stm32h7x) at 0x08000000, size 0x00100000, ...
            #1 : stm32h7x.bank2 (stm32h7x) at 0x08100000, size 0x00100000, ...

        We just need the leading ``#N`` index.
        """
        bank_indices = []
        try:
            banks_out = self.cmd_checked(
                'flash banks', timeout=10, label='flash banks',
            )
            for line in banks_out.splitlines():
                m = re.match(r'\s*#(\d+)\s*:', line)
                if m:
                    bank_indices.append(int(m.group(1)))
        except OpenOcdRpcError as exc:
            # No flash banks enumerable usually means the target.cfg has no
            # ``flash bank`` directive at all. Fall back to bank 0 / the
            # erase_address path; if those also fail we surface the TCL
            # error rather than printing a misleading "Erase complete!"
            # (the old behaviour silently swallowed both).
            logger.warning(
                'flash banks enumeration failed (%s); falling back to bank 0 only', exc,
            )

        # If enumeration produced nothing usable (older OpenOCD, exotic
        # target.cfg, or no flash bank declared), default to "bank 0 only".
        if not bank_indices:
            bank_indices = [0]

        outputs = []
        last_exc = None
        for bank in bank_indices:
            try:
                outputs.append(self.cmd_checked(
                    f'flash erase_sector {bank} 0 last',
                    timeout=120, label=f'flash erase_sector {bank}',
                ))
            except OpenOcdRpcError as exc:
                last_exc = exc
                logger.warning(
                    'flash erase_sector bank=%s failed (%s); trying erase_address fallback',
                    bank, exc,
                )
                try:
                    outputs.append(self.cmd_checked(
                        'flash erase_address 0 0xFFFFFFFF',
                        timeout=120, label='flash erase_address',
                    ))
                    # erase_address ignores the bank index and walks all
                    # banks itself on most target.cfgs, so one fallback is
                    # enough — bail out of the per-bank loop.
                    return '\n'.join(outputs)
                except OpenOcdRpcError as exc2:
                    last_exc = exc2
                    logger.warning('flash erase_address fallback failed: %s', exc2)
        if not outputs and last_exc is not None:
            # All paths failed — propagate the last error so callers don't
            # report a successful erase.
            raise last_exc
        return '\n'.join(outputs)

    def flash_erase_range(self, start, length):
        end = start + length - 1
        return self.cmd_checked(
            f'flash erase_address {hex(start)} {hex(end)}',
            timeout=120, label='flash erase_address',
        )

    def read_memory(self, address, length):
        """Read *length* bytes starting at *address*.

        OpenOCD's ``mdb`` prints one address per line:

            0x20000000: 12 34 56 78 9a bc de f0
            0x20000008: ...

        We tolerate extra whitespace/format jitter between OpenOCD versions.
        """
        # ``mdb`` is byte-granular; we need to halt for reliable reads on
        # most targets but OpenOCD itself enforces that — surface the
        # error to the caller if the target isn't halted.
        out = self.cmd(f'mdb {hex(address)} {length}', timeout=30)
        data = bytearray()
        for line in out.splitlines():
            line = line.strip()
            if not line or ':' not in line:
                continue
            # "0x20000000: 12 34 56 78  9a bc de f0  ASCII"
            _addr, _, rest = line.partition(':')
            for token in rest.split():
                if len(token) == 2 and all(c in '0123456789abcdefABCDEF' for c in token):
                    data.append(int(token, 16))
                else:
                    # Reached the ASCII column — stop parsing this line.
                    break
        return bytes(data[:length])

    def rtt_setup(self, search_addr=0x20000000, search_size=0x10000, id_str='SEGGER RTT'):
        return self.cmd(
            f'rtt setup {hex(search_addr)} {hex(search_size)} "{id_str}"'
        )

    def rtt_start(self):
        return self.cmd('rtt start')

    def rtt_server_start(self, port, channel=0):
        # OpenOCD: ``rtt server start <port> <channel>`` — opens a telnet
        # listener identical to J-Link's RTT telnet port, so the same
        # downstream socket-streaming code works.
        return self.cmd(f'rtt server start {port} {channel}')

    def rtt_server_stop(self, port):
        return self.cmd(f'rtt server stop {port}')


__all__ = [
    'get_openocd_exe_path',
    'get_openocd_script_dir',
    'interface_config_for_address',
    'target_config_for_device',
    'get_openocd_status',
    'stop_openocd',
    'start_openocd_gdbserver',
    'OpenOcdRpc',
    'OpenOcdRpcError',
]

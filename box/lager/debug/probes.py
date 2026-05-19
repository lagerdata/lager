# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Debug probe helpers — serial parsing, backend resolution, slot allocation, per-probe paths.

A debug net's ``address`` field stores the full VISA resource string for the
debug probe, e.g. ``USB0::0x1366::0x0101::000051014439::INSTR`` (J-Link) or
``USB0::0x0483::0x374B::066BFF...::INSTR`` (ST-Link v2-1). The fourth segment
is the USB serial number; the second is the vendor ID we use to pick the
backend (J-Link → ``jlink`` driver, everything else → ``openocd``).

Each running probe occupies one **slot** with its own port window:

* GDB protocol:  ``2331 + 3*slot``
* SWO output:    ``2332 + 3*slot`` (J-Link only; OpenOCD doesn't use it)
* Telnet I/O:    ``2333 + 3*slot`` (J-Link only)
* OpenOCD telnet: ``4444 + slot`` (OpenOCD only)
* OpenOCD TCL:    ``6666 + slot`` (OpenOCD only)
* RTT base:      ``9090 + 2*slot``

The 3-port stride for the J-Link GDB window exists because
``JLinkGDBServerCLExe``'s ``-swoport``/``-telnetport`` defaults (2332/2333)
are hardcoded — a stride of 1 collides them with the next slot's GDB port.
OpenOCD's ports are independent ranges so a stride of 1 is fine there.

Slot 0 is the legacy single-probe path: GDB on 2331, RTT on 9090, J-Link PID
file ``/tmp/jlink_gdbserver.pid``, OpenOCD PID file ``/tmp/openocd.pid``.
``serial=None`` always resolves to slot 0, preserving behaviour for nets
without a parseable address.
"""

import re

_VISA_RE = re.compile(
    # Serial slot may be empty for FTDI chips whose EEPROM was never
    # programmed (the scanner emits ``USB0::0x0403::0x6011::::INSTR`` in
    # that case). We still want VID/PID extraction to succeed so backend
    # resolution can pick OpenOCD vs J-Link; ``parse_probe_address``
    # normalises the empty serial to ``None``.
    r'USB\d*::0x([0-9A-Fa-f]+)::0x([0-9A-Fa-f]+)::([^:]*)::INSTR',
    re.IGNORECASE,
)

# Vendor IDs handled by each backend. Anything not in this map falls back to
# the J-Link backend so existing nets keep working when the VID is unknown.
#
# Only include VIDs we can map to an OpenOCD interface config without user
# help — otherwise auto-resolution lands the user on the OpenOCD backend and
# immediately fails because we have no ``interface/*.cfg`` to load. Keep
# this set in lockstep with ``openocd.interface_config_for_address``. Users
# with open-hw probes whose VIDs are NOT listed here (e.g. Black Magic Probe
# at 0x1209, Glasgow at 0x20b7) can still use the OpenOCD backend by setting
# ``debug_backend: openocd`` on the net and supplying ``openocd_config``.
_JLINK_VIDS = {'1366'}                  # SEGGER
_OPENOCD_VIDS = {
    '0483',  # STMicroelectronics — ST-Link v2 / v2-1 / v3
    '2e8a',  # Raspberry Pi — RP2040 Picoprobe / CMSIS-DAP
    '0403',  # FTDI — FT2232H / FT232H / FT4232H debug adapters
    '0d28',  # ARM — Cortex-M DAPLink / NXP MK20 CMSIS-DAP
    '03eb',  # Atmel — EDBG / mEDBG
    '15ba',  # Olimex — ARM-USB-OCD-H (FTDI-backed but distinct VID)
}

_GDB_PORT_BASE = 2331
_GDB_PORTS_PER_SLOT = 3  # GDB + SWO + Telnet, must not overlap across slots
_RTT_PORT_BASE = 9090
_RTT_PORTS_PER_SLOT = 2  # Two RTT channels per probe
_OPENOCD_TELNET_PORT_BASE = 4444
_OPENOCD_TCL_PORT_BASE = 6666
MAX_SLOTS = 4

_LEGACY_GDBSERVER_PIDFILE = '/tmp/jlink_gdbserver.pid'
_LEGACY_GDBSERVER_LOGFILE = '/tmp/jlink_gdbserver.log'
_LEGACY_JLINK_PIDFILE = '/tmp/jlink.pid'
_LEGACY_JLINK_LOGFILE = '/tmp/jlink.log'
_LEGACY_OPENOCD_PIDFILE = '/tmp/openocd.pid'
_LEGACY_OPENOCD_LOGFILE = '/tmp/openocd.log'


BACKEND_JLINK = 'jlink'
BACKEND_OPENOCD = 'openocd'

# FTDI vendor ID — chips with multiple MPSSE channels (FT2232H, FT4232H) need
# explicit channel selection in OpenOCD via ``ftdi channel <N>``.
_FTDI_VID = '0403'


def is_ftdi_vid(vid):
    """True iff *vid* is the FTDI vendor ID (case-insensitive, with or without
    leading zeros)."""
    if not vid:
        return False
    return str(vid).lower().zfill(4) == _FTDI_VID


# ----- Multi-channel FTDI: device@channel parsing ------------------------------

# Map interface letters (A/B/C/D) to the FTDI channel index (0/1/2/3) that
# OpenOCD expects in ``ftdi channel <N>``.
_CHANNEL_LETTER_TO_INDEX = {'A': 0, 'B': 1, 'C': 2, 'D': 3}


def parse_device_field(device):
    """Split ``device`` into ``(target, channel)``.

    Lager debug nets store the target MCU in a single string field. To let
    users pick which interface on a multi-channel FTDI a probe is wired to,
    we accept an optional ``@<channel>`` suffix:

    * ``"STM32F4x"``       -> ``("STM32F4x", None)``
    * ``"STM32F4x@A"``     -> ``("STM32F4x", 0)``    (interface letter)
    * ``"STM32F4x@0"``     -> ``("STM32F4x", 0)``    (raw index)
    * ``"STM32F4x@3"``     -> ``("STM32F4x", 3)``

    ``channel`` is always an int 0..3 when present; otherwise ``None`` (the
    backend leaves OpenOCD on its default — almost always channel 0 / A).

    Anything we can't parse is returned as ``(device, None)`` so unknown
    formats fall through to the default channel.
    """
    if not device or not isinstance(device, str):
        return (device, None)
    if '@' not in device:
        return (device, None)
    target, _, raw = device.rpartition('@')
    target = target.strip()
    raw = raw.strip()
    if not target or not raw:
        return (device, None)
    upper = raw.upper()
    if upper in _CHANNEL_LETTER_TO_INDEX:
        return (target, _CHANNEL_LETTER_TO_INDEX[upper])
    if raw.isdigit():
        idx = int(raw)
        if 0 <= idx <= 3:
            return (target, idx)
    return (device, None)


def parse_probe_address(address):
    """Return (vid, pid, serial) from a VISA address, or (None, None, None)."""
    if not address or not isinstance(address, str):
        return (None, None, None)
    match = _VISA_RE.match(address.strip())
    if not match:
        return (None, None, None)
    vid = match.group(1).lower().lstrip('0') or '0'
    # Re-pad to 4 chars so we can compare against the static maps consistently.
    vid = vid.zfill(4)
    pid = match.group(2).lower().zfill(4)
    serial = match.group(3).strip() or None
    return (vid, pid, serial)


def parse_jlink_serial(address):
    """Legacy: return USB serial only if *address* is a J-Link probe.

    Kept for back-compat with callers that still want J-Link-only behaviour.
    Prefer ``parse_probe_address`` for anything new.
    """
    vid, _pid, serial = parse_probe_address(address)
    if vid in _JLINK_VIDS:
        return serial
    return None


def parse_probe_serial(address):
    """Return the USB serial for *any* recognised debug probe, or None."""
    _vid, _pid, serial = parse_probe_address(address)
    return serial


def resolve_backend(net):
    """Return ``'jlink'`` or ``'openocd'`` for a net dict.

    Resolution order:

    1. ``net['debug_backend']`` if explicitly set (escape hatch for users
       whose probe VID we can't auto-classify).
    2. VID from ``net['address']`` mapped through the static tables above.
    3. Default to J-Link for back-compat with existing nets / fixtures.
    """
    if isinstance(net, dict):
        explicit = (net.get('debug_backend') or '').strip().lower()
        if explicit in (BACKEND_JLINK, BACKEND_OPENOCD):
            return explicit
        vid, _pid, _serial = parse_probe_address(net.get('address'))
        if vid in _OPENOCD_VIDS:
            return BACKEND_OPENOCD
        if vid in _JLINK_VIDS:
            return BACKEND_JLINK
    return BACKEND_JLINK


def resolve_serial_from_net(net):
    """Return the probe USB serial for a net dict, or None.

    Recognises any supported debug probe VID, not just J-Link.
    """
    if not isinstance(net, dict):
        return None
    return parse_probe_serial(net.get('address'))


def compute_slot(serial, all_serials):
    """Deterministic slot 0..MAX_SLOTS-1 for *serial*.

    Slots are assigned by sorted order of *all_serials* so they survive service
    restarts. Returns 0 when *serial* is None or not present (legacy path).
    """
    if serial is None:
        return 0
    sorted_serials = sorted(s for s in all_serials if s)
    try:
        return sorted_serials.index(serial)
    except ValueError:
        return 0


def gdb_port_for_slot(slot):
    """GDB protocol port for *slot*. Stride is 3 to leave room for SWO + Telnet."""
    return _GDB_PORT_BASE + slot * _GDB_PORTS_PER_SLOT


def swo_port_for_slot(slot):
    """SWO raw output port for *slot* (gdb_port + 1 by JLinkGDBServer convention)."""
    return _GDB_PORT_BASE + slot * _GDB_PORTS_PER_SLOT + 1


def telnet_port_for_slot(slot):
    """Terminal I/O port for *slot* (gdb_port + 2 by JLinkGDBServer convention)."""
    return _GDB_PORT_BASE + slot * _GDB_PORTS_PER_SLOT + 2


def rtt_port_for_slot(slot):
    """Base RTT telnet port for *slot*; channel N adds 0..(_RTT_PORTS_PER_SLOT-1)."""
    return _RTT_PORT_BASE + slot * _RTT_PORTS_PER_SLOT


def jlink_gdbserver_pidfile(serial):
    if serial is None:
        return _LEGACY_GDBSERVER_PIDFILE
    return f'/tmp/jlink_gdbserver_{serial}.pid'


def jlink_gdbserver_logfile(serial):
    if serial is None:
        return _LEGACY_GDBSERVER_LOGFILE
    return f'/tmp/jlink_gdbserver_{serial}.log'


def jlink_pidfile(serial):
    if serial is None:
        return _LEGACY_JLINK_PIDFILE
    return f'/tmp/jlink_{serial}.pid'


def jlink_logfile(serial):
    if serial is None:
        return _LEGACY_JLINK_LOGFILE
    return f'/tmp/jlink_{serial}.log'


# ---- OpenOCD per-probe helpers -------------------------------------------------

def openocd_telnet_port_for_slot(slot):
    """OpenOCD interactive telnet port (default 4444); unique per slot."""
    return _OPENOCD_TELNET_PORT_BASE + slot


def openocd_tcl_port_for_slot(slot):
    """OpenOCD TCL/RPC port (default 6666); unique per slot. We dispatch all
    runtime commands (flash/erase/reset/memrd) through this port, so it MUST
    be unique across concurrent OpenOCD instances on the same box."""
    return _OPENOCD_TCL_PORT_BASE + slot


def openocd_pidfile(serial):
    if serial is None:
        return _LEGACY_OPENOCD_PIDFILE
    return f'/tmp/openocd_{serial}.pid'


def openocd_logfile(serial):
    if serial is None:
        return _LEGACY_OPENOCD_LOGFILE
    return f'/tmp/openocd_{serial}.log'

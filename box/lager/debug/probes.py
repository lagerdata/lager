# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
J-Link probe helpers — serial parsing, slot allocation, per-probe paths.

A debug net's ``address`` field stores the full VISA resource string for the
J-Link probe, e.g. ``USB0::0x1366::0x0101::000051014439::INSTR``. The fourth
segment is the J-Link USB serial number. These helpers extract it and turn
it into deterministic per-probe paths and ports so multiple J-Links can run
concurrently on the same box.

Slot 0 is the legacy single-probe path: GDB on 2331, RTT on 9090, PID file
``/tmp/jlink_gdbserver.pid``. ``serial=None`` always resolves to slot 0,
preserving behaviour for nets without a parseable address.
"""

import re

# VISA J-Link resource: USB[0]::0x1366::0x<product>::<serial>::INSTR
_VISA_JLINK_RE = re.compile(
    r'USB\d*::0x1366::0x[0-9A-Fa-f]+::([^:]+)::INSTR',
    re.IGNORECASE,
)

_GDB_PORT_BASE = 2331
_RTT_PORT_BASE = 9090
_RTT_PORTS_PER_SLOT = 2  # Two RTT channels per probe
MAX_SLOTS = 4

_LEGACY_GDBSERVER_PIDFILE = '/tmp/jlink_gdbserver.pid'
_LEGACY_GDBSERVER_LOGFILE = '/tmp/jlink_gdbserver.log'
_LEGACY_JLINK_PIDFILE = '/tmp/jlink.pid'
_LEGACY_JLINK_LOGFILE = '/tmp/jlink.log'


def parse_jlink_serial(address):
    """Return the J-Link USB serial in *address*, or None if unparseable."""
    if not address or not isinstance(address, str):
        return None
    match = _VISA_JLINK_RE.match(address.strip())
    if not match:
        return None
    serial = match.group(1).strip()
    return serial or None


def resolve_serial_from_net(net):
    """Return the J-Link serial for a net dict, or None."""
    if not isinstance(net, dict):
        return None
    return parse_jlink_serial(net.get('address'))


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
    return _GDB_PORT_BASE + slot


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

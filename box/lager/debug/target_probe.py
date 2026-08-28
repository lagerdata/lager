# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Does the part answer, as distinct from: is a gdbserver process alive.

`/debug/status` reported one boolean, `connected`, and it meant "the gdbserver
PID is alive". Every debug subcommand short-circuited on it, so on a box where
the server outlives the target, `flash`, `erase`, `reset` and `memrd` all
proceeded against hardware that was not there believing they were connected.
#344 fixed the erase verdict at one call site by reading the programmer's
output; the shared cause is that the CLI could not tell a live session from an
attached part.

This module answers the second question. It returns a tri-state on purpose:

    True   the target answered
    False  it demonstrably did not
    None   could not be established

None is not False. An older box that does not probe, a gdbserver that refuses a
second GDB client because a user is already attached, a probe that times out --
none of those are evidence the part is absent, and treating them as such would
turn a working session into a spurious reconnect. Callers must test `is True`
before trusting an attachment, and must not treat None as a failure.
"""

import logging
import os
import re

from .api import _attach_failed
from .probes import jlink_gdbserver_logfile, openocd_logfile

logger = logging.getLogger(__name__)

# Cortex-M SCB CPUID. Readable over AHB-AP while the core runs, so probing it
# neither halts the target nor perturbs a running test.
CPUID_ADDRESS = 0xE000ED00

# `x/4xb <addr>` renders as an address, a colon, then the bytes. A target that
# is not attached answers with an error instead, or with nothing.
# Tolerant on purpose: pygdbmi hands back console payloads whose tabs may be
# escaped rather than literal, and the symbol annotation (`<SCB>`) is present
# only when the target has symbols loaded. All that must be recognised is
# "an address, a colon, and at least one byte".
_MEMORY_ROW_RE = re.compile(r'0x[0-9a-f]+[^:\n]*:.*0x[0-9a-f]{2}', re.IGNORECASE)
_MEMORY_ERROR_RE = re.compile(
    r'cannot access memory'
    r'|memory access error'
    r'|target is not responding'
    r'|no such target'
    r'|remote communication error',
    re.IGNORECASE,
)


def _log_says_attach_failed(backend, serial):
    """Cheap check: did the server already record that it never attached?

    Reuses #344's predicate rather than a second copy of the same regexes.
    `get_jlink_status` has consulted the logfile since before this existed;
    `/debug/status` never did, which is why a failed attach could still read as
    connected.
    """
    logfile = openocd_logfile(serial) if backend == 'openocd' else jlink_gdbserver_logfile(serial)
    try:
        if not os.path.exists(logfile):
            return False
        with open(logfile, 'r', errors='replace') as handle:
            return _attach_failed([handle.read()])
    except Exception as exc:
        logger.debug('Could not read %s: %s', logfile, exc)
        return False


def _probe_over_gdb(device, gdb_port):
    """Read the CPUID through the running gdbserver. Tri-state."""
    try:
        from .gdb import read_memory
        responses = read_memory(CPUID_ADDRESS, 4, device=device, port=gdb_port)
    except Exception as exc:
        # A refused or unreachable controller is not evidence about the target.
        logger.debug('CPUID probe could not run on port %s: %s', gdb_port, exc)
        return None

    text = '\n'.join(
        str(r.get('payload', '')) for r in (responses or []) if isinstance(r, dict)
    )
    if _MEMORY_ERROR_RE.search(text):
        return False
    if _MEMORY_ROW_RE.search(text):
        return True
    return None


def _probe_over_openocd(tcl_port):
    """Read the CPUID over OpenOCD's TCL/RPC. Tri-state."""
    try:
        from .openocd import OpenOcdRpc
        with OpenOcdRpc(port=tcl_port) as rpc:
            out = rpc.mdw(CPUID_ADDRESS, 1)
    except Exception as exc:
        logger.debug('CPUID probe could not run on TCL port %s: %s', tcl_port, exc)
        return None

    # An empty RPC reply is a known and still-open OpenOCD behaviour; it says
    # nothing about the target, so it must not read as absent.
    if not out or not str(out).strip():
        return None
    text = str(out)
    if _MEMORY_ERROR_RE.search(text):
        return False
    if re.search(r'0x[0-9a-f]{8}', text, re.IGNORECASE):
        return True
    return None


def target_attached(backend, serial, gdb_port=None, tcl_port=None, device=None, probe=False):
    """Whether the part answers. See the module docstring for the tri-state.

    `probe` gates the wire access. `/debug/status` is called by every debug
    subcommand through `_auto_connect_if_needed`, and opening a GDB controller
    costs on the order of a second, so the default stays free: the logfile is
    consulted either way, and the live read happens only when a caller asks for
    it.
    """
    if _log_says_attach_failed(backend, serial):
        return False
    if not probe:
        return None
    if backend == 'openocd':
        if tcl_port is None:
            return None
        return _probe_over_openocd(tcl_port)
    if gdb_port is None:
        return None
    return _probe_over_gdb(device, gdb_port)

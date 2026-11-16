# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Run the Box's debug service natively on macOS.

The debug service is imported and adjusted from outside; none of its files
are edited. Three things differ on a Mac, each found on real hardware:

* SEGGER's tools live under ``/Applications/SEGGER/JLink``.
* J-Link Commander stops at "Connecting to J-Link via USB..." waiting on a
  dialog unless it is started with ``-NoGui 1``. A wrapper adds the flag.
* The service learns a GDB server's command line from ``/proc/<pid>``, which
  macOS does not have. The modules that do this get an ``open`` of their own
  that answers those two paths from ``ps`` and passes everything else on.
* RTT auto-detection starts ``gdb-multiarch``, a Debian name. The ARM GDB
  that is installed is started instead.
"""
from __future__ import annotations

import builtins
import io
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

logger = logging.getLogger(__name__)

SEGGER_DIRS = ("/Applications/SEGGER/JLink",)
# Debian's gdb-multiarch has no macOS counterpart; these do the same job.
GDB_NAMES = ("gdb-multiarch", "arm-none-eabi-gdb")
# A service started by launchd does not get Homebrew on its PATH.
CONNECT_RESULT_WAIT_S = 15.0
GDB_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
_PROC_PATH = re.compile(r"^/proc/(\d+)/(cmdline|stat)$")


def _ps(pid: str, fields: str) -> str:
    result = subprocess.run(["ps", "-o", fields, "-p", pid],
                            capture_output=True, text=True, timeout=5.0, check=False)
    out = result.stdout.strip()
    if result.returncode != 0 or not out:
        raise FileNotFoundError(2, "No such process", f"/proc/{pid}")
    return out


def proc_cmdline(pid: str) -> bytes:
    """``/proc/<pid>/cmdline``: the arguments, NUL-separated.

    ``ps`` joins arguments with spaces, so one that itself contains a space
    comes back split. The tools this is used for are started by the debug
    service with paths that have none.
    """
    return b"\0".join(part.encode() for part in _ps(pid, "args=").split()) + b"\0"


def proc_stat(pid: str) -> str:
    """``/proc/<pid>/stat``, as far as it is read: state and parent pid."""
    state, ppid = _ps(pid, "state=,ppid=").split()[:2]
    return f"{pid} (process) {state[0]} {ppid}"


def proc_open(file, mode="r", *args, **kwargs):
    """``open`` for a module that reads ``/proc``; anything else is untouched."""
    match = _PROC_PATH.match(file) if isinstance(file, str) else None
    if match is None:
        return builtins.open(file, mode, *args, **kwargs)
    pid, what = match.groups()
    if what == "cmdline":
        data = proc_cmdline(pid)
        return io.BytesIO(data) if "b" in mode else io.StringIO(data.decode())
    text = proc_stat(pid)
    return io.BytesIO(text.encode()) if "b" in mode else io.StringIO(text)


def commander_wrapper(real: str, directory: str) -> str:
    """A script that runs J-Link Commander without its dialogs."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "JLinkExe")
    with builtins.open(path, "w", encoding="utf-8") as fh:
        fh.write(f'#!/bin/sh\nexec "{real}" -NoGui 1 "$@"\n')
    os.chmod(path, 0o700)
    return path


def find_gdb():
    """A GDB that understands ARM targets, or None."""
    for name in GDB_NAMES:
        found = shutil.which(name)
        if found:
            return found
        for directory in GDB_DIRS:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return path
    return None


def _gdb_controller(real_controller, gdb_path):
    """``GdbController`` that starts ``gdb_path`` where ``gdb-multiarch`` is asked for."""
    def controller(command=None, *args, **kwargs):
        if command and command[0] == "gdb-multiarch":
            command = [gdb_path] + list(command[1:])
        gdbmi = real_controller(command, *args, **kwargs)
        _wait_for_connect_result(gdbmi)
        return gdbmi
    return controller


def _wait_for_connect_result(gdbmi) -> None:
    """Make ``write('tar ext ...')`` return GDB's verdict, not just its first words.

    The Box code decides how to proceed from the result of the connect
    command. GDB 18 takes about six seconds to refuse non-stop mode against
    J-Link, long after the first output that ends an ordinary ``write``, so
    without this the refusal is missed and every later command fails.
    """
    real_write = gdbmi.write

    def write(command, *args, **kwargs):
        responses = real_write(command, *args, **kwargs)
        if isinstance(command, str) and command.startswith("tar ext"):
            deadline = time.monotonic() + CONNECT_RESULT_WAIT_S
            while (time.monotonic() < deadline
                   and not any(r.get("type") == "result" for r in responses)):
                responses += gdbmi.get_gdb_response(
                    timeout_sec=1.0, raise_error_on_timeout=False)
        return responses

    gdbmi.write = write


def install(wrapper_dir=None) -> None:
    """Adjust the already-importable debug modules for macOS."""
    from lager.debug import api, gdbserver, jlink, mappings, service

    for module in (api, mappings, service):
        module.open = proc_open

    from lager.debug import gdb

    gdb_path = find_gdb()
    if gdb_path:
        gdb.GdbController = _gdb_controller(gdb.GdbController, gdb_path)
    else:
        logger.warning("no ARM GDB found (%s); RTT auto-detection will not work",
                       ", ".join(GDB_NAMES))

    wrapper_dir = wrapper_dir or os.path.join(
        tempfile.gettempdir(), f"lager-host-{os.getuid()}")
    for directory in SEGGER_DIRS:
        commander = os.path.join(directory, "JLinkExe")
        if os.path.exists(commander):
            jlink.JLINK_EXE_PATHS.insert(0, commander_wrapper(commander, wrapper_dir))
        server = os.path.join(directory, "JLinkGDBServerCLExe")
        if os.path.exists(server):
            gdbserver.JLINK_GDB_SERVER_PATHS.insert(0, server)
    if jlink.get_jlink_exe_path() is None:
        logger.warning("J-Link software not found in %s", ", ".join(SEGGER_DIRS))


def serve(port: int) -> None:
    from lager.debug import service
    from lager.hostlink import config

    install()
    # The service's own handler calls server.shutdown() from the thread that
    # is serving, which waits on itself and never returns. Under Docker that
    # is hidden by the kill that follows ten seconds later; here, leave.
    service.DebugService._handle_signal = lambda self, signum, frame: sys.exit(0)
    service.DebugService(host=config.DEFAULT_BIND, port=port).start()

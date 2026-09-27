#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Drive an interactive ``lager uart`` session from a script.

``lager uart --interactive`` requires BOTH stdin and stdout to be terminals
(cli/commands/communication/uart.py:512) and exits 1 otherwise, so a shell
test cannot pipe commands into it -- and cannot pipe its output to ``tee``
either, since that makes stdout a pipe. This allocates a pty, runs the session
on it, types the commands, and prints what came back on real stdout for the
caller to assert on.

Usage:
    uart_pty_driver.py --net NET --box BOX [--baud N] [--settle S]
                       [--deadline S] CMD [CMD ...]

Exit codes:
    0  session ran and its output was captured (says nothing about content)
    2  the CLI refused to start the session (bad net, held net, no TTY, ...)
    3  the session ignored SIGINT and had to be killed -- the net may still be
       held box-side, so the next run can fail for that reason alone

Exit 2 matters: it is how a caller tells "the CLI would not start" apart from
"the device did not answer". Reporting the first as the second is how a broken
test comes to look like absent hardware.
"""
from __future__ import annotations

import argparse
import errno
import os
import pty
import select
import signal
import subprocess
import sys
import time

# How the session is asked to stop.
#
# SIGINT, not a 0x03 byte. The client's stdin reader does treat 0x03 as Ctrl-C,
# but the byte never reaches it here: the client puts the tty in cbreak mode,
# which leaves ISIG enabled, so the line discipline intercepts 0x03 and signals
# the terminal's foreground process group. The child is not a session leader
# with this pty as its controlling terminal, so there is no such group and the
# byte is swallowed -- the session then runs until the deadline.
#
# SIGINT reaches the same place by a shorter route: it raises KeyboardInterrupt
# in the client's main thread, whose `finally` calls _release_box_session().
# That is the part that matters -- killing the process instead leaves the net
# held and the next run fails with "already in use by another session".
STOP_SIGNAL = signal.SIGINT

# Output is considered complete once this long passes with nothing arriving.
# Pacing on silence rather than a fixed delay keeps a slow reply from being
# overtaken by the next command, which interleaves the transcript.
QUIET_PERIOD = 0.35

# Printed by the client once the session is up. Waiting for it rather than
# sleeping a fixed time keeps a slow box from silently eating the commands.
READY_MARKER = "Connected to"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", required=True)
    ap.add_argument("--box", required=True)
    ap.add_argument("--baud", default="115200")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds to keep reading after the last command")
    ap.add_argument("--deadline", type=float, default=45.0)
    ap.add_argument("--ready-timeout", type=float, default=20.0)
    ap.add_argument("commands", nargs="+")
    args = ap.parse_args()

    cmd = ["lager", "uart", args.net, "-i",
           "--baudrate", str(args.baud), "--box", args.box]

    master, slave = pty.openpty()
    # start_new_session so the child leads its own process group: `lager` is a
    # pyenv bash shim that execs through `pyenv exec` to the real entry point,
    # and signalling a single pid in that chain does not reliably reach the
    # Python process. Signalling the group does, whatever the chain looks like.
    proc = subprocess.Popen(cmd, stdin=slave, stdout=slave, stderr=slave,
                            close_fds=True, start_new_session=True)
    os.close(slave)

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None

    captured = bytearray()
    started = time.monotonic()

    def pump(until: float) -> None:
        """Read whatever is available until *until*, tolerating EOF."""
        while time.monotonic() < until:
            try:
                ready, _, _ = select.select([master], [], [], 0.1)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                # EIO is the normal read error once the child has closed its
                # side of the pty; anything else is worth surfacing.
                if exc.errno == errno.EIO:
                    return
                raise
            if not chunk:
                return
            captured.extend(chunk)

    def pump_until_quiet(limit: float) -> None:
        """Read until QUIET_PERIOD passes with no new bytes, or *limit*."""
        last_len = len(captured)
        last_change = time.monotonic()
        while time.monotonic() < limit:
            pump(min(time.monotonic() + 0.1, limit))
            if len(captured) != last_len:
                last_len = len(captured)
                last_change = time.monotonic()
            elif time.monotonic() - last_change >= QUIET_PERIOD:
                return

    try:
        # 1. Wait for the session to come up, so commands are not typed into a
        #    client that has not attached to the device yet.
        ready_by = started + args.ready_timeout
        while time.monotonic() < ready_by:
            pump(min(time.monotonic() + 0.2, ready_by))
            if READY_MARKER in captured.decode("utf-8", "replace"):
                break
            if proc.poll() is not None:
                break

        text = captured.decode("utf-8", "replace")
        if READY_MARKER not in text:
            sys.stdout.write(text)
            sys.stdout.flush()
            return 2

        # 2. Type the commands, waiting for each reply to finish arriving
        #    before sending the next. The client's stdin reader suppresses one
        #    inbound line per line SENT, and `suppress_next_line` is a boolean
        #    rather than a counter, so commands delivered faster than the
        #    replies come back arm it unevenly and interleave the transcript.
        for line in args.commands:
            os.write(master, line.encode() + b"\n")
            pump_until_quiet(time.monotonic() + 5.0)

        # 3. Let any trailing output land.
        pump(time.monotonic() + args.settle)

        # 4. Ask for a clean stop, escalating until it actually stops.
        #
        # SIGINT first, because that is the path that prints "Disconnected"
        # and calls _release_box_session(). SIGTERM, then SIGKILL, only if it
        # will not go. A SIGKILL is survivable -- the box releases the net on
        # the socket.io disconnect, and the bridge's flock drops with the
        # process -- but it skips the explicit release, so it is reported.
        def signal_group(sig) -> None:
            try:
                if pgid is not None:
                    os.killpg(pgid, sig)
                else:
                    proc.send_signal(sig)
            except (OSError, ProcessLookupError):
                pass

        clean = False
        for sig, grace in ((STOP_SIGNAL, 8.0), (signal.SIGTERM, 5.0)):
            signal_group(sig)
            deadline_at = time.monotonic() + grace
            while time.monotonic() < deadline_at:
                pump(time.monotonic() + 0.2)
                if proc.poll() is not None:
                    clean = True
                    break
            if clean:
                break

        if not clean:
            signal_group(signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            sys.stdout.write(captured.decode("utf-8", "replace"))
            sys.stdout.flush()
            return 3
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        # Backstop for the early-return paths (exit 2/3) and for exceptions.
        # Kill the group, not just the pid: the pyenv shim chain means a
        # surviving descendant would otherwise keep holding the net.
        if proc.poll() is None:
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    proc.kill()
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    sys.stdout.write(captured.decode("utf-8", "replace"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

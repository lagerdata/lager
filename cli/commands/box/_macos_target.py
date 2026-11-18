# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Commands the CLI would run on a Box over SSH, when the Box is a Mac.

Two things differ from a Linux Box, and both are decided here so that the
commands themselves stay as they are:

* A Box installed on the Mac the CLI runs on is not reached over SSH at all
  (Remote Login is off by default, and there is no ``lagerdata`` user). The
  command runs in a local shell instead.
* Some commands manage the Linux host: apt packages, sysctl, udev rules,
  systemd units, sudoers. Run on a Mac they would fail obscurely or touch
  the user's machine, so they are refused by name with a plain message.

For any Box that is not a Mac :func:`run` returns None and the caller goes
on to SSH exactly as before.
"""
import re
import subprocess
from typing import Optional, Tuple

LINUX_ONLY = re.compile(
    r"(?<![\w.-])(apt-get|apt|dpkg|sysctl|udevadm|systemctl|journalctl|ufw)(?![\w.-])"
    r"|/etc/(udev|sysctl\.d|sudoers\.d|systemd)\b|start_box\.sh")
REFUSED_STATUS = 1


def _is_local(box_ip: str) -> bool:
    from ..utility._local_install import is_local_target
    return is_local_target(box_ip)


def is_macos_box(box_ip: str) -> bool:
    """True for the Box on this Mac, or a saved Box installed as a Mac."""
    if _is_local(box_ip):
        return True
    from ..utility._remote_macos import saved_macos_box
    return saved_macos_box(box_ip)


def refusal(cmd: str) -> Optional[str]:
    """Why ``cmd`` cannot run on a Mac, or None when it can."""
    found = LINUX_ONLY.search(cmd or "")
    if found is None:
        return None
    what = (found.group(1) or found.group(0)).strip("/")
    return (f"This manages the Linux host of a Box ({what}) and is not supported "
            "for a Box on macOS.")


def run(box_ip: str, cmd: str, *, stdin: Optional[str] = None, timeout: int = 60,
        runner=None) -> Optional[Tuple[int, str, str]]:
    """``(rc, stdout, stderr)`` when this module handles ``cmd``; else None.

    None for every Box that is not a Mac, and for a remote Mac running a
    command that is fine there: the caller then uses SSH as it always has.
    """
    if not is_macos_box(box_ip):
        return None
    why = refusal(cmd)
    if why is not None:
        return REFUSED_STATUS, "", why
    if not _is_local(box_ip):
        return None
    try:
        proc = (runner or subprocess.run)(["/bin/sh", "-c", cmd], input=stdin, capture_output=True,
                      text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return REFUSED_STATUS, "", f"the command timed out after {timeout}s on this Mac"
    return proc.returncode, proc.stdout, proc.stderr


def local_shell_argv(box_ip: str, command=(), environ=None) -> Optional[list]:
    """What `lager ssh` runs for the Box on this Mac; None for any other Box.

    The user's own shell, interactive with no command, or running the
    command the way ``ssh host <command>`` would have.
    """
    if not _is_local(box_ip):
        return None
    import os
    import shlex
    shell = (os.environ if environ is None else environ).get("SHELL") or "/bin/sh"
    if not command:
        return [shell, "-l"]
    return [shell, "-c", " ".join(shlex.quote(arg) for arg in command)]

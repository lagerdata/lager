# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Shared SSH transport for `lager box config` host-side operations.

A single default runner (with optional stdin), and a parameterized
sudo-error helper. Callers (mount prep, apply-time host ops, container
bounce) inject custom runners for unit tests; production code uses the
default. Keeps the BatchMode + user-resolution policy in one place so
fixes don't have to be made three times.
"""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Optional, Tuple

SshRunner = Callable[..., Tuple[int, str, str]]

# Dedicated key used by `lager install` / `lager update`. When present,
# all `lager box config` SSH calls use it too — without this, host-side
# apt/sysctl/bounce calls fall back to the user's default key, which
# isn't authorized on the box and the operation fails with
# "Permission denied (publickey,password)" even though other lager
# commands work fine.
_LAGER_BOX_KEY = os.path.expanduser("~/.ssh/lager_box")


def default_ssh_runner(
    box_ip: str,
    cmd: str,
    *,
    stdin: Optional[str] = None,
    timeout: int = 60,
) -> Tuple[int, str, str]:
    """Run `cmd` on the box over SSH and return (rc, stdout, stderr).

    BatchMode=yes refuses to prompt, so a missing key fails fast instead
    of hanging. User defaults to "lagerdata" when the box record has no
    explicit user. `stdin` is piped to the remote command's stdin when
    supplied — used by sysctl_apply to ship the conf body to `sudo tee`
    without expanding metacharacters through the shell.
    """
    # `box_ip` is already a resolved IP (see _resolve_box). get_box_user
    # is keyed by name, so reverse-look the name first; otherwise every
    # box with a custom user silently falls back to "lagerdata".
    from ...box_storage import get_box_name_by_ip, get_box_user
    name = get_box_name_by_ip(box_ip)
    user = (get_box_user(name) if name else None) or "lagerdata"
    ssh_cmd = ["ssh", "-o", "BatchMode=yes"]
    if os.path.exists(_LAGER_BOX_KEY):
        ssh_cmd.extend(["-i", _LAGER_BOX_KEY])
    ssh_cmd.extend([f"{user}@{box_ip}", cmd])
    proc = subprocess.run(
        ssh_cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def sudo_error_message(
    stderr: str,
    *,
    base_text: str,
    bootstrap_text: str,
) -> str:
    """Format a sudo-failure message for the caller's HostOpResult / PrepResult.

    Returns raw stderr for non-sudo errors (so e.g. apt's "Unable to
    locate package" isn't buried under bootstrap noise). For sudo
    errors, returns `base_text` plus the caller's `bootstrap_text` —
    the sudoers rule the caller needs is feature-specific (mount prep
    needs only mkdir+chown; apply needs apt/sysctl/tee/rm too), so the
    bootstrap is passed in rather than hard-coded here.
    """
    err = (stderr or "").strip()
    if err and "a password is required" not in err.lower() and "sudo:" not in err.lower():
        return err
    out = base_text
    if err:
        out += f" Stderr: {err}"
    return out + "\n\n" + bootstrap_text

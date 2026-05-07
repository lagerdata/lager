# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Auto-prep helper for `lager box config` mounts.

The lager container runs as uid 33 (www-data). For every host->container
bind mount, the host path must (a) exist and (b) for read-write mounts,
be writable by uid 33. Without this, the mount succeeds but anything the
container tries to write under it gets EACCES.

`ensure_host_path_owned` SSHes to the box, classifies the current state
of the host path, and runs `sudo -n mkdir`/`sudo -n chown` to make it
correct. The result is a structured PrepResult so callers can render
green-path messages, refuse on destructive cases, and surface manual
fallbacks when sudo is unavailable.

Pure logic; the SSH transport is injected so unit tests can drive every
branch without a real box.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

CONTAINER_UID = 33
CONTAINER_GID = 33
EXPECTED_OWNER = f"{CONTAINER_UID}:{CONTAINER_GID}"

SshRunner = Callable[[str, str], Tuple[int, str, str]]


@dataclass
class PrepResult:
    ok: bool
    action: str
    host_path: str
    current_owner: Optional[str] = None
    is_populated: bool = False
    message: str = ""
    manual_fix: Optional[str] = None


def manual_fix_command(host_path: str, *, recursive: bool = False) -> str:
    """Copy-pasteable shell command the user can run on the box."""
    flag = " -R" if recursive else ""
    quoted = shlex.quote(host_path)
    return f"sudo mkdir -p {quoted} && sudo chown{flag} {EXPECTED_OWNER} {quoted}"


def _default_ssh_runner(box_ip: str, cmd: str) -> Tuple[int, str, str]:
    from ...box_storage import get_box_user

    user = get_box_user(box_ip) or "lagerdata"
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", f"{user}@{box_ip}", cmd],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def ensure_host_path_owned(
    box_ip: str,
    host_path: str,
    *,
    readonly: bool = False,
    recursive: bool = False,
    ssh_runner: Optional[SshRunner] = None,
) -> PrepResult:
    """Make the host path safe for the container to bind-mount.

    For RW mounts: ensure the path exists and is owned by uid 33:33.
    For RO mounts: ensure the path exists. Ownership doesn't affect
    readability for the container, so we don't chown.

    If the path exists with wrong ownership and contains files, we refuse
    unless `recursive=True` is passed — recursive chown is destructive
    and must be explicit.

    Returns:
        PrepResult — `ok` False means the caller should abort and surface
        `message` plus `manual_fix`. `ok` True means the mount is ready.
    """
    runner = ssh_runner or _default_ssh_runner
    quoted = shlex.quote(host_path)

    rc, stdout, _ = runner(box_ip, f"stat -c %u:%g {quoted} 2>/dev/null")
    exists = rc == 0
    current_owner = stdout.strip() if exists and stdout.strip() else None

    if not exists:
        cmd = f"sudo -n mkdir -p {quoted}"
        if not readonly:
            cmd += f" && sudo -n chown {EXPECTED_OWNER} {quoted}"
        rc, _, stderr = runner(box_ip, cmd)
        if rc != 0:
            return PrepResult(
                ok=False,
                action="sudo_failed",
                host_path=host_path,
                message=_sudo_error_message(stderr),
                manual_fix=manual_fix_command(host_path),
            )
        return PrepResult(
            ok=True,
            action="created",
            host_path=host_path,
            current_owner=None if readonly else EXPECTED_OWNER,
            message=(
                f"Created {host_path} on box."
                if readonly
                else f"Created {host_path} and chowned to {EXPECTED_OWNER}."
            ),
        )

    if readonly:
        return PrepResult(
            ok=True,
            action="ok_readonly",
            host_path=host_path,
            current_owner=current_owner,
            message=f"{host_path} exists; readonly mount needs no chown.",
        )

    if current_owner == EXPECTED_OWNER:
        return PrepResult(
            ok=True,
            action="ok",
            host_path=host_path,
            current_owner=current_owner,
            message=f"{host_path} already owned by {EXPECTED_OWNER}.",
        )

    rc, populated_out, _ = runner(box_ip, f"find {quoted} -mindepth 1 -print -quit")
    is_populated = bool(populated_out.strip())

    if is_populated and not recursive:
        return PrepResult(
            ok=False,
            action="refused_populated",
            host_path=host_path,
            current_owner=current_owner,
            is_populated=True,
            message=(
                f"{host_path} is owned by {current_owner} and contains files. "
                "Re-run with --recursive-chown to recursively change ownership."
            ),
            manual_fix=manual_fix_command(host_path, recursive=True),
        )

    flag = " -R" if recursive else ""
    rc, _, stderr = runner(box_ip, f"sudo -n chown{flag} {EXPECTED_OWNER} {quoted}")
    if rc != 0:
        return PrepResult(
            ok=False,
            action="sudo_failed",
            host_path=host_path,
            current_owner=current_owner,
            is_populated=is_populated,
            message=_sudo_error_message(stderr),
            manual_fix=manual_fix_command(host_path, recursive=recursive),
        )
    return PrepResult(
        ok=True,
        action="recursive_chowned" if recursive else "chowned",
        host_path=host_path,
        current_owner=EXPECTED_OWNER,
        is_populated=is_populated,
        message=(
            f"Recursively chowned {host_path} to {EXPECTED_OWNER}."
            if recursive
            else f"Chowned {host_path} to {EXPECTED_OWNER}."
        ),
    )


SUDOERS_BOOTSTRAP = (
    "Auto-prep needs passwordless sudo for two specific commands. Run this "
    "ONCE on the box (you'll be prompted for the sudo password the one "
    "time):\n"
    "\n"
    "  echo 'lagerdata ALL=(root) NOPASSWD: /bin/mkdir, /bin/chown' \\\n"
    "    | sudo tee /etc/sudoers.d/lager-box-config\n"
    "  sudo chmod 440 /etc/sudoers.d/lager-box-config\n"
    "\n"
    "Then re-run `lager box config mount add ...` (or `apply`). The "
    "rule is narrow-scoped (mkdir + chown only)."
)


def _sudo_error_message(stderr: str) -> str:
    err = (stderr or "").strip()
    base = (
        "passwordless sudo is not configured on the box for the auto-prep "
        "commands."
    )
    if err and "a password is required" not in err.lower() and "sudo:" not in err.lower():
        # An unexpected sudo error (binary missing, permission, etc.) — surface
        # the raw stderr without the bootstrap noise; bootstrap won't help.
        return err
    if err:
        base += f" Stderr: {err}"
    return base + "\n\n" + SUDOERS_BOOTSTRAP

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager box authorize` -- install this machine's lager_box SSH key on a box.

Wraps the ssh-keygen / ssh-copy-id dance (see
cli/deployment/scripts/setup_ssh_key.sh for the shell ancestor) so a
user who hits "Permission denied (publickey,password)" can fix it with
one command instead of knowing the key path and ssh-copy-id incantation.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

import click

from ._ssh import _KEY_FALLBACK_DESTS, _LAGER_BOX_KEY, resolve_box_user
from ...box_storage import resolve_and_validate_box
from ...errors import LagerError

_CONNECT_TIMEOUT = 5


def _ensure_keypair(key_path: str = _LAGER_BOX_KEY) -> bool:
    """Generate the ed25519 keypair if missing. Returns True if generated."""
    if os.path.exists(key_path):
        return False
    os.makedirs(os.path.dirname(key_path), mode=0o700, exist_ok=True)
    proc = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", key_path, "-N", "", "-C", "lager-box-access"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise LagerError(
            "Could not generate the SSH key.",
            cause=(proc.stderr or "").strip() or None,
            fixes=[f'Generate it manually: ssh-keygen -t ed25519 -f {key_path} -N ""'],
        )
    return True


def _key_auth_works(dest: str, key_path: str = _LAGER_BOX_KEY) -> bool:
    """Probe whether the key is already authorized, without ever prompting."""
    proc = subprocess.run(
        [
            "ssh", "-i", key_path,
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
            dest, "true",
        ],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


@click.command(
    name="authorize",
    help="Authorize this machine's SSH key on a box (enter the box password "
         "once; lager commands work passwordless after).",
)
@click.argument("box", required=False, metavar="[BOX]")
@click.pass_context
def box_authorize(ctx: click.Context, box: Optional[str]) -> None:
    ip = resolve_and_validate_box(ctx, box)
    user = resolve_box_user(ip)
    dest = f"{user}@{ip}"

    if _ensure_keypair():
        click.echo(f"Generated SSH key at {_LAGER_BOX_KEY}")

    if _key_auth_works(dest):
        _KEY_FALLBACK_DESTS.discard(dest)
        click.secho(f"{dest} is already authorized — no password needed.", fg="green")
        return

    if shutil.which("ssh-copy-id") is None:
        raise LagerError(
            "ssh-copy-id was not found on this machine.",
            fixes=[
                "Install OpenSSH client tools, or append the key manually:",
                f"  cat {_LAGER_BOX_KEY}.pub | ssh {dest} "
                "'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'",
            ],
        )

    click.echo(f"Installing key on {dest} — enter the box password when prompted.")
    # No capture/stdin kwargs: ssh-copy-id must inherit the TTY so its
    # one-time password prompt reaches the user.
    rc = subprocess.run(["ssh-copy-id", "-i", f"{_LAGER_BOX_KEY}.pub", dest]).returncode
    if rc != 0:
        raise LagerError(
            f"ssh-copy-id to {dest} failed.",
            cause="Wrong password, or the box rejected the connection.",
            fixes=[
                f"Retry manually: ssh-copy-id -i {_LAGER_BOX_KEY}.pub {dest}",
                "Confirm the box user and password with your admin.",
            ],
        )

    if not _key_auth_works(dest):
        raise LagerError(
            "The key was copied but key authentication still fails.",
            fixes=[
                f"Test manually: ssh -i {_LAGER_BOX_KEY} {dest}",
                "Check the box's sshd_config allows publickey authentication.",
            ],
        )

    _KEY_FALLBACK_DESTS.discard(dest)
    click.secho(f"Success — {dest} is authorized.", fg="green")
    click.echo("lager commands for this box now work without a password.")

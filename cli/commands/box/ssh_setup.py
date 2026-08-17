# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager ssh-setup` -- install this machine's lager_box SSH key on a box.

Wraps the ssh-keygen / ssh-copy-id dance (see
cli/deployment/scripts/setup_ssh_key.sh for the shell ancestor) so a
user who hits "Permission denied (publickey,password)" can fix it with
one command instead of knowing the key path and ssh-copy-id incantation.

Formerly `lager authorize`; renamed because "authorize" read like
authentication once `lager login` (gateway auth) arrived. A hidden
`authorize` alias warns and forwards.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Optional

import click

from ._ssh import (
    _KEY_FALLBACK_DESTS,
    _LAGER_BOX_KEY,
    BOX_KEYS_DIR,
    ensure_lager_box_keypair,
    key_installed_on_box,
    register_lager_box_key,
    resolve_box_user,
)
from ...box_storage import resolve_and_validate_box
from ...core.net_group import BoxCommand
from ...errors import LagerError


def registration_sudoers_line(box_user: str) -> str:
    """The grant a restricted-sudo fleet needs for registration to work.

    Lager does NOT install this. Three cases and only one of them needs it:
    an unmanaged box's key directory is writable by the box user already; a
    fleet whose box user has broad NOPASSWD is covered by the `sudo -n`
    fallback; and a fleet that scopes sudo tightly should add this through
    its own provisioning, because that is the system that decided the
    directory should be root-owned and is the right place to decide who may
    write to it.

    Scoped to the filename shape rather than the directory: a sudoers
    wildcard does not match "/", so it cannot reach outside. It does not
    confine the box user, whose choice of key content this is — on a box
    where that user is already root-equivalent that changes nothing, and on
    one where it is not, this is a grant worth reading before adding.
    """
    return (
        f"{box_user} ALL=(ALL) NOPASSWD: "
        f"/usr/bin/tee {BOX_KEYS_DIR}/lager-box-*.pub"
    )


def register_or_warn(dest: str) -> bool:
    """Register the key in the box's key directory, warning if it fails.

    Not fatal. By the time this runs the key is installed and authenticating,
    so the command has done its headline job; what registration adds is
    durability. It is worth a visible warning rather than silence, because
    the failure mode it prevents is invisible until the day someone rebuilds
    the box's authorized_keys and every operator loses access at once.
    """
    ok, detail = register_lager_box_key(dest)
    if ok:
        return True
    box_user = dest.rsplit("@", 1)[0] or "<box-user>"
    click.secho(
        f"Warning: the key works, but could not be registered in {BOX_KEYS_DIR} "
        f"on the box ({detail}). It will keep working until something rebuilds "
        "the box's authorized_keys, and will not survive that.\n"
        "  Both the direct write and the sudo fallback failed, so this box's "
        "key directory is managed by another system with sudo scoped tightly "
        "enough to exclude Lager. Do NOT widen the directory to fix it — a "
        "writable key directory lets any user on the box authorize any key, "
        "which is what that system closed. Add this grant through the same "
        "provisioning that manages the box, if you want registration here:\n"
        f"    {registration_sudoers_line(box_user)}",
        fg="yellow", err=True,
    )
    return False


def provision_lager_box_key(dest: str) -> bool:
    """Generate, install, verify and register the lager_box key on `dest`.

    The body of `lager ssh-setup`, extracted so `lager install` can call it
    instead of dead-ending on "run this other command first". Returns True
    when the key was already present (nothing to do), False when it was
    installed. Raises :class:`LagerError` for the failures worth stopping
    on; a registration failure is not one of them and only warns.
    """
    if ensure_lager_box_keypair():
        click.echo(f"Generated SSH key at {_LAGER_BOX_KEY}")

    # Ask the box whether the key is actually in its authorized_keys, rather
    # than inferring it from a successful login. A login proves only that
    # SOME identity worked — and an operator with a `Host *` IdentityFile in
    # ssh_config has one for every host — so the old auth probe reported
    # "already authorized" for boxes this key had been purged from, and this
    # command declined to reinstall it. That is the exact repair this command
    # exists to perform, and it could not fire.
    #
    # None means the box could not be asked at all; fall through to
    # ssh-copy-id, which is the right move for a box we cannot reach
    # unattended anyway.
    if key_installed_on_box(dest) is True:
        _KEY_FALLBACK_DESTS.discard(dest)
        # Still register: this is the path that repairs a box whose key was
        # installed before registration existed, and it costs one keyed
        # round-trip and no prompt.
        register_or_warn(dest)
        return True

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
    # -f, because ssh-copy-id's own "is it already installed?" filter has the
    # same blind spot this command was just fixed for: it decides by logging
    # in with the key, and that login succeeds on ANY identity ssh offers —
    # including an ssh_config `Host *` IdentityFile. On such a machine it
    # reports "All keys were skipped because they already exist on the remote
    # system" and installs nothing, for a box the key is demonstrably not on.
    #
    # Skipping the filter is safe precisely because we no longer rely on it:
    # key_installed_on_box above already established the key is absent by
    # grepping authorized_keys, which is exact. Reaching this line means it
    # needs installing.
    #
    # No capture/stdin kwargs: ssh-copy-id must inherit the TTY so its
    # one-time password prompt reaches the user.
    rc = subprocess.run(["ssh-copy-id", "-f", "-i", f"{_LAGER_BOX_KEY}.pub", dest]).returncode
    if rc != 0:
        raise LagerError(
            f"ssh-copy-id to {dest} failed.",
            cause="Wrong password, or the box rejected the connection.",
            fixes=[
                f"Retry manually: ssh-copy-id -i {_LAGER_BOX_KEY}.pub {dest}",
                "Confirm the box user and password with your admin.",
            ],
        )

    # Verify by presence, not by authentication: a login here would succeed
    # on the operator's own identity whether or not ssh-copy-id landed the
    # key, which is how a silent no-op used to report success.
    if key_installed_on_box(dest) is False:
        raise LagerError(
            "ssh-copy-id reported success but the key is not in the box's "
            "authorized_keys.",
            fixes=[
                f"Check for a full disk or a read-only home on {dest}.",
                f"Append it manually: cat {_LAGER_BOX_KEY}.pub | ssh {dest} "
                "'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys'",
            ],
        )

    _KEY_FALLBACK_DESTS.discard(dest)
    register_or_warn(dest)
    return False


@click.command(
    name="ssh-setup",
    cls=BoxCommand,
    help="Set up passwordless SSH to a box (enter the box password "
         "once; lager commands work passwordless after).",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def ssh_setup(ctx: click.Context, box: Optional[str]) -> None:
    ip = resolve_and_validate_box(ctx, box)
    dest = f"{resolve_box_user(ip)}@{ip}"

    if provision_lager_box_key(dest):
        click.secho(f"{dest} is already authorized — no password needed.", fg="green")
        return

    click.secho(f"Success — {dest} is authorized.", fg="green")
    click.echo("lager commands for this box now work without a password.")


@click.command(name="authorize", cls=BoxCommand, hidden=True,
               help="Deprecated alias for `lager ssh-setup`.")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def authorize(ctx: click.Context, box: Optional[str]) -> None:
    click.secho(
        "DEPRECATED: `lager authorize` is now `lager ssh-setup`. "
        "The old spelling still works but will be removed in a future release.",
        fg="yellow", err=True,
    )
    ctx.invoke(ssh_setup, box=box)

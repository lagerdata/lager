# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""`lager install` with neither --box nor --ip: install onto this machine.

The operating system is detected, never asked for. macOS is provisioned by
``cli.deployment.macos_box``. Linux is installed by the same steps as a remote
Linux Box, over SSH to the machine's own loopback address: one deploy path,
whichever machine the CLI runs on. A remote install does not come through here.
"""
import getpass
import os
import socket
import sys

import click

from ...box_storage import add_box, delete_box, get_box_name_by_ip
from ...errors import LagerError

DEFAULT_LOCAL_NAME = "local"
LOOPBACK_ADDRESSES = ("127.0.0.1", "localhost", "::1")

# `lager install` options that only mean something for a Linux Box, and why.
LINUX_ONLY_INSTALL_OPTIONS = (
    ("user", "--user", "this Mac's Box is installed as you, not over SSH"),
    ("skip_firewall", "--skip-firewall", "the installer configures no firewall on a Mac"),
    ("corporate_vpn", "--corporate-vpn", "the installer configures no firewall on a Mac"),
    ("skip_verify", "--skip-verify", "the install always waits for the Box to answer"),
    ("pull", "--pull", "a Mac always uses the pre-built image"),
    ("no_pull", "--no-pull", "a Mac always uses the pre-built image"),
    ("timeout", "--timeout", "nothing is built in the container on a Mac"),
)


def ignored_options(ctx, options=LINUX_ONLY_INSTALL_OPTIONS):
    """The Linux-only options the user typed, each with why a Mac ignores it."""
    from click.core import ParameterSource

    typed = (ParameterSource.COMMANDLINE, ParameterSource.ENVIRONMENT)
    return [(flag, why) for name, flag, why in options
            if name in ctx.params and ctx.get_parameter_source(name) in typed]


def is_local_target(ip, platform=None):
    """True when ``ip`` names this machine and it is a Mac.

    A Box saved at a loopback address on a Mac is the one installed on that
    Mac, so `--box <its name>` must not be sent over SSH to a Linux script.
    Off macOS this is always False: a loopback address there keeps going
    over SSH exactly as before.
    """
    return (platform or sys.platform) == "darwin" and ip in LOOPBACK_ADDRESSES


def _unsupported(system):
    return LagerError(
        "Installing a Lager Box on this Windows machine is not yet supported."
        if system == "win32" else
        f"Installing a Lager Box on this machine ({system}) is not yet supported.",
        fixes=["Install onto a Linux machine or a Mac: lager install --ip <address>"])


def _ssh_server_answers(address=LOOPBACK_ADDRESSES[0], port=22, timeout=3.0):
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def local_ssh_target(user=None, platform=None, answers=None):
    """``(address, user)`` for a Box on this Linux machine; None off Linux.

    A Linux Box is deployed over SSH, so the machine the CLI runs on is
    reached the same way, at its loopback address, as the user running the
    CLI unless ``--user`` names another. Everything after that is the
    remote install, unchanged. It needs an SSH server on this machine.
    """
    if not (platform or sys.platform).startswith("linux"):
        return None
    if not (answers or _ssh_server_answers)():
        raise LagerError(
            "This machine does not accept SSH connections, which the install uses "
            "to reach it.",
            cause="Nothing answered on 127.0.0.1 port 22.",
            fixes=["Install and start an SSH server: "
                   "sudo apt install openssh-server && sudo systemctl enable --now ssh",
                   "Then run this command again."])
    return LOOPBACK_ADDRESSES[0], user or getpass.getuser()


def install_locally(ctx, *, version, version_defaulted, yes, fork=None, platform=None,
                    skip_jlink=False, skip_picoscope=False):
    platform = platform or sys.platform
    if platform != "darwin":
        raise _unsupported(platform)
    if fork:
        raise click.UsageError("--fork is not supported for an install on this Mac yet.")

    from ... import __version__ as cli_version
    from ...deployment import macos_box

    checkout = macos_box.running_from_checkout()
    if checkout is not None:
        click.echo(f"Installing a Lager Box on this Mac from the checkout at {checkout}.")
    else:
        click.echo(f"Installing Lager Box {version} on this Mac.")
    click.echo("This sets up a background agent for your instruments and a "
               "container for the Box.")
    if not (os.path.isdir(macos_box.ETC_DIR) and os.access(macos_box.ETC_DIR, os.W_OK)):
        click.echo(f"Your password is asked for once, to create {macos_box.ETC_DIR}.")
    for flag, why in ignored_options(ctx):
        click.secho(f"Ignoring {flag}: {why}.", fg='yellow')
    if not yes and not click.confirm("Continue?", default=True):
        click.secho("Installation cancelled.", fg='yellow')
        ctx.exit(1)

    result = macos_box.install(
        version, cli_version, echo=click.echo,
        confirm=(lambda question: True) if yes
        else (lambda question: click.confirm(question, default=True)),
        skip=[piece for piece, wanted in (("jlink", skip_jlink),
                                          ("picoscope", skip_picoscope)) if wanted])

    name = get_box_name_by_ip(macos_box.LOCAL_ADDRESS)
    if name is None:
        name = macos_box.remembered_box_name() or DEFAULT_LOCAL_NAME
        if not yes:
            name = click.prompt("Box name", type=str, default=name).strip() or name
    add_box(name, macos_box.LOCAL_ADDRESS, user=getpass.getuser(),
            version=result.box_version, python_port=macos_box.PYTHON_SERVICE_PORT)
    macos_box.remember_box_name(name)

    click.echo()
    if result.ready:
        click.echo("Set up: " + ", ".join(result.ready) + ".")
    for line in result.skipped:
        click.secho(f"Not set up -- {line}", fg='yellow')
    if result.skipped:
        click.echo("Install what is missing and run `lager install` again to add it.")
        click.echo()
    click.secho("Installation complete!", fg='green', bold=True)
    click.secho(f"Saved as '{name}'. You can now use: lager hello --box {name}", fg='cyan')


def uninstall_locally(ctx, *, purge_config, yes, dry_run, platform=None,
                      keep_docker_images=False):
    platform = platform or sys.platform
    if platform != "darwin":
        raise _unsupported(platform)
    from ...deployment import macos_box

    click.echo("This removes from this Mac:")
    for piece in macos_box.installed_pieces(keep_images=keep_docker_images):
        click.echo(f"  - {piece}")
    if purge_config:
        click.echo(f"  - everything in {macos_box.ETC_DIR} (saved nets, Box "
                   "configuration), after copying it to your home directory")
    else:
        click.echo(f"{macos_box.ETC_DIR} (saved nets, Box configuration) is kept.")
    if dry_run:
        click.echo("Dry run: nothing was changed.")
        return
    if not yes and not click.confirm("Continue?", default=False):
        click.secho("Uninstall cancelled.", fg='yellow')
        ctx.exit(1)

    macos_box.uninstall(purge_config=purge_config, echo=click.echo,
                        keep_images=keep_docker_images)

    name = get_box_name_by_ip(macos_box.LOCAL_ADDRESS)
    if name and (yes or click.confirm(
            f"Remove '{name}' from local .lager configuration?", default=True)):
        delete_box(name)
    click.secho("Uninstall complete.", fg='green', bold=True)


def update_locally(ctx, *, version, yes, check=False, fork=None):
    """`lager update` for the Box on this Mac: the install, run again.

    Every install step is safe to repeat, and the agent and the container
    are always replaced together, so there is no separate update procedure.
    """
    if check:
        click.echo("`lager update --check` is not available for a Box on this "
                   "Mac yet. Run `lager update` to bring it to this CLI's version.")
        ctx.exit(2)
    from .update import default_box_version

    install_locally(ctx, version=version or default_box_version(),
                    version_defaulted=not version, yes=yes, fork=fork)

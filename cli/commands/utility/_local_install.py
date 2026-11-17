# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""`lager install` with neither --box nor --ip: install onto this machine.

The operating system is detected, never asked for. macOS is provisioned by
``cli.deployment.macos_box``. A remote install does not come through here.
"""
import sys

import click

from ...box_storage import add_box, delete_box, get_box_name_by_ip
from ...errors import LagerError

DEFAULT_LOCAL_NAME = "local"
LOOPBACK_ADDRESSES = ("127.0.0.1", "localhost", "::1")


def is_local_target(ip, platform=None):
    """True when ``ip`` names this machine and it is a Mac.

    A Box saved at a loopback address on a Mac is the one installed on that
    Mac, so `--box <its name>` must not be sent over SSH to a Linux script.
    Off macOS this is always False: a loopback address there keeps going
    over SSH exactly as before.
    """
    return (platform or sys.platform) == "darwin" and ip in LOOPBACK_ADDRESSES


def _unsupported(system):
    if system == "win32":
        return LagerError(
            "Installing a Lager Box on this Windows machine is not yet supported.",
            fixes=["Install onto a Linux machine or a Mac: lager install --ip <address>"])
    return LagerError(
        "Installing a Lager Box on this machine is not yet supported on Linux.",
        cause="Without --box or --ip, `lager install` installs onto the machine "
              "it runs on, which today works on macOS only.",
        fixes=["Install over SSH instead: lager install --ip <address> "
               "(the address may be this machine's own)."])


def install_locally(ctx, *, version, version_defaulted, yes, fork=None, platform=None):
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
               "container for the Box. Your password is asked for once, to "
               f"create {macos_box.ETC_DIR}.")
    if not yes and not click.confirm("Continue?", default=True):
        click.secho("Installation cancelled.", fg='yellow')
        ctx.exit(1)

    result = macos_box.install(version, cli_version, echo=click.echo)

    name = get_box_name_by_ip(macos_box.LOCAL_ADDRESS)
    if name is None:
        name = DEFAULT_LOCAL_NAME
        if not yes:
            name = click.prompt("Box name", type=str, default=DEFAULT_LOCAL_NAME).strip() \
                or DEFAULT_LOCAL_NAME
    add_box(name, macos_box.LOCAL_ADDRESS, version=result.box_version,
            python_port=macos_box.PYTHON_SERVICE_PORT)

    click.echo()
    for line in result.skipped:
        click.secho(f"Not set up -- {line}", fg='yellow')
    if result.skipped:
        click.echo("Install what is missing and run `lager install` again to add it.")
        click.echo()
    click.secho("Installation complete!", fg='green', bold=True)
    click.secho(f"You can now use: lager hello --box {name}", fg='cyan')


def uninstall_locally(ctx, *, purge_config, yes, dry_run, platform=None):
    platform = platform or sys.platform
    if platform != "darwin":
        raise _unsupported(platform)
    from ...deployment import macos_box

    click.echo("This removes from this Mac:")
    for piece in macos_box.installed_pieces():
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

    macos_box.uninstall(purge_config=purge_config, echo=click.echo)

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

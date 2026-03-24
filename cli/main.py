# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.cli

    Command line interface entry point
"""
import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import trio
    import trio_websocket

import os
import urllib.parse
import sys

import traceback
import click

from . import __version__
from .config import read_config_file
from .context import LagerContext
from .update_check import start_background_check, notify_if_update_available


def _launch_terminal():
    """Launch the interactive Lager Terminal."""
    try:
        from .terminal.ui.repl import LagerREPL
        repl = LagerREPL()
        repl.run()
    except ImportError as e:
        click.echo(f"Lager Terminal dependencies not installed: {e}")
        click.echo("Install with: pip install prompt_toolkit rich")
        click.echo("\nShowing help instead:\n")
        return False
    return True


@click.command('terminal')
def terminal_cmd():
    """Launch the interactive Lager Terminal."""
    if not _launch_terminal():
        raise SystemExit(1)

# Communication commands (from cli.commands.communication)
from .commands.communication import uart, ble, blufi, _wifi, usb, spi, i2c, router

# Development commands (from cli.commands.development)
from .commands.development import _debug, arm, python, devenv

# Power commands (from commands/power/)
from .commands.power.supply import supply
from .commands.power.battery import battery
from .commands.power.solar import solar
from .commands.power.eload import eload

# Measurement commands (from commands/measurement/)
from .commands.measurement.adc import adc
from .commands.measurement.dac import dac
from .commands.measurement.gpi import gpi
from .commands.measurement.gpo import gpo
from .commands.measurement.scope import scope
from .commands.measurement.logic import logic
from .commands.measurement.thermocouple import thermocouple
from .commands.measurement.watt import watt
from .commands.measurement.energy import energy

# Box commands (from commands.box package)
from .commands.box import hello, boxes, instruments, nets, ssh

# Utility commands (from commands.utility package)
from .commands.utility import defaults, binaries, update, pip, exec_, logs, webcam, install, uninstall, install_wheel

def _check_venv_shadowing():
    """Warn if a system-installed lager is shadowing the venv version."""
    virtual_env = os.environ.get('VIRTUAL_ENV')
    if not virtual_env:
        return

    # If VIRTUAL_ENV is set but sys.prefix doesn't point to it, the
    # system `lager` entry-point script (with a hardcoded shebang) is
    # running instead of the venv's copy.  This is the exact shadowing
    # scenario we want to catch.
    if os.path.realpath(sys.prefix) == os.path.realpath(virtual_env):
        return

    click.secho(
        f"WARNING: Lager CLI v{__version__} is running from a system Python ({sys.prefix}), "
        f"not from your active virtual environment ({virtual_env}).",
        fg='yellow', err=True,
    )
    click.secho(
        "This can cause version mismatches. To fix:\n"
        "  hash -r              # clear shell cache, then retry\n"
        "  pip install --force-reinstall lager-cli   # if hash -r alone doesn't help\n"
        "Or uninstall the system version:\n"
        "  deactivate && pip uninstall lager-cli && source <venv>/bin/activate",
        fg='yellow', err=True,
    )
    click.echo(err=True)


def _decode_environment():
    for key in os.environ:
        if key.startswith('LAGER_'):
            os.environ[key] = urllib.parse.unquote(os.environ[key])

@click.group(invoke_without_command=True)
@click.pass_context
@click.option('--version', 'see_version', is_flag=True, help='See package version')
@click.option('--debug', 'debug', is_flag=True, help='Show debug output', default=False)
@click.option('--colorize', 'colorize', is_flag=True, help='Enable colored terminal output', default=False)
@click.option('--interpreter', '-i', required=False, default=None, help='Select a specific interpreter / user interface', hidden=True)
def cli(ctx=None, see_version=None, debug=False, colorize=False, interpreter=None):
    """
        Lager CLI
    """
    _check_venv_shadowing()

    if os.getenv('LAGER_DECODE_ENV'):
        _decode_environment()

    if see_version:
        click.echo(__version__)
        click.get_current_context().exit(0)
    if ctx.invoked_subcommand is None:
        # Launch interactive terminal when no subcommand is given
        if not _launch_terminal():
            click.echo(ctx.get_help())
    else:
        setup_context(ctx, debug, colorize, interpreter)
        _schedule_update_check(ctx)

cli.add_command(adc)
cli.add_command(ble)
cli.add_command(blufi)
cli.add_command(_debug)
cli.add_command(defaults)
cli.add_command(devenv)
cli.add_command(exec_)
cli.add_command(uart)
cli.add_command(python)
cli.add_command(_wifi)
cli.add_command(webcam)
cli.add_command(pip)
cli.add_command(scope)
cli.add_command(logic)
cli.add_command(supply)
cli.add_command(battery)
cli.add_command(eload)
cli.add_command(nets)
cli.add_command(solar)
cli.add_command(usb)
cli.add_command(spi)
cli.add_command(i2c)
cli.add_command(router)
cli.add_command(hello)
cli.add_command(arm)
cli.add_command(thermocouple)
cli.add_command(watt)
cli.add_command(energy)
cli.add_command(dac)
cli.add_command(gpi)
cli.add_command(gpo)
cli.add_command(boxes)
cli.add_command(instruments)
cli.add_command(ssh)
cli.add_command(update)
cli.add_command(logs)
cli.add_command(binaries)
cli.add_command(install)
cli.add_command(uninstall)
cli.add_command(install_wheel)
cli.add_command(terminal_cmd)

def _schedule_update_check(ctx):
    """Start a background update check and register a close callback to show the result."""
    # Skip in CI or when explicitly disabled
    if os.getenv('CI') or os.getenv('LAGER_NO_UPDATE_CHECK'):
        return
    thread, result_holder = start_background_check()
    ctx.call_on_close(lambda: notify_if_update_available(__version__, thread, result_holder))


def setup_context(ctx, debug, colorize, interpreter):
    """
        Setup the CLI context
    """
    config = read_config_file()
    ctx.obj = LagerContext(
        ctx=ctx,
        defaults=config['LAGER'],
        debug=debug,
        style=click.style if colorize else lambda string, **kwargs: string,
        interpreter=interpreter,
    )


def main():
    """Console script entry point: run the Click CLI."""
    cli()


if __name__ == '__main__':
    main()

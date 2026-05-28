# Copyright 2024-2026 Lager Data
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
    """Launch the interactive Lager Terminal"""
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
from .commands.box import hello, boxes, instruments, nets, ssh, box
from .commands.box.diagnose import diagnose

# Utility commands (from commands.utility package)
from .commands.utility import defaults, binaries, update, exec_, logs, webcam, install, uninstall, install_wheel

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


class SectionedGroup(click.Group):
    """Root CLI group that lists commands under category headings.

    A flat, alphabetical wall of 40+ commands is hard for a new user to scan.
    This renders ``lager --help`` with the commands grouped into the same
    categories the codebase already organises them by (see imports above), so
    a newcomer can find "the power-supply one" or "the debug one" at a glance.

    Any command not listed in ``COMMAND_SECTIONS`` (e.g. a newly added one that
    nobody categorised yet) still shows up — under a trailing "Other" section —
    so commands can never silently disappear from help.
    """

    COMMAND_SECTIONS = [
        ("Debug & development", ["debug", "python", "arm", "devenv", "exec"]),
        ("Power", ["supply", "battery", "solar", "eload"]),
        ("Measurement & I/O", ["scope", "logic", "energy", "adc", "dac",
                                "gpi", "gpo", "thermocouple", "watt"]),
        ("Communication", ["uart", "usb", "spi", "i2c", "ble", "blufi",
                            "router"]),
        ("Box setup & management", ["hello", "boxes", "box", "nets",
                                    "instruments", "ssh", "defaults", "webcam"]),
        ("Install & maintenance", ["update", "install", "uninstall",
                                   "install-wheel", "binaries", "logs",
                                   "terminal"]),
    ]

    def format_commands(self, ctx, formatter):
        # Collect visible commands keyed by name.
        commands = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or getattr(cmd, "hidden", False):
                continue
            commands[name] = cmd

        if not commands:
            return

        # One shared column width so every section's short-help lines up.
        col_max = max(len(name) for name in commands)
        limit = formatter.width - 6 - col_max

        assigned = set()
        sections = []
        for title, names in self.COMMAND_SECTIONS:
            rows = []
            for name in names:
                cmd = commands.get(name)
                if cmd is None:
                    continue
                assigned.add(name)
                rows.append((name, cmd.get_short_help_str(limit)))
            if rows:
                sections.append((title, rows))

        leftovers = sorted(
            (name, commands[name].get_short_help_str(limit))
            for name in commands if name not in assigned
        )
        if leftovers:
            sections.append(("Other", leftovers))

        for title, rows in sections:
            with formatter.section(title):
                formatter.write_dl(rows, col_max=col_max)


@click.group(cls=SectionedGroup, invoke_without_command=True)
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
cli.add_command(diagnose)
cli.add_command(arm)
cli.add_command(thermocouple)
cli.add_command(watt)
cli.add_command(energy)
cli.add_command(dac)
cli.add_command(gpi)
cli.add_command(gpo)
cli.add_command(boxes)
cli.add_command(box)
cli.add_command(instruments)
cli.add_command(ssh)
# `lager update` is the canonical box-update command. It previously also
# existed as `lager box update`; that form was removed in favor of the
# shorter top-level spelling.
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

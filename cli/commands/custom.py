# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.custom

    `lager custom`: drive any custom-driver net by name, without knowing its
    type. It always works, so scripts and CI use it; the short form
    `lager <type> <NET> <action>` is the same call with typed options.

        lager custom --box B                     list custom nets
        lager custom RIG_1 --box B               list RIG_1's actions
        lager custom RIG_1 jog --seconds 3 --box B
"""
from __future__ import annotations

import click
from texttable import Texttable

from ..core.custom_drivers import describe_actions, driver_request, echo_result, remember_drivers
from ..core.net_group import NetCommand
from ..core.net_helpers import post_net_command, resolve_box, resolve_box_locked


def parse_action_args(tokens) -> dict:
    """Turn ``--seconds 3 --hold-time=2 --fast --no-home`` into a dict.

    Values stay strings; the box types and range-checks them against the
    driver's manifest. A flag with no value is "true"; ``--no-X`` is X "false".
    """
    out = {}
    tokens = list(tokens)
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith('--') or token == '--':
            raise click.UsageError(f"Unexpected argument '{token}'. Action arguments are "
                                   f"given as --NAME VALUE.")
        key, eq, value = token[2:].partition('=')
        if not eq:
            if i + 1 < len(tokens) and not tokens[i + 1].startswith('--'):
                value = tokens[i + 1]
                i += 1
            elif key.startswith('no-'):
                key, value = key[3:], 'false'
            else:
                value = 'true'
        if not key:
            raise click.UsageError(f"Unexpected argument '{token}'")
        out[key.replace('-', '_')] = value
        i += 1
    return out


def _drivers(ctx, box_ip):
    drivers = driver_request(ctx, box_ip, 'GET', '/drivers').get('drivers') or []
    remember_drivers(box_ip, drivers)
    return drivers


def _driver_for_net(drivers, netname):
    return next((d for d in drivers if netname in (d.get('nets') or [])), None)


@click.command('custom', cls=NetCommand,
               context_settings={'ignore_unknown_options': True})
@click.argument('netname', required=False, metavar='[NET_NAME]')
@click.argument('action', required=False, metavar='[ACTION]')
@click.argument('action_args', nargs=-1, type=click.UNPROCESSED, metavar='[--ARG VALUE]...')
@click.option('--box', required=False, help='Lager Box name or IP')
@click.option('--json', 'as_json', is_flag=True, help="Print the action's result as JSON")
@click.pass_context
def custom(ctx, netname, action, action_args, box, as_json):
    """
        Run an action on a custom-driver net

        Works for any custom driver installed on the box, by net name. With
        no NET_NAME, lists the custom nets; with no ACTION, lists the net's
        actions.
    """
    if netname is None:
        box_ip = resolve_box(ctx, box, read_only=True)
        drivers = _drivers(ctx, box_ip)
        rows = [(net, d.get('type', ''), d.get('version') or '-', d.get('status', ''))
                for d in drivers for net in (d.get('nets') or [])]
        if not drivers:
            click.echo("No custom drivers are installed on this box.")
            return
        if not rows:
            click.echo("No custom nets on this box. Add one with "
                       "`lager nets add-custom <NET_NAME> <TYPE> --param NAME=VALUE`.")
            return
        table = Texttable()
        table.set_deco(Texttable.HEADER)
        table.set_cols_dtype(['t'] * 4)
        table.header(['Net', 'Driver', 'Version', 'Status'])
        for row in sorted(rows):
            table.add_row(list(row))
        click.echo(table.draw())
        return

    if action is None:
        box_ip = resolve_box(ctx, box, read_only=True)
        entry = _driver_for_net(_drivers(ctx, box_ip), netname)
        if entry is None:
            click.secho(f"Error: '{netname}' is not a custom net on this box. "
                        f"List them with `lager custom --box <BOX>`.", fg='red', err=True)
            ctx.exit(1)
        if entry.get('status') != 'ok':
            click.secho(f"Error: driver '{entry.get('type')}' failed to load: "
                        f"{entry.get('error')}", fg='red', err=True)
            ctx.exit(1)
        click.echo(f"{netname}: {entry['type']} {entry.get('version') or ''}")
        click.echo("Actions:")
        for line in describe_actions(entry['manifest']):
            click.echo(line)
        return

    args = parse_action_args(action_args)
    box_ip = resolve_box_locked(ctx, box, f"custom {action}")
    # The box resolves the net's role itself and checks the args against the
    # driver's manifest, so nothing here needs to know the driver.
    result = post_net_command(ctx, box_ip, netname, action, quiet=True,
                              http_timeout=660, **args)
    echo_result(result, as_json)


custom.net_examples = [
    "lager custom --box <BOX>                       (list custom nets)",
    "lager custom RIG_1 --box <BOX>                 (list the net's actions)",
    "lager custom RIG_1 jog --seconds 3 --box <BOX>",
]

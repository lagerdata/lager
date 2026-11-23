# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.defaults

    Default settings management commands
"""
import click
from texttable import Texttable
from ...config import read_config_file, write_config_file
from ...core.group_usage import LagerGroup
from ...box_storage import load_boxes
from ...sort_utils import natural_sort_key


@click.group(cls=LagerGroup, invoke_without_command=True)
@click.pass_context
def defaults(ctx):
    """Manage default settings"""
    if ctx.invoked_subcommand is None:
        # Default behavior: list defaults
        config = read_config_file()

        if not config.has_section('LAGER') or not config.options('LAGER'):
            click.echo("No defaults are currently set. Add one with: lager defaults add")
            return

        table = Texttable()
        table.set_deco(Texttable.HEADER)
        table.set_cols_dtype(['t', 't'])
        table.set_cols_align(["l", "l"])
        table.add_row(['Default', 'Value'])

        for key in config.options('LAGER'):
            value = config.get('LAGER', key)
            # Make the display name more user-friendly
            if key == 'gateway_id':
                display_name = 'box'
            elif key == 'serial_device':
                display_name = 'serial-port'
            elif key.startswith('net_'):
                # Convert net_power_supply to supply-net (special case for power supply)
                net_type = key[4:]  # Remove 'net_' prefix
                if net_type == 'power_supply':
                    display_name = 'supply-net'
                else:
                    display_name = f"{net_type.replace('_', '-')}-net"
            else:
                display_name = key.replace('_', '-')
            table.add_row([display_name, value])

        click.echo(table.draw())


# Every net default `lager defaults add` can set: (option and delete subcommand
# name, config key, name used in help and messages). The `add` options, the
# `delete` subcommands and the key each net command reads all come from here,
# so a command's fallback and the option that sets it cannot drift apart.
_net_types = [
    ('supply-net', 'net_power_supply', 'supply net'),
    ('battery-net', 'net_battery', 'battery net'),
    ('solar-net', 'net_solar', 'solar net'),
    ('scope-net', 'net_scope', 'scope net'),
    ('logic-net', 'net_logic', 'logic net'),
    ('adc-net', 'net_adc', 'ADC net'),
    ('dac-net', 'net_dac', 'DAC net'),
    ('gpio-net', 'net_gpio', 'GPIO net'),
    ('debug-net', 'net_debug', 'debug net'),
    ('eload-net', 'net_eload', 'electronic load net'),
    ('usb-net', 'net_usb', 'USB hub net'),
    ('webcam-net', 'net_webcam', 'webcam net'),
    ('watt-meter-net', 'net_watt_meter', 'watt meter net'),
    ('energy-net', 'net_energy', 'energy analyzer net'),
    ('thermocouple-net', 'net_thermocouple', 'thermocouple net'),
    ('dmm-net', 'net_dmm', 'multimeter net'),
    ('ble-sniffer-net', 'net_ble_sniffer', 'BLE sniffer net'),
    ('uart-net', 'net_uart', 'UART net'),
    ('i2c-net', 'net_i2c', 'I2C net'),
    ('spi-net', 'net_spi', 'SPI net'),
    ('arm-net', 'net_arm', 'robotic arm net'),
]


def _param(option):
    return option.replace('-', '_')


def _net_options(func):
    for option, _key, display_name in reversed(_net_types):
        func = click.option(f'--{option}', _param(option), required=False,
                            help=f'Set default {display_name} name')(func)
    return func


@defaults.command()
@click.option("--box", required=False, help="Set default box")
@click.option('--serial-port', required=False, help='Set default serial port path')
@_net_options
@click.option('--user', required=False,
              help='Set the username recorded on box locks. To have a lock that '
                   'another tool records under your email recognized as yours, '
                   'set this to that email.')
def add(box, serial_port, user, **nets):
    """
        Set default values
    """
    # Check if at least one option is provided
    if all(v is None for v in [box, serial_port, user, *nets.values()]):
        click.secho("Error: You must specify at least one setting", fg='red', err=True)
        click.echo("\nAvailable options:", err=True)
        click.echo(f"  {'--box [BOX_NAME]':<30}Set default box", err=True)
        click.echo(f"  {'--serial-port [PATH]':<30}Set default serial port", err=True)
        for option, _key, display_name in _net_types:
            click.echo(f"  {'--' + option + ' [NET_NAME]':<30}Set default {display_name}", err=True)
        click.echo(f"  {'--user [USERNAME]':<30}Set default username for box locking", err=True)
        click.echo("\nExample: lager defaults add --box mybox --supply-net supply1", err=True)
        raise click.Abort()

    config = read_config_file()
    changes = []

    # Handle box default
    if box is not None:
        # Validate that the box exists in saved boxes
        boxes = load_boxes()
        if box not in boxes:
            click.secho(f"Error: Box '{box}' not found in saved boxes.", fg='red', err=True)
            if boxes:
                click.echo("\nAvailable boxes:", err=True)
                for name, ip in sorted(boxes.items(), key=lambda x: natural_sort_key(x[0])):
                    click.echo(f"  {name}: {ip}", err=True)
                click.echo(f"\nDid you mean one of these? Or add a new box with:", err=True)
            else:
                click.echo("\nNo boxes are currently saved.", err=True)
                click.echo("To add a new box:", err=True)
            click.echo(f"  lager boxes add --name {box} --ip [TAILSCALE_IP] --user [USERNAME]", err=True)
            click.echo("\nTo find your Tailscale IP, run: tailscale status", err=True)
            raise click.Abort()

        config.set('LAGER', 'gateway_id', box)
        changes.append(f"box: {box}")

    # Handle serial port default
    if serial_port is not None:
        config.set('LAGER', 'serial_device', serial_port)
        changes.append(f"serial-port: {serial_port}")

    # Handle user default
    if user is not None:
        config.set('LAGER', 'user', user)
        changes.append(f"user: {user}")

    # Handle net defaults
    for option, config_key, _display_name in _net_types:
        value = nets[_param(option)]
        if value is not None:
            config.set('LAGER', config_key, value)
            changes.append(f"{option}: {value}")

    write_config_file(config)

    # Display success message
    if len(changes) == 1:
        click.secho(f"Set default {changes[0]}", fg='green')
    else:
        click.secho(f"Set defaults:", fg='green')
        for change in changes:
            click.echo(f"  - {change}")


@defaults.command('list')
@click.pass_context
def list_defaults(ctx):
    """
        List default settings
    """
    # Reuse the default behavior
    ctx.invoke(defaults)


@defaults.group()
def delete():
    """
        Delete default settings
    """
    pass


@delete.command('box')
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
def delete_box_default(yes):
    """
        Delete default box
    """
    config = read_config_file()

    # Check if gateway_id is set
    if not config.has_option('LAGER', 'gateway_id'):
        click.echo("No default box is currently set.")
        return

    # Get value to display
    value = config.get('LAGER', 'gateway_id')

    # Display what will be deleted and ask for confirmation unless --yes flag is provided
    if not yes:
        click.echo(f"\nYou are about to delete the following default:")
        click.echo(f"  box: {value}")
        click.echo()

        if not click.confirm("Delete this default?", default=False):
            click.echo("Cancelled. Default not deleted.")
            return

    # Remove the gateway_id option
    config.remove_option('LAGER', 'gateway_id')

    write_config_file(config)
    click.secho(f"Deleted default box '{value}'", fg='green')


@delete.command()
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
def serial_port(yes):
    """
        Delete default serial port
    """
    config = read_config_file()

    # Check if serial_device is set
    if not config.has_option('LAGER', 'serial_device'):
        click.echo("No default serial port is currently set.")
        return

    # Get value to display
    value = config.get('LAGER', 'serial_device')

    # Display what will be deleted and ask for confirmation unless --yes flag is provided
    if not yes:
        click.echo(f"\nYou are about to delete the following default:")
        click.echo(f"  serial-port: {value}")
        click.echo()

        if not click.confirm("Delete this default?", default=False):
            click.echo("Cancelled. Default not deleted.")
            return

    # Remove the serial_device option
    config.remove_option('LAGER', 'serial_device')

    write_config_file(config)
    click.secho(f"Deleted default serial port '{value}'", fg='green')


@delete.command('user')
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
def delete_user_default(yes):
    """Delete default user"""
    config = read_config_file()

    if not config.has_option('LAGER', 'user'):
        click.echo("No default user is currently set.")
        return

    value = config.get('LAGER', 'user')

    if not yes:
        click.echo(f"\nYou are about to delete the following default:")
        click.echo(f"  user: {value}")
        click.echo()

        if not click.confirm("Delete this default?", default=False):
            click.echo("Cancelled. Default not deleted.")
            return

    config.remove_option('LAGER', 'user')
    write_config_file(config)
    click.secho(f"Deleted default user '{value}'", fg='green')


# Helper function to create delete commands for net types
def _create_net_delete_command(net_type, config_key, display_name):
    """Create a delete command for a specific net type"""
    @delete.command(name=net_type, help=f"Delete default {display_name}")
    @click.option('--yes', is_flag=True, help='Skip confirmation prompt')
    def delete_net(yes):
        config = read_config_file()

        if not config.has_option('LAGER', config_key):
            click.echo(f"No default {display_name} is currently set.")
            return

        value = config.get('LAGER', config_key)

        if not yes:
            click.echo(f"\nYou are about to delete the following default:")
            click.echo(f"  {net_type}: {value}")
            click.echo()

            if not click.confirm("Delete this default?", default=False):
                click.echo("Cancelled. Default not deleted.")
                return

        config.remove_option('LAGER', config_key)
        write_config_file(config)
        click.secho(f"Deleted default {display_name} '{value}'", fg='green')

    return delete_net


# Create delete commands for all net types
for net_type, config_key, display_name in _net_types:
    _create_net_delete_command(net_type, config_key, display_name)


@defaults.command('delete-all')
@click.option('--yes', is_flag=True, help='Skip confirmation prompt')
def delete_all(yes):
    """
        Delete all defaults
    """
    config = read_config_file()

    # Check if there are any defaults set
    if not config.has_section('LAGER') or not config.options('LAGER'):
        click.echo("No defaults are currently set.")
        return

    # Count the defaults
    defaults_list = []
    for key in config.options('LAGER'):
        value = config.get('LAGER', key)
        if key == 'gateway_id':
            display_name = 'box'
        elif key == 'serial_device':
            display_name = 'serial-port'
        elif key.startswith('net_'):
            # Convert net_power_supply to supply-net (special case for power supply)
            net_type = key[4:]  # Remove 'net_' prefix
            if net_type == 'power_supply':
                display_name = 'supply-net'
            else:
                display_name = f"{net_type.replace('_', '-')}-net"
        else:
            display_name = key.replace('_', '-')
        defaults_list.append((display_name, value))

    defaults_count = len(defaults_list)

    # Display warning and defaults list
    click.echo(click.style(f"\nWARNING: You are about to delete ALL {defaults_count} default(s):", fg='yellow', bold=True))
    click.echo()
    for display_name, value in defaults_list:
        click.echo(f"  - {display_name}: {value}")
    click.echo()

    # Ask for confirmation unless --yes flag is provided (default is No)
    if not yes and not click.confirm("Are you sure you want to delete ALL defaults?", default=False):
        click.echo("Cancelled. No defaults were deleted.")
        return

    # Remove all options from LAGER section
    config.remove_section('LAGER')
    config.add_section('LAGER')

    write_config_file(config)
    click.secho(f"Deleted all {defaults_count} default(s)", fg='green')

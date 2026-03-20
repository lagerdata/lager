# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.utility.defaults

    Default settings management commands
"""
import click
from texttable import Texttable
from ...config import read_config_file, write_config_file
from ...box_storage import load_boxes
from ...sort_utils import natural_sort_key


@click.group(invoke_without_command=True)
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


@defaults.command()
@click.option("--box", required=False, help="Set default box")
@click.option('--serial-port', required=False, help='Set default serial port path')
@click.option('--supply-net', required=False, help='Set default supply net name')
@click.option('--battery-net', required=False, help='Set default battery net name')
@click.option('--solar-net', required=False, help='Set default solar net name')
@click.option('--scope-net', required=False, help='Set default scope net name')
@click.option('--logic-net', required=False, help='Set default logic net name')
@click.option('--adc-net', required=False, help='Set default ADC net name')
@click.option('--dac-net', required=False, help='Set default DAC net name')
@click.option('--gpio-net', required=False, help='Set default GPIO net name')
@click.option('--debug-net', required=False, help='Set default debug net name')
@click.option('--eload-net', required=False, help='Set default electronic load net name')
@click.option('--usb-net', required=False, help='Set default USB hub net name')
@click.option('--webcam-net', required=False, help='Set default webcam net name')
@click.option('--watt-meter-net', required=False, help='Set default watt meter net name')
@click.option('--thermocouple-net', required=False, help='Set default thermocouple net name')
@click.option('--uart-net', required=False, help='Set default UART net name')
@click.option('--arm-net', required=False, help='Set default robotic arm net name')
@click.option('--user', required=False, help='Set default username for box locking')
def add(box, serial_port, supply_net, battery_net, solar_net, scope_net, logic_net,
        adc_net, dac_net, gpio_net, debug_net, eload_net, usb_net, webcam_net,
        watt_meter_net, thermocouple_net, uart_net, arm_net, user):
    """
        Set default values
    """
    # Check if at least one option is provided
    if all(v is None for v in [box, serial_port, supply_net, battery_net, solar_net,
                                scope_net, logic_net, adc_net, dac_net, gpio_net, debug_net,
                                eload_net, usb_net, webcam_net, watt_meter_net, thermocouple_net,
                                uart_net, arm_net, user]):
        click.secho("Error: You must specify at least one setting", fg='red', err=True)
        click.echo("\nAvailable options:", err=True)
        click.echo("  --box <name>              Set default box", err=True)
        click.echo("  --serial-port <path>      Set default serial port", err=True)
        click.echo("  --supply-net <name>       Set default power supply net", err=True)
        click.echo("  --battery-net <name>      Set default battery net", err=True)
        click.echo("  --solar-net <name>        Set default solar net", err=True)
        click.echo("  --scope-net <name>        Set default oscilloscope net", err=True)
        click.echo("  --logic-net <name>        Set default logic analyzer net", err=True)
        click.echo("  --adc-net <name>          Set default ADC net", err=True)
        click.echo("  --dac-net <name>          Set default DAC net", err=True)
        click.echo("  --gpio-net <name>         Set default GPIO net", err=True)
        click.echo("  --debug-net <name>        Set default debug net", err=True)
        click.echo("  --eload-net <name>        Set default electronic load net", err=True)
        click.echo("  --usb-net <name>          Set default USB hub net", err=True)
        click.echo("  --webcam-net <name>       Set default webcam net", err=True)
        click.echo("  --watt-meter-net <name>   Set default watt meter net", err=True)
        click.echo("  --thermocouple-net <name> Set default thermocouple net", err=True)
        click.echo("  --uart-net <name>         Set default UART net", err=True)
        click.echo("  --arm-net <name>          Set default robotic arm net", err=True)
        click.echo("  --user <name>             Set default username for box locking", err=True)
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
            click.echo(f"  lager boxes add --name {box} --ip <TAILSCALE_IP>", err=True)
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
    net_defaults = {
        'supply_net': ('net_power_supply', supply_net),
        'battery_net': ('net_battery', battery_net),
        'solar_net': ('net_solar', solar_net),
        'scope_net': ('net_scope', scope_net),
        'logic_net': ('net_logic', logic_net),
        'adc_net': ('net_adc', adc_net),
        'dac_net': ('net_dac', dac_net),
        'gpio_net': ('net_gpio', gpio_net),
        'debug_net': ('net_debug', debug_net),
        'eload_net': ('net_eload', eload_net),
        'usb_net': ('net_usb', usb_net),
        'webcam_net': ('net_webcam', webcam_net),
        'watt_meter_net': ('net_watt_meter', watt_meter_net),
        'thermocouple_net': ('net_thermocouple', thermocouple_net),
        'uart_net': ('net_uart', uart_net),
        'arm_net': ('net_arm', arm_net),
    }

    for display_name, (config_key, value) in net_defaults.items():
        if value is not None:
            config.set('LAGER', config_key, value)
            # Convert display name to hyphenated format (e.g., power_supply_net -> power-supply-net)
            display = display_name.replace('_', '-')
            changes.append(f"{display}: {value}")

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
    ('thermocouple-net', 'net_thermocouple', 'thermocouple net'),
    ('uart-net', 'net_uart', 'UART net'),
    ('arm-net', 'net_arm', 'robotic arm net'),
]

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

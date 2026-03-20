# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    cli.context.core

    LagerContext class and core utility functions for CLI context management.
"""
import os

import click

from .session import DirectHTTPSession
from ..sort_utils import natural_sort_key


class LagerContext:  # pylint: disable=too-few-public-methods
    """
        Lager Context manager for direct box connections.
    """
    def __init__(self, ctx, defaults, debug, style, interpreter=None, force_command=False):
        self.defaults = defaults
        self.style = style
        self.debug = debug
        self.interpreter = interpreter
        self.force_command = force_command

    def get_session_for_box(self, box, box_name=None):
        """
        Get session for direct box connection.

        Args:
            box: IP address of box
            box_name: Optional box name (unused)

        Returns:
            DirectHTTPSession for direct HTTP communication
        """
        if self.debug:
            click.echo(f"[DEBUG] Using direct HTTP to {box}", err=True)

        return DirectHTTPSession(box)

    @property
    def default_box(self):
        """
            Get default box id from config
        """
        return self.defaults.get('gateway_id')

    @default_box.setter
    def default_box(self, box_id):
        self.defaults['gateway_id'] = str(box_id)


def get_default_box(ctx):
    """
        Check for a default box in config.
        Also checks if the box name is a local box and resolves it to an IP address.
    """
    import ipaddress
    from ..box_storage import get_box_ip, list_boxes

    name = os.getenv('LAGER_BOX')
    if name is None:
        name = ctx.obj.default_box

    if name is None:
        # No box specified - provide helpful error
        local_boxes = list_boxes()

        click.secho('No box specified and no default box configured.', fg='red', err=True)
        click.echo()

        if local_boxes:
            click.echo('Available boxes:')
            for box_name in sorted(local_boxes.keys(), key=natural_sort_key):
                click.echo(f'  - {box_name}')
            click.echo()
            click.echo('You can either:')
            click.echo('  1. Specify a box with: --box <name>')
            click.echo('  2. Set a default box with: lager defaults add --box <name>')
        else:
            click.echo('No boxes found in .lager file.')
            click.echo()
            click.echo('To add a box, run:')
            click.echo('  lager boxes add --name <name> --ip <ip-address>')
            click.echo()
            click.echo('Then you can either:')
            click.echo('  1. Specify a box with: --box <name>')
            click.echo('  2. Set a default box with: lager defaults add --box <name>')

        ctx.exit(1)

    # Check if the box name is a local box that should be resolved to an IP
    local_ip = get_box_ip(name)
    if local_ip:
        return local_ip

    # Check if it's a valid IP address
    try:
        ipaddress.ip_address(name)
        # It's a valid IP address, use it directly
        return name
    except ValueError:
        # Not a valid IP and not in local boxes - Show helpful error
        click.secho(f"Error: Box '{name}' not found.", fg='red', err=True)
        click.echo("", err=True)

        saved_boxes = list_boxes()
        if saved_boxes:
            click.echo("Available boxes:", err=True)
            for box_name, box_info in sorted(saved_boxes.items(), key=lambda x: natural_sort_key(x[0])):
                if isinstance(box_info, dict):
                    box_ip = box_info.get('ip', 'unknown')
                else:
                    box_ip = box_info
                click.echo(f"  - {box_name} ({box_ip})", err=True)
        else:
            click.echo("No boxes are currently saved.", err=True)

        click.echo("", err=True)
        click.echo("To add a new box, use:", err=True)
        click.echo(f"  lager boxes add --name {name} --ip <IP_ADDRESS>", err=True)
        ctx.exit(1)


def get_impl_path(filename):
    """
        Get the path to an implementation script in cli/impl/

        Searches subdirectories first (power/, measurement/, communication/, device/),
        then falls back to the root impl/ directory for backward compatibility.

        Args:
            filename: The implementation script filename (e.g., 'supply.py')

        Returns:
            Full path to the implementation script
    """
    base = os.path.dirname(os.path.dirname(__file__))
    impl_dir = os.path.join(base, 'impl')

    # Subdirectories to search (in order)
    subdirs = ['power', 'measurement', 'communication', 'device']

    # First check subdirectories
    for subdir in subdirs:
        subdir_path = os.path.join(impl_dir, subdir, filename)
        if os.path.exists(subdir_path):
            return subdir_path

    # Fall back to root impl/ directory (backward compatibility)
    return os.path.join(impl_dir, filename)


def get_default_net(ctx, net_type):
    """
    Get the default net name for a specific net type from config.

    Args:
        ctx: Click context
        net_type: Type of net (e.g., 'power_supply', 'battery', 'scope', etc.)

    Returns:
        Default net name if configured, None otherwise
    """
    from ..config import read_config_file

    config_key = f'net_{net_type}'
    config = read_config_file()

    if config.has_option('LAGER', config_key):
        return config.get('LAGER', config_key)

    return None


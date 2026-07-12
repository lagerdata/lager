# Copyright 2024-2026 Lager Data
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
    def __init__(self, ctx, defaults, debug, style, interpreter=None):
        self.defaults = defaults
        self.style = style
        self.debug = debug
        self.interpreter = interpreter

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

    from ..errors import LagerError

    name = os.getenv('LAGER_BOX')
    if name is None:
        name = ctx.obj.default_box

    if name is None:
        # No box specified - provide helpful, box-aware guidance.
        local_boxes = list_boxes()

        if local_boxes:
            box_lines = '\n'.join(
                f'      - {box_name}'
                for box_name in sorted(local_boxes.keys(), key=natural_sort_key)
            )
            raise LagerError(
                'No box specified, and no default box is configured.',
                cause='Your saved boxes:\n' + box_lines,
                fixes=[
                    'Pick one for this command: --box [BOX_NAME]',
                    'Or set a default once: lager defaults add --box [BOX_NAME]',
                ],
            )
        raise LagerError(
            'No box specified, and you have no saved boxes yet.',
            cause='Lager needs to know which box to talk to.',
            fixes=[
                'Add a box: lager boxes add --name [BOX_NAME] --ip [IP_ADDRESS]',
                'Then use --box [BOX_NAME], or set a default: lager defaults add --box [BOX_NAME]',
            ],
        )

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
        # Not a valid IP and not in local boxes - show an actionable error.
        from ..box_storage import box_not_found_error
        raise box_not_found_error(name)


def get_impl_path(filename):
    """
        Get the path to an implementation script in cli/impl/

        Searches subdirectories first (power/, measurement/, communication/, device/),
        then falls back to the root impl/ directory for backward compatibility.

        Args:
            filename: The implementation script filename (e.g., 'scope.py')

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


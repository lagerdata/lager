# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.i2c.commands

Commands for I2C (Inter-Integrated Circuit) communication.

I2C is a synchronous serial communication protocol using:
- SDA: Serial Data (bidirectional)
- SCL: Serial Clock

This module provides CLI commands for I2C operations via LabJack T7
and Aardvark adapters.
"""
from __future__ import annotations

import json
import re
import sys
from typing import List, Optional, Tuple

import click
import requests
from texttable import Texttable

from ...core.net_helpers import resolve_box
from ...context import get_impl_path, get_default_net
from ...options import force_command_option
from ..development.python import run_python_internal

I2C_ROLE = "i2c"


# Custom group class to handle --box after netname
class I2CGroup(click.Group):
    """
    Custom Click Group that allows --box option after NETNAME argument
    when no subcommand is invoked.

    This fixes: lager i2c i2c1 --box DEMO
    """

    def parse_args(self, ctx, args):
        # Check if we have a pattern like: NETNAME --box VALUE (no subcommand)
        if args and len(args) >= 3:
            if not args[0].startswith('-') and args[1] == '--box':
                netname = args[0]
                box_flag = args[1]
                box_value = args[2]
                rest = args[3:]
                args = [box_flag, box_value, netname] + rest

        return super().parse_args(ctx, args)


# ---------- helpers ----------

def _resolve_box_with_name(ctx, box):
    """
    Resolve box parameter to IP address.
    Returns tuple of (ip_address, box_name).
    """
    from ...box_storage import get_box_name_by_ip

    resolved_ip = resolve_box(ctx, box)

    if box and not box.replace('.', '').isdigit():
        resolved_name = box
    else:
        resolved_name = get_box_name_by_ip(resolved_ip)

    return (resolved_ip, resolved_name)


def _fetch_i2c_nets(ctx: click.Context, box_ip: str) -> list[dict]:
    """
    Fetch I2C nets from the box by reading saved_nets.json.
    """
    try:
        # This endpoint returns all saved nets; we filter client-side by role.
        box_url = f'http://{box_ip}:9000/uart/nets/list'
        response = requests.get(box_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            nets = data.get('nets', [])
            return [n for n in nets if n.get("role") == I2C_ROLE]
        return []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def _list_i2c_nets(ctx, box):
    """List all I2C nets on the box."""
    return _fetch_i2c_nets(ctx, box)


def _parse_frequency(freq_str: str) -> int:
    """
    Parse frequency string with optional suffix.

    Supports: "100000", "100k", "400k", "1M", "100000hz"
    """
    freq_str = freq_str.strip().lower()

    if freq_str.endswith('hz'):
        freq_str = freq_str[:-2]

    multiplier = 1
    if freq_str.endswith('m'):
        multiplier = 1_000_000
        freq_str = freq_str[:-1]
    elif freq_str.endswith('k'):
        multiplier = 1_000
        freq_str = freq_str[:-1]

    try:
        base_value = float(freq_str)
        result = int(base_value * multiplier)
    except ValueError:
        raise click.BadParameter(
            f"Invalid frequency '{freq_str}'. Use numeric value with optional suffix "
            f"(e.g., '100000', '100k', '400k', '1M')"
        )

    if result <= 0:
        raise click.BadParameter("Frequency must be a positive value")

    return result


def _parse_hex_data(data_str: str) -> List[int]:
    """
    Parse hex data string into list of byte values.

    Supports:
    - "0x0a" -> [0x0a]
    - "0x0a03" -> [0x0a, 0x03]
    - "0a 03" -> [0x0a, 0x03]
    - "0a,03" -> [0x0a, 0x03]
    """
    data_str = data_str.strip()

    has_separators = bool(re.search(r'[\s,:\-]', data_str))

    if has_separators:
        parts = re.split(r'[\s,:\-]+', data_str)
        parts = [p.strip() for p in parts if p.strip()]

        result = []
        for part in parts:
            if part.lower().startswith('0x'):
                part = part[2:]
            try:
                value = int(part, 16)
                if value > 0xFF:
                    raise click.BadParameter(
                        f"Hex value '0x{part}' exceeds byte range (max 0xFF)"
                    )
                result.append(value)
            except ValueError:
                raise click.BadParameter(
                    f"Invalid hex value '{part}'. Use hex format (e.g., '0a', '03')"
                )
        return result
    else:
        if data_str.lower().startswith('0x'):
            data_str = data_str[2:]

        # Pad to even length
        if len(data_str) % 2:
            data_str = '0' + data_str

        pairs = [data_str[i:i+2] for i in range(0, len(data_str), 2)]

        result = []
        for pair in pairs:
            try:
                value = int(pair, 16)
                result.append(value)
            except ValueError:
                raise click.BadParameter(
                    f"Invalid hex value '{pair}'. Use hex format (e.g., '0a', '03')"
                )
        return result


def _parse_address(addr_str: str) -> int:
    """
    Parse I2C device address string.

    Validates 7-bit range (0x00-0x7F).

    Supports: "0x48", "48", "72" (decimal)
    """
    addr_str = addr_str.strip().lower()

    try:
        if addr_str.startswith('0x'):
            value = int(addr_str, 16)
        elif all(c in '0123456789abcdef' for c in addr_str) and len(addr_str) <= 2:
            value = int(addr_str, 16)
        else:
            value = int(addr_str)
    except ValueError:
        raise click.BadParameter(
            f"Invalid I2C address '{addr_str}'. "
            f"Use hex (0x48) or decimal (72)"
        )

    if value < 0x00 or value > 0x7F:
        raise click.BadParameter(
            f"I2C address 0x{value:02x} out of 7-bit range (0x00-0x7F)"
        )

    return value


def _read_data_file(filepath: str) -> List[int]:
    """Read binary data from file."""
    try:
        with open(filepath, 'rb') as f:
            return list(f.read())
    except (IOError, OSError) as e:
        raise click.BadParameter(f"Could not read data file '{filepath}': {e}")


def display_nets(ctx, box, netname: Optional[str] = None):
    """Display I2C nets with their configuration parameters."""
    i2c_nets = _list_i2c_nets(ctx, box)

    if not i2c_nets:
        click.echo("No I2C nets found on this box.")
        click.echo("\nTo create an I2C net, add to saved_nets.json:")
        click.echo('  {')
        click.echo('    "name": "my_i2c",')
        click.echo('    "role": "i2c",')
        click.echo('    "instrument": "labjack_t7",')
        click.echo('    "params": {')
        click.echo('      "sda_pin": 4, "scl_pin": 5')
        click.echo('    }')
        click.echo('  }')
        return

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_max_width(0)
    table.set_cols_dtype(["t", "t", "t", "t", "t"])
    table.set_cols_align(["l", "l", "l", "l", "l"])
    table.header(["Name", "Instrument", "Pins (SDA/SCL)", "Frequency", "Pull-ups"])

    for rec in i2c_nets:
        if netname is None or netname == rec.get("name"):
            name = rec.get("name", "")
            instrument = rec.get("instrument", "labjack_t7")
            params = rec.get("params", {})

            # Pin configuration
            pin_field = rec.get("pin", "")
            if pin_field == "I2C0":
                pins = "Fixed (SDA/SCL)"
            elif pin_field.startswith("FIO") and "-" in pin_field:
                pins = pin_field.replace("-", "/")
            elif params.get("sda_pin") is not None:
                sda = params.get("sda_pin", "?")
                scl = params.get("scl_pin", "?")
                pins = f"FIO{sda}/FIO{scl}"
            else:
                pins = pin_field if pin_field else "?/?"

            # I2C parameters
            freq = params.get("frequency_hz", 100_000)
            freq_str = f"{freq/1_000_000:.1f}M" if freq >= 1_000_000 else f"{freq/1000:.0f}k"
            pull_ups = "on" if params.get("pull_ups") else "off"

            table.add_row([name, instrument, pins, freq_str, pull_ups])

    click.echo(table.draw())


def _run_i2c_backend(ctx, box_ip, action: str, **params):
    """Run I2C backend command."""
    data = {
        "action": action,
        "params": params,
    }
    try:
        run_python_internal(
            ctx,
            get_impl_path("i2c.py"),
            box_ip,
            env=(f"LAGER_COMMAND_DATA={json.dumps(data)}",),
            passenv=(),
            kill=False,
            download=(),
            allow_overwrite=False,
            signum="SIGTERM",
            timeout=0,
            detach=False,
            port=(),
            org=None,
            args=(),
        )
    except SystemExit as e:
        if e.code != 0:
            raise


# ---------- CLI ----------

@click.group(name="i2c", cls=I2CGroup, invoke_without_command=True)
@click.argument("NETNAME", required=False)
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@force_command_option
def i2c(ctx, netname, box):
    """Perform I2C data transfers"""
    if netname is None:
        netname = get_default_net(ctx, 'i2c')

    if netname is not None:
        ctx.obj.netname = netname

    # Store box param for subcommands
    ctx.obj.i2c_box_param = box

    if ctx.invoked_subcommand is None:
        target_box, _ = _resolve_box_with_name(ctx, box)

        if not netname:
            display_nets(ctx, target_box, None)
        else:
            display_nets(ctx, target_box, netname)


@i2c.command()
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--frequency', default=None,
              help='Clock frequency (e.g., 100k, 400k)')
@click.option('--pull-ups', type=click.Choice(["on", "off"]), default=None,
              help='Enable/disable internal pull-ups (Aardvark only)')
def config(ctx, box, frequency, pull_ups):
    """
    Configure I2C bus parameters.

    Example:
      lager i2c MY_I2C config --frequency 400k --pull-ups on
    """
    box_param = box or getattr(ctx.obj, 'i2c_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No I2C net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    params = {
        "netname": netname,
    }
    if frequency is not None:
        params["frequency_hz"] = _parse_frequency(frequency)
    if pull_ups is not None:
        params["pull_ups"] = (pull_ups == "on")

    _run_i2c_backend(ctx, box_ip, "config", **params)


@i2c.command()
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--start', default="0x08",
              help='Start address in hex (default: 0x08)')
@click.option('--end', default="0x77",
              help='End address in hex (default: 0x77)')
def scan(ctx, box, start, end):
    """
    Scan I2C bus for connected devices.

    Probes addresses and reports those that ACK.

    Example:
      lager i2c MY_I2C scan
    """
    box_param = box or getattr(ctx.obj, 'i2c_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No I2C net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    start_addr = _parse_address(start)
    end_addr = _parse_address(end)

    _run_i2c_backend(
        ctx, box_ip, "scan",
        netname=netname,
        start_addr=start_addr,
        end_addr=end_addr,
    )


@i2c.command()
@click.argument("NUM_BYTES", type=click.IntRange(min=0))
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--address', required=True,
              help='Device address in hex (e.g., 0x48)')
@click.option('--frequency', default=None,
              help='Clock frequency override (e.g., 100k, 400k)')
@click.option('--format', 'output_format', type=click.Choice(["hex", "bytes", "json"]), default="hex",
              help='Output format')
def read(ctx, num_bytes, box, address, frequency, output_format):
    """
    Read bytes from an I2C device.

    Example:
      lager i2c MY_I2C read 4 --address 0x48
    """
    box_param = box or getattr(ctx.obj, 'i2c_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No I2C net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    addr = _parse_address(address)

    overrides = {}
    if frequency is not None:
        overrides['frequency_hz'] = _parse_frequency(frequency)

    _run_i2c_backend(
        ctx, box_ip, "read",
        netname=netname,
        address=addr,
        num_bytes=num_bytes,
        output_format=output_format,
        overrides=overrides if overrides else None,
    )


@i2c.command()
@click.argument("DATA", required=False, default=None)
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--address', required=True,
              help='Device address in hex (e.g., 0x48)')
@click.option('--data-file', type=click.Path(exists=True), default=None,
              help='File containing data to write')
@click.option('--frequency', default=None,
              help='Clock frequency override (e.g., 100k, 400k)')
@click.option('--format', 'output_format', type=click.Choice(["hex", "bytes", "json"]), default="hex",
              help='Output format')
def write(ctx, data, box, address, data_file, frequency, output_format):
    """
    Write bytes to an I2C device.

    Example:
      lager i2c MY_I2C write 0x0A03 --address 0x48
    """
    box_param = box or getattr(ctx.obj, 'i2c_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No I2C net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    addr = _parse_address(address)

    # Parse data from argument or file
    data_bytes = []
    if data:
        data_bytes = _parse_hex_data(data)
    elif data_file:
        data_bytes = _read_data_file(data_file)
    else:
        click.secho("No data provided. Provide DATA argument or --data-file.", fg="red", err=True)
        ctx.exit(1)

    overrides = {}
    if frequency is not None:
        overrides['frequency_hz'] = _parse_frequency(frequency)

    _run_i2c_backend(
        ctx, box_ip, "write",
        netname=netname,
        address=addr,
        data=data_bytes,
        output_format=output_format,
        overrides=overrides if overrides else None,
    )


@i2c.command()
@click.argument("NUM_BYTES", type=click.IntRange(min=0))
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--address', required=True,
              help='Device address in hex (e.g., 0x48)')
@click.option('--data', 'data_str', default=None,
              help='Data to write before reading (hex)')
@click.option('--data-file', type=click.Path(exists=True), default=None,
              help='File containing data to write')
@click.option('--frequency', default=None,
              help='Clock frequency override (e.g., 100k, 400k)')
@click.option('--format', 'output_format', type=click.Choice(["hex", "bytes", "json"]), default="hex",
              help='Output format')
def transfer(ctx, num_bytes, box, address, data_str, data_file, frequency, output_format):
    """
    Write then read in a single I2C transaction (repeated start).

    Common pattern: write register address, read register value.

    Example:
      lager i2c MY_I2C transfer 2 --address 0x48 --data 0x0A
    """
    box_param = box or getattr(ctx.obj, 'i2c_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No I2C net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    addr = _parse_address(address)

    # Parse data from option or file
    data_bytes = []
    if data_str:
        data_bytes = _parse_hex_data(data_str)
    elif data_file:
        data_bytes = _read_data_file(data_file)

    overrides = {}
    if frequency is not None:
        overrides['frequency_hz'] = _parse_frequency(frequency)

    _run_i2c_backend(
        ctx, box_ip, "transfer",
        netname=netname,
        address=addr,
        num_bytes=num_bytes,
        data=data_bytes,
        output_format=output_format,
        overrides=overrides if overrides else None,
    )

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.spi.commands

Commands for SPI (Serial Peripheral Interface) communication.

SPI is a synchronous serial communication protocol using:
- SCLK: Serial Clock
- MOSI: Master Out Slave In
- MISO: Master In Slave Out
- CS: Chip Select

This module provides CLI commands for SPI operations via LabJack T7
(with future support for Aardvark and FTDI adapters).
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

SPI_ROLE = "spi"


# Custom group class to handle --box after netname
class SPIGroup(click.Group):
    """
    Custom Click Group that allows --box option after NETNAME argument
    when no subcommand is invoked.

    This fixes: lager spi spi1 --box DEMO
    """

    def parse_args(self, ctx, args):
        # Check if we have a pattern like: NETNAME --box VALUE (no subcommand)
        # We need to extract --box from args if it appears after what looks like a netname
        if args and len(args) >= 3:
            # Check if first arg doesn't start with - (it's the netname)
            # and second arg is --box
            if not args[0].startswith('-') and args[1] == '--box':
                # Reorder: move --box VALUE before NETNAME
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
    Returns tuple of (ip_address, box_name) where box_name is used for username lookup.
    """
    from ...box_storage import get_box_name_by_ip

    resolved_ip = resolve_box(ctx, box)

    if box and not box.replace('.', '').isdigit():
        resolved_name = box
    else:
        resolved_name = get_box_name_by_ip(resolved_ip)

    return (resolved_ip, resolved_name)


def _fetch_spi_nets(ctx: click.Context, box_ip: str) -> list[dict]:
    """
    Fetch SPI nets from the box by reading saved_nets.json.
    """
    try:
        box_url = f'http://{box_ip}:9000/nets/list'
        response = requests.get(box_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            nets = data.get('nets', [])
            return [n for n in nets if n.get("role") == SPI_ROLE]
        else:
            # Fallback: this endpoint returns all saved nets despite the URL path.
            box_url = f'http://{box_ip}:9000/uart/nets/list'
            response = requests.get(box_url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                nets = data.get('nets', [])
                return [n for n in nets if n.get("role") == SPI_ROLE]
            return []
    except (requests.RequestException, json.JSONDecodeError):
        return []


def _list_spi_nets(ctx, box):
    """List all SPI nets on the box."""
    return _fetch_spi_nets(ctx, box)


def _get_spi_net(ctx, box, netname):
    """Get a specific SPI net by name."""
    nets = _list_spi_nets(ctx, box)
    for net in nets:
        if net.get("name") == netname:
            return net
    return None


def _parse_frequency(freq_str: str) -> int:
    """
    Parse frequency string with optional suffix.

    Supports:
    - Plain numbers: "1000000" -> 1000000
    - Hz suffix: "1000000hz" -> 1000000
    - k suffix: "1000k" -> 1000000
    - K suffix: "1000K" -> 1000000
    - M suffix: "1M" -> 1000000
    - m suffix: "1m" -> 1000000

    Returns:
        Frequency in Hz as integer
    """
    freq_str = freq_str.strip().lower()

    # Remove optional 'hz' suffix
    if freq_str.endswith('hz'):
        freq_str = freq_str[:-2]

    # Handle multiplier suffixes
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
            f"(e.g., '1000000', '1M', '500k', '1000000hz')"
        )

    if result <= 0:
        raise click.BadParameter("Frequency must be a positive value")

    return result


def _parse_hex_data(data_str: str, word_size: int = None) -> List[int]:
    """
    Parse hex data string into list of values.

    Supports:
    - "0x9f" -> [0x9f]
    - "0x9f01" -> [0x9f, 0x01] (for 8-bit) or [0x9f01] (for 16-bit or unspecified)
    - "9f 01 02" -> [0x9f, 0x01, 0x02]
    - "9f,01,02" -> [0x9f, 0x01, 0x02]
    - "0x1234 0x5678" -> [0x1234, 0x5678] (for 16-bit word size)

    When word_size is None (not explicitly specified), continuous hex strings
    are parsed as a single value.  Byte-pair splitting only occurs when
    word_size is explicitly 8.

    Args:
        data_str: Hex string to parse
        word_size: Word size in bits (8, 16, or 32), or None if not specified.

    Returns:
        List of word values as integers
    """
    data_str = data_str.strip()

    # Determine max value based on word size
    if word_size is not None:
        max_value = (1 << word_size) - 1  # 0xFF for 8-bit, 0xFFFF for 16-bit, etc.
        hex_digits_per_word = word_size // 4  # 2 for 8-bit, 4 for 16-bit, 8 for 32-bit
    else:
        max_value = None  # No range check -- validated on box side
        hex_digits_per_word = None

    # Check if data has separators (spaces, commas, colons)
    has_separators = bool(re.search(r'[\s,:\-]', data_str))

    if has_separators:
        # Split by separators - each part is a separate word
        parts = re.split(r'[\s,:\-]+', data_str)
        parts = [p.strip() for p in parts if p.strip()]

        result = []
        for part in parts:
            # Remove 0x prefix if present
            if part.lower().startswith('0x'):
                part = part[2:]

            try:
                value = int(part, 16)
                if max_value is not None and value > max_value:
                    raise click.BadParameter(
                        f"Hex value '0x{part}' exceeds {word_size}-bit range "
                        f"(max 0x{max_value:X})"
                    )
                result.append(value)
            except ValueError:
                raise click.BadParameter(
                    f"Invalid hex value '{part}'. Use hex format (e.g., '9f', '1234')"
                )
        return result
    else:
        # No separators - parse as continuous hex string
        # Remove 0x prefix if present
        raw_str = data_str
        if data_str.lower().startswith('0x'):
            data_str = data_str[2:]

        # For explicit 8-bit words, split into byte pairs
        if word_size == 8:
            # Check if the entire value exceeds the word size before splitting.
            # e.g., 0x1FF (511) should be rejected for 8-bit word size even though
            # it pads to "01FF" and splits into valid-looking bytes [0x01, 0xFF].
            try:
                full_value = int(data_str, 16)
            except ValueError:
                raise click.BadParameter(
                    f"Invalid hex value '{raw_str}'. Use hex format (e.g., '9f', '01')"
                )
            if full_value > max_value and len(data_str) <= hex_digits_per_word + 1:
                # Single value that exceeds the word size
                raise click.BadParameter(
                    f"Hex value '0x{data_str}' exceeds {word_size}-bit range "
                    f"(max 0x{max_value:X})"
                )

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
                        f"Invalid hex value '{pair}'. Use hex format (e.g., '9f', '01')"
                    )
            return result
        else:
            # For 16/32-bit words or when word_size not specified (None),
            # treat the whole string as one value
            try:
                value = int(data_str, 16)
                if max_value is not None and value > max_value:
                    raise click.BadParameter(
                        f"Hex value '0x{data_str}' exceeds {word_size}-bit range "
                        f"(max 0x{max_value:X})"
                    )
                return [value]
            except ValueError:
                raise click.BadParameter(
                    f"Invalid hex value '{data_str}'. Use hex format"
                )


def _parse_fill_byte(fill_str: str) -> int:
    """
    Parse fill byte value (hex or decimal).

    Supports:
    - "0xff" -> 255
    - "0xFF" -> 255
    - "ff" -> 255
    - "255" -> 255
    """
    fill_str = fill_str.strip().lower()

    try:
        if fill_str.startswith('0x'):
            return int(fill_str, 16)
        elif all(c in '0123456789abcdef' for c in fill_str) and len(fill_str) <= 2:
            return int(fill_str, 16)
        else:
            return int(fill_str)
    except ValueError:
        raise click.BadParameter(
            f"Invalid fill value '{fill_str}'. Use hex (0xff) or decimal (255)"
        )


def _read_data_file(filepath: str) -> List[int]:
    """
    Read binary data from file.

    Returns:
        List of byte values
    """
    try:
        with open(filepath, 'rb') as f:
            return list(f.read())
    except (IOError, OSError) as e:
        raise click.BadParameter(f"Could not read data file '{filepath}': {e}")


def display_nets(ctx, box, netname: Optional[str] = None):
    """Display SPI nets with their configuration parameters."""
    spi_nets = _list_spi_nets(ctx, box)

    if not spi_nets:
        click.echo("No SPI nets found on this box.")
        click.echo("\nTo create an SPI net, add to saved_nets.json:")
        click.echo('  {')
        click.echo('    "name": "my_spi",')
        click.echo('    "role": "spi",')
        click.echo('    "instrument": "labjack_t7",')
        click.echo('    "params": {')
        click.echo('      "cs_pin": 0, "clk_pin": 1, "mosi_pin": 2, "miso_pin": 3')
        click.echo('    }')
        click.echo('  }')
        return

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_max_width(0)  # Disable wrapping
    table.set_cols_dtype(["t", "t", "t", "t", "t", "t", "t"])
    table.set_cols_align(["l", "l", "l", "l", "l", "l", "l"])
    table.header(["Name", "Instrument", "Pins (CS/CLK/MOSI/MISO)", "Mode", "Frequency", "Word Size", "Bit Order"])

    for rec in spi_nets:
        if netname is None or netname == rec.get("name"):
            name = rec.get("name", "")
            instrument = rec.get("instrument", "labjack_t7")
            params = rec.get("params", {})

            # Pin configuration - check both params and pin field
            pin_field = rec.get("pin", "")
            if pin_field == "SPI0":
                # Aardvark - fixed hardware pins
                pins = "Fixed (MOSI/MISO/SCK/SS)"
            elif pin_field == "FIO0-FIO3":
                # Standard LabJack SPI pin mapping
                pins = "FIO0/FIO1/FIO2/FIO3"
            elif params.get("cs_pin") or params.get("clk_pin"):
                # Legacy params-based configuration
                cs = params.get("cs_pin", "?")
                clk = params.get("clk_pin", "?")
                mosi = params.get("mosi_pin", "?")
                miso = params.get("miso_pin", "?")
                pins = f"{cs}/{clk}/{mosi}/{miso}"
            else:
                pins = pin_field if pin_field else "?/?/?/?"

            # SPI parameters
            mode = params.get("mode", 0)
            freq = params.get("frequency_hz", 1_000_000)
            freq_str = f"{freq/1_000_000:.1f}M" if freq >= 1_000_000 else f"{freq/1000:.0f}k"
            word_size = params.get("word_size", 8)
            bit_order = params.get("bit_order", "msb").upper()

            table.add_row([name, instrument, pins, str(mode), freq_str, str(word_size), bit_order])

    click.echo(table.draw())


def _run_spi_backend(ctx, box_ip, action: str, **params):
    """Run SPI backend command."""
    data = {
        "action": action,
        "params": params,
    }
    try:
        run_python_internal(
            ctx,
            get_impl_path("spi.py"),
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

@click.group(name="spi", cls=SPIGroup, invoke_without_command=True)
@click.argument("NETNAME", required=False)
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@force_command_option
def spi(ctx, netname, box):
    """Perform SPI data transfers"""
    # Store netname and box on context object (LagerContext)
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'spi')

    if netname is not None:
        ctx.obj.netname = netname

    # Store box param for subcommands
    ctx.obj.spi_box_param = box

    if ctx.invoked_subcommand is None:
        # No subcommand - list nets or show net info
        target_box, _ = _resolve_box_with_name(ctx, box)

        if not netname:
            display_nets(ctx, target_box, None)
        else:
            # Show specific net configuration
            display_nets(ctx, target_box, netname)


@spi.command()
@click.argument("NUM_WORDS", type=int)
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--mode', type=click.Choice(["0", "1", "2", "3"]), default=None,
              help='SPI mode (0-3)')
@click.option('--bit-order', type=click.Choice(["msb", "lsb"]), default=None,
              help='Bit order')
@click.option('--frequency', default=None,
              help='Clock frequency (e.g., 1M, 500k, 1000000hz)')
@click.option('--cs-active', type=click.Choice(["low", "high"]), default=None,
              help='Chip select polarity')
@click.option('--keep-cs', is_flag=True, default=False,
              help='Keep CS asserted after transfer')
@click.option('--word-size', type=click.Choice(["8", "16", "32"]), default=None,
              help='Word size in bits')
@click.option('--data', 'data_str', default=None,
              help='Data to transmit (hex string, e.g., 0x9f01)')
@click.option('--data-file', type=click.Path(exists=True), default=None,
              help='File containing data to transmit')
@click.option('--fill', default="0xFF",
              help='Fill byte for padding (default: 0xFF)')
@click.option('--format', 'output_format', type=click.Choice(["hex", "bytes", "json"]), default="hex",
              help='Output format')
def transfer(ctx, num_words, box, mode, bit_order, frequency, cs_active, keep_cs,
             word_size, data_str, data_file, fill, output_format):
    """
    Perform SPI transfer.

    Sends data and receives response simultaneously (full-duplex).
    If data is shorter than NUM_WORDS, pads with --fill value.
    If data is longer than NUM_WORDS, truncates.

    Examples:

      \b
      # Read device ID (send 0x9f command, read 3 bytes)
      lager spi MY_SPI transfer --data 0x9f 4

      \b
      # Send data at 5MHz
      lager spi MY_SPI transfer --data "01 02 03 04" --frequency 5M 4

      \b
      # Output as JSON
      lager spi MY_SPI transfer --data 0x9f 4 --format json
    """
    # Use box from subcommand option, fall back to group-level option
    box_param = box or getattr(ctx.obj, 'spi_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No SPI net specified and no default configured.", fg="red", err=True)
        click.echo("Provide a net name or set a default.", err=True)
        ctx.exit(1)

    # Determine word size for parsing (None if not explicitly specified)
    ws = int(word_size) if word_size else None

    # Parse data with appropriate word size
    data = []
    if data_str:
        data = _parse_hex_data(data_str, word_size=ws)
    elif data_file:
        data = _read_data_file(data_file)

    # Parse fill value
    fill_value = _parse_fill_byte(fill)

    # Build overrides
    overrides = {}
    if mode is not None:
        overrides['mode'] = int(mode)
    if bit_order is not None:
        overrides['bit_order'] = bit_order
    if frequency is not None:
        overrides['frequency_hz'] = _parse_frequency(frequency)
    if cs_active is not None:
        overrides['cs_active'] = cs_active
    if word_size is not None:
        overrides['word_size'] = ws

    _run_spi_backend(
        ctx, box_ip, "transfer",
        netname=netname,
        n_words=num_words,
        data=data,
        fill=fill_value,
        keep_cs=keep_cs,
        output_format=output_format,
        overrides=overrides if overrides else None,
    )


@spi.command()
@click.argument("NUM_WORDS", type=int)
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--mode', type=click.Choice(["0", "1", "2", "3"]), default=None,
              help='SPI mode (0-3)')
@click.option('--bit-order', type=click.Choice(["msb", "lsb"]), default=None,
              help='Bit order')
@click.option('--frequency', default=None,
              help='Clock frequency (e.g., 1M, 500k)')
@click.option('--cs-active', type=click.Choice(["low", "high"]), default=None,
              help='Chip select polarity')
@click.option('--keep-cs', is_flag=True, default=False,
              help='Keep CS asserted after transfer')
@click.option('--word-size', type=click.Choice(["8", "16", "32"]), default=None,
              help='Word size in bits')
@click.option('--fill', default="0xFF",
              help='Fill byte to send while reading (default: 0xFF)')
@click.option('--format', 'output_format', type=click.Choice(["hex", "bytes", "json"]), default="hex",
              help='Output format')
def read(ctx, num_words, box, mode, bit_order, frequency, cs_active, keep_cs,
         word_size, fill, output_format):
    """
    Read data from SPI slave.

    Sends fill bytes while reading response.

    Example:
      lager spi MY_SPI read 5 --fill 0xFF
    """
    box_param = box or getattr(ctx.obj, 'spi_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No SPI net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    fill_value = _parse_fill_byte(fill)

    overrides = {}
    if mode is not None:
        overrides['mode'] = int(mode)
    if bit_order is not None:
        overrides['bit_order'] = bit_order
    if frequency is not None:
        overrides['frequency_hz'] = _parse_frequency(frequency)
    if cs_active is not None:
        overrides['cs_active'] = cs_active
    if word_size is not None:
        overrides['word_size'] = int(word_size)

    _run_spi_backend(
        ctx, box_ip, "read",
        netname=netname,
        n_words=num_words,
        fill=fill_value,
        keep_cs=keep_cs,
        output_format=output_format,
        overrides=overrides if overrides else None,
    )


@spi.command()
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--mode', type=click.Choice(["0", "1", "2", "3"]), default=None,
              help='SPI mode (0-3)')
@click.option('--bit-order', type=click.Choice(["msb", "lsb"]), default=None,
              help='Bit order')
@click.option('--frequency', default=None,
              help='Clock frequency (e.g., 1M, 500k)')
@click.option('--cs-active', type=click.Choice(["low", "high"]), default=None,
              help='Chip select polarity')
@click.option('--word-size', type=click.Choice(["8", "16", "32"]), default=None,
              help='Word size in bits')
@click.option('--cs-mode', type=click.Choice(["auto", "manual"]), default=None,
              help='CS assertion mode: auto (hardware SS) or manual (user-managed GPIO)')
def config(ctx, box, mode, bit_order, frequency, cs_active, word_size, cs_mode):
    """
    Configure SPI parameters.

    Sets the SPI configuration for subsequent transfers.

    Example:

      \b
      lager spi MY_SPI config --mode 0 --frequency 5M --word-size 8

      \b
      # Use manual CS mode for Aardvark (manage CS via separate GPIO net)
      lager spi MY_SPI config --cs-mode manual
    """
    box_param = box or getattr(ctx.obj, 'spi_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No SPI net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    params = {"netname": netname}
    if mode is not None:
        params["mode"] = int(mode)
    if bit_order is not None:
        params["bit_order"] = bit_order
    if frequency is not None:
        params["frequency_hz"] = _parse_frequency(frequency)
    if word_size is not None:
        params["word_size"] = int(word_size)
    if cs_active is not None:
        params["cs_active"] = cs_active
    if cs_mode is not None:
        params["cs_mode"] = cs_mode

    _run_spi_backend(ctx, box_ip, "config", **params)


@spi.command()
@click.argument("DATA", required=True)
@click.pass_context
@click.option('--box', required=False, help="Lagerbox name or IP")
@click.option('--mode', type=click.Choice(["0", "1", "2", "3"]), default=None,
              help='SPI mode (0-3)')
@click.option('--bit-order', type=click.Choice(["msb", "lsb"]), default=None,
              help='Bit order')
@click.option('--frequency', default=None,
              help='Clock frequency (e.g., 1M, 500k)')
@click.option('--cs-active', type=click.Choice(["low", "high"]), default=None,
              help='Chip select polarity')
@click.option('--keep-cs', is_flag=True, default=False,
              help='Keep CS asserted after transfer')
@click.option('--word-size', type=click.Choice(["8", "16", "32"]), default=None,
              help='Word size in bits')
@click.option('--format', 'output_format', type=click.Choice(["hex", "bytes", "json"]), default="hex",
              help='Output format')
def write(ctx, data, box, mode, bit_order, frequency, cs_active, keep_cs,
          word_size, output_format):
    """
    Write data to SPI slave.

    Performs a full-duplex transfer but outputs the received data.

    Example:
      lager spi MY_SPI write 0x9f01020304
    """
    box_param = box or getattr(ctx.obj, 'spi_box_param', None)
    box_ip, _ = _resolve_box_with_name(ctx, box_param)

    netname = getattr(ctx.obj, 'netname', None)
    if not netname:
        click.secho("No SPI net specified and no default configured.", fg="red", err=True)
        ctx.exit(1)

    # Determine word size for parsing (None if not explicitly specified)
    ws = int(word_size) if word_size else None

    data_bytes = _parse_hex_data(data, word_size=ws)

    overrides = {}
    if mode is not None:
        overrides['mode'] = int(mode)
    if bit_order is not None:
        overrides['bit_order'] = bit_order
    if frequency is not None:
        overrides['frequency_hz'] = _parse_frequency(frequency)
    if cs_active is not None:
        overrides['cs_active'] = cs_active
    if word_size is not None:
        overrides['word_size'] = ws

    _run_spi_backend(
        ctx, box_ip, "read_write",
        netname=netname,
        data=data_bytes,
        keep_cs=keep_cs,
        output_format=output_format,
        overrides=overrides if overrides else None,
    )

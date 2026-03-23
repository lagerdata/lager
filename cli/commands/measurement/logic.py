# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    Logic commands (using local nets)
"""
from __future__ import annotations

import click
from ...context import get_default_net
from ...core.net_helpers import (
    require_netname,
    resolve_box,
    validate_net,
    display_nets,
    run_backend,
)

LOGIC_ROLE = "logic"


# ---------- helpers ----------

def _require_netname(ctx) -> str:
    return require_netname(ctx, "logic")


def _resolve_box(ctx, box):
    return resolve_box(ctx, box)


def _validate_logic_net(ctx, box_ip: str, netname: str) -> bool:
    """Validate net exists and is a logic net, exit with error if not."""
    if not validate_net(ctx, box_ip, netname, LOGIC_ROLE):
        click.secho(f"Error: '{netname}' is not a logic net", fg="red", err=True)
        click.secho("Use 'lager logic --box <box>' to list available logic nets.", err=True)
        ctx.exit(1)
    return True

# ---------- CLI ----------

@click.group(invoke_without_command=True)
@click.argument("NETNAME", required=False)
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
def logic(ctx, box, netname):
    """Control logic analyzer channels and triggers"""
    # Use provided netname, or fall back to default if not provided
    if netname is None:
        netname = get_default_net(ctx, 'logic')

    if netname is not None:
        ctx.obj.netname = netname

    if ctx.invoked_subcommand is None:
        box_ip = _resolve_box(ctx, box)
        display_nets(ctx, box_ip, None, LOGIC_ROLE, "logic")


def _run_backend(ctx, dut, action: str, **params):
    """Run backend command for logic operations"""
    return run_backend(ctx, dut, "enable_disable.py", action, **params)


@logic.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def disable(ctx, box, mcu):
    """
        Disable Net
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_backend(ctx, box_ip, "disable_net", netname=netname, mcu=mcu)

@logic.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def enable(ctx, box, mcu):
    """
        Enable Net
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_backend(ctx, box_ip, "enable_net", netname=netname, mcu=mcu)

@logic.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def start(ctx, box, mcu):
    """
        Start waveform capture
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_backend(ctx, box_ip, "start_capture", netname=netname, mcu=mcu)

@logic.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def start_single(ctx, box, mcu):
    """
        Start a single waveform capture
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_backend(ctx, box_ip, "start_single", netname=netname, mcu=mcu)

@logic.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--mcu", required=False)
def stop(ctx, box, mcu):
    """
        Stop waveform capture
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_backend(ctx, box_ip, "stop_capture", netname=netname, mcu=mcu)


@logic.group()
def measure():
    """
        Measure characteristics of logic nets
    """
    pass

def _run_measurement_backend(ctx, dut, action: str, **params):
    """Run backend command for measurement operations"""
    return run_backend(ctx, dut, "measurement.py", action, **params)


def _run_trigger_backend(ctx, dut, action: str, **params):
    """Run backend command for trigger operations"""
    return run_backend(ctx, dut, "trigger.py", action, **params)


def _run_cursor_backend(ctx, dut, action: str, **params):
    """Run backend command for cursor operations"""
    return run_backend(ctx, dut, "cursor.py", action, **params)


@measure.command()
@click.pass_context
@click.option("--mcu", required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option("--display", default=False, type=click.BOOL, help="Display measurement on screen")
@click.option("--cursor", default=False, type=click.BOOL, help="Enable measurement cursor")
def period(ctx, mcu, box, display, cursor):
    """
    Measure period of captured net waveform
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_measurement_backend(ctx, box_ip, "measure_period", netname=netname, display=display, cursor=cursor, mcu=mcu)

@measure.command()
@click.pass_context
@click.option('--mcu', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--display', default=False, type=click.BOOL, help='Display measurement on screen')
@click.option('--cursor', default=False, type=click.BOOL, help='Enable measurement cursor')
def freq(ctx, mcu, box, display, cursor):
    """
    Measure frequency of captured net waveform
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_measurement_backend(ctx, box_ip, "measure_freq", netname=netname, display=display, cursor=cursor, mcu=mcu)

@measure.command()
@click.pass_context
@click.option('--mcu', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--display', default=False, type=click.BOOL, help='Display measurement on screen')
@click.option('--cursor', default=False, type=click.BOOL, help='Enable measurement cursor')
def dc_pos(ctx, mcu, box, display, cursor):
    """
    Measure positive duty cycle
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_measurement_backend(ctx, box_ip, "measure_dc_pos", netname=netname, display=display, cursor=cursor, mcu=mcu)

@measure.command()
@click.pass_context
@click.option('--mcu', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--display', default=False, type=click.BOOL, help='Display measurement on screen')
@click.option('--cursor', default=False, type=click.BOOL, help='Enable measurement cursor')
def dc_neg(ctx, mcu, box, display, cursor):
    """
    Measure negative duty cycle
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_measurement_backend(ctx, box_ip, "measure_dc_neg", netname=netname, display=display, cursor=cursor, mcu=mcu)

@measure.command()
@click.pass_context
@click.option('--mcu', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--display', default=False, type=click.BOOL, help='Display measurement on screen')
@click.option('--cursor', default=False, type=click.BOOL, help='Enable measurement cursor')
def pw_pos(ctx, mcu, box, display, cursor):
    """
    Measure positive pulse width
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_measurement_backend(ctx, box_ip, "measure_pw_pos", netname=netname, display=display, cursor=cursor, mcu=mcu)

@measure.command()
@click.pass_context
@click.option('--mcu', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--display', default=False, type=click.BOOL, help='Display measurement on screen')
@click.option('--cursor', default=False, type=click.BOOL, help='Enable measurement cursor')
def pw_neg(ctx, mcu, box, display, cursor):
    """
    Measure negative pulse width
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_measurement_backend(ctx, box_ip, "measure_pw_neg", netname=netname, display=display, cursor=cursor, mcu=mcu)


@logic.group()
def trigger():
    """
        Set up trigger properties for logic nets
    """
    pass


MODE_CHOICES = click.Choice(('normal', 'auto', 'single'))
COUPLING_CHOICES = click.Choice(('dc', 'ac', 'low_freq_rej', 'high_freq_rej'))

@trigger.command()
@click.pass_context
@click.option('--mcu', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mode', default='normal', type=MODE_CHOICES, help='Trigger mode', show_default=True)
@click.option('--coupling', default='dc', type=COUPLING_CHOICES, help='Coupling mode', show_default=True)
@click.option('--source', required=False, help='Trigger source', metavar='NET')
@click.option('--slope', type=click.Choice(('rising', 'falling', 'both')), help='Trigger slope')
@click.option('--level', type=click.FLOAT, help='Trigger level')
def edge(ctx, mcu, box, mode, coupling, source, slope, level):
    """
    Set edge trigger
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_trigger_backend(ctx, box_ip, "trigger_edge", netname=netname, mode=mode, coupling=coupling, source=source, slope=slope, level=level, mcu=mcu)


@trigger.command()
@click.pass_context
@click.option('--mcu', required=False)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mode', default='normal', type=MODE_CHOICES, help='Trigger mode', show_default=True)
@click.option('--coupling', default='dc', type=COUPLING_CHOICES, help='Coupling mode', show_default=True)
@click.option('--source', required=False, help='Trigger source', metavar='NET')
@click.option('--level', type=click.FLOAT, help='Trigger level')
@click.option('--trigger-on', type=click.Choice(('gt', 'lt', 'gtlt')), help='Trigger on')
@click.option('--upper', type=click.FLOAT, help='upper width')
@click.option('--lower', type=click.FLOAT, help='lower width')
def pulse(ctx, mcu, box, mode, coupling, source, level, trigger_on, upper, lower):
    """
    Set pulse trigger
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_trigger_backend(ctx, box_ip, "trigger_pulse", netname=netname, mode=mode, coupling=coupling, source=source, level=level, trigger_on=trigger_on, upper=upper, lower=lower, mcu=mcu)

@trigger.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
@click.option('--mode', default='normal', type=MODE_CHOICES, help='Trigger mode, e.g. Normal, Automatic, or Single Shot', show_default=True)
@click.option('--coupling', default='dc', type=COUPLING_CHOICES, help='Coupling mode', show_default=True)
@click.option('--source-scl', required=False, help='Trigger source', metavar='NET')
@click.option('--source-sda', required=False, help='Trigger source', metavar='NET')
@click.option('--level-scl', type=click.FLOAT, help='Trigger scl level')
@click.option('--level-sda', type=click.FLOAT, help='Trigger sda level')
@click.option('--trigger-on', type=click.Choice(('start', 'restart', 'stop', 'nack', 'address', 'data', 'addr_data')), help='Trigger on')
@click.option('--address', type=click.INT, help='Address value to trigger on in ADDRESS mode')
@click.option('--addr-width', type=click.Choice(('7', '8', '9', '10')), help='Address width in bits')
@click.option('--data', type=click.INT, help='Data value to trigger on in DATA mode')
@click.option('--data-width', type=click.Choice(('1', '2', '3', '4', '5')), help='Data width in bytes')
@click.option('--direction', type=click.Choice(('write', 'read', 'rw')), help='Direction to trigger on')
def i2c(ctx, box, mcu, mode, coupling, source_scl, level_scl, source_sda, level_sda, trigger_on, address, addr_width, data, data_width, direction):
    """
    Set I2C trigger
    """
    if addr_width is not None:
        addr_width = int(addr_width)
    if data_width is not None:
        data_width = int(data_width)

    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_trigger_backend(ctx, box_ip, "trigger_i2c", netname=netname, mode=mode, coupling=coupling, source_scl=source_scl, source_sda=source_sda, level_scl=level_scl, level_sda=level_sda, trigger_on=trigger_on, address=address, addr_width=addr_width, data=data, data_width=data_width, direction=direction, mcu=mcu)

@trigger.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
@click.option('--mode', default='normal', type=MODE_CHOICES, help='Trigger mode, e.g. Normal, Automatic, or Single Shot', show_default=True)
@click.option('--coupling', default='dc', type=COUPLING_CHOICES, help='Coupling mode', show_default=True)
@click.option('--source', required=False, help='Trigger source', metavar='NET')
@click.option('--level', type=click.FLOAT, help='Trigger level')
@click.option('--trigger-on', type=click.Choice(('start', 'error', 'cerror', 'data')), help='Trigger on')
@click.option('--parity', type=click.Choice(('even', 'odd', 'none')), help='Data trigger parity')
@click.option('--stop-bits', type=click.Choice(('1', '1.5', '2')), help='Data trigger stop bits')
@click.option('--baud', type=click.INT, help='Data trigger baud')
@click.option('--data-width', type=click.INT, help='Data trigger data width in bits')
@click.option('--data', type=click.INT, help='Data trigger data')
def uart(ctx, box, mcu, mode, coupling, source, level, trigger_on, parity, stop_bits, baud, data_width, data):
    """
    Set UART trigger
    """
    # Validate baud rate if provided
    if baud is not None:
        if baud < 110 or baud > 20000000:
            click.secho(f"Error: Baud rate must be between 110 and 20000000, got {baud}", fg="red", err=True)
            ctx.exit(1)

    # Validate data width if provided (typically 5-9 bits for UART)
    if data_width is not None:
        if data_width < 5 or data_width > 9:
            click.secho(f"Error: Data width must be between 5 and 9 bits, got {data_width}", fg="red", err=True)
            ctx.exit(1)

    if stop_bits is not None:
        stop_bits = float(stop_bits)

    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_trigger_backend(ctx, box_ip, "trigger_uart", netname=netname, mode=mode, coupling=coupling, source=source, level=level, trigger_on=trigger_on, parity=parity, stop_bits=stop_bits, baud=baud, data_width=data_width, data=data, mcu=mcu)

@trigger.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
@click.option('--mode', default='normal', type=MODE_CHOICES, help='Trigger mode, e.g. Normal, Automatic, or Single Shot', show_default=True)
@click.option('--coupling', default='dc', type=COUPLING_CHOICES, help='Coupling mode', show_default=True)
@click.option('--source-mosi-miso', required=False, help='Trigger master/slave data source', metavar='NET')
@click.option('--source-sck', required=False, help='Trigger clock source', metavar='NET')
@click.option('--source-cs', required=False, help='Trigger chip select source', metavar='NET')
@click.option('--level-mosi-miso', type=click.FLOAT, help='Trigger mosi/miso level')
@click.option('--level-sck', type=click.FLOAT, help='Trigger sck level')
@click.option('--level-cs', type=click.FLOAT, help='Trigger cs level')
@click.option('--data', type=click.INT, help='Trigger data value')
@click.option('--data-width', type=click.INT, help='Data width in bits')
@click.option('--clk-slope', type=click.Choice(('positive', 'negative')), help='Slope of clock edge to sample data')
@click.option('--trigger-on', type=click.Choice(('timeout', 'cs')), help='Trigger on')
@click.option('--cs-idle', type=click.Choice(('high', 'low')), help='CS Idle type')
@click.option('--timeout', type=click.FLOAT, help='Timeout length')
def spi(ctx, box, mcu, mode, coupling, source_mosi_miso, source_sck, source_cs, level_mosi_miso, level_sck, level_cs, data, data_width, clk_slope, trigger_on, cs_idle, timeout):
    """
    Set SPI trigger
    """
    # Validate timeout if provided (must be positive)
    if timeout is not None:
        if timeout <= 0:
            click.secho(f"Error: Timeout must be positive, got {timeout}", fg="red", err=True)
            ctx.exit(1)

    # Validate data width if provided (typically 4-32 bits for SPI)
    if data_width is not None:
        if data_width < 4 or data_width > 32:
            click.secho(f"Error: Data width must be between 4 and 32 bits, got {data_width}", fg="red", err=True)
            ctx.exit(1)

    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_trigger_backend(ctx, box_ip, "trigger_spi", netname=netname, mode=mode, coupling=coupling, source_mosi_miso=source_mosi_miso, source_sck=source_sck, source_cs=source_cs, level_mosi_miso=level_mosi_miso, level_sck=level_sck, level_cs=level_cs, data=data, data_width=data_width, clk_slope=clk_slope, trigger_on=trigger_on, cs_idle=cs_idle, timeout=timeout, mcu=mcu)

@logic.group()
def cursor():
    """
        Move scope cursor on a given net
    """
    pass

@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
@click.option('--x', required=False, type=click.FLOAT, help='cursor a x coordinate')
@click.option('--y', required=False, type=click.FLOAT, help='cursor a y coordinate')
def set_a(ctx, box, mcu, x, y):
    """
        Set cursor a's x position
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_cursor_backend(ctx, box_ip, "set_a", netname=netname, x=x, y=y, mcu=mcu)

@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
@click.option('--x', required=False, type=click.FLOAT, help='cursor b x coordinate')
@click.option('--y', required=False, type=click.FLOAT, help='cursor b y coordinate')
def set_b(ctx, box, mcu, x, y):
    """
        Set cursor b's x position
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_cursor_backend(ctx, box_ip, "set_b", netname=netname, x=x, y=y, mcu=mcu)

@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
@click.option('--del-x', required=False, type=click.FLOAT, help='shift a\'s x coordinate')
@click.option('--del-y', required=False, type=click.FLOAT, help='shift a\'s y coordinate')
def move_a(ctx, box, mcu, del_x, del_y):
    """
        Shift cursor a's  position
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_cursor_backend(ctx, box_ip, "move_a", netname=netname, del_x=del_x, del_y=del_y, mcu=mcu)

@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
@click.option('--del-x', required=False, type=click.FLOAT, help='shift b\'s x coordinate')
@click.option('--del-y', required=False, type=click.FLOAT, help='shift b\'s y coordinate')
def move_b(ctx, box, mcu, del_x, del_y):
    """
        Shift cursor b's position
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_cursor_backend(ctx, box_ip, "move_b", netname=netname, del_x=del_x, del_y=del_y, mcu=mcu)

@cursor.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--mcu', required=False)
def hide(ctx, box, mcu):
    """
        Hide cursor
    """
    box_ip = _resolve_box(ctx, box)
    netname = _require_netname(ctx)

    _validate_logic_net(ctx, box_ip, netname)

    _run_cursor_backend(ctx, box_ip, "hide_cursor", netname=netname, mcu=mcu)

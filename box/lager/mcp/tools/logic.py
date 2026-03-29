# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for logic analyzer control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_logic_list_nets(box: str) -> str:
    """List available logic analyzer nets on a box.

    Shows all configured logic nets with their instrument type and
    channel information.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("logic", "--box", box)


# --- Channel control ---


@mcp.tool()
def lager_logic_enable(box: str, net: str) -> str:
    """Enable a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
    """
    return run_lager("logic", net, "enable", "--box", box)


@mcp.tool()
def lager_logic_disable(box: str, net: str) -> str:
    """Disable a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
    """
    return run_lager("logic", net, "disable", "--box", box)


# --- Capture control ---


@mcp.tool()
def lager_logic_start(box: str, net: str) -> str:
    """Start logic analyzer waveform capture.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
    """
    return run_lager("logic", net, "start", "--box", box)


@mcp.tool()
def lager_logic_start_single(box: str, net: str) -> str:
    """Start a single logic analyzer waveform capture.

    Captures one trigger event then stops automatically.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
    """
    return run_lager("logic", net, "start-single", "--box", box)


@mcp.tool()
def lager_logic_stop(box: str, net: str) -> str:
    """Stop logic analyzer waveform capture.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
    """
    return run_lager("logic", net, "stop", "--box", box)


# --- Measurements ---


def _measure(box, net, measurement, display=False, cursor=False):
    """Run a logic analyzer measurement command."""
    args = ["logic", net, "measure", measurement, "--box", box]
    if display:
        args.append("--display")
    if cursor:
        args.append("--cursor")
    return run_lager(*args)


@mcp.tool()
def lager_logic_measure_period(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure waveform period on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        display: Show measurement on the screen (default: false)
        cursor: Enable measurement cursor (default: false)
    """
    return _measure(box, net, "period", display, cursor)


@mcp.tool()
def lager_logic_measure_freq(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure waveform frequency on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        display: Show measurement on the screen (default: false)
        cursor: Enable measurement cursor (default: false)
    """
    return _measure(box, net, "freq", display, cursor)


@mcp.tool()
def lager_logic_measure_dc_pos(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure positive duty cycle on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        display: Show measurement on the screen (default: false)
        cursor: Enable measurement cursor (default: false)
    """
    return _measure(box, net, "dc-pos", display, cursor)


@mcp.tool()
def lager_logic_measure_dc_neg(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure negative duty cycle on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        display: Show measurement on the screen (default: false)
        cursor: Enable measurement cursor (default: false)
    """
    return _measure(box, net, "dc-neg", display, cursor)


@mcp.tool()
def lager_logic_measure_pw_pos(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure positive pulse width on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        display: Show measurement on the screen (default: false)
        cursor: Enable measurement cursor (default: false)
    """
    return _measure(box, net, "pw-pos", display, cursor)


@mcp.tool()
def lager_logic_measure_pw_neg(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure negative pulse width on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        display: Show measurement on the screen (default: false)
        cursor: Enable measurement cursor (default: false)
    """
    return _measure(box, net, "pw-neg", display, cursor)


# --- Triggers ---


@mcp.tool()
def lager_logic_trigger_edge(
    box: str, net: str,
    source: str = None, slope: str = None, level: float = None,
    mode: str = "normal", coupling: str = "dc",
) -> str:
    """Configure edge trigger on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        source: Trigger source channel
        slope: Trigger slope ('rising', 'falling', or 'both')
        level: Trigger level in volts
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', 'low_freq_rej', or 'high_freq_rej'; default: 'dc')
    """
    args = ["logic", net, "trigger", "edge", "--box", box,
            "--mode", mode, "--coupling", coupling]
    if source is not None:
        args.extend(["--source", source])
    if slope is not None:
        args.extend(["--slope", slope])
    if level is not None:
        args.extend(["--level", str(level)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_trigger_pulse(
    box: str, net: str,
    trigger_on: str = None,
    mode: str = "normal", coupling: str = "dc",
    source: str = None, level: float = None,
    upper: float = None, lower: float = None,
) -> str:
    """Configure pulse width trigger on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        trigger_on: Trigger condition ('gt', 'lt', or 'gtlt')
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', 'low_freq_rej', or 'high_freq_rej'; default: 'dc')
        source: Trigger source channel
        level: Trigger level in volts
        upper: Upper pulse width limit in seconds
        lower: Lower pulse width limit in seconds
    """
    args = ["logic", net, "trigger", "pulse", "--box", box,
            "--mode", mode, "--coupling", coupling]
    if trigger_on is not None:
        args.extend(["--trigger-on", trigger_on])
    if source is not None:
        args.extend(["--source", source])
    if level is not None:
        args.extend(["--level", str(level)])
    if upper is not None:
        args.extend(["--upper", str(upper)])
    if lower is not None:
        args.extend(["--lower", str(lower)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_trigger_i2c(
    box: str, net: str,
    trigger_on: str = None, addr_width: str = None,
    data_width: str = None, direction: str = None,
    mode: str = "normal", coupling: str = "dc",
    source_scl: str = None, source_sda: str = None,
    level_scl: float = None, level_sda: float = None,
    address: int = None, data: int = None,
) -> str:
    """Configure I2C protocol trigger on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        trigger_on: Trigger condition ('start', 'restart', 'stop', 'nack', 'address', 'data', or 'addr_data')
        addr_width: Address width in bits ('7', '8', '9', or '10')
        data_width: Data width in bytes ('1', '2', '3', '4', or '5')
        direction: Transfer direction ('write', 'read', or 'rw')
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', 'low_freq_rej', or 'high_freq_rej'; default: 'dc')
        source_scl: SCL source channel
        source_sda: SDA source channel
        level_scl: SCL trigger level in volts
        level_sda: SDA trigger level in volts
        address: I2C address to trigger on
        data: Data value to trigger on
    """
    args = ["logic", net, "trigger", "i2c", "--box", box,
            "--mode", mode, "--coupling", coupling]
    if trigger_on is not None:
        args.extend(["--trigger-on", trigger_on])
    if addr_width is not None:
        args.extend(["--addr-width", addr_width])
    if data_width is not None:
        args.extend(["--data-width", data_width])
    if direction is not None:
        args.extend(["--direction", direction])
    if source_scl is not None:
        args.extend(["--source-scl", source_scl])
    if source_sda is not None:
        args.extend(["--source-sda", source_sda])
    if level_scl is not None:
        args.extend(["--level-scl", str(level_scl)])
    if level_sda is not None:
        args.extend(["--level-sda", str(level_sda)])
    if address is not None:
        args.extend(["--address", str(address)])
    if data is not None:
        args.extend(["--data", str(data)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_trigger_uart(
    box: str, net: str,
    trigger_on: str = None, parity: str = None,
    stop_bits: str = None, baud: int = None,
    data_width: int = None, data: int = None,
    mode: str = "normal", coupling: str = "dc",
    source: str = None, level: float = None,
) -> str:
    """Configure UART protocol trigger on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        trigger_on: Trigger condition ('start', 'error', 'cerror', or 'data')
        parity: Parity setting ('even', 'odd', or 'none')
        stop_bits: Stop bits ('1', '1.5', or '2')
        baud: Baud rate
        data_width: Data width in bits
        data: Data value to trigger on
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', 'low_freq_rej', or 'high_freq_rej'; default: 'dc')
        source: Trigger source channel
        level: Trigger level in volts
    """
    args = ["logic", net, "trigger", "uart", "--box", box,
            "--mode", mode, "--coupling", coupling]
    if trigger_on is not None:
        args.extend(["--trigger-on", trigger_on])
    if parity is not None:
        args.extend(["--parity", parity])
    if stop_bits is not None:
        args.extend(["--stop-bits", stop_bits])
    if baud is not None:
        args.extend(["--baud", str(baud)])
    if data_width is not None:
        args.extend(["--data-width", str(data_width)])
    if data is not None:
        args.extend(["--data", str(data)])
    if source is not None:
        args.extend(["--source", source])
    if level is not None:
        args.extend(["--level", str(level)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_trigger_spi(
    box: str, net: str,
    trigger_on: str = None, data_width: int = None,
    clk_slope: str = None, cs_idle: str = None,
    data: int = None, timeout: float = None,
    mode: str = "normal", coupling: str = "dc",
    source_mosi_miso: str = None, source_sck: str = None,
    source_cs: str = None,
    level_mosi_miso: float = None, level_sck: float = None,
    level_cs: float = None,
) -> str:
    """Configure SPI protocol trigger on a logic analyzer channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        trigger_on: Trigger condition ('timeout' or 'cs')
        data_width: Data width in bits
        clk_slope: Clock edge ('positive' or 'negative')
        cs_idle: CS idle state ('high' or 'low')
        data: Data value to trigger on
        timeout: Timeout value in seconds
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', 'low_freq_rej', or 'high_freq_rej'; default: 'dc')
        source_mosi_miso: MOSI/MISO source channel
        source_sck: SCK source channel
        source_cs: CS source channel
        level_mosi_miso: MOSI/MISO trigger level in volts
        level_sck: SCK trigger level in volts
        level_cs: CS trigger level in volts
    """
    args = ["logic", net, "trigger", "spi", "--box", box,
            "--mode", mode, "--coupling", coupling]
    if trigger_on is not None:
        args.extend(["--trigger-on", trigger_on])
    if data_width is not None:
        args.extend(["--data-width", str(data_width)])
    if clk_slope is not None:
        args.extend(["--clk-slope", clk_slope])
    if cs_idle is not None:
        args.extend(["--cs-idle", cs_idle])
    if data is not None:
        args.extend(["--data", str(data)])
    if timeout is not None:
        args.extend(["--timeout", str(timeout)])
    if source_mosi_miso is not None:
        args.extend(["--source-mosi-miso", source_mosi_miso])
    if source_sck is not None:
        args.extend(["--source-sck", source_sck])
    if source_cs is not None:
        args.extend(["--source-cs", source_cs])
    if level_mosi_miso is not None:
        args.extend(["--level-mosi-miso", str(level_mosi_miso)])
    if level_sck is not None:
        args.extend(["--level-sck", str(level_sck)])
    if level_cs is not None:
        args.extend(["--level-cs", str(level_cs)])
    return run_lager(*args)


# --- Cursors ---


@mcp.tool()
def lager_logic_cursor_set_a(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Set logic analyzer cursor A position.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        x: Cursor A x coordinate
        y: Cursor A y coordinate
    """
    args = ["logic", net, "cursor", "set-a", "--box", box]
    if x is not None:
        args.extend(["--x", str(x)])
    if y is not None:
        args.extend(["--y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_cursor_set_b(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Set logic analyzer cursor B position.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        x: Cursor B x coordinate
        y: Cursor B y coordinate
    """
    args = ["logic", net, "cursor", "set-b", "--box", box]
    if x is not None:
        args.extend(["--x", str(x)])
    if y is not None:
        args.extend(["--y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_cursor_move_a(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Move logic analyzer cursor A by a relative offset.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        x: Relative x movement (delta)
        y: Relative y movement (delta)
    """
    args = ["logic", net, "cursor", "move-a", "--box", box]
    if x is not None:
        args.extend(["--del-x", str(x)])
    if y is not None:
        args.extend(["--del-y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_cursor_move_b(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Move logic analyzer cursor B by a relative offset.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
        x: Relative x movement (delta)
        y: Relative y movement (delta)
    """
    args = ["logic", net, "cursor", "move-b", "--box", box]
    if x is not None:
        args.extend(["--del-x", str(x)])
    if y is not None:
        args.extend(["--del-y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_logic_cursor_hide(box: str, net: str) -> str:
    """Hide logic analyzer cursor display.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Logic net name (e.g., 'logic1')
    """
    return run_lager("logic", net, "cursor", "hide", "--box", box)

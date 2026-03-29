# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for oscilloscope control."""

from ..server import mcp, run_lager


@mcp.tool()
def lager_scope_list_nets(box: str) -> str:
    """List available oscilloscope nets on a box.

    Shows all configured scope nets with their instrument type and
    channel information.

    Args:
        box: Box name (e.g., 'DEMO')
    """
    return run_lager("scope", "--box", box)


def _measure(box, net, measurement, display=False, cursor=False):
    """Run a scope measurement command with optional display/cursor flags."""
    args = ["scope", net, "measure", measurement, "--box", box]
    if display:
        args.append("--display")
    if cursor:
        args.append("--cursor")
    return run_lager(*args)


@mcp.tool()
def lager_scope_autoscale(box: str, net: str) -> str:
    """Auto-adjust oscilloscope scale to fit the current signal.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "autoscale", "--box", box)


@mcp.tool()
def lager_scope_measure_freq(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure waveform frequency on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "freq", display, cursor)


@mcp.tool()
def lager_scope_measure_vpp(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure peak-to-peak voltage on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "vpp", display, cursor)


@mcp.tool()
def lager_scope_measure_vrms(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure RMS voltage on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "vrms", display, cursor)


# --- Channel control ---


@mcp.tool()
def lager_scope_enable(box: str, net: str) -> str:
    """Enable an oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "enable", "--box", box)


@mcp.tool()
def lager_scope_disable(box: str, net: str) -> str:
    """Disable an oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "disable", "--box", box)


# --- Capture control ---


@mcp.tool()
def lager_scope_start(box: str, net: str, single: bool = False) -> str:
    """Start oscilloscope acquisition.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        single: If true, capture a single trigger event then stop (default: false)
    """
    args = ["scope", net, "start", "--box", box]
    if single:
        args.insert(3, "--single")
    return run_lager(*args)


@mcp.tool()
def lager_scope_stop(box: str, net: str) -> str:
    """Stop oscilloscope acquisition.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "stop", "--box", box)


@mcp.tool()
def lager_scope_force(box: str, net: str) -> str:
    """Force an immediate oscilloscope trigger.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "force", "--box", box)


# --- Channel settings ---


@mcp.tool()
def lager_scope_scale(box: str, net: str, volts_per_div: float) -> str:
    """Set oscilloscope vertical scale (volts per division).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        volts_per_div: Vertical scale in volts per division
    """
    return run_lager(
        "scope", net, "scale", str(volts_per_div), "--box", box,
    )


@mcp.tool()
def lager_scope_coupling(box: str, net: str, mode: str) -> str:
    """Set oscilloscope input coupling mode.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        mode: Coupling mode ('dc', 'ac', or 'gnd')
    """
    return run_lager("scope", net, "coupling", mode, "--box", box)


@mcp.tool()
def lager_scope_probe(box: str, net: str, attenuation: str) -> str:
    """Set oscilloscope probe attenuation ratio.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        attenuation: Probe ratio ('1x', '10x', '100x', or '1000x')
    """
    return run_lager("scope", net, "probe", attenuation, "--box", box)


@mcp.tool()
def lager_scope_timebase(box: str, net: str, seconds_per_div: float) -> str:
    """Set oscilloscope horizontal timebase (seconds per division).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        seconds_per_div: Time per division in seconds
    """
    return run_lager(
        "scope", net, "timebase", str(seconds_per_div), "--box", box,
    )


# --- Additional measurements ---


@mcp.tool()
def lager_scope_measure_vmax(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure maximum voltage on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "vmax", display, cursor)


@mcp.tool()
def lager_scope_measure_vmin(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure minimum voltage on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "vmin", display, cursor)


@mcp.tool()
def lager_scope_measure_vavg(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure average voltage on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "vavg", display, cursor)


@mcp.tool()
def lager_scope_measure_period(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure waveform period on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "period", display, cursor)


@mcp.tool()
def lager_scope_measure_pw_pos(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure positive pulse width on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "pulse-width-pos", display, cursor)


@mcp.tool()
def lager_scope_measure_pw_neg(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure negative pulse width on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "pulse-width-neg", display, cursor)


@mcp.tool()
def lager_scope_measure_duty_pos(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure positive duty cycle on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "duty-cycle-pos", display, cursor)


@mcp.tool()
def lager_scope_measure_duty_neg(
    box: str, net: str, display: bool = False, cursor: bool = False,
) -> str:
    """Measure negative duty cycle on the oscilloscope channel.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        display: Show measurement on the scope screen (default: false)
        cursor: Enable measurement cursor on the scope (default: false)
    """
    return _measure(box, net, "duty-cycle-neg", display, cursor)


# --- Trigger ---


@mcp.tool()
def lager_scope_trigger_edge(
    box: str, net: str,
    source: str = None, slope: str = None, level: float = None,
) -> str:
    """Configure oscilloscope edge trigger.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        source: Trigger source channel (omit for current setting)
        slope: Trigger slope ('rising' or 'falling')
        level: Trigger level in volts
    """
    args = ["scope", net, "trigger", "edge", "--box", box]
    if source is not None:
        args.extend(["--source", source])
    if slope is not None:
        args.extend(["--slope", slope])
    if level is not None:
        args.extend(["--level", str(level)])
    return run_lager(*args)


@mcp.tool()
def lager_scope_trigger_uart(
    box: str, net: str,
    baud: int = 9600, parity: str = "none", stop_bits: str = "1",
    data_width: int = 8, trigger_on: str = "start",
    mode: str = "normal", coupling: str = "dc",
    source: str = None, level: float = None, data: str = None,
) -> str:
    """Configure UART protocol trigger on oscilloscope (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        baud: Baud rate (default: 9600)
        parity: Parity setting ('none', 'even', or 'odd'; default: 'none')
        stop_bits: Stop bits ('1', '1.5', or '2'; default: '1')
        data_width: Data width in bits (default: 8)
        trigger_on: Trigger condition ('start', 'stop', 'data', or 'error'; default: 'start')
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', or 'gnd'; default: 'dc')
        source: Trigger source channel (omit for current setting)
        level: Trigger level in volts
        data: Data pattern to match (hex string)
    """
    args = ["scope", net, "trigger", "uart", "--box", box,
            "--baud", str(baud), "--parity", parity,
            "--stop-bits", stop_bits, "--data-width", str(data_width),
            "--trigger-on", trigger_on, "--mode", mode, "--coupling", coupling]
    if source is not None:
        args.extend(["--source", source])
    if level is not None:
        args.extend(["--level", str(level)])
    if data is not None:
        args.extend(["--data", data])
    return run_lager(*args)


@mcp.tool()
def lager_scope_trigger_i2c(
    box: str, net: str,
    trigger_on: str = "start", addr_width: str = "7",
    data_width: int = 8, direction: str = "read_write",
    mode: str = "normal", coupling: str = "dc",
    source_scl: str = None, source_sda: str = None,
    level_scl: float = None, level_sda: float = None,
    address: str = None, data: str = None,
) -> str:
    """Configure I2C protocol trigger on oscilloscope (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        trigger_on: Trigger condition ('start', 'restart', 'stop', 'ack_miss', 'address', 'data', or 'addr_data'; default: 'start')
        addr_width: Address width in bits ('7', '8', or '10'; default: '7')
        data_width: Data width in bits (default: 8)
        direction: Transfer direction ('read', 'write', or 'read_write'; default: 'read_write')
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', or 'gnd'; default: 'dc')
        source_scl: SCL source channel
        source_sda: SDA source channel
        level_scl: SCL trigger level in volts
        level_sda: SDA trigger level in volts
        address: I2C address to match (hex string)
        data: Data pattern to match (hex string)
    """
    args = ["scope", net, "trigger", "i2c", "--box", box,
            "--trigger-on", trigger_on, "--addr-width", addr_width,
            "--data-width", str(data_width), "--direction", direction,
            "--mode", mode, "--coupling", coupling]
    if source_scl is not None:
        args.extend(["--source-scl", source_scl])
    if source_sda is not None:
        args.extend(["--source-sda", source_sda])
    if level_scl is not None:
        args.extend(["--level-scl", str(level_scl)])
    if level_sda is not None:
        args.extend(["--level-sda", str(level_sda)])
    if address is not None:
        args.extend(["--address", address])
    if data is not None:
        args.extend(["--data", data])
    return run_lager(*args)


@mcp.tool()
def lager_scope_trigger_spi(
    box: str, net: str,
    trigger_on: str = "cs", data_width: int = 8,
    clk_slope: str = "rising", cs_idle: str = "high",
    mode: str = "normal", coupling: str = "dc",
    source_mosi_miso: str = None, source_sck: str = None,
    source_cs: str = None, level_mosi_miso: float = None,
    level_sck: float = None, level_cs: float = None,
    data: str = None, timeout: float = None,
) -> str:
    """Configure SPI protocol trigger on oscilloscope (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        trigger_on: Trigger condition ('timeout' or 'cs'; default: 'cs')
        data_width: Data width in bits (default: 8)
        clk_slope: Clock edge ('rising' or 'falling'; default: 'rising')
        cs_idle: CS idle state ('high' or 'low'; default: 'high')
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', or 'gnd'; default: 'dc')
        source_mosi_miso: MOSI/MISO source channel
        source_sck: SCK source channel
        source_cs: CS source channel
        level_mosi_miso: MOSI/MISO trigger level in volts
        level_sck: SCK trigger level in volts
        level_cs: CS trigger level in volts
        data: Data pattern to match (hex string)
        timeout: Timeout value in seconds
    """
    args = ["scope", net, "trigger", "spi", "--box", box,
            "--trigger-on", trigger_on, "--data-width", str(data_width),
            "--clk-slope", clk_slope, "--cs-idle", cs_idle,
            "--mode", mode, "--coupling", coupling]
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
    if data is not None:
        args.extend(["--data", data])
    if timeout is not None:
        args.extend(["--timeout", str(timeout)])
    return run_lager(*args)


@mcp.tool()
def lager_scope_trigger_pulse(
    box: str, net: str,
    trigger_on: str = "positive",
    mode: str = "normal", coupling: str = "dc",
    source: str = None, level: float = None,
    upper: float = None, lower: float = None,
) -> str:
    """Configure pulse width trigger on oscilloscope (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        trigger_on: Trigger condition ('positive', 'negative', 'positive_greater', 'negative_greater', 'positive_less', or 'negative_less'; default: 'positive')
        mode: Trigger mode ('normal', 'auto', or 'single'; default: 'normal')
        coupling: Coupling mode ('dc', 'ac', or 'gnd'; default: 'dc')
        source: Trigger source channel
        level: Trigger level in volts
        upper: Upper pulse width limit in seconds
        lower: Lower pulse width limit in seconds
    """
    args = ["scope", net, "trigger", "pulse", "--box", box,
            "--trigger-on", trigger_on, "--mode", mode, "--coupling", coupling]
    if source is not None:
        args.extend(["--source", source])
    if level is not None:
        args.extend(["--level", str(level)])
    if upper is not None:
        args.extend(["--upper", str(upper)])
    if lower is not None:
        args.extend(["--lower", str(lower)])
    return run_lager(*args)


# --- Cursors ---


@mcp.tool()
def lager_scope_cursor_set_a(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Set oscilloscope cursor A position (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        x: Cursor A x coordinate
        y: Cursor A y coordinate
    """
    args = ["scope", net, "cursor", "set-a", "--box", box]
    if x is not None:
        args.extend(["--x", str(x)])
    if y is not None:
        args.extend(["--y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_scope_cursor_set_b(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Set oscilloscope cursor B position (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        x: Cursor B x coordinate
        y: Cursor B y coordinate
    """
    args = ["scope", net, "cursor", "set-b", "--box", box]
    if x is not None:
        args.extend(["--x", str(x)])
    if y is not None:
        args.extend(["--y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_scope_cursor_move_a(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Move oscilloscope cursor A by a relative offset (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        x: Relative x movement (delta)
        y: Relative y movement (delta)
    """
    args = ["scope", net, "cursor", "move-a", "--box", box]
    if x is not None:
        args.extend(["--x", str(x)])
    if y is not None:
        args.extend(["--y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_scope_cursor_move_b(
    box: str, net: str, x: float = None, y: float = None,
) -> str:
    """Move oscilloscope cursor B by a relative offset (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        x: Relative x movement (delta)
        y: Relative y movement (delta)
    """
    args = ["scope", net, "cursor", "move-b", "--box", box]
    if x is not None:
        args.extend(["--x", str(x)])
    if y is not None:
        args.extend(["--y", str(y)])
    return run_lager(*args)


@mcp.tool()
def lager_scope_cursor_hide(box: str, net: str) -> str:
    """Hide oscilloscope cursor display (Rigol only).

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "cursor", "hide", "--box", box)


# --- Streaming (PicoScope) ---


@mcp.tool()
def lager_scope_stream_start(
    box: str, net: str,
    channel: str = "A", volts_per_div: float = 1.0,
    time_per_div: float = 0.001, trigger_level: float = 0.0,
    trigger_slope: str = "rising", capture_mode: str = "auto",
    coupling: str = "dc",
) -> str:
    """Start PicoScope streaming with web visualization.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        channel: Channel to enable ('A', 'B', '1', or '2'; default: 'A')
        volts_per_div: Vertical scale in volts per division (default: 1.0)
        time_per_div: Horizontal scale in seconds per division (default: 0.001)
        trigger_level: Trigger threshold voltage (default: 0.0)
        trigger_slope: Trigger edge direction ('rising', 'falling', or 'either'; default: 'rising')
        capture_mode: Triggering mode ('auto', 'normal', or 'single'; default: 'auto')
        coupling: Input coupling type ('dc' or 'ac'; default: 'dc')
    """
    return run_lager(
        "scope", net, "stream", "start", "--box", box,
        "--channel", channel,
        "--volts-per-div", str(volts_per_div),
        "--time-per-div", str(time_per_div),
        "--trigger-level", str(trigger_level),
        "--trigger-slope", trigger_slope,
        "--capture-mode", capture_mode,
        "--coupling", coupling,
    )


@mcp.tool()
def lager_scope_stream_stop(box: str, net: str) -> str:
    """Stop PicoScope streaming acquisition.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "stream", "stop", "--box", box)


@mcp.tool()
def lager_scope_stream_status(box: str, net: str) -> str:
    """Check PicoScope streaming daemon status.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
    """
    return run_lager("scope", net, "stream", "status", "--box", box)


@mcp.tool()
def lager_scope_stream_capture(
    box: str, net: str,
    output: str = "scope_data.csv", duration: float = 1.0,
    samples: int = None,
) -> str:
    """Capture PicoScope waveform data to a CSV file on the box.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        output: CSV output file path (default: 'scope_data.csv')
        duration: Capture duration in seconds (default: 1.0)
        samples: Maximum number of samples to capture (optional)
    """
    args = ["scope", net, "stream", "capture", "--box", box,
            "--output", output, "--duration", str(duration)]
    if samples is not None:
        args.extend(["--samples", str(samples)])
    return run_lager(*args)


@mcp.tool()
def lager_scope_stream_config(
    box: str, net: str,
    channel: str = None, volts_per_div: float = None,
    time_per_div: float = None, trigger_level: float = None,
    trigger_source: str = None, trigger_slope: str = None,
    capture_mode: str = None, coupling: str = None,
    enable: bool = None,
) -> str:
    """Configure PicoScope streaming settings without starting/stopping.

    Args:
        box: Box name (e.g., 'DEMO')
        net: Scope net name (e.g., 'scope1')
        channel: Channel to configure ('A', 'B', '1', or '2')
        volts_per_div: Vertical scale in volts per division
        time_per_div: Horizontal scale in seconds per division
        trigger_level: Trigger threshold voltage
        trigger_source: Trigger source channel
        trigger_slope: Trigger edge direction ('rising', 'falling', or 'either')
        capture_mode: Triggering mode ('auto', 'normal', or 'single')
        coupling: Input coupling type ('dc' or 'ac')
        enable: Enable (true) or disable (false) the channel
    """
    args = ["scope", net, "stream", "config", "--box", box]
    if channel is not None:
        args.extend(["--channel", channel])
    if volts_per_div is not None:
        args.extend(["--volts-per-div", str(volts_per_div)])
    if time_per_div is not None:
        args.extend(["--time-per-div", str(time_per_div)])
    if trigger_level is not None:
        args.extend(["--trigger-level", str(trigger_level)])
    if trigger_source is not None:
        args.extend(["--trigger-source", trigger_source])
    if trigger_slope is not None:
        args.extend(["--trigger-slope", trigger_slope])
    if capture_mode is not None:
        args.extend(["--capture-mode", capture_mode])
    if coupling is not None:
        args.extend(["--coupling", coupling])
    if enable is True:
        args.append("--enable")
    elif enable is False:
        args.append("--disable")
    return run_lager(*args)

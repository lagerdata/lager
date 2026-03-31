# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for logic analyzer control via direct on-box Net API.

Supports Rigol MSO5000-class logic analyzer channels.  All methods
forward through `Net.get()` → mapper → hardware service.
"""

import json

from ..server import mcp

_METRIC_MAP = {
    "freq": "frequency",
    "frequency": "frequency",
    "period": "period",
    "dc_pos": "duty_cycle_positive",
    "duty_cycle_pos": "duty_cycle_positive",
    "dc_neg": "duty_cycle_negative",
    "duty_cycle_neg": "duty_cycle_negative",
    "pw_pos": "pulse_width_positive",
    "pulse_width_pos": "pulse_width_positive",
    "pw_neg": "pulse_width_negative",
    "pulse_width_neg": "pulse_width_negative",
}


# ── Channel control ──────────────────────────────────────────────────

@mcp.tool()
def logic_enable(net: str) -> str:
    """Enable a logic analyzer channel.

    Args:
        net: Logic net name (e.g., 'logic1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Logic).enable()
    return json.dumps({"status": "ok", "net": net, "enabled": True})


@mcp.tool()
def logic_disable(net: str) -> str:
    """Disable a logic analyzer channel.

    Args:
        net: Logic net name (e.g., 'logic1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Logic).disable()
    return json.dumps({"status": "ok", "net": net, "enabled": False})


@mcp.tool()
def logic_threshold(net: str, voltage: float) -> str:
    """Set the signal threshold voltage for a logic analyzer channel.

    Args:
        net: Logic net name (e.g., 'logic1')
        voltage: Threshold voltage in volts
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Logic).set_signal_threshold(voltage)
    return json.dumps({"status": "ok", "net": net, "threshold_v": voltage})


# ── Capture control ──────────────────────────────────────────────────

@mcp.tool()
def logic_start(net: str, single: bool = False) -> str:
    """Start logic analyzer waveform capture.

    Args:
        net: Logic net name (e.g., 'logic1')
        single: Capture one trigger event then stop (default: false)
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    if single:
        la.start_single_capture()
    else:
        la.start_capture()
    return json.dumps({"status": "ok", "net": net, "single": single})


@mcp.tool()
def logic_stop(net: str) -> str:
    """Stop logic analyzer waveform capture.

    Args:
        net: Logic net name (e.g., 'logic1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Logic).stop_capture()
    return json.dumps({"status": "ok", "net": net, "action": "stop"})


# ── Measurements ─────────────────────────────────────────────────────

@mcp.tool()
def logic_measure(net: str, metric: str, display: bool = False) -> str:
    """Take a measurement on a logic analyzer channel.

    Supported metrics: freq, period, dc_pos, dc_neg, pw_pos, pw_neg.

    Args:
        net: Logic net name (e.g., 'logic1')
        metric: Measurement type (see list above)
        display: Show measurement on the scope screen (default: false)
    """
    from lager import Net, NetType

    method_name = _METRIC_MAP.get(metric.lower())
    if not method_name:
        return json.dumps({
            "status": "error",
            "error": f"Unknown metric '{metric}'. Supported: {', '.join(sorted(_METRIC_MAP))}",
        })

    la = Net.get(net, type=NetType.Logic)
    value = getattr(la.measurement, method_name)(
        display=display, measurement_cursor=False,
    )
    return json.dumps({"status": "ok", "net": net, "metric": metric, "value": value}, default=str)


# ── Triggers ─────────────────────────────────────────────────────────

def _apply_trigger_mode_coupling(trigger_settings, mode=None, coupling=None):
    """Apply trigger mode and coupling if specified."""
    if mode:
        {"auto": trigger_settings.set_mode_auto,
         "normal": trigger_settings.set_mode_normal,
         "single": trigger_settings.set_mode_single}[mode.lower()]()
    if coupling:
        {"dc": trigger_settings.set_coupling_DC,
         "ac": trigger_settings.set_coupling_AC,
         "low_freq_rej": trigger_settings.set_coupling_low_freq_reject,
         "high_freq_rej": trigger_settings.set_coupling_high_freq_reject,
         }[coupling.lower()]()


@mcp.tool()
def logic_trigger_edge(
    net: str,
    source: str = None,
    slope: str = None,
    level: float = None,
    mode: str = "normal",
    coupling: str = "dc",
) -> str:
    """Configure edge trigger on a logic analyzer channel.

    Args:
        net: Logic net name (e.g., 'logic1')
        source: Trigger source channel
        slope: 'rising', 'falling', or 'both'
        level: Trigger level in volts
        mode: Trigger mode — 'normal', 'auto', or 'single' (default: normal)
        coupling: 'dc', 'ac', 'low_freq_rej', or 'high_freq_rej' (default: dc)
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    ts = la.trigger_settings
    _apply_trigger_mode_coupling(ts, mode, coupling)
    edge = ts.edge
    if source is not None:
        edge.set_source(source)
    if slope is not None:
        {"rising": edge.set_slope_rising,
         "falling": edge.set_slope_falling,
         "both": edge.set_slope_both}[slope.lower()]()
    if level is not None:
        edge.set_level(level)
    return json.dumps({"status": "ok", "net": net, "trigger": "edge",
                        "source": source, "slope": slope, "level": level})


@mcp.tool()
def logic_trigger_pulse(
    net: str,
    source: str = None,
    level: float = None,
    condition: str = "greater",
    width: float = None,
    upper: float = None,
    lower: float = None,
    mode: str = "normal",
    coupling: str = "dc",
) -> str:
    """Configure pulse width trigger on a logic analyzer channel.

    Args:
        net: Logic net name (e.g., 'logic1')
        source: Trigger source channel
        level: Trigger level in volts
        condition: 'greater', 'less', or 'range' (default: greater)
        width: Pulse width threshold in seconds (for greater/less)
        upper: Upper width limit in seconds (for range)
        lower: Lower width limit in seconds (for range)
        mode: Trigger mode (default: normal)
        coupling: Trigger coupling (default: dc)
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    ts = la.trigger_settings
    _apply_trigger_mode_coupling(ts, mode, coupling)
    pulse = ts.pulse
    if source is not None:
        pulse.set_source(source)
    if level is not None:
        pulse.set_level(level)
    cond = condition.lower()
    if cond == "greater" and width is not None:
        pulse.set_trigger_on_pulse_greater_than_width(width)
    elif cond == "less" and width is not None:
        pulse.set_trigger_on_pulse_less_than_width(width)
    elif cond == "range":
        pulse.set_trigger_on_pulse_less_than_greater_than(
            max_pulse_width=upper, min_pulse_width=lower,
        )
    return json.dumps({"status": "ok", "net": net, "trigger": "pulse", "condition": condition})


@mcp.tool()
def logic_trigger_uart(
    net: str,
    source: str = None,
    level: float = None,
    trigger_on: str = "start",
    baud: int = 9600,
    parity: str = "none",
    stop_bits: str = "1",
    data_width: int = 8,
    data: str = None,
    mode: str = "normal",
    coupling: str = "dc",
) -> str:
    """Configure UART protocol trigger on logic analyzer (Rigol).

    Args:
        net: Logic net name (e.g., 'logic1')
        source: Trigger source channel
        level: Trigger level in volts
        trigger_on: 'start', 'frame_error', 'check_error', or 'data' (default: start)
        baud: Baud rate (default: 9600)
        parity: 'none', 'even', or 'odd' (default: none)
        stop_bits: '1', '1.5', or '2' (default: 1)
        data_width: Data bits (default: 8)
        data: Data pattern for trigger_on='data'
        mode: Trigger mode (default: normal)
        coupling: Trigger coupling (default: dc)
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    ts = la.trigger_settings
    _apply_trigger_mode_coupling(ts, mode, coupling)
    uart = ts.uart
    if source is not None:
        uart.set_source(source)
    if level is not None:
        uart.set_level(level)
    uart.set_uart_params(
        parity=parity, stopbits=stop_bits, baud=baud, bits=data_width,
    )
    trigger_fn = {
        "start": uart.set_trigger_on_start,
        "frame_error": uart.set_trigger_on_frame_error,
        "check_error": uart.set_trigger_on_check_error,
    }.get(trigger_on.lower())
    if trigger_fn:
        trigger_fn()
    elif trigger_on.lower() == "data":
        uart.set_trigger_on_data(data=data)
    return json.dumps({"status": "ok", "net": net, "trigger": "uart",
                        "trigger_on": trigger_on, "baud": baud})


@mcp.tool()
def logic_trigger_i2c(
    net: str,
    trigger_on: str = "start",
    source_scl: str = None,
    source_sda: str = None,
    level_scl: float = None,
    level_sda: float = None,
    address: str = None,
    addr_bits: int = 7,
    direction: str = "rw",
    data: str = None,
    data_width: int = 1,
    mode: str = "normal",
    coupling: str = "dc",
) -> str:
    """Configure I2C protocol trigger on logic analyzer (Rigol).

    Args:
        net: Logic net name (e.g., 'logic1')
        trigger_on: 'start', 'restart', 'stop', 'nack', 'address', 'data', or 'addr_data'
        source_scl: SCL source channel
        source_sda: SDA source channel
        level_scl: SCL trigger level in volts
        level_sda: SDA trigger level in volts
        address: I2C address to match
        addr_bits: Address width — 7, 8, or 10 (default: 7)
        direction: 'read', 'write', or 'rw' (default: rw)
        data: Data pattern to match
        data_width: Data width in bytes 1-5 (default: 1)
        mode: Trigger mode (default: normal)
        coupling: Trigger coupling (default: dc)
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    ts = la.trigger_settings
    _apply_trigger_mode_coupling(ts, mode, coupling)
    i2c = ts.i2c
    if source_scl is not None or source_sda is not None:
        i2c.set_source(net_scl=source_scl, net_sda=source_sda)
    if level_scl is not None:
        i2c.set_scl_trigger_level(level_scl)
    if level_sda is not None:
        i2c.set_sda_trigger_level(level_sda)

    cond = trigger_on.lower()
    simple = {
        "start": i2c.set_trigger_on_start,
        "restart": i2c.set_trigger_on_restart,
        "stop": i2c.set_trigger_on_stop,
        "nack": i2c.set_trigger_on_nack,
    }
    if cond in simple:
        simple[cond]()
    elif cond == "address":
        i2c.set_trigger_on_address(bits=addr_bits, direction=direction, address=address)
    elif cond == "data":
        i2c.set_trigger_on_data(width=data_width, data=data)
    elif cond == "addr_data":
        i2c.set_trigger_on_addr_data(
            bits=addr_bits, direction=direction, address=address,
            width=data_width, data=data,
        )
    return json.dumps({"status": "ok", "net": net, "trigger": "i2c", "trigger_on": trigger_on})


@mcp.tool()
def logic_trigger_spi(
    net: str,
    trigger_on: str = "cs",
    source_sck: str = None,
    source_mosi_miso: str = None,
    source_cs: str = None,
    level_sck: float = None,
    level_mosi_miso: float = None,
    level_cs: float = None,
    clk_edge: str = "positive",
    data: str = None,
    data_bits: int = 8,
    timeout: float = None,
    mode: str = "normal",
    coupling: str = "dc",
) -> str:
    """Configure SPI protocol trigger on logic analyzer (Rigol).

    Args:
        net: Logic net name (e.g., 'logic1')
        trigger_on: 'cs' or 'timeout' (default: cs)
        source_sck: SCK source channel
        source_mosi_miso: MOSI/MISO source channel
        source_cs: CS source channel
        level_sck: SCK trigger level in volts
        level_mosi_miso: MOSI/MISO trigger level in volts
        level_cs: CS trigger level in volts
        clk_edge: 'positive' or 'negative' (default: positive)
        data: Data pattern to match
        data_bits: Data width in bits (default: 8)
        timeout: Timeout in seconds (for trigger_on='timeout')
        mode: Trigger mode (default: normal)
        coupling: Trigger coupling (default: dc)
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    ts = la.trigger_settings
    _apply_trigger_mode_coupling(ts, mode, coupling)
    spi = ts.spi
    src_kwargs = {}
    if source_sck is not None:
        src_kwargs["net_sck"] = source_sck
    if source_mosi_miso is not None:
        src_kwargs["net_mosi_miso"] = source_mosi_miso
    if source_cs is not None:
        src_kwargs["net_cs"] = source_cs
    if src_kwargs:
        spi.set_source(**src_kwargs)
    if level_sck is not None:
        spi.set_sck_trigger_level(level_sck)
    if level_mosi_miso is not None:
        spi.set_mosi_miso_trigger_level(level_mosi_miso)
    if level_cs is not None:
        spi.set_cs_trigger_level(level_cs)
    if clk_edge.lower() == "positive":
        spi.set_clk_edge_positive()
    else:
        spi.set_clk_edge_negative()
    cond = trigger_on.lower()
    if cond == "timeout" and timeout is not None:
        spi.set_trigger_on_timeout(timeout)
    elif cond == "cs":
        spi.set_trigger_on_cs_high()
    if data is not None:
        spi.set_trigger_data(bits=data_bits, data=data)
    return json.dumps({"status": "ok", "net": net, "trigger": "spi", "trigger_on": trigger_on})


# ── Cursors ──────────────────────────────────────────────────────────

@mcp.tool()
def logic_cursor_set(net: str, cursor: str, x: float = None, y: float = None) -> str:
    """Set a logic analyzer cursor to an absolute position.

    Args:
        net: Logic net name (e.g., 'logic1')
        cursor: Which cursor — 'a' or 'b'
        x: X-axis position (time)
        y: Y-axis position
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    fn = {"a": la.cursor.set_a, "b": la.cursor.set_b}[cursor.lower()]
    fn(x=x, y=y)
    return json.dumps({"status": "ok", "net": net, "cursor": cursor, "x": x, "y": y})


@mcp.tool()
def logic_cursor_move(net: str, cursor: str, x: float = None, y: float = None) -> str:
    """Move a logic analyzer cursor by a relative offset.

    Args:
        net: Logic net name (e.g., 'logic1')
        cursor: Which cursor — 'a' or 'b'
        x: X-axis delta
        y: Y-axis delta
    """
    from lager import Net, NetType

    la = Net.get(net, type=NetType.Logic)
    fn = {"a": la.cursor.move_a, "b": la.cursor.move_b}[cursor.lower()]
    fn(x_del=x, y_del=y)
    return json.dumps({"status": "ok", "net": net, "cursor": cursor, "dx": x, "dy": y})


@mcp.tool()
def logic_cursor_hide(net: str) -> str:
    """Hide logic analyzer cursor display.

    Args:
        net: Logic net name (e.g., 'logic1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Logic).cursor.hide()
    return json.dumps({"status": "ok", "net": net, "action": "cursor_hide"})

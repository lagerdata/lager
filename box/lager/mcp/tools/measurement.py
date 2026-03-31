# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for measurement instruments (GPIO, ADC, DAC, thermocouple, watt meter)
via direct on-box Net API."""

import json

from ..server import mcp


@mcp.tool()
def gpio_read(net: str) -> str:
    """Read the state of a GPIO input pin.

    Returns 0 (low) or 1 (high).

    Args:
        net: GPIO net name (e.g., 'gpio1')
    """
    from lager import Net, NetType

    value = Net.get(net, type=NetType.GPIO).input()
    return json.dumps({"status": "ok", "net": net, "value": value})


@mcp.tool()
def gpio_set(net: str, level: int = 1) -> str:
    """Set a GPIO output pin to a specified level.

    Args:
        net: GPIO net name (e.g., 'gpio1')
        level: Output level — 0 for low, 1 for high (default: 1)
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.GPIO).output(level)
    return json.dumps({"status": "ok", "net": net, "level": level})


@mcp.tool()
def gpio_wait_for(net: str, level: int = 1, timeout: float = 30.0) -> str:
    """Wait for a GPIO input pin to reach a target level.

    Blocks until the pin reaches the target level or timeout expires.

    Args:
        net: GPIO net name (e.g., 'gpio1')
        level: Target level — 0 for low, 1 for high (default: 1)
        timeout: Maximum seconds to wait (default: 30)
    """
    from lager import Net, NetType

    gpio = Net.get(net, type=NetType.GPIO)
    try:
        elapsed = gpio.wait_for_level(level, timeout=timeout)
        return json.dumps({
            "status": "ok", "net": net, "level": level,
            "elapsed_s": round(elapsed, 4),
        })
    except TimeoutError:
        return json.dumps({
            "status": "timeout", "net": net, "level": level,
            "timeout_s": timeout,
        })


@mcp.tool()
def adc_read(net: str) -> str:
    """Read voltage from an ADC (analog-to-digital converter) channel.

    Args:
        net: ADC net name (e.g., 'adc1')
    """
    from lager import Net, NetType

    value = Net.get(net, type=NetType.ADC).input()
    return json.dumps({"status": "ok", "net": net, "voltage": value})


@mcp.tool()
def dac_set(net: str, voltage: float) -> str:
    """Set the output voltage of a DAC (digital-to-analog converter).

    Args:
        net: DAC net name (e.g., 'dac1')
        voltage: Output voltage in volts
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.DAC).output(voltage)
    return json.dumps({"status": "ok", "net": net, "voltage": voltage})


@mcp.tool()
def dac_read(net: str) -> str:
    """Read the current output voltage of a DAC channel.

    Args:
        net: DAC net name (e.g., 'dac1')
    """
    from lager import Net, NetType

    value = Net.get(net, type=NetType.DAC).get_voltage()
    return json.dumps({"status": "ok", "net": net, "voltage": value})


@mcp.tool()
def thermocouple_read(net: str) -> str:
    """Read temperature from a thermocouple in degrees Celsius.

    Args:
        net: Thermocouple net name (e.g., 'tc1')
    """
    from lager import Net, NetType

    value = Net.get(net, type=NetType.Thermocouple).read()
    return json.dumps({"status": "ok", "net": net, "temperature_c": value})


@mcp.tool()
def watt_read(net: str) -> str:
    """Read instantaneous power from a watt meter.

    Args:
        net: Watt meter net name (e.g., 'watt1')
    """
    from lager import Net, NetType

    value = Net.get(net, type=NetType.WattMeter).read()
    return json.dumps({"status": "ok", "net": net, "power_w": value})


@mcp.tool()
def watt_read_all(net: str) -> str:
    """Read all measurements (current, voltage, power) from a watt meter.

    Not all watt meter backends support this — falls back to read() for power only.

    Args:
        net: Watt meter net name (e.g., 'watt1')
    """
    from lager import Net, NetType

    meter = Net.get(net, type=NetType.WattMeter)
    try:
        data = meter.read_all()
        return json.dumps({"status": "ok", "net": net, **data})
    except (AttributeError, NotImplementedError):
        value = meter.read()
        return json.dumps({"status": "ok", "net": net, "power_w": value})

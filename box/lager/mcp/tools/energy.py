# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for energy analyzer via direct on-box Net API.

Supports PPK2 and Joulescope JS220 backends. The concrete implementation
is selected automatically by ``Net.get()`` based on the instrument field
in the bench netlist.
"""

import json

from ..server import mcp


@mcp.tool()
def energy_read(net: str, duration: float = 1.0) -> str:
    """Integrate energy over a duration.

    Accumulates current and power readings for the specified duration
    and returns total energy (joules, watt-hours) and charge (coulombs,
    amp-hours).

    Args:
        net: Energy analyzer net name (e.g., 'energy1')
        duration: Measurement duration in seconds (default: 1.0)
    """
    from lager import Net, NetType

    reading = Net.get(net, type=NetType.EnergyAnalyzer).read_energy(duration)
    return json.dumps({"status": "ok", "net": net, "duration": duration, **reading}, default=str)


@mcp.tool()
def energy_stats(net: str, duration: float = 1.0) -> str:
    """Compute current/voltage/power statistics over a duration.

    Returns mean, min, max, and standard deviation for current, voltage,
    and power over the measurement window.

    Args:
        net: Energy analyzer net name (e.g., 'energy1')
        duration: Measurement duration in seconds (default: 1.0)
    """
    from lager import Net, NetType

    stats = Net.get(net, type=NetType.EnergyAnalyzer).read_stats(duration)
    return json.dumps({"status": "ok", "net": net, "duration": duration, "stats": stats}, default=str)

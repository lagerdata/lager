# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for box health and status (on-box, no CLI subprocess)."""

import json

from ..server import mcp


@mcp.tool()
def box_health() -> str:
    """Check that the box and MCP server are healthy.

    Returns box ID, version, service status, and net count.
    Use this as a quick connectivity test before running operations.
    """
    from ..config import get_box_id, get_box_version
    from ..server_state import get_bench

    bench = get_bench()
    return json.dumps({
        "status": "ok",
        "box_id": get_box_id(),
        "version": get_box_version(),
        "nets": len(bench.nets),
        "instruments": len(bench.instruments),
    })


@mcp.tool()
def list_nets() -> str:
    """List all configured nets (hardware connections) on this box.

    Nets are named references to physical hardware connections:
    power supplies, debug probes, UART ports, SPI/I2C buses,
    GPIO pins, ADC channels, etc.
    """
    from ..server_state import get_bench

    bench = get_bench()
    nets = [
        {"name": n.name, "type": n.net_type, "instrument": n.instrument, "channel": n.channel}
        for n in bench.nets
    ]
    return json.dumps({"status": "ok", "nets": nets, "count": len(nets)}, indent=2)


@mcp.tool()
def list_instruments() -> str:
    """List all hardware instruments attached to this box.

    Shows instrument types (LabJack, Rigol, Aardvark, etc.),
    their connection strings, and channels.
    """
    from ..server_state import get_bench

    bench = get_bench()
    instruments = [
        {
            "name": i.name,
            "type": i.instrument_type,
            "connection": i.connection,
            "channels": i.channels,
        }
        for i in bench.instruments
    ]
    return json.dumps({"status": "ok", "instruments": instruments, "count": len(instruments)}, indent=2)


@mcp.tool()
def reload_bench_config() -> str:
    """Reload bench configuration from disk.

    Re-reads /etc/lager/saved_nets.json and /etc/lager/bench.json
    and rebuilds the capability graph. Use after adding or removing
    nets or instruments.
    """
    from ..server_state import reload_bench

    reload_bench()
    from ..server_state import get_bench, get_capability_graph

    bench = get_bench()
    graph = get_capability_graph()
    return json.dumps({
        "status": "ok",
        "nets": len(bench.nets),
        "instruments": len(bench.instruments),
        "capabilities": len(graph.nodes),
    })

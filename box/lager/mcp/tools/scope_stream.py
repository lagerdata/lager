# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for PicoScope streaming via the on-box oscilloscope daemon.

The PicoScope streaming workflow uses a WebSocket daemon (oscilloscope-daemon)
running on port 8085. These tools communicate with the daemon directly on
localhost to control streaming acquisition, configure channels, and capture
data.
"""

import asyncio
import json
import socket

from ..server import mcp

DAEMON_HOST = "localhost"
DAEMON_COMMAND_PORT = 8085


def _send_command(command: dict) -> dict:
    """Send a JSON command to the oscilloscope daemon via WebSocket."""
    try:
        import websockets
    except ImportError:
        return {"error": "websockets library not installed on box"}

    async def _do():
        uri = f"ws://{DAEMON_HOST}:{DAEMON_COMMAND_PORT}"
        async with websockets.connect(uri, close_timeout=5) as ws:
            await ws.send(json.dumps(command))
            response = await asyncio.wait_for(ws.recv(), timeout=10.0)
            return json.loads(response)

    try:
        return asyncio.run(_do())
    except ConnectionRefusedError:
        return {"error": "Oscilloscope daemon not running"}
    except asyncio.TimeoutError:
        return {"error": "Timeout waiting for daemon response"}
    except Exception as exc:
        return {"error": str(exc)}


def _map_channel(channel: str) -> dict:
    mapping = {"A": "A", "B": "B", "C": "C", "D": "D",
               "1": "A", "2": "B", "3": "C", "4": "D"}
    return {"Alphabetic": mapping.get(channel.upper(), "A")}


def _box_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@mcp.tool()
def picoscope_status() -> str:
    """Check if the PicoScope oscilloscope daemon is running.

    Returns daemon status and whether it is ready for acquisition.
    """
    resp = _send_command({"command": "GetChannelCount"})
    if "error" in resp:
        return json.dumps({"status": "error", "daemon": "not_running", **resp})
    ready_resp = _send_command({"command": "IsReady"})
    return json.dumps({
        "status": "ok",
        "daemon": "running",
        "channel_count": resp,
        "is_ready": ready_resp.get("is_ready", False),
    }, default=str)


@mcp.tool()
def picoscope_stream_start(
    channel: str = "A",
    volts_per_div: float = 1.0,
    time_per_div: float = 0.001,
    trigger_level: float = 0.0,
    trigger_slope: str = "rising",
    capture_mode: str = "auto",
    coupling: str = "dc",
) -> str:
    """Start PicoScope streaming acquisition.

    Configures the channel, trigger, and timebase then starts continuous
    acquisition. The live waveform is viewable via the box's web
    oscilloscope UI.

    Args:
        channel: Channel letter — 'A', 'B', 'C', or 'D' (default: 'A')
        volts_per_div: Vertical scale in V/div (default: 1.0)
        time_per_div: Horizontal scale in s/div (default: 0.001)
        trigger_level: Trigger level in volts (default: 0.0)
        trigger_slope: 'rising', 'falling', or 'either' (default: 'rising')
        capture_mode: 'auto', 'normal', or 'single' (default: 'auto')
        coupling: 'dc' or 'ac' (default: 'dc')
    """
    ch = _map_channel(channel)
    errors = []

    for cmd in [
        {"command": "EnableChannel", "channel": ch},
        {"command": "SetVoltsPerDiv", "channel": ch, "volts_per_div": volts_per_div},
        {"command": "SetTimePerDiv", "time_per_div": time_per_div},
        {"command": "SetCoupling", "channel": ch, "coupling": coupling.upper()},
        {"command": "SetTriggerLevel", "trigger_level": trigger_level},
        {"command": "SetTriggerSource", "trigger_source": ch},
        {"command": "SetTriggerSlope", "trigger_slope": trigger_slope.lower()},
        {"command": "SetCaptureMode", "capture_mode": capture_mode.lower()},
    ]:
        resp = _send_command(cmd)
        if "error" in resp:
            errors.append(f"{cmd['command']}: {resp['error']}")

    resp = _send_command({"command": "StartAcquisition", "trigger_position_percent": 50.0})
    if "error" in resp:
        return json.dumps({"status": "error", "error": resp["error"], "warnings": errors})

    ip = _box_ip()
    return json.dumps({
        "status": "ok",
        "streaming": True,
        "channel": channel,
        "visualization_url": f"http://{ip}:8080/web_oscilloscope.html?host={ip}&port={DAEMON_COMMAND_PORT}",
        **({"warnings": errors} if errors else {}),
    })


@mcp.tool()
def picoscope_stream_stop() -> str:
    """Stop PicoScope streaming acquisition."""
    resp = _send_command({"command": "StopAcquisition"})
    if "error" in resp:
        return json.dumps({"status": "error", **resp})
    return json.dumps({"status": "ok", "streaming": False})


@mcp.tool()
def picoscope_config(
    channel: str = "A",
    enable: bool = True,
    volts_per_div: float = 0,
    time_per_div: float = 0,
    coupling: str = "",
) -> str:
    """Configure a PicoScope channel without starting acquisition.

    Args:
        channel: Channel letter — 'A', 'B', 'C', or 'D'
        enable: True to enable channel, False to disable
        volts_per_div: Vertical scale in V/div (0 = don't change)
        time_per_div: Horizontal scale in s/div (0 = don't change)
        coupling: 'dc' or 'ac' (empty = don't change)
    """
    ch = _map_channel(channel)
    results = {}

    if enable:
        results["enable"] = _send_command({"command": "EnableChannel", "channel": ch})
    else:
        results["disable"] = _send_command({"command": "DisableChannel", "channel": ch})

    if volts_per_div > 0:
        results["volts_per_div"] = _send_command({
            "command": "SetVoltsPerDiv", "channel": ch, "volts_per_div": volts_per_div,
        })
    if time_per_div > 0:
        results["time_per_div"] = _send_command({
            "command": "SetTimePerDiv", "time_per_div": time_per_div,
        })
    if coupling:
        results["coupling"] = _send_command({
            "command": "SetCoupling", "channel": ch, "coupling": coupling.upper(),
        })

    has_error = any("error" in v for v in results.values() if isinstance(v, dict))
    return json.dumps({
        "status": "error" if has_error else "ok",
        "channel": channel,
        "results": results,
    }, default=str)


@mcp.tool()
def picoscope_capture(
    channel: str = "A",
    duration_ms: float = 100.0,
    trigger_level: float = 0.0,
    trigger_slope: str = "rising",
) -> str:
    """Capture a single waveform from the PicoScope and return sample data.

    Configures the scope for a single capture, waits for the trigger,
    and returns the captured waveform data.

    Args:
        channel: Channel letter — 'A', 'B', 'C', or 'D'
        duration_ms: Capture window in milliseconds (default: 100)
        trigger_level: Trigger level in volts (default: 0.0)
        trigger_slope: 'rising' or 'falling' (default: 'rising')
    """
    ch = _map_channel(channel)

    _send_command({"command": "EnableChannel", "channel": ch})
    time_per_div = (duration_ms / 1000.0) / 10.0
    _send_command({"command": "SetTimePerDiv", "time_per_div": time_per_div})
    _send_command({"command": "SetTriggerLevel", "trigger_level": trigger_level})
    _send_command({"command": "SetTriggerSource", "trigger_source": ch})
    _send_command({"command": "SetTriggerSlope", "trigger_slope": trigger_slope.lower()})
    _send_command({"command": "SetCaptureMode", "capture_mode": "single"})

    resp = _send_command({"command": "StartAcquisition", "trigger_position_percent": 50.0})
    if "error" in resp:
        return json.dumps({"status": "error", "error": resp["error"]})

    import time
    deadline = time.time() + (duration_ms / 1000.0) + 5.0
    while time.time() < deadline:
        ready = _send_command({"command": "IsReady"})
        if ready.get("is_ready"):
            break
        time.sleep(0.1)

    data_resp = _send_command({"command": "GetData", "channel": ch})
    _send_command({"command": "StopAcquisition"})

    return json.dumps({
        "status": "ok",
        "channel": channel,
        "duration_ms": duration_ms,
        "data": data_resp,
    }, default=str)

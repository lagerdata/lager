#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Oscilloscope streaming implementation for PicoScope devices.

This script runs on the box and talks to the oscilloscope-daemon over
WebSocket. The daemon listens on loopback only: TCP 127.0.0.1:8085, which
this script uses, and the Unix socket /tmp/lager-scope.sock. A browser never
reaches it. The box HTTP server on port 9000 serves the /scope page and
relays captures to it.

Every reply arrives wrapped as {"Response": {"response": NAME, ...}}, and a
refused command as {"Response": {"response": "Error", "message": ...}}.
"""

import asyncio
import csv
import json
import math
import os
import struct
import sys

try:
    import websockets
except ImportError:
    websockets = None


DAEMON_HOST = "127.0.0.1"
DAEMON_COMMAND_PORT = 8085
DAEMON_URI = f"ws://{DAEMON_HOST}:{DAEMON_COMMAND_PORT}"
REPLY_TIMEOUT_S = 10.0

# LSCP/1, the binary frame a capture arrives in (the daemon's
# protocol/src/lscp.rs): a 64-byte header, a 16-byte descriptor per channel,
# then little-endian int16 samples, one channel after another.
LSCP_MAGIC = 0x5043534C
LSCP_VERSION = 1
LSCP_HEADER = struct.Struct("<IHHQQdIIIBBHI")
LSCP_HEADER_SIZE = 64
LSCP_CHANNEL = struct.Struct("<BBBBffI")
# A sample the rolling screen has not reached yet. No ADC produces it.
LSCP_NO_SAMPLE = -32768

# The columns the box's PicoScope.stream_capture writes, so a CSV from either
# route reads the same.
CSV_FIELDS = ("capture", "channel", "sample_index", "time_ns", "voltage")


class CaptureError(Exception):
    """Why a capture produced no data, and what to do about it."""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


def _unwrap(message) -> dict:
    """The reply inside the daemon's envelope, or {"error": ...} for a refusal."""
    try:
        decoded = json.loads(message)
    except (TypeError, ValueError):
        return {"error": "the oscilloscope daemon sent a reply that is not JSON"}
    reply = decoded.get("Response", decoded) if isinstance(decoded, dict) else None
    if not isinstance(reply, dict):
        return {"error": "the oscilloscope daemon sent an unexpected reply"}
    if reply.get("response") == "Error":
        return {"error": reply.get("message") or "the oscilloscope daemon refused the command"}
    return reply


async def send_command_async(command: dict) -> dict:
    """Send one command to the daemon and return its reply.

    A refusal comes back as {"error": ...}, and so does a daemon that does
    not answer, which also sets "unreachable".
    """
    if websockets is None:
        return {"error": "the websockets library is not installed"}
    try:
        async with websockets.connect(DAEMON_URI, close_timeout=5) as ws:
            await ws.send(json.dumps(command))
            return _unwrap(await asyncio.wait_for(ws.recv(), timeout=REPLY_TIMEOUT_S))
    # Ahead of OSError: from Python 3.11, TimeoutError is one.
    except asyncio.TimeoutError:
        return {"error": "the oscilloscope daemon did not reply in time"}
    except OSError as e:
        return {"error": f"the oscilloscope daemon does not answer on {DAEMON_URI} ({e})",
                "unreachable": True}
    except Exception as e:
        return {"error": f"communication error: {e}"}


def send_command(command: dict) -> dict:
    """Send a command to the oscilloscope daemon and get response."""
    return asyncio.run(send_command_async(command))


def _apply(steps, report=False):
    """Send each (what, command) in turn, and stop at the first refusal."""
    for what, command in steps:
        reply = send_command(command)
        if "error" in reply:
            sys.stderr.write(f"Error: cannot {what}: {reply['error']}\n")
            sys.exit(1)
        if report:
            print(f"[OK] {what[0].upper()}{what[1:]}")


def map_channel(channel: str) -> dict:
    """Map channel string to daemon format."""
    if channel in ("A", "1"):
        return {"Alphabetic": "A"}
    elif channel in ("B", "2"):
        return {"Alphabetic": "B"}
    elif channel in ("C", "3"):
        return {"Alphabetic": "C"}
    elif channel in ("D", "4"):
        return {"Alphabetic": "D"}
    return {"Alphabetic": "A"}


def map_trigger_slope(slope: str) -> str:
    """Map trigger slope to daemon format."""
    mapping = {
        "rising": "rising",
        "falling": "falling",
        "either": "either",
        "both": "either"
    }
    return mapping.get(slope.lower(), "rising")


def map_capture_mode(mode: str) -> str:
    """Map capture mode to daemon format."""
    mapping = {
        "auto": "auto",
        "normal": "normal",
        "single": "single"
    }
    return mapping.get(mode.lower(), "auto")


def map_coupling(coupling: str) -> str:
    """Map coupling to daemon format."""
    return coupling.upper()


def stream_start(params: dict):
    """Apply the stream settings, then start acquisition.

    Any refused setting stops the script before acquisition starts, so a
    stream never runs on settings other than the ones asked for.
    """
    channel = params.get("channel", "A")
    volts_per_div = params.get("volts_per_div", 1.0)
    time_per_div = params.get("time_per_div", 0.001)
    trigger_level = params.get("trigger_level", 0.0)
    trigger_slope = params.get("trigger_slope", "rising")
    capture_mode = params.get("capture_mode", "auto")
    coupling = params.get("coupling", "dc")
    quiet = params.get("quiet", False)
    json_output = params.get("json_output", False)
    verbose = params.get("verbose", False)

    # Validate parameters
    if volts_per_div <= 0:
        sys.stderr.write("Error: volts_per_div must be positive\n")
        sys.exit(2)

    if time_per_div <= 0:
        sys.stderr.write("Error: time_per_div must be positive\n")
        sys.exit(2)

    if channel not in ["A", "B", "C", "D", "1", "2", "3", "4"]:
        sys.stderr.write(f"Error: Invalid channel '{channel}'. Must be A, B, C, or D\n")
        sys.exit(2)

    source = map_channel(channel)
    letter = source["Alphabetic"]
    _apply([
        (f"enable channel {letter}",
         {"command": "EnableChannel", "channel": source}),
        (f"set channel {letter} to {volts_per_div} V/div",
         {"command": "SetVoltsPerDiv", "channel": source, "volts_per_div": volts_per_div}),
        (f"set the timebase to {time_per_div} s/div",
         {"command": "SetTimePerDiv", "time_per_div": time_per_div}),
        (f"set channel {letter} coupling to {map_coupling(coupling)}",
         {"command": "SetCoupling", "channel": source, "coupling": map_coupling(coupling)}),
        (f"set the trigger level to {trigger_level} V",
         {"command": "SetTriggerLevel", "trigger_level": trigger_level}),
        (f"set the trigger source to channel {letter}",
         {"command": "SetTriggerSource", "trigger_source": source}),
        (f"set the trigger slope to {map_trigger_slope(trigger_slope)}",
         {"command": "SetTriggerSlope", "trigger_slope": map_trigger_slope(trigger_slope)}),
        (f"set the capture mode to {map_capture_mode(capture_mode)}",
         {"command": "SetCaptureMode", "capture_mode": map_capture_mode(capture_mode)}),
        ("start acquisition",
         {"command": "StartAcquisition", "trigger_position_percent": 50.0}),
    ], report=verbose)

    # The viewer link is the CLI's to print: on an access-gated box it needs
    # the user's sign-in token, which never comes to the box.
    if json_output:
        print(json.dumps({
            "status": "success",
            "message": "Streaming started",
            "command_port": DAEMON_COMMAND_PORT,
        }))
    elif not quiet:
        print("Streaming started")


def stream_stop(params: dict):
    """Stop oscilloscope streaming acquisition."""
    _apply([("stop acquisition", {"command": "StopAcquisition"})])
    print("Streaming stopped")


def stream_status(params: dict):
    """Report whether the daemon answers and what it reports about the scope.

    Exits 1 when the daemon does not answer or cannot reach the scope, so a
    script can test for a scope that is ready to stream.
    """
    count = send_command({"command": "GetChannelCount"})
    if count.get("unreachable"):
        print("Oscilloscope daemon: NOT RUNNING")
        print(f"  Error: {count['error']}")
        print("\nRestart the box container to start the daemon.")
        sys.exit(1)

    print("Oscilloscope daemon: RUNNING")
    print(f"  Command port: {DAEMON_COMMAND_PORT}")
    if "error" in count:
        print(f"  Scope: {count['error']}")
        sys.exit(1)
    print(f"  Channels: {count.get('channel_count')}")

    ready = send_command({"command": "IsReady"})
    if "error" in ready:
        print(f"  Ready: unknown ({ready['error']})")
        sys.exit(1)
    print(f"  Ready: {bool(ready.get('is_ready'))}")


def stream_config(params: dict):
    """Change settings without starting or stopping acquisition."""
    channel = params.get("channel")
    target = map_channel(channel) if channel else None
    letter = target["Alphabetic"] if target else None

    per_channel = [key for key in ("enable", "volts_per_div", "coupling")
                   if params.get(key) is not None]
    if per_channel and target is None:
        sys.stderr.write(f"Error: {', '.join(per_channel)} needs a channel\n")
        sys.exit(2)

    steps = []
    if params.get("enable") is True:
        steps.append((f"enable channel {letter}",
                      {"command": "EnableChannel", "channel": target}))
    if params.get("enable") is False:
        steps.append((f"disable channel {letter}",
                      {"command": "DisableChannel", "channel": target}))
    if params.get("volts_per_div") is not None:
        steps.append((f"set channel {letter} to {params['volts_per_div']} V/div",
                      {"command": "SetVoltsPerDiv", "channel": target,
                       "volts_per_div": params["volts_per_div"]}))
    if params.get("time_per_div") is not None:
        steps.append((f"set the timebase to {params['time_per_div']} s/div",
                      {"command": "SetTimePerDiv", "time_per_div": params["time_per_div"]}))
    if params.get("trigger_level") is not None:
        steps.append((f"set the trigger level to {params['trigger_level']} V",
                      {"command": "SetTriggerLevel", "trigger_level": params["trigger_level"]}))
    if params.get("trigger_source") is not None:
        source = map_channel(params["trigger_source"])
        steps.append((f"set the trigger source to channel {source['Alphabetic']}",
                      {"command": "SetTriggerSource", "trigger_source": source}))
    if params.get("trigger_slope") is not None:
        slope = map_trigger_slope(params["trigger_slope"])
        steps.append((f"set the trigger slope to {slope}",
                      {"command": "SetTriggerSlope", "trigger_slope": slope}))
    if params.get("capture_mode") is not None:
        mode = map_capture_mode(params["capture_mode"])
        steps.append((f"set the capture mode to {mode}",
                      {"command": "SetCaptureMode", "capture_mode": mode}))
    if params.get("coupling") is not None:
        coupling = map_coupling(params["coupling"])
        steps.append((f"set channel {letter} coupling to {coupling}",
                      {"command": "SetCoupling", "channel": target, "coupling": coupling}))

    if not steps:
        sys.stderr.write("Error: no setting to change\n")
        sys.exit(2)
    _apply(steps, report=True)


def _frame_layout(data):
    """(header fields, channel count, payload offset) of one LSCP/1 frame."""
    if len(data) < LSCP_HEADER_SIZE:
        raise CaptureError(f"a capture frame of {len(data)} bytes is shorter than its header")
    header = LSCP_HEADER.unpack_from(data)
    magic, version = header[0], header[1]
    if magic != LSCP_MAGIC or version != LSCP_VERSION:
        raise CaptureError("a capture frame is not in the LSCP/1 format")
    per_channel, channel_count = header[8], header[9]
    payload = LSCP_HEADER_SIZE + channel_count * LSCP_CHANNEL.size
    if len(data) != payload + 2 * per_channel * channel_count:
        raise CaptureError("a capture frame is not the size its header gives")
    return header, channel_count, payload


def decode_frame(data):
    """(sample interval in ns, samples per channel, channels, samples).

    ``channels`` holds (letter, volts per count, offset volts) for each
    channel, and ``samples`` every channel's counts, one after another.
    """
    header, channel_count, payload = _frame_layout(data)
    interval_ns, per_channel = header[5], header[8]
    channels = []
    for index in range(channel_count):
        number, _range, _coupling, _reserved, scale, offset, _spare = \
            LSCP_CHANNEL.unpack_from(data, LSCP_HEADER_SIZE + index * LSCP_CHANNEL.size)
        channels.append((chr(ord("A") + number), scale, offset))
    samples = struct.unpack_from(f"<{per_channel * channel_count}h", data, payload)
    return interval_ns, per_channel, channels, samples


async def _ask(ws, command):
    """Send a command on an open connection and return its reply."""
    await ws.send(json.dumps(command))
    reply = _unwrap(await asyncio.wait_for(ws.recv(), timeout=REPLY_TIMEOUT_S))
    if "error" in reply:
        raise CaptureError(reply["error"])
    return reply


async def _record(duration, max_samples, netname):
    """The capture frames the scope publishes in the next ``duration`` seconds.

    A subscription gets each new capture once. Asking for the latest one in
    a loop instead returns the same capture again until the next arrives.
    Frames are kept as received, two bytes a sample, and decoded later.
    """
    if websockets is None:
        raise CaptureError("the websockets library is not installed")
    loop = asyncio.get_running_loop()
    frames = []
    per_channel = 0
    try:
        async with websockets.connect(DAEMON_URI, close_timeout=5, max_size=None) as ws:
            state = (await _ask(ws, {"command": "GetState"})).get("state") or {}
            if not (state.get("acquiring") or state.get("rolling")):
                raise CaptureError(
                    "the scope is stopped, so there is nothing to capture",
                    hint=f"Start it first: lager scope {netname} stream start")
            await _ask(ws, {"command": "Subscribe"})

            deadline = loop.time() + duration
            while max_samples is None or per_channel < max_samples:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if isinstance(message, str):
                    # The daemon says so when it drops captures for a
                    # client that reads too slowly.
                    notice = _unwrap(message)
                    if "error" in notice:
                        sys.stderr.write(f"Warning: {notice['error']}\n")
                    continue
                header, _count, _payload = _frame_layout(message)
                frames.append(bytes(message))
                per_channel += header[8]
    except CaptureError:
        raise
    except asyncio.TimeoutError:
        raise CaptureError("the oscilloscope daemon did not reply in time")
    except OSError as e:
        raise CaptureError(f"the oscilloscope daemon does not answer on {DAEMON_URI} ({e})")
    except Exception as e:
        raise CaptureError(f"communication error: {e}")

    if not frames:
        raise CaptureError(
            f"no capture arrived in {duration} s",
            hint="In normal and single mode the scope waits for a trigger. "
                 "Check the trigger level and source.")
    return frames


def write_csv(path, frames, max_samples=None):
    """Write the frames as CSV rows; return (captures, rows)."""
    rows = 0
    written = 0
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_FIELDS)
        for capture, data in enumerate(frames):
            interval_ns, per_channel, channels, samples = decode_frame(data)
            take = per_channel if max_samples is None else min(per_channel, max_samples - written)
            for index, (letter, scale, offset) in enumerate(channels):
                base = index * per_channel
                for i in range(take):
                    count = samples[base + i]
                    volts = math.nan if count == LSCP_NO_SAMPLE else count * scale + offset
                    writer.writerow((capture, letter, i, i * interval_ns, volts))
            rows += take * len(channels)
            written += take
    return len(frames), rows


def _capture_failed(error, json_output):
    if json_output:
        print(json.dumps({"status": "error", "message": str(error)}))
    sys.stderr.write(f"Error: {error}\n")
    if error.hint:
        sys.stderr.write(f"{error.hint}\n")
    sys.exit(1)


def stream_capture(params: dict):
    """Record the captures the scope takes over ``duration`` to a CSV file.

    The scope has to be acquiring already (``stream start``). The file stays
    on the box; a relative path is relative to the directory the script runs
    in, which is the box's /tmp.
    """
    output = params.get("output", "scope_data.csv")
    duration = params.get("duration", 1.0)
    max_samples = params.get("samples")
    quiet = params.get("quiet", False)
    json_output = params.get("json_output", False)
    verbose = params.get("verbose", False)
    netname = params.get("netname") or "NET_NAME"
    scp_host = params.get("scp_host")

    # Validate parameters
    if duration <= 0:
        sys.stderr.write("Error: duration must be positive\n")
        sys.exit(2)

    if max_samples is not None and max_samples <= 0:
        sys.stderr.write("Error: samples must be positive\n")
        sys.exit(2)

    path = os.path.abspath(output)
    output_dir = os.path.dirname(path)
    if not os.path.exists(output_dir):
        sys.stderr.write(f"Error: Output directory does not exist: {output_dir}\n")
        sys.exit(2)
    if not os.access(output_dir, os.W_OK):
        sys.stderr.write(f"Error: Output directory is not writable: {output_dir}\n")
        sys.exit(2)

    if verbose:
        print("Capturing oscilloscope data...")
        print(f"  Output file: {path}")
        print(f"  Duration: {duration}s")
        if max_samples:
            print(f"  Max samples: {max_samples}")
    elif not quiet and not json_output:
        print(f"Capturing to {path} ({duration}s)")

    try:
        frames = asyncio.run(_record(duration, max_samples, netname))
    except CaptureError as e:
        _capture_failed(e, json_output)
    try:
        captures, rows = write_csv(path, frames, max_samples)
    except OSError as e:
        _capture_failed(CaptureError(f"cannot write {path}: {e}"), json_output)

    if json_output:
        print(json.dumps({
            "status": "success",
            "captures": captures,
            "total_samples": rows,
            "output": path,
        }))
    elif not quiet:
        print("Capture complete")
        print(f"Captures: {captures}, rows: {rows}, file on the box: {path}")
        if scp_host:
            print(f"To copy it here: scp {scp_host}:{path} .")


def main():
    """Main entry point."""
    command_data = os.environ.get("LAGER_COMMAND_DATA", "{}")

    try:
        data = json.loads(command_data)
    except json.JSONDecodeError as e:
        print(f"Error parsing command data: {e}", file=sys.stderr)
        sys.exit(1)

    action = data.get("action", "")
    params = data.get("params", {})

    actions = {
        "stream_start": stream_start,
        "stream_stop": stream_stop,
        "stream_status": stream_status,
        "stream_config": stream_config,
        "stream_capture": stream_capture,
    }

    if action in actions:
        actions[action](params)
    else:
        print(f"Unknown action: {action}", file=sys.stderr)
        print(f"Available actions: {', '.join(actions.keys())}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

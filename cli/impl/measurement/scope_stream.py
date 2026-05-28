#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Oscilloscope streaming implementation for PicoScope devices.

This script runs on the box and communicates with the oscilloscope-daemon
via WebSocket to control PicoScope streaming.

The oscilloscope-daemon runs on ports:
  - 8082: Commands (control channel) - WebSocket
  - 8083: Browser streaming (visualization) - WebSocket
  - 8084: Database streaming (data logging) - WebSocket
"""

import json
import os
import sys
import time
import csv
import asyncio

try:
    import websockets
except ImportError:
    websockets = None


DAEMON_HOST = "localhost"
DAEMON_COMMAND_PORT = 8085  # WebSocket port for CLI commands


async def send_command_async(command: dict) -> dict:
    """Send a command to the oscilloscope daemon via WebSocket and get response."""
    if websockets is None:
        return {"error": "websockets library not installed. Run: pip install websockets"}

    uri = f"ws://{DAEMON_HOST}:{DAEMON_COMMAND_PORT}"
    try:
        async with websockets.connect(uri, close_timeout=5) as ws:
            # Send command as JSON
            await ws.send(json.dumps(command))

            # Wait for response
            response = await asyncio.wait_for(ws.recv(), timeout=10.0)
            return json.loads(response)

    except ConnectionRefusedError:
        return {"error": "Oscilloscope daemon not running. Is the daemon started?"}
    except asyncio.TimeoutError:
        return {"error": "Timeout waiting for daemon response"}
    except Exception as e:
        return {"error": f"Communication error: {str(e)}"}


def send_command(command: dict) -> dict:
    """Send a command to the oscilloscope daemon and get response."""
    return asyncio.run(send_command_async(command))


def check_daemon_status() -> dict:
    """Check if the oscilloscope daemon is running and responsive."""
    try:
        # Try to get channel count via WebSocket
        response = send_command({"command": "GetChannelCount"})
        if "error" not in response:
            return {
                "status": "running",
                "port": DAEMON_COMMAND_PORT,
                "response": response
            }
        return {"status": "not running", "error": response.get("error", "Unknown error")}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
    """Start oscilloscope streaming acquisition."""
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

    # Enable channel
    response = send_command({
        "command": "EnableChannel",
        "channel": map_channel(channel)
    })
    if "error" in response:
        sys.stderr.write(f"Error: Could not enable channel: {response['error']}\n")
        sys.exit(1)

    # Set volts per division
    response = send_command({
        "command": "SetVoltsPerDiv",
        "channel": map_channel(channel),
        "volts_per_div": volts_per_div
    })
    if "error" in response:
        sys.stderr.write(f"Warning: Could not set volts/div: {response.get('error', response)}\n")

    # Set time per division
    response = send_command({
        "command": "SetTimePerDiv",
        "time_per_div": time_per_div
    })
    if "error" in response:
        sys.stderr.write(f"Warning: Could not set time/div: {response.get('error', response)}\n")

    # Set coupling
    response = send_command({
        "command": "SetCoupling",
        "channel": map_channel(channel),
        "coupling": map_coupling(coupling)
    })
    if "error" in response:
        sys.stderr.write(f"Warning: Could not set coupling: {response.get('error', response)}\n")

    # Set trigger level
    response = send_command({
        "command": "SetTriggerLevel",
        "trigger_level": trigger_level
    })
    if "error" in response:
        sys.stderr.write(f"Warning: Could not set trigger level: {response.get('error', response)}\n")

    # Set trigger source
    response = send_command({
        "command": "SetTriggerSource",
        "trigger_source": map_channel(channel)
    })
    if "error" in response:
        sys.stderr.write(f"Warning: Could not set trigger source: {response.get('error', response)}\n")

    # Set trigger slope
    response = send_command({
        "command": "SetTriggerSlope",
        "trigger_slope": map_trigger_slope(trigger_slope)
    })
    if "error" in response:
        sys.stderr.write(f"Warning: Could not set trigger slope: {response.get('error', response)}\n")

    # Set capture mode
    response = send_command({
        "command": "SetCaptureMode",
        "capture_mode": map_capture_mode(capture_mode)
    })
    if "error" in response:
        sys.stderr.write(f"Warning: Could not set capture mode: {response.get('error', response)}\n")

    # Start acquisition
    response = send_command({
        "command": "StartAcquisition",
        "trigger_position_percent": 50.0
    })
    if "error" in response:
        sys.stderr.write(f"Error: Could not start acquisition: {response['error']}\n")
        sys.exit(1)

    # Get box IP from params
    box_ip = params.get("box_ip", "localhost")

    # Output based on format flags
    if json_output:
        result = {
            "status": "success",
            "message": "Streaming started",
            "visualization_url": f"http://{box_ip}:8080/web_oscilloscope.html?host={box_ip}&port={DAEMON_COMMAND_PORT}",
            "command_port": DAEMON_COMMAND_PORT
        }
        print(json.dumps(result))
    elif not quiet:
        print("Streaming started")
        print(f"Visualization: http://{box_ip}:8080/web_oscilloscope.html?host={box_ip}&port={DAEMON_COMMAND_PORT}")


def stream_stop(params: dict):
    """Stop oscilloscope streaming acquisition."""
    response = send_command({"command": "StopAcquisition"})
    if "error" in response:
        sys.stderr.write(f"Error: Could not stop acquisition: {response['error']}\n")
        sys.exit(1)
    print("Streaming stopped")


def stream_status(params: dict):
    """Check oscilloscope daemon status."""
    status = check_daemon_status()

    if status.get("status") == "running":
        print("Oscilloscope daemon: RUNNING")
        print(f"  Command port: {status.get('port', DAEMON_COMMAND_PORT)}")

        # Get additional info
        response = send_command({"command": "GetChannelCount"})
        if "channel_count" in str(response):
            print(f"  Response: {response}")

        # Check if ready
        response = send_command({"command": "IsReady"})
        if "is_ready" in str(response):
            ready = response.get("is_ready", False)
            print(f"  Ready: {ready}")
    else:
        print("Oscilloscope daemon: NOT RUNNING")
        print(f"  Error: {status.get('error', 'Unknown')}")
        print("\nTo start the daemon, restart the Docker container or run:")
        print("  /usr/local/bin/oscilloscope-daemon")


def stream_config(params: dict):
    """Configure oscilloscope settings."""
    channel = params.get("channel")

    if params.get("enable") is True and channel:
        response = send_command({
            "command": "EnableChannel",
            "channel": map_channel(channel)
        })
        print(f"Enable channel {channel}: {response}")

    if params.get("enable") is False and channel:
        response = send_command({
            "command": "DisableChannel",
            "channel": map_channel(channel)
        })
        print(f"Disable channel {channel}: {response}")

    if params.get("volts_per_div") is not None and channel:
        response = send_command({
            "command": "SetVoltsPerDiv",
            "channel": map_channel(channel),
            "volts_per_div": params["volts_per_div"]
        })
        print(f"Set volts/div: {response}")

    if params.get("time_per_div") is not None:
        response = send_command({
            "command": "SetTimePerDiv",
            "time_per_div": params["time_per_div"]
        })
        print(f"Set time/div: {response}")

    if params.get("trigger_level") is not None:
        response = send_command({
            "command": "SetTriggerLevel",
            "trigger_level": params["trigger_level"]
        })
        print(f"Set trigger level: {response}")

    if params.get("trigger_source") is not None:
        response = send_command({
            "command": "SetTriggerSource",
            "trigger_source": map_channel(params["trigger_source"])
        })
        print(f"Set trigger source: {response}")

    if params.get("trigger_slope") is not None:
        response = send_command({
            "command": "SetTriggerSlope",
            "trigger_slope": map_trigger_slope(params["trigger_slope"])
        })
        print(f"Set trigger slope: {response}")

    if params.get("capture_mode") is not None:
        response = send_command({
            "command": "SetCaptureMode",
            "capture_mode": map_capture_mode(params["capture_mode"])
        })
        print(f"Set capture mode: {response}")

    if params.get("coupling") is not None and channel:
        response = send_command({
            "command": "SetCoupling",
            "channel": map_channel(channel),
            "coupling": map_coupling(params["coupling"])
        })
        print(f"Set coupling: {response}")


def stream_capture(params: dict):
    """Capture oscilloscope data to file.

    NOTE: This function currently has limitations communicating with the oscilloscope daemon.
    The GetTriggeredData command may not return data in the expected format.
    Consider using the web visualization interface for data capture instead.
    """
    output = params.get("output", "scope_data.csv")
    duration = params.get("duration", 1.0)
    max_samples = params.get("samples")
    quiet = params.get("quiet", False)
    json_output = params.get("json_output", False)
    verbose = params.get("verbose", False)

    # Validate parameters
    if duration <= 0:
        sys.stderr.write("Error: duration must be positive\n")
        sys.exit(2)

    if max_samples is not None and max_samples <= 0:
        sys.stderr.write("Error: samples must be positive\n")
        sys.exit(2)

    # Validate output directory is writable
    import os.path
    output_dir = os.path.dirname(output) or "."
    if not os.path.exists(output_dir):
        sys.stderr.write(f"Error: Output directory does not exist: {output_dir}\n")
        sys.exit(2)
    if not os.access(output_dir, os.W_OK):
        sys.stderr.write(f"Error: Output directory is not writable: {output_dir}\n")
        sys.exit(2)

    if verbose:
        print(f"Capturing oscilloscope data...")
        print(f"  Output file: {output}")
        print(f"  Duration: {duration}s")
        if max_samples:
            print(f"  Max samples: {max_samples}")
    elif not quiet and not json_output:
        print(f"Capturing to {output} ({duration}s)")

    # Start acquisition if not already running
    response = send_command({"command": "IsReady"})

    start_time = time.time()
    all_samples = []
    capture_count = 0

    while True:
        elapsed = time.time() - start_time
        if elapsed >= duration:
            break
        if max_samples and len(all_samples) >= max_samples:
            break

        # Check if data is ready
        response = send_command({"command": "IsReady"})
        is_ready = response.get("is_ready", False)

        if is_ready:
            # Get triggered data
            response = send_command({"command": "GetTriggeredData"})
            if "triggered_data" in response:
                triggered_data = response["triggered_data"]
                samples = triggered_data.get("samples", [])
                sample_interval_ns = triggered_data.get("sample_interval_ns", 1)

                for sample in samples:
                    if max_samples and len(all_samples) >= max_samples:
                        break
                    all_samples.append({
                        "capture": capture_count,
                        "channel": sample.get("channel", "A"),
                        "sample_index": sample.get("sample_index", 0),
                        "voltage": sample.get("voltage", 0.0),
                        "time_ns": sample.get("sample_index", 0) * sample_interval_ns
                    })

                capture_count += 1
                if verbose:
                    print(f"  Captured {len(samples)} samples (total: {len(all_samples)})")

        time.sleep(0.01)  # Small delay between checks

    # Write to CSV
    if all_samples:
        with open(output, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=["capture", "channel", "sample_index", "time_ns", "voltage"])
            writer.writeheader()
            writer.writerows(all_samples)

        if json_output:
            result = {
                "status": "success",
                "total_samples": len(all_samples),
                "captures": capture_count,
                "output": output
            }
            print(json.dumps(result))
        elif not quiet:
            print("Capture complete")
            print(f"Samples: {len(all_samples)}, Output: {output}")
    else:
        if json_output:
            result = {
                "status": "no_data",
                "message": "No data captured. Daemon may not be returning data in expected format."
            }
            print(json.dumps(result))
        elif not quiet:
            print("No data captured.")
            print("Note: The oscilloscope daemon may not be providing data in the expected format.")
            print("Try using the web visualization interface instead:")
            print("  lager scope [NET_NAME] stream web --box [BOX_NAME]")


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

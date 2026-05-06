#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Implementation script for webcam commands.

Runs on the box to start/stop/query webcam streams.
"""

import json
import os
import sys

# Import from lager box libraries
from lager.automation.webcam import start_stream, stop_stream, get_stream_info
from lager.nets.net import Net


def get_command_data():
    """Get command data from environment variable."""
    data_json = os.environ.get("LAGER_COMMAND_DATA", "{}")
    try:
        return json.loads(data_json)
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid command data"}))
        sys.exit(1)


def get_net_video_device(net_name: str) -> str:
    """
    Look up the video device path from the net configuration.

    Args:
        net_name: Name of the camera net

    Returns:
        Video device path (e.g., "/dev/video0")

    Raises:
        ValueError: If net not found or not a camera net
    """
    nets = Net.list_saved()

    # Find the net
    net = None
    for n in nets:
        if n.get("name") == net_name:
            net = n
            break

    if not net:
        raise ValueError(f"Net '{net_name}' not found. Use 'lager nets' to list available nets.")

    # Verify it's a webcam net
    if net.get("role") != "webcam":
        raise ValueError(
            f"Net '{net_name}' is not a webcam net (type: {net.get('role')}). "
            "Only webcam nets can be used with webcam commands."
        )

    # Get the video device from the channel/pin field
    video_device = net.get("pin")
    if not video_device:
        raise ValueError(f"Net '{net_name}' does not have a video device configured.")

    # Ensure it's a full path
    if not video_device.startswith("/dev/"):
        video_device = f"/dev/{video_device}"

    return video_device


def get_all_webcam_nets():
    """
    Get all webcam nets from saved configuration.

    Returns:
        List of webcam net names
    """
    nets = Net.list_saved()
    webcam_nets = [n.get("name") for n in nets if n.get("role") == "webcam"]
    return webcam_nets


def main():
    """Main entry point."""
    data = get_command_data()
    action = data.get("action")
    net_name = data.get("net_name")
    box_ip = data.get("box_ip", "localhost")

    if not action:
        print(json.dumps({"error": "No action specified"}))
        sys.exit(1)

    # For start-all, stop-all, and url-all, net_name is not required
    if action not in ["start-all", "stop-all", "url-all"] and not net_name:
        print(json.dumps({"error": "No net_name specified"}))
        sys.exit(1)

    try:
        if action == "start":
            # Get video device from net config
            video_device = get_net_video_device(net_name)

            # Start the stream
            result = start_stream(net_name, video_device, box_ip)

            # Return URL and status
            print(json.dumps({
                "ok": True,
                "url": result["url"],
                "port": result["port"],
                "already_running": result.get("already_running", False)
            }))

        elif action == "stop":
            # Stop the stream
            success = stop_stream(net_name)

            if success:
                print(json.dumps({"ok": True, "message": "Stream stopped"}))
            else:
                print(json.dumps({"ok": False, "message": "Stream not running"}))

        elif action == "url":
            # Get stream URL
            info = get_stream_info(net_name, box_ip)

            if info:
                print(json.dumps({
                    "ok": True,
                    "url": info["url"],
                    "port": info["port"],
                    "video_device": info["video_device"]
                }))
            else:
                print(json.dumps({
                    "ok": False,
                    "message": f"No active stream for net '{net_name}'"
                }))

        elif action == "start-all":
            # Get all webcam nets
            webcam_nets = get_all_webcam_nets()

            if not webcam_nets:
                print(json.dumps({
                    "ok": True,
                    "message": "No webcam nets found",
                    "started": []
                }))
                return

            results = []
            for net in webcam_nets:
                try:
                    video_device = get_net_video_device(net)
                    result = start_stream(net, video_device, box_ip)
                    results.append({
                        "net": net,
                        "success": True,
                        "url": result["url"],
                        "already_running": result.get("already_running", False)
                    })
                except Exception as e:
                    results.append({
                        "net": net,
                        "success": False,
                        "error": str(e)
                    })

            print(json.dumps({
                "ok": True,
                "message": f"Started {len([r for r in results if r['success']])} webcam streams",
                "results": results
            }))

        elif action == "stop-all":
            # Get all webcam nets
            webcam_nets = get_all_webcam_nets()

            if not webcam_nets:
                print(json.dumps({
                    "ok": True,
                    "message": "No webcam nets found",
                    "stopped": []
                }))
                return

            results = []
            for net in webcam_nets:
                try:
                    success = stop_stream(net)
                    results.append({
                        "net": net,
                        "success": success,
                        "was_running": success
                    })
                except Exception as e:
                    results.append({
                        "net": net,
                        "success": False,
                        "error": str(e)
                    })

            stopped_count = len([r for r in results if r.get("was_running")])
            print(json.dumps({
                "ok": True,
                "message": f"Stopped {stopped_count} webcam streams",
                "results": results
            }))

        elif action == "url-all":
            # Get all webcam nets
            webcam_nets = get_all_webcam_nets()

            if not webcam_nets:
                print(json.dumps({
                    "ok": True,
                    "message": "No webcam nets found",
                    "results": []
                }))
                return

            results = []
            for net in webcam_nets:
                try:
                    info = get_stream_info(net, box_ip)
                    if info:
                        results.append({
                            "net": net,
                            "url": info["url"],
                            "port": info["port"],
                            "video_device": info["video_device"]
                        })
                except Exception as e:
                    # Skip nets that aren't currently streaming
                    pass

            if not results:
                print(json.dumps({
                    "ok": True,
                    "message": "No active webcam streams",
                    "results": []
                }))
            else:
                print(json.dumps({
                    "ok": True,
                    "results": results
                }))

        else:
            print(json.dumps({"error": f"Unknown action: {action}"}))
            sys.exit(1)

    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"Unexpected error: {str(e)}"}))
        sys.exit(1)


if __name__ == "__main__":
    main()

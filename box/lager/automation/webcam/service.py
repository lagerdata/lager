# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
lager.webcam.service

Webcam streaming service using OpenCV and Flask.
Manages multiple concurrent webcam streams with unique ports.
"""

import json
import os
import subprocess
import signal
import time
from pathlib import Path
from typing import Dict, Optional, List

from ...constants import WEBCAM_STREAMS_PATH

# State file path
STATE_FILE = Path(WEBCAM_STREAMS_PATH)
BASE_PORT = 8086  # Changed from 8081 to avoid conflict with oscilloscope UI (8081) and daemon (8082-8085)


class WebcamStreamState:
    """Manages the state of active webcam streams."""

    def __init__(self, state_file: Path = STATE_FILE):
        self.state_file = state_file
        self._ensure_state_file()

    def _ensure_state_file(self):
        """Ensure state file and directory exist."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            self.state_file.write_text(json.dumps({}))

    def load(self) -> Dict:
        """Load stream state from file."""
        try:
            return json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def save(self, state: Dict):
        """Save stream state to file."""
        self.state_file.write_text(json.dumps(state, indent=2))

    def add_stream(self, net_name: str, video_device: str, port: int, pid: int):
        """Add a new active stream."""
        state = self.load()
        state[net_name] = {
            "video_device": video_device,
            "port": port,
            "pid": pid,
            "started_at": time.time(),
            "zoom": 1.0,  # Default zoom level (1.0 = no zoom)
            "focus_mode": "auto",  # Default to autofocus
            "focus_value": 0,  # Manual focus value (when in manual mode)
            "brightness": 128  # Brightness value (0-255)
        }
        self.save(state)

    def remove_stream(self, net_name: str):
        """Remove a stream from state."""
        state = self.load()
        if net_name in state:
            del state[net_name]
            self.save(state)

    def rename_stream(self, old_name: str, new_name: str) -> bool:
        """
        Rename a stream in the state.

        Args:
            old_name: Current stream name
            new_name: New stream name

        Returns:
            True if renamed successfully, False if stream not found
        """
        state = self.load()
        if old_name not in state:
            return False

        # Move the stream data to the new name
        state[new_name] = state[old_name]
        del state[old_name]
        self.save(state)
        return True

    def get_stream(self, net_name: str) -> Optional[Dict]:
        """Get stream info by net name."""
        state = self.load()
        return state.get(net_name)

    def get_all_streams(self) -> Dict:
        """Get all active streams."""
        return self.load()

    def allocate_port(self) -> int:
        """Find next available port starting from BASE_PORT."""
        state = self.load()
        used_ports = {info["port"] for info in state.values()}

        port = BASE_PORT
        while port in used_ports:
            port += 1

        return port

    def get_zoom(self, net_name: str) -> float:
        """Get the zoom level for a stream."""
        stream = self.get_stream(net_name)
        if stream:
            return stream.get("zoom", 1.0)
        return 1.0

    def set_zoom(self, net_name: str, zoom: float) -> bool:
        """
        Set the zoom level for a stream.

        Args:
            net_name: Name of the stream
            zoom: Zoom level (1.0 = no zoom, 2.0 = 2x zoom, etc.)

        Returns:
            True if set successfully, False if stream not found
        """
        state = self.load()
        if net_name in state:
            # Clamp zoom between 1.0 and 4.0
            zoom = max(1.0, min(4.0, zoom))
            state[net_name]["zoom"] = zoom
            self.save(state)
            return True
        return False

    def get_focus(self, net_name: str) -> Dict:
        """Get the focus settings for a stream."""
        stream = self.get_stream(net_name)
        if stream:
            return {
                "mode": stream.get("focus_mode", "auto"),
                "value": stream.get("focus_value", 0)
            }
        return {"mode": "auto", "value": 0}

    def set_focus(self, net_name: str, mode: str, value: int = None) -> bool:
        """
        Set the focus settings for a stream.

        Args:
            net_name: Name of the stream
            mode: Focus mode ("auto" or "manual")
            value: Manual focus value (0-255, only used in manual mode)

        Returns:
            True if set successfully, False if stream not found
        """
        state = self.load()
        if net_name in state:
            state[net_name]["focus_mode"] = mode
            if mode == "manual" and value is not None:
                # Clamp value between 0 and 255
                state[net_name]["focus_value"] = max(0, min(255, value))
            self.save(state)
            return True
        return False

    def get_brightness(self, net_name: str) -> int:
        """Get the brightness value for a stream."""
        stream = self.get_stream(net_name)
        if stream:
            return stream.get("brightness", 128)
        return 128

    def set_brightness(self, net_name: str, value: int) -> bool:
        """
        Set the brightness value for a stream.

        Args:
            net_name: Name of the stream
            value: Brightness value (0-255)

        Returns:
            True if set successfully, False if stream not found
        """
        state = self.load()
        if net_name in state:
            state[net_name]["brightness"] = max(0, min(255, value))
            self.save(state)
            return True
        return False


class WebcamService:
    """Manages webcam streaming using OpenCV and a simple HTTP server."""

    def __init__(self):
        self.state = WebcamStreamState()

    def start_stream(self, net_name: str, video_device: str, box_ip: str) -> Dict:
        """
        Start a webcam stream for a given net.

        Args:
            net_name: Name of the net (e.g., "camera1")
            video_device: Video device path (e.g., "/dev/video0")
            box_ip: IP address of the Box

        Returns:
            Dict with 'url' and 'port' keys

        Raises:
            RuntimeError: If stream is already running or device not found
        """
        # Check if already running for this net name
        existing = self.state.get_stream(net_name)
        if existing:
            pid = existing["pid"]
            if self._is_process_alive(pid):
                return {
                    "url": f"http://{box_ip}:{existing['port']}/",
                    "port": existing["port"],
                    "already_running": True
                }
            else:
                # Process died, clean up
                self.state.remove_stream(net_name)

        # Check if device is already in use by another stream
        all_streams = self.state.get_all_streams()
        for stream_net_name, stream_info in all_streams.items():
            if stream_info["video_device"] == video_device:
                # Check if that stream is still alive
                if self._is_process_alive(stream_info["pid"]):
                    raise RuntimeError(
                        f"Video device {video_device} is already in use by stream '{stream_net_name}' "
                        f"on port {stream_info['port']}. Stop that stream first or use a different device."
                    )
                else:
                    # Dead stream, clean it up
                    self.state.remove_stream(stream_net_name)

        # Validate video device exists
        if not os.path.exists(video_device):
            raise RuntimeError(f"Video device {video_device} not found")

        # Allocate port
        port = self.state.allocate_port()

        # Start streaming process
        pid = self._start_streaming_process(video_device, port, net_name, box_ip)

        # Save state
        self.state.add_stream(net_name, video_device, port, pid)

        # Give server a moment to start
        time.sleep(1)

        return {
            "url": f"http://{box_ip}:{port}/",
            "port": port,
            "already_running": False
        }

    def stop_stream(self, net_name: str) -> bool:
        """
        Stop a webcam stream.

        Args:
            net_name: Name of the net

        Returns:
            True if stopped successfully, False if not running
        """
        stream_info = self.state.get_stream(net_name)
        if not stream_info:
            return False

        pid = stream_info["pid"]
        port = stream_info["port"]

        # Try to kill the process
        try:
            os.kill(pid, signal.SIGTERM)
            # Wait a bit for graceful shutdown
            time.sleep(0.5)
            # Force kill if still alive
            if self._is_process_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Already dead

        # Clean up temporary script file
        script_path = f"/tmp/webcam_stream_{port}.py"
        try:
            os.remove(script_path)
        except FileNotFoundError:
            pass

        # Remove from state
        self.state.remove_stream(net_name)

        return True

    def get_stream_url(self, net_name: str, box_ip: str) -> Optional[str]:
        """
        Get the URL for an active stream.

        Args:
            net_name: Name of the net
            box_ip: IP address of the Box

        Returns:
            URL string or None if not running
        """
        stream_info = self.state.get_stream(net_name)
        if not stream_info:
            return None

        # Verify process is still alive
        if not self._is_process_alive(stream_info["pid"]):
            # Clean up dead stream
            self.state.remove_stream(net_name)
            return None

        return f"http://{box_ip}:{stream_info['port']}/"

    def get_stream_info(self, net_name: str, box_ip: str) -> Optional[Dict]:
        """
        Get stream information including URL, port, and device.

        Args:
            net_name: Name of the net
            box_ip: IP address of the Box

        Returns:
            dict or None: Stream info dict with 'url', 'port', 'video_device' keys, or None if not running
        """
        self.cleanup_dead_streams()

        url = self.get_stream_url(net_name, box_ip)
        if not url:
            return None

        stream_data = self.state.get_stream(net_name)
        return {
            "url": url,
            "port": stream_data["port"],
            "video_device": stream_data["video_device"]
        }

    def _start_streaming_process(self, video_device: str, port: int, net_name: str, box_ip: str) -> int:
        """
        Start the webcam streaming process.
        This runs directly as we're already inside the container.

        Returns:
            PID of the started process
        """
        state_file_path = WEBCAM_STREAMS_PATH
        # Create a simple Python script that streams the webcam
        script = f'''
import cv2
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
import sys
import json
import threading
import time

# Current stream info
CURRENT_NET = "{net_name}"
BOX_IP = "{box_ip}"
STATE_FILE = "{state_file_path}"

# Thread-safe zoom level
zoom_lock = threading.Lock()
current_zoom = 1.0

# Thread-safe focus settings
focus_lock = threading.Lock()
current_focus_mode = "auto"
current_focus_value = 0

# Thread-safe brightness settings
brightness_lock = threading.Lock()
current_brightness = 128

# Thread-safe FPS tracking
fps_lock = threading.Lock()
current_fps = 0.0
frame_count = 0
fps_start_time = None

def load_zoom():
    """Load zoom level from state file."""
    global current_zoom
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            if CURRENT_NET in state:
                with zoom_lock:
                    current_zoom = state[CURRENT_NET].get("zoom", 1.0)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

def save_zoom(zoom_level):
    """Save zoom level to state file."""
    global current_zoom
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        if CURRENT_NET in state:
            # Clamp zoom between 1.0 and 4.0
            zoom_level = max(1.0, min(4.0, zoom_level))
            state[CURRENT_NET]["zoom"] = zoom_level

            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)

            with zoom_lock:
                current_zoom = zoom_level
            return True
    except Exception as e:
        print(f"Error saving zoom: {{e}}", file=sys.stderr)
    return False

def get_zoom():
    """Get current zoom level (thread-safe)."""
    with zoom_lock:
        return current_zoom

def load_focus():
    """Load focus settings from state file."""
    global current_focus_mode, current_focus_value
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            if CURRENT_NET in state:
                with focus_lock:
                    current_focus_mode = state[CURRENT_NET].get("focus_mode", "auto")
                    current_focus_value = state[CURRENT_NET].get("focus_value", 0)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

def save_focus(mode, value=None):
    """Save focus settings to state file."""
    global current_focus_mode, current_focus_value
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        if CURRENT_NET in state:
            state[CURRENT_NET]["focus_mode"] = mode
            if mode == "manual" and value is not None:
                # Clamp value between 0 and 255
                value = max(0, min(255, value))
                state[CURRENT_NET]["focus_value"] = value

            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)

            with focus_lock:
                current_focus_mode = mode
                if mode == "manual" and value is not None:
                    current_focus_value = value
            return True
    except Exception as e:
        print(f"Error saving focus: {{e}}", file=sys.stderr)
    return False

def get_focus():
    """Get current focus settings (thread-safe)."""
    with focus_lock:
        return {{"mode": current_focus_mode, "value": current_focus_value}}

def load_brightness():
    """Load brightness setting from state file."""
    global current_brightness
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
            if CURRENT_NET in state:
                with brightness_lock:
                    current_brightness = state[CURRENT_NET].get("brightness", 128)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

def save_brightness(value):
    """Save brightness setting to state file."""
    global current_brightness
    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        if CURRENT_NET in state:
            value = max(0, min(255, value))
            state[CURRENT_NET]["brightness"] = value

            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)

            with brightness_lock:
                current_brightness = value
            return True
    except Exception as e:
        print(f"Error saving brightness: {{e}}", file=sys.stderr)
    return False

def get_brightness():
    """Get current brightness (thread-safe)."""
    with brightness_lock:
        return current_brightness

def apply_brightness(value):
    """Apply brightness setting to the camera (v4l2-ctl on Linux, no-op on macOS)."""
    if sys.platform == 'darwin':
        return True  # v4l2-ctl not available on macOS; brightness set at capture time via cv2
    import subprocess

    try:
        value = max(0, min(255, value))
        result = subprocess.run(
            ["v4l2-ctl", "-d", "{video_device}", "-c", f"brightness={{value}}"],
            capture_output=True,
            text=True,
            timeout=2
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Error applying brightness: {{e}}", file=sys.stderr)
    return False

def apply_focus(mode, value=None):
    """Apply focus settings to the camera (v4l2-ctl on Linux, no-op on macOS)."""
    if sys.platform == 'darwin':
        return True  # v4l2-ctl not available on macOS
    import subprocess

    try:
        if mode == "auto":
            # Enable autofocus
            result = subprocess.run(
                ["v4l2-ctl", "-d", "{video_device}", "-c", "focus_automatic_continuous=1"],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode != 0:
                # Try alternative autofocus control
                result = subprocess.run(
                    ["v4l2-ctl", "-d", "{video_device}", "-c", "focus_auto=1"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
            return result.returncode == 0
        else:
            # Disable autofocus first
            subprocess.run(
                ["v4l2-ctl", "-d", "{video_device}", "-c", "focus_automatic_continuous=0"],
                capture_output=True,
                text=True,
                timeout=2
            )
            # Try alternative autofocus control
            subprocess.run(
                ["v4l2-ctl", "-d", "{video_device}", "-c", "focus_auto=0"],
                capture_output=True,
                text=True,
                timeout=2
            )

            # Set manual focus value
            if value is not None:
                result = subprocess.run(
                    ["v4l2-ctl", "-d", "{video_device}", "-c", f"focus_absolute={{value}}"],
                    capture_output=True,
                    text=True,
                    timeout=2
                )
                return result.returncode == 0
        return False
    except Exception as e:
        print(f"Error applying focus: {{e}}", file=sys.stderr)
        return False

def update_fps():
    """Update FPS calculation (call this for each frame)."""
    global current_fps, frame_count, fps_start_time

    with fps_lock:
        frame_count += 1
        current_time = time.time()

        if fps_start_time is None:
            fps_start_time = current_time

        elapsed = current_time - fps_start_time

        # Update FPS every 30 frames
        if frame_count >= 30:
            current_fps = frame_count / elapsed
            frame_count = 0
            fps_start_time = current_time

def get_fps():
    """Get current FPS (thread-safe)."""
    with fps_lock:
        return current_fps

# Load initial settings
load_zoom()
load_focus()
load_brightness()

# Apply initial focus and brightness settings
apply_focus(current_focus_mode, current_focus_value)
apply_brightness(current_brightness)

def apply_zoom(frame, zoom_level):
    """
    Apply zoom to frame by cropping center and resizing.

    Args:
        frame: Input frame from camera
        zoom_level: Zoom level (1.0 = no zoom, 2.0 = 2x zoom, etc.)

    Returns:
        Zoomed frame at original resolution
    """
    if zoom_level <= 1.0:
        return frame

    height, width = frame.shape[:2]

    # Calculate crop dimensions (smaller region for higher zoom)
    crop_width = int(width / zoom_level)
    crop_height = int(height / zoom_level)

    # Calculate center crop coordinates
    x1 = (width - crop_width) // 2
    y1 = (height - crop_height) // 2
    x2 = x1 + crop_width
    y2 = y1 + crop_height

    # Crop the center region
    cropped = frame[y1:y2, x1:x2]

    # Resize back to original resolution
    zoomed = cv2.resize(cropped, (width, height), interpolation=cv2.INTER_LINEAR)

    return zoomed

class StreamingHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        """Handle HEAD requests"""
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def do_POST(self):
        """Handle POST requests for zoom control"""
        path_only = self.path.split('?')[0]

        if path_only == "/api/zoom/in":
            # Zoom in by 0.25x
            current = get_zoom()
            new_zoom = min(4.0, current + 0.25)
            success = save_zoom(new_zoom)

            response = {{"success": success, "zoom": get_zoom()}}
            json_response = json.dumps(response)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json_response.encode("utf-8"))

        elif path_only == "/api/zoom/out":
            # Zoom out by 0.25x
            current = get_zoom()
            new_zoom = max(1.0, current - 0.25)
            success = save_zoom(new_zoom)

            response = {{"success": success, "zoom": get_zoom()}}
            json_response = json.dumps(response)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json_response.encode("utf-8"))

        elif path_only == "/api/zoom/reset":
            # Reset zoom to 1.0
            success = save_zoom(1.0)

            response = {{"success": success, "zoom": get_zoom()}}
            json_response = json.dumps(response)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json_response.encode("utf-8"))

        elif path_only == "/api/zoom/set":
            # Set zoom to an explicit value
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{{}}"

            try:
                data = json.loads(body)
                target_zoom = float(data.get("zoom", get_zoom()))
            except Exception:
                target_zoom = get_zoom()

            success = save_zoom(target_zoom)

            response = {{"success": success, "zoom": get_zoom()}}
            json_response = json.dumps(response)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json_response.encode("utf-8"))

        elif path_only == "/api/focus/auto":
            # Enable autofocus
            success = save_focus("auto")
            if success:
                apply_focus("auto")

            focus_settings = get_focus()
            response = {{"success": success, "mode": focus_settings["mode"], "value": focus_settings["value"]}}
            json_response = json.dumps(response)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json_response.encode("utf-8"))

        elif path_only == "/api/focus/manual":
            # Set manual focus
            # Read the request body to get the focus value
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{{}}"

            try:
                data = json.loads(body)
                value = int(data.get("value", 128))  # Default to middle value
            except (json.JSONDecodeError, ValueError, TypeError):
                value = 128

            success = save_focus("manual", value)
            if success:
                apply_focus("manual", value)

            focus_settings = get_focus()
            response = {{"success": success, "mode": focus_settings["mode"], "value": focus_settings["value"]}}
            json_response = json.dumps(response)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json_response.encode("utf-8"))

        elif path_only == "/api/brightness/set":
            # Set brightness value
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{{}}"

            try:
                data = json.loads(body)
                value = int(data.get("value", 128))
            except (json.JSONDecodeError, ValueError, TypeError):
                value = 128

            success = save_brightness(value)
            if success:
                apply_brightness(value)

            response = {{"success": success, "value": get_brightness()}}
            json_response = json.dumps(response)

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json_response.encode("utf-8"))

        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        # Handle query parameters by checking path without query string
        path_only = self.path.split('?')[0]

        if path_only == "/api/zoom":
            # Return current zoom level
            try:
                response_data = {{"zoom": get_zoom()}}
                json_response = json.dumps(response_data)

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(json_response.encode("utf-8"))
            except Exception as e:
                error_response = '{{"error":"' + str(e) + '"}}'
                self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(error_response.encode("utf-8"))

        elif path_only == "/api/fps":
            # Return current FPS
            try:
                response_data = {{"fps": round(get_fps(), 1)}}
                json_response = json.dumps(response_data)

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(json_response.encode("utf-8"))
            except Exception as e:
                error_response = '{{"error":"' + str(e) + '"}}'
                self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(error_response.encode("utf-8"))

        elif path_only == "/api/focus":
            # Return current focus settings
            try:
                focus_settings = get_focus()
                response_data = {{"mode": focus_settings["mode"], "value": focus_settings["value"]}}
                json_response = json.dumps(response_data)

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(json_response.encode("utf-8"))
            except Exception as e:
                error_response = '{{"error":"' + str(e) + '"}}'
                self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(error_response.encode("utf-8"))

        elif path_only == "/api/brightness":
            # Return current brightness value
            try:
                response_data = {{"value": get_brightness()}}
                json_response = json.dumps(response_data)

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(json_response.encode("utf-8"))
            except Exception as e:
                error_response = '{{"error":"' + str(e) + '"}}'
                self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(error_response.encode("utf-8"))

        elif path_only == "/api/streams":
            # Return list of active streams as JSON
            try:
                streams_list = []

                # Try to read state file
                try:
                    with open("{state_file_path}", "r") as f:
                        state = json.load(f)
                        for name, info in state.items():
                            streams_list.append({{
                                "name": name,
                                "port": info.get("port", 8081)
                            }})
                        streams_list.sort(key=lambda x: x["name"])
                except (FileNotFoundError, json.JSONDecodeError, OSError):
                    pass  # Empty list if file doesn't exist or can't be read

                response_data = {{
                    "streams": streams_list,
                    "current": CURRENT_NET,
                    "box_ip": BOX_IP
                }}

                json_response = json.dumps(response_data)

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Content-Length", str(len(json_response)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(json_response.encode("utf-8"))
            except Exception as e:
                # Fallback error response
                error_response = '{{"streams":[],"current":"","box_ip":"","error":"' + str(e) + '"}}'
                self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.send_header("Content-Length", str(len(error_response)))
                self.end_headers()
                self.wfile.write(error_response.encode("utf-8"))

        elif path_only == "/test":
            # Simple test endpoint
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        elif path_only == "/":
            # Serve a simple HTML page with the video stream
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            html = """
<!DOCTYPE html>
<html>
<head>
    <title>Lager Webcam Stream</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            padding: 0;
            padding-bottom: 3rem;
            background-color: #1a1a1a;
            color: #ffffff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            display: flex;
            min-height: 100vh;
            overflow-y: auto;
        }}

        .sidebar {{
            width: 220px;
            background-color: #0a0a0a;
            border-right: 2px solid #ff69b4;
            padding: 1rem;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }}

        .sidebar h2 {{
            font-size: 1.1rem;
            color: #ff69b4;
            margin-bottom: 0.75rem;
            font-weight: 600;
        }}

        .webcam-button {{
            background-color: #2a2a2a;
            color: #ffffff;
            border: 2px solid #3a3a3a;
            border-radius: 6px;
            padding: 0.75rem;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 500;
            transition: all 0.2s ease;
            text-align: left;
            word-wrap: break-word;
            display: block;
            width: 100%;
            margin-bottom: 0.5rem;
        }}

        .webcam-button:hover {{
            background-color: #3a3a3a;
            border-color: #ff69b4;
            transform: translateX(4px);
        }}

        .webcam-button.active {{
            background-color: #ff69b4;
            color: #000000;
            border-color: #ff69b4;
        }}

        .main-content {{
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 1rem;
            padding-bottom: 2rem;
            overflow-y: auto;
        }}

        h1 {{
            font-size: 2rem;
            font-weight: 600;
            color: #ffffff;
            margin-bottom: 0.5rem;
            text-align: center;
            letter-spacing: 0.5px;
        }}

        .stream-info {{
            font-size: 1rem;
            color: #aaaaaa;
            text-align: center;
            margin-bottom: 1.5rem;
            font-weight: 400;
        }}

        .stream-info strong {{
            color: #ff69b4;
            font-weight: 600;
        }}

        .video-container {{
            width: 70vw;
            max-width: 1200px;
            display: flex;
            gap: 1rem;
            justify-content: center;
            align-items: flex-start;
            margin-bottom: 1rem;
            position: relative;
        }}

        .video-wrapper {{
            flex: 1;
            min-width: 0;
            position: relative;
        }}

        .fps-badge {{
            position: absolute;
            top: -2.5rem;
            right: 0;
            background-color: #1a1a1a;
            border: 2px solid #4169e1;
            border-radius: 6px;
            padding: 0.4rem 0.75rem;
            display: flex;
            gap: 0.5rem;
            align-items: center;
            z-index: 10;
        }}

        .fps-badge .fps-label {{
            font-size: 0.7rem;
            color: #888;
            font-weight: 500;
        }}

        .fps-badge #fps-value {{
            font-size: 0.9rem;
            color: #4169e1;
            font-weight: 600;
        }}

        img {{
            width: 100%;
            height: auto;
            border: 3px solid #ff69b4;
            border-radius: 4px;
            box-shadow: 0 0 20px rgba(255, 105, 180, 0.3);
        }}

        .zoom-controls {{
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            padding-top: 0.5rem;
        }}

        .zoom-level {{
            background-color: #1a1a1a;
            border: 2px solid #ff69b4;
            border-radius: 6px;
            padding: 0.5rem;
            text-align: center;
            font-size: 0.85rem;
            color: #ff69b4;
            font-weight: 600;
        }}

        .focus-controls {{
            border-top: 2px solid #3a3a3a;
            padding-top: 1rem;
            margin-top: 1rem;
        }}

        .brightness-controls {{
            border-top: 2px solid #3a3a3a;
            padding-top: 1rem;
            margin-top: 1rem;
        }}

        .focus-mode-button {{
            background-color: #2a2a2a;
            color: #ffffff;
            border: 2px solid #ff69b4;
            border-radius: 6px;
            width: 100%;
            height: 40px;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 600;
            transition: all 0.2s ease;
            margin-bottom: 0.75rem;
        }}

        .focus-mode-button:hover {{
            background-color: #3a3a3a;
            transform: scale(1.02);
        }}

        .focus-mode-button.active {{
            background-color: #ff69b4;
            color: #000000;
        }}

        .focus-slider {{
            width: 100%;
            height: 6px;
            border-radius: 3px;
            background: #3a3a3a;
            outline: none;
            -webkit-appearance: none;
            margin-bottom: 0.5rem;
        }}

        .focus-slider::-webkit-slider-thumb {{
            -webkit-appearance: none;
            appearance: none;
            width: 18px;
            height: 18px;
            border-radius: 50%;
            background: #ff69b4;
            cursor: pointer;
            border: 2px solid #000;
        }}

        .focus-slider::-moz-range-thumb {{
            width: 18px;
            height: 18px;
            border-radius: 50%;
            background: #ff69b4;
            cursor: pointer;
            border: 2px solid #000;
        }}

        .focus-slider:disabled {{
            opacity: 0.3;
            cursor: not-allowed;
        }}

        .focus-slider:disabled::-webkit-slider-thumb {{
            background: #666;
            cursor: not-allowed;
        }}

        .focus-slider:disabled::-moz-range-thumb {{
            background: #666;
            cursor: not-allowed;
        }}

        .focus-value {{
            background-color: #1a1a1a;
            border: 2px solid #ff69b4;
            border-radius: 6px;
            padding: 0.4rem;
            text-align: center;
            font-size: 0.8rem;
            color: #ff69b4;
            font-weight: 600;
        }}

        .focus-label {{
            font-size: 0.75rem;
            color: #888;
            margin-bottom: 0.4rem;
            display: block;
            font-weight: 500;
        }}

        .info {{
            font-size: 0.85rem;
            color: #cccccc;
            text-align: center;
            font-weight: 400;
            letter-spacing: 0.3px;
            margin-bottom: 3rem;
        }}

        .info strong {{
            color: #ffffff;
        }}
    </style>
</head>
<body>
    <div class="sidebar">
        <h2>Webcams</h2>
        <div id="webcam-list"></div>
    </div>
    <div class="main-content">
        <h1>Lager Webcam Stream</h1>
        <p class="stream-info"><strong>{net_name}</strong> on <strong>{box_ip}</strong></p>
        <div class="video-container">
            <div class="video-wrapper">
                <div class="fps-badge">
                    <span class="fps-label">FPS</span>
                    <span id="fps-value">--</span>
                </div>
                <img src="/stream" alt="Webcam Stream">
            </div>
            <div class="zoom-controls">
                <span class="focus-label">ZOOM</span>
                <div class="zoom-level" id="zoom-display">1.0x</div>
                <input type="range" id="zoom-slider" class="focus-slider" min="1.0" max="4.0" step="0.10" value="1.0">
                <div class="focus-controls">
                    <span class="focus-label">FOCUS</span>
                    <div class="focus-value" id="focus-display">Auto</div>
                    <input type="range" id="focus-slider" class="focus-slider" min="0" max="255" value="128">
                    <button id="focus-auto" class="focus-mode-button">Autofocus</button>
                </div>
                <div class="brightness-controls">
                    <span class="focus-label">BRIGHTNESS</span>
                    <div class="focus-value" id="brightness-display">128</div>
                    <input type="range" id="brightness-slider" class="focus-slider" min="0" max="255" value="128">
                </div>
            </div>
        </div>
        <p class="info"><strong>Device:</strong> {video_device} | <strong>Port:</strong> {port}</p>
    </div>

    <script>
        var retryCount = 0;
        var maxRetries = 5;
        var retryDelay = 500; // Start with 500ms

        // Zoom control functionality
        var currentZoom = 1.0;

        function updateZoomDisplay(zoom) {{
            currentZoom = zoom;
            document.getElementById('zoom-display').textContent = zoom.toFixed(2) + 'x';
            document.getElementById('zoom-slider').value = zoom;
        }}

        function fetchZoomLevel() {{
            fetch('/api/zoom')
                .then(function(response) {{ return response.json(); }})
                .then(function(data) {{
                    if (data.zoom !== undefined) {{
                        updateZoomDisplay(data.zoom);
                    }}
                }})
                .catch(function(error) {{
                    console.error('Error fetching zoom level:', error);
                }});
        }}

        var zoomTimeout = null;
        var zoomRequestController = null;

        function handleZoomSliderChange() {{
            var slider = document.getElementById('zoom-slider');
            var value = parseFloat(slider.value);

            // Update display immediately for responsiveness
            document.getElementById('zoom-display').textContent = value.toFixed(2) + 'x';

            // Clear any pending zoom update
            if (zoomTimeout) {{
                clearTimeout(zoomTimeout);
            }}

            // Debounce the API call to avoid too many requests and keep the latest value
            zoomTimeout = setTimeout(function() {{
                // Abort any in-flight request so the latest value wins
                if (zoomRequestController) {{
                    zoomRequestController.abort();
                }}

                zoomRequestController = new AbortController();
                var controller = zoomRequestController;

                fetch('/api/zoom/set', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ zoom: value }}),
                    signal: controller.signal
                }})
                    .then(function(response) {{ return response.json(); }})
                    .then(function(data) {{
                        if (data.success && data.zoom !== undefined) {{
                            currentZoom = data.zoom;
                            updateZoomDisplay(data.zoom);
                        }}
                    }})
                    .catch(function(error) {{
                        if (error.name !== 'AbortError') {{
                            console.error('Error adjusting zoom:', error);
                        }}
                    }})
                    .finally(function() {{
                        if (zoomRequestController === controller) {{
                            zoomRequestController = null;
                        }}
                    }});
            }}, 75); // light debounce to keep updates snappy
        }}

        // Set up zoom slider event listener
        document.getElementById('zoom-slider').addEventListener('input', handleZoomSliderChange);

        // Fetch initial zoom level
        fetchZoomLevel();

        // Periodically update zoom level (in case it changes externally)
        setInterval(fetchZoomLevel, 2000);

        // Focus control functionality
        var currentFocusMode = "auto";
        var currentFocusValue = 0;

        function updateFocusDisplay(mode, value) {{
            currentFocusMode = mode;
            currentFocusValue = value;

            var autoButton = document.getElementById('focus-auto');
            var slider = document.getElementById('focus-slider');
            var display = document.getElementById('focus-display');

            if (mode === "auto") {{
                autoButton.style.display = 'none';  // Hide button in auto mode
                display.textContent = 'Auto';
            }} else {{
                autoButton.style.display = 'block';  // Show button in manual mode
                slider.value = value;
                display.textContent = value;
            }}
        }}

        function fetchFocusSettings() {{
            fetch('/api/focus')
                .then(function(response) {{ return response.json(); }})
                .then(function(data) {{
                    if (data.mode !== undefined) {{
                        updateFocusDisplay(data.mode, data.value || 0);
                    }}
                }})
                .catch(function(error) {{
                    console.error('Error fetching focus settings:', error);
                }});
        }}

        function returnToAutofocus() {{
            // Always switch to auto mode (button only appears in manual mode)
            fetch('/api/focus/auto', {{ method: 'POST' }})
                .then(function(response) {{ return response.json(); }})
                .then(function(data) {{
                    if (data.success) {{
                        updateFocusDisplay(data.mode, data.value);
                    }}
                }})
                .catch(function(error) {{
                    console.error('Error switching to autofocus:', error);
                }});
        }}

        function handleFocusSliderChange() {{
            var slider = document.getElementById('focus-slider');
            var value = parseInt(slider.value);

            // Update display immediately for responsiveness
            document.getElementById('focus-display').textContent = value;

            // Automatically switch to manual mode and send to server
            fetch('/api/focus/manual', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ value: value }})
            }})
                .then(function(response) {{ return response.json(); }})
                .then(function(data) {{
                    if (data.success) {{
                        currentFocusMode = data.mode;
                        currentFocusValue = data.value;
                        // Show the "Return to Autofocus" button
                        document.getElementById('focus-auto').style.display = 'block';
                    }}
                }})
                .catch(function(error) {{
                    console.error('Error adjusting focus:', error);
                }});
        }}

        // Set up focus control event listeners
        document.getElementById('focus-auto').addEventListener('click', returnToAutofocus);
        document.getElementById('focus-slider').addEventListener('input', handleFocusSliderChange);

        // Fetch initial focus settings
        fetchFocusSettings();

        // Periodically update focus settings (in case they change externally)
        setInterval(fetchFocusSettings, 2000);

        // Brightness control functionality
        var currentBrightness = 128;

        function updateBrightnessDisplay(value) {{
            currentBrightness = value;
            document.getElementById('brightness-display').textContent = value;
            document.getElementById('brightness-slider').value = value;
        }}

        function fetchBrightnessSettings() {{
            fetch('/api/brightness')
                .then(function(response) {{ return response.json(); }})
                .then(function(data) {{
                    if (data.value !== undefined) {{
                        updateBrightnessDisplay(data.value);
                    }}
                }})
                .catch(function(error) {{
                    console.error('Error fetching brightness:', error);
                }});
        }}

        var brightnessTimeout = null;
        var brightnessRequestController = null;

        function handleBrightnessSliderChange() {{
            var slider = document.getElementById('brightness-slider');
            var value = parseInt(slider.value);

            // Update display immediately for responsiveness
            document.getElementById('brightness-display').textContent = value;

            // Clear any pending brightness update
            if (brightnessTimeout) {{
                clearTimeout(brightnessTimeout);
            }}

            brightnessTimeout = setTimeout(function() {{
                if (brightnessRequestController) {{
                    brightnessRequestController.abort();
                }}

                brightnessRequestController = new AbortController();
                var controller = brightnessRequestController;

                fetch('/api/brightness/set', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ value: value }}),
                    signal: controller.signal
                }})
                    .then(function(response) {{ return response.json(); }})
                    .then(function(data) {{
                        if (data.success && data.value !== undefined) {{
                            currentBrightness = data.value;
                        }}
                    }})
                    .catch(function(error) {{
                        if (error.name !== 'AbortError') {{
                            console.error('Error adjusting brightness:', error);
                        }}
                    }})
                    .finally(function() {{
                        if (brightnessRequestController === controller) {{
                            brightnessRequestController = null;
                        }}
                    }});
            }}, 75);
        }}

        // Set up brightness slider event listener
        document.getElementById('brightness-slider').addEventListener('input', handleBrightnessSliderChange);

        // Fetch initial brightness setting
        fetchBrightnessSettings();

        // Periodically update brightness (in case it changes externally)
        setInterval(fetchBrightnessSettings, 2000);

        // FPS display functionality
        function updateFPSDisplay() {{
            fetch('/api/fps')
                .then(function(response) {{ return response.json(); }})
                .then(function(data) {{
                    if (data.fps !== undefined) {{
                        var fpsValue = document.getElementById('fps-value');
                        if (fpsValue) {{
                            fpsValue.textContent = data.fps.toFixed(1);
                        }}
                    }}
                }})
                .catch(function(error) {{
                    console.error('Error fetching FPS:', error);
                }});
        }}

        // Initial FPS fetch
        updateFPSDisplay();

        // Update FPS display every second
        setInterval(updateFPSDisplay, 1000);

        function populateWebcamList(isRetry) {{
            // Add timestamp to prevent caching
            var timestamp = new Date().getTime();
            var url = '/api/streams?_=' + timestamp;

            fetch(url, {{
                cache: 'no-store',
                headers: {{
                    'Cache-Control': 'no-cache, no-store, must-revalidate',
                    'Pragma': 'no-cache'
                }}
            }})
                .then(function(response) {{
                    if (!response.ok) {{
                        throw new Error('HTTP ' + response.status);
                    }}
                    return response.json();
                }})
                .then(function(data) {{
                    retryCount = 0; // Reset on success
                    var webcamList = document.getElementById('webcam-list');
                    if (!webcamList) return;

                    webcamList.innerHTML = '';

                    var boxIp = data.box_ip || window.location.hostname;
                    var currentNet = data.current || '';

                    if (data.streams && data.streams.length > 0) {{
                        data.streams.forEach(function(stream) {{
                            var button = document.createElement('button');
                            button.className = 'webcam-button';
                            button.textContent = stream.name;

                            if (stream.name === currentNet) {{
                                button.classList.add('active');
                            }}

                            button.onclick = function() {{
                                window.location.href = 'http://' + boxIp + ':' + stream.port + '/';
                            }};

                            webcamList.appendChild(button);
                        }});
                    }} else {{
                        webcamList.innerHTML = '<p style="color: #888; font-size: 0.85rem; padding: 0.5rem;">No active streams</p>';
                    }}
                }})
                .catch(function(error) {{
                    console.error('Error loading webcams (attempt ' + (retryCount + 1) + '):', error);

                    // Retry logic for initial load
                    if (isRetry && retryCount < maxRetries) {{
                        retryCount++;
                        var delay = retryDelay * retryCount;
                        console.log('Retrying in ' + delay + 'ms...');
                        setTimeout(function() {{
                            populateWebcamList(true);
                        }}, delay);
                    }} else {{
                        var webcamList = document.getElementById('webcam-list');
                        if (webcamList) {{
                            webcamList.innerHTML = '<p style="color: #ff6b6b; font-size: 0.85rem; padding: 0.5rem;">Loading...</p>';
                        }}
                    }}
                }});
        }}

        // Initial load with retry
        if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', function() {{
                populateWebcamList(true);
                // Refresh every 3 seconds
                setInterval(function() {{ populateWebcamList(false); }}, 3000);
            }});
        }} else {{
            populateWebcamList(true);
            // Refresh every 3 seconds
            setInterval(function() {{ populateWebcamList(false); }}, 3000);
        }}
    </script>
</body>
</html>
            """
            self.wfile.write(html.encode())

        elif path_only == "/stream" or path_only.startswith("/stream/"):
            # Stream the actual video
            self.send_response(200)
            self.send_header("Content-type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()

            cap = cv2.VideoCapture("{video_device}")
            if not cap.isOpened():
                print("ERROR: Could not open video device {video_device}", file=sys.stderr)
                return

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)

            try:
                while True:
                    ret, frame = cap.read()
                    if not ret:
                        break

                    # Apply zoom if needed
                    zoom = get_zoom()
                    if zoom > 1.0:
                        frame = apply_zoom(frame, zoom)

                    # Update FPS counter
                    update_fps()

                    _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])

                    self.wfile.write(b"--frame\\r\\n")
                    self.wfile.write(b"Content-Type: image/jpeg\\r\\n\\r\\n")
                    self.wfile.write(jpeg.tobytes())
                    self.wfile.write(b"\\r\\n")
            except BrokenPipeError:
                pass
            finally:
                cap.release()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logs

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads."""
    daemon_threads = True
    allow_reuse_address = True

server = ThreadedHTTPServer(("0.0.0.0", {port}), StreamingHandler)
print("Webcam server started on port {port}", file=sys.stderr)
server.serve_forever()
'''

        # Write script to a temporary file
        script_path = f"/tmp/webcam_stream_{port}.py"
        with open(script_path, "w") as f:
            f.write(script)

        # Make script executable
        os.chmod(script_path, 0o755)

        # Start the process in background using nohup to truly detach
        # Redirect output to log file for debugging
        log_path = f"/tmp/webcam_stream_{port}.log"

        # Use shell=True with nohup to ensure process persists
        cmd = f"nohup python3 {script_path} > {log_path} 2>&1 & echo $!"
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise RuntimeError(f"Failed to start webcam stream: {result.stderr}")

        # Get the PID from the echo output
        try:
            pid = int(result.stdout.strip())
        except ValueError:
            # Fallback: try to find the process
            time.sleep(0.5)
            find_result = subprocess.run(
                ["pgrep", "-f", script_path],
                capture_output=True,
                text=True
            )
            if find_result.returncode == 0 and find_result.stdout.strip():
                pid = int(find_result.stdout.strip().split()[0])
            else:
                raise RuntimeError(f"Failed to get PID of webcam stream process")

        return pid

    def _is_process_alive(self, pid: int) -> bool:
        """Check if a process is still running."""
        try:
            os.kill(pid, 0)  # Signal 0 checks if process exists
            return True
        except ProcessLookupError:
            return False

    def cleanup_dead_streams(self):
        """Clean up state for streams whose processes have died."""
        state = self.state.load()
        dead_streams = []

        for net_name, info in state.items():
            if not self._is_process_alive(info["pid"]):
                dead_streams.append(net_name)

        for net_name in dead_streams:
            self.state.remove_stream(net_name)

    def get_active_streams(self) -> List[str]:
        """
        Get list of active stream names.

        Returns:
            List of stream names currently running
        """
        self.cleanup_dead_streams()
        return list(self.state.get_all_streams().keys())


# Convenience functions for use in CLI implementation scripts

def start_stream(net_name: str, video_device: str, box_ip: str) -> Dict:
    """Start a webcam stream."""
    service = WebcamService()
    service.cleanup_dead_streams()
    return service.start_stream(net_name, video_device, box_ip)


def stop_stream(net_name: str) -> bool:
    """Stop a webcam stream."""
    service = WebcamService()
    return service.stop_stream(net_name)


def get_stream_info(net_name: str, box_ip: str) -> Optional[Dict]:
    """Get stream info including URL."""
    service = WebcamService()
    service.cleanup_dead_streams()

    url = service.get_stream_url(net_name, box_ip)
    if not url:
        return None

    stream_data = service.state.get_stream(net_name)
    return {
        "url": url,
        "port": stream_data["port"],
        "video_device": stream_data["video_device"]
    }


def get_active_streams() -> List[str]:
    """Get list of active stream names."""
    service = WebcamService()
    service.cleanup_dead_streams()
    return list(service.state.get_all_streams().keys())


def rename_stream(old_name: str, new_name: str) -> bool:
    """
    Rename a webcam stream.

    Args:
        old_name: Current stream name
        new_name: New stream name

    Returns:
        True if renamed successfully, False if stream not found
    """
    state = WebcamStreamState()
    return state.rename_stream(old_name, new_name)

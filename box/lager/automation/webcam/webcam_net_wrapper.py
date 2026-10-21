# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Webcam Net wrapper class for the lager Python API.

Provides clean access to webcam streaming from Python scripts.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from . import controls as _controls
# Import the webcam service
from .service import WebcamService

# How long snapshot() waits for the streamer's answer. The streamer itself
# waits up to 5 s for a first frame after opening the camera.
SNAPSHOT_TIMEOUT = 10.0


class WebcamNetWrapper:
    """
    Represents a webcam net configuration.

    Provides clean API for controlling webcam streams from Python scripts.

    Example:
        from lager import Net, NetType

        # Get the webcam net
        webcam = Net.get('camera1', NetType.Webcam)

        # Start streaming
        result = webcam.start(box_ip='<BOX_IP>')
        print(f"Stream URL: {result['url']}")

        # Get stream info
        info = webcam.get_info(box_ip='<BOX_IP>')

        # Look at the bench while the stream runs
        image = webcam.frame()          # numpy BGR array, as the camera sees it

        # Stop streaming
        webcam.stop()
    """

    def __init__(self, name: str, net_config: dict):
        """
        Initialize webcam net.

        Args:
            name: Net name
            net_config: Net configuration dict from saved_nets.json
        """
        self.name = name
        self.type = 'Webcam'  # For compatibility with Net API
        self._config = net_config

        # Extract video device path from channel or pin
        device = net_config.get('channel') or net_config.get('pin')
        if isinstance(device, int):
            self.video_device = f'/dev/video{device}'
        else:
            self.video_device = device or '/dev/video0'

        # Create service instance
        self._service = WebcamService()

    def start(self, box_ip: str) -> Dict[str, any]:
        """
        Start webcam stream.

        Args:
            box_ip: Lager Box IP address for URL generation

        Returns:
            dict with keys:
                - url: Full stream URL
                - port: Port number for the stream
                - already_running: Boolean indicating if stream was already active
        """
        return self._service.start_stream(self.name, self.video_device, box_ip,
                                          source='api')

    def stop(self) -> bool:
        """
        Stop webcam stream.

        Returns:
            bool: True if stopped successfully, False if not running
        """
        return self._service.stop_stream(self.name)

    def get_info(self, box_ip: str) -> Optional[Dict[str, any]]:
        """
        Get stream information.

        Args:
            box_ip: Lager Box IP address

        Returns:
            dict or None: Stream info dict or None if not running
        """
        return self._service.get_stream_info(self.name, box_ip)

    def get_url(self, box_ip: str) -> Optional[str]:
        """
        Get stream URL.

        Args:
            box_ip: Lager Box IP address

        Returns:
            str or None: Stream URL or None if not running
        """
        return self._service.get_stream_url(self.name, box_ip)

    def is_active(self) -> bool:
        """
        Check if stream is currently active.

        Returns:
            bool: True if stream is running
        """
        active_streams = self._service.get_active_streams()
        return self.name in active_streams

    # ---------- frames ----------

    def _stream_port(self) -> int:
        stream = self._service.state.get_stream(self.name)
        if not stream or not self._service._is_process_alive(stream["pid"]):
            raise RuntimeError(
                f"Webcam stream '{self.name}' is not running. Start it first "
                f"with start(box_ip=...).")
        return stream["port"]

    def snapshot(self, raw: bool = False) -> bytes:
        """
        One JPEG frame from the running stream.

        Taken from the stream's own capture, so it never competes with the
        stream (or a browser watching it) for the camera.

        Args:
            raw: True for the frame as the camera delivered it; False for the
                picture the viewer shows, after its rotation, flips and zoom.

        Returns:
            bytes: the JPEG

        Raises:
            RuntimeError: if the stream is not running or has no frame yet
        """
        url = f"http://127.0.0.1:{self._stream_port()}/snapshot"
        if raw:
            url += "?raw=1"
        try:
            with urllib.request.urlopen(url, timeout=SNAPSHOT_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            raise RuntimeError(
                f"Webcam stream '{self.name}' has no frame (HTTP {err.code}); "
                f"is the camera delivering?") from err
        except (urllib.error.URLError, OSError) as err:
            raise RuntimeError(f"Webcam stream '{self.name}' did not answer: {err}") from err

    def frame(self, raw: bool = True):
        """
        One frame from the running stream, decoded for analysis.

        Args:
            raw: True (the default) for the frame as the camera delivered it,
                before the viewer's rotation, flips and zoom.

        Returns:
            numpy.ndarray: height x width x 3, BGR, as OpenCV uses
        """
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(self.snapshot(raw=raw), dtype=np.uint8),
                             cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"Webcam stream '{self.name}' sent a frame that does not decode")
        return image

    # ---------- camera controls ----------

    def controls(self) -> Dict[str, Dict[str, Any]]:
        """
        The camera's image controls, each with its value and range.

        Only what this camera has: exposure, gain, white balance, focus,
        brightness, contrast, saturation, hue, gamma, sharpness,
        backlight_compensation, power_line_frequency. A value an auto mode can
        take over carries ``auto`` (True, False, or None when the camera has
        no auto mode for it); power_line_frequency carries ``options``.
        Works whether or not the stream is running.
        """
        return _controls.describe_controls(self.video_device)

    def set_control(self, name: str, value) -> bool:
        """
        Set one camera control, and keep it for this net.

        The value is re-applied each time the stream starts, so it survives
        a stream restart and a camera that forgets its settings when it loses
        power. Works whether or not the stream is running.

        Args:
            name: exposure_auto, exposure, gain, white_balance_auto,
                white_balance_temperature, focus_auto, focus, brightness,
                contrast, saturation, hue, gamma, sharpness,
                backlight_compensation or power_line_frequency
            value: a bool for the *_auto switches, else a number in the
                camera's range (it is clamped to it); power_line_frequency
                takes one of its ``options``

        Returns:
            bool: True if the camera took the value
        """
        if name not in _controls.CONTROL_NAMES:
            raise ValueError(f"unknown control {name!r}; "
                             f"one of: {', '.join(_controls.CONTROL_NAMES)}")
        if name in _controls.SWITCHES:
            value = bool(value)
        parsed = _controls.read_controls(self.video_device)
        ok = _controls.set_control(self.video_device, name, value, controls=parsed)
        if ok:
            self._service.state.save_controls(self.name, _controls.as_saved(parsed, {name: value}))
        return ok

    def lock_controls(self, **overrides) -> Dict[str, Any]:
        """
        Fix every image control, for pictures that compare frame to frame.

        Turns auto exposure, auto white balance and autofocus off, keeps
        every other control at its current value unless overridden, and saves
        the lot for this net so each stream start puts it back. Then reads the
        camera back.

        Example:
            webcam.lock_controls(exposure=312, white_balance_temperature=4600)

        Args:
            **overrides: control name -> value, as set_control takes them

        Returns:
            dict: ``ok`` (True if every control reads back as wanted) and
            ``controls``: {name: {"wanted", "actual"}}. A value outside the
            camera's range is clamped, so it reads back different and ``ok``
            is False; the clamped value is the one saved.
        """
        parsed = _controls.read_controls(self.video_device)
        if not parsed:
            raise RuntimeError(
                f"Could not read the controls of {self.video_device} "
                f"(is the camera plugged in, and v4l2-ctl installed?)")
        wanted = _controls.lock_values(parsed, overrides)
        _controls.apply_controls(self.video_device, wanted)
        self._service.state.save_controls(self.name, _controls.as_saved(parsed, wanted))
        report, ok = _controls.compare(wanted, _controls.read_values(self.video_device))
        return {"ok": ok, "controls": report}

    def get_config(self) -> dict:
        """Get the raw net configuration dict."""
        return self._config.copy()

    def __str__(self):
        return f'<WebcamNetWrapper name="{self.name}" device="{self.video_device}">'

    def __repr__(self):
        return str(self)

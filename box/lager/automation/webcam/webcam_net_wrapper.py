# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Webcam Net wrapper class for the lager Python API.

Provides clean access to webcam streaming from Python scripts.
"""
from __future__ import annotations

from typing import Optional, Dict

# Import the webcam service
from .service import WebcamService


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
        return self._service.start_stream(self.name, self.video_device, box_ip)

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

    def get_config(self) -> dict:
        """Get the raw net configuration dict."""
        return self._config.copy()

    def __str__(self):
        return f'<WebcamNetWrapper name="{self.name}" device="{self.video_device}">'

    def __repr__(self):
        return str(self)

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.webcam

Webcam streaming service for box devices.
"""

from .service import WebcamService, get_active_streams, start_stream, stop_stream, get_stream_info, rename_stream

__all__ = [
    'WebcamService',
    'get_active_streams',
    'start_stream',
    'stop_stream',
    'get_stream_info',
    'rename_stream',
]

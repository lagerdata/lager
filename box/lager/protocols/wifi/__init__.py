# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
WiFi module for box

This module provides WiFi functionality including:
- Router management (Asus router parental control)
- Network scanning and connection
- Status monitoring
"""
from .router import Wifi, toggle_internet_access, set_internet_access
from .connect import connect_to_wifi
from .disconnect import disconnect_wifi
from .scan import scan_wifi
from .status import get_wifi_status

__all__ = [
    # Router management
    'Wifi',
    'toggle_internet_access',
    'set_internet_access',
    # CLI operations
    'connect_to_wifi',
    'disconnect_wifi',
    'scan_wifi',
    'get_wifi_status',
]

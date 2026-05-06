# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Box management CLI commands.

This package contains commands for box connectivity, configuration, and management:
- hello: Test box connectivity and show version
- boxes: Manage box names and IP addresses
- instruments: List attached instruments
- nets: Manage saved nets
- ssh: SSH into boxes

All commands handle box resolution and validation through shared utilities.
"""

from .hello import hello
from .boxes import boxes
from .instruments import instruments
from .nets import nets
from .ssh import ssh
from .box_group import box

__all__ = [
    "hello",
    "boxes",
    "instruments",
    "nets",
    "ssh",
    "box",
]

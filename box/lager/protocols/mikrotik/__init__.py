# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MikroTik RouterOS module for box

Provides control of MikroTik routers via the RouterOS REST API.
"""
from .router import MikroTikRouter

__all__ = ['MikroTikRouter']

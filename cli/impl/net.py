#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Net management implementation for direct execution in container.
This script runs directly in the box container and imports the net module.
"""
import sys
import os

# Add the box python path to sys.path so we can import lager modules
sys.path.insert(0, '/app/lager')

# Now import and run the net module's CLI
from lager.nets.net_cli import _cli

if __name__ == "__main__":
    _cli()
#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Box config CLI shim shipped into the container by run_python_internal.
Mirrors cli/impl/net.py.
"""
import sys

sys.path.insert(0, '/app/lager')

from lager.box_config.box_config_cli import _cli

if __name__ == "__main__":
    _cli()

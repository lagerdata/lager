# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Lager Terminal - Interactive CLI for lager commands

This module provides an interactive terminal/REPL for running lager commands
with tab completion, command history, and a pleasant UI.
"""

from .main import run_terminal

__all__ = ["run_terminal"]

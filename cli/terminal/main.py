# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Lager Terminal - Interactive CLI for lager commands.

Main entry point for the terminal functionality.
"""
from cli import __version__
from .ui.repl import LagerREPL


def run_terminal():
    """
    Start the interactive Lager Terminal.

    This launches the REPL with tab completion, command history,
    and a pleasant user interface.
    """
    repl = LagerREPL()
    repl.run()


if __name__ == "__main__":
    run_terminal()

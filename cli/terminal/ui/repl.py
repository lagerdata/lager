# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Main REPL (Read-Eval-Print Loop) for the Lager Terminal.
"""
import os
import re
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from rich.console import Console

from .completer import LagerCompleter
from .themes import PROMPT_STYLE
from .display import (
    display_welcome,
    display_command_result,
    display_help,
    display_goodbye,
)
from ..core.executor import execute_command
from cli import __version__


console = Console()


# Commands that require an interactive terminal and shouldn't run inside Lager Terminal
INTERACTIVE_COMMANDS = [
    r'\btui\b',           # Any command with 'tui' subcommand (supply tui, battery tui, etc.)
    r'^uart\s+--box\b',   # uart --box (interactive UART session)
    r'^uart\s+\w+\s*$',   # uart <net> (interactive UART session)
]


def is_interactive_command(command: str) -> bool:
    """
    Check if a command would start an interactive TUI or session.

    These commands don't work well inside Lager Terminal because they
    need direct terminal control.

    Args:
        command: The command to check

    Returns:
        True if the command is interactive, False otherwise
    """
    cmd_lower = command.lower().strip()
    for pattern in INTERACTIVE_COMMANDS:
        if re.search(pattern, cmd_lower):
            return True
    return False


class LagerREPL:
    """Interactive REPL for lager commands."""

    def __init__(self):
        """Initialize the REPL with history and completion."""
        self.history_file = Path.home() / ".lager_terminal_history"
        self.session = PromptSession(
            history=FileHistory(str(self.history_file)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=LagerCompleter(),
            style=PROMPT_STYLE,
            complete_while_typing=True,
            enable_history_search=True,
        )

    def run(self) -> None:
        """
        Main REPL loop.

        This method displays the welcome message and then enters the main
        loop, processing user input until exit is requested.
        """
        # Display welcome screen
        display_welcome(__version__, os.getcwd())

        while True:
            try:
                # Get input with arrow prompt
                user_input = self.session.prompt("❯ ")

                # Skip empty input
                if not user_input.strip():
                    continue

                # Process the input
                self._process_input(user_input.strip())

            except KeyboardInterrupt:
                # Handle Ctrl+C gracefully
                console.print("\n[dim](Use 'exit' to quit)[/dim]")
                continue
            except EOFError:
                # Handle Ctrl+D
                console.print()
                display_goodbye()
                break

    def _process_input(self, user_input: str) -> bool:
        """
        Process user input and execute commands.

        Args:
            user_input: The raw user input string

        Returns:
            True to continue the REPL, False to exit
        """
        cmd = user_input.lower()

        # Handle exit commands
        if cmd in ("exit", "quit"):
            display_goodbye()
            raise SystemExit(0)

        # Handle help commands
        if cmd in ("help", "?"):
            display_help()
            return True

        # Handle clear command
        if cmd == "clear":
            os.system("clear" if os.name != "nt" else "cls")
            return True

        # Block interactive commands that don't work inside Lager Terminal
        if is_interactive_command(user_input):
            console.print(
                "[yellow]Warning:[/yellow] This command opens an interactive TUI/session "
                "that doesn't work inside Lager Terminal."
            )
            console.print(
                "[dim]Run this command directly from your shell instead:[/dim]"
            )
            # Show the full command they should run
            if not user_input.lower().startswith("lager "):
                console.print(f"  [cyan]lager {user_input}[/cyan]")
            else:
                console.print(f"  [cyan]{user_input}[/cyan]")
            return True

        # Execute lager command with spinner
        with console.status("[dim]Running...[/dim]", spinner="dots"):
            result = execute_command(user_input)

        # Display the result
        display_command_result(
            command=result.command,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )

        return True

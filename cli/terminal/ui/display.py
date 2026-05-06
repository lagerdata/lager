# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Display functions for the Lager Terminal
"""
import os
import shutil
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table

from .logo import LAGER_LOGO, LAGER_LOGO_COMPACT
from .themes import PRIMARY, ERROR, WARNING, DIM, SUCCESS_INDICATOR, ERROR_INDICATOR

console = Console()


def get_terminal_width() -> int:
    """Get the current terminal width."""
    return shutil.get_terminal_size().columns


def display_welcome(version: str, cwd: str) -> None:
    """
    Show logo, version, working directory, and hints.

    Args:
        version: The current version string
        cwd: Current working directory
    """
    # Use compact logo if terminal is narrow
    term_width = get_terminal_width()
    logo = LAGER_LOGO if term_width >= 50 else LAGER_LOGO_COMPACT

    # Print the logo in pink
    console.print(logo, style="magenta bold")

    # Print version and subtitle centered
    subtitle = f"v{version} · Lager Command Terminal"
    console.print(f"      {subtitle}", style="dim")

    # Print working directory in green
    # Shorten home directory to ~
    home = os.path.expanduser("~")
    display_cwd = cwd.replace(home, "~") if cwd.startswith(home) else cwd
    console.print(f"      {display_cwd}", style="green")

    # Print status and hints
    console.print()
    console.print(" Lager command helper - run commands easily", style="dim")
    console.print("   ? for help · ↑↓ history · Tab complete", style="dim")
    console.print()


def display_command_result(
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration_ms: float
) -> None:
    """
    Show command output with exit code indicator.

    Args:
        command: The command that was executed
        exit_code: The exit code from the command
        stdout: Standard output from the command
        stderr: Standard error from the command
        duration_ms: Execution duration in milliseconds
    """
    # Determine success/failure indicator
    if exit_code == 0:
        indicator = SUCCESS_INDICATOR
        cmd_style = "green"
    else:
        indicator = ERROR_INDICATOR
        cmd_style = "red"

    # Format duration
    if duration_ms < 1000:
        duration_str = f"{duration_ms:.0f}ms"
    else:
        duration_str = f"{duration_ms/1000:.2f}s"

    # Print command with indicator
    console.print(f"{indicator} lager {command} [dim]({duration_str})[/dim]")

    # Print stdout if present
    if stdout and stdout.strip():
        console.print(stdout.rstrip())

    # Print stderr if present (in red/warning style)
    if stderr and stderr.strip():
        # Determine if it's an error or warning
        stderr_lower = stderr.lower()
        if "error" in stderr_lower or exit_code != 0:
            console.print(stderr.rstrip(), style="red")
        elif "warning" in stderr_lower:
            console.print(stderr.rstrip(), style="yellow")
        else:
            # Some commands output info to stderr
            console.print(stderr.rstrip(), style="dim")

    console.print()


def display_help() -> None:
    """Show available commands and usage."""
    console.print()
    console.print("[bold green]Lager Terminal Help[/bold green]")
    console.print()

    # Built-in commands table
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("Command", style="cyan")
    table.add_column("Description")

    builtin_commands = [
        ("exit, quit", "Exit the terminal"),
        ("help, ?", "Show this help message"),
        ("clear", "Clear the screen"),
        ("Ctrl+C", "Cancel current input (use 'exit' to quit)"),
        ("↑/↓", "Navigate command history"),
        ("Tab", "Auto-complete commands and flags"),
    ]

    for cmd, desc in builtin_commands:
        table.add_row(cmd, desc)

    console.print("[bold]Built-in Commands:[/bold]")
    console.print(table)
    console.print()

    # Lager command categories
    console.print("[bold]Lager Command Categories:[/bold]")
    console.print()

    categories = [
        ("Power", "supply, battery, solar, eload", "Power equipment control"),
        ("Measurement", "adc, dac, gpi, gpo, scope, logic, thermocouple, watt", "Measurements and I/O"),
        ("Communication", "uart, ble, wifi, usb", "Communication protocols"),
        ("Development", "debug, arm, python, devenv", "Embedded development"),
        ("Box", "hello, status, boxes, instruments, nets, ssh", "Box management"),
        ("Utility", "defaults, binaries, update, pip, logs, webcam, exec", "Utilities"),
    ]

    cat_table = Table(show_header=True, header_style="bold", box=None)
    cat_table.add_column("Category", style="cyan")
    cat_table.add_column("Commands", style="green")
    cat_table.add_column("Description")

    for cat, cmds, desc in categories:
        cat_table.add_row(cat, cmds, desc)

    console.print(cat_table)
    console.print()

    # Usage examples
    console.print("[bold]Usage Examples:[/bold]")
    console.print()
    examples = [
        "hello --box my-box          # Test connection to a box",
        "supply voltage 3.3 --yes    # Set supply voltage to 3.3V",
        "adc read VCC                # Read ADC value from net 'VCC'",
        "uart --baudrate 115200      # Connect to UART at 115200 baud",
        "debug flash --hexfile fw.hex  # Flash firmware",
    ]

    for example in examples:
        console.print(f"  [dim]❯[/dim] {example}")

    console.print()
    console.print("[dim]Tip: Commands are automatically prefixed with 'lager'[/dim]")
    console.print("[dim]     Type 'supply voltage 3.3' instead of 'lager supply voltage 3.3'[/dim]")
    console.print()


def display_error(message: str) -> None:
    """Display an error message."""
    console.print(f"[red]Error:[/red] {message}")


def display_warning(message: str) -> None:
    """Display a warning message."""
    console.print(f"[yellow]Warning:[/yellow] {message}")


def display_info(message: str) -> None:
    """Display an info message."""
    console.print(f"[blue]Info:[/blue] {message}")


def display_goodbye() -> None:
    """Display exit message."""
    console.print("[green]Goodbye![/green]")

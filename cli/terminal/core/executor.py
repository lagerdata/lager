# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Command execution for the Lager Terminal.
"""
import subprocess
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CommandResult:
    """Result of a command execution."""
    command: str
    full_command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: float


def execute_command(
    command: str,
    timeout: Optional[float] = None,
) -> CommandResult:
    """
    Execute a lager command.

    Args:
        command: The command to execute (with or without 'lager' prefix)
        timeout: Optional timeout in seconds

    Returns:
        CommandResult with execution details
    """
    # Prepend 'lager' if not present
    if not command.startswith("lager ") and command != "lager":
        full_command = f"lager {command}"
    else:
        full_command = command

    # Execute the command
    start_time = time.time()

    try:
        result = subprocess.run(
            full_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired:
        exit_code = 124  # Standard timeout exit code
        stdout = ""
        stderr = f"Command timed out after {timeout} seconds"
    except Exception as e:
        exit_code = 1
        stdout = ""
        stderr = str(e)

    duration_ms = (time.time() - start_time) * 1000

    return CommandResult(
        command=command,
        full_command=full_command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
    )


def is_builtin_command(command: str) -> bool:
    """
    Check if a command is a built-in terminal command.

    Args:
        command: The command to check

    Returns:
        True if it's a built-in command, False otherwise
    """
    builtin_commands = {"exit", "quit", "help", "?", "clear"}
    cmd_lower = command.strip().lower()
    return cmd_lower in builtin_commands

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tab completion for lager commands
"""
from prompt_toolkit.completion import Completer, Completion
from typing import Iterable

# Comprehensive list of lager commands with subcommands and flags
LAGER_COMMANDS = {
    # Power commands
    "supply": {
        "subcommands": ["voltage", "current", "enable", "disable", "status", "measure", "on", "off"],
        "flags": ["--box", "--yes", "-y", "--no-confirm", "--timeout", "--verbose", "--channel"],
        "description": "Power supply control",
    },
    "battery": {
        "subcommands": ["voltage", "current", "soc", "status", "enable", "disable", "on", "off", "resistance"],
        "flags": ["--box", "--yes", "-y", "--no-confirm", "--timeout", "--verbose"],
        "description": "Battery emulator control",
    },
    "solar": {
        "subcommands": ["voltage", "current", "irradiance", "enable", "disable", "status", "on", "off"],
        "flags": ["--box", "--yes", "-y", "--no-confirm", "--timeout", "--verbose"],
        "description": "Solar panel simulator control",
    },
    "eload": {
        "subcommands": ["current", "enable", "disable", "status", "on", "off", "mode"],
        "flags": ["--box", "--yes", "-y", "--no-confirm", "--timeout", "--verbose"],
        "description": "Electronic load control",
    },

    # Measurement commands
    "adc": {
        "subcommands": ["read", "stream", "config"],
        "flags": ["--box", "--timeout", "--verbose", "--samples", "--rate"],
        "description": "ADC measurement",
    },
    "dac": {
        "subcommands": ["set", "read", "config"],
        "flags": ["--box", "--timeout", "--verbose"],
        "description": "DAC output control",
    },
    "gpi": {
        "subcommands": ["read", "config"],
        "flags": ["--box", "--timeout", "--verbose"],
        "description": "GPIO input reading",
    },
    "gpo": {
        "subcommands": ["set", "read", "config", "high", "low", "toggle"],
        "flags": ["--box", "--timeout", "--verbose"],
        "description": "GPIO output control",
    },
    "scope": {
        "subcommands": ["capture", "stream", "measure", "trigger", "config", "screenshot"],
        "flags": ["--box", "--timeout", "--verbose", "--channel", "--timebase", "--voltage-scale"],
        "description": "Oscilloscope control",
    },
    "logic": {
        "subcommands": ["capture", "trigger", "config"],
        "flags": ["--box", "--timeout", "--verbose", "--channels", "--rate"],
        "description": "Logic analyzer",
    },
    "thermocouple": {
        "subcommands": ["read", "stream", "config"],
        "flags": ["--box", "--timeout", "--verbose", "--type"],
        "description": "Temperature measurement",
    },
    "watt": {
        "subcommands": ["read", "stream", "config"],
        "flags": ["--box", "--timeout", "--verbose"],
        "description": "Power measurement",
    },

    # Communication commands
    "uart": {
        "subcommands": ["send", "receive", "monitor", "config"],
        "flags": ["--box", "--baudrate", "-b", "--timeout", "--verbose", "--parity", "--stopbits"],
        "description": "UART communication",
    },
    "ble": {
        "subcommands": ["scan", "connect", "disconnect", "read", "write", "notify"],
        "flags": ["--box", "--timeout", "--verbose", "--address", "--service", "--characteristic"],
        "description": "Bluetooth LE communication",
    },
    "wifi": {
        "subcommands": ["scan", "connect", "disconnect", "status", "config"],
        "flags": ["--box", "--timeout", "--verbose", "--ssid", "--password"],
        "description": "WiFi control",
    },
    "usb": {
        "subcommands": ["list", "info", "enable", "disable", "reset", "power"],
        "flags": ["--box", "--timeout", "--verbose", "--port"],
        "description": "USB hub control",
    },

    # Development commands
    "debug": {
        "subcommands": ["flash", "reset", "halt", "resume", "connect", "disconnect", "gdb", "read", "write"],
        "flags": ["--box", "--net", "-n", "--timeout", "--verbose", "--hexfile", "--elffile", "--probe", "--target"],
        "description": "Debug interface control",
    },
    "arm": {
        "subcommands": ["position", "move", "home", "calibrate", "grip", "release"],
        "flags": ["--box", "--timeout", "--verbose", "--x", "--y", "--z", "--speed"],
        "description": "Robotic arm control",
    },
    "python": {
        "subcommands": [],
        "flags": ["--box", "--timeout", "--verbose", "--script"],
        "description": "Python execution on box",
    },
    "devenv": {
        "subcommands": ["start", "stop", "status", "logs"],
        "flags": ["--box", "--timeout", "--verbose"],
        "description": "Development environment",
    },

    # Box management commands
    "hello": {
        "subcommands": [],
        "flags": ["--box", "--timeout", "--verbose"],
        "description": "Test box connection",
    },
    "boxes": {
        "subcommands": ["list", "add", "remove", "info"],
        "flags": ["--name", "--ip", "--verbose"],
        "description": "Manage connected boxes",
    },
    "instruments": {
        "subcommands": ["list", "info", "scan"],
        "flags": ["--box", "--timeout", "--verbose", "--json"],
        "description": "List available instruments",
    },
    "nets": {
        "subcommands": ["list", "create", "delete", "info", "show"],
        "flags": ["--box", "--timeout", "--verbose", "--json", "--type"],
        "description": "Manage net configurations",
    },
    "ssh": {
        "subcommands": [],
        "flags": ["--box", "--user", "--command"],
        "description": "SSH into box",
    },

    # Utility commands
    "defaults": {
        "subcommands": ["show", "set", "clear", "list"],
        "flags": ["--box", "--verbose"],
        "description": "Manage default settings",
    },
    "binaries": {
        "subcommands": ["list", "upload", "download", "delete"],
        "flags": ["--box", "--verbose"],
        "description": "Manage binary files",
    },
    "update": {
        "subcommands": [],
        "flags": ["--box", "--force", "--verbose"],
        "description": "Update box software",
    },
    "pip": {
        "subcommands": ["install", "uninstall", "list", "freeze"],
        "flags": ["--box", "--verbose"],
        "description": "Manage Python packages on box",
    },
    "logs": {
        "subcommands": ["show", "clear", "tail", "follow"],
        "flags": ["--box", "--verbose", "--lines", "-n"],
        "description": "View box logs",
    },
    "webcam": {
        "subcommands": ["capture", "stream", "list"],
        "flags": ["--box", "--timeout", "--verbose", "--device", "--output"],
        "description": "Webcam control",
    },
    "exec": {
        "subcommands": [],
        "flags": ["--box", "--timeout", "--verbose"],
        "description": "Execute raw command on box",
    },
}

# Common flags that apply to most commands
COMMON_FLAGS = ["--box", "--help", "-h", "--verbose", "-v", "--timeout", "--yes", "-y", "--no-confirm"]


class LagerCompleter(Completer):
    """Tab completion for lager commands."""

    def get_completions(self, document, complete_event) -> Iterable[Completion]:
        text = document.text_before_cursor
        words = text.split()

        # Handle empty input or first word
        if len(words) == 0 or (len(words) == 1 and not text.endswith(' ')):
            word = words[0] if words else ''
            # Remove 'lager ' prefix if present
            word = word.replace('lager ', '').lstrip()

            # Complete command names
            for cmd in sorted(LAGER_COMMANDS.keys()):
                if cmd.startswith(word):
                    desc = LAGER_COMMANDS[cmd].get("description", "")
                    yield Completion(
                        cmd,
                        start_position=-len(word),
                        display_meta=desc,
                    )
            return

        # Get the command (first word after optional 'lager')
        first_word = words[0]
        if first_word == "lager" and len(words) > 1:
            cmd = words[1]
            words = words[1:]  # Shift words
        else:
            cmd = first_word

        # Check if command exists
        if cmd not in LAGER_COMMANDS:
            return

        cmd_info = LAGER_COMMANDS[cmd]
        current_word = words[-1] if not text.endswith(' ') else ''

        # Check if we're completing a flag value (previous word was a flag expecting value)
        if len(words) >= 2:
            prev_word = words[-2] if text.endswith(' ') else (words[-2] if len(words) > 1 else '')
            if prev_word in ["--box", "-b"]:
                # Could add box name completion here if we had access to config
                return
            if prev_word in ["--baudrate"]:
                for rate in ["9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"]:
                    if rate.startswith(current_word):
                        yield Completion(rate, start_position=-len(current_word))
                return

        # Complete subcommands (only as second word after command)
        if len(words) == 1 or (len(words) == 2 and not text.endswith(' ')):
            subcommands = cmd_info.get("subcommands", [])
            for sub in subcommands:
                if sub.startswith(current_word):
                    yield Completion(sub, start_position=-len(current_word))

        # Complete flags (can appear anywhere after command)
        if current_word.startswith('-') or text.endswith(' '):
            flags = cmd_info.get("flags", []) + COMMON_FLAGS
            # Don't suggest flags already used
            used_flags = set(w for w in words if w.startswith('-'))

            for flag in flags:
                # Skip if flag already used (unless it's a repeatable one)
                if flag in used_flags:
                    continue
                if flag.startswith(current_word):
                    yield Completion(flag, start_position=-len(current_word))

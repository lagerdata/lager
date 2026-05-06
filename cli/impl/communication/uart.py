#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
UART monitoring implementation script.
This script runs on the box and uses the UART dispatcher to interact with hardware.
"""
import os
import sys
import json
import importlib

_RED = "\033[31m"
_RESET = "\033[0m"


def die(message: str, code: int = 0) -> None:
    """Print an error message and exit with the given code."""
    if code == 0:
        print(message)
    else:
        print(f"{_RED}{message}{_RESET}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    cmd_data = os.environ.get("LAGER_COMMAND_DATA")
    if not cmd_data:
        die("ERROR [usage] No command data provided to UART backend", code=64)

    try:
        command = json.loads(cmd_data)
    except Exception as exc:
        die(f"ERROR [usage] Could not parse command data: {exc}", code=64)

    action = command.get("action")
    params = command.get("params", {}) or {}
    net_name = params.pop("netname", None)
    overrides = params.pop("overrides", {})

    if not action or not net_name:
        die("ERROR [usage] Missing action or net name", code=64)

    try:
        dispatcher = importlib.import_module("lager.uart.dispatcher")

        func = getattr(dispatcher, action, None)
        if func is None:
            die(f"ERROR [unexpected] Unknown UART command: {action}", code=1)

        func(net_name, overrides=overrides, **params)

    except SystemExit:
        raise  # let deliberate exits bubble
    except Exception as exc:
        die(f"ERROR [unexpected] {exc}", code=1)


if __name__ == "__main__":
    main()

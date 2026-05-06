#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
SPI implementation script.
This script runs on the box and uses the SPI dispatcher to interact with hardware.
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
        die("ERROR [usage] No command data provided to SPI backend", code=64)

    try:
        command = json.loads(cmd_data)
    except Exception as exc:
        die(f"ERROR [usage] Could not parse command data: {exc}", code=64)

    action = command.get("action")
    params = command.get("params", {}) or {}
    net_name = params.pop("netname", None)
    overrides = params.pop("overrides", None) or {}

    if not action:
        die("ERROR [usage] Missing action", code=64)

    if not net_name:
        die("ERROR [usage] Missing net name", code=64)

    try:
        # Import the SPI dispatcher from lager.protocols.spi
        dispatcher = importlib.import_module("lager.protocols.spi.dispatcher")

        func = getattr(dispatcher, action, None)
        if func is None:
            die(f"ERROR [unexpected] Unknown SPI command: {action}", code=1)

        # Call the dispatcher function with netname and parameters
        func(net_name, overrides=overrides, **params)

    except SystemExit:
        raise  # let deliberate exits bubble
    except Exception as exc:
        die(f"ERROR [unexpected] {exc}", code=1)


if __name__ == "__main__":
    main()

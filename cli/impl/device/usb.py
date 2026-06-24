# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import sys
import importlib

from lager import (
    USBBackendError,
    LibraryMissingError,
    DeviceNotFoundError,
    PortStateError,
)

EXIT_USAGE_ERROR = 64  # EX_USAGE from <sysexits.h>

import sys

_RED = "\033[31m"
_RESET = "\033[0m"

def die(message: str, code: int = 0) -> None:
    """
    Print *message* and exit with *code*.
    """
    if code == 0:
        print(message)
    else:
        print(f"{_RED}{message}{_RESET}", file=sys.stderr)
    sys.exit(code)

def _call(fn, net_name, settle):
    """Invoke a dispatcher fn with optional settle, tolerating older boxes.

    A box that predates the settle support exposes ``fn(net_name)`` only, so
    fall back to the no-settle call rather than crashing with a TypeError.
    """
    if settle is None:
        return fn(net_name)
    try:
        return fn(net_name, settle=settle)
    except TypeError:
        return fn(net_name)


def main(argv: list[str]) -> None:  # pragma: no cover
    if len(argv) not in (3, 4):
        die("Usage: usb.py <enable|disable|toggle> <net_name> [settle_seconds]")

    command, net_name = argv[1], argv[2]
    settle = None
    if len(argv) == 4:
        try:
            settle = float(argv[3])
        except ValueError:
            settle = None

    try:
        usb_pkg = importlib.import_module("lager.automation.usb_hub")

        if command == "enable":
            _call(usb_pkg.enable, net_name, settle)
        elif command == "disable":
            _call(usb_pkg.disable, net_name, settle)
        elif command == "toggle":
            _call(usb_pkg.toggle, net_name, settle)
        else:
            die(f"ERROR [unexpected] Unknown USB command: {command}", code=1)

    except LibraryMissingError as exc:
        die(f"ERROR [library-missing] {exc}", code=2)
    except DeviceNotFoundError as exc:
        die(f"ERROR [device-not-found] {exc}", code=3)
    except PortStateError as exc:
        die(f"ERROR [port-state] {exc}", code=4)
    except USBBackendError as exc:
        die(f"ERROR [backend] {exc}", code=5)
    except Exception as exc:
        die(f"ERROR [unexpected] {exc}", code=1)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv)

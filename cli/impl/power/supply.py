# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import json
import importlib
from lager.power.supply.supply_net import LibraryMissingError, DeviceNotFoundError, SupplyBackendError

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
        die("ERROR [usage] No command data provided to supply backend", code=64)

    try:
        command = json.loads(cmd_data)
    except Exception as exc:
        die(f"ERROR [usage] Could not parse command data: {exc}", code=64)

    action = command.get("action")
    params = command.get("params", {}) or {}
    net_name = params.pop("netname", None)

    if not action or not net_name:
        die("ERROR [usage] Missing action or net name", code=64)

    try:
        dispatcher = importlib.import_module("lager.power.supply.dispatcher")

        func = getattr(dispatcher, action, None)
        if func is None:
            die(f"ERROR [unexpected] Unknown supply command: {action}", code=1)

        func(net_name, **params)

    except LibraryMissingError as exc:
        die(f"ERROR [library-missing] {exc}", code=2)
    except DeviceNotFoundError as exc:
        # 0.20.0+: map raw libusb/pyvisa errnos (16/19/110) into actionable
        # messages instead of dumping the raw "[Errno 16] Resource busy"
        # string. See cli/impl/power/battery.py for the helper definition.
        _die_mapped_or_raw(exc, fallback_prefix="ERROR [device-not-found]", code=3)
    except SupplyBackendError as exc:
        _die_mapped_or_raw(exc, fallback_prefix="ERROR [backend]", code=5)
    except SystemExit:
        raise  # let deliberate exits bubble
    except Exception as exc:
        _die_mapped_or_raw(exc, fallback_prefix="ERROR [unexpected]", code=1)


def _die_mapped_or_raw(exc, fallback_prefix, code):
    """See cli/impl/power/battery._die_mapped_or_raw for rationale."""
    try:
        from cli.context.error_handlers import format_system_error_for_user
        mapped = format_system_error_for_user(str(exc))
    except Exception:
        mapped = None
    if mapped:
        die(mapped, code=code)
    else:
        die(f"{fallback_prefix} {exc}", code=code)


if __name__ == "__main__":
    main()
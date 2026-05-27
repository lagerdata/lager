# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import json
import importlib
from lager.power.battery.battery_net import LibraryMissingError, DeviceNotFoundError, BatteryBackendError

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
        die("ERROR [usage] No command data provided to battery backend", code=64)  # EX_USAGE

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
        dispatcher = importlib.import_module("lager.power.battery.dispatcher")

        func = getattr(dispatcher, action, None)
        if func is None:
            die(f"ERROR [unexpected] Unknown battery command: {action}", code=1)

        func(net_name, **params)

    except LibraryMissingError as exc:
        die(f"ERROR [library-missing] {exc}", code=2)
    except DeviceNotFoundError as exc:
        # 0.20.0+: map raw libusb/pyvisa errnos (16/19/110) into actionable
        # messages instead of dumping the raw "[Errno 16] Resource busy"
        # string. Fall back to the raw error if no mapping applies.
        _die_mapped_or_raw(exc, fallback_prefix="ERROR [device-not-found]", code=3)
    except BatteryBackendError as exc:
        _die_mapped_or_raw(exc, fallback_prefix="ERROR [backend]", code=4)
    except SystemExit:
        raise  # let deliberate exits bubble
    except Exception as exc:
        _die_mapped_or_raw(exc, fallback_prefix="ERROR [unexpected]", code=1)


def _die_mapped_or_raw(exc, fallback_prefix, code):
    """Try the 0.20.0 system-error mapper; if it recognizes the exception
    (errno 16/19/110), print the actionable text. Otherwise fall back to
    the bare exception string with the legacy `ERROR [xxx]` prefix."""
    try:
        from cli.context.error_handlers import format_system_error_for_user
        mapped = format_system_error_for_user(str(exc))
    except Exception:
        mapped = None
    if mapped:
        die(mapped, code=code)
    else:
        die(f"{fallback_prefix} {exc}", code=code)


if __name__ == '__main__':
    main()
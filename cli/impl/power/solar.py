# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import json
import importlib
from lager.power.solar.solar_net import LibraryMissingError, DeviceNotFoundError, SolarBackendError, DeviceLockError

_RED = "\033[31m"
_RESET = "\033[0m"

def die(msg: str, code: int = 1) -> None:
    """Print message to stderr (if error) or stdout (if code 0), then exit."""
    if code == 0:
        print(msg)
    else:
        print(f"{_RED}{msg}{_RESET}", file=sys.stderr)
    sys.exit(code)

def main() -> None:
    # Parse command data from environment
    raw = os.environ.get("LAGER_COMMAND_DATA")
    if not raw:
        die("ERROR [usage] No command data provided to solar backend", code=64)
    try:
        command = json.loads(raw)
    except Exception as exc:
        die(f"ERROR [usage] Could not parse command data: {exc}", code=64)
    action = command.get("action")
    params = command.get("params", {}) or {}
    net_name = params.pop("netname", None)
    if not action or not net_name:
        die("ERROR [usage] Missing action or net name", code=64)
    try:
        dispatcher = importlib.import_module("lager.power.solar.dispatcher")
        func = getattr(dispatcher, action, None)
        if func is None:
            die(f"ERROR [unexpected] Unknown solar command: {action}", code=1)
        # Invoke the corresponding action in the solar dispatcher
        func(net_name, **params)
    except LibraryMissingError as exc:
        die(f"ERROR [library-missing] {exc}", code=2)
    except DeviceNotFoundError as exc:
        die(f"ERROR [device-not-found] {exc}", code=3)
    except DeviceLockError as exc:
        die(f"ERROR [device-busy] {exc}", code=4)
    except SolarBackendError as exc:
        die(f"ERROR [backend] {exc}", code=5)
    except SystemExit:
        raise  # allow deliberate exits to propagate
    except Exception as exc:
        die(f"ERROR [unexpected] {exc}", code=1)

if __name__ == "__main__":
    main()

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

import os
import json
import importlib

from lager.cli_output import die, ExitCode
from lager.power.supply.supply_net import (
    LibraryMissingError,
    DeviceNotFoundError,
    SupplyBackendError,
)


def main() -> None:
    cmd_data = os.environ.get("LAGER_COMMAND_DATA")
    if not cmd_data:
        die("No command data provided to supply backend",
            code=ExitCode.USAGE, category="usage", command="supply")

    try:
        command = json.loads(cmd_data)
    except Exception as exc:
        die(f"Could not parse command data: {exc}",
            code=ExitCode.USAGE, category="usage", command="supply")

    action = command.get("action")
    params = command.get("params", {}) or {}
    net_name = params.pop("netname", None)

    if not action or not net_name:
        die("Missing action or net name",
            code=ExitCode.USAGE, category="usage", command="supply")

    cmd_path = f"supply.{action}"
    subject = {"net": net_name}

    try:
        dispatcher = importlib.import_module("lager.power.supply.dispatcher")
        func = getattr(dispatcher, action, None)
        if func is None:
            die(f"Unknown supply command: {action}",
                code=ExitCode.USAGE, category="usage",
                command=cmd_path, subject=subject)
        func(net_name, **params)

    except LibraryMissingError as exc:
        die(str(exc), code=ExitCode.LIBRARY_MISSING,
            category="library-missing", command=cmd_path, subject=subject)
    except DeviceNotFoundError as exc:
        die(str(exc), code=ExitCode.DEVICE_NOT_FOUND,
            category="device-not-found", command=cmd_path, subject=subject)
    except SupplyBackendError as exc:
        die(str(exc), code=ExitCode.BACKEND_ERROR,
            category="backend", command=cmd_path, subject=subject)
    except SystemExit:
        raise  # let deliberate exits bubble
    except Exception as exc:
        die(str(exc), code=ExitCode.UNEXPECTED,
            category="unexpected", command=cmd_path, subject=subject)


if __name__ == "__main__":
    main()

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

# box/lager/arm/dispatcher.py
from __future__ import annotations
import os, sys, json, argparse
from typing import Optional, Tuple, Any

from .rotrics import Dexarm  # Dexarm subclasses ArmBase
from .arm_net import ArmBackendError, MovementTimeoutError, LibraryMissingError, DeviceNotFoundError

from ...constants import SAVED_NETS_PATH as _DEFAULT_SAVED_NETS_PATH
_SAVED_NETS_PATH = os.environ.get("SAVED_NETS_PATH", _DEFAULT_SAVED_NETS_PATH)

# ---------- JSON helpers ----------

def _json_ok(result: Any = None) -> None:
    print(json.dumps({"status": "ok", "result": result}, separators=(",", ":")))
    sys.exit(0)

def _json_err(msg: str, *, code: int = 1) -> None:
    print(json.dumps({"status": "error", "error": str(msg)}, separators=(",", ":")))
    sys.exit(code)

# ---------- nets resolution ----------

def _load_saved_nets() -> dict:
    """
    Loads a nets DB that looks like: {"nets":[{...}, {...}]}
    - Returns an empty structure if file is missing/empty/invalid instead of crashing the runner.
    - Honors SAVED_NETS_PATH if set.
    """
    try:
        with open(_SAVED_NETS_PATH, "r", encoding="utf-8") as f:
            raw = f.read()
        if not raw.strip():
            # Treat empty as no nets rather than raising JSON error
            return {"nets": []}
        data = json.loads(raw)
        if isinstance(data, list):
            # Accept legacy list format and wrap it
            return {"nets": data}
        if isinstance(data, dict) and "nets" in data and isinstance(data["nets"], list):
            return data
        # Unknown shape -> treat as empty
        return {"nets": []}
    except FileNotFoundError:
        # Don’t hard-fail; allow --serial or autodetect to work without nets file
        return {"nets": []}
    except json.JSONDecodeError as exc:
        # Surface a clear, structured error
        raise ArmBackendError(f"Corrupt nets database at '{_SAVED_NETS_PATH}': {exc}") from exc

def _resolve_net(net_name: str) -> Optional[dict]:
    data = _load_saved_nets()
    for n in data.get("nets", []):
        # minimal schema: {"name": "...", "type": "Arm", "serial": "...", "port": "...", "pin": "..."}
        if n.get("name") == net_name and str(n.get("type", "")).lower() == "arm":
            return n
    return None

def _open_arm(net: Optional[str], serial: Optional[str]) -> Dexarm:
    """
    Resolution order:
    1) If --net given -> read from saved_nets.json (prefer explicit 'port', else 'serial')
    2) Else if --serial given -> use it
    3) Else -> autodetect (Dexarm will do it)
    """
    port = None
    name = "arm0"
    pin = "usb"
    serial_number = None

    if net:
        cfg = _resolve_net(net)
        if not cfg:
            # Keep error JSON-only in main(); raise here to be caught and wrapped.
            raise DeviceNotFoundError(f"net '{net}' not found or not type=Arm in nets DB")
        name = cfg.get("name", name)
        pin = cfg.get("pin", pin)
        port = cfg.get("port") or None
        serial_number = cfg.get("serial") or None
    elif serial:
        serial_number = serial

    # If neither port nor serial, Dexarm will autodetect
    if port:
        return Dexarm(port=port, serial_number=serial_number, name=name, pin=pin)
    return Dexarm(port=None, serial_number=serial_number, name=name, pin=pin)

# ---------- args ----------

def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="arm-dispatcher", add_help=False)
    parser.add_argument("command", type=str)
    # selectors
    parser.add_argument("--net", type=str, default=None)
    parser.add_argument("--serial", type=str, default=None)
    # motion args
    parser.add_argument("--x", type=float)
    parser.add_argument("--y", type=float)
    parser.add_argument("--z", type=float)
    parser.add_argument("--dx", type=float)
    parser.add_argument("--dy", type=float)
    parser.add_argument("--dz", type=float)
    parser.add_argument("--timeout", type=float, default=5.0)
    # passthrough help (so click --help does not blow up)
    parser.add_argument("--help", action="store_true")
    ns, _ = parser.parse_known_args(argv)
    return ns

# ---------- entry ----------

def main():
    try:
        ns = _parse_args(sys.argv[1:])
        if ns.help:
            _json_ok({"usage": "dispatcher.py <command> [--net NET | --serial SERIAL] [--x/--y/--z or --dx/--dy/--dz] [--timeout SEC]"})

        cmd = ns.command

        if cmd == "position":
            with _open_arm(ns.net, ns.serial) as arm:
                x, y, z = arm.position()
                _json_ok({"x": x, "y": y, "z": z})

        elif cmd == "disable_motor":
            with _open_arm(ns.net, ns.serial) as arm:
                arm.disable_motor()
                _json_ok()

        elif cmd == "enable_motor":
            with _open_arm(ns.net, ns.serial) as arm:
                arm.enable_motor()
                _json_ok()

        elif cmd == "read_and_save_position":
            with _open_arm(ns.net, ns.serial) as arm:
                arm.read_and_save_position()
                _json_ok()

        elif cmd == "delta":
            dx = ns.dx if ns.dx is not None else (ns.x if ns.x is not None else 0.0)
            dy = ns.dy if ns.dy is not None else (ns.y if ns.y is not None else 0.0)
            dz = ns.dz if ns.dz is not None else (ns.z if ns.z is not None else 0.0)
            with _open_arm(ns.net, ns.serial) as arm:
                x, y, z = arm.move_relative(dx=dx, dy=dy, dz=dz, timeout=ns.timeout)
                _json_ok({"x": x, "y": y, "z": z})

        elif cmd == "move":
            if ns.x is None or ns.y is None or ns.z is None:
                raise ArmBackendError("move requires --x, --y, --z")
            with _open_arm(ns.net, ns.serial) as arm:
                arm.move_to(ns.x, ns.y, ns.z, timeout=ns.timeout)
                x, y, z = arm.position()
                _json_ok({"x": x, "y": y, "z": z})

        elif cmd == "go_home":
            with _open_arm(ns.net, ns.serial) as arm:
                arm.go_home()
                _json_ok()

        else:
            raise ArmBackendError(f"unknown command: {cmd}")

    # Known, user-actionable errors → code 1
    except (ArmBackendError, MovementTimeoutError, LibraryMissingError, DeviceNotFoundError) as e:
        _json_err(str(e), code=1)

    # argparse exits should be passthrough (but usually won’t happen since we don’t call parser.error)
    except SystemExit:
        raise

    # Everything else → code 2
    except Exception as e:
        _json_err(repr(e), code=2)

if __name__ == "__main__":
    main()

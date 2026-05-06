# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import sys
import json
from typing import Any, Dict, Optional
from lager.nets.net import Net, NetType

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

def _as_float(v: Optional[Any]) -> Optional[float]:
    if v is None or v == "None":
        return None
    return float(v)

def main() -> int:
    arm = None
    try:
        # Expect a single JSON payload in argv[1], same as thermocouple
        data: Dict[str, Any] = json.loads(sys.argv[1])

        netname: str = data["netname"]
        command: str = data["command"]

        # Optional args
        x   = _as_float(data.get("x"))
        y   = _as_float(data.get("y"))
        z   = _as_float(data.get("z"))
        dx  = _as_float(data.get("dx"))
        dy  = _as_float(data.get("dy"))
        dz  = _as_float(data.get("dz"))
        timeout = _as_float(data.get("timeout")) or 15.0

        # Resolve the Arm net via your Net registry
        arm = Net.get(netname, type=NetType.Arm)

        if command == "position":
            px, py, pz = arm.position()
            sys.stdout.write(f"{GREEN}X: {px} Y: {py} Z: {pz}{RESET}\n")
            sys.stdout.flush()
            return 0

        elif command == "move":
            if x is None or y is None or z is None:
                raise ValueError("move requires x, y, z")
            arm.move_to(x, y, z, timeout=timeout)
            px, py, pz = arm.position()
            sys.stdout.write(f"{GREEN}X: {px} Y: {py} Z: {pz}{RESET}\n")
            sys.stdout.flush()
            return 0

        elif command == "move_by":
            ddx = dx or 0.0
            ddy = dy or 0.0
            ddz = dz or 0.0
            px, py, pz = arm.move_relative(ddx, ddy, ddz, timeout=timeout)
            sys.stdout.write(f"{GREEN}X: {px} Y: {py} Z: {pz}{RESET}\n")
            sys.stdout.flush()
            return 0

        elif command == "go_home":
            arm.go_home()
            return 0

        elif command == "enable_motor":
            arm.enable_motor()
            return 0

        elif command == "disable_motor":
            arm.disable_motor()
            return 0

        elif command == "read_and_save_position":
            arm.read_and_save_position()
            return 0

        elif command == "set_acceleration":
            # required: acceleration, travel_acceleration; optional: retract_acceleration (default=60)
            acceleration = int(data["acceleration"])
            travel_acceleration = int(data["travel_acceleration"])
            retract_acceleration = int(data.get("retract_acceleration", 60))
            arm.set_acceleration(acceleration, travel_acceleration, retract_acceleration=retract_acceleration)
            sys.stdout.write(
                f"{GREEN}Acceleration set (M204): print={acceleration} travel={travel_acceleration} retract={retract_acceleration}{RESET}\n"
            )
            sys.stdout.flush()
            return 0


        else:
            raise ValueError(f"unknown command: {command}")

    except Exception as e:
        sys.stderr.write(f"{RED}{e}{RESET}\n")
        sys.stderr.flush()
        return 1

    finally:
        # CRITICAL: Always close the serial port
        if arm is not None and hasattr(arm, 'close'):
            try:
                arm.close()
            except Exception:
                pass  # Ignore cleanup errors

if __name__ == "__main__":
    sys.exit(main())

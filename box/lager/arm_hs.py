# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for robot-arm nets (create_device factory).

See ``adc_hs`` for why this is a role-unique top-level module. The adapter
owns the Dexarm's serial handle inside hardware_service, so the port is opened
once and cached (no per-command open/close like the old :5000 impl script) and
every call serializes under the net's shared ``device_id`` lock — two
concurrent move commands can never interleave G-code on one arm.

Workspace bounds are enforced here (box-side) for both absolute and relative
moves; the old path only checked them CLI-side, so ``move_by`` could walk the
arm out of bounds.
"""
from __future__ import annotations


class ArmHardwareAdapter:
    def __init__(self, netname: str) -> None:
        self._netname = netname
        self._arm = None

    def _get(self):
        """Resolve (and cache) the Dexarm driver for this net."""
        if self._arm is None:
            from lager.nets.net import Net
            from lager.nets.constants import NetType
            arm = Net.get_from_saved_json(self._netname, NetType.Arm)
            if arm is None:
                raise RuntimeError(f"Arm net '{self._netname}' not found")
            self._arm = arm
        return self._arm

    def _check_bounds(self, x, y, z):
        """Raise if (x, y, z) is outside the Dexarm workspace."""
        arm = self._get()
        problems = []
        if not arm.BOUNDS_X_MIN <= x <= arm.BOUNDS_X_MAX:
            problems.append(f"X={x} outside [{arm.BOUNDS_X_MIN}, {arm.BOUNDS_X_MAX}]")
        if not arm.BOUNDS_Y_MIN <= y <= arm.BOUNDS_Y_MAX:
            problems.append(f"Y={y} outside [{arm.BOUNDS_Y_MIN}, {arm.BOUNDS_Y_MAX}]")
        if not arm.BOUNDS_Z_MIN <= z <= arm.BOUNDS_Z_MAX:
            problems.append(f"Z={z} outside [{arm.BOUNDS_Z_MIN}, {arm.BOUNDS_Z_MAX}]")
        if problems:
            raise RuntimeError(
                "Coordinates out of bounds: %s. Bounds: %s"
                % ("; ".join(problems), arm.get_bounds_string()))

    def position(self):
        x, y, z = self._get().position()
        return [float(x), float(y), float(z)]

    def move(self, x, y, z, timeout=15.0):
        """Absolute move with blocking wait; returns the resulting position."""
        self._check_bounds(float(x), float(y), float(z))
        arm = self._get()
        arm.move_to(float(x), float(y), float(z), timeout=float(timeout))
        return self.position()

    def move_by(self, dx=0.0, dy=0.0, dz=0.0, timeout=15.0):
        """Relative move with blocking wait; returns the resulting position."""
        arm = self._get()
        cx, cy, cz = arm.position()
        self._check_bounds(cx + float(dx), cy + float(dy), cz + float(dz))
        nx, ny, nz = arm.move_relative(
            float(dx), float(dy), float(dz), timeout=float(timeout))
        return [float(nx), float(ny), float(nz)]

    def go_home(self):
        self._get().go_home()
        return True

    def enable_motor(self):
        self._get().enable_motor()
        return True

    def disable_motor(self):
        self._get().disable_motor()
        return True

    def read_and_save_position(self):
        """Read the current position, persist it on the arm (M889)."""
        x, y, z = self._get().read_and_save_position()
        return [float(x), float(y), float(z)]

    def set_acceleration(self, acceleration, travel_acceleration,
                         retract_acceleration=60):
        self._get().set_acceleration(
            int(acceleration), int(travel_acceleration),
            retract_acceleration=int(retract_acceleration))
        return True

    def close(self):
        """Release the serial port (called by hardware_service cache eviction)."""
        if self._arm is not None:
            try:
                self._arm.close()
            finally:
                self._arm = None


def create_device(net_info, **_):
    netname = (net_info or {}).get("name")
    return ArmHardwareAdapter(netname)

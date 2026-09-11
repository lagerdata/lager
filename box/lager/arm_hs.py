# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
hardware_service adapter for robot-arm nets (create_device factory).

See ``adc_hs`` for why this is a role-unique top-level module. The adapter
owns the Dexarm's serial handle inside hardware_service, so the port is opened
once and cached (no per-command open/close like the old :5000 impl script) and
every call serializes under the net's shared ``device_id`` lock — two
concurrent move commands can never interleave G-code on one arm.

Workspace bounds are enforced by the driver's ``move_to`` and
``move_relative`` (``Dexarm.check_bounds``), which this adapter calls, so
``lager arm`` and the Python API refuse the same targets.

A serial error drops the cached handle so the next call reopens the port.
hardware_service's own recreate-and-retry path recognizes only VISA and ENODEV
errors, and pyserial's disconnect error ("device reports readiness to read but
returned no data") is neither. Without this, an arm that was unplugged,
power-cycled, or briefly opened by another process stayed unusable until the
service restarted.
"""
from __future__ import annotations

# hardware_service abandons a driver call that is still running after 30 s
# (_INVOKE_DEADLINE_S), keeps the device lock, and restarts itself, which
# interrupts every instrument on the box. One move's wait is capped below that,
# leaving room for the position reads around the move.
MAX_MOVE_TIMEOUT_S = 25.0


def validate_move_timeout(timeout) -> float:
    """Return ``timeout`` as seconds; raise ValueError outside (0, MAX_MOVE_TIMEOUT_S]."""
    value = float(timeout)
    if not 0 < value <= MAX_MOVE_TIMEOUT_S:
        raise ValueError(
            "Move timeout must be greater than 0 and at most %g s, got %g"
            % (MAX_MOVE_TIMEOUT_S, value))
    return value


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

    def _drop(self):
        """Close and forget the cached driver; the next call reopens the port."""
        arm, self._arm = self._arm, None
        if arm is not None:
            try:
                arm.close()
            except Exception:
                pass

    def _call(self, fn, *, retry=False):
        """Run ``fn(arm)``, dropping the cached port if the call hits a serial error.

        pyserial's SerialException is an OSError. With ``retry``, the call runs
        once more on a freshly opened port. Only read-only calls may retry: a
        move or a setting may already have reached the arm.
        """
        arm = self._get()
        try:
            return fn(arm)
        except OSError as exc:
            self._drop()
            if not retry:
                raise RuntimeError(
                    "Arm serial connection lost (%s). The port was closed and "
                    "reopens on the next command. The arm may still be "
                    "finishing the last command it received." % (exc,)) from exc
        return fn(self._get())

    def position(self):
        x, y, z = self._call(lambda arm: arm.position(), retry=True)
        return [float(x), float(y), float(z)]

    def move(self, x, y, z, timeout=15.0):
        """Absolute move with blocking wait; returns the resulting position."""
        wait = validate_move_timeout(timeout)

        def _move(arm):
            arm.move_to(float(x), float(y), float(z), timeout=wait)
            return arm.position()

        nx, ny, nz = self._call(_move)
        return [float(nx), float(ny), float(nz)]

    def move_by(self, dx=0.0, dy=0.0, dz=0.0, timeout=15.0):
        """Relative move with blocking wait; returns the resulting position."""
        wait = validate_move_timeout(timeout)
        nx, ny, nz = self._call(lambda arm: arm.move_relative(
            float(dx), float(dy), float(dz), timeout=wait))
        return [float(nx), float(ny), float(nz)]

    def go_home(self):
        self._call(lambda arm: arm.go_home())
        return True

    def enable_motor(self):
        self._call(lambda arm: arm.enable_motor())
        return True

    def disable_motor(self):
        self._call(lambda arm: arm.disable_motor())
        return True

    def read_and_save_position(self):
        """Read the current position, then recalibrate on it (M889)."""
        x, y, z = self._call(lambda arm: arm.read_and_save_position())
        return [float(x), float(y), float(z)]

    def set_acceleration(self, acceleration, travel_acceleration,
                         retract_acceleration=60):
        self._call(lambda arm: arm.set_acceleration(
            int(acceleration), int(travel_acceleration),
            retract_acceleration=int(retract_acceleration)))
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

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import serial
import re
import time
from typing import List, Optional, Tuple
from serial.tools import list_ports

from .arm_net import ArmBase
from .arm_net import MovementTimeoutError
from .arm_net import NotHomedError
from .arm_net import OutOfBoundsError
from .arm_net import UnsupportedFirmwareError
TOLERANCE = 0.5

# Rotrics interchanged the X and Y axes in DexArm firmware V2.1.4. Older
# firmware treats X as the forward axis (home reads X300 Y0), so a target in
# the documented frame (home X0 Y300) swings the arm sideways. Coordinate moves
# are refused below this version.
MIN_FIRMWARE_VERSION = (2, 1, 4)
_FIRMWARE_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Every wait loop below re-checks its own deadline after each read, so a short
# serial read timeout keeps those deadlines accurate.
SERIAL_READ_TIMEOUT_S = 1.0
# The arm answers M114 in a few milliseconds. Three attempts at this wait keep a
# position read well inside the hardware_service call budget.
POSITION_REPLY_TIMEOUT_S = 3.0
# M1112 sends its ok only once the arm is home: about 3 s from the calibration
# pose.
HOME_TIMEOUT_S = 20.0

def get_arm_device(serial_number: Optional[str] = None) -> str:
    all_ports = []
    for port in list_ports.comports():
        if serial_number is not None:
            if serial_number == port.serial_number:
                return port.device
        else:
            if port.pid == 0x5740 and port.vid == 0x0483:
                all_ports.append(port.device)
    else:
        if serial_number is not None:
            raise RuntimeError(f'Arm with USB serial {serial_number} not found')
        if not all_ports:
            raise RuntimeError('Arm not found!')
        elif len(all_ports) > 1:
            raise RuntimeError('Multiple arms found; please supply a serial number')
        else:
            return all_ports[0]


class Dexarm(ArmBase):
    """High-level Dexarm that also satisfies the ArmBase interface.

    Backward-compatible with prior usage:
        with Dexarm(port="/dev/ttyACM0") as arm: ...
    And ArmBase-friendly construction:
        Dexarm(name="arm0", pin="usb", serial_number="ABCD1234")
    """

    # Approximate workspace bounds for Rotrics Dexarm (in mm)
    # Based on Rotrics specifications:
    # - X: left/right from center
    # - Y: forward distance from base
    # - Z: down/up relative to table level
    BOUNDS_X_MIN = -300
    BOUNDS_X_MAX = 300
    BOUNDS_Y_MIN = 170
    BOUNDS_Y_MAX = 360
    BOUNDS_Z_MIN = -140
    BOUNDS_Z_MAX = 100

    @classmethod
    def get_bounds_string(cls) -> str:
        """Return a human-readable string describing workspace bounds."""
        return (
            f"X: {cls.BOUNDS_X_MIN} to {cls.BOUNDS_X_MAX}, "
            f"Y: {cls.BOUNDS_Y_MIN} to {cls.BOUNDS_Y_MAX}, "
            f"Z: {cls.BOUNDS_Z_MIN} to {cls.BOUNDS_Z_MAX}"
        )

    @classmethod
    def check_bounds(cls, x: float, y: float, z: float) -> None:
        """Raise ``OutOfBoundsError`` if (x, y, z) is outside the workspace bounds.

        ``move_to`` and ``move_relative`` call this before they send anything.
        The hardware_service adapter behind ``lager arm`` moves through those
        same methods, so the CLI and the Python API refuse the same targets.
        """
        problems = []
        if not cls.BOUNDS_X_MIN <= x <= cls.BOUNDS_X_MAX:
            problems.append(f"X={x} outside [{cls.BOUNDS_X_MIN}, {cls.BOUNDS_X_MAX}]")
        if not cls.BOUNDS_Y_MIN <= y <= cls.BOUNDS_Y_MAX:
            problems.append(f"Y={y} outside [{cls.BOUNDS_Y_MIN}, {cls.BOUNDS_Y_MAX}]")
        if not cls.BOUNDS_Z_MIN <= z <= cls.BOUNDS_Z_MAX:
            problems.append(f"Z={z} outside [{cls.BOUNDS_Z_MIN}, {cls.BOUNDS_Z_MAX}]")
        if problems:
            raise OutOfBoundsError(
                "Coordinates out of bounds: %s. Bounds: %s"
                % ("; ".join(problems), cls.get_bounds_string()))

    @staticmethod
    def serial_from_net_record(rec) -> Optional[str]:
        """USB serial of the arm a saved net record points at, or None.

        Checked in order: ``serial``, ``location.serial_number``, then the
        serial field of a VISA-style address
        (``USB0::0x0483::0x5740::<serial>::INSTR``) under ``address`` or a mux
        mapping's ``device_override``. ``lager nets add-all`` records the serial
        only in the address. Without that last step a saved net opened
        whichever 0483:5740 device it found first, and that VID:PID is a generic
        STM32 virtual COM port id that other boards also use.
        """
        if not isinstance(rec, dict):
            return None
        location = rec.get("location")
        serial_number = rec.get("serial") or (
            location.get("serial_number") if isinstance(location, dict) else None)
        if serial_number:
            return str(serial_number)
        for key in ("address", "device_override"):
            parts = str(rec.get(key) or "").split("::")
            if len(parts) > 3 and parts[3]:
                return parts[3]
        return None

    def __init__(
        self,
        port: Optional[str] = None,
        serial_number: Optional[str] = None,
        *,
        name: str = "arm0",
        pin: int | str = "usb",
    ):
        # Initialize ArmBase fields
        super().__init__(name=name, pin=pin)

        # Keep old constructor semantics too
        self._serial_number = serial_number
        if port is None:
            port = get_arm_device(serial_number)

        # A short read timeout: every wait loop checks its own deadline between
        # reads. write_timeout=5 prevents write() from blocking indefinitely.
        self.ser = serial.Serial(port, 115200, timeout=SERIAL_READ_TIMEOUT_S, write_timeout=5)
        self.is_open = self.ser.isOpen()
        if not self.is_open:
            raise RuntimeError("Could not open arm")
        self.firmware_version = self._read_firmware_version()

    # ---- Context manager ----
    def __enter__(self) -> "Dexarm":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ---- Firmware version ----
    def _read_firmware_version(self) -> Optional[Tuple[int, int, int]]:
        """Ask the arm for its firmware version (M2010). None if it does not say."""
        try:
            lines = self._send_cmd("M2010\r\n", timeout=2.0)
        except RuntimeError:
            return None
        for line in lines:
            if "firmware" in line.lower():
                match = _FIRMWARE_VERSION_RE.search(line)
                if match:
                    return tuple(int(v) for v in match.groups())
        return None

    def _require_supported_firmware(self) -> None:
        """Refuse a coordinate move unless the firmware uses the documented frame."""
        version = self.firmware_version
        if version is None:
            raise UnsupportedFirmwareError(
                "Could not read the Dexarm firmware version (M2010), so the arm's "
                "coordinate frame is unknown. lager moves the arm only on firmware "
                "V2.1.4 or later. Update the arm firmware. go-home and position "
                "still work.")
        if version < MIN_FIRMWARE_VERSION:
            raise UnsupportedFirmwareError(
                "Dexarm firmware V%d.%d.%d is older than V2.1.4, the version where "
                "Rotrics swapped the X and Y axes. A move in lager's coordinates "
                "would swing this arm sideways, so it is refused. Update the arm "
                "firmware to V2.1.4 or later. go-home and position still work."
                % version)

    # ---- ArmBase required methods ----
    def position(self) -> Tuple[float, float, float]:
        """ArmBase: return (x, y, z)."""
        x, y, z, *_ = self.get_full_position()
        return x, y, z

    def move_to(self, x: float, y: float, z: float, *, timeout: float = 15.0) -> None:
        """ArmBase: absolute move with blocking wait.

        Args:
            x, y, z: Target coordinates in mm
            timeout: Timeout in seconds (default: 15.0)

        Raises:
            OutOfBoundsError: The target is outside the workspace bounds.
                Nothing is sent to the arm.
            UnsupportedFirmwareError: The arm firmware is older than V2.1.4, or
                did not report its version. Nothing is sent to the arm.
            NotHomedError: The arm has not been homed since it powered on.
        """
        self.check_bounds(x, y, z)
        self._require_supported_firmware()
        self.move_to_blocking(x, y, z, timeout=timeout)

    def move_relative(
        self,
        dx: float = 0.0,
        dy: float = 0.0,
        dz: float = 0.0,
        *,
        timeout: float = 15.0
    ) -> Tuple[float, float, float]:
        """ArmBase: relative move, return new (x, y, z).

        Args:
            dx, dy, dz: Delta coordinates in mm
            timeout: Timeout in seconds (default: 15.0)

        Raises:
            UnsupportedFirmwareError: The arm firmware is older than V2.1.4, or
                did not report its version. Nothing is sent to the arm.
            OutOfBoundsError: The resulting target is outside the workspace
                bounds. The position is read, but no move is sent.
            NotHomedError: The arm has not been homed since it powered on.
        """
        self._require_supported_firmware()
        cx, cy, cz, *_ = self.get_full_position()
        self.check_bounds(cx + dx, cy + dy, cz + dz)
        self.move_to_blocking(cx + dx, cy + dy, cz + dz, timeout=timeout)
        nx, ny, nz, *_ = self.get_full_position()
        return nx, ny, nz

    def go_home(self, timeout: float = HOME_TIMEOUT_S) -> None:
        """Move to the home position (M1112) and return when the arm is there.

        M1112 prints "busy: processing" while it runs and sends its ok only when
        the arm arrives, so waiting for that ok is waiting for the arm. After
        power-on, M1112 is also what initializes the arm: the firmware refuses
        motion until then. Home is X0 Y300 Z0 on firmware V2.1.4 or later.
        """
        self._send_cmd("M1112\r", timeout=timeout)

    def enable_motor(self) -> None:
        """Energize the stepper motors (M17), holding position under load."""
        self._send_cmd("M17\r")

    def disable_motor(self) -> None:
        """Release the stepper motors (M18) so the arm can be moved by hand."""
        self._send_cmd("M18\r")

    def save_position(self) -> None:
        """Recalibrate: store the current pose as the calibration position (M889).

        M889 reads the joint encoders and saves them as the reference that
        every later move is computed from. Send it only with the arm physically
        in its calibration pose, as in the Rotrics recalibration procedure. In
        any other pose it offsets every later move.
        """
        self._send_cmd("M889\r")

    def read_and_save_position(self) -> Tuple[float, float, float]:
        """Read the current position, then recalibrate on it with M889.

        Returns the (x, y, z) read before M889. This replaces the arm's stored
        calibration; see ``save_position``.
        """
        x, y, z, *_ = self.get_full_position()
        self.save_position()
        return x, y, z

    # ---- Low-level helpers / existing API ----
    def _send_cmd(self, data: str, wait: bool = True, timeout: float = 15.0) -> List[str]:
        """Send a command. Unless ``wait`` is False, return its reply lines up to the ok."""
        # Clear any pending data in the input buffer before sending new command
        # This prevents leftover responses from previous commands from interfering
        time.sleep(0.05)  # Let any pending data arrive
        while self.ser.in_waiting > 0:
            self.ser.read(self.ser.in_waiting)  # Discard pending data

        self.ser.write(data.encode())
        if not wait:
            # Don't wait for response, but give ARM time to start processing
            time.sleep(0.1)
            return []

        start_time = time.time()
        lines = []
        while True:
            if time.time() - start_time > timeout:
                raise RuntimeError(f"Timeout waiting for 'ok' response from ARM (waited {timeout}s)")

            serial_str = self.ser.readline().decode("utf-8", errors="replace")
            if serial_str:
                lines.append(serial_str.strip())
                if "ok" in serial_str:
                    return lines

    def set_workorigin(self) -> None:
        """Define the current position as the work origin, X0 Y0 Z0 (G92)."""
        self._send_cmd("G92 X0 Y0 Z0 E0\r")

    def set_acceleration(self, acceleration: int, travel_acceleration: int, retract_acceleration: int = 60) -> None:
        """Set print (P), travel (T) and retract (R) acceleration in mm/s^2 (M204)."""
        # Marlin reads retract acceleration from R. The P/T/T string sent here
        # before (inherited from the Rotrics pydexarm example) set travel
        # acceleration to the retract value and never set retract.
        cmd = (
            "M204 P" + str(acceleration)
            + " T" + str(travel_acceleration)
            + " R" + str(retract_acceleration)
            + "\r\n"
        )
        self._send_cmd(cmd)

    def set_module_type(self, module_type: int) -> None:
        """0=PEN, 1=LASER, 2=PNEUMATIC, 3=3D."""
        self._send_cmd("M888 P" + str(module_type) + "\r")

    def get_module_type(self) -> Optional[str]:
        """Return 'PEN'|'LASER'|'PUMP'|'3D' (if detectable)."""
        # Don't call reset_input_buffer() - it can hang on some serial devices
        self.ser.write('M888\r'.encode())
        module_type: Optional[str] = None

        # Add timeout to prevent infinite loop if ARM doesn't respond
        start_time = time.time()
        timeout = 15.0  # 15 second timeout for module type query

        while True:
            # Check if we've exceeded timeout
            if time.time() - start_time > timeout:
                raise RuntimeError(f"Timeout waiting for module type response from ARM (waited {timeout}s)")

            serial_str = self.ser.readline().decode("utf-8")
            if serial_str:
                if "PEN" in serial_str:
                    module_type = 'PEN'
                if "LASER" in serial_str:
                    module_type = 'LASER'
                if "PUMP" in serial_str:
                    module_type = 'PUMP'
                if "3D" in serial_str:
                    module_type = '3D'
                if "ok" in serial_str:
                    return module_type

    def move_to_blocking(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        e: Optional[float] = None,
        feedrate: int = 2000,
        mode: str = "G1",
        wait: bool = True,
        *,
        timeout: float = 15.0
    ) -> None:
        """Move to a cartesian position and wait until the arm is there.

        Does not check workspace bounds or the firmware version; ``move_to``
        and ``move_relative`` do.

        Args:
            x, y, z, e: Target coordinates (None means don't change that axis)
            feedrate: Movement speed
            mode: G-code mode (G0 or G1)
            wait: Whether to wait for command acknowledgment
            timeout: Timeout in seconds (default: 15.0)

        Raises:
            NotHomedError: The firmware refused the move because the arm has
                not been homed since it powered on.
            MovementTimeoutError: The arm did not reach the target in time.
        """
        # The firmware acknowledges a move as soon as it queues it; the ok does
        # not wait for the move to finish. Before the arm is homed after
        # power-on, the firmware answers with a refusal line instead of moving,
        # so read the reply rather than discarding it.
        lines = self.move_to_gcode(x, y, z, e, feedrate, mode, wait=True)
        if any("initialize" in line.lower() for line in lines):
            raise NotHomedError(
                "The arm has not been homed since it powered on, so the firmware "
                "refused the move. Run go-home first.")

        # Give the ARM a moment to start moving
        time.sleep(0.2)

        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                # move_to and move_relative check the bounds before a move is
                # sent, so a timeout means the arm did not arrive: an obstruction,
                # an unreachable target inside the bounds, or a long move.
                raise MovementTimeoutError(
                    "The arm did not reach the target within %g s. It may be "
                    "obstructed or the target unreachable, or a long move may need "
                    "a longer timeout." % timeout,
                    target_x=x,
                    target_y=y,
                    target_z=z,
                    bounds_hint=self.get_bounds_string(),
                )

            time.sleep(0.3)  # Poll every 0.3s to reduce serial traffic
            my_x, my_y, my_z, *_ = self.get_full_position()
            if (
                (x is None or abs(x - my_x) < TOLERANCE)
                and (y is None or abs(y - my_y) < TOLERANCE)
                and (z is None or abs(z - my_z) < TOLERANCE)
            ):
                break

    def move_to_gcode(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        z: Optional[float] = None,
        e: Optional[float] = None,
        feedrate: int = 2000,
        mode: str = "G1",
        wait: bool = True
    ) -> List[str]:
        """Raw G-code move (non-ArmBase API). Does not check workspace bounds
        or the firmware version. Returns the reply lines when ``wait`` is True."""
        cmd = mode + "F" + str(feedrate)
        if x is not None:
            cmd += "X" + str(x)
        if y is not None:
            cmd += "Y" + str(y)
        if z is not None:
            cmd += "Z" + str(z)
        if e is not None:
            cmd += "E" + str(round(e))
        cmd += "\r\n"
        return self._send_cmd(cmd, wait=wait)

    def fast_move_to(self, x: Optional[float] = None, y: Optional[float] = None, z: Optional[float] = None, feedrate: int = 2000, wait: bool = True) -> None:
        """Convenience for G0 moves. Does not check workspace bounds or the firmware version."""
        self.move_to_gcode(x=x, y=y, z=z, feedrate=feedrate, mode="G0", wait=wait)

    def get_full_position(self) -> Tuple[float, float, float, float, float, float, float]:
        """Return (x, y, z, e, a, b, c)."""
        # Retry up to 3 times to handle intermittent incomplete responses
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Clear any pending data in the input buffer before querying position
                # This prevents old responses from interfering when polling rapidly
                time.sleep(0.05)  # Let any pending data arrive
                while self.ser.in_waiting > 0:
                    self.ser.read(self.ser.in_waiting)  # Discard pending data

                self.ser.write('M114\r'.encode())
                x = y = z = e = a = b = c = None

                start_time = time.time()
                timeout = POSITION_REPLY_TIMEOUT_S

                while True:
                    # Check if we've exceeded timeout
                    if time.time() - start_time > timeout:
                        raise RuntimeError(f"Timeout waiting for position response from ARM (waited {timeout}s)")

                    serial_str = self.ser.readline().decode("utf-8", errors="replace")
                    if serial_str:
                        if "X:" in serial_str:
                            temp = re.findall(r"[-+]?\d*\.\d+|\d+", serial_str)
                            x = float(temp[0])
                            y = float(temp[1])
                            z = float(temp[2])
                            e = float(temp[3])
                        if "DEXARM Theta" in serial_str:
                            temp = re.findall(r"[-+]?\d*\.\d+|\d+", serial_str)
                            a = float(temp[0])
                            b = float(temp[1])
                            c = float(temp[2])
                        if "ok" in serial_str:
                            # Basic sanity in case the arm echoed ok before all fields were parsed
                            if x is None or y is None or z is None:
                                raise RuntimeError("Incomplete position response from Dexarm")
                            return x, y, z, e, a, b, c

            except RuntimeError as e:
                if attempt < max_retries - 1:
                    # Retry with a longer delay
                    time.sleep(0.2)
                    continue
                else:
                    # Last attempt failed, re-raise
                    raise

    # Original method names kept for backward compatibility.
    # Correctly spelled aliases are provided below.
    def dealy_ms(self, value: int) -> None:
        """Pause queue for ms (original name kept for compat)."""
        self._send_cmd("G4 P" + str(value) + '\r')

    def dealy_s(self, value: int) -> None:
        """Pause queue for s (original name kept for compat)."""
        self._send_cmd("G4 S" + str(value) + '\r')

    # Aliases with correct spelling
    def delay_ms(self, value: int) -> None:
        """Pause the motion queue for *value* milliseconds (G4 P)."""
        self.dealy_ms(value)

    def delay_s(self, value: int) -> None:
        """Pause the motion queue for *value* seconds (G4 S)."""
        self.dealy_s(value)

    # End-effector helpers
    def soft_gripper_pick(self) -> None:
        """Close the soft gripper to grasp an object (M1001)."""
        self._send_cmd("M1001\r")

    def soft_gripper_place(self) -> None:
        """Open the soft gripper to release an object (M1000)."""
        self._send_cmd("M1000\r")

    def soft_gripper_neutral(self) -> None:
        """Return the soft gripper to its neutral position (M1002)."""
        self._send_cmd("M1002\r")

    def soft_gripper_stop(self) -> None:
        """Stop the soft gripper and release air pressure (M1003)."""
        self._send_cmd("M1003\r")

    def air_picker_pick(self) -> None:
        """Apply suction to pick up an object (M1000)."""
        self._send_cmd("M1000\r")

    def air_picker_place(self) -> None:
        """Release suction to place an object (M1001)."""
        self._send_cmd("M1001\r")

    def air_picker_neutral(self) -> None:
        """Return the air picker to its neutral state (M1002)."""
        self._send_cmd("M1002\r")

    def air_picker_stop(self) -> None:
        """Stop the air picker pump (M1003)."""
        self._send_cmd("M1003\r")

    def laser_on(self, value: int = 0) -> None:
        """Turn the laser module on at power *value* (M3 S)."""
        self._send_cmd("M3 S" + str(value) + '\r')

    def laser_off(self) -> None:
        """Turn the laser module off (M5)."""
        self._send_cmd("M5\r")

    # Conveyor
    def conveyor_belt_forward(self, speed: int = 0) -> None:
        """Run the conveyor belt forward at *speed* (M2012 D0)."""
        self._send_cmd("M2012 F" + str(speed) + 'D0\r')

    def conveyor_belt_backward(self, speed: int = 0) -> None:
        """Run the conveyor belt backward at *speed* (M2012 D1)."""
        self._send_cmd("M2012 F" + str(speed) + 'D1\r')

    def conveyor_belt_stop(self) -> None:
        """Stop the conveyor belt (M2013)."""
        self._send_cmd("M2013\r")

    # Sliding rail
    def sliding_rail_init(self) -> None:
        """Initialize and home the sliding rail accessory (M2005)."""
        self._send_cmd("M2005\r")

    def close(self) -> None:
        """Release the serial port."""
        self.ser.close()

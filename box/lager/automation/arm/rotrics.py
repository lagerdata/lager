# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import serial
import re
import datetime
import time
from typing import Optional, Tuple
from serial.tools import list_ports

from .arm_net import ArmBase
from .arm_net import MovementTimeoutError
TOLERANCE = 0.5

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

        # Use 10 second timeout to prevent indefinite blocking on serial I/O
        # timeout=None caused hangs when ARM didn't respond to commands
        # write_timeout=5 prevents write() from blocking indefinitely
        self.ser = serial.Serial(port, 115200, timeout=10, write_timeout=5)
        self.is_open = self.ser.isOpen()
        if not self.is_open:
            raise RuntimeError("Could not open arm")

    # ---- Context manager ----
    def __enter__(self) -> "Dexarm":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

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
        """
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
        """
        cx, cy, cz, *_ = self.get_full_position()
        self.move_to_blocking(cx + dx, cy + dy, cz + dz, timeout=timeout)
        nx, ny, nz, *_ = self.get_full_position()
        return nx, ny, nz

    def go_home(self) -> None:
        """Send the go-home command. The M1112 command moves the arm to home position (X=0, Y=300, Z=0)."""
        # M1112 appears to not send "ok" immediately, so we don't wait for it
        # Instead, send the command and let the arm handle it asynchronously
        self._send_cmd("M1112\r", wait=False)
        # Give the arm a moment to start the homing sequence
        time.sleep(0.5)

    def enable_motor(self) -> None:
        self._send_cmd("M17\r")

    def disable_motor(self) -> None:
        self._send_cmd("M18\r")

    def save_position(self) -> None:
        self._send_cmd("M889\r")

    def read_and_save_position(self) -> Tuple[float, float, float]:
        """Read the current position, persist it on the arm (M889), and
        return (x, y, z).

        The CLI's ``read-and-save-position`` command has always called this
        method, but only ``save_position()`` existed — so the command failed
        with AttributeError on every box.
        """
        x, y, z, *_ = self.get_full_position()
        self.save_position()
        return x, y, z

    # ---- Low-level helpers / existing API ----
    def _send_cmd(self, data: str, wait: bool = True) -> None:
        """Send command to the arm, optionally wait for 'ok'."""
        # Clear any pending data in the input buffer before sending new command
        # This prevents leftover responses from previous commands from interfering
        time.sleep(0.05)  # Let any pending data arrive
        while self.ser.in_waiting > 0:
            self.ser.read(self.ser.in_waiting)  # Discard pending data

        self.ser.write(data.encode())
        if not wait:
            # Don't wait for response, but give ARM time to start processing
            time.sleep(0.1)
            return

        # Add timeout to prevent infinite loop if ARM doesn't respond
        start_time = time.time()
        timeout = 15.0  # 15 second timeout for command acknowledgment

        while True:
            # Check if we've exceeded timeout
            if time.time() - start_time > timeout:
                raise RuntimeError(f"Timeout waiting for 'ok' response from ARM (waited {timeout}s)")

            serial_str = self.ser.readline().decode("utf-8")
            if serial_str and ("ok" in serial_str):
                break

    def set_workorigin(self) -> None:
        self._send_cmd("G92 X0 Y0 Z0 E0\r")

    def set_acceleration(self, acceleration: int, travel_acceleration: int, retract_acceleration: int = 60) -> None:
        cmd = (
            "M204"
            + "P" + str(acceleration)
            + "T" + str(travel_acceleration)
            + "T" + str(retract_acceleration)
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

        Args:
            x, y, z, e: Target coordinates (None means don't change that axis)
            feedrate: Movement speed
            mode: G-code mode (G0 or G1)
            wait: Whether to wait for command acknowledgment
            timeout: Timeout in seconds (default: 15.0)
        """
        # Don't wait for 'ok' from movement commands - they don't send it immediately
        # Instead, poll position to detect when movement completes
        self.move_to_gcode(x, y, z, e, feedrate, mode, wait=False)

        # Give the ARM a moment to start moving
        time.sleep(0.2)

        now = datetime.datetime.utcnow()
        delta = datetime.timedelta(seconds=timeout)
        while True:
            if datetime.datetime.utcnow() - now > delta:
                raise MovementTimeoutError(
                    "Movement timed out. Arm may be obstructed or coordinates are out of bounds.",
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
    ) -> None:
        """Raw G-code move (non-ArmBase API)."""
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
        self._send_cmd(cmd, wait=wait)

    def fast_move_to(self, x: Optional[float] = None, y: Optional[float] = None, z: Optional[float] = None, feedrate: int = 2000, wait: bool = True) -> None:
        """Convenience for G0 moves."""
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

                # Add timeout to prevent infinite loop if ARM doesn't respond
                start_time = time.time()
                timeout = 15.0  # 15 second timeout for position query

                while True:
                    # Check if we've exceeded timeout
                    if time.time() - start_time > timeout:
                        raise RuntimeError(f"Timeout waiting for position response from ARM (waited {timeout}s)")

                    serial_str = self.ser.readline().decode("utf-8")
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
        self.dealy_ms(value)

    def delay_s(self, value: int) -> None:
        self.dealy_s(value)

    # End-effector helpers
    def soft_gripper_pick(self) -> None:
        self._send_cmd("M1001\r")

    def soft_gripper_place(self) -> None:
        self._send_cmd("M1000\r")

    def soft_gripper_neutral(self) -> None:
        self._send_cmd("M1002\r")

    def soft_gripper_stop(self) -> None:
        self._send_cmd("M1003\r")

    def air_picker_pick(self) -> None:
        self._send_cmd("M1000\r")

    def air_picker_place(self) -> None:
        self._send_cmd("M1001\r")

    def air_picker_neutral(self) -> None:
        self._send_cmd("M1002\r")

    def air_picker_stop(self) -> None:
        self._send_cmd("M1003\r")

    def laser_on(self, value: int = 0) -> None:
        self._send_cmd("M3 S" + str(value) + '\r')

    def laser_off(self) -> None:
        self._send_cmd("M5\r")

    # Conveyor
    def conveyor_belt_forward(self, speed: int = 0) -> None:
        self._send_cmd("M2012 F" + str(speed) + 'D0\r')

    def conveyor_belt_backward(self, speed: int = 0) -> None:
        self._send_cmd("M2012 F" + str(speed) + 'D1\r')

    def conveyor_belt_stop(self) -> None:
        self._send_cmd("M2013\r")

    # Sliding rail
    def sliding_rail_init(self) -> None:
        self._send_cmd("M2005\r")

    def close(self) -> None:
        """Release the serial port."""
        self.ser.close()

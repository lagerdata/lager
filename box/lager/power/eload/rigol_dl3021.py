# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Rigol DL3021 Electronic Load implementation."""

import pyvisa
import warnings
from .eload_net import ELoadNet, DeviceNotFoundError, LibraryMissingError

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

# Suppress the pyvisa-py USBTMC warning about unexpected MsgID format
# This is a known issue with some Rigol devices and doesn't affect functionality
warnings.filterwarnings(
    "ignore",
    message="Unexpected MsgID format",
    category=UserWarning,
    module="pyvisa_py.protocols.usbtmc"
)


class RigolDL3021(ELoadNet):
    """Rigol DL3021 Electronic Load driver."""

    # Mode mapping: user-friendly names <-> SCPI commands
    # Device uses "CURR", "VOLT", "RES", "POW" for SCPI commands
    MODE_TO_SCPI = {
        "CC": "CURR",
        "CV": "VOLT",
        "CR": "RES",
        "CW": "POW",
        "CP": "POW",  # Accept both CW and CP for constant power
    }

    SCPI_TO_MODE = {
        "CURR": "CC",
        "VOLT": "CV",
        "RES": "CR",
        "POW": "CW",  # Device reports as CW
    }

    def __init__(self, net_info):
        """Initialize the Rigol DL3021."""
        self.net_info = net_info
        self.address = net_info.get("address")
        if not self.address:
            raise ValueError("No address specified for Rigol DL3021")

        self.rm = pyvisa.ResourceManager()
        self.visa_resource = None
        self._connect()

    def _connect(self):
        """Connect to the device."""
        try:
            self.visa_resource = self.rm.open_resource(self.address)
            self.visa_resource.timeout = 5000

            # Verify connection
            idn = self.visa_resource.query("*IDN?")
            if "DL3021" not in idn:
                raise DeviceNotFoundError(f"Device at {self.address} is not a Rigol DL3021: {idn}")
        except pyvisa.errors.VisaIOError as e:
            raise LibraryMissingError(f"VISA library error: {e}")
        except DeviceNotFoundError:
            raise
        except Exception as e:
            raise DeviceNotFoundError(f"Failed to connect to Rigol DL3021 at {self.address}: {e}")

    def _write(self, command):
        """Write command to device."""
        if not self.visa_resource:
            self._connect()
        self.visa_resource.write(command)

    def _query(self, command):
        """Query device."""
        if not self.visa_resource:
            self._connect()
        return self.visa_resource.query(command).strip()

    def _query_float(self, command):
        """Query and return float value."""
        response = self._query(command)
        return float(response)

    def mode(self, mode_type: str | None = None) -> str | None:
        """Set or read the electronic load operation mode."""
        if mode_type is None:
            # Query mode from device
            scpi_response = self._query(":SOURce:FUNCtion?")
            # Convert device response (CURR/VOLT/RES/POW) to user-friendly format (CC/CV/CR/CW)
            return self.SCPI_TO_MODE.get(scpi_response, scpi_response)

        mode_type = mode_type.upper()

        # Validate mode
        if mode_type not in self.MODE_TO_SCPI:
            raise ValueError(f"Invalid mode: {mode_type}. Must be CC, CV, CR, or CP/CW.")

        # Convert user mode to SCPI command (CC->CURR, CV->VOLT, etc.)
        scpi_mode = self.MODE_TO_SCPI[mode_type]
        self._write(f":SOURce:FUNCtion {scpi_mode}")
        return None

    def set_mode(self, mode):
        """Set operation mode (CC/CV/CR/CW). Legacy method for backward compatibility."""
        self.mode(mode)

    def get_mode(self):
        """Get current operation mode. Legacy method for backward compatibility."""
        return self.mode()

    def current(self, value: float | None = None) -> float | None:
        """Set or read the constant current setting."""
        if value is None:
            return self._query_float(":SOURce:CURRent:LEVel:IMMediate?")

        if value < 0:
            raise ValueError("Current must be non-negative")
        self._write(f":SOURce:CURRent:LEVel:IMMediate {value}")
        return None

    def set_current(self, current):
        """Set current level in Amps. Legacy method for backward compatibility."""
        self.current(current)

    def get_current(self):
        """Get current setting. Legacy method for backward compatibility."""
        return self.current()

    def voltage(self, value: float | None = None) -> float | None:
        """Set or read the constant voltage setting."""
        if value is None:
            return self._query_float(":SOURce:VOLTage:LEVel:IMMediate?")

        if value < 0:
            raise ValueError("Voltage must be non-negative")
        self._write(f":SOURce:VOLTage:LEVel:IMMediate {value}")
        return None

    def set_voltage(self, voltage):
        """Set voltage level in Volts. Legacy method for backward compatibility."""
        self.voltage(voltage)

    def get_voltage(self):
        """Get voltage setting. Legacy method for backward compatibility."""
        return self.voltage()

    def resistance(self, value: float | None = None) -> float | None:
        """Set or read the constant resistance setting."""
        if value is None:
            return self._query_float(":SOURce:RESistance:LEVel:IMMediate?")

        if value < 0:
            raise ValueError("Resistance must be non-negative")

        # DL3021 has a minimum resistance limit (typically 0.02Ω - 2.0Ω depending on range)
        # Values below the minimum will be silently clamped by the instrument
        if value < 0.02:
            import sys
            print(f"{RED}Warning: Resistance {value}Ω is below typical minimum (0.02Ω).{RESET}", file=sys.stderr)
            print(f"{RED}Warning: Device may clamp to its minimum value.{RESET}", file=sys.stderr)

        self._write(f":SOURce:RESistance:LEVel:IMMediate {value}")

        # Verify the value was set correctly
        actual_value = self._query_float(":SOURce:RESistance:LEVel:IMMediate?")
        if abs(actual_value - value) > 0.01:
            import sys
            print(f"{RED}Warning: Requested {value}Ω but device set to {actual_value}Ω (clamped to minimum){RESET}", file=sys.stderr)

        return None

    def set_resistance(self, resistance):
        """Set resistance level in Ohms. Legacy method for backward compatibility."""
        self.resistance(resistance)

    def get_resistance(self):
        """Get resistance setting. Legacy method for backward compatibility."""
        return self.resistance()

    def power(self, value: float | None = None) -> float | None:
        """Set or read the constant power setting."""
        if value is None:
            return self._query_float(":SOURce:POWer:LEVel:IMMediate?")

        if value < 0:
            raise ValueError("Power must be non-negative")
        self._write(f":SOURce:POWer:LEVel:IMMediate {value}")
        return None

    def set_power(self, power):
        """Set power level in Watts. Legacy method for backward compatibility."""
        self.power(power)

    def get_power(self):
        """Get power setting. Legacy method for backward compatibility."""
        return self.power()

    def enable(self) -> None:
        """Enable (turn on) the electronic load input."""
        self._write(":SOURce:INPut:STATe ON")

    def disable(self) -> None:
        """Disable (turn off) the electronic load input."""
        self._write(":SOURce:INPut:STATe OFF")

    def get_input_state(self) -> bool:
        """Get input state (enabled/disabled)."""
        response = self._query(":SOURce:INPut:STATe?")
        return response.upper() in ["ON", "1"]

    def measured_voltage(self) -> float:
        """Read the measured input voltage."""
        return self._query_float(":MEASure:VOLTage?")

    def measured_current(self) -> float:
        """Read the measured input current."""
        return self._query_float(":MEASure:CURRent?")

    def measured_power(self) -> float:
        """Read the measured input power."""
        return self._query_float(":MEASure:POWer?")

    def print_state(self) -> None:
        """Print comprehensive electronic load state."""
        mode = self.mode()
        input_state = "Enabled" if self.get_input_state() else "Disabled"

        print(f"{GREEN}Electronic Load State:{RESET}")
        print(f"{GREEN}  Mode: {mode}{RESET}")
        print(f"{GREEN}  Input: {input_state}{RESET}")
        print(f"{GREEN}  Measured Voltage: {self.measured_voltage():.3f} V{RESET}")
        print(f"{GREEN}  Measured Current: {self.measured_current():.3f} A{RESET}")
        print(f"{GREEN}  Measured Power: {self.measured_power():.3f} W{RESET}")

        if mode == "CC":
            print(f"{GREEN}  Current Setting: {self.current():.3f} A{RESET}")
        elif mode == "CV":
            print(f"{GREEN}  Voltage Setting: {self.voltage():.3f} V{RESET}")
        elif mode == "CR":
            print(f"{GREEN}  Resistance Setting: {self.resistance():.3f} Ω{RESET}")
        elif mode in ["CW", "CP"]:
            print(f"{GREEN}  Power Setting: {self.power():.3f} W{RESET}")

    def close(self) -> None:
        """Close the VISA connection and release resources."""
        if hasattr(self, 'visa_resource') and self.visa_resource is not None:
            try:
                self.visa_resource.close()
            except Exception:
                pass
            finally:
                self.visa_resource = None

    def __del__(self):
        """Cleanup when instance is garbage collected."""
        self.close()


def create_device(net_info):
    """Factory function for hardware_service.

    Creates a RigolDL3021 instance from the net_info dict.
    This allows hardware_service to instantiate the device without knowing the constructor signature.
    """
    return RigolDL3021(net_info)

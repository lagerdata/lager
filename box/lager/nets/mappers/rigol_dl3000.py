# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from lager.instrument_wrappers.rigol_dl3000_defines import Mode


class RigolDL3000FunctionMapper:
    """Mapper for Rigol DL3000 series electronic loads.

    Provides both the documented Net API methods and legacy set_*_load() methods
    for backwards compatibility.
    """

    def __init__(self, net, device):
        self.net = net
        self.device = device

    # -------------------------------------------------------------------------
    # Net API Methods (documented in eload.mdx)
    # -------------------------------------------------------------------------

    def mode(self, mode_type=None):
        """Set or get operation mode (CC/CV/CR/CW).

        Args:
            mode_type: One of 'CC', 'CV', 'CR', 'CW' (or None to read)

        Returns:
            Current mode string if mode_type is None
        """
        return self.device.mode(mode_type)

    def current(self, value=None):
        """Set or get constant current setting.

        Args:
            value: Current in Amps (or None to read)

        Returns:
            Current setting in Amps if value is None
        """
        return self.device.current(value)

    def voltage(self, value=None):
        """Set or get constant voltage setting.

        Args:
            value: Voltage in Volts (or None to read)

        Returns:
            Voltage setting in Volts if value is None
        """
        return self.device.voltage(value)

    def resistance(self, value=None):
        """Set or get constant resistance setting.

        Args:
            value: Resistance in Ohms (or None to read)

        Returns:
            Resistance setting in Ohms if value is None
        """
        return self.device.resistance(value)

    def power(self, value=None):
        """Set or get constant power setting.

        Args:
            value: Power in Watts (or None to read)

        Returns:
            Power setting in Watts if value is None
        """
        return self.device.power(value)

    def enable(self):
        """Enable the electronic load input."""
        return self.device.enable()

    def disable(self):
        """Disable the electronic load input."""
        return self.device.disable()

    def measured_voltage(self):
        """Read the measured input voltage.

        Returns:
            Measured voltage in Volts
        """
        return self.device.measured_voltage()

    def measured_current(self):
        """Read the measured input current.

        Returns:
            Measured current in Amps
        """
        return self.device.measured_current()

    def measured_power(self):
        """Read the measured input power.

        Returns:
            Measured power in Watts
        """
        return self.device.measured_power()

    def print_state(self):
        """Print comprehensive electronic load state."""
        return self.device.print_state()

    # -------------------------------------------------------------------------
    # Legacy Methods (backwards compatibility)
    # -------------------------------------------------------------------------

    def set_resistance_load(self, *, max_voltage, max_current, resistance=None):
        """Legacy: Set up constant resistance mode with limits."""
        self.device.mode('CR')
        self.set_voltage_upper_limit_all(max_voltage)
        self.set_current_upper_limit_all(max_current)
        if resistance is not None:
            self.device.resistance(resistance)

    def set_voltage_load(self, *, max_voltage, max_current, voltage=None):
        """Legacy: Set up constant voltage mode with limits."""
        self.device.mode('CV')
        self.set_voltage_upper_limit_all(max_voltage)
        self.set_current_upper_limit_all(max_current)
        if voltage is not None:
            self.device.voltage(voltage)

    def set_current_load(self, *, max_voltage, max_current, current=None, slew_rate=None):
        """Legacy: Set up constant current mode with limits."""
        self.device.mode('CC')
        self.set_voltage_upper_limit_all(max_voltage)
        self.set_current_upper_limit_all(max_current)
        if current is not None:
            self.device.current(current)
            if slew_rate is not None:
                self.set_cc_slew_rate(slew_rate)

    def set_power_load(self, *, max_voltage, max_current, power=None):
        """Legacy: Set up constant power mode with limits."""
        self.device.mode('CW')
        self.set_voltage_upper_limit_all(max_voltage)
        self.set_current_upper_limit_all(max_current)
        if power is not None:
            self.device.power(power)

    def slew_rate(self):
        """Legacy: Get CC slew rate."""
        return self.get_cc_slew_rate()

    def __getattr__(self, attr):
        return getattr(self.device, attr)

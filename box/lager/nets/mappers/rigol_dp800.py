# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

class RigolDP800FunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def set_voltage(self, voltage):
        if self.net.channel == 2:
            self.enable_sense(self.net.channel)      
        self.set_channel_voltage(voltage, self.net.channel)

    def set_current(self, current):
        if self.net.channel == 2:
            self.enable_sense(self.net.channel)
        self.set_channel_current(current, self.net.channel)

    def set_ovp(self, voltage):
        self.set_overvoltage_protection_value(voltage, self.net.channel)
        self.enable_overvoltage_protection(self.net.channel)

    def set_ocp(self, current):
        self.set_overcurrent_protection_value(current, self.net.channel)
        self.enable_overcurrent_protection(self.net.channel)

    def get_ovp_limit(self):
        """Get OVP limit as float."""
        result = self.get_overvoltage_protection_value(self.net.channel)
        return float(result) if result is not None else None

    def is_ovp(self):
        return self.overvoltage_protection_is_tripped(self.net.channel)

    def clear_ovp(self):
        self.clear_overvoltage_protection_trip(self.net.channel)

    def get_ocp_limit(self):
        """Get OCP limit as float."""
        result = self.get_overcurrent_protection_value(self.net.channel)
        return float(result) if result is not None else None

    def is_ocp(self):
        return self.overcurrent_protection_is_tripped(self.net.channel)

    def clear_ocp(self):
        self.clear_overcurrent_protection_trip(self.net.channel)

    def voltage(self):
        """Read measured voltage as float."""
        result = self.measure_voltage(self.net.channel)
        return float(result) if result is not None else None

    def current(self):
        """Read measured current as float."""
        result = self.measure_current(self.net.channel)
        return float(result) if result is not None else None

    def power(self):
        """Read measured power as float."""
        result = self.measure_power(self.net.channel)
        return float(result) if result is not None else None

    def set_mode(self, mode):
        """
            Intentional no-op, set_mode is for Keithley which can function as battery or power supply
        """
        pass

    def enable(self):
        """Enable power supply output for this channel."""
        return self.device.enable_output(self.net.channel)

    def disable(self):
        """Disable power supply output for this channel."""
        return self.device.disable_output(self.net.channel)

    def __getattr__(self, attr):
        return getattr(self.device, attr)

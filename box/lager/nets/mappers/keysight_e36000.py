# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Function mapper for Keysight E36xxx series power supplies.

Supports both E36200 series (E36233A) and E36300 series (E3631xA).
"""


class KeysightE36000FunctionMapper:
    """Mapper that translates high-level net operations to Keysight E36xxx device commands."""

    def __init__(self, net, device):
        self.net = net
        self.device = device

    def set_voltage(self, voltage):
        self.device.set_voltage(voltage, self.net.channel)

    def set_current(self, current):
        self.device.set_current(current, self.net.channel)

    def set_ovp(self, voltage):
        self.device.set_overvoltage_protection(voltage, self.net.channel)
        self.device.enable_overvoltage_protection(self.net.channel)

    def set_ocp(self, current):
        self.device.set_overcurrent_protection(current, self.net.channel)
        self.device.enable_overcurrent_protection(self.net.channel)

    def get_ovp_limit(self):
        return self.device.get_overvoltage_protection(self.net.channel)

    def is_ovp(self):
        return self.device.is_overvoltage_tripped()

    def clear_ovp(self):
        self.device.clear_overvoltage_protection(self.net.channel)

    def get_ocp_limit(self):
        return self.device.get_overcurrent_protection(self.net.channel)

    def is_ocp(self):
        return self.device.is_overcurrent_tripped()

    def clear_ocp(self):
        self.device.clear_overcurrent_protection(self.net.channel)

    def voltage(self):
        return self.device.measure_voltage(self.net.channel)

    def current(self):
        return self.device.measure_current(self.net.channel)

    def power(self):
        return self.current() * self.voltage()

    def set_mode(self, mode):
        """
        Intentional no-op, set_mode is for Keithley which can function as battery or power supply.
        """
        pass

    def __getattr__(self, attr):
        return getattr(self.device, attr)

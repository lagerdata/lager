# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from ..defines import (
    Mode,
    SimMode)

class KeithleyBatteryFunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def list_battery_models(self):
        pass

    def set_mode_battery(self):
        """Initialize Keithley 2281S to battery simulation mode"""
        # Delegate to KeithleyBattery.set_to_battery_mode()
        self.set_to_battery_mode()

    def enable_sim_output(self):
        """Enable battery simulator output"""
        # Delegate to KeithleyBattery.enable()
        self.enable()

    def disable_sim_output(self):
        """Disable battery simulator output"""
        # Delegate to KeithleyBattery.disable()
        self.disable()

    def setup_battery(self, *,
        sim_mode=None,
        soc=None,
        voc=None,
        voltage_full=None,
        voltage_empty=None,
        current_limit=None,
        capacity=None,
        model=None):
        self.set_to_battery_mode()
        self.disable()
        if sim_mode!=None:
            self.set_mode(sim_mode)

        if model!=None:
            self.set_model(model)


        if voltage_full!=None:
            self.set_volt_full(round(voltage_full,1))

        if voltage_empty!=None:
            self.set_volt_empty(voltage_empty)

        if current_limit!=None:
            self.set_current_limit(current_limit)

        if soc!=None:
            if soc:
                if 100 < soc < 0:
                    raise ValueError("SOC must be between 0 and 100")
                self.set_soc(soc)

        if voc!=None:
            self.set_voc(voc)

        if capacity!=None:
            self.set_capacity(capacity)

    # Note: All other methods (soc, voc, voltage_full, voltage_empty, capacity, current_limit,
    # mode, model, terminal_voltage, current, esr, etc.) are already implemented in KeithleyBattery
    # and will be automatically delegated via __getattr__ below.

    def __getattr__(self, attr):
        return getattr(self.device, attr)

def extract_voltage(conc):
    parts = conc.split(',')
    part = parts[1]
    if part.lower().endswith('mv'):
        return float(part[:-2]) / 1000.0
    elif part.lower().endswith('v'):
        return float(part[:-1])
    else:
        raise ValueError(f'Invalid voltage value {part}')

def extract_current(conc):
    parts = conc.split(',')
    part = parts[0]
    if part.lower().endswith('ma'):
        return float(part[:-2]) / 1000.0
    elif part.lower().endswith('a'):
        return float(part[:-1])
    else:
        raise ValueError(f'Invalid current value {part}')

class KeithleyPowerSupplyFunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def set_voltage(self, voltage):
        """Set voltage. Maps to device.voltage(value)."""
        self.device.voltage(voltage)

    def set_current(self, current):
        """Set current limit. Maps to device.current(value)."""
        self.device.current(current)

    def enable(self):
        """Enable power supply output."""
        return self.device.enable()

    def disable(self):
        """Disable power supply output."""
        return self.device.disable()

    def init_continuous(self):
        """Initialize continuous mode (no-op for Keithley, called by Net.enable())."""
        pass

    def voltage(self):
        """Read voltage (measured if enabled, setpoint if disabled)."""
        if hasattr(self.device, 'output_is_enabled') and self.device.output_is_enabled():
            return float(self.device.measure_voltage())
        else:
            # Return setpoint when output is disabled
            return float(self.device.get_channel_voltage())

    def current(self):
        """Read current (measured if enabled, setpoint if disabled)."""
        if hasattr(self.device, 'output_is_enabled') and self.device.output_is_enabled():
            return float(self.device.measure_current())
        else:
            # Return setpoint when output is disabled
            return float(self.device.get_channel_current())

    def power(self):
        """Read measured power."""
        return float(self.device.measure_power())

    def set_ovp(self, voltage):
        """Set over-voltage protection."""
        self.device.ovp(voltage)

    def get_ovp_limit(self):
        """Get OVP limit."""
        # Read by calling ovp() without arguments - it prints, need to capture differently
        # For now, use the device method that returns a value
        return float(self.device.get_overvoltage_protection_value())

    def is_ovp(self):
        """Check if OVP is tripped."""
        return self.device.overvoltage_protection_is_tripped()

    def clear_ovp(self):
        """Clear OVP trip."""
        self.device.clear_ovp()

    def set_ocp(self, current):
        """Set over-current protection."""
        self.device.ocp(current)

    def get_ocp_limit(self):
        """Get OCP limit."""
        return float(self.device.get_overcurrent_protection_value())

    def is_ocp(self):
        """Check if OCP is tripped."""
        return self.device.overcurrent_protection_is_tripped()

    def clear_ocp(self):
        """Clear OCP trip."""
        self.device.clear_ocp()

    def __getattr__(self, attr):
        return getattr(self.device, attr)



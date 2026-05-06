# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import pyvisa

CONDITION_OVP = 0x1
CONDITION_OCP = 0x2
CONDITION_OPP = 0x4

class EAMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def set_voltage(self, voltage):
        self.set_source_voltage(voltage)

    def set_current(self, current):
        self.set_source_current(current)

    def set_ovp(self, voltage):
        self.set_source_voltage_protection(voltage)

    def set_ocp(self, current):
        self.set_source_current_protection(current)

    def set_opp(self, power):
        self.set_source_power_protection(power)

    def get_ovp_limit(self):
        return self.get_source_voltage_protection()

    def is_ovp(self):
        register = int(self.get_condition_register())
        return bool(register & CONDITION_OVP)

    def clear_supply_ovp(self):
        return self.supply_ovoltage_count()

    def get_source_ocp_limit(self):
        return self.get_source_current_protection()

    def is_ocp(self):
        register = int(self.get_condition_register())
        return bool(register & CONDITION_OCP)

    def clear_supply_ocp(self):
        return self.supply_ocurrent_count()

    def is_opp(self):
        register = int(self.get_condition_register())
        return bool(register & CONDITION_OPP)

    def clear_supply_opp(self):
        return self.supply_opower_count()

    def voltage(self):
        return self.measure_voltage()

    def current(self):
        return self.measure_current()

    def power(self):
        return self.measure_power()

    def set_mode(self, mode):
        """
            Intentional no-op, set_mode is for Keithley which can function as battery or power supply
        """
        pass

    def set_resistance_load(self, *, ocp, opp, resistance, current):
        self.enable_resistance_mode()
        self.set_sink_current_protection(ocp)
        self.set_sink_power_protection(opp)
        self.set_sink_resistance(resistance)
        self.set_sink_current(current)

    def slew_rate(self):
        raise NotImplementedError

    def resistance(self):
        raise NotImplementedError

    def clear_sink_opp(self):
        return self.sink_opower_count()

    def clear_sink_ocp(self):
        return self.sink_ocurrent_count()

    def start_pv_mode(self):
        self.disable_resistance_mode()
        self.start_photovoltaic_sim()

    def stop_pv_mode(self):
        state = self.get_photovoltaic_sim_state()
        if state != 'STOP':
            self.stop_photovoltaic_sim()
        self.enable_resistance_mode()

    def __getattr__(self, attr):
        return getattr(self.device, attr)
    
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Net-level API for an EA PSB supply net (``Net.get(name, NetType.PowerSupply)``).

Every call here ends up on ``lager.power.supply.ea.EA`` through the device
proxy, so each method names a method that driver actually defines. A test
checks that (test/unit/box/test_ea_mapper_targets.py).

EA PSB units are bidirectional (source and sink) and have a photovoltaic
simulator, but the supply driver only drives the source side. Sink and
power-protection calls are refused here with a clear error; the PV simulator
is the ``solar`` net (``lager.power.solar``).
"""


def _unsupported(what):
    return NotImplementedError(
        f"{what} is not supported on an EA supply net through the Python API"
    )


class EAMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    # ----------------------------- setpoints -----------------------------

    def set_voltage(self, voltage):
        self.device.voltage(value=voltage)

    def set_current(self, current):
        self.device.current(value=current)

    def set_ovp(self, voltage):
        self.device.ovp(value=voltage)

    def set_ocp(self, current):
        self.device.ocp(value=current)

    def set_opp(self, power):
        raise _unsupported("Over-power protection")

    # ----------------------------- protection ----------------------------

    def get_ovp_limit(self):
        return self.device.get_overvoltage_protection_value()

    def get_ocp_limit(self):
        return self.device.get_overcurrent_protection_value()

    # Old name, kept so existing scripts keep working.
    get_source_ocp_limit = get_ocp_limit

    def is_ovp(self):
        return bool(self.device.overvoltage_protection_is_tripped())

    def is_ocp(self):
        return bool(self.device.overcurrent_protection_is_tripped())

    def is_opp(self):
        raise _unsupported("Over-power protection")

    def clear_ovp(self):
        return self.device.clear_ovp()

    def clear_ocp(self):
        return self.device.clear_ocp()

    # Old names, kept so existing scripts keep working.
    clear_supply_ovp = clear_ovp
    clear_supply_ocp = clear_ocp

    def clear_supply_opp(self):
        raise _unsupported("Over-power protection")

    # ---------------------------- measurements ---------------------------

    def voltage(self):
        return self.device.measure_voltage()

    def current(self):
        return self.device.measure_current()

    def power(self):
        return self.device.measure_power()

    # ------------------------------- output ------------------------------

    def enable(self):
        self.device.enable()

    def disable(self):
        self.device.disable()

    def set_mode(self, mode=None):
        """Intentional no-op; set_mode is for the Keithley, which can act as a battery or a supply."""
        pass

    # --------------------------- not supported ---------------------------

    def set_resistance_load(self, *, ocp, opp, resistance, current):
        raise _unsupported("Sink (electronic load) mode")

    def slew_rate(self):
        raise _unsupported("Slew rate")

    def resistance(self):
        raise _unsupported("Sink resistance")

    def clear_sink_opp(self):
        raise _unsupported("Sink (electronic load) mode")

    def clear_sink_ocp(self):
        raise _unsupported("Sink (electronic load) mode")

    def start_pv_mode(self):
        raise _unsupported("PV simulation (use a solar net)")

    def stop_pv_mode(self):
        raise _unsupported("PV simulation (use a solar net)")

    def __getattr__(self, attr):
        return getattr(self.device, attr)

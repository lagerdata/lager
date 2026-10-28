# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Yepkit YKUSH3 5V power output as a power-supply net.

Besides its three switchable USB ports, the YKUSH3 has a switchable 5V output
for non-USB loads. It is not a USB port, so it is driven as a power-supply net
rather than a ``usb`` one. The output is fixed at 5V and the hub measures
nothing, so only on/off and its state are real: setting 5V is accepted as a
no-op, and every other setpoint or protection call is refused.

Every operation goes through ``YKUSHUSBNet._with_device``, which opens the hub
fresh, runs under the hub's cross-process lock and releases the handle. The
lock is keyed on the hub's address, so switching the 5V output serialises
with the ``usb`` nets on the same YKUSH3.
"""

from __future__ import annotations

from lager.automation.usb_hub import ykush as _ykush
from lager.automation.usb_hub.usb_net import PortStateError
from lager.exceptions import SupplyBackendError

from .supply_net import SupplyNet, _safe

GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

NOMINAL_VOLTAGE = 5.0
# A requested voltage this close to 5V counts as asking for 5V.
_VOLTAGE_TOLERANCE = 1e-3


def _unsupported(what: str) -> SupplyBackendError:
    return SupplyBackendError(
        f"{what} is not supported on the YKUSH3 5V output "
        f"(a fixed {NOMINAL_VOLTAGE:g}V output that can only be switched on or off)."
    )


class YKUSH3PowerOutput(SupplyNet):
    """The YKUSH3's fixed 5V output. ``channel`` is accepted for interface
    compatibility; the hub has exactly one such output."""

    def __init__(self, address: str | None = None, channel: int | str = 1) -> None:
        self.address = address
        self.channel = channel
        self._hub = _ykush.YKUSHUSBNet({"address": address})

    # ----------------------------------------------------------------
    #  hub access
    # ----------------------------------------------------------------
    def _switch(self, on: bool) -> None:
        def _do(dev):
            ok = dev.set_power_output_on() if on else dev.set_power_output_off()
            if not ok:
                raise SupplyBackendError(
                    f"YKUSH3 did not accept the 5V output {'on' if on else 'off'} command")

        self._hub._with_device(_do, what="5v-on" if on else "5v-off")

    def output_is_enabled(self, channel=None) -> bool:
        """Live 5V output state. pykush answers 255 when the hub did not
        answer, which raises rather than reading as on or off."""
        def _read(dev):
            raw = dev.get_power_output_state()
            if raw == _ykush._PORT_UP:
                return True
            if raw == _ykush._PORT_DOWN:
                return False
            raise PortStateError(f"YKUSH3 5V output state unreadable (got {raw!r})")

        return self._hub._with_device(_read, what="5v-state")

    def output_state(self, channel=None):
        return _safe(lambda: self.output_is_enabled(channel))

    # ----------------------------------------------------------------
    #  granular methods (HTTP, WebSocket and Python API paths)
    # ----------------------------------------------------------------
    def enable_output(self, channel=None) -> None:
        self._switch(True)

    def disable_output(self, channel=None) -> None:
        self._switch(False)

    def set_channel_voltage(self, value: float, source=None) -> None:
        if abs(float(value) - NOMINAL_VOLTAGE) > _VOLTAGE_TOLERANCE:
            raise SupplyBackendError(
                f"The YKUSH3 5V output is fixed at {NOMINAL_VOLTAGE:g}V; "
                f"cannot set {value}V.")

    def get_channel_voltage(self, source=None, channel=None) -> float:
        return NOMINAL_VOLTAGE

    def set_channel_current(self, value: float, source=None) -> None:
        raise _unsupported("Setting a current limit")

    def get_channel_current(self, source=None, channel=None) -> float:
        raise _unsupported("A current setpoint")

    def measure_voltage(self, channel=None) -> float:
        raise _unsupported("Measuring voltage")

    def measure_current(self, channel=None) -> float:
        raise _unsupported("Measuring current")

    def measure_power(self, channel=None) -> float:
        raise _unsupported("Measuring power")

    def get_channel_limits(self, channel=None) -> dict:
        # Every limit is 0, "none reported", so the supply handler's range
        # checks never fire and each request reaches this driver, which
        # refuses what a fixed output cannot do with a message that says so.
        # A 5V voltage_max made the handler refuse 6V as "exceeds hardware
        # limit 5.0V" and, as the default ovp_max, OVP 6 as "exceeds the OVP
        # maximum", both implying a range that does not exist. The TUI's
        # displayed maximum comes from get_monitor_state, not from here.
        return {'voltage_max': 0, 'current_max': 0, 'ovp_max': 0, 'ocp_max': 0}

    def set_overcurrent_protection_value(self, value: float, channel=None) -> None:
        raise _unsupported("Over-current protection")

    def get_overcurrent_protection_value(self, channel=None) -> float:
        raise _unsupported("Over-current protection")

    def enable_overcurrent_protection(self, channel=None) -> None:
        raise _unsupported("Over-current protection")

    def overcurrent_protection_is_tripped(self, channel=None) -> bool:
        raise _unsupported("Over-current protection")

    def clear_overcurrent_protection_trip(self, channel=None) -> None:
        raise _unsupported("Over-current protection")

    def set_overvoltage_protection_value(self, value: float, channel=None) -> None:
        raise _unsupported("Over-voltage protection")

    def get_overvoltage_protection_value(self, channel=None) -> float:
        raise _unsupported("Over-voltage protection")

    def enable_overvoltage_protection(self, channel=None) -> None:
        raise _unsupported("Over-voltage protection")

    def overvoltage_protection_is_tripped(self, channel=None) -> bool:
        raise _unsupported("Over-voltage protection")

    def clear_overvoltage_protection_trip(self, channel=None) -> None:
        raise _unsupported("Over-voltage protection")

    def get_monitor_state(self, channel=None) -> dict:
        """The supply TUI's monitor state. The hub measures nothing, so the
        measurements are None ("n/a"), never a made-up 5V or 0A; only the
        on/off state is read from the hub."""
        return {
            'voltage': None,
            'current': None,
            'power': None,
            'enabled': self.output_state(channel),
            'mode': 'CV',
            'voltage_set': NOMINAL_VOLTAGE,
            'current_set': None,
            'voltage_max': NOMINAL_VOLTAGE,
            'current_max': 0,
            'ocp_limit': None,
            'ocp_tripped': None,
            'ovp_limit': None,
            'ovp_tripped': None,
        }

    # ----------------------------------------------------------------
    #  SupplyNet interface (in-process dispatcher)
    # ----------------------------------------------------------------
    def voltage(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        if ocp is not None:
            raise _unsupported("Over-current protection")
        if ovp is not None:
            raise _unsupported("Over-voltage protection")
        if value is not None:
            self.set_channel_voltage(value)
            print(f"{GREEN}Voltage set to: {NOMINAL_VOLTAGE:.4f}V (fixed){RESET}")
            return
        print(f"{GREEN}Voltage: {NOMINAL_VOLTAGE:.4f} (fixed, not measured){RESET}")

    def current(self, value: float | None = None,
                ocp: float | None = None,
                ovp: float | None = None) -> None:
        raise _unsupported("Current control")

    def enable(self) -> None:
        self.enable_output()

    def disable(self) -> None:
        self.disable_output()

    def set_mode(self) -> None:
        # Always a DC output; nothing to switch.
        return

    def clear_ocp(self) -> None:
        raise _unsupported("Over-current protection")

    def clear_ovp(self) -> None:
        raise _unsupported("Over-voltage protection")

    def ocp(self, value: float | None = None) -> None:
        raise _unsupported("Over-current protection")

    def ovp(self, value: float | None = None) -> None:
        raise _unsupported("Over-voltage protection")

    def state(self) -> None:
        enabled = self.output_is_enabled()
        print(f"{GREEN}Channel: 1{RESET}")
        print(f"{GREEN}Enabled: {'ON' if enabled else 'OFF'}{RESET}")
        print(f"{GREEN}Voltage: {NOMINAL_VOLTAGE} (fixed, not measured){RESET}")
        print(f"{GREEN}Current: n/a (not measured){RESET}")


def create_device(net_info: dict) -> YKUSH3PowerOutput:
    """Factory used by hardware_service.py:/invoke to instantiate this driver.

    ``net_info`` carries {address, channel, instrument} from
    ``lager.dispatchers.helpers.resolve_net_proxy``, or the whole saved net
    record (with ``pin``, no ``channel``) from the Python API.
    """
    return YKUSH3PowerOutput(address=net_info.get("address"),
                             channel=net_info.get("channel") or net_info.get("pin") or 1)

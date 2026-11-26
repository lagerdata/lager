# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""GPIO, ADC and DAC nets, for a container whose instruments are on the host.

On a Linux Box a script's ``Net.get('x', NetType.GPIO)`` returns the driver
itself, which opens the LabJack (or other adapter) over USB from the script.
Where the container has no USB, these stand-ins are returned instead. Each
call goes to the hardware service's own adapter for that role (``gpio_hs``,
``adc_hs``, ``dac_hs``) -- the route ``lager gpi``, ``lager adc`` and
``lager dac`` already take -- which on this kind of Box runs in the host
agent, under the same per-device lock as every other caller.

What each call sends is what that route sends, built by the same function
(``device_identity.hardware_service_net_info``): the net's name, its
instrument and the physical device's lock key, never the saved address. The
hardware service keys its device cache on an address when it is given one,
so sending it would hand every net on one instrument the first net's adapter.

Used only when the container is started with ``LAGER_HOSTLINK=1``; a Linux
Box never imports this.

A failure is raised as ``lager.nets.device.DeviceError`` carrying the
driver's message, not as the driver's own exception class.
"""
from __future__ import annotations

from ..io.adc.adc_net import ADCBase
from ..io.dac.dac_net import DACBase
from ..io.gpio.gpio_net import GPIOBase
from ..nets.device import Device
from ..nets.device_identity import hardware_service_net_info

# Added to a wait's own timeout so the wait, not the HTTP call, gives up first.
_WAIT_MARGIN_S = 10.0
# With no timeout the driver waits until the level arrives.
_NO_TIMEOUT_S = 24 * 3600.0


def _pin(net_info):
    pin = net_info.get("pin")
    return net_info.get("channel") if pin is None else pin


class HostGPIO(GPIOBase):
    def __init__(self, name, net_info):
        super().__init__(name, _pin(net_info))
        self._sent = hardware_service_net_info("gpio", name, net_info)
        self._device = Device("gpio_hs", self._sent)

    def input(self) -> int:
        return int(self._device.input())

    def output(self, level) -> None:
        self._device.output(level)

    def wait_for_level(self, level, timeout=None, poll_interval=0.01, **kwargs):
        budget = _NO_TIMEOUT_S if timeout is None else float(timeout) + _WAIT_MARGIN_S
        device = Device("gpio_hs", self._sent, timeout=budget)
        return float(device.wait_for_level(level, timeout=timeout,
                                           poll_interval=poll_interval, **kwargs))


class HostADC(ADCBase):
    def __init__(self, name, net_info):
        super().__init__(name, _pin(net_info))
        self._device = Device("adc_hs", hardware_service_net_info("adc", name, net_info))

    def input(self) -> float:
        return float(self._device.input())


class HostDAC(DACBase):
    def __init__(self, name, net_info):
        super().__init__(name, _pin(net_info))
        self._device = Device("dac_hs", hardware_service_net_info("dac", name, net_info))

    def get_voltage(self) -> float:
        return float(self._device.input())

    def input(self) -> float:
        return self.get_voltage()

    def output(self, voltage) -> None:
        self._device.output(voltage)


_STAND_INS = {"gpio": HostGPIO, "adc": HostADC, "dac": HostDAC}


def for_role(role, name, net_info):
    """The stand-in for a saved net of ``role`` (its saved name), or None."""
    stand_in = _STAND_INS.get(role)
    return stand_in(name, net_info) if stand_in else None

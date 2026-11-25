# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""SPI and I2C nets, for a container whose adapters are on the host.

On a Linux Box a script's ``Net.get('x', NetType.SPI)`` returns an ``SPINet``
whose every call asks the SPI dispatcher for the driver, and the dispatcher
opens the adapter (LabJack, Aardvark, FTDI) over USB from the script. The
same holds for I2C.

Where the container has no USB, the script still gets the Box's own
``SPINet`` / ``I2CNet``, unedited. What changes is the driver the dispatcher
hands out in that script's process: ``install()`` replaces each dispatcher's
``_resolve_net_and_driver`` with one that returns a stand-in driver. Each
call on the stand-in goes to the hardware service's own adapter for the role
(``spi_hs``, ``i2c_hs``) -- the route ``lager spi`` and ``lager i2c`` already
take -- which on this kind of Box runs in the host agent.

Used only when the container is started with ``LAGER_HOSTLINK=1``; a Linux
Box never imports this. The dispatcher files themselves are not edited.

A driver failure is raised as the dispatcher's own error class
(``SPIBackendError`` / ``I2CBackendError``) carrying the driver's message.
"""
from __future__ import annotations

import threading

from ..nets.device import Device, DeviceError

_lock = threading.Lock()
_drivers = {}
_installed = False


def _call(error_class, fn, *args):
    try:
        return fn(*args)
    except DeviceError as exc:
        raise error_class(str(exc)) from exc


class HostSPIDriver:
    """What the SPI dispatcher and ``SPINet`` use of a driver."""

    def __init__(self, net_info, error_class):
        self._device = Device("spi_hs", dict(net_info))
        self._error = error_class
        self._word_size = int((net_info.get("params") or {}).get("word_size", 8))
        self.clamp_warnings = []

    def config(self, **overrides):
        result = _call(self._error, self._device.config, overrides or None)
        self.clamp_warnings = list((result or {}).get("warnings") or [])
        if overrides.get("word_size") is not None:
            self._word_size = int(overrides["word_size"])

    def _words(self, result):
        self._word_size = int(result.get("word_size", self._word_size))
        return [int(w) for w in result.get("words") or []]

    def read(self, n_words, fill=0xFF, keep_cs=False):
        return self._words(_call(self._error, self._device.read,
                                 n_words, fill, keep_cs, None))

    def read_write(self, data, keep_cs=False):
        return self._words(_call(self._error, self._device.read_write,
                                 list(data or []), keep_cs, None))

    def transfer(self, n_words, data=None, fill=0xFF, keep_cs=False):
        return self._words(_call(self._error, self._device.transfer,
                                 list(data or []), n_words, fill, keep_cs, None))

    def write(self, data, keep_cs=False):
        self.read_write(data, keep_cs=keep_cs)


class HostI2CDriver:
    """What the I2C dispatcher and ``I2CNet`` use of a driver.

    Overrides given when the driver is asked for are sent with every call,
    which is where the hardware service's adapter takes them.
    """

    def __init__(self, net_info, error_class):
        self._device = Device("i2c_hs", dict(net_info))
        self._error = error_class
        self._overrides = {}
        self.clamp_warnings = []

    def _apply(self, **overrides):
        self._overrides.update(overrides)

    def config(self, frequency_hz=None, pull_ups=None):
        result = _call(self._error, self._device.config, frequency_hz, pull_ups)
        self.clamp_warnings = list((result or {}).get("warnings") or [])

    def scan(self, start_addr=0x08, end_addr=0x77):
        return [int(a) for a in _call(self._error, self._device.scan,
                                      start_addr, end_addr, self._overrides or None)]

    def read(self, address, num_bytes):
        return [int(b) for b in _call(self._error, self._device.read,
                                      address, num_bytes, self._overrides or None)]

    def write(self, address, data):
        _call(self._error, self._device.write, address, list(data or []),
              self._overrides or None)

    def write_read(self, address, data, num_bytes):
        return [int(b) for b in _call(
            self._error, self._device.write_read, address, list(data or []),
            num_bytes, self._overrides or None)]


def _i2c_overrides(overrides):
    return {k: overrides[k] for k in ("frequency_hz", "pull_ups") if k in overrides}


def _resolver(dispatcher, kind, driver_class, error_class, apply):
    """A ``_resolve_net_and_driver`` that hands out stand-in drivers.

    Follows the dispatcher's own: the net must exist and have the role, the
    driver is kept for the life of the process, and overrides are applied to
    it. The Box's script runner drops the hardware service's drivers before
    each script, so the host's driver starts from the saved configuration
    just as a script's own driver does on Linux.
    """
    def resolve(netname, overrides=None):
        overrides = overrides or {}
        with _lock:
            driver = _drivers.get((kind, netname))
            if driver is None:
                rec = dispatcher.helpers.find_saved_net(netname, error_class)
                dispatcher.helpers.ensure_role(rec, dispatcher.ROLE, error_class)
                driver = driver_class(rec, error_class)
                _drivers[(kind, netname)] = driver
        if overrides:
            apply(driver, overrides)
        return driver

    return resolve


def install() -> None:
    """Point this process's SPI and I2C dispatchers at the host. Idempotent."""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    from ..exceptions import I2CBackendError, SPIBackendError
    from ..protocols.i2c import dispatcher as i2c
    from ..protocols.spi import dispatcher as spi

    from ..protocols import i2c as i2c_package, spi as spi_package

    spi._resolve_net_and_driver = _resolver(
        spi, "spi", HostSPIDriver, SPIBackendError,
        lambda driver, overrides: driver.config(**overrides))
    i2c._resolve_net_and_driver = _resolver(
        i2c, "i2c", HostI2CDriver, I2CBackendError,
        lambda driver, overrides: driver._apply(**_i2c_overrides(overrides)))
    # The packages re-export the name; keep the two in step.
    spi_package._resolve_net_and_driver = spi._resolve_net_and_driver
    i2c_package._resolve_net_and_driver = i2c._resolve_net_and_driver

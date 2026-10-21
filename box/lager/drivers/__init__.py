# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.drivers - Custom drivers: instruments whose code lives on the box only.

A company that cannot publish an instrument driver installs it on its box as
a bundle (``lager drivers install``)::

    stepper/
      lager_driver.json   # manifest: type, params, actions (see manifest.py)
      driver.py           # class Driver(CustomDriver)

A net whose role is the driver type then works like a built-in net::

    lager stepper RIG_1 jog --seconds 3 --box B

The stock CLI builds that command from the manifest the box serves at
GET :9000/drivers, so no user installs anything extra. Each call runs the
driver in its own process (lager.drivers.runner), so a driver that crashes or
hangs cannot take the box HTTP server down with it.

Writing a driver::

    from lager.drivers import CustomDriver, action

    class Driver(CustomDriver):
        def setup(self):
            self.port = self.base_net("uart_net").connect(timeout=2)

        @action
        def jog(self, seconds):
            ...
            return {"message": "jogged %ss" % seconds}

        def teardown(self):
            self.port.close()

Using one from a ``lager python`` script::

    from lager.drivers import open_driver

    with open_driver("RIG_1") as rig:
        rig.jog(seconds=3)
"""

import contextlib
import logging

from lager.drivers.errors import (  # noqa: F401  (re-exported)
    DriverBusy,
    DriverFailed,
    DriverTimeout,
    DriverUnavailable,
)
from lager.drivers.manifest import ManifestError  # noqa: F401


def action(fn):
    """Mark a Driver method as callable from the CLI and /net/command.

    Only marked methods run, so a helper on the class cannot be invoked by
    naming it as an action.
    """
    fn._lager_action = True
    return fn


class CustomDriver:
    """Base class for a custom driver's ``Driver``.

    Attributes:
        net:     the saved net record (dict) this instance drives
        name:    the net's name
        params:  the net's params, typed and validated against the manifest
        log:     a logger; its output goes to the box log, not the CLI
    """

    def __init__(self, net, params=None, base_nets=None):
        self.net = net
        self.name = net.get('name')
        self.params = dict(params if params is not None else (net.get('params') or {}))
        self._base_nets = dict(base_nets or {})
        self.log = logging.getLogger('lager.drivers.%s' % net.get('role'))

    def base_net(self, param):
        """The built-in net named by a ``"type": "net"`` param, as a Net object.

        For a UART net this is a UARTNet: ``.connect()`` returns an open
        pyserial port.
        """
        rec = self._base_nets.get(param)
        if rec is None:
            raise KeyError("param '%s' does not name a net" % param)
        from lager.nets.net import Net, NetType
        return Net.get(rec['name'], NetType.from_role(rec['role']))

    def setup(self):
        """Called once before the action. Open ports here."""

    def teardown(self):
        """Called once after the action, even when it raised. Close ports here."""


@contextlib.contextmanager
def open_driver(netname):
    """Load the driver for a custom net in this process, for scripts.

    Runs ``setup()`` on entry and ``teardown()`` on exit. Inside the block,
    call actions as methods: ``rig.jog(seconds=3)``.
    """
    from lager.drivers import loader
    instance = loader.instance_for_net(netname)
    instance.setup()
    try:
        yield instance
    finally:
        instance.teardown()


__all__ = [
    'CustomDriver',
    'action',
    'open_driver',
    'ManifestError',
    'DriverBusy',
    'DriverFailed',
    'DriverTimeout',
    'DriverUnavailable',
]

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import enum
import os
import pyvisa
from string import digits
from contextlib import closing
import time

try:
    from Phidget22.Devices.Encoder import Encoder
except ModuleNotFoundError:
    pass

"""
:meta private:
"""

class Rotation:
    """
        Class for managing access to rotation encoder
    """

    def __init__(self, name, pin, location):
        self._name = name
        self._pin = pin
        if location.startswith('phidget'):
            _, serial, port = location.split(':')
            serial = int(serial, 10)
            port = int(port, 10)
            self._kind = 'phidget'
            self.encoder = Encoder()
            self.encoder.setHubPort(port)
            self.encoder.setDeviceSerialNumber(serial)
            self.encoder.openWaitForAttachment(5000)
        else:
            raise RuntimeError('Unsupported rotation device')


    @property
    def name(self):
        return self._name

    @property
    def pin(self):
        return self._pin

    def __str__(self):
        return f'<lager.Rotation name="{self.name}" pin={self.pin}>'

    def read(self):
        """
        Read the value for the encoder

        ``position = my_rotation_net.read()``
        

        """
        if self._kind == 'phidget':
            value = self.encoder.getPosition()
            return {'position': value}
        raise RuntimeError(f'Invalid kind {self._kind}')

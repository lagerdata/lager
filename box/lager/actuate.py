# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

import enum
import os
import pyvisa
from string import digits
from contextlib import closing
import time
from .automation.arm.rotrics import Dexarm

"""
:meta private:
"""

class Actuate:
    """
        Class for managing actuation
    """

    def __init__(self, name, pin, location):
        self._name = name
        self._pin = pin
        self._location = location

    @property
    def name(self):
        return self._name

    @property
    def pin(self):
        return self._pin

    def __str__(self):
        return f'<lager.Actuate name="{self.name}" pin={self.pin}>'

    def actuate(self):
        """
            Move to the specified position
        """
        with Dexarm(serial_number=self._location.get('serial_number')) as arm:
            x, y, z = self._location['position']
            # move_to, not move_to_blocking: it refuses a position outside the
            # workspace and firmware older than V2.1.4 (which swaps X and Y)
            # before anything is sent, and its timeout covers a full traverse.
            arm.move_to(x, y, z)

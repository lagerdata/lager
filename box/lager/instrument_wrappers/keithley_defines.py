# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from .visa_enum import VisaEnum

class Mode(VisaEnum):
    Entry = ("ENTR")
    PowerSupply = ("POW")
    BatteryTest = ("TEST")
    BatterySimulator = ("SIM")

class SimMethod(VisaEnum):
    Static = ("STAT")
    Dynamic = ("DYN")

class ConstantCurrentMode(Exception):
    def __str__(self):
        return "The output is in Constant Current mode"

    def __repr__(self):
        return str(self)

class ConstantVoltageMode(Exception):
    def __str__(self):
        return "The output is in Constant Voltage mode"

    def __repr__(self):
        return str(self)

class OvercurrentProtectionError(Exception):
    def __str__(self):
        return "The overcurrent protection has tripped"

    def __repr__(self):
        return str(self)

class OvervoltageProtectionError(Exception):
    def __str__(self):
        return "The overvoltage protection has tripped"

    def __repr__(self):
        return str(self)

class OvertemperatureProtectionError(Exception):
    def __str__(self):
        return "The overtemperature protection has tripped"

    def __repr__(self):
        return str(self)

class SenseLeadsReversedError(Exception):
    def __str__(self):
        return "The sense leads are reversed"

    def __repr__(self):
        return str(self)
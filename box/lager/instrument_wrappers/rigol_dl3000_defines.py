# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from .visa_enum import VisaEnum

class Mode(VisaEnum):
    ConstantCurrent = ("CURR", "CC")
    ConstantResistance = ("RES", "CR")
    ConstantVoltage = ("VOLT", "CV")
    ConstantPower = ("POW", "CP")

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from .visa_enum import VisaEnum

class OCPDelayStartMode(VisaEnum):
    SCHANGE = "SChange"
    CCTRANS = "CCTRans"

class InstrumentChannel(VisaEnum):
    CH1 = "CH1"
    CH2 = "CH2"
    CH3 = "CH3"

    def to_numeric(self):
        return 1 if self == InstrumentChannel.CH1 else 2

    @classmethod
    def from_numeric(cls, value):
        if str(value).strip() == "1":
            return cls.CH1
        elif str(value).strip() == "2":
            return cls.CH2
        else:
            raise ValueError(f"Unknown numeric InstrumentChannel: {value}")

class ApplySpecialValue(VisaEnum):
    MIN = "MINimum"
    MAX = "MAXimum"
    DEF = "DEFault"

class StatusCondition(VisaEnum):
    OFF_OR_UNREGULATED = 0  # The output is off or unregulated
    CONSTANT_CURRENT = 1    # The output is in CC (constant current) mode
    CONSTANT_VOLTAGE = 2    # The output is in CV (constant voltage) mode
    HARDWARE_FAILURE = 3    # The output has a hardware failure

    @classmethod
    def from_cmd(cls, cmd):
        return cls(int(cmd))

class VoltageRange(VisaEnum):
    P6V = "P6V"
    P8V = "P8V"
    P25V = "P25V"
    P20V = "P20V"
    P50V = "P50V"
    N25V = "N25V"
    LOW = "LOW"
    HIGH = "HIGH"
        
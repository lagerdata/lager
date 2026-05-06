# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from .visa_enum import VisaEnum

class MathFunction(VisaEnum):
    NoFunction = ("NONE")
    Relative = ("REL")
    dB = ("DB")
    dBm = ("DBM")
    Minimum = ("MIN")
    Maximum = ("MAX")
    Average = ("AVERAGE")
    Total = ("TOTAL")
    PF = ("PF")

class FunctionRange(VisaEnum):
    Minimum = ("MIN")
    Maximum = ("MAX")
    Default = ("DEF")

class PFResult(VisaEnum):
    Pass = ("PASS")
    High = ("HI")
    Low = ("LO")

class Function(VisaEnum):
    DCVoltage = ("VOLTage:DC", "DCV")
    ACVoltage = ("VOLTage:AC", "ACV")
    DCCurrent = ("CURRent:DC", "DCI")
    ACCurrent = ("CURRent:AC", "ACI")
    Resistance = ("RESistance", "2WR")
    Capacitance = ("CAPacitance", "CAP")
    Continuity = ("CONTinuity", "CONT")
    FResistance = ("FRESistance", "4WR")
    Diode = ("DIODe", "DIODE")
    Frequency = ("FREQuency", "FREQ")
    Period = ("PERiod", "PERI")

class MeasurementMode(VisaEnum):
    Auto = ("AUTO")
    Manual = ("MANU")

class DCVoltageRange(VisaEnum):
    Range_200mV = (0)
    Range_2V = (1)
    Range_20V = (2)
    Range_200V = (3)
    Range_1000V = (4)
    Minimum = ("MIN")
    Maximum = ("MAX")
    Default = ("DEF")

class ACVoltageRange(VisaEnum):
    Range_200mV = (0)
    Range_2V = (1)
    Range_20V = (2)
    Range_200V = (3)
    Range_750V = (4)
    Minimum = ("MIN")
    Maximum = ("MAX")
    Default = ("DEF")

class DCCurrentRange(VisaEnum):
    Range_200uA = (0)
    Range_2mA = (1)
    Range_20mA = (2)
    Range_200mA = (3)
    Range_2A = (4)
    Range_10A = (5)
    Minimum = ("MIN")
    Maximum = ("MAX")
    Default = ("DEF")

class ACCurrentRange(VisaEnum):
    Range_20mA = (0)
    Range_200mA = (1)
    Range_2A = (2)
    Range_10A = (3)
    Minimum = ("MIN")
    Maximum = ("MAX")
    Default = ("DEF")

class DCImpedance(VisaEnum):
    Impedance_10Meg = ("10M")
    Impedance_10Gig = ("10G")

class ResistanceRange(VisaEnum):
    Range_200Ohm = (0)
    Range_2kOhm = (1)
    Range_20kOhm = (2)
    Range_200kOhm = (3)
    Range_1MOhm = (4)
    Range_10MOhm = (5)
    Range_100MOhm = (6)
    Minimum = ("MIN")
    Maximum = ("MAX")
    Default = ("DEF")

class CapacitanceRange(VisaEnum):
    Range_2nF = (0)
    Range_20nF = (1)
    Range_200nF = (2)
    Range_2uF = (3)
    Range_200uF = (4)
    Range_10000uF = (5)
    Minimum = ("MIN")
    Maximum = ("MAX")
    Default = ("DEF")
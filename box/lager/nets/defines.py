# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Equipment independent constants that gets mapped back to equipment specific constants
"""
from enum import Enum, auto
from lager.instrument_wrappers import rigol_mso5000_defines
from lager.instrument_wrappers import keithley_defines

TriggerType = rigol_mso5000_defines.TriggerType
TriggerMode = rigol_mso5000_defines.TriggerMode
TriggerStatus = rigol_mso5000_defines.TriggerStatus
TriggerCoupling = rigol_mso5000_defines.TriggerCoupling
TriggerEdgeSlope = rigol_mso5000_defines.TriggerEdgeSlope
TriggerSlopeCondition = rigol_mso5000_defines.TriggerSlopeCondition
TriggerSlopeWindow = rigol_mso5000_defines.TriggerSlopeWindow
TriggerPulseCondition = rigol_mso5000_defines.TriggerPulseCondition
TriggerUARTCondition = rigol_mso5000_defines.TriggerUARTCondition
TriggerUARTParity = rigol_mso5000_defines.TriggerUARTParity
TriggerI2CCondition = rigol_mso5000_defines.TriggerI2CCondition
TriggerI2CDirection = rigol_mso5000_defines.TriggerI2CDirection
TriggerSPICondition = rigol_mso5000_defines.TriggerSPICondition
TriggerSPISlope = rigol_mso5000_defines.TriggerSPISlope
TriggerSPICSMode = rigol_mso5000_defines.TriggerSPICSMode
TriggerCANCondition = rigol_mso5000_defines.TriggerCANCondition
TriggerCANSigType = rigol_mso5000_defines.TriggerCANSigType
BusMode = rigol_mso5000_defines.BusMode
BusFormat = rigol_mso5000_defines.BusFormat
BusView = rigol_mso5000_defines.BusView
BusType = rigol_mso5000_defines.BusType
BusLogicSource = rigol_mso5000_defines.BusLogicSource
BusEndianness = rigol_mso5000_defines.BusEndianness
BusUARTPolarity = rigol_mso5000_defines.BusUARTPolarity
BusUARTParity = rigol_mso5000_defines.BusUARTParity
BusUARTPacketEnd = rigol_mso5000_defines.BusUARTPacketEnd
BusI2CAddressMode = rigol_mso5000_defines.BusI2CAddressMode
BusSPISCLSlope = rigol_mso5000_defines.BusSPISCLSlope
BusSPIPolarity = rigol_mso5000_defines.BusSPIPolarity
BusSPIMode = rigol_mso5000_defines.BusSPIMode
BusCANSigType = rigol_mso5000_defines.BusCANSigType
BusFlexRaySigType = rigol_mso5000_defines.BusFlexRaySigType

Mode = keithley_defines.Mode
SimMode = keithley_defines.SimMethod

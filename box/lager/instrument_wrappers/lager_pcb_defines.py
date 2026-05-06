# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from enum import Enum, auto
from lager.instrument_wrappers import rigol_mso5000_defines
RigolTriggerType = rigol_mso5000_defines.TriggerType

class TriggerType(Enum):
    Edge=auto()
    Pulse=auto()
    Slope=auto()
    Video=auto()
    Pattern=auto()
    Duration=auto()
    Timeout=auto()
    Runt=auto()
    Window=auto()
    Delay=auto()
    Setup=auto()
    NEdge=auto()
    RS232=auto()
    IIC=auto()
    SPI=auto()
    CAN=auto()
    Flexray=auto()
    LIN=auto()
    IIS=auto()
    M1553=auto()

TriggerType_TO_Rigol = {
    TriggerType.Edge: RigolTriggerType.Edge,
    TriggerType.Pulse: RigolTriggerType.Pulse,
    TriggerType.Slope: RigolTriggerType.Slope,
    TriggerType.Video: RigolTriggerType.Video,
    TriggerType.Pattern: RigolTriggerType.Pattern,
    TriggerType.Duration: RigolTriggerType.Duration,
    TriggerType.Timeout: RigolTriggerType.Timeout,
    TriggerType.Runt: RigolTriggerType.Runt,
    TriggerType.Window: RigolTriggerType.Window,
    TriggerType.Delay: RigolTriggerType.Delay,
    TriggerType.Setup: RigolTriggerType.Setup,
    TriggerType.NEdge: RigolTriggerType.NEdge,
    TriggerType.RS232: RigolTriggerType.UART,
    TriggerType.IIC: RigolTriggerType.I2C,
    TriggerType.SPI: RigolTriggerType.SPI,
    TriggerType.CAN: RigolTriggerType.CAN,
    TriggerType.Flexray: RigolTriggerType.Flexray,
    TriggerType.LIN: RigolTriggerType.LIN,
    TriggerType.IIS: RigolTriggerType.I2S,
    TriggerType.M1553: RigolTriggerType.M1553,
}

Rigol_TO_TriggerType = {v: k for k, v in TriggerType_TO_Rigol.items()}

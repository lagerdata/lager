# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from .mappers import (
    BusUART_RigolMSO5000FunctionMapper,
    BusI2C_RigolMSO5000FunctionMapper,
    BusSPI_RigolMSO5000FunctionMapper,
    BusCAN_RigolMSO5000FunctionMapper,
    BusFlex_RigolMSO5000FunctionMapper,
)

class UART:
    def __init__(self,*, tx, rx):
        if tx.device_type == 'rigol_mso5000':
            self.device = BusUART_RigolMSO5000FunctionMapper(tx=tx,rx=rx)
        else:
            raise ValueError(f"Invalid device type: {tx.device_type}")

    def __getattr__(self, attr):
        return getattr(self.device, attr)


class SPI:
    def __init__(self, *, clk, mosi, miso, cs):
        if clk.device_type == 'rigol_mso5000':
            self.device = BusSPI_RigolMSO5000FunctionMapper(clk=clk, mosi=mosi, miso=miso)
        else:
            raise ValueError(f"Invalid device type: {clk.device_type}")

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class I2C:
    def __init__(self, *, scl, sda):
        if scl.device_type == 'rigol_mso5000':
            self.device = BusI2C_RigolMSO5000FunctionMapper(scl=scl, sda=sda)
        else:
            raise ValueError(f"Invalid device type: {scl.device_type}")

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class I2S:
    def __init__(self):
        raise NotImplementedError("I2S support not yet implemented")

class CAN:
    def __init__(self, *, can):
        if can.device_type == 'rigol_mso5000':
            self.device = BusCAN_RigolMSO5000FunctionMapper(can=can)
        else:
            raise ValueError(f"Invalid device type: {can.device_type}")

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class FLEX:
    def __init__(self, *, flex):
        if flex.device_type == 'rigol_mso5000':
            self.device = BusFlex_RigolMSO5000FunctionMapper(flex=flex)
        else:
            raise ValueError(f"Invalid device type: {flex.device_type}")

    def __getattr__(self, attr):
        return getattr(self.device, attr)

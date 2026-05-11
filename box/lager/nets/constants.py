# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from enum import Enum, auto

from lager.constants import HARDWARE_SERVICE_PORT

# Re-export for backward compatibility within pcb module
HARDWARE_PORT = HARDWARE_SERVICE_PORT

class NetType(Enum):
    Analog = auto()
    Logic = auto()
    Waveform = auto()
    Battery = auto()
    ELoad = auto()
    PowerSupply = auto()
    GPIO = auto()
    ADC = auto()
    Thermocouple = auto()
    Rotation = auto()
    Wifi = auto()
    Actuate = auto()
    PowerSupply2Q = auto()
    DAC = auto()
    Debug = auto()
    Arm = auto()
    Usb = auto()
    WattMeter = auto()
    UART = auto()
    Webcam = auto()
    SPI = auto()
    I2C = auto()
    EnergyAnalyzer = auto()
    Router = auto()

    # UPPERCASE / SCREAMING_SNAKE_CASE aliases for the TitleCase members
    # above. Python's enum convention is SCREAMING_SNAKE_CASE, so code
    # (including LLM-generated test scripts) that writes NetType.DEBUG,
    # NetType.POWER_SUPPLY, etc. resolves to the same member rather than
    # raising AttributeError. Members already in UPPERCASE form
    # (GPIO/ADC/DAC/UART/SPI/I2C) need no alias.
    DEBUG = Debug
    BATTERY = Battery
    ANALOG = Analog
    LOGIC = Logic
    WAVEFORM = Waveform
    POWERSUPPLY = PowerSupply
    POWER_SUPPLY = PowerSupply
    ELOAD = ELoad
    THERMOCOUPLE = Thermocouple
    ROTATION = Rotation
    WIFI = Wifi
    ACTUATE = Actuate
    POWERSUPPLY2Q = PowerSupply2Q
    POWER_SUPPLY_2Q = PowerSupply2Q
    ARM = Arm
    USB = Usb
    WATTMETER = WattMeter
    WATT_METER = WattMeter
    WEBCAM = Webcam
    ENERGYANALYZER = EnergyAnalyzer
    ENERGY_ANALYZER = EnergyAnalyzer
    ROUTER = Router

    @classmethod
    def from_role(cls, role):
        mapping = {
            'analog': cls.Analog,
            'logic': cls.Logic,
            'waveform': cls.Waveform,
            'battery': cls.Battery,
            'power-supply': cls.PowerSupply,
            'eload': cls.ELoad,
            'gpio': cls.GPIO,
            'adc': cls.ADC,
            'dac': cls.DAC,
            'thermocouple': cls.Thermocouple,
            'rotation': cls.Rotation,
            'wifi': cls.Wifi,
            'actuate': cls.Actuate,
            'power-supply-2q': cls.PowerSupply2Q,
            'debug': cls.Debug,
            'arm': cls.Arm,
            'usb': cls.Usb,
            'watt-meter': cls.WattMeter,
            'uart': cls.UART,
            'webcam': cls.Webcam,
            'scope': cls.Analog,  # Scope nets use Analog type for Rigol, PicoScope handled separately
            'spi': cls.SPI,
            'i2c': cls.I2C,
            'energy-analyzer': cls.EnergyAnalyzer,
            'router': cls.Router,
            'mikrotik': cls.Router,
        }
        return mapping[role]

    @property
    def device_type(self):
        mapping = {
            self.Analog: 'rigol_mso5000',
            self.Logic: 'rigol_mso5000',
            self.Waveform: 'rigol_mso5000',
            self.Battery: 'keithley',
            self.ELoad: 'rigol_dl3000',
            self.PowerSupply: 'rigol_dp800',
            self.Debug: 'jlink',
            self.GPIO: None,
            self.ADC: None,
            self.DAC: None,
            self.Thermocouple: None,
            self.Rotation: None,
            self.Wifi: None,
            self.Actuate: None,
            self.PowerSupply2Q: None,
            self.Arm: None,
            self.Usb: None,
            self.WattMeter: None,
            self.UART: None,
            self.Webcam: None,
            self.SPI: None,
            self.I2C: None,
            self.EnergyAnalyzer: None,
            self.Router: None,
        }
        return mapping[self]

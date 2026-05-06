# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

from lager.instrument_wrappers import rigol_mso5000_defines
from ..defines import (
    TriggerType,
    TriggerMode,
    TriggerCoupling,
    TriggerEdgeSlope,
    BusMode,
    BusFormat,
    BusView,
    BusType,
    BusLogicSource,
    BusEndianness,
    BusUARTPolarity,
    BusUARTParity,
    BusUARTPacketEnd,
    BusI2CAddressMode,
    BusSPISCLSlope,
    BusSPIPolarity,
    BusSPIMode,
    BusCANSigType,
    BusFlexRaySigType
)
from ..constants import NetType

TriggerEdgeSource = rigol_mso5000_defines.TriggerEdgeSource
TriggerPulseSource = rigol_mso5000_defines.TriggerPulseSource
TriggerUARTSource = rigol_mso5000_defines.TriggerUARTSource
TriggerI2CSource = rigol_mso5000_defines.TriggerI2CSource
TriggerSPISource = rigol_mso5000_defines.TriggerSPISource
TriggerPulseCondition = rigol_mso5000_defines.TriggerPulseCondition
TriggerI2CCondition = rigol_mso5000_defines.TriggerI2CCondition
TriggerI2CDirection = rigol_mso5000_defines.TriggerI2CDirection
TriggerUARTCondition = rigol_mso5000_defines.TriggerUARTCondition
TriggerSPICondition = rigol_mso5000_defines.TriggerSPICondition
TriggerSPISlope = rigol_mso5000_defines.TriggerSPISlope
TriggerSPICSMode = rigol_mso5000_defines.TriggerSPICSMode
TriggerCANSource = rigol_mso5000_defines.TriggerCANSource
TriggerCANCondition = rigol_mso5000_defines.TriggerCANCondition
TriggerCANSigType = rigol_mso5000_defines.TriggerCANSigType
MeasurementSource = rigol_mso5000_defines.MeasurementSource
MeasurementClear = rigol_mso5000_defines.MeasurementClear
MeasurementItem = rigol_mso5000_defines.MeasurementItem
CursorSource = rigol_mso5000_defines.CursorSource
CursorUnit = rigol_mso5000_defines.CursorUnit
CursorType = rigol_mso5000_defines.CursorType
CursorMode = rigol_mso5000_defines.CursorMode
LogicDisplaySize = rigol_mso5000_defines.LogicDisplaySize
LogicChannel = rigol_mso5000_defines.LogicChannel

def map_mux_channel_to_scope(mux_ch):
    chan = None
    if mux_ch == 1:
        chan = MeasurementSource.Channel1
    elif mux_ch == 2:
        chan = MeasurementSource.Channel2
    elif mux_ch == 3:
        chan = MeasurementSource.Channel3
    elif mux_ch == 4:
        chan = MeasurementSource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")
    return chan


def _map_analog_source_to_trigger_uart_source(net):
    if net.channel == 1:
        return TriggerUARTSource.Channel1
    elif net.channel == 2:
        return TriggerUARTSource.Channel2
    elif net.channel == 3:
        return TriggerUARTSource.Channel3
    elif net.channel == 4:
        return TriggerUARTSource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")

def _map_digital_source_to_trigger_uart_source(net):
    if net.channel == 0:
        return TriggerUARTSource.D0
    elif net.channel == 1:
        return TriggerUARTSource.D1
    elif net.channel == 2:
        return TriggerUARTSource.D2
    elif net.channel == 3:
        return TriggerUARTSource.D3
    elif net.channel == 4:
        return TriggerUARTSource.D4
    elif net.channel == 5:
        return TriggerUARTSource.D5
    elif net.channel == 6:
        return TriggerUARTSource.D6
    elif net.channel == 7:
        return TriggerUARTSource.D7
    elif net.channel == 8:
        return TriggerUARTSource.D8
    elif net.channel == 9:
        return TriggerUARTSource.D9
    elif net.channel == 10:
        return TriggerUARTSource.D10
    elif net.channel == 11:
        return TriggerUARTSource.D11
    elif net.channel == 12:
        return TriggerUARTSource.D12
    elif net.channel == 13:
        return TriggerUARTSource.D13
    elif net.channel == 14:
        return TriggerUARTSource.D14
    elif net.channel == 15:
        return TriggerUARTSource.D15
    else:
        raise ValueError("Digital channel must be in the range 0-15")

def _map_analog_source_to_trigger_edge_source(net):
    if net.channel == 1:
        return TriggerEdgeSource.Channel1
    elif net.channel == 2:
        return TriggerEdgeSource.Channel2
    elif net.channel == 3:
        return TriggerEdgeSource.Channel3
    elif net.channel == 4:
        return TriggerEdgeSource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")

def _map_digital_source_to_trigger_edge_source(net):
    if net.channel == 0:
        return TriggerEdgeSource.D0
    elif net.channel == 1:
        return TriggerEdgeSource.D1
    elif net.channel == 2:
        return TriggerEdgeSource.D2
    elif net.channel == 3:
        return TriggerEdgeSource.D3
    elif net.channel == 4:
        return TriggerEdgeSource.D4
    elif net.channel == 5:
        return TriggerEdgeSource.D5
    elif net.channel == 6:
        return TriggerEdgeSource.D6
    elif net.channel == 7:
        return TriggerEdgeSource.D7
    elif net.channel == 8:
        return TriggerEdgeSource.D8
    elif net.channel == 9:
        return TriggerEdgeSource.D9
    elif net.channel == 10:
        return TriggerEdgeSource.D10
    elif net.channel == 11:
        return TriggerEdgeSource.D11
    elif net.channel == 12:
        return TriggerEdgeSource.D12
    elif net.channel == 13:
        return TriggerEdgeSource.D13
    elif net.channel == 14:
        return TriggerEdgeSource.D14
    elif net.channel == 15:
        return TriggerEdgeSource.D15
    else:
        raise ValueError("Digital channel must be in the range 0-15")

def _map_analog_source_to_trigger_i2c_source(net):
    if net.channel == 1:
        return TriggerI2CSource.Channel1
    elif net.channel == 2:
        return TriggerI2CSource.Channel2
    elif net.channel == 3:
        return TriggerI2CSource.Channel3
    elif net.channel == 4:
        return TriggerI2CSource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")

def _map_digital_source_to_trigger_i2c_source(net):
    if net.channel == 0:
        return TriggerI2CSource.D0
    elif net.channel == 1:
        return TriggerI2CSource.D1
    elif net.channel == 2:
        return TriggerI2CSource.D2
    elif net.channel == 3:
        return TriggerI2CSource.D3
    elif net.channel == 4:
        return TriggerI2CSource.D4
    elif net.channel == 5:
        return TriggerI2CSource.D5
    elif net.channel == 6:
        return TriggerI2CSource.D6
    elif net.channel == 7:
        return TriggerI2CSource.D7
    elif net.channel == 8:
        return TriggerI2CSource.D8
    elif net.channel == 9:
        return TriggerI2CSource.D9
    elif net.channel == 10:
        return TriggerI2CSource.D10
    elif net.channel == 11:
        return TriggerI2CSource.D11
    elif net.channel == 12:
        return TriggerI2CSource.D12
    elif net.channel == 13:
        return TriggerI2CSource.D13
    elif net.channel == 14:
        return TriggerI2CSource.D14
    elif net.channel == 15:
        return TriggerI2CSource.D15
    else:
        raise ValueError("Digital channel must be in the range 0-15")

def _map_analog_source_to_trigger_spi_source(net):
    if net.channel == 1:
        return TriggerSPISource.Channel1
    elif net.channel == 2:
        return TriggerSPISource.Channel2
    elif net.channel == 3:
        return TriggerSPISource.Channel3
    elif net.channel == 4:
        return TriggerSPISource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")

def _map_digital_source_to_trigger_spi_source(net):
    if net.channel == 0:
        return TriggerSPISource.D0
    elif net.channel == 1:
        return TriggerSPISource.D1
    elif net.channel == 2:
        return TriggerSPISource.D2
    elif net.channel == 3:
        return TriggerSPISource.D3
    elif net.channel == 4:
        return TriggerSPISource.D4
    elif net.channel == 5:
        return TriggerSPISource.D5
    elif net.channel == 6:
        return TriggerSPISource.D6
    elif net.channel == 7:
        return TriggerSPISource.D7
    elif net.channel == 8:
        return TriggerSPISource.D8
    elif net.channel == 9:
        return TriggerSPISource.D9
    elif net.channel == 10:
        return TriggerSPISource.D10
    elif net.channel == 11:
        return TriggerSPISource.D11
    elif net.channel == 12:
        return TriggerSPISource.D12
    elif net.channel == 13:
        return TriggerSPISource.D13
    elif net.channel == 14:
        return TriggerSPISource.D14
    elif net.channel == 15:
        return TriggerSPISource.D15
    else:
        raise ValueError("Digital channel must be in the range 0-15")

def _map_analog_source_to_trigger_can_source(net):
    if net.channel == 1:
        return TriggerCANSource.Channel1
    elif net.channel == 2:
        return TriggerCANSource.Channel2
    elif net.channel == 3:
        return TriggerCANSource.Channel3
    elif net.channel == 4:
        return TriggerCANSource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")

def _map_digital_source_to_trigger_can_source(net):
    if net.channel == 0:
        return TriggerCANSource.D0
    elif net.channel == 1:
        return TriggerCANSource.D1
    elif net.channel == 2:
        return TriggerCANSource.D2
    elif net.channel == 3:
        return TriggerCANSource.D3
    elif net.channel == 4:
        return TriggerCANSource.D4
    elif net.channel == 5:
        return TriggerCANSource.D5
    elif net.channel == 6:
        return TriggerCANSource.D6
    elif net.channel == 7:
        return TriggerCANSource.D7
    elif net.channel == 8:
        return TriggerCANSource.D8
    elif net.channel == 9:
        return TriggerCANSource.D9
    elif net.channel == 10:
        return TriggerCANSource.D10
    elif net.channel == 11:
        return TriggerCANSource.D11
    elif net.channel == 12:
        return TriggerCANSource.D12
    elif net.channel == 13:
        return TriggerCANSource.D13
    elif net.channel == 14:
        return TriggerCANSource.D14
    elif net.channel == 15:
        return TriggerCANSource.D15
    else:
        raise ValueError("Digital channel must be in the range 0-15")


def _map_analog_source_to_trigger_pulse_source(net):
    if net.channel == 1:
        return TriggerPulseSource.Channel1
    elif net.channel == 2:
        return TriggerPulseSource.Channel2
    elif net.channel == 3:
        return TriggerPulseSource.Channel3
    elif net.channel == 4:
        return TriggerPulseSource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")

def _map_digital_source_to_trigger_pulse_source(net):
    if net.channel == 0:
        return TriggerPulseSource.D0
    elif net.channel == 1:
        return TriggerPulseSource.D1
    elif net.channel == 2:
        return TriggerPulseSource.D2
    elif net.channel == 3:
        return TriggerPulseSource.D3
    elif net.channel == 4:
        return TriggerPulseSource.D4
    elif net.channel == 5:
        return TriggerPulseSource.D5
    elif net.channel == 6:
        return TriggerPulseSource.D6
    elif net.channel == 7:
        return TriggerPulseSource.D7
    elif net.channel == 8:
        return TriggerPulseSource.D8
    elif net.channel == 9:
        return TriggerPulseSource.D9
    elif net.channel == 10:
        return TriggerPulseSource.D10
    elif net.channel == 11:
        return TriggerPulseSource.D11
    elif net.channel == 12:
        return TriggerPulseSource.D12
    elif net.channel == 13:
        return TriggerPulseSource.D13
    elif net.channel == 14:
        return TriggerPulseSource.D14
    elif net.channel == 15:
        return TriggerPulseSource.D15
    else:
        raise ValueError("Digital channel must be in the range 0-15")


def _map_analog_source_to_measurement_source(net):
    if net.channel == 1:
        return MeasurementSource.Channel1
    elif net.channel == 2:
        return MeasurementSource.Channel2
    elif net.channel == 3:
        return MeasurementSource.Channel3
    elif net.channel == 4:
        return MeasurementSource.Channel4
    else:
        raise ValueError("Analog channel must be in the range 1-4")

def _map_digital_source_to_measurement_source(net):
    if net.channel == 0:
        return MeasurementSource.D0
    elif net.channel == 1:
        return MeasurementSource.D1
    elif net.channel == 2:
        return MeasurementSource.D2
    elif net.channel == 3:
        return MeasurementSource.D3
    elif net.channel == 4:
        return MeasurementSource.D4
    elif net.channel == 5:
        return MeasurementSource.D5
    elif net.channel == 6:
        return MeasurementSource.D6
    elif net.channel == 7:
        return MeasurementSource.D7
    elif net.channel == 8:
        return MeasurementSource.D8
    elif net.channel == 9:
        return MeasurementSource.D9
    elif net.channel == 10:
        return MeasurementSource.D10
    elif net.channel == 11:
        return MeasurementSource.D11
    elif net.channel == 12:
        return MeasurementSource.D12
    elif net.channel == 13:
        return MeasurementSource.D13
    elif net.channel == 14:
        return MeasurementSource.D14
    elif net.channel == 15:
        return MeasurementSource.D15
    else:
        raise ValueError("Digital channel must be in the range 0-15")

class LevelCheckMixin:
    def get_volts_per_div(self):
        return self.parent.trace_settings.get_volts_per_div()

    def get_volt_offset(self):
        return self.parent.trace_settings.get_volt_offset()

    def level_is_in_range(self, level):
        volts_per_div = self.get_volts_per_div()
        volt_offset = self.get_volt_offset()
        return -5 * volts_per_div - volt_offset < level < 5 * volts_per_div - volt_offset


class TraceSettings_RigolMSO5000FunctionMapper:
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def set_volt_offset(self, offset):
        self.set_channel_offset(offset, self.net.channel)

    def get_volt_offset(self):
        return float(self.get_channel_offset(self.net.channel))

    def set_volts_per_div(self, volts):
        offset = self.get_volt_offset()
        self.set_channel_scale(volts, self.net.channel)
        self.set_volt_offset(offset)

    def get_volts_per_div(self):
        return float(self.get_channel_scale(self.net.channel))

    def set_time_per_div(self, time):
        self.set_timebase_scale(time)

    def get_time_per_div(self):
        return float(self.get_timebase_scale())

    def set_time_offset(self, time):
        self.set_timebase_offset(time)

    def get_time_offset(self):
        return float(self.get_timebase_offset())

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettings_RigolMSO5000FunctionMapper:
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device
        self.edge = TriggerSettingsEdge_RigolMSO5000FunctionMapper(parent, self.net, self.device)
        self.pulse = TriggerSettingsPulse_RigolMSO5000FunctionMapper(parent, self.net, self.device)
        self.slope = TriggerSettingsSlope_RigolMSO5000FunctionMapper(self.net, self.device)
        self.pattern = TriggerSettingsPattern_RigolMSO5000FunctionMapper(self.net, self.device)
        self.duration = TriggerSettingsDuration_RigolMSO5000FunctionMapper(self.net, self.device)
        self.timeout = TriggerSettingsTimeout_RigolMSO5000FunctionMapper(self.net, self.device)
        self.uart = TriggerSettingsUART_RigolMSO5000FunctionMapper(parent, self.net, self.device)
        self.i2c = TriggerSettingsI2C_RigolMSO5000FunctionMapper(parent, self.net, self.device)
        self.spi = TriggerSettingsSPI_RigolMSO5000FunctionMapper(parent, self.net, self.device)
        self.can = TriggerSettingsCAN_RigolMSO5000FunctionMapper(parent, self.net, self.device)

    def get_status(self):
        return self.get_trigger_status()

    def set_mode_auto(self):
        self.set_trigger_mode(TriggerMode.Auto)

    def set_mode_normal(self):
        self.set_trigger_mode(TriggerMode.Normal)

    def set_mode_single(self):
        self.set_trigger_mode(TriggerMode.Single)

    def get_mode(self):
        return self.get_trigger_mode()

    def set_coupling_AC(self):
        self.set_trigger_coupling(TriggerCoupling.AC)

    def set_coupling_DC(self):
        self.set_trigger_coupling(TriggerCoupling.DC)

    def set_coupling_low_freq_reject(self):
        self.set_trigger_coupling(TriggerCoupling.LF_Reject)

    def set_coupling_high_freq_reject(self):
        self.set_trigger_coupling(TriggerCoupling.HF_Reject)

    def get_coupling(self):
        return self.get_trigger_coupling()

    def set_type(self, trigger_type):
        self.set_trigger_type(trigger_type)

    def get_type(self):
        return self.get_trigger_type()

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsEdge_RigolMSO5000FunctionMapper(LevelCheckMixin):
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def set_source(self, net):
        if net.type == NetType.Analog:
            self.set_trigger_edge_source(_map_analog_source_to_trigger_edge_source(net))
        elif net.type == NetType.Logic:
            self.set_trigger_edge_source(_map_digital_source_to_trigger_edge_source(net))

    def set_slope_rising(self):
        self.set_trigger_edge_slope(TriggerEdgeSlope.Positive)

    def set_slope_falling(self):
        self.set_trigger_edge_slope(TriggerEdgeSlope.Negative)

    def set_slope_both(self):
        self.set_trigger_edge_slope(TriggerEdgeSlope.Either)

    def get_slope(self):
        return self.get_trigger_edge_slope()

    def set_level(self, level):
        self.set_trigger_edge_level(level)

    def get_level(self):
        try:
            return float(self.get_trigger_edge_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsPulse_RigolMSO5000FunctionMapper(LevelCheckMixin):
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def set_source(self, net):
        if net.type == NetType.Analog:
            self.set_trigger_pulse_source(_map_analog_source_to_trigger_pulse_source(net))
        elif net.type == NetType.Logic:
            self.set_trigger_pulse_source(_map_digital_source_to_trigger_pulse_source(net))

    def set_level(self, level):
        self.set_trigger_pulse_level(level)

    def get_level(self):
        try:
            return float(self.get_trigger_pulse_level())
        except (ValueError, TypeError, AttributeError):
            return None
        
    def set_trigger_on_pulse_greater_than_width(self, pulse_width):
        self.set_trigger_pulse_when(TriggerPulseCondition.Greater)
        self.set_trigger_pulse_lower(pulse_width)

    def set_trigger_on_pulse_less_than_width(self, pulse_width):
        self.set_trigger_pulse_when(TriggerPulseCondition.Less)
        self.set_trigger_pulse_upper(pulse_width)

    def set_trigger_on_pulse_less_than_greater_than(self, *, max_pulse_width=None, min_pulse_width=None):
        self.set_trigger_pulse_when(TriggerPulseCondition.Box)
        if max_pulse_width==None:
            max_pulse_width=float(self.get_trigger_pulse_upper())
        if min_pulse_width==None:
            min_pulse_width=float(self.get_trigger_pulse_lower())
        if max_pulse_width < min_pulse_width:
            raise ValueError(f"Max pulse width {max_pulse_width} must be greater than min pulse width {min_pulse_width}")
        if min_pulse_width!=None:
            self.set_trigger_pulse_lower(min_pulse_width)
        if max_pulse_width!=None:
            self.set_trigger_pulse_upper(max_pulse_width)


    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsSlope_RigolMSO5000FunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsPattern_RigolMSO5000FunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsDuration_RigolMSO5000FunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsTimeout_RigolMSO5000FunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsUART_RigolMSO5000FunctionMapper(LevelCheckMixin):
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def set_source(self,net):
        if net.type == NetType.Analog:
            self.set_trigger_uart_source(_map_analog_source_to_trigger_uart_source(net))
        elif net.type == NetType.Logic:
            self.set_trigger_uart_source(_map_digital_source_to_trigger_uart_source(net))

    def set_level(self, level):
        self.set_trigger_uart_level(level)

    def get_level(self):
        try:
            return float(self.get_trigger_uart_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_uart_params(self, *, parity=None, stopbits=None, baud=None, bits=None):
        if baud!=None:
            self.set_trigger_uart_baud(baud)
        if parity!=None:
            self.set_trigger_uart_parity(parity)
        if stopbits!=None:
            self.set_trigger_uart_stopbits(stopbits)
        if bits!=None: 
            if 5 > bits > 8:
                raise ValueError(f"Invalid width value {bits}")            
            self.set_trigger_uart_data_width(bits)                       

    def set_trigger_on_start(self):
        self.set_trigger_uart_condition(TriggerUARTCondition.Start)

    def set_trigger_on_frame_error(self):
        self.set_trigger_uart_condition(TriggerUARTCondition.Error)

    def set_trigger_on_check_error(self):
        self.set_trigger_uart_condition(TriggerUARTCondition.CError)

    def set_trigger_on_data(self, data=None):
        self.set_trigger_uart_condition(TriggerUARTCondition.Data)
        bits=int(self.get_trigger_uart_data_width())      

        if data!=None:            
            if data > (pow(2,bits) - 1):
                raise ValueError(f"Data {data} too large for width value {bits}")
            self.set_trigger_uart_data(data)


    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsI2C_RigolMSO5000FunctionMapper(LevelCheckMixin):
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def set_source(self, *, net_scl=None, net_sda=None):
        if net_scl != None:
            if net_scl.type == NetType.Analog:
                self.set_trigger_i2c_source_scl(_map_analog_source_to_trigger_i2c_source(net_scl))
            elif net_scl.type == NetType.Logic:
                self.set_trigger_i2c_source_scl(_map_digital_source_to_trigger_i2c_source(net_scl))

        if net_sda != None:
            if net_sda.type == NetType.Analog:
                self.set_trigger_i2c_source_sda(_map_analog_source_to_trigger_i2c_source(net_sda))
            elif net_sda.type == NetType.Logic:
                self.set_trigger_i2c_source_sda(_map_digital_source_to_trigger_i2c_source(net_sda))

    def set_scl_trigger_level(self, level):
        self.set_trigger_i2c_scl_level(level)

    def get_scl_trigger_level(self):
        try:
            return float(self.get_trigger_i2c_scl_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_sda_trigger_level(self, level):
        self.set_trigger_i2c_sda_level(level)

    def get_sda_trigger_level(self):
        try:
            return float(self.get_trigger_i2c_sda_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_trigger_on_start(self):
        self.set_trigger_i2c_condition(TriggerI2CCondition.Start)

    def set_trigger_on_restart(self):
        self.set_trigger_i2c_condition(TriggerI2CCondition.ReStart)

    def set_trigger_on_stop(self):
        self.set_trigger_i2c_condition(TriggerI2CCondition.Stop)

    def set_trigger_on_nack(self):
        self.set_trigger_i2c_condition(TriggerI2CCondition.NACK)

    def set_trigger_on_address(self, *, bits=None, direction=None, address=None):
        self.set_trigger_i2c_condition(TriggerI2CCondition.Address)
        if bits==None:
            bits = int(self.get_trigger_i2c_address_width())
        else:
            if 7 > bits > 10:
                raise ValueError(f"Bits {bits} not valid. Should be between 7 and 10 inclusive")            
            if bits == 9:
                raise ValueError(f"{bits} not a valid bit count ")            
            self.set_trigger_i2c_address_width(bits)    

        if address!=None:
            if bits == 7 and address > 127:
                raise ValueError(f"Address {address} too large for bit value of {bits} ")
            if bits == 8 and address > 255:
                raise ValueError(f"Address {address} too large for bit value of {bits} ")
            if bits == 10 and address > 1023:
                raise ValueError(f"Address {address} too large for bit value of {bits} ")
            if bits != 8:
                if direction!=None:
                    self.set_trigger_i2c_direction(direction)
            self.set_trigger_i2c_address(address)

    def set_trigger_on_data(self, *, width=None, data=None):
        self.set_trigger_i2c_condition(TriggerI2CCondition.Data)
        if width==None:
            width=int(self.get_trigger_i2c_bytes())
        else:
            if 1 > width > 5:
                raise ValueError(f"{width} is not a valid value. Should be between 1 and 5 inclusive")
            self.set_trigger_i2c_bytes(width)
        if data !=None:
            if data > (pow(2,width*8) -1):
                raise ValueError(f"Data value {data} is out of range for byte width of {width}")
            self.set_trigger_i2c_data(data)

    def set_trigger_on_addr_data(self, *, bits=None, direction=None, address=None, width=None, data=None):
        self.set_trigger_on_address(bits=bits, direction=direction, address=address)
        self.set_trigger_on_data( width=width, data=data)
        self.set_trigger_i2c_condition(TriggerI2CCondition.AddrData)        

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsSPI_RigolMSO5000FunctionMapper(LevelCheckMixin):
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def set_source(self, *, net_sck=None, net_mosi_miso=None, net_cs=None):
        if net_sck != None:
            if net_sck.type == NetType.Analog:
                self.set_trigger_spi_source_scl(_map_analog_source_to_trigger_spi_source(net_sck))
            elif net_sck.type == NetType.Logic:
                self.set_trigger_spi_source_scl(_map_digital_source_to_trigger_spi_source(net_sck))

        if net_mosi_miso != None:
            if net_mosi_miso.type == NetType.Analog:
                self.set_trigger_spi_source_sda(_map_analog_source_to_trigger_spi_source(net_mosi_miso))
            elif net_mosi_miso.type == NetType.Logic:
                self.set_trigger_spi_source_sda(_map_digital_source_to_trigger_spi_source(net_mosi_miso))

        if net_cs != None:
            if net_cs.type == NetType.Analog:
                self.set_trigger_spi_source_cs(_map_analog_source_to_trigger_spi_source(net_cs))
            elif net_cs.type == NetType.Logic:
                self.set_trigger_spi_source_cs(_map_digital_source_to_trigger_spi_source(net_cs))

    def set_sck_trigger_level(self, level):
        self.set_trigger_spi_scl_level(level)

    def get_sck_trigger_level(self):
        try:
            return float(self.get_trigger_spi_scl_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_mosi_miso_trigger_level(self, level):
        self.set_trigger_spi_sda_level(level)

    def get_mosi_miso_trigger_level(self):
        try:
            return float(self.get_trigger_spi_sda_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_cs_trigger_level(self, level):
        self.set_trigger_spi_cs_level(level)

    def get_cs_trigger_level(self):
        try:
            return float(self.get_trigger_spi_cs_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_clk_edge_positive(self):
        self.set_trigger_spi_slope(TriggerSPISlope.Positive)

    def set_clk_edge_negative(self):
        self.set_trigger_spi_slope(TriggerSPISlope.Negative)        

    def get_clk_edge_slope(self):
        return self.get_trigger_spi_slope()

    def set_trigger_on_timeout(self, timeout):
        self.set_trigger_spi_condition(TriggerSPICondition.Timeout)
        self.set_trigger_spi_timeout(timeout)

    def get_trigger_timeout(self):
        try:
            return float(self.get_trigger_spi_timeout())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_trigger_on_cs_high(self):
        self.set_trigger_spi_condition(TriggerSPICondition.CS)
        self.set_trigger_spi_mode(TriggerSPICSMode.High)

    def set_trigger_on_cs_low(self):
        self.set_trigger_spi_condition(TriggerSPICondition.CS)
        self.set_trigger_spi_mode(TriggerSPICSMode.Low)        

    def set_trigger_data(self, *, bits=None, data=None):
        if bits==None:
            bits=int(self.get_trigger_spi_width())
        else:
            if 4 > bits > 32:
                raise ValueError(f"Data Width {bits} out of range. Should be between 4 and 32 inclusive")
            self.set_trigger_spi_width(bits)

        if data != None:
            if data > (pow(2,bits) - 1):
                raise ValueError(f"Data{data} is too large for bit size {bits}")
            self.set_trigger_spi_data(data)

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class TriggerSettingsCAN_RigolMSO5000FunctionMapper(LevelCheckMixin):
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def set_source(self,net):
        if net.type == NetType.Analog:
            self.set_trigger_can_source(_map_analog_source_to_trigger_can_source(net))
        elif net.type == NetType.Logic:
            self.set_trigger_can_source(_map_digital_source_to_trigger_can_source(net))

    def set_level(self, level):
        self.set_trigger_can_level(level)

    def get_level(self):
        try:
            return float(self.get_trigger_can_level())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_baud(self, baud):
        self.set_trigger_can_baud(baud)

    def get_baud(self):
        try:
            return float(self.get_trigger_can_baud())
        except (ValueError, TypeError, AttributeError):
            return None

    def set_sample_point(self, sample_point):
        self.set_trigger_can_sample_point(sample_point)        

    def get_sample_point(self):
        try:
            return float(self.get_trigger_can_sample_point())
        except (ValueError, TypeError, AttributeError):
            return None             

    def set_signal_type_can_high(self):
        self.set_trigger_can_signal_type(TriggerCANSigType.CANHigh)

    def set_signal_type_can_low(self):
        self.set_trigger_can_signal_type(TriggerCANSigType.CANLow)

    def set_signal_type_can_txrx(self):
        self.set_trigger_can_signal_type(TriggerCANSigType.TXRX)

    def set_signal_type_can_diff(self):
        self.set_trigger_can_signal_type(TriggerCANSigType.Differential)

    def get_signal_type(self):
        return self.get_trigger_can_signal_type()

    def set_trigger_on_sof(self):
        self.set_trigger_can_condition(TriggerCANCondition.SOF)

    def set_trigger_on_eof(self):
        self.set_trigger_can_condition(TriggerCANCondition.EOF)

    def set_trigger_on_id_remote(self):
        self.set_trigger_can_condition(TriggerCANCondition.IDRemote) 

    def set_trigger_on_over_load(self):
        self.set_trigger_can_condition(TriggerCANCondition.OverLoad)

    def set_trigger_on_id_frame(self):
        self.set_trigger_can_condition(TriggerCANCondition.IDFrame)

    def set_trigger_on_data_frame(self):
        self.set_trigger_can_condition(TriggerCANCondition.DataFrame)

    def set_trigger_on_id_data(self):
        self.set_trigger_can_condition(TriggerCANCondition.IDData)

    def set_trigger_on_error_frame(self):
        self.set_trigger_can_condition(TriggerCANCondition.ErrorFrame)

    def set_trigger_on_error_reply(self):
        self.set_trigger_can_condition(TriggerCANCondition.ErrorAnswer)

    def set_trigger_on_error_checksum(self):
        self.set_trigger_can_condition(TriggerCANCondition.ErrorCheck) 

    def set_trigger_on_error_format(self):
        self.set_trigger_can_condition(TriggerCANCondition.ErrorFormat)

    def set_trigger_on_error_random(self):
        self.set_trigger_can_condition(TriggerCANCondition.ErrorRandom) 

    def set_trigger_on_error_bit(self):
        self.set_trigger_can_condition(TriggerCANCondition.ErrorBit)                                                                                     

    def get_trigger_condition(self):
        return self.get_trigger_can_condition()

    def __getattr__(self, attr):
        return getattr(self.device, attr)


class Measurement_RigolMSO5000FunctionMapper:
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device

    def __getattr__(self, attr):
        """Delegate attribute access to the device for methods like get_measure_item, set_measurement_source, etc."""
        return getattr(self.device, attr)

    def _get_measurement_extra(self, display, measurement_cursor, item):
        # Note: measurement source is already set by the calling method (voltage_max, etc.)
        # so we don't need to pass a channel here
        try:
            return float(self.get_measure_item(item))
        except (ValueError, TypeError, AttributeError):
            return None
        finally:
            if(display==False):
                self.clear_measurement(MeasurementClear.All)
                self.disable_cursor_measure_mode()
            if(measurement_cursor==True):
                self.enable_cursor_measure_mode()

    def voltage_max(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net)) 
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VMax)

    def voltage_min(self, display=False, measurement_cursor=False):
        """Returns the minimum voltage. Include more descriptive text
        here if you want.

        :param display: This describes the `display` parameter
        :type display: This describes the type of `display`, optional
        :param measurement_cursor: This describes the `measurement_cursor` parameter
        :type measurement_cursor: This describes the type of `measurement_cursor`, optional
        :return: Describe the return value here
        :rtype: Describe the return type here
        """
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net)) 
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VMin)

    def voltage_peak_to_peak(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VPP)

    def voltage_flat_top(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VTop)

    def voltage_flat_base(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VBase)

    def voltage_flat_amplitude(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VAmp)

    def voltage_average(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VAvg)

    def voltage_rms(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VRMS)

    def voltage_overshoot(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.Overshoot)

    def voltage_preshoot(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.Preshoot)

    def waveform_area(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.MArea)

    def waveform_period_area(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.MPArea)

    def period(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net)) 
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.Period)

    def frequency(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))       
        
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.Frequency)

    def rise_time(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.RTime)

    def fall_time(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.FTime)

    def pulse_width_positive(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.PWidth)

    def pulse_width_negative(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.NWidth)

    def duty_cycle_positive(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.PDuty)

    def duty_cycle_negative(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.NDuty)

    def time_at_voltage_max(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.TVMax)

    def time_at_voltage_min(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.TVMin)

    def positive_slew_rate(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.PSlewrate)

    def negative_slew_rate(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.NSlewrate)

    def voltage_threshold_upper(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VUpper)

    def voltage_threshold_lower(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VLower)

    def voltage_threshold_mid(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.VMid)

    def variance(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.Variance)

    def voltage_rms_period(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.PVRMS)

    def positive_pulse_count(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.PPulses)

    def negative_pulse_count(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.NPulses)

    def positive_edge_count(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.PEdges)

    def negative_edge_count(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.NEdges)

    def delay_rising_rising_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.RRDelay)

    def delay_rising_falling_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.RFDelay)

    def delay_falling_rising_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.FRDelay)

    def delay_falling_falling_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.FFDelay)

    def phase_rising_rising_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.RRPhase)

    def phase_rising_falling_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.RFPhase)

    def phase_falling_rising_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.FRPhase)

    def phase_falling_falling_edge(self, *, display=False, measurement_cursor=False):
        if self.net.type == NetType.Analog:
            self.set_measurement_source(_map_analog_source_to_measurement_source(self.net))
        elif self.net.type == NetType.Logic:
            self.set_measurement_source(_map_digital_source_to_measurement_source(self.net))         
        return self._get_measurement_extra(display, measurement_cursor, MeasurementItem.FFPhase)

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class Cursor_RigolMSO5000FunctionMapper:
    def __init__(self, parent, net, device):
        self.parent = parent
        self.net = net
        self.device = device
        self.source = None
        # Cache cursor positions to avoid querying (which causes timeouts)
        self._cursor_a_x = 240  # Default center position
        self._cursor_a_y = 240
        self._cursor_b_x = 240
        self._cursor_b_y = 240

    def _map_net_to_scope(self):
        chan = None
        if self.net.type == NetType.Analog:
            if self.net.channel == 1:
                chan = CursorSource.Channel1
            elif self.net.channel == 2:
                chan = CursorSource.Channel2
            elif self.net.channel == 3:
                chan = CursorSource.Channel3
            elif self.net.channel == 4:
                chan = CursorSource.Channel4
            else:
                raise ValueError("Analog channel must be in the range 1-4")
        elif self.net.type == NetType.Logic:
            chan = CursorSource.Logic
        else:
            raise ValueError("Unsupported channel type")
        return chan

    def _cursor_setup(self):
        self.set_cursor_mode(CursorMode.Manual)
        self.set_cursor_manual_type("XY")  # Set cursor type to XY for both X and Y cursors
        self.source = self._map_net_to_scope()
        self.set_cursor_manual_source(self.source)

    def hide(self):
        self.set_cursor_mode(CursorMode.Off)

    def set_a(self, *, x=None, y=None):
        self._cursor_setup()
        if x != None:
            x_pos = 240 + x
            # Fixed validation: should be "not (0 <= x_pos <= 479)"
            if not (0 <= x_pos <= 479):
                raise ValueError(f"X position {x_pos} is out of range (0-479). Input x={x} resulted in screen position {x_pos}.")
            self.set_cursor_manual_x_a(x_pos)
            self._cursor_a_x = x_pos  # Cache the position
        if y != None:
            y_pos = 240 - y
            # Fixed validation: should be "not (0 <= y_pos <= 479)"
            if not (0 <= y_pos <= 479):
                raise ValueError(f"Y position {y_pos} is out of range (0-479). Input y={y} resulted in screen position {y_pos}.")
            self.set_cursor_manual_y_a(y_pos)
            self._cursor_a_y = y_pos  # Cache the position

    def set_b(self, *, x=None, y=None):
        self._cursor_setup()
        if x != None:
            x_pos = 240 + x
            # Fixed validation: should be "not (0 <= x_pos <= 479)"
            if not (0 <= x_pos <= 479):
                raise ValueError(f"X position {x_pos} is out of range (0-479). Input x={x} resulted in screen position {x_pos}.")
            self.set_cursor_manual_x_b(x_pos)
            self._cursor_b_x = x_pos  # Cache the position
        if y != None:
            y_pos = 240 - y
            # Fixed validation: should be "not (0 <= y_pos <= 479)"
            if not (0 <= y_pos <= 479):
                raise ValueError(f"Y position {y_pos} is out of range (0-479). Input y={y} resulted in screen position {y_pos}.")
            self.set_cursor_manual_y_b(y_pos)
            self._cursor_b_y = y_pos  # Cache the position

    def get_a(self):
        return (int(self.get_cursor_manual_x_a())-240),(int(self.get_cursor_manual_y_a())-240)

    def get_b(self):
        return (int(self.get_cursor_manual_x_b())-240),(int(self.get_cursor_manual_y_b())-240)

    def move_a(self, *, x_del=None, y_del=None):
        self._cursor_setup()
        if x_del != None:
            # Use cached position instead of querying (which causes timeout)
            new_x = self._cursor_a_x + x_del
            # Fixed validation
            if not (0 <= new_x <= 479):
                raise ValueError(f"New X position {new_x} is out of range (0-479)")
            self.set_cursor_manual_x_a(new_x)
            self._cursor_a_x = new_x
        if y_del != None:
            # Use cached position instead of querying (which causes timeout)
            new_y = self._cursor_a_y - y_del
            # Fixed validation
            if not (0 <= new_y <= 479):
                raise ValueError(f"New Y position {new_y} is out of range (0-479)")
            self.set_cursor_manual_y_a(new_y)
            self._cursor_a_y = new_y

    def move_b(self, *, x_del=None, y_del=None):
        self._cursor_setup()
        if x_del != None:
            # Use cached position instead of querying (which causes timeout)
            new_x = self._cursor_b_x + x_del
            # Fixed validation
            if not (0 <= new_x <= 479):
                raise ValueError(f"New X position {new_x} is out of range (0-479)")
            self.set_cursor_manual_x_b(new_x)
            self._cursor_b_x = new_x
        if y_del != None:
            # Use cached position instead of querying (which causes timeout)
            new_y = self._cursor_b_y - y_del
            # Fixed validation
            if not (0 <= new_y <= 479):
                raise ValueError(f"New Y position {new_y} is out of range (0-479)")
            self.set_cursor_manual_y_b(new_y)
            self._cursor_b_y = new_y

    def x_delta(self):
        return float(self.get_cursor_manual_x_delta())

    def y_delta(self):
        return float(self.get_cursor_manual_y_delta())

    def frequency(self):
        return float(self.get_cursor_manual_x_inverse_delta())

    def a_x(self):
        return float(self.get_cursor_manual_x_value_a())

    def a_y(self):
        return float(self.get_cursor_manual_y_value_a())

    def b_x(self):
        return float(self.get_cursor_manual_x_value_b())

    def b_y(self):
        return float(self.get_cursor_manual_y_value_b())

    def __getattr__(self, attr):
        return getattr(self.device, attr)

def map_channel_to_bus_logic_source(net):
    if net.type == NetType.Logic:
        if net.channel == 0:
            return BusLogicSource.D0
        if net.channel == 1:
            return BusLogicSource.D1
        if net.channel == 2:
            return BusLogicSource.D2
        if net.channel == 3:
            return BusLogicSource.D3
        if net.channel == 4:
            return BusLogicSource.D4
        if net.channel == 5:
            return BusLogicSource.D5
        if net.channel == 6:
            return BusLogicSource.D6
        if net.channel == 7:
            return BusLogicSource.D7
        if net.channel == 8:
            return BusLogicSource.D8
        if net.channel == 9:
            return BusLogicSource.D9
        if net.channel == 10:
            return BusLogicSource.D10
        if net.channel == 11:
            return BusLogicSource.D11
        if net.channel == 12:
            return BusLogicSource.D12
        if net.channel == 13:
            return BusLogicSource.D13
        if net.channel == 14:
            return BusLogicSource.D14
        if net.channel == 15:
            return BusLogicSource.D15
    elif net.type == NetType.Analog:
        if net.channel == 1:
            return BusLogicSource.Channel1
        if net.channel == 2:
            return BusLogicSource.Channel2
        if net.channel == 3:
            return BusLogicSource.Channel3
        if net.channel == 4:
            return BusLogicSource.Channel4

class Bus_RigolMSO5000FunctionMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device
        self.format(BusFormat.Hex)

    def enable(self):
        self.device.enable_bus_display(1)

    def disable(self):
        self.device.disable_bus_display(1)

    def format_hex(self):
        self.set_bus_format(1,BusFormat.Hex)

    def format_decimal(self):
        self.set_bus_format(1,BusFormat.Decimal) 

    def format_binary(self):
        self.set_bus_format(1,BusFormat.Binary) 

    def format_ascii(self):
        self.set_bus_format(1,BusFormat.ASCII)                        

    def show_table(self):
        self.enable_bus_event_table(1)

    def table_format_ascii(self):
        self.set_bus_table_format(1,BusFormat.ASCII)

    def table_format_hex(self):
        self.set_bus_table_format(1,BusFormat.Hex)

    def table_format_decimal(self):
        self.set_bus_table_format(1,BusFormat.Decimal)

    def table_format_binary(self):
        self.set_bus_table_format(1,BusFormat.Binary)

    def set_view_packet(self):
        self.set_bus_table_view(1, BusView.Packets)

    def set_view_details(self):
        self.set_bus_table_view(1, BusView.Details)

    def set_view_payload(self):
        self.set_bus_table_view(1, BusView.Payload)

    def hide_table(self):
        self.disable_bus_event_table(1)

    def bus_data(self):
        return self.get_bus_table_data(1)

    def save_bus_data(self, filename):
        self.save_bus_data(1, filename)


    def __getattr__(self, attr):
        return getattr(self.device, attr)

class BusUART_RigolMSO5000FunctionMapper(Bus_RigolMSO5000FunctionMapper):
    def __init__(self, *, tx, rx):
        super().__init__(tx.net, tx.device)
        self.tx = tx
        self.rx = rx
        self.setup()

    def setup(self):
        self.set_bus_type(1, BusMode.RS232)
        self.set_bus_uart_tx_source(1, map_channel_to_bus_logic_source(self.tx.net))
        self.set_bus_uart_rx_source(1, map_channel_to_bus_logic_source(self.rx.net))

        self.set_bus_uart_polarity(1, BusUARTPolarity.Negative)
        self.set_bus_uart_endianness(1, BusEndianness.MSB)
        self.set_bus_uart_baud(1, 9600)
        self.set_bus_uart_data_bits(1, 8)
        self.set_bus_uart_stop_bits(1, 1)
        self.set_bus_uart_parity(1,BusUARTParity.NoParity)
        self.disable_bus_uart_packet_end(1)

    def set_baud(self, baud):
        self.set_bus_uart_baud(1, baud)

    def set_parity_none(self):
        self.set_bus_uart_parity(1,BusUARTParity.NoParity)

    def set_parity_odd(self):
        self.set_bus_uart_parity(1,BusUARTParity.Odd)

    def set_parity_even(self):
        self.set_bus_uart_parity(1,BusUARTParity.Even)

    def set_polarity_positive(self):
        self.set_bus_uart_polarity(1, BusUARTPolarity.Positive)

    def set_polarity_negative(self):
        self.set_bus_uart_polarity(1, BusUARTPolarity.Negative)  

    def set_endianness_msb(self):
        self.set_bus_uart_endianness(1, BusEndianness.MSB)

    def set_endianness_lsb(self):
        self.set_bus_uart_endianness(1, BusEndianness.LSB)

    def set_stop_bits(self, bits):
        if bits !=1 and bits!=1.5 and bits!=2:
            raise ValueError(f"{bits} is not a valid value")
        self.set_bus_uart_stop_bits(1, bits)

    def set_data_bits(self, bits):
        if 5 > bits > 9:
            raise ValueError(f"{bits} is not a valid value")
        self.set_bus_uart_data_bits(1, bits)

    def set_packet_ending_null(self):
        self.enable_bus_uart_packet_end(1)
        self.set_bus_uart_packet_end(1, BusUARTPacketEnd.NULL)

    def set_packet_ending_lf(self):
        self.enable_bus_uart_packet_end(1)
        self.set_bus_uart_packet_end(1, BusUARTPacketEnd.LF) 

    def set_packet_ending_cr(self):
        self.enable_bus_uart_packet_end(1)
        self.set_bus_uart_packet_end(1, BusUARTPacketEnd.CR)  

    def set_packet_ending_space(self):
        self.enable_bus_uart_packet_end(1)
        self.set_bus_uart_packet_end(1, BusUARTPacketEnd.SP)                        

    def disable_packet_ending(self):
        self.disable_bus_uart_packet_end(1)

    def set_signal_threshold(self, rx=None, tx=None):
        if rx != None:
            self.set_bus_threshold(1,BusType.RX, rx)
        if tx != None:
            self.set_bus_threshold(1,BusType.TX, tx)



class BusI2C_RigolMSO5000FunctionMapper(Bus_RigolMSO5000FunctionMapper):
    def __init__(self, *, scl, sda):
        super().__init__(scl.net, scl.device)
        self.scl = scl
        self.sda = sda
        self.setup()

    def setup(self):
        self.set_bus_type(1, BusMode.I2C)
        self.set_bus_i2c_scl_source(1, map_channel_to_bus_logic_source(self.scl.net))
        self.set_bus_i2c_sda_source(1, map_channel_to_bus_logic_source(self.sda.net))
        self.set_bus_i2c_addr_mode(1,BusI2CAddressMode.Normal)

    def set_signal_threshold(self, *, sda=None, scl=None):
        if sda != None:
            self.set_bus_threshold(1,BusType.SDA, sda)
        if scl != None:
            self.set_bus_threshold(1,BusType.SCL, scl)

    def rw_on(self):
        self.set_bus_i2c_addr_mode(1,BusI2CAddressMode.RW)

    def rw_off(self):
        self.set_bus_i2c_addr_mode(1,BusI2CAddressMode.Normal)        



class BusSPI_RigolMSO5000FunctionMapper(Bus_RigolMSO5000FunctionMapper):
    def __init__(self, *, clk, mosi, miso, cs=None):
        super().__init__(clk.net, clk.device)
        self.clk = clk
        self.mosi = mosi
        self.miso = miso
        self.cs = cs
        self.setup()

    def setup(self):
        self.set_bus_type(1, BusMode.SPI)
        self.set_bus_spi_scl_source(1, map_channel_to_bus_logic_source(self.clk.net))
        self.set_bus_spi_mosi_source(1, map_channel_to_bus_logic_source(self.mosi.net))
        self.set_bus_spi_miso_source(1, map_channel_to_bus_logic_source(self.miso.net))
        if self.cs:
            self.set_bus_spi_ss_source(1, map_channel_to_bus_logic_source(self.cs.net))
            self.set_bus_spi_ss_polarity(1, BusSPIPolarity.High)

        self.set_bus_spi_scl_slope(1, BusSPISCLSlope.Positive)
        self.set_bus_spi_mosi_polarity(1, BusSPIPolarity.High)
        self.set_bus_spi_miso_polarity(1, BusSPIPolarity.High)
        self.set_bus_spi_data_bits(1,8)
        self.set_bus_spi_data_endianness(1, BusEndianness.MSB)
        self.set_bus_spi_mode(1, BusSPIMode.CS)

    def set_signal_polarity(self, mosi=None,miso=None,cs=None):
        if mosi!=None:
            if mosi == 0:
                self.set_bus_spi_mosi_polarity(1, BusSPIPolarity.Low)
            else:
                self.set_bus_spi_mosi_polarity(1, BusSPIPolarity.High)
        if miso!=None:
            if miso == 0:
                self.set_bus_spi_miso_polarity(1, BusSPIPolarity.Low)
            else:
                self.set_bus_spi_miso_polarity(1, BusSPIPolarity.High) 
        if cs!=None:
            if BusSPIMode.Timeout == self.get_bus_spi_mode(1):
                self.set_bus_spi_mode(1, BusSPIMode.CS)
                if cs==0:
                    self.set_bus_spi_ss_polarity(1, BusSPIPolarity.Low)
                else:
                    self.set_bus_spi_ss_polarity(1, BusSPIPolarity.High) 
                self.set_bus_spi_mode(1, BusSPIMode.Timeout)
            else:
                if cs==0:
                    self.set_bus_spi_ss_polarity(1, BusSPIPolarity.Low)
                else:
                    self.set_bus_spi_ss_polarity(1, BusSPIPolarity.High)                 


    def set_sck_phase_rising_edge(self):
        self.set_bus_spi_scl_slope(1, BusSPISCLSlope.Positive)

    def set_sck_phase_falling_edge(self):
        self.set_bus_spi_scl_slope(1, BusSPISCLSlope.Negative)

    def set_signal_threshold(self, mosi=None,miso=None,sck=None,cs=None):
        if self.get_bus_spi_mosi_source(1) != BusLogicSource.Off:
            if mosi != None:
                self.set_bus_threshold(1,BusType.MOSI, mosi)
            if miso != None:
                self.set_bus_threshold(1,BusType.MISO, miso)
            if sck != None:
                self.set_bus_threshold(1,BusType.CLK, sck)
            if cs != None:
                if BusSPIMode.Timeout == self.get_bus_spi_mode(1):
                    self.set_bus_spi_mode(1, BusSPIMode.CS)
                    self.set_bus_threshold(1,BusType.CS, cs)
                    self.set_bus_spi_mode(1, BusSPIMode.Timeout)
                else:
                    self.set_bus_threshold(1,BusType.CS, cs)

    def set_capture_mode_timeout(self, timeout):
        self.set_bus_spi_mode(1, BusSPIMode.Timeout)
        self.set_bus_spi_timeout(1, timeout)

    def set_capture_mode_cs(self):
        self.set_bus_spi_mode(1, BusSPIMode.CS)

    def set_endianness_msb(self):
        self.set_bus_spi_data_endianness(1, BusEndianness.MSB)

    def set_endianness_lsb(self):
        self.set_bus_spi_data_endianness(1, BusEndianness.LSB)

    def set_data_width(self, bits):
        if 4 > bits > 32:
            raise ValueError(f"{bits} is not a valid value")
        self.set_bus_spi_data_bits(1,bits)

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class BusCAN_RigolMSO5000FunctionMapper(Bus_RigolMSO5000FunctionMapper):
    def __init__(self, *, can):
        super().__init__(can.net, can.device)
        self.can = can
        self.setup()

    def setup(self):
        self.set_bus_type(1, BusMode.CAN)
        self.set_bus_can_source(1, map_channel_to_bus_logic_source(self.can.net))
        
        self.set_bus_can_signal_type(1, BusCANSigType.TX)
        self.set_bus_can_baud(1, 500000)
        self.set_bus_can_sample_point_percentage(1, 50)

    def set_signal_threshold(self, threshold):
        self.set_bus_threshold(1,BusType.CAN, threshold)

    def set_baud(self, baud):
        self.set_bus_can_baud(1, baud)

    def set_signal_type_tx(self):
        self.set_bus_can_signal_type(1, BusCANSigType.TX)

    def set_signal_type_rx(self):
        self.set_bus_can_signal_type(1, BusCANSigType.RX)

    def set_signal_type_can_high(self):
        self.set_bus_can_signal_type(1, BusCANSigType.CANHigh) 

    def set_signal_type_can_low(self):
        self.set_bus_can_signal_type(1, BusCANSigType.CANLow)

    def set_signal_type_can_differential(self):
        self.set_bus_can_signal_type(1, BusCANSigType.Differential)                               

    def set_sample_position(self, position):
        self.set_bus_can_sample_point_percentage(1, position)


    def __getattr__(self, attr):
        return getattr(self.device, attr)

class BusFlex_RigolMSO5000FunctionMapper(Bus_RigolMSO5000FunctionMapper):
    def __init__(self, *, flex):
        super().__init__(flex.net, flex.device)
        self.flex = flex
        self.setup()

    def setup(self,*, baud=500000, sample_pt=50,signal_type=BusFlexRaySigType.BP):
        self.set_bus_type(1, BusMode.FlexRay)
        self.set_bus_flex_source(1, map_channel_to_bus_logic_source(self.can.flex))
        self.set_bus_flex_signal_type(1, signal_type)
        self.set_bus_flex_baud(1, baud)
        self.set_bus_flex_sample_point_percentage(1, sample_pt)

    def set_signal_threshold(self, threshold):
        self.set_bus_threshold(1,BusType.FLEX, threshold)

    def __getattr__(self, attr):
        return getattr(self.device, attr)


class RigolMSO5000AnalogMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device
        self.measurement = Measurement_RigolMSO5000FunctionMapper(self, self.net, self.device)
        self.trigger_settings = TriggerSettings_RigolMSO5000FunctionMapper(self, self.net, self.device)
        self.trace_settings = TraceSettings_RigolMSO5000FunctionMapper(self, self.net, self.device)
        self.cursor = Cursor_RigolMSO5000FunctionMapper(self, self.net, self.device)

    def autoscale(self):
        self.device.autoscale()

    def start_capture(self):
        self.run()

    def stop_capture(self):
        self.stop()

    def start_single_capture(self):
        self.single()

    def force_trigger(self):
        self.trigger_force()

    def __getattr__(self, attr):
        return getattr(self.device, attr)

class RigolMSO5000LogicMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device
        self.trigger_settings = TriggerSettings_RigolMSO5000FunctionMapper(self, self.net, self.device)
        self.measurement = Measurement_RigolMSO5000FunctionMapper(self, self.net, self.device)
        self.cursor = Cursor_RigolMSO5000FunctionMapper(self, self.net, self.device)

    def set_signal_threshold(self, voltage):
        if self.net.channel <= 7:
            self.set_la_threshold(1,voltage)
        else:
            self.set_la_threshold(2,voltage)

    def display_position(self, position):
        self.set_la_display_position(self.net.channel, position)

    def start_capture(self):
        self.run()

    def stop_capture(self):
        self.stop()

    def start_single_capture(self):
        self.single()

    def force_trigger(self):
        self.trigger_force()

    def size_large(self):
        self.set_enabled_channel_size(LogicDisplaySize.Large)

    def size_medium(self):
        self.set_enabled_channel_size(LogicDisplaySize.Medium)

    def size_small(self):
        self.set_enabled_channel_size(LogicDisplaySize.Small)

    def __getattr__(self, attr):
        return getattr(self.device, attr)



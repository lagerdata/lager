# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Rigol MSO5000 Series Oscilloscope SCPI Control

Provides low-level SCPI commands for Rigol MSO5000 series oscilloscopes.
This module is used by the hardware_service to handle Device proxy calls.
"""

import pyvisa
import logging

logger = logging.getLogger(__name__)

# Cache for VISA resource manager and instruments
_rm = None
_instruments = {}


def get_resource_manager():
    """Get or create the VISA resource manager."""
    global _rm
    if _rm is None:
        _rm = pyvisa.ResourceManager('@py')
    return _rm


def get_instrument(address):
    """Get or create a connection to an instrument at the given address."""
    global _instruments
    if address not in _instruments:
        rm = get_resource_manager()
        # Cross-process advisory lock around the open — defends against an
        # ad-hoc box-side pyvisa client racing for the same USB-TMC interface
        # on this Rigol oscilloscope. See power/battery/keithley.py for the
        # longer rationale.
        from lager.util.device_lock import device_lock
        with device_lock(address, timeout=2.0):
            instr = rm.open_resource(address)
        instr.timeout = 5000  # 5 second timeout
        _instruments[address] = instr
        logger.info(f"Connected to Rigol oscilloscope at {address}")
    return _instruments[address]


class RigolMso5000:
    """
    Low-level SCPI interface for Rigol MSO5000 series oscilloscopes.

    The hardware_service instantiates this with net_info containing:
    - address: VISA address (e.g., USB0::0x1AB1::0x0515::...::INSTR)
    - pin/channel: Channel number (1-4)
    """

    def __init__(self, address=None, pin=None, channel=None, **kwargs):
        self.address = address
        self.channel = pin or channel or 1
        self._instr = None

    @property
    def instr(self):
        """Lazy connection to the instrument."""
        if self._instr is None and self.address:
            self._instr = get_instrument(self.address)
        return self._instr

    def write(self, cmd):
        """Send a SCPI command."""
        if self.instr:
            self.instr.write(cmd)

    def query(self, cmd):
        """Send a SCPI query and return the response."""
        if self.instr:
            return self.instr.query(cmd).strip()
        return ""

    # ============ Acquisition Control ============

    def run(self):
        """Start continuous acquisition."""
        self.write(":RUN")
        return {"status": "running"}

    def stop(self):
        """Stop acquisition."""
        self.write(":STOP")
        return {"status": "stopped"}

    def single(self):
        """Start single acquisition."""
        self.write(":SINGle")
        return {"status": "single"}

    def trigger_force(self):
        """Force a trigger."""
        self.write(":TFORce")
        return {"status": "triggered"}

    def autoscale(self):
        """Perform autoscale and wait for completion."""
        # Save current timeout
        original_timeout = self.instr.timeout

        # Increase timeout for autoscale (can take 10+ seconds)
        self.instr.timeout = 15000  # 15 seconds

        try:
            # Send autoscale command
            self.write(":AUToscale")

            # Wait for operation to complete using *OPC? query
            # This blocks until autoscale finishes
            self.query("*OPC?")

            return {"status": "autoscaled"}
        finally:
            # Restore original timeout
            self.instr.timeout = original_timeout

    # ============ Channel Control ============

    def enable(self, channel=None):
        """Enable a channel display (alias for enable_channel)."""
        return self.enable_channel(channel)

    def disable(self, channel=None):
        """Disable a channel display (alias for disable_channel)."""
        return self.disable_channel(channel)

    def enable_channel(self, channel=None):
        """Enable a channel display."""
        ch = channel or self.channel
        self.write(f":CHANnel{ch}:DISPlay ON")
        return {"channel": ch, "enabled": True}

    def disable_channel(self, channel=None):
        """Disable a channel display."""
        ch = channel or self.channel
        self.write(f":CHANnel{ch}:DISPlay OFF")
        return {"channel": ch, "enabled": False}

    def get_channel_display(self, channel=None):
        """Get channel display state."""
        ch = channel or self.channel
        resp = self.query(f":CHANnel{ch}:DISPlay?")
        return resp == "1" or resp.upper() == "ON"

    def set_channel_scale(self, scale, channel=None):
        """Set channel vertical scale (V/div)."""
        ch = channel or self.channel
        self.write(f":CHANnel{ch}:SCALe {scale}")
        return {"channel": ch, "scale": scale}

    def get_channel_scale(self, channel=None):
        """Get channel vertical scale."""
        ch = channel or self.channel
        return float(self.query(f":CHANnel{ch}:SCALe?"))

    def set_channel_offset(self, offset, channel=None):
        """Set channel vertical offset."""
        ch = channel or self.channel
        self.write(f":CHANnel{ch}:OFFSet {offset}")
        return {"channel": ch, "offset": offset}

    def get_channel_offset(self, channel=None):
        """Get channel vertical offset."""
        ch = channel or self.channel
        return float(self.query(f":CHANnel{ch}:OFFSet?"))

    def set_channel_coupling(self, coupling, channel=None):
        """Set channel coupling (DC, AC, GND)."""
        ch = channel or self.channel
        self.write(f":CHANnel{ch}:COUPling {coupling}")
        return {"channel": ch, "coupling": coupling}

    def get_channel_coupling(self, channel=None):
        """Get channel coupling."""
        ch = channel or self.channel
        return self.query(f":CHANnel{ch}:COUPling?")

    def set_channel_probe(self, ratio, channel=None):
        """Set channel probe attenuation ratio (1, 10, 100, etc.)."""
        ch = channel or self.channel
        self.write(f":CHANnel{ch}:PROBe {ratio}")
        return {"channel": ch, "probe": ratio}

    def get_channel_probe(self, channel=None):
        """Get channel probe attenuation ratio."""
        ch = channel or self.channel
        return float(self.query(f":CHANnel{ch}:PROBe?"))

    # ============ Timebase Control ============

    def set_timebase_scale(self, scale):
        """Set timebase scale (s/div)."""
        self.write(f":TIMebase:MAIN:SCALe {scale}")
        return {"timebase_scale": scale}

    def get_timebase_scale(self):
        """Get timebase scale."""
        return float(self.query(":TIMebase:MAIN:SCALe?"))

    def set_timebase_offset(self, offset):
        """Set timebase offset."""
        self.write(f":TIMebase:MAIN:OFFSet {offset}")
        return {"timebase_offset": offset}

    def get_timebase_offset(self):
        """Get timebase offset."""
        return float(self.query(":TIMebase:MAIN:OFFSet?"))

    # ============ Trigger Control ============

    def set_trigger_mode(self, mode):
        """Set trigger mode (AUTO, NORMal, SINGle)."""
        self.write(f":TRIGger:SWEep {mode}")
        return {"trigger_mode": mode}

    def get_trigger_mode(self):
        """Get trigger mode."""
        return self.query(":TRIGger:SWEep?")

    def set_trigger_coupling(self, coupling):
        """Set trigger coupling (DC, AC, LFReject, HFReject)."""
        self.write(f":TRIGger:COUPling {coupling}")
        return {"trigger_coupling": coupling}

    def get_trigger_coupling(self):
        """Get trigger coupling."""
        return self.query(":TRIGger:COUPling?")

    def set_trigger_level(self, level, source=None):
        """Set trigger level."""
        src = source or f"CHANnel{self.channel}"
        self.write(f":TRIGger:EDGe:LEVel {level},{src}")
        return {"trigger_level": level, "source": src}

    def get_trigger_level(self, source=None):
        """Get trigger level."""
        src = source or f"CHANnel{self.channel}"
        return float(self.query(f":TRIGger:EDGe:LEVel? {src}"))

    def set_trigger_source(self, source):
        """Set trigger source (CHANnel1-4, EXT, etc.)."""
        self.write(f":TRIGger:EDGe:SOURce {source}")
        return {"trigger_source": source}

    def get_trigger_source(self):
        """Get trigger source."""
        return self.query(":TRIGger:EDGe:SOURce?")

    def set_trigger_slope(self, slope):
        """Set trigger slope (POSitive, NEGative, RFALl)."""
        self.write(f":TRIGger:EDGe:SLOPe {slope}")
        return {"trigger_slope": slope}

    def get_trigger_slope(self):
        """Get trigger slope."""
        return self.query(":TRIGger:EDGe:SLOPe?")

    # Alias methods for mapper compatibility
    def set_trigger_edge_level(self, level, source=None):
        """Alias for set_trigger_level for edge trigger."""
        return self.set_trigger_level(level, source)

    def get_trigger_edge_level(self, source=None):
        """Alias for get_trigger_level for edge trigger."""
        return self.get_trigger_level(source)

    def set_trigger_edge_slope(self, slope):
        """Alias for set_trigger_slope for edge trigger."""
        return self.set_trigger_slope(slope)

    def get_trigger_edge_slope(self):
        """Alias for get_trigger_slope for edge trigger."""
        return self.get_trigger_slope()

    def set_trigger_edge_source(self, source):
        """Alias for set_trigger_source for edge trigger.

        Args:
            source: Source channel (TriggerEdgeSource enum or string like "CHAN1")
        """
        # Convert enum to string if needed
        source_str = str(source) if not isinstance(source, str) else source
        return self.set_trigger_source(source_str)

    def get_trigger_edge_source(self):
        """Alias for get_trigger_source for edge trigger."""
        return self.get_trigger_source()

    def set_trigger_type(self, trigger_type):
        """Set the trigger type (EDGE, PULSe, RUNT, WIND, NEDGe, etc.).

        Args:
            trigger_type: Trigger type string or enum (e.g., "EDGE", "PULSe")
        """
        # Convert enum to string if needed
        type_str = str(trigger_type) if not isinstance(trigger_type, str) else trigger_type
        self.write(f":TRIGger:MODE {type_str}")
        return {"trigger_type": type_str}

    def get_trigger_type(self):
        """Get the current trigger type."""
        return self.query(":TRIGger:MODE?")

    # ============ Measurement Control ============

    def measure_frequency(self, channel=None):
        """Measure frequency on a channel."""
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")
        return float(self.query(":MEASure:FREQuency?"))

    def measure_period(self, channel=None):
        """Measure period on a channel."""
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")
        return float(self.query(":MEASure:PERiod?"))

    def measure_vpp(self, channel=None):
        """Measure peak-to-peak voltage."""
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")
        return float(self.query(":MEASure:VPP?"))

    def measure_vmax(self, channel=None):
        """Measure maximum voltage."""
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")
        return float(self.query(":MEASure:VMAX?"))

    def measure_vmin(self, channel=None):
        """Measure minimum voltage."""
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")
        return float(self.query(":MEASure:VMIN?"))

    def measure_vrms(self, channel=None):
        """Measure RMS voltage."""
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")
        return float(self.query(":MEASure:VRMS?"))

    def measure_vavg(self, channel=None):
        """Measure average voltage."""
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")
        return float(self.query(":MEASure:VAVerage?"))

    def get_measure_item(self, item, channel=None):
        """Generic measurement method for any measurement item.

        Args:
            item: Measurement item enum or string (e.g., MeasurementItem.VPP, "VPP", etc.)
            channel: Channel number (1-4) or None for default channel
                    If None, assumes source is already set via set_measurement_source()

        Returns:
            Measurement value as float, or None if measurement fails
        """
        # Only set source if channel is explicitly provided
        # Otherwise assume it's already set by caller (e.g., via set_measurement_source)
        if channel is not None:
            self.write(f":MEASure:SOURce CHANnel{channel}")

        # Convert enum to string if needed
        # Handle both actual enums and serialized enum dictionaries from Device proxy
        if isinstance(item, dict) and '__enum__' in item:
            # Deserialized enum from Device proxy: {'__enum__': {'type': 'MeasurementItem', 'value': 'VPP'}}
            item_str = item['__enum__']['value']
        elif hasattr(item, 'value'):
            # Actual enum object
            item_str = item.value
        elif hasattr(item, '__class__'):
            # Fallback: extract from string representation
            item_str = str(item).split('.')[-1]
        else:
            # Plain string
            item_str = str(item)

        try:
            result = self.query(f":MEASure:{item_str}?")

            # Rigol returns 9.9E+37 when measurement is invalid/unavailable
            float_result = float(result)

            if float_result > 9e36:
                return None

            return float_result
        except (ValueError, TypeError):
            return None
        except Exception:
            return None

    def set_measurement_source(self, source):
        """Set the measurement source channel.

        Args:
            source: Channel source (e.g., "CHANnel1", "CHANnel2", etc.)
                   Can also be a MeasurementSource enum value
        """
        # Convert enum to string if needed
        source_str = str(source) if not isinstance(source, str) else source
        self.write(f":MEASure:SOURce {source_str}")
        return {"measurement_source": source_str}

    def get_measurement_source(self):
        """Get the current measurement source."""
        return self.query(":MEASure:SOURce?")

    def clear_measurement(self, clear_type="ALL"):
        """Clear measurement items.

        Args:
            clear_type: Type of clear operation (e.g., "ALL", "ITEM")
                       Can also be a MeasurementClear enum value
        """
        # Convert enum to string if needed
        clear_str = str(clear_type) if not isinstance(clear_type, str) else clear_type
        self.write(f":MEASure:CLEar {clear_str}")
        return {"measurement_clear": clear_str}

    def enable_cursor_measure_mode(self):
        """Enable cursor measurement mode."""
        # Enable cursor tracking mode for measurements
        self.write(":CURSor:MODE TRACk")
        return {"cursor_measure_mode": "enabled"}

    def disable_cursor_measure_mode(self):
        """Disable cursor measurement mode."""
        # Turn off cursor display
        self.write(":CURSor:MODE OFF")
        return {"cursor_measure_mode": "disabled"}

    # ============ Cursor Control ============

    def set_cursor_mode(self, mode):
        """Set cursor mode (OFF, MANual, TRACk, AUTO, XY).

        Args:
            mode: Cursor mode string or enum (e.g., "MANual", "OFF")
        """
        # Convert enum to string if needed
        mode_str = str(mode) if not isinstance(mode, str) else mode
        self.write(f":CURSor:MODE {mode_str}")
        return {"cursor_mode": mode_str}

    def get_cursor_mode(self):
        """Get the current cursor mode."""
        return self.query(":CURSor:MODE?")

    def set_cursor_manual_source(self, source):
        """Set cursor manual mode source channel.

        Args:
            source: Channel source (e.g., "CHANnel1", "CHANnel2", etc.)
        """
        source_str = str(source) if not isinstance(source, str) else source
        self.write(f":CURSor:MANual:SOURce {source_str}")
        return {"cursor_source": source_str}

    def get_cursor_manual_source(self):
        """Get cursor manual mode source."""
        return self.query(":CURSor:MANual:SOURce?")

    def set_cursor_manual_type(self, cursor_type):
        """Set cursor manual mode type (X, Y, or XY).

        Args:
            cursor_type: Cursor type string or enum (e.g., "XY", "X", "Y")
        """
        type_str = str(cursor_type) if not isinstance(cursor_type, str) else cursor_type
        self.write(f":CURSor:MANual:TYPE {type_str}")
        return {"cursor_type": type_str}

    def get_cursor_manual_type(self):
        """Get cursor manual mode type."""
        return self.query(":CURSor:MANual:TYPE?")

    def set_cursor_manual_x_a(self, x):
        """Set cursor A X position in manual mode."""
        self.write(f":CURSor:MANual:AX {x}")
        return {"cursor_a_x": x}

    def get_cursor_manual_x_a(self):
        """Get cursor A X position."""
        return self.query(":CURSor:MANual:AX?")

    def set_cursor_manual_y_a(self, y):
        """Set cursor A Y position in manual mode."""
        self.write(f":CURSor:MANual:AY {y}")
        return {"cursor_a_y": y}

    def get_cursor_manual_y_a(self):
        """Get cursor A Y position."""
        return self.query(":CURSor:MANual:AY?")

    def set_cursor_manual_x_b(self, x):
        """Set cursor B X position in manual mode."""
        self.write(f":CURSor:MANual:BX {x}")
        return {"cursor_b_x": x}

    def get_cursor_manual_x_b(self):
        """Get cursor B X position."""
        return self.query(":CURSor:MANual:BX?")

    def set_cursor_manual_y_b(self, y):
        """Set cursor B Y position in manual mode."""
        self.write(f":CURSor:MANual:BY {y}")
        return {"cursor_b_y": y}

    def get_cursor_manual_y_b(self):
        """Get cursor B Y position."""
        return self.query(":CURSor:MANual:BY?")

    def get_cursor_manual_x_delta(self):
        """Get X delta between cursor A and B."""
        return self.query(":CURSor:MANual:XDELta?")

    def get_cursor_manual_y_delta(self):
        """Get Y delta between cursor A and B."""
        return self.query(":CURSor:MANual:YDELta?")

    def get_cursor_manual_x_inverse_delta(self):
        """Get inverse X delta (1/delta) between cursor A and B."""
        return self.query(":CURSor:MANual:IXDelta?")

    def get_cursor_manual_x_value_a(self):
        """Get cursor A X value (time/frequency)."""
        return self.query(":CURSor:MANual:AXValue?")

    def get_cursor_manual_y_value_a(self):
        """Get cursor A Y value (voltage)."""
        return self.query(":CURSor:MANual:AYValue?")

    def get_cursor_manual_x_value_b(self):
        """Get cursor B X value (time/frequency)."""
        return self.query(":CURSor:MANual:BXValue?")

    def get_cursor_manual_y_value_b(self):
        """Get cursor B Y value (voltage)."""
        return self.query(":CURSor:MANual:BYValue?")

    # ============ Identification ============

    def get_identification(self):
        """Get instrument identification."""
        return self.query("*IDN?")

    def reset(self):
        """Reset instrument to default state."""
        self.write("*RST")
        return {"status": "reset"}


def create_device(net_info):
    """Create a RigolMso5000 device from net_info dict.

    This is called by the hardware_service when instantiating the device.
    """
    address = net_info.get('address')
    channel = net_info.get('pin') or net_info.get('channel') or 1
    return RigolMso5000(address=address, channel=channel)

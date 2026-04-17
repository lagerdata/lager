#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Consolidated oscilloscope implementation for both PicoScope and Rigol devices.

This script handles all scope operations:
- Basic operations: enable/disable channel, start/stop/single capture, force trigger
- Measurements: voltage (vpp, vmax, vmin, vrms, vavg), timing (freq, period),
                pulse (width pos/neg), duty cycle (pos/neg)
- Trigger configuration: edge, uart, i2c, spi, pulse
- Cursor control: set/move/hide cursors (Rigol only)

For PicoScope: Uses WebSocket commands to the oscilloscope-daemon
For Rigol: Uses VISA/SCPI commands via the Net/Mapper infrastructure
"""

import json
import os
import sys
import asyncio

# Constants — LAGER_STATE_DIR is /etc/lager on Linux boxes (default) and
# /Library/Application Support/Lager on macOS boxes (set by the launchd plist).
_LAGER_STATE_DIR = os.environ.get("LAGER_STATE_DIR", "/etc/lager")
LOCAL_NETS_PATH = os.path.join(_LAGER_STATE_DIR, "saved_nets.json")
DAEMON_HOST = "localhost"
DAEMON_COMMAND_PORT = 8085

# ANSI color codes
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
RESET = '\033[0m'


# ============================================================================
# COMMON HELPER FUNCTIONS
# ============================================================================

def load_saved_nets():
    """Load nets from the saved nets file."""
    try:
        with open(LOCAL_NETS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"{RED}Error loading saved nets: {e}{RESET}", file=sys.stderr)
        return []


def get_net_info(netname):
    """Get net info by name from saved nets."""
    nets = load_saved_nets()
    for net in nets:
        if net.get("name") == netname and net.get("role") == "scope":
            return net
    return None


def is_picoscope(net_info):
    """Check if the net is a PicoScope based on instrument name."""
    if not net_info:
        return False
    instrument = net_info.get("instrument", "").lower()
    return "pico" in instrument or "picoscope" in instrument


def is_rigol(net_info):
    """Check if the net is a Rigol oscilloscope."""
    if not net_info:
        return False
    instrument = net_info.get("instrument", "").lower()
    return "rigol" in instrument or "mso" in instrument


def get_rigol_net(netname):
    """Get a Rigol Net object using Net.get()."""
    from lager import Net, NetType

    net = Net.get(netname, NetType.Analog)
    return net


def get_source_net(source_name):
    """Get source net if specified."""
    if not source_name:
        return None
    from lager import Net, NetType
    return Net.get(source_name, NetType.Analog)


# ============================================================================
# PICOSCOPE WEBSOCKET COMMUNICATION
# ============================================================================

def send_command_pico(command: dict) -> dict:
    """Send a command to the PicoScope daemon via WebSocket."""
    try:
        import websockets
    except ImportError:
        return {"error": "websockets library not installed"}

    async def send_async():
        uri = f"ws://{DAEMON_HOST}:{DAEMON_COMMAND_PORT}"
        try:
            async with websockets.connect(uri, close_timeout=5) as ws:
                await ws.send(json.dumps(command))
                response = await asyncio.wait_for(ws.recv(), timeout=10.0)
                return json.loads(response)
        except ConnectionRefusedError:
            return {"error": "Oscilloscope daemon not running"}
        except asyncio.TimeoutError:
            return {"error": "Timeout waiting for daemon response"}
        except Exception as e:
            return {"error": f"Communication error: {str(e)}"}

    return asyncio.run(send_async())


def map_channel_pico(channel):
    """Map channel number/letter to PicoScope daemon format."""
    channel_str = str(channel)
    if channel_str in ("A", "1"):
        return {"Alphabetic": "A"}
    elif channel_str in ("B", "2"):
        return {"Alphabetic": "B"}
    elif channel_str in ("C", "3"):
        return {"Alphabetic": "C"}
    elif channel_str in ("D", "4"):
        return {"Alphabetic": "D"}
    return {"Alphabetic": "A"}


# ============================================================================
# BASIC OPERATIONS - PICOSCOPE
# ============================================================================

def enable_channel_pico(channel):
    """Enable a PicoScope channel."""
    response = send_command_pico({
        "command": "EnableChannel",
        "channel": map_channel_pico(channel)
    })
    if "error" in response:
        print(f"{RED}Error enabling channel: {response['error']}{RESET}")
        return False
    print(f"{GREEN}Channel {channel} enabled{RESET}")
    return True


def disable_channel_pico(channel):
    """Disable a PicoScope channel."""
    response = send_command_pico({
        "command": "DisableChannel",
        "channel": map_channel_pico(channel)
    })
    if "error" in response:
        print(f"{RED}Error disabling channel: {response['error']}{RESET}")
        return False
    print(f"{GREEN}Channel {channel} disabled{RESET}")
    return True


def start_capture_pico():
    """Start PicoScope capture."""
    response = send_command_pico({
        "command": "StartAcquisition",
        "trigger_position_percent": 50.0
    })
    if "error" in response:
        print(f"{RED}Error starting capture: {response['error']}{RESET}")
        return False
    print(f"{GREEN}Capture started{RESET}")
    return True


def stop_capture_pico():
    """Stop PicoScope capture."""
    response = send_command_pico({"command": "StopAcquisition"})
    if "error" in response:
        print(f"{RED}Error stopping capture: {response['error']}{RESET}")
        return False
    print(f"{GREEN}Capture stopped{RESET}")
    return True


def start_single_pico():
    """Start single capture on PicoScope."""
    # First set capture mode to single
    response = send_command_pico({
        "command": "SetCaptureMode",
        "capture_mode": "single"
    })
    if "error" in response:
        print(f"{YELLOW}Warning: Could not set single mode: {response['error']}{RESET}")

    # Then start acquisition
    response = send_command_pico({
        "command": "StartAcquisition",
        "trigger_position_percent": 50.0
    })
    if "error" in response:
        print(f"{RED}Error starting single capture: {response['error']}{RESET}")
        return False
    print(f"{GREEN}Single capture started{RESET}")
    return True


def force_trigger_pico():
    """Force trigger on PicoScope."""
    response = send_command_pico({"command": "ForceTrigger"})
    if "error" in response:
        print(f"{RED}Error forcing trigger: {response['error']}{RESET}")
        return False
    print(f"{GREEN}Trigger forced{RESET}")
    return True


# ============================================================================
# BASIC OPERATIONS - RIGOL
# ============================================================================

def enable_channel_rigol(netname):
    """Enable a Rigol scope channel."""
    try:
        net = get_rigol_net(netname)
        net.enable()
        channel = getattr(net, 'channel', '?')
        print(f"{GREEN}Channel {channel} enabled{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error enabling channel: {e}{RESET}")
        return False


def disable_channel_rigol(netname):
    """Disable a Rigol scope channel."""
    try:
        net = get_rigol_net(netname)
        net.disable()
        channel = getattr(net, 'channel', '?')
        print(f"{GREEN}Channel {channel} disabled{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error disabling channel: {e}{RESET}")
        return False


def start_capture_rigol(netname):
    """Start Rigol scope capture."""
    try:
        net = get_rigol_net(netname)
        net.start_capture()
        print(f"{GREEN}Capture started{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error starting capture: {e}{RESET}")
        return False


def stop_capture_rigol(netname):
    """Stop Rigol scope capture."""
    try:
        net = get_rigol_net(netname)
        net.stop_capture()
        print(f"{GREEN}Capture stopped{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error stopping capture: {e}{RESET}")
        return False


def start_single_rigol(netname):
    """Start single capture on Rigol scope."""
    try:
        net = get_rigol_net(netname)
        net.start_single_capture()
        print(f"{GREEN}Single capture started{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error starting single capture: {e}{RESET}")
        return False


def force_trigger_rigol(netname):
    """Force trigger on Rigol scope."""
    try:
        net = get_rigol_net(netname)
        net.force_trigger()
        print(f"{GREEN}Trigger forced{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error forcing trigger: {e}{RESET}")
        return False


def set_scale_rigol(netname, volts_per_div):
    """Set vertical scale for Rigol oscilloscope."""
    try:
        net = get_rigol_net(netname)
        net.trace_settings.set_volts_per_div(volts_per_div)
        channel = getattr(net, 'channel', '?')
        print(f"{GREEN}Channel {channel} vertical scale set to {volts_per_div} V/div{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error setting scale: {e}{RESET}")
        return False


def set_timebase_rigol(netname, seconds_per_div):
    """Set horizontal timebase for Rigol oscilloscope."""
    try:
        net = get_rigol_net(netname)
        net.trace_settings.set_time_per_div(seconds_per_div)
        print(f"{GREEN}Timebase set to {seconds_per_div} s/div{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error setting timebase: {e}{RESET}")
        return False


def do_autoscale_rigol(netname):
    """Perform autoscale on Rigol oscilloscope."""
    try:
        net = get_rigol_net(netname)
        net.autoscale()
        print(f"{GREEN}Autoscale completed{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error during autoscale: {e}{RESET}")
        return False


def set_probe_rigol(netname, ratio):
    """Set probe attenuation for Rigol oscilloscope."""
    try:
        net = get_rigol_net(netname)
        net.set_channel_probe(ratio, net.channel)
        channel = getattr(net, 'channel', '?')
        print(f"{GREEN}Channel {channel} probe attenuation set to {ratio}x{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error setting probe attenuation: {e}{RESET}")
        return False


# ============================================================================
# BASIC OPERATIONS - DISPATCH
# ============================================================================

def enable_net(netname):
    """Enable scope channel - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    channel = net_info.get("pin", 1)

    if is_picoscope(net_info):
        return enable_channel_pico(channel)
    elif is_rigol(net_info):
        return enable_channel_rigol(netname)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return enable_channel_rigol(netname)


def disable_net(netname):
    """Disable scope channel - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    channel = net_info.get("pin", 1)

    if is_picoscope(net_info):
        return disable_channel_pico(channel)
    elif is_rigol(net_info):
        return disable_channel_rigol(netname)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return disable_channel_rigol(netname)


def start_capture(netname):
    """Start capture - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        return start_capture_pico()
    elif is_rigol(net_info):
        return start_capture_rigol(netname)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return start_capture_rigol(netname)


def stop_capture(netname):
    """Stop capture - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        return stop_capture_pico()
    elif is_rigol(net_info):
        return stop_capture_rigol(netname)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return stop_capture_rigol(netname)


def start_single(netname):
    """Start single capture - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        return start_single_pico()
    elif is_rigol(net_info):
        return start_single_rigol(netname)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return start_single_rigol(netname)


def force_trigger(netname):
    """Force trigger - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        return force_trigger_pico()
    elif is_rigol(net_info):
        return force_trigger_rigol(netname)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return force_trigger_rigol(netname)


def set_scale(netname, volts_per_div):
    """Set vertical scale - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        print(f"{YELLOW}Use 'lager scope {netname} stream start --volts-per-div {volts_per_div}' for PicoScope{RESET}")
        return True
    elif is_rigol(net_info):
        return set_scale_rigol(netname, volts_per_div)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return set_scale_rigol(netname, volts_per_div)


def set_timebase(netname, seconds_per_div):
    """Set horizontal timebase - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        print(f"{YELLOW}Use 'lager scope {netname} stream start --time-per-div {seconds_per_div}' for PicoScope{RESET}")
        return True
    elif is_rigol(net_info):
        return set_timebase_rigol(netname, seconds_per_div)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return set_timebase_rigol(netname, seconds_per_div)


def do_autoscale(netname):
    """Perform autoscale - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        print(f"{YELLOW}Autoscale is not supported for PicoScope devices{RESET}")
        return True
    elif is_rigol(net_info):
        return do_autoscale_rigol(netname)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return do_autoscale_rigol(netname)


def set_probe(netname, ratio):
    """Set probe attenuation - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        print(f"{YELLOW}PicoScope probe attenuation is set via web UI{RESET}")
        return True
    elif is_rigol(net_info):
        return set_probe_rigol(netname, ratio)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return set_probe_rigol(netname, ratio)


def set_coupling_rigol(netname, mode):
    """Set channel coupling for Rigol oscilloscope."""
    try:
        net = get_rigol_net(netname)
        # Map to SCPI coupling values (DC, AC, GND)
        coupling_map = {"dc": "DC", "ac": "AC", "gnd": "GND"}
        scpi_mode = coupling_map.get(mode.lower(), "DC")
        net.set_channel_coupling(scpi_mode, net.channel)
        print(f"{GREEN}Coupling set to {mode.upper()}{RESET}")
        return True
    except Exception as e:
        print(f"{RED}Error setting coupling: {e}{RESET}")
        return False


def set_coupling(netname, mode):
    """Set channel coupling - dispatches to PicoScope or Rigol."""
    net_info = get_net_info(netname)
    if not net_info:
        print(f"{RED}Net '{netname}' not found or not a scope net{RESET}")
        return False

    if is_picoscope(net_info):
        print(f"{YELLOW}Use 'lager scope {netname} stream start --coupling {mode}' for PicoScope{RESET}")
        return True
    elif is_rigol(net_info):
        return set_coupling_rigol(netname, mode)
    else:
        print(f"{YELLOW}Unknown scope type for {netname}, trying Rigol...{RESET}")
        return set_coupling_rigol(netname, mode)


# ============================================================================
# MEASUREMENTS - RIGOL ONLY
# ============================================================================

def measure_vavg(netname, display, cursor):
    """Measure average voltage."""
    net = get_rigol_net(netname)
    return net.measurement.voltage_average(display=display, measurement_cursor=cursor)


def measure_vmax(netname, display, cursor):
    """Measure maximum voltage."""
    net = get_rigol_net(netname)
    return net.measurement.voltage_max(display=display, measurement_cursor=cursor)


def measure_vmin(netname, display, cursor):
    """Measure minimum voltage."""
    net = get_rigol_net(netname)
    return net.measurement.voltage_min(display=display, measurement_cursor=cursor)


def measure_vpp(netname, display, cursor):
    """Measure peak-to-peak voltage."""
    net = get_rigol_net(netname)
    return net.measurement.voltage_peak_to_peak(display=display, measurement_cursor=cursor)


def measure_vrms(netname, display, cursor):
    """Measure RMS voltage."""
    net = get_rigol_net(netname)
    return net.measurement.voltage_rms(display=display, measurement_cursor=cursor)


def measure_period(netname, display, cursor):
    """Measure waveform period."""
    net = get_rigol_net(netname)
    return net.measurement.period(display=display, measurement_cursor=cursor)


def measure_freq(netname, display, cursor):
    """Measure waveform frequency."""
    net = get_rigol_net(netname)
    return net.measurement.frequency(display=display, measurement_cursor=cursor)


def measure_dc_pos(netname, display, cursor):
    """Measure positive duty cycle."""
    net = get_rigol_net(netname)
    return net.measurement.duty_cycle_positive(display=display, measurement_cursor=cursor)


def measure_dc_neg(netname, display, cursor):
    """Measure negative duty cycle."""
    net = get_rigol_net(netname)
    return net.measurement.duty_cycle_negative(display=display, measurement_cursor=cursor)


def measure_pw_pos(netname, display, cursor):
    """Measure positive pulse width."""
    net = get_rigol_net(netname)
    return net.measurement.pulse_width_positive(display=display, measurement_cursor=cursor)


def measure_pw_neg(netname, display, cursor):
    """Measure negative pulse width."""
    net = get_rigol_net(netname)
    return net.measurement.pulse_width_negative(display=display, measurement_cursor=cursor)


# ============================================================================
# TRIGGER CONFIGURATION - PICOSCOPE
# ============================================================================

def trigger_edge_pico(netname, mode, coupling, source, slope, level):
    """Configure edge trigger for PicoScope."""
    errors = []

    # Set trigger mode
    if mode:
        response = send_command_pico({
            "command": "SetCaptureMode",
            "capture_mode": mode.lower()
        })
        if "error" in response:
            errors.append(f"mode: {response['error']}")

    # Set trigger level
    if level is not None:
        response = send_command_pico({
            "command": "SetTriggerLevel",
            "trigger_level": level
        })
        if "error" in response:
            errors.append(f"level: {response['error']}")

    # Set trigger slope
    if slope:
        slope_map = {"rising": "rising", "falling": "falling", "both": "either"}
        response = send_command_pico({
            "command": "SetTriggerSlope",
            "trigger_slope": slope_map.get(slope.lower(), "rising")
        })
        if "error" in response:
            errors.append(f"slope: {response['error']}")

    if errors:
        print(f"{YELLOW}Some settings could not be applied: {'; '.join(errors)}{RESET}")
    else:
        print(f"{GREEN}Trigger configured successfully{RESET}")


# ============================================================================
# TRIGGER CONFIGURATION - RIGOL
# ============================================================================

def trigger_edge(netname, mode, coupling, source, level, slope):
    """Configure edge trigger for Rigol oscilloscope."""
    target_net = get_rigol_net(netname)
    target_net.enable()
    source_net = get_source_net(source)
    if source_net:
        source_net.enable()

    if mode.lower() == "auto":
        target_net.trigger_settings.set_mode_auto()
    elif mode.lower() == "normal":
        target_net.trigger_settings.set_mode_normal()
    elif mode.lower() == "single":
        target_net.trigger_settings.set_mode_single()
    else:
        raise Exception(f"{mode} is not a valid option")

    if coupling.lower() == "dc":
        target_net.trigger_settings.set_coupling_DC()
    elif coupling.lower() == "ac":
        target_net.trigger_settings.set_coupling_AC()
    elif coupling.lower() == "low_freq_rej":
        target_net.trigger_settings.set_coupling_low_freq_reject()
    elif coupling.lower() == "high_freq_rej":
        target_net.trigger_settings.set_coupling_high_freq_reject()
    else:
        raise Exception(f"{coupling} type is not a valid option")

    from lager.nets.defines import TriggerType
    target_net.trigger_settings.set_type(TriggerType.Edge)
    if source_net:
        target_net.trigger_settings.edge.set_source(source_net)

    if level is not None:
        target_net.trigger_settings.edge.set_level(level)
    if slope:
        if slope.lower() == "rising":
            target_net.trigger_settings.edge.set_slope_rising()
        elif slope.lower() == "falling":
            target_net.trigger_settings.edge.set_slope_falling()
        elif slope.lower() == "both":
            target_net.trigger_settings.edge.set_slope_both()
        else:
            raise Exception(f"{slope} is not a valid option")

    print(f"{GREEN}Edge trigger configured{RESET}")


def trigger_uart(netname, mode, coupling, source, level, trigger_on, parity, stop_bits, baud, data_width, data):
    """Configure UART trigger for Rigol oscilloscope."""
    target_net = get_rigol_net(netname)
    target_net.enable()
    source_net = get_source_net(source)
    if source_net:
        source_net.enable()

    if mode.lower() == "auto":
        target_net.trigger_settings.set_mode_auto()
    elif mode.lower() == "normal":
        target_net.trigger_settings.set_mode_normal()
    elif mode.lower() == "single":
        target_net.trigger_settings.set_mode_single()
    else:
        raise Exception(f"{mode} is not a valid option")

    if coupling.lower() == "dc":
        target_net.trigger_settings.set_coupling_DC()
    elif coupling.lower() == "ac":
        target_net.trigger_settings.set_coupling_AC()
    elif coupling.lower() == "low_freq_rej":
        target_net.trigger_settings.set_coupling_low_freq_reject()
    elif coupling.lower() == "high_freq_rej":
        target_net.trigger_settings.set_coupling_high_freq_reject()
    else:
        raise Exception(f"{coupling} type is not a valid option")

    from lager.nets.defines import TriggerType, TriggerUARTParity
    target_net.trigger_settings.set_type(TriggerType.UART)

    if source_net:
        target_net.trigger_settings.uart.set_source(source_net)

    if level is not None:
        target_net.trigger_settings.uart.set_level(level)

    trig_parity = None
    if parity:
        if parity.lower() == "even":
            trig_parity = TriggerUARTParity.Even
        elif parity.lower() == "odd":
            trig_parity = TriggerUARTParity.Odd
        elif parity.lower() == "none":
            trig_parity = TriggerUARTParity.NoParity
        else:
            raise Exception(f"{parity} is not a valid option")
    target_net.trigger_settings.uart.set_uart_params(parity=trig_parity, stopbits=stop_bits, baud=baud, bits=data_width)

    if trigger_on:
        if trigger_on.lower() == "start":
            target_net.trigger_settings.uart.set_trigger_on_start()
        elif trigger_on.lower() == "error":
            target_net.trigger_settings.uart.set_trigger_on_error()
        elif trigger_on.lower() == "cerror":
            target_net.trigger_settings.uart.set_trigger_on_cerror()
        elif trigger_on.lower() == "data":
            target_net.trigger_settings.uart.set_trigger_on_data(data=data)
        else:
            raise Exception(f"{trigger_on} type is not a valid option")

    print(f"{GREEN}UART trigger configured{RESET}")


def trigger_pulse(netname, mode, coupling, source, level, trigger_on, upper, lower):
    """Configure pulse trigger for Rigol oscilloscope."""
    target_net = get_rigol_net(netname)
    target_net.enable()
    source_net = get_source_net(source)
    if source_net:
        source_net.enable()

    if mode.lower() == "auto":
        target_net.trigger_settings.set_mode_auto()
    elif mode.lower() == "normal":
        target_net.trigger_settings.set_mode_normal()
    elif mode.lower() == "single":
        target_net.trigger_settings.set_mode_single()
    else:
        raise Exception(f"{mode} is not a valid option")

    if coupling.lower() == "dc":
        target_net.trigger_settings.set_coupling_DC()
    elif coupling.lower() == "ac":
        target_net.trigger_settings.set_coupling_AC()
    elif coupling.lower() == "low_freq_rej":
        target_net.trigger_settings.set_coupling_low_freq_reject()
    elif coupling.lower() == "high_freq_rej":
        target_net.trigger_settings.set_coupling_high_freq_reject()
    else:
        raise Exception(f"{coupling} is not a valid option")

    from lager.nets.defines import TriggerType
    target_net.trigger_settings.set_type(TriggerType.Pulse)

    if source_net:
        target_net.trigger_settings.pulse.set_source(source_net)

    if level is not None:
        target_net.trigger_settings.pulse.set_level(level)

    if trigger_on:
        trigger_lower = trigger_on.lower()
        if trigger_lower in ("positive", "positive_greater"):
            if upper:
                target_net.trigger_settings.pulse.set_trigger_on_pulse_greater_than_width(upper)
        elif trigger_lower in ("negative", "negative_greater"):
            if upper:
                target_net.trigger_settings.pulse.set_trigger_on_pulse_greater_than_width(upper)
        elif trigger_lower == "positive_less":
            if lower:
                target_net.trigger_settings.pulse.set_trigger_on_pulse_less_than_width(lower)
        elif trigger_lower == "negative_less":
            if lower:
                target_net.trigger_settings.pulse.set_trigger_on_pulse_less_than_width(lower)
        else:
            raise Exception(f"{trigger_on} is not a valid option")

    print(f"{GREEN}Pulse trigger configured{RESET}")


def trigger_i2c(netname, mode, coupling, source_scl, level_scl, source_sda, level_sda, trigger_on, address, addr_width, data, data_width, direction):
    """Configure I2C trigger for Rigol oscilloscope."""
    target_net = get_rigol_net(netname)
    target_net.enable()

    source_scl_net = get_source_net(source_scl)
    if source_scl_net:
        source_scl_net.enable()

    source_sda_net = get_source_net(source_sda)
    if source_sda_net:
        source_sda_net.enable()

    if mode.lower() == "auto":
        target_net.trigger_settings.set_mode_auto()
    elif mode.lower() == "normal":
        target_net.trigger_settings.set_mode_normal()
    elif mode.lower() == "single":
        target_net.trigger_settings.set_mode_single()
    else:
        raise Exception(f"{mode} is not a valid option")

    if coupling.lower() == "dc":
        target_net.trigger_settings.set_coupling_DC()
    elif coupling.lower() == "ac":
        target_net.trigger_settings.set_coupling_AC()
    elif coupling.lower() == "low_freq_rej":
        target_net.trigger_settings.set_coupling_low_freq_reject()
    elif coupling.lower() == "high_freq_rej":
        target_net.trigger_settings.set_coupling_high_freq_reject()
    else:
        raise Exception(f"{coupling} is not a valid option")

    from lager.nets.defines import TriggerType, TriggerI2CDirection
    target_net.trigger_settings.set_type(TriggerType.I2C)

    target_net.trigger_settings.i2c.set_source(net_scl=source_scl_net, net_sda=source_sda_net)

    if level_scl is not None:
        target_net.trigger_settings.i2c.set_scl_trigger_level(level_scl)

    if level_sda is not None:
        target_net.trigger_settings.i2c.set_sda_trigger_level(level_sda)

    if direction:
        if direction == 'write':
            direction = TriggerI2CDirection.Write
        elif direction == 'read':
            direction = TriggerI2CDirection.Read
        elif direction == 'rw':
            direction = TriggerI2CDirection.RW
        else:
            raise Exception(f"{direction} is not a valid option")

    if trigger_on:
        if trigger_on.lower() == "start":
            target_net.trigger_settings.i2c.set_trigger_on_start()
        elif trigger_on.lower() == "restart":
            target_net.trigger_settings.i2c.set_trigger_on_restart()
        elif trigger_on.lower() == "stop":
            target_net.trigger_settings.i2c.set_trigger_on_stop()
        elif trigger_on.lower() == "nack":
            target_net.trigger_settings.i2c.set_trigger_on_nack()
        elif trigger_on.lower() == "address":
            target_net.trigger_settings.i2c.set_trigger_on_address(bits=addr_width, direction=direction, address=address)
        elif trigger_on.lower() == "data":
            target_net.trigger_settings.i2c.set_trigger_on_data(width=data_width, data=data)
        elif trigger_on.lower() == "addr_data":
            target_net.trigger_settings.i2c.set_trigger_on_addr_data(bits=addr_width, direction=direction, address=address, width=data_width, data=data)
        else:
            raise Exception(f"{trigger_on} is not a valid option")

    print(f"{GREEN}I2C trigger configured{RESET}")


def trigger_spi(netname, mode, coupling, source_mosi_miso, source_sck, source_cs, level_mosi_miso, level_sck, level_cs, data, data_width, clk_slope, trigger_on, cs_idle, timeout):
    """Configure SPI trigger for Rigol oscilloscope."""
    target_net = get_rigol_net(netname)
    target_net.enable()

    source_mosi_miso_net = get_source_net(source_mosi_miso)
    if source_mosi_miso_net:
        source_mosi_miso_net.enable()

    source_sck_net = get_source_net(source_sck)
    if source_sck_net:
        source_sck_net.enable()

    source_cs_net = get_source_net(source_cs)
    if source_cs_net:
        source_cs_net.enable()

    if mode.lower() == "auto":
        target_net.trigger_settings.set_mode_auto()
    elif mode.lower() == "normal":
        target_net.trigger_settings.set_mode_normal()
    elif mode.lower() == "single":
        target_net.trigger_settings.set_mode_single()
    else:
        raise Exception(f"{mode} is not a valid option")

    if coupling.lower() == "dc":
        target_net.trigger_settings.set_coupling_DC()
    elif coupling.lower() == "ac":
        target_net.trigger_settings.set_coupling_AC()
    elif coupling.lower() == "low_freq_rej":
        target_net.trigger_settings.set_coupling_low_freq_reject()
    elif coupling.lower() == "high_freq_rej":
        target_net.trigger_settings.set_coupling_high_freq_reject()
    else:
        raise Exception(f"{coupling} is not a valid option")

    from lager.nets.defines import TriggerType
    target_net.trigger_settings.set_type(TriggerType.SPI)
    target_net.trigger_settings.spi.set_source(net_sck=source_sck_net, net_mosi_miso=source_mosi_miso_net, net_cs=source_cs_net)

    if level_mosi_miso is not None:
        target_net.trigger_settings.spi.set_mosi_miso_trigger_level(level_mosi_miso)

    if level_sck is not None:
        target_net.trigger_settings.spi.set_sck_trigger_level(level_sck)

    if level_cs is not None:
        target_net.trigger_settings.spi.set_cs_trigger_level(level_cs)

    target_net.trigger_settings.spi.set_trigger_data(bits=data_width, data=data)

    if clk_slope:
        if clk_slope.lower() == "positive":
            target_net.trigger_settings.spi.set_clk_edge_positive()
        elif clk_slope.lower() == "negative":
            target_net.trigger_settings.spi.set_clk_edge_negative()

    if trigger_on:
        if trigger_on.lower() == "timeout":
            if timeout is not None:
                target_net.trigger_settings.spi.set_trigger_on_timeout(timeout)
        elif trigger_on.lower() == "cs":
            if cs_idle:
                if cs_idle.lower() == "high":
                    target_net.trigger_settings.spi.set_trigger_on_cs_low()
                elif cs_idle.lower() == "low":
                    target_net.trigger_settings.spi.set_trigger_on_cs_high()
                else:
                    raise Exception(f"{cs_idle} is not a valid option")
        else:
            raise Exception(f"{trigger_on} is not a valid option")

    print(f"{GREEN}SPI trigger configured{RESET}")


# ============================================================================
# CURSOR CONTROL - RIGOL ONLY
# ============================================================================

def set_cursor_a(netname, x, y):
    """Set cursor A position."""
    net = get_rigol_net(netname)
    net.cursor.set_a(x=x, y=y)
    print(f"{GREEN}Cursor A set{RESET}")


def set_cursor_b(netname, x, y):
    """Set cursor B position."""
    net = get_rigol_net(netname)
    net.cursor.set_b(x=x, y=y)
    print(f"{GREEN}Cursor B set{RESET}")


def move_cursor_a(netname, del_x, del_y):
    """Move cursor A by delta."""
    net = get_rigol_net(netname)
    net.cursor.move_a(x_del=del_x, y_del=del_y)
    print(f"{GREEN}Cursor A moved{RESET}")


def move_cursor_b(netname, del_x, del_y):
    """Move cursor B by delta."""
    net = get_rigol_net(netname)
    net.cursor.move_b(x_del=del_x, y_del=del_y)
    print(f"{GREEN}Cursor B moved{RESET}")


def hide_cursor(netname):
    """Hide the cursors."""
    net = get_rigol_net(netname)
    net.cursor.hide()
    print(f"{GREEN}Cursors hidden{RESET}")


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def main():
    """Main entry point for all scope operations."""
    command_data = os.environ.get("LAGER_COMMAND_DATA", "{}")

    try:
        data = json.loads(command_data)
    except json.JSONDecodeError as e:
        print(f"{RED}Error parsing command data: {e}{RESET}", file=sys.stderr)
        sys.exit(1)

    action = data.get("action", "")
    params = data.get("params", {})
    netname = params.get("netname")

    if not netname:
        print(f"{RED}No netname provided{RESET}", file=sys.stderr)
        sys.exit(1)

    # Get net info to determine device type
    net_info = get_net_info(netname)

    # ========== BASIC OPERATIONS ==========
    basic_operations = {
        "enable_net": lambda: enable_net(netname),
        "disable_net": lambda: disable_net(netname),
        "start_capture": lambda: start_capture(netname),
        "stop_capture": lambda: stop_capture(netname),
        "start_single": lambda: start_single(netname),
        "force_trigger": lambda: force_trigger(netname),
    }

    if action in basic_operations:
        success = basic_operations[action]()
        sys.exit(0 if success else 1)

    # ========== CHANNEL SETTINGS ==========
    if action == 'set_scale':
        try:
            volts_per_div = params.get('volts_per_div')
            if volts_per_div is None:
                print(f"{RED}No volts_per_div value provided{RESET}", file=sys.stderr)
                sys.exit(1)
            success = set_scale(netname, volts_per_div)
            sys.exit(0 if success else 1)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    if action == 'set_coupling':
        try:
            mode = params.get('mode')
            if mode is None:
                print(f"{RED}No coupling mode provided{RESET}", file=sys.stderr)
                sys.exit(1)
            success = set_coupling(netname, mode)
            sys.exit(0 if success else 1)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    if action == 'set_probe':
        try:
            ratio = params.get('ratio')
            if ratio is None:
                print(f"{RED}No probe ratio value provided{RESET}", file=sys.stderr)
                sys.exit(1)
            success = set_probe(netname, ratio)
            sys.exit(0 if success else 1)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    if action == 'autoscale':
        try:
            success = do_autoscale(netname)
            sys.exit(0 if success else 1)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    if action == 'set_timebase':
        try:
            seconds_per_div = params.get('seconds_per_div')
            if seconds_per_div is None:
                print(f"{RED}No seconds_per_div value provided{RESET}", file=sys.stderr)
                sys.exit(1)
            success = set_timebase(netname, seconds_per_div)
            sys.exit(0 if success else 1)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    # ========== MEASUREMENTS ==========
    # PicoScope measurements not supported - show message
    if action.startswith('measure_') and is_picoscope(net_info):
        print(f"{YELLOW}Measurements are not supported for PicoScope devices.{RESET}")
        print(f"{YELLOW}Use 'lager scope {netname} stream start' for streaming data.{RESET}")
        sys.exit(0)

    # Rigol measurements
    measurement_actions = {
        'measure_vavg': measure_vavg,
        'measure_vmax': measure_vmax,
        'measure_vmin': measure_vmin,
        'measure_vpp': measure_vpp,
        'measure_vrms': measure_vrms,
        'measure_period': measure_period,
        'measure_freq': measure_freq,
        'measure_dc_pos': measure_dc_pos,
        'measure_dc_neg': measure_dc_neg,
        'measure_pulse_width_pos': measure_pw_pos,
        'measure_pulse_width_neg': measure_pw_neg,
    }

    if action in measurement_actions:
        try:
            display = params.get('display', False)
            cursor = params.get('cursor', False)
            result = measurement_actions[action](netname, display, cursor)

            # Handle None results with helpful message
            if result is None:
                # Frequency/period measurements on DC signals return None
                if action in ['measure_freq', 'measure_period']:
                    print(f"{YELLOW}No frequency/period detected.{RESET}")
                    print(f"{YELLOW}The signal may be DC or outside the measurable range.{RESET}")
                    print(f"{YELLOW}Tip: Check that channel is enabled and has an AC signal.{RESET}")
                else:
                    print(f"{YELLOW}Measurement returned no data.{RESET}")
                    print(f"{YELLOW}Ensure the channel is enabled and signal is within range.{RESET}")
            else:
                print(f"{GREEN}{result}{RESET}")
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    # ========== TRIGGER CONFIGURATION ==========
    # PicoScope only supports edge trigger
    if action.startswith('trigger_') and is_picoscope(net_info):
        if action == 'trigger_edge':
            trigger_edge_pico(
                netname,
                params.get('mode', 'auto'),
                params.get('coupling', 'dc'),
                params.get('source'),
                params.get('slope'),
                params.get('level')
            )
            sys.exit(0)
        else:
            print(f"{YELLOW}Only edge trigger is supported for PicoScope.{RESET}")
            print(f"{YELLOW}Use: lager scope {netname} trigger edge --slope rising --level 0{RESET}")
            sys.exit(0)

    # Rigol trigger configuration
    if action == 'trigger_edge':
        try:
            trigger_edge(
                params.get('netname'),
                params.get('mode', 'normal'),
                params.get('coupling', 'dc'),
                params.get('source'),
                params.get('level'),
                params.get('slope')
            )
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)
    elif action == 'trigger_uart':
        try:
            trigger_uart(**params)
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)
    elif action == 'trigger_i2c':
        try:
            trigger_i2c(**params)
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)
    elif action == 'trigger_spi':
        try:
            trigger_spi(**params)
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)
    elif action == 'trigger_pulse':
        try:
            trigger_pulse(**params)
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    # ========== CURSOR CONTROL ==========
    # PicoScope cursor control not supported
    if action.startswith(('set_', 'move_', 'hide_')) and is_picoscope(net_info):
        print(f"{YELLOW}Cursor control is not supported for PicoScope devices.{RESET}")
        print(f"{YELLOW}Use the web visualization for cursor measurements.{RESET}")
        sys.exit(0)

    # Rigol cursor operations
    cursor_actions = {
        'set_a': lambda: set_cursor_a(**params),
        'move_a': lambda: move_cursor_a(**params),
        'set_b': lambda: set_cursor_b(**params),
        'move_b': lambda: move_cursor_b(**params),
        'hide_cursor': lambda: hide_cursor(**params),
    }

    if action in cursor_actions:
        try:
            cursor_actions[action]()
            sys.exit(0)
        except Exception as e:
            print(f"{RED}Error: {e}{RESET}", file=sys.stderr)
            sys.exit(1)

    # Unknown action
    print(f"{RED}Unknown action: {action}{RESET}", file=sys.stderr)
    print(f"Available action categories: basic_operations, measurements, triggers, cursors", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()

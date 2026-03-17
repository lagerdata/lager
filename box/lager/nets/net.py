# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import sys
import json
import traceback
from typing import Any, Dict, List

import requests  # used by get_state(); safe to keep

from .device import Device
from .mux import Mux
from .constants import HARDWARE_PORT, NetType
from .defines import (
    TriggerType,
    TriggerStatus,
    TriggerCoupling,
    TriggerMode,
    TriggerEdgeSlope,
    TriggerSlopeCondition,
    TriggerSlopeWindow,
    TriggerPulseCondition,
    TriggerUARTCondition,
    TriggerUARTParity,
    TriggerI2CCondition,
    TriggerI2CDirection,
    TriggerSPICondition,
    TriggerSPISlope,
    TriggerSPICSMode,
    TriggerCANCondition,
    TriggerCANSigType,
    SimMode,
    Mode,
)
from .mappers import (
    RigolMSO5000AnalogMapper,
    RigolMSO5000LogicMapper,
    RigolDP800FunctionMapper,
    RigolDL3000FunctionMapper,
    KeithleyBatteryFunctionMapper,
    KeithleyPowerSupplyFunctionMapper,
    PassthroughFunctionMapper,
    EAMapper,
    KeysightE36000FunctionMapper,
)

# -------- debug net import (backward compatibility re-export) --------
from .debug_net import DebugNet, _NullDebug, make_debug, _debug_available

from ..io.gpio.labjack_t7 import LabJackGPIO
from ..io.gpio.usb202 import USB202GPIO
from ..io.gpio.ft232h_gpio import FT232HGPIO
from ..io.gpio.aardvark_gpio import AardvarkGPIO
from ..io.adc.labjack_t7 import LabJackADC
from ..io.adc.usb202 import USB202ADC
from ..io.dac.labjack_t7 import LabJackDAC
from ..io.dac.usb202 import USB202DAC
from ..measurement.thermocouple.phidget import PhidgetThermocouple
from ..measurement.watt.yocto_watt import YoctoWatt
from ..measurement.watt.joulescope_js220 import JoulescopeJS220
from ..measurement.energy_analyzer.joulescope_energy import JoulescopeEnergyAnalyzer
from ..automation.arm.rotrics import Dexarm
from ..rotation import Rotation
from ..actuate import Actuate
from ..protocols.wifi import Wifi
from ..protocols.mikrotik.router import MikroTikRouter
from ..protocols.uart.uart_net import UARTNet
from ..protocols.spi.spi_net import SPINet
from ..protocols.i2c.i2c_net import I2CNet
from ..automation.usb_hub.usb_net_wrapper import USBNetWrapper
from ..automation.webcam.webcam_net_wrapper import WebcamNetWrapper
from ..cache import get_nets_cache


# ------------------------------- constants -------------------------------

LOCAL_NETS_PATH = "/etc/lager/saved_nets.json"


# ------------------------------ HTTP helpers -----------------------------

def get_state() -> Dict[str, Any]:
    resp = requests.get(f"http://hardware:{HARDWARE_PORT}/equipment")
    resp.raise_for_status()
    return resp.json()


# ------------------------------- mappers --------------------------------

def mapper_factory(net, device_type, net_info):
    if device_type == "rigol_mso5000":
        if net.type == NetType.Analog:
            return RigolMSO5000AnalogMapper(net, Device(device_type, net_info))
        elif net.type == NetType.Logic:
            return RigolMSO5000LogicMapper(net, Device(device_type, net_info))
    elif device_type == "picoscope_2000":
        # Picoscope uses passthrough mapper - actual implementation via websocket daemon
        return PassThroughMapper(net, Device(device_type, net_info))
    elif device_type in ("rigol_dp800", "rigol_dp800_2"):
        return RigolDP800FunctionMapper(net, Device(device_type, net_info))
    elif device_type in ("rigol_dl3000", "rigol_dl3021"):
        # Use explicit mapper for consistent API behavior
        from .mappers.rigol_dl3000 import RigolDL3000FunctionMapper
        return RigolDL3000FunctionMapper(net, Device(device_type, net_info))
    elif device_type in ("keithley", "keithley_2", "keithley_3"):
        if net.type == NetType.Battery:
            # Use unique device name for battery to avoid import conflict with supply
            return KeithleyBatteryFunctionMapper(net, Device("keithley_battery", net_info))
        elif net.type == NetType.PowerSupply:
            return KeithleyPowerSupplyFunctionMapper(net, Device(device_type, net_info))
        else:
            raise ValueError(f"Invalid device type `{device_type}` for keithley")
    elif device_type.startswith("ea"):
        return EAMapper(net, Device(device_type, net_info))
    elif device_type == "keysight_e36000":
        return KeysightE36000FunctionMapper(net, Device(device_type, net_info))
    raise TypeError(f"Invalid mapper type {device_type}")


# ------------------------------ passthrough ------------------------------

class PassThroughMapper:
    def __init__(self, net, device):
        self.net = net
        self.device = device

    def __getattr__(self, attr):
        return getattr(self.device, attr)


# ------------------------------- errors ---------------------------------

class InvalidNetError(Exception):
    def __str__(self):
        return f"Invalid Net: {self.args[0]}"
    def __repr__(self):
        return str(self)


class SetupFunctionRequiredError(Exception):
    def __str__(self):
        return f"Setup function required for Net {self.args[0]} (type {self.args[1]})"
    def __repr__(self):
        return str(self)


# ------------------------------- helpers --------------------------------

def _ensure_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)


def _load_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return []


def _atomic_write_json(path: str, payload: Any) -> None:
    _ensure_dir(path)
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        # Clean up temp file if something went wrong
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def channel_name_to_number(name):
    if isinstance(name, int):
        return name
    try:
        return int(name, 10)
    except ValueError:
        pass

    if name not in ("A", "B", "C", "D"):
        raise ValueError(f"Invalid channel: {name}")
    return ord(name) - ord("A") + 1


def channel_num(mux, mapping):
    point = mux["scope_points"][0][1]
    if mux["role"] == "analog":
        return ord(point) - ord("A") + 1
    if mux["role"] == "logic":
        return int(point)
    try:
        numeric = int(point, 10)
        return numeric
    except ValueError:
        return ord(point) - ord("A") + 1


# --------------------------------- Net ----------------------------------

class Net:
    # ---------- saved nets file (used by TUI and `lager nets`) ----------

    @classmethod
    def get_local_nets(cls) -> List[Dict[str, Any]]:
        """Get all saved nets using cached lookup."""
        return get_nets_cache().get_nets()

    @classmethod
    def save_local_nets(cls, nets: List[Dict[str, Any]]) -> None:
        if not isinstance(nets, list):
            nets = []
        _atomic_write_json(LOCAL_NETS_PATH, nets)
        # Invalidate cache so next access sees the new data
        get_nets_cache().invalidate()

    @classmethod
    def filter_nets(cls, all_nets, name, role=None):
        if role is None:
            return [net for net in all_nets if net.get("name") != name]
        return [net for net in all_nets if not (net.get("name") == name and net.get("role") == role)]

    @classmethod
    def delete_local_net(cls, name, role=None) -> bool:
        local_nets = cls.get_local_nets()
        new_nets = cls.filter_nets(local_nets, name, role)
        changed = (len(new_nets) != len(local_nets))
        if changed:
            cls.save_local_nets(new_nets)
        return changed

    @classmethod
    def delete_all_local_nets(cls) -> bool:
        cls.save_local_nets([])
        return True

    @classmethod
    def rename_local_net(cls, old_name: str, new_name: str) -> bool:
        local_nets = cls.get_local_nets()
        changed = False
        renamed_net_role = None
        for n in local_nets:
            if n.get("name") == old_name:
                n["name"] = new_name
                renamed_net_role = n.get("role")
                changed = True
        if changed:
            cls.save_local_nets(local_nets)

            # If we renamed a webcam net, also rename any active webcam stream
            if renamed_net_role == "webcam":
                try:
                    from ..automation.webcam import rename_stream
                    rename_stream(old_name, new_name)
                except Exception:
                    # Don't fail the net rename if webcam rename fails
                    # (e.g., webcam module not available, stream not active)
                    pass
        return changed

    @classmethod
    def save_local_net(cls, data: Dict[str, Any]) -> None:
        # normalize minimal mapping info expected elsewhere
        pin = data.get("pin", 0)
        mapping = {"net": data["name"], "pin": pin, "location": str(pin)}
        if data.get("address"):
            mapping["device_override"] = data["address"]
        if data.get("serial") is not None:
            mapping["serial"] = data.get("serial")
        if data.get("port"):
            mapping["port"] = data.get("port")
        if data.get("channel_key"):
            mapping["channel_key"] = data.get("channel_key")
        data["mappings"] = [mapping]
        data["scope_points"] = [[pin, str(pin)]]

        local_nets = cls.get_local_nets()
        # de-dup by name+role
        filtered = [n for n in local_nets if not (n.get("name") == data["name"] and n.get("role") == data.get("role"))]
        filtered.append(data)
        cls.save_local_nets(filtered)

    # ---------- hardware/env nets (historical list_all behavior) ----------

    @classmethod
    def list_all_from_env(cls) -> List[Dict[str, Any]]:
        """
        Historical behavior: list nets from LAGER_MUXES env (if provided by the launcher).
        Returns [{'name', 'role', 'channel'}, ...]
        """
        muxes = []
        try:
            muxes = json.loads(os.getenv("LAGER_MUXES", "[]"))
        except Exception:
            traceback.print_exc(file=sys.stderr)
            muxes = []

        output = []
        for mux in muxes:
            for mapping in mux.get("mappings", []):
                channel = channel_num(mux, mapping)
                output.append({"name": mapping.get("net"), "role": mux.get("role"), "channel": channel})
        return output

    @classmethod
    def list_saved(cls) -> List[Dict[str, Any]]:
        """
        What the TUI expects: list of nets persisted on the box.
        """
        nets = cls.get_local_nets()
        # Keep it simple: return as-is (already JSON-serializable).
        return nets

    # ---------- factory ----------
    @classmethod
    def get_from_saved_json(cls, name, role):
        saved_nets = cls.get_local_nets()

        def _norm_pin(item):
            pin = item.get("pin") or item.get("channel")
            try:
                return int(pin)
            except Exception:
                return pin

        def _get_location(item):
            loc = item.get("location")
            if loc is None:
                mappings = item.get("mappings") or []
                if mappings and isinstance(mappings[0], dict):
                    loc = mappings[0].get("location")
            return loc

        for item in saved_nets:
            # Skip nets with unknown roles (e.g., webcam) that aren't in NetType enum
            try:
                item_role = NetType.from_role(item['role'])
            except KeyError:
                continue
            if name == item.get("name") and role and role == item_role:
                if role == NetType.PowerSupply:
                    net = cls.__new__(cls)
                    net.name = name

                    # Get channel/pin from item
                    channel = _norm_pin(item)

                    # Set mapping to item's mappings if available, otherwise create a simple one
                    mappings = item.get("mappings", [])
                    if mappings and isinstance(mappings, list) and len(mappings) > 0:
                        net.mapping = mappings[0]
                    else:
                        net.mapping = {"net": name, "pin": channel}

                    net.type = NetType.PowerSupply
                    instrument_lower = item['instrument'].lower()
                    if 'keithley' in instrument_lower:
                        net.device_type = 'keithley'
                    elif 'rigol_dp' in instrument_lower or instrument_lower.startswith('dp8'):
                        # Covers DP800, DP821, DP831, etc.
                        net.device_type = 'rigol_dp800'
                    elif 'rigol_dl' in instrument_lower or 'dl3000' in instrument_lower:
                        net.device_type = 'rigol_dl3000'
                    elif 'keysight' in instrument_lower and 'e36' in instrument_lower:
                        # Covers all E36xxx series: E36233A (E36200), E36311A/E36312A/E36313A (E36300)
                        net.device_type = 'keysight_e36000'
                    elif 'ea' in instrument_lower:
                        net.device_type = 'ea'
                    else:
                        raise RuntimeError(f'Unknown power supply device type: {item["instrument"]}')

                    # Set channel and mux - for power supplies, use the channel directly
                    net.channel = channel
                    net.mux = None  # Power supplies don't use mux

                    net.setup_commands = []
                    net.teardown_function = None
                    net.setup_function = None
                    net.device = mapper_factory(net, net.device_type, item)
                    return net

                if role == NetType.ELoad:
                    net = cls.__new__(cls)
                    net.name = name

                    # Get channel/pin from item
                    channel = _norm_pin(item)

                    # Set mapping to item's mappings if available, otherwise create a simple one
                    mappings = item.get("mappings", [])
                    if mappings and isinstance(mappings, list) and len(mappings) > 0:
                        net.mapping = mappings[0]
                    else:
                        net.mapping = {"net": name, "pin": channel}

                    net.type = NetType.ELoad
                    instrument_lower = item['instrument'].lower()
                    if 'rigol_dl' in instrument_lower or 'dl3' in instrument_lower:
                        net.device_type = 'rigol_dl3021'
                    else:
                        raise RuntimeError(f'Unknown eload device type: {item["instrument"]}')

                    # Set channel and mux - for eloads, use the channel directly
                    net.channel = channel
                    net.mux = None  # ELoads don't use mux

                    net.setup_commands = []
                    net.teardown_function = None
                    net.setup_function = None
                    net.device = mapper_factory(net, net.device_type, item)
                    return net

                if role == NetType.Battery:
                    net = cls.__new__(cls)
                    net.name = name

                    # Get channel/pin from item
                    channel = _norm_pin(item)

                    # Set mapping to item's mappings if available, otherwise create a simple one
                    mappings = item.get("mappings", [])
                    if mappings and isinstance(mappings, list) and len(mappings) > 0:
                        net.mapping = mappings[0]
                    else:
                        net.mapping = {"net": name, "pin": channel}

                    net.type = NetType.Battery
                    instrument_lower = item['instrument'].lower()
                    if 'keithley' in instrument_lower:
                        net.device_type = 'keithley'
                    else:
                        raise RuntimeError(f'Unknown battery simulator device type: {item["instrument"]}')

                    # Set channel and mux - for battery simulators, use the channel directly
                    net.channel = channel
                    net.mux = None  # Battery simulators don't use mux

                    net.setup_commands = []
                    net.teardown_function = None
                    net.setup_function = None
                    net.device = mapper_factory(net, net.device_type, item)
                    return net

                if role == NetType.GPIO:
                    # Check for instrument type - support MCC_USB-202, FT232H, or default to LabJack
                    instrument = item.get('instrument', '').lower()
                    if 'usb-202' in instrument or 'usb202' in instrument or 'mcc_usb-202' in instrument:
                        unique_id = item.get('unique_id') or item.get('address')
                        return USB202GPIO(name, _norm_pin(item), unique_id=unique_id)
                    elif 'ft232h' in instrument or 'ftdi' in instrument:
                        address = item.get('address') or ''
                        serial = None
                        if '::' in address:
                            parts = address.split('::')
                            if len(parts) >= 4:
                                serial = parts[3]
                        elif address and not address.startswith('ftdi://'):
                            serial = address
                        return FT232HGPIO(name, _norm_pin(item), serial=serial)
                    elif 'aardvark' in instrument or 'totalphase' in instrument:
                        params = item.get('params') or {}
                        port = int(params.get('port', 0))
                        target_power = bool(params.get('target_power', False))
                        serial = item.get('address') or None
                        return AardvarkGPIO(name, _norm_pin(item), port=port,
                                            serial=serial, target_power=target_power)
                    else:
                        # Default to LabJack for backward compatibility
                        return LabJackGPIO(name, _norm_pin(item))

                if role == NetType.ADC:
                    # Check for instrument type - support MCC_USB-202 or default to LabJack
                    instrument = item.get('instrument', '').lower()
                    if 'usb-202' in instrument or 'usb202' in instrument or 'mcc_usb-202' in instrument:
                        unique_id = item.get('unique_id') or item.get('address')
                        return USB202ADC(name, _norm_pin(item), unique_id=unique_id)
                    else:
                        # Default to LabJack for backward compatibility
                        return LabJackADC(name, _norm_pin(item))

                if role == NetType.DAC:
                    # Check for instrument type - support MCC_USB-202 or default to LabJack
                    instrument = item.get('instrument', '').lower()
                    if 'usb-202' in instrument or 'usb202' in instrument or 'mcc_usb-202' in instrument:
                        unique_id = item.get('unique_id') or item.get('address')
                        return USB202DAC(name, _norm_pin(item), unique_id=unique_id)
                    else:
                        # Default to LabJack for backward compatibility
                        return LabJackDAC(name, _norm_pin(item))

                if role == NetType.Debug:
                    # use optional make_debug (pass net_info for device configuration)
                    return make_debug(name, item)

                if role == NetType.Thermocouple:
                    return PhidgetThermocouple(name, _norm_pin(item), _get_location(item))

                if role == NetType.WattMeter:
                    instrument = (item.get('instrument') or '').lower()
                    if 'joulescope' in instrument or 'js220' in instrument:
                        return JoulescopeJS220(name, _norm_pin(item), _get_location(item))
                    return YoctoWatt(name, _norm_pin(item), _get_location(item))

                if role == NetType.EnergyAnalyzer:
                    instrument = (item.get('instrument') or '').lower()
                    if 'joulescope' in instrument or 'js220' in instrument:
                        return JoulescopeEnergyAnalyzer(name, _norm_pin(item), _get_location(item))
                    raise RuntimeError(f"Unsupported energy-analyzer instrument: {item.get('instrument')}")

                if role == NetType.Rotation:
                    return Rotation(name, _norm_pin(item), _get_location(item))

                if role == NetType.Wifi:
                    return Wifi(name, _norm_pin(item), _get_location(item))

                if role == NetType.Router:
                    address = item.get('address', '')
                    location = _get_location(item)
                    if isinstance(location, dict):
                        username = location.get('username', 'admin')
                        password = location.get('password', '')
                        use_ssl = location.get('use_ssl', False)
                    else:
                        username = item.get('username', 'admin')
                        password = item.get('password', '')
                        use_ssl = False
                    return MikroTikRouter(name, address, username, password, use_ssl=use_ssl)

                if role == NetType.Actuate:
                    return Actuate(name, _norm_pin(item), _get_location(item))

                if role == NetType.Arm:
                    serial = (
                        item.get("serial")
                        or ((item.get("location") or {}) if isinstance(item.get("location"), dict) else {}).get("serial_number")
                        or None
                    )
                    port = item.get("port") or None
                    pin_val = _norm_pin(item)
                    return Dexarm(port=port, serial_number=serial, name=name, pin=pin_val)

                if role == NetType.UART:
                    return UARTNet(name, item)

                if role == NetType.Usb:
                    return USBNetWrapper(name, item)

                if role == NetType.Webcam:
                    return WebcamNetWrapper(name, item)

                if role == NetType.SPI:
                    return SPINet(name, item)

                if role == NetType.I2C:
                    return I2CNet(name, item)

                if role in (NetType.Analog, NetType.Logic):
                    # Handle scope/analog nets (e.g., Rigol oscilloscopes)
                    net = cls.__new__(cls)
                    net.name = name
                    channel = _norm_pin(item)
                    mappings = item.get("mappings", [])
                    if mappings and isinstance(mappings, list) and len(mappings) > 0:
                        net.mapping = mappings[0]
                    else:
                        net.mapping = {"net": name, "pin": channel}
                    net.type = role
                    instrument_lower = item['instrument'].lower()
                    if 'rigol' in instrument_lower or 'mso' in instrument_lower or 'ms0' in instrument_lower:
                        net.device_type = 'rigol_mso5000'
                    elif 'picoscope' in instrument_lower or 'pico' in instrument_lower:
                        # PicoScope handled via streaming daemon
                        net.device_type = 'picoscope_2000'
                    else:
                        raise RuntimeError(f'Unknown scope device type: {item["instrument"]}')
                    net.channel = channel
                    net.mux = None
                    net.setup_commands = []
                    net.teardown_function = None
                    net.setup_function = None
                    net.device = mapper_factory(net, net.device_type, item)
                    return net

        return None

    @classmethod
    def get(cls, name, type, *, setup_function=None, teardown_function=None):
        local_net = cls.get_from_saved_json(name, type)
        if local_net:
            return local_net

        muxes = cls.get_local_nets()
        if not muxes:
            try:
                muxes = json.loads(os.getenv("LAGER_MUXES", "[]"))
            except Exception:
                muxes = []

        if type is None or type in (
            NetType.GPIO,
            NetType.ADC,
            NetType.DAC,
            NetType.Thermocouple,
            NetType.WattMeter,
            NetType.EnergyAnalyzer,
            NetType.Rotation,
            NetType.Wifi,
            NetType.Router,
            NetType.Actuate,
            NetType.Debug,
            NetType.Analog,
            NetType.Logic,
            NetType.UART,
            NetType.Usb,
        ):
            for mux in muxes:
                # Skip nets with unknown roles (e.g., webcam) that aren't in NetType enum
                try:
                    mux_role = NetType.from_role(mux["role"])
                except KeyError:
                    continue
                for mapping in mux.get("mappings", []):
                    if mapping.get("net") == name and (type is None or type == mux_role):
                        _, pin = mux["scope_points"][0]

                        if mux_role == NetType.GPIO:
                            try:
                                norm_pin = int(pin)
                            except Exception:
                                norm_pin = pin
                            # Check for instrument type - support MCC_USB-202, FT232H, or default to LabJack
                            instrument = mapping.get('instrument', '').lower()
                            if 'usb-202' in instrument or 'usb202' in instrument or 'mcc_usb-202' in instrument:
                                unique_id = mapping.get('unique_id') or mapping.get('device_override')
                                return USB202GPIO(name, norm_pin, unique_id=unique_id)
                            elif 'ft232h' in instrument or 'ftdi' in instrument:
                                address = mapping.get('device_override') or ''
                                serial = None
                                if '::' in address:
                                    parts = address.split('::')
                                    if len(parts) >= 4:
                                        serial = parts[3]
                                elif address and not address.startswith('ftdi://'):
                                    serial = address
                                return FT232HGPIO(name, norm_pin, serial=serial)
                            elif 'aardvark' in instrument or 'totalphase' in instrument:
                                params = mapping.get('params') or {}
                                port = int(params.get('port', 0))
                                target_power = bool(params.get('target_power', False))
                                serial = mapping.get('device_override') or None
                                return AardvarkGPIO(name, norm_pin, port=port,
                                                    serial=serial, target_power=target_power)
                            else:
                                return LabJackGPIO(name, norm_pin)

                        if mux_role == NetType.ADC:
                            try:
                                norm_pin = int(pin)
                            except Exception:
                                norm_pin = pin
                            # Check for instrument type - support MCC_USB-202 or default to LabJack
                            instrument = mapping.get('instrument', '').lower()
                            if 'usb-202' in instrument or 'usb202' in instrument or 'mcc_usb-202' in instrument:
                                unique_id = mapping.get('unique_id') or mapping.get('device_override')
                                return USB202ADC(name, norm_pin, unique_id=unique_id)
                            else:
                                return LabJackADC(name, norm_pin)

                        if mux_role == NetType.DAC:
                            try:
                                norm_pin = int(pin)
                            except Exception:
                                norm_pin = pin
                            # Check for instrument type - support MCC_USB-202 or default to LabJack
                            instrument = mapping.get('instrument', '').lower()
                            if 'usb-202' in instrument or 'usb202' in instrument or 'mcc_usb-202' in instrument:
                                unique_id = mapping.get('unique_id') or mapping.get('device_override')
                                return USB202DAC(name, norm_pin, unique_id=unique_id)
                            else:
                                return LabJackDAC(name, norm_pin)

                        if mux_role == NetType.Debug:
                            return make_debug(name, mapping)

                        if mux_role == NetType.Thermocouple:
                            return PhidgetThermocouple(name, int(pin), mapping["location"])
                        if mux_role == NetType.WattMeter:
                            instrument = (mapping.get('instrument') or '').lower()
                            if 'joulescope' in instrument or 'js220' in instrument:
                                return JoulescopeJS220(name, int(pin), mapping.get("location"))
                            return YoctoWatt(name, int(pin), mapping.get("location"))
                        if mux_role == NetType.EnergyAnalyzer:
                            instrument = (mapping.get('instrument') or '').lower()
                            if 'joulescope' in instrument or 'js220' in instrument:
                                return JoulescopeEnergyAnalyzer(name, int(pin), mapping.get("location"))
                            raise RuntimeError(f"Unsupported energy-analyzer instrument: {mapping.get('instrument')}")
                        if mux_role == NetType.Rotation:
                            return Rotation(name, int(pin), mapping.get("location"))
                        if mux_role == NetType.Wifi:
                            return Wifi(name, int(pin), mapping.get("location"))
                        if mux_role == NetType.Actuate:
                            return Actuate(name, int(pin), mapping.get("location"))
                        if mux_role == NetType.Arm:
                            serial = (
                                mapping.get("serial")
                                or ((mapping.get("location") or {}) if isinstance(mapping.get("location"), dict) else {}).get("serial_number")
                                or None
                            )
                            port = mapping.get("port") or None
                            try:
                                pin_val = int(pin)
                            except Exception:
                                pin_val = pin
                            return Dexarm(port=port, serial_number=serial, name=name, pin=pin_val)

                        if mux_role == NetType.UART:
                            return UARTNet(name, mux)

                        if mux_role == NetType.Usb:
                            return USBNetWrapper(name, mux)

                        if mux_role == NetType.Webcam:
                            return WebcamNetWrapper(name, mux)

        return cls(
            name,
            type,
            muxes,
            setup_function=setup_function,
            teardown_function=teardown_function,
        )

    # ---------- instance ----------

    def __init__(self, name, type, muxes, *, setup_function=None, teardown_function=None):
        if type is not None and not isinstance(type, NetType):
            raise TypeError("Net type must be NetType enum")

        self.name = name
        self.mapping = None
        self.setup_commands = []
        self.teardown_function = teardown_function

        for mux in muxes:
            # Skip nets with unknown roles (e.g., webcam) that aren't in NetType enum
            try:
                mux_role = NetType.from_role(mux["role"])
            except KeyError:
                continue
            for mapping in mux.get("mappings", []):
                if mapping.get("net") == name and (type is None or type == mux_role):
                    _, letter = mux["scope_points"][0]
                    self.type = mux_role
                    self.device_type = mapping.get("device_override", mux_role.device_type)
                    self.mapping = mapping
                    self.mux = Mux(letter)
                    self.channel = channel_name_to_number(letter)

        if self.mapping is None:
            raise InvalidNetError(name)

        if self.needs_mux and setup_function is None:
            raise SetupFunctionRequiredError(name, self.type)
        self.setup_function = setup_function
        self.device = mapper_factory(self, self.device_type, self.mapping)

    def __str__(self):
        return f'<Net name="{self.name}" type={self.type} device_type={self.device_type}>'

    @property
    def needs_mux(self):
        return self.type in (NetType.Analog,)

    @property
    def location(self):
        parts = self.mapping["location"].strip().split(",")
        if len(parts) != 3:
            raise ValueError(f"Invalid location parts: {parts}")
        return [float(parts[0].strip()), float(parts[1].strip()), float(parts[2].strip())]

    # ---------- enable/disable ----------

    def enable(self):
        if self.setup_function:
            self.setup_function(self, self.device)
        if self.type == NetType.Analog:
            if self.mux:
                self.mux.connect(self)
            self.device.enable_channel(self.channel)

        if self.type == NetType.Logic:
            if not self.device.is_la_enabled():
                self.device.enable_la()
            self.device.enable_la_channel(self.channel)
            self.device.set_la_active_channel(self.channel)

        if self.type == NetType.Battery:
            self.device.enable_sim_output()

        if self.type == NetType.PowerSupply:
            if self.device_type in ("keithley", "keithley_2", "keithley_3"):
                self.device.enable()
                self.device.init_continuous()
            else:
                self.device.enable_output(self.channel)

        if self.type == NetType.ELoad:
            self.device.enable()

        if self.type == NetType.PowerSupply2Q:
            self.device.enable_output()

    def disable(self, teardown=True):
        if teardown and self.teardown_function:
            self.teardown_function(self, self.device)

        if self.type == NetType.Analog:
            if self.mux:
                self.mux.clear()
            self.device.disable_channel(self.channel)

        if self.type == NetType.Logic:
            self.device.disable_la_channel(self.channel)
            for i in range(0, 16):
                if self.device.is_la_channel_enabled(i):
                    return
            self.device.disable_la()

        if self.type == NetType.Battery:
            self.device.disable_sim_output()

        if self.type == NetType.PowerSupply:
            if self.device_type in ("keithley", "keithley_2", "keithley_3"):
                self.device.disable()
            else:
                self.device.disable_output(self.channel)

        if self.type == NetType.ELoad:
            self.device.disable()

        if self.type == NetType.PowerSupply2Q:
            self.device.disable_output()

    def __getattr__(self, attr):
        return getattr(self.device, attr)


# ----------------------------- example hooks -----------------------------

def setup_vbus(net, device):
    # placeholder for device-specific analog scope setup
    pass


def teardown_vbus(net, device):
    # placeholder for device-specific analog scope teardown
    pass


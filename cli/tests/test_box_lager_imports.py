#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Comprehensive Import Verification for box/lager Module Restructure.

This script tests that all documented public APIs are accessible from both
old (backward-compatible) and new (canonical) import paths.

Usage:
    python cli/tests/test_box_lager_imports.py

Returns:
    Exit code 0 if all imports pass (or only expected dependency failures), 1 if actual failures
"""

import sys
import importlib
import traceback
from typing import Dict, List, Tuple, Any, Optional

# Track results
RESULTS: Dict[str, Dict[str, Any]] = {
    "passed": [],
    "failed": [],
    "expected_failures": [],  # Missing optional dependencies
    "warnings": [],
}

# Optional dependencies that may not be available on dev machines
OPTIONAL_DEPENDENCIES = [
    "bleak",           # BLE support
    "pexpect",         # Debug/GDB support
    "flask_socketio",  # HTTP WebSocket support
    "flask",           # HTTP support
    "pyserial",        # Serial support
    "pyvisa",          # VISA instrument support
    "pyvisa-py",       # VISA backend
    "brainstem",       # Acroname USB hub
    "ykushcmd",        # YKUSH USB hub
    "Phidget22",       # Phidget thermocouple
    "yoctopuce",       # Yocto wattmeter
    "labjack",         # LabJack I/O
    "mcculw",          # MCC USB-202
]


def is_expected_dependency_failure(error: str) -> bool:
    """Check if the error is due to a missing optional dependency."""
    if not error:
        return False
    for dep in OPTIONAL_DEPENDENCIES:
        if f"No module named '{dep}" in error:
            return True
    return False


def log_pass(category: str, message: str):
    """Log a passing test."""
    RESULTS["passed"].append({"category": category, "message": message})
    print(f"  \033[92m[PASS]\033[0m {message}")


def log_fail(category: str, message: str, error: Optional[str] = None):
    """Log a failing test."""
    # Check if this is an expected failure due to missing optional dependency
    if is_expected_dependency_failure(error):
        RESULTS["expected_failures"].append({"category": category, "message": message, "error": error})
        print(f"  \033[93m[SKIP]\033[0m {message} (missing optional dependency)")
    else:
        RESULTS["failed"].append({"category": category, "message": message, "error": error})
        print(f"  \033[91m[FAIL]\033[0m {message}")
        if error:
            print(f"         Error: {error}")


def log_warn(category: str, message: str):
    """Log a warning."""
    RESULTS["warnings"].append({"category": category, "message": message})
    print(f"  \033[93m[WARN]\033[0m {message}")


def test_import(module_path: str) -> bool:
    """Try to import a module and return True if successful."""
    try:
        importlib.import_module(module_path)
        return True
    except ImportError as e:
        return False
    except Exception:
        return False


def test_attribute_import(module_path: str, attr_name: str) -> Tuple[bool, Any, Optional[str]]:
    """Try to import an attribute from a module. Returns (success, value, error)."""
    try:
        mod = importlib.import_module(module_path)
        if hasattr(mod, attr_name):
            return True, getattr(mod, attr_name), None
        else:
            return False, None, f"Module '{module_path}' has no attribute '{attr_name}'"
    except ImportError as e:
        return False, None, f"ImportError: {e}"
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def test_from_import(module_path: str, names: List[str]) -> Dict[str, Tuple[bool, Optional[str]]]:
    """Test importing multiple names from a module."""
    results = {}
    for name in names:
        success, _, error = test_attribute_import(module_path, name)
        results[name] = (success, error)
    return results


# =============================================================================
# TEST DEFINITIONS
# =============================================================================

def test_main_lager_init():
    """Test the main lager module exports."""
    print("\n=== Testing lager main module exports ===")

    # These should be exported from lager/__init__.py
    main_exports = [
        # Core
        "Interface",
        "Transport",
        "OutputEncoders",
        "output",
        "lager_excepthook",
        "restore_excepthook",
        "install_excepthook",
        "Hexfile",
        "Binfile",
        "read_adc",
        "get_available_instruments",
        "get_saved_nets",
        "LAGER_HOST",
        # PCB/Net
        "Net",
        "NetType",
        "InvalidNetError",
        "SetupFunctionRequiredError",
        # USB Hub (backward compat)
        "USBBackendError",
        "LibraryMissingError",
        "DeviceNotFoundError",
        "PortStateError",
    ]

    results = test_from_import("lager", main_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("main", f"from lager import {name}")
        else:
            log_fail("main", f"from lager import {name}", error)


def test_power_module():
    """Test power module (supply, battery, solar, eload) imports."""
    print("\n=== Testing power module imports ===")

    # --- Supply: New path (canonical) ---
    supply_new_exports = [
        "voltage", "current", "enable", "disable", "state",
        "set_mode", "clear_ocp", "clear_ovp"
    ]

    print("  [New path] lager.power.supply:")
    results = test_from_import("lager.power.supply", supply_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("supply_new", f"from lager.power.supply import {name}")
        else:
            log_fail("supply_new", f"from lager.power.supply import {name}", error)

    # --- Supply: Old path (backward compat) ---
    print("  [Old path] lager.supply:")
    results = test_from_import("lager.supply", supply_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("supply_old", f"from lager.supply import {name}")
        else:
            log_fail("supply_old", f"from lager.supply import {name}", error)

    # --- Battery: New path ---
    battery_new_exports = [
        "set_mode", "set_to_battery_mode", "set", "set_soc", "set_voc",
        "set_volt_full", "set_volt_empty", "set_capacity", "set_current_limit",
        "set_ovp", "set_ocp", "set_model", "enable_battery", "disable_battery",
        "print_state", "clear", "clear_ovp", "clear_ocp", "terminal_voltage",
        "current", "esr", "BatteryNet", "BatteryBackendError", "KeithleyBattery",
        "Keithley", "create_device",
    ]

    print("  [New path] lager.power.battery:")
    results = test_from_import("lager.power.battery", battery_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("battery_new", f"from lager.power.battery import {name}")
        else:
            log_fail("battery_new", f"from lager.power.battery import {name}", error)

    # --- Battery: Old path ---
    print("  [Old path] lager.battery:")
    results = test_from_import("lager.battery", battery_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("battery_old", f"from lager.battery import {name}")
        else:
            log_fail("battery_old", f"from lager.battery import {name}", error)

    # --- Solar: New path ---
    solar_new_exports = [
        "SolarNet", "SolarBackendError", "LibraryMissingError", "DeviceNotFoundError",
        "DeviceLockError", "EA", "set_to_solar_mode", "stop_solar_mode", "irradiance",
        "mpp_current", "mpp_voltage", "resistance", "temperature", "voc",
    ]

    print("  [New path] lager.power.solar:")
    results = test_from_import("lager.power.solar", solar_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("solar_new", f"from lager.power.solar import {name}")
        else:
            log_fail("solar_new", f"from lager.power.solar import {name}", error)

    # --- Solar: Old path ---
    print("  [Old path] lager.solar:")
    results = test_from_import("lager.solar", solar_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("solar_old", f"from lager.solar import {name}")
        else:
            log_fail("solar_old", f"from lager.solar import {name}", error)

    # --- ELoad: New path ---
    eload_new_exports = [
        "ELoadNet", "ELoadBackendError", "LibraryMissingError", "DeviceNotFoundError",
        "set_constant_current", "get_constant_current", "set_constant_voltage",
        "get_constant_voltage", "set_constant_resistance", "get_constant_resistance",
        "set_constant_power", "get_constant_power", "get_state",
    ]

    print("  [New path] lager.power.eload:")
    results = test_from_import("lager.power.eload", eload_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("eload_new", f"from lager.power.eload import {name}")
        else:
            log_fail("eload_new", f"from lager.power.eload import {name}", error)

    # --- ELoad: Old path ---
    print("  [Old path] lager.eload:")
    results = test_from_import("lager.eload", eload_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("eload_old", f"from lager.eload import {name}")
        else:
            log_fail("eload_old", f"from lager.eload import {name}", error)


def test_io_module():
    """Test I/O module (adc, dac, gpio) imports."""
    print("\n=== Testing I/O module imports ===")

    # --- ADC: New path ---
    adc_new_exports = [
        "ADCBase", "UnsupportedInstrumentError", "LabJackADC", "USB202ADC",
        "ADCDispatcher", "read", "voltage", "_do_adc_read",
    ]

    print("  [New path] lager.io.adc:")
    results = test_from_import("lager.io.adc", adc_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("adc_new", f"from lager.io.adc import {name}")
        else:
            log_fail("adc_new", f"from lager.io.adc import {name}", error)

    # --- ADC: Old path ---
    print("  [Old path] lager.adc:")
    results = test_from_import("lager.adc", adc_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("adc_old", f"from lager.adc import {name}")
        else:
            log_fail("adc_old", f"from lager.adc import {name}", error)

    # --- DAC: New path ---
    dac_new_exports = [
        "DACBase", "UnsupportedInstrumentError", "LabJackDAC", "USB202DAC",
        "read", "write",
    ]

    print("  [New path] lager.io.dac:")
    results = test_from_import("lager.io.dac", dac_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("dac_new", f"from lager.io.dac import {name}")
        else:
            log_fail("dac_new", f"from lager.io.dac import {name}", error)

    # --- DAC: Old path ---
    print("  [Old path] lager.dac:")
    results = test_from_import("lager.dac", dac_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("dac_old", f"from lager.dac import {name}")
        else:
            log_fail("dac_old", f"from lager.dac import {name}", error)

    # --- GPIO: New path ---
    gpio_new_exports = [
        "GPIOBase", "UnsupportedInstrumentError", "LabJackGPIO", "USB202GPIO",
        "read", "write", "gpi", "gpo",
    ]

    print("  [New path] lager.io.gpio:")
    results = test_from_import("lager.io.gpio", gpio_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("gpio_new", f"from lager.io.gpio import {name}")
        else:
            log_fail("gpio_new", f"from lager.io.gpio import {name}", error)

    # --- GPIO: Old path ---
    print("  [Old path] lager.gpio:")
    results = test_from_import("lager.gpio", gpio_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("gpio_old", f"from lager.gpio import {name}")
        else:
            log_fail("gpio_old", f"from lager.gpio import {name}", error)


def test_measurement_module():
    """Test measurement module (scope, thermocouple, watt) imports."""
    print("\n=== Testing measurement module imports ===")

    # --- Thermocouple: New path ---
    thermo_new_exports = [
        "ThermocoupleBase", "ThermocoupleBackendError", "PhidgetThermocouple",
        "ThermocoupleDispatcher", "read",
    ]

    print("  [New path] lager.measurement.thermocouple:")
    results = test_from_import("lager.measurement.thermocouple", thermo_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("thermo_new", f"from lager.measurement.thermocouple import {name}")
        else:
            log_fail("thermo_new", f"from lager.measurement.thermocouple import {name}", error)

    # --- Thermocouple: Old path ---
    print("  [Old path] lager.thermocouple:")
    results = test_from_import("lager.thermocouple", thermo_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("thermo_old", f"from lager.thermocouple import {name}")
        else:
            log_fail("thermo_old", f"from lager.thermocouple import {name}", error)

    # --- Watt: New path ---
    watt_new_exports = [
        "WattMeterBase", "WattMeterBackendError", "WattBackendError",
        "UnsupportedInstrumentError", "YoctoWatt", "WattMeterDispatcher", "read",
    ]

    print("  [New path] lager.measurement.watt:")
    results = test_from_import("lager.measurement.watt", watt_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("watt_new", f"from lager.measurement.watt import {name}")
        else:
            log_fail("watt_new", f"from lager.measurement.watt import {name}", error)

    # --- Watt: Old path ---
    print("  [Old path] lager.watt:")
    results = test_from_import("lager.watt", watt_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("watt_old", f"from lager.watt import {name}")
        else:
            log_fail("watt_old", f"from lager.watt import {name}", error)

    # --- Scope: New path ---
    scope_new_exports = [
        "RigolMso5000", "create_device",
    ]

    print("  [New path] lager.measurement.scope:")
    results = test_from_import("lager.measurement.scope", scope_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("scope_new", f"from lager.measurement.scope import {name}")
        else:
            log_fail("scope_new", f"from lager.measurement.scope import {name}", error)

    # --- Scope: Old path ---
    print("  [Old path] lager.scope:")
    results = test_from_import("lager.scope", scope_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("scope_old", f"from lager.scope import {name}")
        else:
            log_fail("scope_old", f"from lager.scope import {name}", error)


def test_protocols_module():
    """Test protocols module (uart, ble, wifi) imports."""
    print("\n=== Testing protocols module imports ===")

    # --- UART: New path ---
    uart_new_exports = [
        "monitor", "monitor_interactive", "UARTBridge", "UARTNet",
        "UARTBackendError", "_resolve_net_and_driver",
    ]

    print("  [New path] lager.protocols.uart:")
    results = test_from_import("lager.protocols.uart", uart_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("uart_new", f"from lager.protocols.uart import {name}")
        else:
            log_fail("uart_new", f"from lager.protocols.uart import {name}", error)

    # --- UART: Old path ---
    print("  [Old path] lager.uart:")
    results = test_from_import("lager.uart", uart_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("uart_old", f"from lager.uart import {name}")
        else:
            log_fail("uart_old", f"from lager.uart import {name}", error)

    # --- BLE: New path ---
    ble_new_exports = [
        "Client", "Central", "noop_handler", "notify_handler", "waiter",
    ]

    print("  [New path] lager.protocols.ble:")
    results = test_from_import("lager.protocols.ble", ble_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("ble_new", f"from lager.protocols.ble import {name}")
        else:
            log_fail("ble_new", f"from lager.protocols.ble import {name}", error)

    # --- BLE: Old path ---
    print("  [Old path] lager.ble:")
    results = test_from_import("lager.ble", ble_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("ble_old", f"from lager.ble import {name}")
        else:
            log_fail("ble_old", f"from lager.ble import {name}", error)

    # --- WiFi: New path ---
    wifi_new_exports = [
        "Wifi", "toggle_internet_access", "set_internet_access",
        "connect_to_wifi", "scan_wifi", "get_wifi_status",
    ]

    print("  [New path] lager.protocols.wifi:")
    results = test_from_import("lager.protocols.wifi", wifi_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("wifi_new", f"from lager.protocols.wifi import {name}")
        else:
            log_fail("wifi_new", f"from lager.protocols.wifi import {name}", error)

    # --- WiFi: Old path ---
    print("  [Old path] lager.wifi:")
    results = test_from_import("lager.wifi", wifi_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("wifi_old", f"from lager.wifi import {name}")
        else:
            log_fail("wifi_old", f"from lager.wifi import {name}", error)


def test_automation_module():
    """Test automation module (arm, usb_hub, webcam) imports."""
    print("\n=== Testing automation module imports ===")

    # --- Arm: New path ---
    arm_new_exports = [
        "ArmBase", "ArmBackendError", "MovementTimeoutError",
        "LibraryMissingError", "DeviceNotFoundError", "Dexarm",
    ]

    print("  [New path] lager.automation.arm:")
    results = test_from_import("lager.automation.arm", arm_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("arm_new", f"from lager.automation.arm import {name}")
        else:
            log_fail("arm_new", f"from lager.automation.arm import {name}", error)

    # --- Arm: Old path ---
    print("  [Old path] lager.arm:")
    results = test_from_import("lager.arm", arm_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("arm_old", f"from lager.arm import {name}")
        else:
            log_fail("arm_old", f"from lager.arm import {name}", error)

    # --- USB Hub: New path ---
    usb_new_exports = [
        "USBBackendError", "LibraryMissingError", "DeviceNotFoundError",
        "PortStateError", "enable", "disable", "toggle",
    ]

    print("  [New path] lager.automation.usb_hub:")
    results = test_from_import("lager.automation.usb_hub", usb_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("usb_new", f"from lager.automation.usb_hub import {name}")
        else:
            log_fail("usb_new", f"from lager.automation.usb_hub import {name}", error)

    # --- USB Hub: Old path ---
    print("  [Old path] lager.usb_hub:")
    results = test_from_import("lager.usb_hub", usb_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("usb_old", f"from lager.usb_hub import {name}")
        else:
            log_fail("usb_old", f"from lager.usb_hub import {name}", error)

    # --- Webcam: New path ---
    webcam_new_exports = [
        "WebcamService", "get_active_streams", "start_stream", "stop_stream",
        "get_stream_info", "rename_stream",
    ]

    print("  [New path] lager.automation.webcam:")
    results = test_from_import("lager.automation.webcam", webcam_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("webcam_new", f"from lager.automation.webcam import {name}")
        else:
            log_fail("webcam_new", f"from lager.automation.webcam import {name}", error)

    # --- Webcam: Old path ---
    print("  [Old path] lager.webcam:")
    results = test_from_import("lager.webcam", webcam_new_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("webcam_old", f"from lager.webcam import {name}")
        else:
            log_fail("webcam_old", f"from lager.webcam import {name}", error)


def test_debug_module():
    """Test debug module imports."""
    print("\n=== Testing debug module imports ===")

    debug_exports = [
        # Core API
        "connect", "connect_jlink", "disconnect", "reset_device", "erase_flash",
        "chip_erase", "flash_device", "read_memory", "RTT",
        # Exceptions
        "DebugError", "JLinkStartError", "JLinkAlreadyRunningError", "JLinkNotRunning",
        "DebuggerNotConnectedError",
        # Status
        "get_jlink_status",
        # GDB
        "get_arch", "get_controller", "gdb_reset",
        # Low-level
        "JLink",
        # GDBServer management
        "start_jlink_gdbserver", "stop_jlink_gdbserver", "get_jlink_gdbserver_status",
    ]

    results = test_from_import("lager.debug", debug_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("debug", f"from lager.debug import {name}")
        else:
            log_fail("debug", f"from lager.debug import {name}", error)


def test_http_module():
    """Test HTTP handlers module imports."""
    print("\n=== Testing HTTP handlers module imports ===")

    http_exports = [
        "active_uart_sessions", "active_uart_sessions_lock",
        "register_uart_routes", "register_uart_socketio", "cleanup_uart_sessions",
    ]

    results = test_from_import("lager.http", http_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("http", f"from lager.http import {name}")
        else:
            log_fail("http", f"from lager.http import {name}", error)


def test_pcb_module():
    """Test PCB module imports."""
    print("\n=== Testing PCB module imports ===")

    # Test pcb.net imports
    nets_net_exports = [
        "Net", "NetType", "InvalidNetError", "SetupFunctionRequiredError",
    ]

    results = test_from_import("lager.nets.net", nets_net_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("nets_net", f"from lager.nets.net import {name}")
        else:
            log_fail("nets_net", f"from lager.nets.net import {name}", error)

    # Test nets.constants
    nets_const_exports = ["NetType"]
    results = test_from_import("lager.nets.constants", nets_const_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("nets_constants", f"from lager.nets.constants import {name}")
        else:
            log_fail("nets_constants", f"from lager.nets.constants import {name}", error)


def test_dispatchers_module():
    """Test dispatchers infrastructure imports."""
    print("\n=== Testing dispatchers module imports ===")

    dispatcher_exports = [
        "BaseDispatcher", "ensure_role", "find_saved_net",
        "resolve_address", "resolve_channel",
    ]

    results = test_from_import("lager.dispatchers", dispatcher_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("dispatchers", f"from lager.dispatchers import {name}")
        else:
            log_fail("dispatchers", f"from lager.dispatchers import {name}", error)


def test_exceptions_module():
    """Test exceptions module imports."""
    print("\n=== Testing exceptions module imports ===")

    exception_exports = [
        # Legacy exceptions
        "LagerDeviceConnectionError", "LagerDeviceNotSupportedError",
        "LagerBoxConnectionError",
        "LagerTestingFailure", "LagerTestingSuccess",
        # Base backend exceptions
        "LagerBackendError", "LibraryMissingError", "DeviceNotFoundError",
        "DeviceLockError", "PortStateError",
        # Domain-specific backend exceptions
        "SupplyBackendError", "BatteryBackendError", "SolarBackendError",
        "ELoadBackendError", "USBBackendError", "ThermocoupleBackendError",
        "WattBackendError", "ADCBackendError", "DACBackendError",
        "GPIOBackendError", "UARTBackendError",
    ]

    results = test_from_import("lager.exceptions", exception_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("exceptions", f"from lager.exceptions import {name}")
        else:
            log_fail("exceptions", f"from lager.exceptions import {name}", error)


def test_cache_module():
    """Test cache module imports."""
    print("\n=== Testing cache module imports ===")

    cache_exports = ["get_nets_cache", "NetsCache"]

    results = test_from_import("lager.cache", cache_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("cache", f"from lager.cache import {name}")
        else:
            log_fail("cache", f"from lager.cache import {name}", error)


def test_constants_module():
    """Test constants module imports."""
    print("\n=== Testing constants module imports ===")

    constants_exports = [
        "SAVED_NETS_PATH", "AVAILABLE_INSTRUMENTS_PATH", "BOX_ID_PATH",
        "HARDWARE_SERVICE_PORT", "BOX_HTTP_PORT", "DEBUG_SERVICE_PORT",
        "DEFAULT_VISA_TIMEOUT", "DEFAULT_HTTP_TIMEOUT", "GDB_TIMEOUT",
    ]

    results = test_from_import("lager.constants", constants_exports)
    for name, (success, error) in results.items():
        if success:
            log_pass("constants", f"from lager.constants import {name}")
        else:
            log_fail("constants", f"from lager.constants import {name}", error)


def test_power_group_submodule_access():
    """Test that power group exposes submodules via attribute access."""
    print("\n=== Testing power group submodule access ===")

    submodules = ["supply", "battery", "solar", "eload"]

    for submodule in submodules:
        success, _, error = test_attribute_import("lager.power", submodule)
        if success:
            log_pass("power_submodule", f"from lager.power import {submodule}")
        else:
            log_fail("power_submodule", f"from lager.power import {submodule}", error)


def test_io_group_submodule_access():
    """Test that io group exposes submodules via attribute access."""
    print("\n=== Testing io group submodule access ===")

    submodules = ["adc", "dac", "gpio"]

    for submodule in submodules:
        success, _, error = test_attribute_import("lager.io", submodule)
        if success:
            log_pass("io_submodule", f"from lager.io import {submodule}")
        else:
            log_fail("io_submodule", f"from lager.io import {submodule}", error)


def test_protocols_group_submodule_access():
    """Test that protocols group exposes submodules via attribute access."""
    print("\n=== Testing protocols group submodule access ===")

    submodules = ["uart", "ble", "wifi"]

    for submodule in submodules:
        success, _, error = test_attribute_import("lager.protocols", submodule)
        if success:
            log_pass("protocols_submodule", f"from lager.protocols import {submodule}")
        else:
            log_fail("protocols_submodule", f"from lager.protocols import {submodule}", error)


def print_summary():
    """Print summary of test results."""
    print("\n" + "=" * 70)
    print("IMPORT VERIFICATION SUMMARY")
    print("=" * 70)

    total_passed = len(RESULTS["passed"])
    total_failed = len(RESULTS["failed"])
    total_expected = len(RESULTS["expected_failures"])
    total_warnings = len(RESULTS["warnings"])

    print(f"\n  \033[92mPassed:\033[0m   {total_passed}")
    print(f"  \033[91mFailed:\033[0m   {total_failed}")
    print(f"  \033[93mSkipped:\033[0m  {total_expected} (missing optional dependencies)")
    print(f"  \033[93mWarnings:\033[0m {total_warnings}")

    if total_failed > 0:
        print("\n\033[91m--- FAILED IMPORTS (requires investigation) ---\033[0m")
        for item in RESULTS["failed"]:
            print(f"  [{item['category']}] {item['message']}")
            if item['error']:
                print(f"    Error: {item['error']}")

    if total_expected > 0:
        print("\n\033[93m--- SKIPPED (missing optional dependencies) ---\033[0m")
        print("  These modules require hardware-specific libraries not installed on dev machine:")
        # Group by dependency
        deps_found = set()
        for item in RESULTS["expected_failures"]:
            error = item['error'] or ""
            for dep in OPTIONAL_DEPENDENCIES:
                if f"No module named '{dep}" in error:
                    deps_found.add(dep)
        for dep in sorted(deps_found):
            print(f"    - {dep}")

    if total_warnings > 0:
        print("\n\033[93m--- WARNINGS ---\033[0m")
        for item in RESULTS["warnings"]:
            print(f"  [{item['category']}] {item['message']}")

    print("\n" + "=" * 70)

    # Consider test passed if only expected failures (missing optional dependencies)
    return total_failed == 0


def main():
    """Run all import tests."""
    print("=" * 70)
    print("BOX/LAGER IMPORT VERIFICATION")
    print("Backward Compatibility Verification")
    print("=" * 70)

    # Set up path for imports
    import os
    box_path = os.path.join(os.path.dirname(__file__), "..", "..", "box")
    if box_path not in sys.path:
        sys.path.insert(0, box_path)

    # Run all tests
    test_main_lager_init()
    test_power_module()
    test_io_module()
    test_measurement_module()
    test_protocols_module()
    test_automation_module()
    test_debug_module()
    test_http_module()
    test_pcb_module()
    test_dispatchers_module()
    test_exceptions_module()
    test_cache_module()
    test_constants_module()
    test_power_group_submodule_access()
    test_io_group_submodule_access()
    test_protocols_group_submodule_access()

    # Print summary
    success = print_summary()

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

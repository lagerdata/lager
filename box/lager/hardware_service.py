#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Lager Hardware Invocation Service

Provides HTTP endpoint for invoking hardware control functions via the Device proxy pattern.
This service runs on port 8080 and handles dynamic method invocation on hardware modules.

The Device proxy (pcb/device.py) sends POST requests to /invoke with:
- device: module name (e.g., 'rigol_dp800', 'keithley', 'labjack')
- function: method name to call
- args/kwargs: function arguments
- net_info: network/channel configuration

This service imports the appropriate module, instantiates the device, and calls the method.
"""

import sys
import os
import json
import logging
import importlib
import traceback
import atexit
from flask import Flask, request, jsonify, send_from_directory

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10MB max request size

# Service configuration
SERVICE_HOST = '0.0.0.0'  # Listen on all interfaces for multi-VPN support
SERVICE_PORT = 8080
SERVICE_VERSION = '1.0.0'

# Cache for instantiated devices to avoid repeated initialization
# Format: {(device_name, net_info_hash): device_instance}
device_cache = {}

# Cache for modules used to create devices (for retry on stale sessions)
# Format: {(device_name, net_info_hash): module}
module_cache = {}

# Keywords indicating a stale VISA session that should trigger retry
_VISA_SESSION_ERROR_KEYWORDS = ('session', 'resource', 'closed', 'invalid')

def get_net_info_hash(net_info):
    """Create a hashable representation of net_info dict

    Recursively converts unhashable types (lists, dicts) to hashable types (tuples).
    """
    if net_info is None:
        return None

    def make_hashable(obj):
        """Recursively convert unhashable types to hashable"""
        if isinstance(obj, dict):
            return tuple(sorted((k, make_hashable(v)) for k, v in obj.items()))
        elif isinstance(obj, list):
            return tuple(make_hashable(item) for item in obj)
        elif isinstance(obj, set):
            return frozenset(make_hashable(item) for item in obj)
        else:
            # Primitives (str, int, float, bool, None) are already hashable
            return obj

    return make_hashable(net_info)

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'hardware-invocation-service',
        'version': SERVICE_VERSION,
        'port': SERVICE_PORT
    })

def _is_visa_session_error(exc):
    """Check if an exception indicates a stale VISA session."""
    error_msg = str(exc).lower()
    return any(kw in error_msg for kw in _VISA_SESSION_ERROR_KEYWORDS)


def _create_device_with_retry(module, device_name, net_info):
    """Create device with one retry on VISA session errors.

    If the first attempt fails with a session/resource error, clears the
    module's resource cache (if present) and retries once with a fresh connection.
    """
    try:
        return module.create_device(net_info)
    except Exception as e:
        if _is_visa_session_error(e):
            logger.warning(f"VISA session error creating {device_name}, clearing cache and retrying: {e}")
            if hasattr(module, 'clear_resource_cache'):
                module.clear_resource_cache()
            return module.create_device(net_info)
        raise


@app.route('/invoke', methods=['POST'])
def invoke():
    """
    Main endpoint for invoking hardware device methods.

    Expected JSON payload:
    {
        "device": "rigol_dp800",          # Module name under lager.*
        "function": "enable_output",       # Method to call
        "args": [1],                       # Positional arguments
        "kwargs": {},                      # Keyword arguments
        "net_info": {"address": "...", ... }  # Device configuration
    }
    """
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'Missing JSON payload'}), 400

        device_name = data.get('device')
        function_name = data.get('function')
        args = data.get('args', [])
        kwargs = data.get('kwargs', {})
        net_info = data.get('net_info')

        if not device_name:
            return jsonify({'error': 'Missing "device" field'}), 400
        if not function_name:
            return jsonify({'error': 'Missing "function" field'}), 400

        logger.info(f"Invoking {device_name}.{function_name}({args}, {kwargs}) with net_info={net_info}")

        # Try to get cached device or create new one
        # For multi-channel devices (e.g., Rigol DP821), cache by address to avoid "Resource busy" errors
        # Multiple channels share the same device instance, with channel passed as parameter
        address = net_info.get('address') if net_info else None
        cache_key = (device_name, address) if address else (device_name, get_net_info_hash(net_info))
        device = device_cache.get(cache_key)

        if device is None:
            # Import the hardware module
            # Try multiple paths in order of likelihood (new grouped structure)
            import_paths = [
                f'lager.{device_name}',                      # Direct: lager.rigol_dp800
                # Power group
                f'lager.power.supply.{device_name}',         # Power supplies: lager.power.supply.keysight_e36300
                f'lager.power.battery.{device_name}',        # Battery simulators: lager.power.battery.keithley
                f'lager.power.solar.{device_name}',          # Solar simulators: lager.power.solar.ea
                f'lager.power.eload.{device_name}',          # Electronic loads: lager.power.eload.rigol_dl3021
                # Measurement group
                f'lager.measurement.scope.{device_name}',    # Oscilloscopes: lager.measurement.scope.rigol_mso5000
                f'lager.measurement.thermocouple.{device_name}',  # Thermocouples
                f'lager.measurement.watt.{device_name}',     # Watt meters
                # I/O group
                f'lager.io.adc.{device_name}',               # ADC: lager.io.adc.labjack_t7
                f'lager.io.dac.{device_name}',               # DAC: lager.io.dac.labjack_t7
                f'lager.io.gpio.{device_name}',              # GPIO: lager.io.gpio.*
                # Automation group
                f'lager.automation.usb_hub.{device_name}',   # USB hubs: lager.automation.usb_hub.acroname
                f'lager.automation.arm.{device_name}',       # Robot arm: lager.automation.arm.rotrics
                f'lager.automation.webcam.{device_name}',    # Webcam
                # Protocols group
                f'lager.protocols.uart.{device_name}',       # UART
                f'lager.protocols.ble.{device_name}',        # BLE
                f'lager.protocols.wifi.{device_name}',       # WiFi
                # Legacy paths (backwards compatibility)
                f'lager.nets.mappers.{device_name}',          # Mappers: lager.nets.mappers.rigol_mso5000
                f'lager.instrument_wrappers.{device_name}',  # Instrument wrappers
            ]

            module = None
            for import_path in import_paths:
                try:
                    module = importlib.import_module(import_path)
                    logger.info(f"Successfully imported {import_path}")
                    break
                except ModuleNotFoundError:
                    continue

            if module is None:
                logger.error(f"Module not found after trying: {import_paths}")
                return jsonify({
                    'error': f'Hardware module not found: {device_name}',
                    'details': f'Module does not exist in any of: {", ".join(import_paths)}'
                }), 404

            # Instantiate the device
            # Most hardware modules have a create function or constructor that takes net_info
            if hasattr(module, 'create_device'):
                device = _create_device_with_retry(module, device_name, net_info)
            elif hasattr(module, 'create'):
                device = module.create(net_info)
            elif net_info:
                # Try to find a class matching the module name and instantiate it
                class_name = ''.join(word.capitalize() for word in device_name.split('_'))
                if hasattr(module, class_name):
                    device_class = getattr(module, class_name)
                    device = device_class(**net_info) if net_info else device_class()
                else:
                    # Fallback: return the module itself (for modules with top-level functions)
                    device = module
            else:
                device = module

            # For SupplyNet/BatteryNet/etc high-level wrappers, extract the low-level device
            # The mappers expect low-level device methods (e.g., enable_output(channel))
            # High-level classes like KeysightE36300 have a .device attribute with the low-level device
            if hasattr(device, 'device') and not callable(getattr(device, 'device')):
                logger.info(f"Extracting low-level device from {device.__class__.__name__}.device")
                device = device.device

            # Cache the device and its module (for retry on stale sessions)
            device_cache[cache_key] = device
            module_cache[cache_key] = module
            logger.info(f"Created and cached device: {device_name}")

        # Get the function from the device
        if not hasattr(device, function_name):
            return jsonify({
                'error': f'Function not found: {function_name}',
                'details': f'Device {device_name} does not have method {function_name}'
            }), 404

        func = getattr(device, function_name)

        # Call the function
        try:
            result = func(*args, **kwargs)

            # Return the result
            # Note: EnumEncoder is handled by device.py when it decodes the response
            return jsonify(result)

        except Exception as e:
            # Check if this is a stale VISA session error on a cached device
            mod = module_cache.get(cache_key)
            if _is_visa_session_error(e) and mod and hasattr(mod, 'create_device'):
                logger.warning(f"VISA session error on cached {device_name}.{function_name}, recreating device: {e}")
                # Remove stale device from cache
                device_cache.pop(cache_key, None)
                # Clear the module's resource cache if available
                if hasattr(mod, 'clear_resource_cache'):
                    mod.clear_resource_cache()
                # Recreate device and retry the call once
                try:
                    device = mod.create_device(net_info)
                    if hasattr(device, 'device') and not callable(getattr(device, 'device')):
                        device = device.device
                    device_cache[cache_key] = device
                    func = getattr(device, function_name)
                    result = func(*args, **kwargs)
                    return jsonify(result)
                except Exception as retry_e:
                    logger.error(f"Retry also failed for {device_name}.{function_name}: {retry_e}")
                    logger.error(traceback.format_exc())
                    return jsonify({
                        'error': f'Function call failed (after retry): {str(retry_e)}',
                        'details': traceback.format_exc()
                    }), 500

            logger.error(f"Error calling {device_name}.{function_name}: {e}")
            logger.error(traceback.format_exc())
            return jsonify({
                'error': f'Function call failed: {str(e)}',
                'details': traceback.format_exc()
            }), 500

    except Exception as e:
        logger.error(f"Error in /invoke: {e}")
        logger.error(traceback.format_exc())
        return jsonify({
            'error': f'Internal server error: {str(e)}',
            'details': traceback.format_exc()
        }), 500

def _close_device(device, cache_key):
    """
    Close a device and release its VISA/USB resources.

    Tries multiple approaches to close the device:
    1. close() method (standard pattern)
    2. instr.instr.close() (InstrumentWrap pattern)
    3. instr.close() (direct VISA resource)
    4. visa_resource.close() (alternative pattern)
    """
    try:
        # Try close() method first (standard pattern)
        if hasattr(device, 'close') and callable(device.close):
            device.close()
            return True
        # Fallback: close underlying VISA resource directly
        elif hasattr(device, 'instr'):
            if hasattr(device.instr, 'instr') and hasattr(device.instr.instr, 'close'):
                device.instr.instr.close()
                return True
            elif hasattr(device.instr, 'close'):
                device.instr.close()
                return True
        # For drivers using visa_resource attribute
        elif hasattr(device, 'visa_resource') and device.visa_resource:
            device.visa_resource.close()
            return True
    except Exception as e:
        logger.warning(f"Error closing device {cache_key}: {e}")
    return False


@app.route('/cache/clear', methods=['POST'])
def clear_cache():
    """Clear the device cache and properly close all VISA/USB resources"""
    global device_cache
    closed_count = 0
    error_count = 0

    for cache_key, device in list(device_cache.items()):
        if _close_device(device, cache_key):
            closed_count += 1
        else:
            error_count += 1

    total_count = len(device_cache)
    device_cache.clear()
    module_cache.clear()
    logger.info(f"Cleared device cache: {closed_count} closed, {error_count} errors")
    return jsonify({
        'status': 'success',
        'cleared': total_count,
        'closed': closed_count,
        'errors': error_count
    })

@app.route('/cache/stats', methods=['GET'])
def cache_stats():
    """Get cache statistics"""
    return jsonify({
        'cached_devices': len(device_cache),
        'devices': [
            {'name': device_name, 'net_info': dict(net_info) if net_info else None}
            for (device_name, net_info) in device_cache.keys()
        ]
    })

@app.route('/web_oscilloscope.html', methods=['GET'])
def serve_web_oscilloscope():
    """Serve the web oscilloscope HTML interface"""
    html_path = os.environ.get('LAGER_APP_DIR', '/app/lager')
    return send_from_directory(html_path, 'web_oscilloscope.html')

def _cleanup_device_cache():
    """Cleanup function called on normal process exit."""
    global device_cache, module_cache
    logger.info("Cleaning up device cache on exit...")
    for cache_key, device in list(device_cache.items()):
        _close_device(device, cache_key)
    device_cache.clear()
    module_cache.clear()


# Register cleanup handler for normal process exit
atexit.register(_cleanup_device_cache)


def run_service():
    """Run the hardware invocation service"""
    logger.info(f"Starting Lager Hardware Invocation Service v{SERVICE_VERSION}")
    logger.info(f"Listening on {SERVICE_HOST}:{SERVICE_PORT}")
    logger.info(f"Endpoints:")
    logger.info(f"  POST /invoke - Invoke hardware device methods")
    logger.info(f"  GET  /health - Health check")
    logger.info(f"  POST /cache/clear - Clear device cache")
    logger.info(f"  GET  /cache/stats - Get cache statistics")
    logger.info(f"  GET  /web_oscilloscope.html - Web oscilloscope interface")

    # Run Flask app with threading
    # Using threaded=True for concurrent request handling
    app.run(
        host=SERVICE_HOST,
        port=SERVICE_PORT,
        debug=False,
        threaded=True
    )

if __name__ == '__main__':
    run_service()

#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
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
import threading
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

# Per-device locks for serializing concurrent /invoke calls to the same
# cached driver. Flask runs threaded=True, so without this two requests
# (e.g. WS monitor tick + TUI command) racing on the same VISA session
# produce "Query INTERRUPTED" pyvisa errors. Lock is acquired per call;
# never held across calls.
#
# When the request has a VISA address, we lock on the address itself so
# that two driver classes wrapping the same physical USB device (e.g.
# Keithley 2281S supply + battery) serialize their SCPI calls. Locking
# on cache_key alone would let them race on the USB bus.
device_locks = {}
device_locks_meta_lock = threading.Lock()


def _get_device_lock(cache_key):
    with device_locks_meta_lock:
        lock = device_locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            device_locks[cache_key] = lock
        return lock


def _get_address_lock(address):
    """Per-VISA-address lock — serializes SCPI across all driver classes
    that share one physical USB device (e.g. Keithley 2281S supply +
    battery). Stored in `device_locks` under a sentinel key to avoid
    colliding with cache_key entries."""
    return _get_device_lock(('__address__', address))


# Shared pyvisa Resource cache keyed by VISA address. Lets multiple driver
# classes wrap one underlying pyvisa session — fixes the v0.16.7 Keithley
# 2281S dual-role known limitation where supply and battery drivers each
# opened their own session against the same USB device and the second one
# hit [Errno 16] Resource busy.
_visa_resources = {}                       # address -> (rm, raw_session)
_visa_resources_meta_lock = threading.Lock()

# Drivers that should share one pyvisa session per VISA address. Add a
# device_name here whenever a single physical instrument exposes more than
# one role (e.g. Keithley 2281S as both power supply and battery simulator).
_SHARED_VISA_DEVICE_NAMES = frozenset({'keithley', 'keithley_battery'})


_RESOURCE_BUSY_RETRY_DELAYS_S = (0.2, 0.5, 1.0, 2.0)


def _is_resource_busy_error(exc):
    """libusb returns 'Resource busy' / Errno 16 when a previous claim on the
    same USB device hasn't been fully released yet. This is transient — give
    the kernel a moment and the next open succeeds."""
    msg = str(exc).lower()
    return 'resource busy' in msg or 'errno 16' in msg


# Substrings that classify an error as ENODEV (libusb's "device disappeared"
# signature after USB re-enumeration — mains power-cycle of the instrument,
# accidental unplug, or USB hub port toggle). Different from a stale session:
# the file descriptor still resolves but points at nothing. Recovery requires
# evicting every cached entry for the affected address — both this driver's
# cached instance AND any sibling drivers (e.g. Keithley 2281S supply +
# battery share one address) AND the shared pyvisa session pool entry.
_ENODEV_ERROR_KEYWORDS = ('no such device', 'cannot find', 'errno 19', 'enodev')


def _is_enodev_error(exc):
    """True when the error looks like libusb ENODEV (USB re-enumeration)."""
    msg = str(exc).lower()
    return any(kw in msg for kw in _ENODEV_ERROR_KEYWORDS)


def _get_or_open_visa_resource(address):
    """Return (rm, raw_session) for `address`, opening a new pyvisa session
    on first call and reusing it on subsequent calls. Thread-safe.

    The returned `rm` is kept alive in the cache so pyvisa's GC doesn't
    invalidate the session handle.

    On `Resource busy` (libusb's async release-interface race after a recent
    close — happens when /cache/clear or a script exit just tore the session
    down), retry the open with a short exponential backoff. Other errors
    propagate immediately."""
    import pyvisa  # local import — pyvisa is optional at module import time
    import time as _time
    with _visa_resources_meta_lock:
        entry = _visa_resources.get(address)
        if entry is not None:
            logger.info(f"Reusing shared pyvisa session for {address}")
            return entry
        rm = pyvisa.ResourceManager()
        raw = None
        last_exc = None
        attempts = (0.0,) + _RESOURCE_BUSY_RETRY_DELAYS_S
        for delay in attempts:
            if delay:
                _time.sleep(delay)
            try:
                raw = rm.open_resource(address)
                break
            except Exception as e:
                last_exc = e
                if not _is_resource_busy_error(e):
                    raise
                logger.warning(
                    f"open_resource({address}) hit Resource busy; "
                    f"retrying after {delay if delay else 0.0}s"
                )
        if raw is None:
            raise last_exc
        try:
            raw.read_termination = '\n'
            raw.write_termination = '\n'
            raw.timeout = 5000  # ms; drivers can override
        except Exception:
            pass
        _visa_resources[address] = (rm, raw)
        logger.info(f"Opened shared pyvisa session for {address}")
        return rm, raw


def _close_visa_resource(address):
    """Close and remove the shared pyvisa session for `address`. Safe to
    call when no entry exists."""
    with _visa_resources_meta_lock:
        entry = _visa_resources.pop(address, None)
    if entry is None:
        return
    rm, raw = entry
    try:
        raw.close()
    except Exception as e:
        logger.warning(f"Error closing shared pyvisa raw for {address}: {e}")
    try:
        rm.close()
    except Exception as e:
        logger.warning(f"Error closing shared pyvisa rm for {address}: {e}")
    logger.info(f"Closed shared pyvisa session for {address}")


# --- Self-restart on an unrecoverable wedged VISA session -------------------
# After a USB instrument re-enumerates (mains power-cycle / USB hub-port
# toggle), the libusb interface claim from the now-dead session can be orphaned
# *inside this process*: the device is back on the bus and a fresh process can
# open it, but this one never can — no amount of close/evict/reopen helps
# (reproduced on a Keithley 2281S). The only reliable recovery is a fresh
# process, so we exit and let start-services.sh's `while true` supervisor
# respawn hardware_service with a clean libusb context. The monitor's next tick
# then gets a working session and the TUI self-heals (~2s blip, no container
# restart). Heavily gated so it only fires for the real wedge.
_HW_SELF_RESTART_STAMP = "/tmp/lager-hardware-service-self-restart"
_HW_SELF_RESTART_COOLDOWN_S = 60.0
_OPEN_FAILURE_MARKERS = (
    'open_resource', 'open_bare_resource', 'after_parsing',
    'could not open instrument',
)


def _usb_ids_from_address(address):
    """Parse (vid, pid, serial) from a USB VISA address, else (None, None, None)."""
    import re
    m = re.match(r'USB\d*::(0x[0-9A-Fa-f]+)::(0x[0-9A-Fa-f]+)::([^:]+)::',
                 str(address or ''))
    if not m:
        return (None, None, None)
    try:
        return (int(m.group(1), 16), int(m.group(2), 16), m.group(3))
    except ValueError:
        return (None, None, None)


def _usb_device_enumerated(address):
    """True/False if the USB instrument is / isn't on the bus right now; None
    if unknown (non-USB address or sysfs unavailable). Used to decide whether a
    self-restart can possibly help: an *enumerated* device we can't reopen is a
    wedged in-process session (restart helps); an absent one is unplugged
    (restart can't help — don't loop).

    Reads sysfs (the kernel's device list) rather than libusb/PyUSB on purpose:
    the wedge is precisely that THIS process's libusb context is stale and can't
    see the re-enumerated device, so a libusb-based check would wrongly report
    the device as gone and suppress the restart. sysfs is unaffected."""
    import glob
    vid, pid, serial = _usb_ids_from_address(address)
    if vid is None:
        return None
    vid_s, pid_s = f"{vid:04x}", f"{pid:04x}"
    try:
        for dev_dir in glob.glob("/sys/bus/usb/devices/*/"):
            def _read(name):
                try:
                    with open(os.path.join(dev_dir, name)) as fh:
                        return fh.read().strip()
                except OSError:
                    return None
            if (_read("idVendor") or "").lower() != vid_s:
                continue
            if (_read("idProduct") or "").lower() != pid_s:
                continue
            if serial is None:
                return True
            dev_serial = _read("serial")
            if dev_serial is None or dev_serial == serial:
                # serial matches, or is unreadable but VID/PID matched — present
                return True
        return False
    except Exception:
        return None


def _looks_like_open_failure(exc):
    """True when an exception/traceback looks like a failure to OPEN the VISA
    session (vs. a normal command error on an already-open device)."""
    blob = (traceback.format_exc() + ' ' + str(exc)).lower()
    return any(m in blob for m in _OPEN_FAILURE_MARKERS)


def _maybe_self_restart_for_wedged_session(address, context):
    """Exit so the supervisor respawns us, when a VISA session is wedged
    in-process. No-op unless the device is enumerated (a restart can actually
    help) and we haven't restarted within the cooldown (anti-loop)."""
    import time
    # The wedge is detected at the tail of a re-enumeration, so the device may
    # not be back in sysfs for a beat. Retry briefly (~4s) so a still-
    # re-enumerating instrument isn't misread as unplugged — that false
    # "not on the bus" would suppress the restart and leave the TUI dead.
    enumerated = None
    for _delay in (0.0, 0.5, 1.0, 1.0, 1.5):
        if _delay:
            time.sleep(_delay)
        enumerated = _usb_device_enumerated(address)
        if enumerated is not False:  # True (present) or None (unknown) — stop
            break
    if enumerated is None:
        logger.warning(
            f"[self-restart] {context}: cannot confirm USB enumeration for "
            f"{address}; not restarting.")
        return
    if enumerated is False:
        logger.warning(
            f"[self-restart] {context}: {address} is not on the USB bus "
            f"(unplugged/off) — a restart can't help; surfacing the error.")
        return
    now = time.time()
    try:
        last = os.path.getmtime(_HW_SELF_RESTART_STAMP)
    except OSError:
        last = 0.0
    if now - last < _HW_SELF_RESTART_COOLDOWN_S:
        logger.error(
            f"[self-restart] {context}: {address} wedged, but self-restarted "
            f"{int(now - last)}s ago (< {int(_HW_SELF_RESTART_COOLDOWN_S)}s "
            f"cooldown); surfacing the error instead of restarting again.")
        return
    try:
        with open(_HW_SELF_RESTART_STAMP, 'w') as fh:
            fh.write(str(now))
    except OSError:
        pass
    logger.critical(
        f"[self-restart] {context}: VISA session for {address} is wedged "
        f"in-process (device is enumerated but cannot be reopened — orphaned "
        f"libusb claim). Exiting so the supervisor respawns hardware_service "
        f"with a clean libusb context.")
    logging.shutdown()  # flush handlers before the hard exit
    os._exit(70)  # EX_SOFTWARE; start-services.sh's `while true` respawns us


# Substrings that classify an error as a stale pyvisa session worth recreating.
# Note: 'resource' was previously here but matched 'Resource busy' — a kernel-level
# USB-claim error from a *live* concurrent session, not a stale one. That caused a
# retry loop (pop the live cache entry, then call create_device on the same address
# while the original session is still alive in this process) producing a second
# Resource busy. Removed in v0.16.7.
#
# ENODEV keywords ('no such device', 'cannot find', 'errno 19', 'enodev') added
# in 0.20.0 — libusb returns these after USB re-enumeration (e.g. instrument
# mains power-cycle) when the held file descriptor points at a device number
# the kernel has since reassigned. Detected separately via _is_enodev_error()
# so the retry path can do a more aggressive cleanup (evict siblings + force
# shared-session refresh) than for a plain stale-session error.
_VISA_SESSION_ERROR_KEYWORDS = (
    'session', 'closed', 'invalid',
    'no such device', 'cannot find', 'errno 19', 'enodev',
)

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


@app.route('/diagnose/dispatcher', methods=['GET'])
def diagnose_dispatcher():
    """Report the in-process VISA session pool + driver cache state for a
    given VISA address. Consumed by `lager diagnose <net>` (0.20.0+) to
    classify a misbehaving net as "hw_service has a stale handle" vs
    other failure modes.

    Query params:
      address   VISA address (required) — looked up in the shared session
                pool and matched against device_cache keys whose second
                element is this address.
    """
    address = (request.args.get('address') or '').strip()
    if not address:
        return jsonify({'error': 'address parameter required'}), 400

    with _visa_resources_meta_lock:
        cached_session = address in _visa_resources

    cached_drivers = []
    for cache_key, dev in list(device_cache.items()):
        # cache_key is (device_name, address) when an address was used.
        if len(cache_key) >= 2 and cache_key[1] == address:
            cached_drivers.append({
                'device_name': cache_key[0],
                'driver_class': type(dev).__name__,
            })

    return jsonify({
        'address': address,
        'cached_session': cached_session,
        'cached_drivers': cached_drivers,
        'shared_pool_size': len(_visa_resources),
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


def _sync_device_channel(device, net_info):
    """Re-point a shared, cached multi-channel driver at the channel for THIS request.

    Multi-output instruments are cached once per address (cache_key omits the
    channel) so every channel shares a single USB/pyvisa session and avoids
    "[Errno 16] Resource busy". The trade-off: the cached instance carries a
    bound channel (e.g. self.chan) fixed to whichever net first created it.
    Net-level methods that act on that bound channel rather than an explicit
    argument — voltage()/current()/enable()/disable()/state() on the supply
    drivers — would otherwise be misrouted to the first channel (e.g. a CH2
    voltage command applied to CH1, which then rejects anything above CH1's
    limit). Drivers that can be re-pointed expose set_active_channel(); we call
    it under the per-address lock so the set-then-call is atomic against other
    channels' requests. Drivers without set_active_channel are unaffected.
    """
    if not net_info:
        return
    channel = net_info.get('channel')
    setter = getattr(device, 'set_active_channel', None)
    if channel is None or not callable(setter):
        return
    try:
        setter(channel)
    except Exception as e:
        logger.warning(f"Could not sync device channel to {channel!r}: {e}")


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
                # For instruments with multiple roles per physical USB device (Keithley
                # 2281S supply + battery), share one pyvisa session across drivers so
                # the second open doesn't hit [Errno 16] Resource busy. The driver's
                # create_device must accept a `raw_resource=` kwarg; if it doesn't, we
                # fall back to the legacy per-driver-opens-its-own-session path.
                shared_raw = None
                if address and device_name in _SHARED_VISA_DEVICE_NAMES:
                    try:
                        _, shared_raw = _get_or_open_visa_resource(address)
                    except Exception as e:
                        logger.warning(
                            f"Could not open shared pyvisa session for {address}: {e}; "
                            f"falling back to per-driver session"
                        )
                        shared_raw = None
                if shared_raw is not None:
                    logger.info(
                        f"Creating {device_name} with shared raw_resource for {address}"
                    )
                    try:
                        device = module.create_device(net_info, raw_resource=shared_raw)
                    except TypeError as te:
                        # Driver's create_device hasn't been updated yet — fall back.
                        logger.warning(
                            f"{device_name}.create_device rejected raw_resource= ({te}); "
                            f"falling back to legacy per-driver-opens-its-own-session path"
                        )
                        device = _create_device_with_retry(module, device_name, net_info)
                else:
                    logger.info(
                        f"Creating {device_name} via legacy path "
                        f"(address={address!r}, in shared list={device_name in _SHARED_VISA_DEVICE_NAMES})"
                    )
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

        # Call the function. Per-device lock serializes concurrent /invoke
        # requests targeting the same physical instrument (e.g. WS monitor
        # tick + TUI command both targeting the same Rigol DP821, or a
        # supply command + a battery command both targeting the same
        # Keithley 2281S). Without this, pyvisa raises "Query INTERRUPTED"
        # or libusb returns Resource busy on USB transfer.
        device_lock = _get_address_lock(address) if address else _get_device_lock(cache_key)
        try:
            with device_lock:
                _sync_device_channel(device, net_info)
                result = func(*args, **kwargs)

            # Return the result
            # Note: EnumEncoder is handled by device.py when it decodes the response
            return jsonify(result)

        except Exception as e:
            # Check if this is a stale VISA session error on a cached device
            mod = module_cache.get(cache_key)
            if _is_visa_session_error(e) and mod and hasattr(mod, 'create_device'):
                logger.warning(f"VISA session error on cached {device_name}.{function_name}, recreating device: {e}")
                # Remove stale device from cache and release its USB claim before
                # opening a new session on the same address. Without the close,
                # libusb refuses the second open with [Errno 16] Resource busy
                # because the popped instance is still alive in this process and
                # still holds the claim — reproduced on Keithley 2281S battery
                # TUI + concurrent battery CLI.
                old_device = device_cache.pop(cache_key, None)
                if old_device is not None:
                    _close_device(old_device, cache_key)
                # ENODEV (USB re-enumeration: instrument mains-cycle, accidental
                # unplug, USB hub port toggle) invalidates every cached session
                # for this address, not just the calling driver's. Evict siblings
                # so a subsequent call against a different role on the same
                # physical instrument (e.g. Keithley supply when battery just
                # hit ENODEV) doesn't reuse a stale fd and EBUSY/ENODEV again.
                is_enodev = _is_enodev_error(e)
                if is_enodev and address:
                    sibling_keys = [
                        k for k in list(device_cache.keys())
                        if len(k) > 1 and k[1] == address
                    ]
                    for sib_key in sibling_keys:
                        sib = device_cache.pop(sib_key, None)
                        if sib is not None:
                            logger.warning(f"ENODEV cascade: evicting sibling cache entry {sib_key}")
                            _close_device(sib, sib_key)
                # Clear the module's resource cache if available
                if hasattr(mod, 'clear_resource_cache'):
                    mod.clear_resource_cache()
                # If this driver shares one pyvisa session per address with a
                # sibling driver (Keithley dual-role), refresh the shared
                # session: the underlying pyvisa handle is the same one that
                # just raised, so reusing it would produce the same error.
                # Closing and reopening releases the USB claim and gives both
                # drivers a clean session.
                #
                # On ENODEV, force-close the shared pool entry for this address
                # even for non-shared drivers — the cached pyvisa session holds
                # a stale fd after USB re-enumeration regardless of which driver
                # owns it. Reopen only for drivers that actually share.
                shared_raw = None
                should_close_shared = address and (
                    device_name in _SHARED_VISA_DEVICE_NAMES or is_enodev
                )
                if should_close_shared:
                    _close_visa_resource(address)
                if address and device_name in _SHARED_VISA_DEVICE_NAMES:
                    try:
                        _, shared_raw = _get_or_open_visa_resource(address)
                    except Exception as e2:
                        logger.warning(
                            f"Could not reopen shared pyvisa session for {address} during retry: {e2}; "
                            f"falling back to per-driver session"
                        )
                        shared_raw = None
                # Recreate device and retry the call once. Same per-device
                # lock — cache_key is unchanged, so retries serialize too.
                try:
                    if shared_raw is not None:
                        try:
                            device = mod.create_device(net_info, raw_resource=shared_raw)
                        except TypeError:
                            device = mod.create_device(net_info)
                    else:
                        device = mod.create_device(net_info)
                    if hasattr(device, 'device') and not callable(getattr(device, 'device')):
                        device = device.device
                    device_cache[cache_key] = device
                    func = getattr(device, function_name)
                    with device_lock:
                        _sync_device_channel(device, net_info)
                        result = func(*args, **kwargs)
                    return jsonify(result)
                except Exception as retry_e:
                    logger.error(f"Retry also failed for {device_name}.{function_name}: {retry_e}")
                    logger.error(traceback.format_exc())
                    # Recovery (evict + reopen + retry) failed for a stale
                    # session/ENODEV error. If the device is still on the bus,
                    # this is the orphaned-libusb-claim wedge — only a fresh
                    # process clears it.
                    if address:
                        _maybe_self_restart_for_wedged_session(
                            address, f"{device_name}.{function_name} (recovery retry failed)")
                    return jsonify({
                        'error': f'Function call failed (after retry): {str(retry_e)}',
                        'details': traceback.format_exc()
                    }), 500

            logger.error(f"Error calling {device_name}.{function_name}: {e}")
            logger.error(traceback.format_exc())
            # A non-session error path can still be a failure to OPEN the
            # session (e.g. the per-driver fallback open after the shared
            # reopen failed). Treat an enumerated-but-unreopenable device as a
            # wedge and let the supervisor respawn us with a clean libusb state.
            if address and _looks_like_open_failure(e):
                _maybe_self_restart_for_wedged_session(
                    address, f"{device_name}.{function_name} (open failed)")
            return jsonify({
                'error': f'Function call failed: {str(e)}',
                'details': traceback.format_exc()
            }), 500

    except Exception as e:
        logger.error(f"Error in /invoke: {e}")
        logger.error(traceback.format_exc())
        # A failure to OPEN the session can surface here too — e.g. a cache-miss
        # tick where create_device's per-driver open fails after the device
        # re-enumerated. Same wedge: if the device is on the bus but unreopenable
        # in-process, let the supervisor respawn us with a clean libusb context.
        _addr = locals().get('address')
        if _addr and _looks_like_open_failure(e):
            _maybe_self_restart_for_wedged_session(
                _addr,
                f"{locals().get('device_name', '?')}.{locals().get('function_name', '?')} "
                f"(open failed, top-level)")
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
    """Clear the device cache and close non-shared VISA/USB resources.

    v0.16.8 note: shared pyvisa sessions (dual-role instruments — see
    `_SHARED_VISA_DEVICE_NAMES`) are deliberately retained. The shared
    session is meant to persist for the container's lifetime so that
    multiple driver classes (e.g. Keithley 2281S supply + battery) can
    wrap one underlying USB session; tearing it down on every CLI script
    exit (the v0.16.5 band-aid that older `lager python` clients still
    emit on every script exit / SIGINT / BrokenPipeError) re-introduced
    the libusb release-interface race that surfaced as
    `[Errno 16] Resource busy` on the next supply or battery command.
    Cached drivers ARE still closed and dropped from `device_cache`, so
    a wedged driver can recover by being recreated on the next call —
    and because the shared raw session stays open, that recreate hits
    pyvisa instantly without a USB renegotiation.

    To force-reset a shared pyvisa session (e.g. after unplugging a USB
    instrument or for diagnostic purposes), use `POST /cache/clear_all`.
    """
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
    with device_locks_meta_lock:
        device_locks.clear()
    with _visa_resources_meta_lock:
        retained = len(_visa_resources)
    logger.info(
        f"Cleared device cache: {closed_count} closed, {error_count} errors, "
        f"{retained} shared visa session(s) retained "
        f"(use /cache/clear_all to force-close)"
    )
    return jsonify({
        'status': 'success',
        'cleared': total_count,
        'closed': closed_count,
        'errors': error_count,
        'shared_retained': retained,
    })


@app.route('/cache/clear_all', methods=['POST'])
def clear_cache_all():
    """Clear the device cache AND force-close every shared pyvisa session.

    This is the pre-v0.16.8 behavior of `/cache/clear`. It is rarely the
    right thing to call automatically — see the docstring on
    `clear_cache()`. Use it after physically unplugging/replugging a USB
    instrument when you specifically need to drop the kernel's cached
    interface descriptor.
    """
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
    with device_locks_meta_lock:
        device_locks.clear()
    with _visa_resources_meta_lock:
        shared_addresses = list(_visa_resources.keys())
    for addr in shared_addresses:
        _close_visa_resource(addr)
    logger.info(
        f"Cleared device cache (force-all): {closed_count} closed, "
        f"{error_count} errors, {len(shared_addresses)} shared visa "
        f"session(s) closed"
    )
    return jsonify({
        'status': 'success',
        'cleared': total_count,
        'closed': closed_count,
        'errors': error_count,
        'shared_closed': len(shared_addresses),
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
    html_path = '/app/lager'
    return send_from_directory(html_path, 'web_oscilloscope.html')

def _cleanup_device_cache():
    """Cleanup function called on normal process exit."""
    global device_cache, module_cache
    logger.info("Cleaning up device cache on exit...")
    for cache_key, device in list(device_cache.items()):
        _close_device(device, cache_key)
    device_cache.clear()
    module_cache.clear()
    # Close any shared pyvisa sessions opened for dual-role instruments.
    with _visa_resources_meta_lock:
        shared_addresses = list(_visa_resources.keys())
    for addr in shared_addresses:
        _close_visa_resource(addr)


# Register cleanup handler for normal process exit
atexit.register(_cleanup_device_cache)


def run_service():
    """Run the hardware invocation service"""
    logger.info(f"Starting Lager Hardware Invocation Service v{SERVICE_VERSION}")
    logger.info(f"Listening on {SERVICE_HOST}:{SERVICE_PORT}")
    logger.info(f"Endpoints:")
    logger.info(f"  POST /invoke - Invoke hardware device methods")
    logger.info(f"  GET  /health - Health check")
    logger.info(f"  POST /cache/clear - Clear device cache (retains shared pyvisa sessions)")
    logger.info(f"  POST /cache/clear_all - Clear device cache AND force-close shared pyvisa sessions")
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

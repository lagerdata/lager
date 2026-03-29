# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
GDB integration module

This module provides utilities for interacting with GDB for embedded debugging,
including architecture detection and GDB controller management.
"""

import os
import json
import time
import signal
import logging
from pygdbmi.gdbcontroller import GdbController
from pygdbmi.constants import GdbTimeoutError

logger = logging.getLogger(__name__)


class DebuggerNotConnectedError(Exception):
    """Raised when attempting to use GDB without an active debugger connection"""
    pass


GDB_TIMEOUT = 10.0

# Global cache for GDB controller to prevent multiple simultaneous connections
# Key: (device, host, port), Value: GdbController instance
_gdb_controller_cache = {}

# Track usage count for lifecycle management
# Key: (device, host, port), Value: usage count
_gdb_use_counts = {}

# Maximum uses before forcing controller recreation
MAX_CONTROLLER_USES = 10


def reap_gdb_zombies():
    """
    Reap any zombie gdb-multiarch processes

    After gdbmi.exit() terminates the gdb-multiarch process, we need to call
    waitpid() to reap the zombie and remove it from the process table.

    This function attempts to reap any zombie children of the current process.
    """
    try:
        # Try to reap any zombie children without blocking
        # WNOHANG = don't block if no children are ready
        # pid=-1 means wait for any child process
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    # No more zombie children available
                    break
            except ChildProcessError:
                # No child processes
                break
            except OSError:
                # Other errors (EINVAL, etc.)
                break
    except Exception:
        # Silently ignore any errors during zombie reaping
        pass


def get_device():
    """
    Get device type from environment

    Returns:
        Device name string

    Note:
        Expects LAGER_BOX_COMMANDS environment variable with JSON containing jlink_device
    """
    return json.loads(os.environ['LAGER_BOX_COMMANDS'])['jlink_device']


def get_arch(device):
    """
    Get ARM architecture for a given device.

    This function maps J-Link device names to GDB ARM architecture strings.
    It uses device family patterns to support the wide range of J-Link compatible devices.

    Args:
        device: Device name (case-insensitive), e.g., 'NRF52840_XXAA', 'STM32F446RE'

    Returns:
        ARM architecture string for GDB (e.g., 'armv6-m', 'armv7e-m', 'armv8-m.main')
        Falls back to 'armv7e-m' for unknown devices (most common architecture)
    """
    device = device.lower()

    # ============================================================
    # Cortex-M0/M0+ (armv6-m)
    # ============================================================
    # Raspberry Pi
    if device.startswith('rp2040'):
        return 'armv6-m'
    # Nordic nRF51 series (Cortex-M0)
    if device.startswith('nrf51'):
        return 'armv6-m'
    # STM32 Cortex-M0/M0+ families: C0, F0, G0, L0
    if (device.startswith('stm32c0') or device.startswith('stm32f0') or
        device.startswith('stm32g0') or device.startswith('stm32l0')):
        return 'armv6-m'
    # NXP LPC800/LPC1100 series (Cortex-M0/M0+)
    if device.startswith('lpc8') or device.startswith('lpc11'):
        return 'armv6-m'
    # Microchip/Atmel SAMD/SAML/SAMC (Cortex-M0+)
    if device.startswith('atsamd') or device.startswith('atsaml') or device.startswith('atsamc'):
        return 'armv6-m'
    # Silicon Labs EFM32 Zero Gecko (Cortex-M0+)
    if 'zero' in device and device.startswith('efm32'):
        return 'armv6-m'

    # ============================================================
    # Cortex-M3 (armv7-m)
    # ============================================================
    # STM32 Cortex-M3 families: F1, F2, L1
    if device.startswith('stm32f1') or device.startswith('stm32f2') or device.startswith('stm32l1'):
        return 'armv7-m'
    # NXP LPC1300/1700/1800 series (Cortex-M3)
    if device.startswith('lpc13') or device.startswith('lpc17') or device.startswith('lpc18'):
        return 'armv7-m'
    # TI Stellaris/Tiva-C (Cortex-M3)
    if device.startswith('lm3') or device.startswith('lm4f'):
        return 'armv7-m'
    # Silicon Labs EFM32 Giant/Leopard/Wonder Gecko (Cortex-M3)
    if ('giant' in device or 'leopard' in device or 'wonder' in device) and device.startswith('efm32'):
        return 'armv7-m'

    # ============================================================
    # Cortex-M4/M7 (armv7e-m) - Most common, used as default
    # ============================================================
    # Nordic nRF52 series (Cortex-M4)
    if device.startswith('nrf52'):
        return 'armv7e-m'
    # STM32 Cortex-M4 families: F3, F4, G4, L4, L4+, WB, WL
    if (device.startswith('stm32f3') or device.startswith('stm32f4') or
        device.startswith('stm32g4') or device.startswith('stm32l4') or
        device.startswith('stm32wb') or device.startswith('stm32wl')):
        return 'armv7e-m'
    # STM32 Cortex-M7 families: F7, H7
    if device.startswith('stm32f7') or device.startswith('stm32h7'):
        return 'armv7e-m'
    # NXP Kinetis K series (Cortex-M4)
    if device.startswith('mk'):
        return 'armv7e-m'
    # NXP LPC4000/LPC5400 series (Cortex-M4)
    if device.startswith('lpc4') or device.startswith('lpc54'):
        return 'armv7e-m'
    # NXP i.MX RT series (Cortex-M7)
    if device.startswith('mimxrt'):
        return 'armv7e-m'
    # TI TM4C (Cortex-M4)
    if device.startswith('tm4c'):
        return 'armv7e-m'
    # TI MSP432 (Cortex-M4)
    if device.startswith('msp432'):
        return 'armv7e-m'
    # TI CC26xx/CC13xx (Cortex-M4/M3)
    if device.startswith('cc26') or device.startswith('cc13'):
        return 'armv7e-m'
    # Microchip/Atmel SAM4/SAME/SAMS/SAMV (Cortex-M4/M7)
    if (device.startswith('atsam4') or device.startswith('atsame') or
        device.startswith('atsams') or device.startswith('atsamv')):
        return 'armv7e-m'
    # Silicon Labs EFM32/EFR32 (most are Cortex-M4)
    if device.startswith('efm32') or device.startswith('efr32'):
        return 'armv7e-m'
    # Infineon PSoC 4/5/6 (Cortex-M0/M4) - default to M4
    if device.startswith('cy') or device.startswith('psoc'):
        return 'armv7e-m'
    # Dialog DA145xx (Cortex-M0) and DA146xx/DA148xx (Cortex-M4)
    if device.startswith('da14'):
        # DA1458x/DA1468x/DA1469x are Cortex-M4, DA145xx are Cortex-M0
        if device.startswith('da1458') or device.startswith('da1468') or device.startswith('da1469'):
            return 'armv7e-m'
        return 'armv6-m'

    # ============================================================
    # Cortex-M23 (armv8-m.base)
    # ============================================================
    # NXP LPC55S0x (Cortex-M23)
    if device.startswith('lpc55s0'):
        return 'armv8-m.base'

    # ============================================================
    # Cortex-M33/M55 (armv8-m.main)
    # ============================================================
    # Nordic nRF53/nRF91 series (Cortex-M33)
    if device.startswith('nrf53') or device.startswith('nrf91'):
        return 'armv8-m.main'
    # STM32 Cortex-M33 families: L5, U5, H5, WBA
    if (device.startswith('stm32l5') or device.startswith('stm32u5') or
        device.startswith('stm32h5') or device.startswith('stm32wba')):
        return 'armv8-m.main'
    # NXP LPC55S1x/LPC55S2x/LPC55S6x (Cortex-M33)
    if device.startswith('lpc55s'):
        return 'armv8-m.main'
    # Renesas RA family (most are Cortex-M33)
    if device.startswith('r7fa'):
        return 'armv8-m.main'

    # ============================================================
    # Default fallback: armv7e-m (Cortex-M4)
    # ============================================================
    # Most embedded devices use Cortex-M4, so this is a safe default.
    # GDB will still work even if the architecture isn't perfectly matched.
    logger.debug(f'Unknown device {device}, defaulting to armv7e-m architecture')
    return 'armv7e-m'


def cleanup_controller(cache_key):
    """
    Clean up a cached GDB controller

    Args:
        cache_key: Tuple of (device, host, port)
    """
    if cache_key in _gdb_controller_cache:
        try:
            gdbmi = _gdb_controller_cache[cache_key]
            gdbmi.exit()
            reap_gdb_zombies()
        except Exception:
            pass  # Ignore cleanup errors
        finally:
            del _gdb_controller_cache[cache_key]
            if cache_key in _gdb_use_counts:
                del _gdb_use_counts[cache_key]


def disconnect_gdb_client(device=None, host='127.0.0.1', port=2331):
    """
    Disconnect the debug service's GDB client connection without stopping J-Link.

    This is useful when you want to free the J-Link GDB server connection for
    external GDB clients while keeping J-Link running.

    IMPORTANT: This function kills the gdb-multiarch process directly without
    sending a proper disconnect command, to avoid J-Link closing all connections.

    Args:
        device: Device name (optional, will read from environment if not provided)
        host: GDB server host
        port: GDB server port

    Returns:
        True if disconnected successfully, False if no connection existed
    """
    import logging
    logger = logging.getLogger(__name__)

    if device is None:
        try:
            device = get_device()
        except Exception:
            # If we can't get device from environment, try to disconnect all cached controllers
            logger.debug("No device specified, disconnecting all cached GDB controllers")
            for cache_key in list(_gdb_controller_cache.keys()):
                # Force-kill GDB processes without proper disconnect
                if cache_key in _gdb_controller_cache:
                    try:
                        gdbmi = _gdb_controller_cache[cache_key]
                        if gdbmi.gdb_process and gdbmi.gdb_process.poll() is None:
                            # Kill the GDB process directly without sending quit command
                            gdbmi.gdb_process.kill()
                            gdbmi.gdb_process.wait(timeout=2)
                            reap_gdb_zombies()
                    except Exception:
                        pass
                    # Remove from cache
                    del _gdb_controller_cache[cache_key]
                    if cache_key in _gdb_use_counts:
                        del _gdb_use_counts[cache_key]
            return len(_gdb_controller_cache) > 0

    cache_key = (device, host, port)

    if cache_key in _gdb_controller_cache:
        logger.info(f"Disconnecting GDB client for {device} (killing GDB process, J-Link will remain running)")
        gdbmi = _gdb_controller_cache[cache_key]

        try:
            # Kill the GDB process directly without sending disconnect command
            # This prevents J-Link from closing all connections
            if gdbmi.gdb_process and gdbmi.gdb_process.poll() is None:
                gdbmi.gdb_process.kill()
                gdbmi.gdb_process.wait(timeout=2)
                reap_gdb_zombies()
        except Exception:
            pass  # Ignore cleanup errors
        finally:
            # Remove from cache
            del _gdb_controller_cache[cache_key]
            if cache_key in _gdb_use_counts:
                del _gdb_use_counts[cache_key]

        return True
    else:
        logger.debug(f"No active GDB client connection for {device}")
        return False


def get_controller(device=None, host='127.0.0.1', port=2331, max_retries=3):
    """
    Create and configure GDB controller (with caching and lifecycle management)

    Args:
        device: Device name (optional, will read from environment if not provided)
        host: GDB server host
        port: GDB server port
        max_retries: Maximum number of connection attempts

    Returns:
        Configured GdbController instance (may be cached from previous call)

    Raises:
        DebuggerNotConnectedError: If unable to connect to debugger

    Note:
        This function caches GDB controller instances to prevent multiple simultaneous
        connections to the same J-Link server, which would cause timeouts. The cache
        is keyed by (device, host, port).

        Controllers are automatically recreated after MAX_CONTROLLER_USES to prevent
        state accumulation and resource leaks.
    """
    import logging
    logger = logging.getLogger(__name__)

    if device is None:
        device = get_device()

    arch = get_arch(device)

    # Allow host to be overridden from environment
    if 'LAGER_GDB_HOST' in os.environ:
        host = os.environ['LAGER_GDB_HOST']

    # Check if we have a cached connection for this device/host/port
    cache_key = (device, host, port)

    # Lifecycle management: recreate controller if used too many times
    if cache_key in _gdb_controller_cache:
        use_count = _gdb_use_counts.get(cache_key, 0)
        if use_count >= MAX_CONTROLLER_USES:
            logger.info(f"GDB controller for {device} used {use_count} times, recreating for consistency...")
            cleanup_controller(cache_key)

    if cache_key in _gdb_controller_cache:
        gdbmi = _gdb_controller_cache[cache_key]
        # Verify the cached controller is still alive
        if gdbmi.gdb_process and gdbmi.gdb_process.poll() is None:
            # Controller is still running, increment use count and reuse it
            _gdb_use_counts[cache_key] = _gdb_use_counts.get(cache_key, 0) + 1
            return gdbmi
        else:
            # Controller died, remove from cache
            cleanup_controller(cache_key)

    # Retry connection to handle J-Link startup timing
    last_error = None
    for attempt in range(max_retries):
        try:
            gdbmi = GdbController(["gdb-multiarch", "--interpreter=mi3"])
            gdbmi.get_gdb_response()
            gdbmi.write(f'set architecture {arch}', timeout_sec=GDB_TIMEOUT)

            # Configure GDB to handle Cortex-M33 and other targets with non-standard
            # register layouts. This prevents "Truncated register N in remote 'g' packet"
            # errors when the J-Link GDB server reports a different register set than
            # GDB expects.
            gdbmi.write('set mem inaccessible-by-default off', timeout_sec=GDB_TIMEOUT)

            resp = gdbmi.write(f'tar ext {host}:{port}', timeout_sec=GDB_TIMEOUT)

            # Check for connection errors (but allow non-fatal warnings to pass)
            for item in resp:
                if item.get('type') == 'result' and item.get('message') == 'error':
                    error_msg = item.get('payload', {}).get('msg', '')
                    # "Truncated register" errors are warnings that don't prevent memory
                    # operations from working - they occur when GDB's expected register
                    # count doesn't match the J-Link GDB server's response (common with
                    # Cortex-M33 devices like nRF5340). Log and continue.
                    if 'Truncated register' in error_msg:
                        logger.warning(f"GDB warning (non-fatal): {error_msg}")
                        continue
                    raise DebuggerNotConnectedError(item)

            # Cache the successful connection
            _gdb_controller_cache[cache_key] = gdbmi
            _gdb_use_counts[cache_key] = 1  # Initialize use count
            return gdbmi

        except (GdbTimeoutError, DebuggerNotConnectedError) as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(1.0)  # Wait before retry
                continue
            # Last attempt failed, raise the error
            raise DebuggerNotConnectedError(f"Failed to connect after {max_retries} attempts: {e}")
        except Exception as e:
            # For other exceptions, don't retry
            raise DebuggerNotConnectedError(f"Unexpected error connecting to debugger: {e}")

    # Should not reach here, but just in case
    raise DebuggerNotConnectedError(f"Failed to connect after {max_retries} attempts: {last_error}")


def _jlink_monitor_reset(device):
    """
    J-Link ``monitor reset`` variant for the target.

    DA1469x: by default use ``monitor reset 2`` (pin reset). Set environment
    ``LAGER_DA1469_PIN_RESET=0`` (or ``false``) on the **box** to use plain
    ``monitor reset`` (SEGGER type 0 / SYSRESETREQ) for boards where pin reset
    misbehaves or nRESET is not wired.
    """
    if device and 'DA1469' in device.upper():
        pin = os.environ.get('LAGER_DA1469_PIN_RESET', '1').strip().lower()
        if pin in ('0', 'false', 'no', 'off'):
            logger.info('DA1469x: LAGER_DA1469_PIN_RESET disabled — using monitor reset (type 0)')
            return 'monitor reset'
        return 'monitor reset 2'
    return 'monitor reset'


def reset(halt=False, device=None):
    """
    Reset device via GDB

    Args:
        halt: Whether to halt after reset
        device: Device name (optional)

    Returns:
        List of GDB responses
    """
    gdbmi = get_controller(device)
    output = []
    mon_reset = _jlink_monitor_reset(device)

    try:
        if halt:
            output += gdbmi.write(mon_reset, timeout_sec=GDB_TIMEOUT)
            output += gdbmi.write('monitor halt', timeout_sec=GDB_TIMEOUT)
        else:
            output += gdbmi.write(mon_reset, timeout_sec=GDB_TIMEOUT)
            output += gdbmi.write('monitor go', timeout_sec=GDB_TIMEOUT)

        return output
    finally:
        # Always cleanup GDB controller to prevent "active connection" errors
        try:
            if not halt:
                # For running device: Give monitor go command time to execute
                # before we disconnect GDB
                import time
                time.sleep(0.2 if device and 'DA1469' in device.upper() else 0.1)
            gdbmi.exit()
            # Reap zombie gdb-multiarch processes
            reap_gdb_zombies()
        except Exception:
            pass  # Ignore cleanup errors


def read_memory(address, length, device=None):
    """
    Read memory via GDB

    Args:
        address: Memory address to read
        length: Number of bytes to read
        device: Device name (optional)

    Returns:
        List of GDB responses
    """
    gdbmi = get_controller(device)
    try:
        cmd = f'x/{length}xb {address}'
        return gdbmi.write(cmd, timeout_sec=GDB_TIMEOUT)
    finally:
        # Always cleanup GDB controller to prevent "active connection" errors
        try:
            gdbmi.exit()
            # Reap zombie gdb-multiarch processes
            reap_gdb_zombies()
        except Exception:
            pass  # Ignore cleanup errors


def write_memory(address, data, device=None):
    """
    Write memory via GDB

    Args:
        address: Memory address to write
        data: Data to write (as hex string or integer)
        device: Device name (optional)

    Returns:
        List of GDB responses
    """
    gdbmi = get_controller(device)
    try:
        cmd = f'set {{int}}{address} = {data}'
        return gdbmi.write(cmd, timeout_sec=GDB_TIMEOUT)
    finally:
        # Always cleanup GDB controller to prevent "active connection" errors
        try:
            gdbmi.exit()
            # Reap zombie gdb-multiarch processes
            reap_gdb_zombies()
        except Exception:
            pass  # Ignore cleanup errors
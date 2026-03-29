# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Unified debug API - J-Link Only

This module provides high-level functions for debug operations including
connect, disconnect, reset, flash, and erase operations.
"""

import os
import json
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from .jlink import JLink
from .mappings import (
    get_jlink_status,
    readfile,
    JL_LOGFILE,
)
from .process import (
    start_jlink,
    stop_jlink,
)
from .gdbserver import get_jlink_gdbserver_status, stop_jlink_gdbserver, start_jlink_gdbserver
from .gdb import get_arch, reset as gdb_reset, read_memory as gdb_read_memory

logger = logging.getLogger(__name__)

# Temp path for J-Link script file (written during connect)
JLINK_SCRIPT_TEMP_PATH = '/tmp/lager_jlink_script.JLinkScript'


def _get_script_file():
    """Return script file path if it exists, None otherwise."""
    if os.path.exists(JLINK_SCRIPT_TEMP_PATH):
        return JLINK_SCRIPT_TEMP_PATH
    return None


class DebugError(Exception):
    """Base class for debug errors"""
    pass


class JLinkStartError(DebugError):
    """Error starting J-Link"""
    def __init__(self, stdout, stderr, logfile):
        self.stdout = stdout
        self.stderr = stderr
        self.logfile = logfile
        # Decode stderr if it's bytes
        if isinstance(stderr, bytes):
            stderr_str = stderr.decode('utf-8', errors='replace')
        else:
            stderr_str = str(stderr)
        super().__init__(stderr_str)


class JLinkAlreadyRunningError(DebugError):
    """J-Link is already running"""
    pass


class JLinkNotRunning(DebugError):
    """J-Link is not running"""
    pass


def ensure_int(value):
    """Ensure value is a valid integer"""
    try:
        return int(value)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid integer value: {value}")


def validate_speed(speed):
    """
    Validate speed parameter

    Args:
        speed: Speed value as string (kHz) or 'adaptive'

    Returns:
        Validated speed string

    Raises:
        ValueError: If speed is invalid
    """
    if speed == 'adaptive':
        return speed

    try:
        speed_int = int(speed)
    except (ValueError, TypeError):
        raise ValueError(
            f"Invalid speed value: '{speed}'. "
            f"Speed must be a positive integer (in kHz) or 'adaptive'"
        )

    if speed_int <= 0:
        raise ValueError(
            f"Invalid speed: {speed_int} kHz. "
            f"Speed must be a positive integer greater than 0"
        )

    if speed_int > 50000:  # 50 MHz is unrealistically high for SWD/JTAG
        raise ValueError(
            f"Invalid speed: {speed_int} kHz. "
            f"Maximum supported speed is 50000 kHz (50 MHz). "
            f"Typical speeds: 100-4000 kHz"
        )

    return speed


def clean_logfile_content(logfile_content, max_length=2000):
    """
    Clean logfile content for error messages

    Args:
        logfile_content: Raw logfile content (may contain null bytes)
        max_length: Maximum length to return

    Returns:
        Cleaned logfile content string
    """
    if isinstance(logfile_content, bytes):
        # Remove null bytes and decode
        cleaned = logfile_content.replace(b'\x00', b'').decode('utf-8', errors='ignore')
    else:
        # Remove null bytes from string
        cleaned = logfile_content.replace('\x00', '')

    # Limit length and add truncation notice if needed
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + f"\n\n... (truncated, total length: {len(logfile_content)} bytes)"

    return cleaned.strip()


def detect_and_configure_rtt(device_type=None, search_addr=0x20000000, search_size=0x10000, chunk_size=0x1000):
    """
    Detect RTT control block in RAM and configure J-Link to use it.

    This function searches RAM for the RTT control block signature ("SEGGER RTT")
    and tells J-Link where to find it. This is necessary because:

    1. J-Link only searches for RTT at 4KB-aligned addresses by default
    2. Many firmwares (especially Rust) place RTT at non-aligned addresses
    3. Without this, RTT streaming will fail even though firmware has RTT enabled

    Args:
        device_type: Optional device type hint (not currently used)
        search_addr: RAM start address to search (default: 0x20000000)
        search_size: Size of RAM region to search in bytes (default: 0x10000 / 64KB)
        chunk_size: Size of each read chunk in bytes (default: 0x1000 / 4KB)

    Returns:
        dict with 'found': bool, 'address': str (hex) if found, 'error': str if error
    """
    from .gdb import get_controller

    result = {
        'found': False,
        'address': None,
        'error': None
    }

    try:
        # Check if debugger is connected (check both PID file paths)
        jlink_status = get_jlink_status()
        gdbserver_status = get_jlink_gdbserver_status()
        if not jlink_status['running'] and not gdbserver_status['running']:
            result['error'] = 'No debugger connection'
            return result

        # Get GDB controller
        gdbmi = get_controller(device=device_type)

        logger.info('Searching RAM for RTT control block...')

        # RTT control block starts with magic bytes: "SEGGER RTT"
        # Search common RAM regions (typically 0x20000000 - 0x20010000 for most ARM devices)
        rtt_signature = b'SEGGER RTT'
        rtt_address = None

        for offset in range(0, search_size, chunk_size):
            addr = search_addr + offset
            try:
                # Read 4KB chunk from RAM using GDB MI command
                # This is the same command format used by the working memrd implementation
                mem_cmd = f'-data-read-memory-bytes {addr} {chunk_size}'
                mem_responses = gdbmi.write(mem_cmd, timeout_sec=2.0, raise_error_on_timeout=False)

                # Parse memory dump and look for RTT signature
                memory_data = []
                for resp in mem_responses:
                    if resp.get('type') == 'result' and resp.get('message') == 'done':
                        memory = resp.get('payload', {}).get('memory', [])
                        if memory and len(memory) > 0:
                            # Contents are in hex string format (e.g., "53454747455220525454")
                            contents = memory[0].get('contents', '')
                            # Convert hex string to bytes
                            if contents:
                                memory_data = bytes.fromhex(contents)
                                break

                # Search for RTT signature in this chunk
                if len(memory_data) >= len(rtt_signature):
                    sig_index = memory_data.find(rtt_signature)
                    if sig_index != -1:
                        rtt_address = hex(addr + sig_index)
                        logger.info(f'Found RTT control block at {rtt_address}')
                        break
            except Exception as chunk_error:
                logger.debug(f'Error searching RAM at {addr:#x}: {chunk_error}')
                continue

        if rtt_address:
            # Tell J-Link where the RTT control block is located
            set_rtt_cmd = f'monitor exec SetRTTAddr {rtt_address}'
            logger.info(f'Setting RTT address: {set_rtt_cmd}')
            set_responses = gdbmi.write(set_rtt_cmd, timeout_sec=2.0, raise_error_on_timeout=False)
            for resp in set_responses:
                if resp.get('type') == 'console':
                    logger.info(f'SetRTTAddr response: {resp.get("payload", "")}')

            result['found'] = True
            result['address'] = rtt_address
            logger.info(f'RTT configured successfully at address {rtt_address}')
        else:
            logger.info('No RTT control block found in RAM (firmware may not use RTT or not initialized yet)')
            result['error'] = 'RTT control block not found in RAM'

    except Exception as e:
        logger.warning(f'RTT auto-detection failed: {e}')
        result['error'] = str(e)

    return result


def connect_jlink(speed, device, transport, force=False, ignore_if_connected=False,
                  vardefs=None, attach='attach', idcode=None):
    """
    Connect to target via J-Link

    Args:
        speed: Interface speed (in kHz) or 'adaptive'
        device: J-Link device name (e.g., 'NRF52840_XXAA', 'R7FA0E107')
        transport: Transport protocol ('SWD' or 'JTAG')
        force: Force connection even if already connected
        ignore_if_connected: Return success if already connected
        vardefs: List of (varname, varvalue) tuples for additional settings
        attach: Attach mode ('attach', 'reset', 'reset-halt')
        idcode: 16-byte IDCODE for Renesas locked devices (hex string)

    Returns:
        Status dictionary

    Raises:
        JLinkAlreadyRunningError: If J-Link is running and force=False
        JLinkStartError: If J-Link fails to start
    """
    if vardefs is None:
        vardefs = []

    # Check both PID files - CLI uses gdbserver path, Python API uses legacy path
    status = get_jlink_status()
    gdbserver_status = get_jlink_gdbserver_status()

    # Consider J-Link running if either path shows it running
    jlink_running = status['running'] or gdbserver_status['running']

    if jlink_running and ignore_if_connected:
        return {'already_running': 'ok'}

    if jlink_running and not force:
        raise JLinkAlreadyRunningError()

    # Stop both code paths to ensure clean state
    stop_jlink()
    stop_jlink_gdbserver()

    # Give hardware time to settle after disconnect to prevent fatigue
    # This prevents "Cannot connect to J-Link" errors during rapid operations
    time.sleep(0.3)  # Reduced from 0.5s - minimum USB release time

    # Set default speed if not provided
    if speed is None:
        speed = '4000'

    # Validate speed parameter
    try:
        speed = validate_speed(speed)
    except ValueError as e:
        raise DebugError(str(e))

    # Try multiple speeds if initial connection fails
    # Start with requested speed, then fall back to slower speeds if it fails
    speeds_to_try = []
    requested_speed = speed  # Save the originally requested speed

    if speed == 'adaptive':
        # For adaptive, try it first, then fall back to known-good speeds
        speeds_to_try = [speed, '4000', '1000', '500', '100']
    elif int(speed) > 1000:
        # For high speeds, try requested, then progressively slower fallbacks
        speeds_to_try = [speed, '1000', '500', '100']
    elif int(speed) > 500:
        # For medium-high speeds, try requested, then slower fallbacks
        speeds_to_try = [speed, '500', '100']
    elif int(speed) > 100:
        # For medium speeds, try requested, then 100 as fallback
        speeds_to_try = [speed, '100']
    else:
        # For speeds <= 100, just use the requested speed
        speeds_to_try = [speed]

    # Remove duplicates while preserving order
    seen = set()
    speeds_to_try = [s for s in speeds_to_try if not (s in seen or seen.add(s))]

    last_error = None

    # Determine halt mode based on attach parameter
    # Don't try multiple halt modes - use what the user requested
    if attach == 'reset-halt':
        halt_mode = '-halt'
    else:
        halt_mode = '-nohalt'

    for attempt_speed in speeds_to_try:
        if len(speeds_to_try) > 1:
            logger.debug(f'Attempting connection at {attempt_speed} kHz...')

        # Use the same start_jlink_gdbserver() function as the CLI
        # This ensures consistent behavior between Python API and CLI
        halt = (attach == 'reset-halt')

        try:
            result = start_jlink_gdbserver(
                device=device,
                speed=attempt_speed,
                transport=transport,
                halt=halt,
                gdb_port=2331
            )
        except Exception as exc:
            last_error = JLinkStartError(b'', str(exc).encode(), str(exc))
            continue

        # Check if gdbserver started successfully
        gdbserver_status = get_jlink_gdbserver_status()
        if gdbserver_status['running']:
            status = {
                'running': True,
                'start': 'ok',
                'speed': attempt_speed,
                'requested_speed': requested_speed,
                'fallback_used': (attempt_speed != requested_speed),
                'pid': result.get('pid'),
                'gdb_port': result.get('gdb_port', 2331),
            }

            # Give J-Link GDB server additional time to start accepting connections
            logger.debug('Waiting for J-Link GDB server to be ready for connections...')
            time.sleep(1.0)

            # Perform reset if requested (after server is confirmed ready)
            if attach == 'reset-halt' or attach == 'reset':
                try:
                    gdb_reset(halt=halt, device=device)
                except Exception as e:
                    logger.warning(f'Reset after connect failed: {e}')

            # EXPLICIT VERIFICATION: Test GDB connection to confirm target is responsive
            try:
                from .gdb import get_controller
                logger.debug('Verifying GDB connection to target...')
                gdbmi = get_controller(device=device)

                # Try a simple monitor command to verify connection
                verify_responses = gdbmi.write('monitor version', timeout_sec=3.0, raise_error_on_timeout=False)
                connection_verified = False
                for resp in verify_responses:
                    if resp.get('type') == 'console':
                        connection_verified = True
                        break

                if connection_verified:
                    status['target_verified'] = True
                    logger.debug('Target connection verified successfully')
                else:
                    logger.warning('Target connection could not be verified (no response from monitor command)')
                    status['target_verified'] = False

            except Exception as e:
                logger.warning(f'Target verification failed: {e}')
                status['target_verified'] = False

            return status
        else:
            # Connection failed at this speed, try next
            stop_jlink_gdbserver()
            time.sleep(0.5)
            last_error = JLinkStartError(b'', b'Connection failed', 'GDB server failed to start')

    # All attempts failed
    stop_jlink_gdbserver()
    logfile_content = status.get('logfile', 'No log available') if 'status' in locals() else 'No log available'
    logfile_content_clean = clean_logfile_content(logfile_content)

    # Check for locked device (Renesas-specific but keep for compatibility)
    if 'Locked Renesas device detected' in logfile_content_clean or 'IDCODE' in logfile_content_clean:
        error_msg = (
            "ERROR: Device is LOCKED\n\n"
            "The target device has ID Code Protection enabled and requires an IDCODE to unlock.\n\n"
            "For Renesas devices, to unlock:\n"
            "1. Use Renesas Flash Programmer (RFP) to unlock the device\n"
            "   - Download from: https://www.renesas.com/software-tool/renesas-flash-programmer-programming-gui\n"
            "   - Connect via J-Link and select 'ID Authentication'\n"
            "   - WARNING: Unlocking will ERASE all flash memory\n\n"
            "2. Or provide the correct 16-byte IDCODE if you have it\n\n"
            f"Device: {device}\n"
            f"Transport: {transport}\n"
        )
    else:
        if len(speeds_to_try) > 1:
            # Provide device-specific pin information for R7FA0E107
            pin_info = ""
            if device.startswith('R7FA0E1'):
                pin_info = (
                    "\nFor R7FA0E107 (RA0E1), verify SWD connections:\n"
                    "  - SWDIO: P108 (pin 15 on 32-pin package)\n"
                    "  - SWCLK: P300 (pin 8 on 32-pin package)\n"
                    "  - RESET: P213 (pin 1) - MUST have 10k pull-up to VCC\n\n"
                )

            error_msg = (
                f"Cannot connect to target device (tried {', '.join(speeds_to_try)} kHz).\n\n"
                "The J-Link probe was found, but cannot establish communication with the target MCU.\n"
                f"{pin_info}"
                "TROUBLESHOOTING CHECKLIST:\n"
                "1. Verify power and connections\n"
                "   - Check target voltage is present and stable (use multimeter)\n"
                "   - Verify debug interface pins (SWDIO/SWCLK or JTAG)\n"
                "   - Check for cold solder joints or poor connections\n"
                "   - Ensure ground connection between J-Link and target\n\n"
                "2. Reset pin issues\n"
                "   - Ensure RESET/nRST has proper pull-up resistor (typically 10k)\n"
                "   - Verify nothing is holding RESET LOW\n"
                "   - Try connecting RESET pin to J-Link's RESET output\n\n"
                "3. Device protection or sleep mode\n"
                "   - Device may have debug protection/readout protection enabled\n"
                "   - Device may be in deep sleep or low power mode\n"
                "   - Try power cycling the target\n"
                "   - Check if SWD pins are configured for alternate functions\n\n"
                f"Full J-Link log:\n{logfile_content_clean}"
            )
        else:
            error_msg = f"J-Link server failed to start or connect to target.\n\nLog output:\n{logfile_content_clean}"

    raise JLinkStartError(b'', error_msg.encode(), logfile_content_clean)


def connect(interface, speed, device, transport, **kwargs):
    """
    Connect to debug target (J-Link only)

    Args:
        interface: Must be 'third-party' for J-Link
        speed: Interface speed
        device: Device name
        transport: Transport protocol
        **kwargs: Additional arguments passed to connect function

    Returns:
        Status dictionary

    Raises:
        DebugError: If non-J-Link interface specified
    """
    if interface != 'third-party':
        raise DebugError(f"Only J-Link (third-party) interface supported. Got: {interface}")
    return connect_jlink(speed, device, transport, **kwargs)


def disconnect(mcu=None, keep_jlink_running=False):
    """
    Disconnect from debug target (J-Link only)

    Args:
        mcu: MCU identifier (optional, unused for J-Link)
        keep_jlink_running: If True, only disconnect GDB client but leave J-Link running.
                           This allows external GDB clients to connect.

    Returns:
        Status dictionary
    """
    from .gdb import disconnect_gdb_client

    if keep_jlink_running:
        # Only disconnect the debug service's GDB client, leave J-Link running
        was_connected = disconnect_gdb_client(device=mcu)
        return {
            'stop': 'ok',
            'gdb_client_disconnected': was_connected,
            'jlink_still_running': True
        }
    else:
        # Original behavior: stop everything
        # Check both PID files (CLI uses gdbserver, Python API uses legacy)
        jlink_status = get_jlink_status()
        gdbserver_status = get_jlink_gdbserver_status()

        if jlink_status['running'] or gdbserver_status['running']:
            # First disconnect GDB client
            disconnect_gdb_client(device=mcu)
            # Stop both code paths to ensure clean state
            stop_jlink()
            stop_jlink_gdbserver()
            return {'stop': 'ok'}

        return {'stop': 'ok'}


def reset_device(halt=False, mcu=None):
    """
    Reset connected device (J-Link only)

    Args:
        halt: Whether to halt after reset
        mcu: MCU identifier (optional, unused for J-Link)

    Returns:
        Generator yielding output from reset operation

    Raises:
        JLinkNotRunning: If J-Link is not running
    """
    # Try legacy path first (uses /tmp/jlink.pid)
    jlink_status = get_jlink_status()
    if jlink_status['running'] and jlink_status.get('cmdline'):
        try:
            jlink = JLink(jlink_status['cmdline'], script_file=_get_script_file())
            return jlink.reset(halt)
        except (ValueError, KeyError):
            pass  # Fall through to gdbserver path

    # Try gdbserver path (uses /tmp/jlink_gdbserver.pid)
    gdbserver_status = get_jlink_gdbserver_status()
    if gdbserver_status['running']:
        pid = gdbserver_status.get('pid')
        if pid:
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = [part.decode() for part in f.read().split(b'\x00')]
                jlink = JLink(cmdline, script_file=_get_script_file())
                return jlink.reset(halt)
            except (OSError, IOError, ValueError, KeyError):
                pass  # Fall through to error

    raise JLinkNotRunning()


def erase_flash(start_addr, length, mcu=None):
    """
    Erase flash memory (J-Link only)

    Args:
        start_addr: Starting address
        length: Number of bytes to erase
        mcu: MCU identifier (optional, unused for J-Link)

    Returns:
        Generator yielding output from erase operation

    Raises:
        JLinkNotRunning: If J-Link is not running
    """
    # Check both PID files (CLI uses gdbserver, Python API uses legacy)
    jlink_status = get_jlink_status()
    if jlink_status['running'] and jlink_status.get('cmdline'):
        try:
            jlink = JLink(jlink_status['cmdline'], script_file=_get_script_file())
            return jlink.erase(start_addr, length)
        except (ValueError, KeyError):
            pass  # Fall through to gdbserver path

    gdbserver_status = get_jlink_gdbserver_status()
    if gdbserver_status['running']:
        pid = gdbserver_status.get('pid')
        if pid:
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmdline = [part.decode() for part in f.read().split(b'\x00')]
                jlink = JLink(cmdline, script_file=_get_script_file())
                return jlink.erase(start_addr, length)
            except (OSError, IOError, ValueError, KeyError):
                pass  # Fall through to error

    raise JLinkNotRunning()


def chip_erase(device, speed='4000', transport='SWD', mcu=None, script_file=None):
    """
    Perform full chip erase (J-Link only)

    This will erase the entire flash memory on the device.
    WARNING: This will erase ALL data on the chip, including any
    protection settings on Renesas devices.

    Args:
        device: J-Link device name (e.g., 'R7FA0E107', 'NRF52840_XXAA')
        speed: Interface speed in kHz (default: 4000)
        transport: Transport protocol ('SWD' or 'JTAG', default: 'SWD')
        mcu: MCU identifier (optional, unused for J-Link)
        script_file: Optional path to J-Link script (from debug service); if None,
            uses a temp file left by connect or api._get_script_file().

    Returns:
        Generator yielding output from erase operation

    Raises:
        JLinkStartError: If J-Link fails to start
    """
    # Lazy import to avoid circular dependencies
    from .jlink import JLink

    # Stop ALL running J-Link processes to free the USB probe for JLinkExe.
    # Legacy start_jlink() uses /tmp/jlink.pid; the debug service uses
    # JLinkGDBServer (/tmp/jlink_gdbserver.pid). Both must be stopped or
    # JLinkExe cannot get exclusive USB access (erase may fail or hit wrong flash).
    stop_jlink()
    stop_jlink_gdbserver()

    # Give the hardware time to be released
    time.sleep(0.5)

    # Build command args for JLinkExe (used by JLink class)
    cmd_args = [
        '-device', device,
        '-if', transport,
        '-speed', str(speed)
    ]

    # Create JLink instance with command args
    # Note: This will use JLinkExe, not GDB server
    class TempJLink:
        def __init__(self, args, script_file=None):
            self.args = args
            self.script_file = script_file

    resolved_script = script_file if (script_file and os.path.exists(script_file)) else _get_script_file()
    if not resolved_script:
        logger.warning(
            'chip_erase: no J-Link script file; DA1469x external QSPI may not be erased'
        )

    jlink = TempJLink(cmd_args, script_file=resolved_script)
    jlink.__class__ = JLink

    return jlink.chip_erase()


def flash_device(files, preverify=False, verify=True, run_after=False, mcu=None, use_gdb=True,
                 script_file=None):
    """
    Flash firmware to device using JLinkExe.

    Note: The use_gdb parameter is deprecated and ignored. Flash always uses JLinkExe
    for reliability. The GDB-based flash method was removed due to unreliable behavior
    where it would report success but not actually program the device.

    Args:
        files: Tuple of (hexfiles, binfiles, elffiles)
        preverify: Verify before flashing (unused)
        verify: Verify after flashing (unused)
        run_after: Reset and run after flashing (JLinkExe does this automatically)
        mcu: MCU identifier (e.g., 'nRF52833_XXAA')
        use_gdb: DEPRECATED - ignored, always uses JLinkExe
        script_file: Optional path to J-Link script file (from debug service)

    Returns:
        Generator yielding output from flash operation
    """
    from .jlink import JLink

    # Stop ALL running J-Link processes to free the USB probe for JLinkExe.
    # Both the legacy path (jlink.pid) and JLinkGDBServer (jlink_gdbserver.pid)
    # must be stopped; otherwise JLinkExe cannot get exclusive USB access.
    stop_jlink()
    stop_jlink_gdbserver()

    # Give the hardware time to be released
    time.sleep(0.5)

    hexfiles, binfiles, elffiles = files

    # Always use JLinkExe for reliable flashing

    # Build J-Link args from mcu parameter and defaults
    device = mcu if mcu else 'nRF52833_XXAA'
    speed = '4000'  # Default speed
    transport = 'SWD'  # Default transport

    jlink_args = ['-device', device, '-if', transport, '-speed', speed]

    yield f"Flashing device {device} via JLinkExe..."

    # Create JLink instance with extracted args
    class TempJLink:
        def __init__(self, args, script_file=None):
            self.args = args
            self.script_file = script_file

    resolved_script = script_file if (script_file and os.path.exists(script_file)) else _get_script_file()
    jlink = TempJLink(jlink_args, script_file=resolved_script)
    jlink.__class__ = JLink

    yield from jlink.flash(files, preverify, verify)

    # Note: JLinkExe's loadfile command automatically resets and runs the device
    # Reconnect GDB server after JLinkExe finishes
    yield "Reconnecting GDB server..."

    time.sleep(1.0)  # Give JLinkExe time to fully disconnect

    try:
        # Use start_jlink() to match the PID file checked by get_jlink_status()
        from .process import start_jlink
        cmd_args = [
            '-nohalt',  # Don't halt after reset
            '-device', device,
            '-if', transport,
            '-speed', speed
        ]
        start_jlink(cmd_args)
        yield "GDB server reconnected"
    except Exception as e:
        yield f"Warning: Failed to reconnect GDB server: {e}"



def read_memory(address, length, mcu=None):
    """
    Read memory from target device via J-Link monitor command

    This function uses J-Link's native monitor commands instead of GDB's examine command
    because GDB's 'x' command can be unreliable in MI mode.

    Args:
        address: Memory address to read (int or hex string)
        length: Number of bytes to read
        mcu: MCU/device name (optional)

    Returns:
        bytes: Memory contents as bytes object

    Raises:
        JLinkNotRunning: If J-Link GDB server is not running
        DebugError: If memory read fails
    """
    # Ensure J-Link is running (check both PID file paths)
    jlink_status = get_jlink_status()
    gdbserver_status = get_jlink_gdbserver_status()
    if not jlink_status['running'] and not gdbserver_status['running']:
        raise JLinkNotRunning("J-Link GDB server is not running. Call connect() first.")

    # Convert address to int if it's a string
    if isinstance(address, str):
        address = int(address, 16 if address.startswith('0x') else 10)

    try:
        # Use J-Link Commander directly for reliable memory reads
        # This bypasses GDB entirely and uses J-Link's native capabilities

        # Try legacy path first, then gdbserver path
        jlink = None
        if jlink_status['running'] and jlink_status.get('cmdline'):
            try:
                jlink = JLink(jlink_status['cmdline'], script_file=_get_script_file())
            except (ValueError, KeyError):
                pass

        if jlink is None and gdbserver_status['running']:
            pid = gdbserver_status.get('pid')
            if pid:
                try:
                    with open(f'/proc/{pid}/cmdline', 'rb') as f:
                        cmdline = [part.decode() for part in f.read().split(b'\x00')]
                    jlink = JLink(cmdline, script_file=_get_script_file())
                except (OSError, IOError, ValueError, KeyError):
                    pass

        if jlink is None:
            raise JLinkNotRunning("J-Link GDB server is not running. Call connect() first.")

        # Use J-Link Commander to read memory directly
        memory_data = jlink.read_memory(address, length)

        if not memory_data:
            raise DebugError("No memory data returned from J-Link")

        if len(memory_data) < length:
            raise DebugError(f"Only read {len(memory_data)} bytes, expected {length}")

        return memory_data

    except Exception as e:
        raise DebugError(f"Failed to read memory at 0x{address:08X}: {e}")


class RTT:
    """
    SEGGER Real-Time Transfer (RTT) context manager for bidirectional communication
    with embedded devices during debugging.

    RTT provides high-speed communication faster than UART with no timing impact on
    the target application.

    Usage:
        with debug_net.rtt() as rtt:
            data = rtt.read_some(timeout=1.0)
            if data:
                print(data.decode('utf-8'))
            rtt.write(b'command\\n')
    """

    def __init__(self, device=None, channel=0, search_addr=None, search_size=None, chunk_size=None):
        """
        Initialize RTT session.

        Args:
            device: Device name (optional, for auto-detection)
            channel: RTT channel number (default: 0)
            search_addr: RAM start address for RTT control block search (default: 0x20000000)
            search_size: Size of RAM region to search in bytes (default: 0x10000 / 64KB)
            chunk_size: Size of each read chunk in bytes (default: 0x1000 / 4KB)
        """
        self.device = device
        self.channel = channel
        self.search_addr = search_addr
        self.search_size = search_size
        self.chunk_size = chunk_size
        self._socket = None
        self._port = 9090 + channel

    def __enter__(self):
        """Enter RTT context - establish connection"""
        import socket
        import time

        # Check if debugger is connected (check both PID file paths)
        status = get_jlink_status()
        gdbserver_status = get_jlink_gdbserver_status()
        if not status['running'] and not gdbserver_status['running']:
            raise JLinkNotRunning("J-Link must be connected before using RTT")

        # Auto-detect and configure RTT control block
        rtt_kwargs = {'device_type': self.device}
        if self.search_addr is not None:
            rtt_kwargs['search_addr'] = self.search_addr
        if self.search_size is not None:
            rtt_kwargs['search_size'] = self.search_size
        if self.chunk_size is not None:
            rtt_kwargs['chunk_size'] = self.chunk_size
        rtt_result = detect_and_configure_rtt(**rtt_kwargs)
        if rtt_result['found']:
            logger.info(f"RTT control block found at {rtt_result['address']}")
        elif rtt_result['error']:
            logger.warning(f"RTT auto-detection warning: {rtt_result['error']}")

        # Connect to J-Link RTT telnet server with retry logic
        max_retries = 5 if self.channel == 0 else 1
        retry_delay = 0.5

        for attempt in range(max_retries):
            try:
                # Try IPv6 first (::1), then fall back to IPv4 (127.0.0.1)
                last_error = None

                for family, addr in [(socket.AF_INET6, '::1'), (socket.AF_INET, '127.0.0.1')]:
                    try:
                        self._socket = socket.socket(family, socket.SOCK_STREAM)
                        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        self._socket.settimeout(2.0)
                        self._socket.connect((addr, self._port))
                        logger.info(f"RTT connected on attempt {attempt + 1} using {addr}:{self._port}")
                        return self
                    except (ConnectionRefusedError, socket.timeout, OSError) as e:
                        last_error = e
                        if self._socket:
                            try:
                                self._socket.close()
                            except OSError:
                                pass
                            self._socket = None

                if last_error:
                    raise last_error

            except (ConnectionRefusedError, socket.timeout, OSError) as conn_err:
                if self._socket:
                    try:
                        self._socket.close()
                    except OSError:
                        pass
                    self._socket = None

                if attempt < max_retries - 1:
                    logger.warning(f"RTT connection attempt {attempt + 1}/{max_retries} failed. Retrying...")
                    time.sleep(retry_delay)
                    retry_delay *= 1.5
                else:
                    if self.channel == 0:
                        error_msg = f'Cannot connect to RTT (port {self._port}). Device may not have RTT initialized.'
                    else:
                        error_msg = f'RTT channel {self.channel} not available. Try channel 0.'
                    raise DebugError(error_msg)

        raise DebugError('Failed to establish RTT connection')

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit RTT context - close connection"""
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        return False

    def read_some(self, timeout=1.0):
        """
        Read available data from RTT with timeout.

        Args:
            timeout: Read timeout in seconds (default: 1.0)

        Returns:
            bytes: Data read from RTT, or None if timeout
        """
        if not self._socket:
            raise DebugError("RTT not connected")

        import select

        # Wait for data with timeout
        ready = select.select([self._socket], [], [], timeout)
        if not ready[0]:
            return None

        try:
            data = self._socket.recv(4096)
            return data if data else None
        except Exception as e:
            logger.error(f"RTT read error: {e}")
            return None

    def write(self, data):
        """
        Write data to RTT.

        Args:
            data: bytes to send to target

        Returns:
            int: Number of bytes written
        """
        if not self._socket:
            raise DebugError("RTT not connected")

        if isinstance(data, str):
            data = data.encode('utf-8')

        try:
            return self._socket.send(data)
        except Exception as e:
            logger.error(f"RTT write error: {e}")
            raise DebugError(f"Failed to write to RTT: {e}")
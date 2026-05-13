# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Debug net classes for embedded device debugging via Net API.

This module provides the DebugNet class for debug operations and a fallback
_NullDebug class when the debug module is not available.
"""
from __future__ import annotations

from .constants import NetType


# -------- _NullDebug fallback (always defined for imports) --------
class _NullDebug:
    """Fallback debug class when debug module is not available."""
    def __init__(self, name, net_info=None):
        self.name = name
        self.type = NetType.Debug
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def __call__(self, *a, **k): return self
    def log(self, *a, **k): pass
    def connect(self, *a, **k): raise RuntimeError("Debug module not available")
    def disconnect(self, *a, **k): raise RuntimeError("Debug module not available")
    def reset(self, *a, **k): raise RuntimeError("Debug module not available")
    def flash(self, *a, **k): raise RuntimeError("Debug module not available")
    def erase(self, *a, **k): raise RuntimeError("Debug module not available")
    def status(self, *a, **k): raise RuntimeError("Debug module not available")
    def read_memory(self, *a, **k): raise RuntimeError("Debug module not available")
    def rtt(self, *a, **k): raise RuntimeError("Debug module not available")
    def rtt_read(self, *a, **k): raise RuntimeError("Debug module not available")


# -------- optional debug import (never crash if lager.debug is missing) --------
try:
    from ..debug import (
        connect_jlink,
        disconnect,
        reset_device,
        flash_device,
        chip_erase,
        erase_flash,
        get_jlink_status,
        get_jlink_gdbserver_status,
        read_memory as debug_read_memory,
        RTT,
    )

    class DebugNet:
        """Wrapper for debug operations via Net API"""
        def __init__(self, name, net_info):
            self.name = name
            self.type = NetType.Debug
            self._net_info = net_info
            # Extract device info from net configuration
            # Device type is stored in 'pin' field (legacy) or 'channel' field
            instrument = net_info.get('instrument', '').lower()
            device = net_info.get('channel') or net_info.get('pin')
            if not device:
                raise ValueError(
                    f"Debug net '{name}' has no device type configured. "
                    f"Please specify a J-Link device type (e.g., 'NRF52840_XXAA', 'STM32F446RE') "
                    f"when creating the debug net."
                )
            self.device = device
            self.speed = '4000'  # default speed
            self.transport = 'SWD'  # default transport

        def connect(self, speed=None, transport=None):
            """Connect to target device"""
            speed = speed or self.speed
            transport = transport or self.transport
            return connect_jlink(
                speed=speed,
                device=self.device,
                transport=transport
            )

        def disconnect(self):
            """Disconnect from target"""
            return disconnect()

        def reset(self, halt=False):
            """Reset the device"""
            results = []
            for line in reset_device(halt=halt):
                results.append(line)
            return '\n'.join(results)

        def flash(self, firmware_path):
            """Flash firmware to device"""
            import os
            ext = os.path.splitext(firmware_path)[1].lower()
            if ext == '.hex':
                files = ([firmware_path], [], [])
            elif ext == '.bin':
                files = ([], [(firmware_path, 0x00000000)], [])
            elif ext == '.elf':
                files = ([], [], [firmware_path])
            else:
                raise ValueError(f"Unsupported firmware file type: {ext}")

            results = []
            for line in flash_device(files, mcu=self.device):
                results.append(line)
            return '\n'.join(results)

        def erase(self):
            """Perform chip erase"""
            results = []
            for line in chip_erase(device=self.device, speed=self.speed, transport=self.transport):
                results.append(line)
            return '\n'.join(results)

        def read_memory(self, address, length):
            """Read memory from target device"""
            return debug_read_memory(address, length, mcu=self.device)

        def status(self):
            """Get connection status"""
            status = get_jlink_status()
            gdbserver_status = get_jlink_gdbserver_status()
            # Consider connected if either path shows running
            if gdbserver_status['running'] and not status['running']:
                return gdbserver_status
            return status

        def rtt(self, channel=0, search_addr=None, search_size=None, chunk_size=None):
            """
            Create RTT (Real-Time Transfer) session for bidirectional communication.

            Args:
                channel: RTT channel number (default: 0)
                search_addr: RAM start address for RTT control block search (default: 0x20000000)
                search_size: Size of RAM region to search in bytes (default: 0x10000 / 64KB)
                chunk_size: Size of each read chunk in bytes (default: 0x1000 / 4KB)

            Returns:
                RTT context manager

            Example:
                with debug.rtt() as rtt:
                    data = rtt.read_some(timeout=1.0)
                    if data:
                        print(data.decode('utf-8'))
                    rtt.write(b'command\\n')
            """
            return RTT(device=self.device, channel=channel,
                       search_addr=search_addr, search_size=search_size, chunk_size=chunk_size)

        def rtt_read(self, timeout=3.0, max_chars=None, channel=0, settle=1.5,
                     search_addr=None, search_size=None, chunk_size=None,
                     encoding='utf-8'):
            """
            One-shot RTT read: open the RTT channel, collect output for up to
            ``timeout`` seconds (or until ``max_chars`` characters are gathered),
            close it, and return the decoded text.

            This is a convenience wrapper over ``rtt()``. Unlike
            ``rtt().read_some()`` (which returns ``bytes`` or ``None`` and must be
            looped), this manages the connect/read-loop/close lifecycle for you
            and always returns a ``str`` — empty if the device produced nothing.

            Args:
                timeout: total seconds to collect RTT output (default: 3.0)
                max_chars: stop early once this many characters are collected
                channel: RTT channel number (default: 0)
                settle: seconds to wait after opening RTT before reading, to let
                        the J-Link RTT relay spin up (default: 1.5). The relay
                        typically emits nothing for ~1s after connect.
                search_addr/search_size/chunk_size: forwarded to ``rtt()``
                encoding: text encoding (default 'utf-8'; undecodable bytes are replaced)

            Returns:
                str: collected RTT output

            Example:
                debug.flash("/tmp/firmware.hex")
                debug.reset()
                output = debug.rtt_read(timeout=3.0)
                assert "blink" in output
            """
            import time as _time
            # rtt() requires the probe to already be connected; flash()/connect()
            # normally handles that. If nothing is connected yet, connect best-effort
            # so a bare "flash, then read the console" or "just read the console" flow
            # works without an explicit connect() call.
            try:
                _st = self.status()
                _connected = bool(_st and _st.get('running'))
            except Exception:
                _connected = False
            if not _connected:
                try:
                    self.connect()
                except Exception:
                    pass
            chunks = []
            with self.rtt(channel=channel, search_addr=search_addr,
                          search_size=search_size, chunk_size=chunk_size) as rtt:
                if settle and settle > 0:
                    _time.sleep(float(settle))
                deadline = _time.time() + max(0.0, float(timeout))
                while _time.time() < deadline:
                    remaining = deadline - _time.time()
                    data = rtt.read_some(timeout=min(0.5, max(0.05, remaining)))
                    if data:
                        chunks.append(data)
                        # max_chars counts decoded characters, not raw bytes —
                        # decode-and-measure so multi-byte (UTF-8) output isn't
                        # cut short. Cheap: max_chars is rarely set.
                        if max_chars is not None and \
                                len(b"".join(chunks).decode(encoding, errors='replace')) >= max_chars:
                            break
                    else:
                        _time.sleep(0.02)
            text = b"".join(chunks).decode(encoding, errors='replace')
            if max_chars is not None and len(text) > max_chars:
                text = text[:max_chars]
            return text

    def make_debug(name, net_info=None):  # type: ignore
        """Factory function to create a DebugNet instance."""
        return DebugNet(name, net_info or {})

    _debug_available = True

except Exception:  # fallback no-op if debug module not available
    def make_debug(name, net_info=None):  # type: ignore
        """Factory function to create a fallback _NullDebug instance."""
        return _NullDebug(name, net_info)

    _debug_available = False


# Re-export DebugNet for type hints (use _NullDebug as fallback type)
if not _debug_available:
    DebugNet = _NullDebug  # type: ignore


__all__ = ['DebugNet', '_NullDebug', 'make_debug', '_debug_available']

# Copyright 2024-2026 Lager Data
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
        start_openocd_gdbserver,
        stop_openocd,
        get_openocd_status,
        OpenOcdRpc,
        OpenOcdRpcError,
    )
    from ..debug.probes import (
        resolve_serial_from_net,
        resolve_backend,
        gdb_port_for_slot,
        rtt_port_for_slot,
        openocd_telnet_port_for_slot,
        openocd_tcl_port_for_slot,
        parse_device_field,
        BACKEND_JLINK,
        BACKEND_OPENOCD,
    )

    class _OpenOcdRtt:
        """RTT context manager for OpenOCD probes — mirrors ``lager.debug.RTT``.

        OpenOCD's ``rtt server start <port> <channel>`` binds a TCP listener
        that emits raw RTT bytes (no SEGGER banner), so the read/write
        interface here is identical to the J-Link RTT class — just with the
        OpenOCD-specific setup at __enter__.
        """

        def __init__(self, *, channel, rtt_telnet_port, tcl_port,
                     search_addr=None, search_size=None):
            """Construct an OpenOCD RTT session.

            Note: the J-Link ``RTT`` class accepts a ``chunk_size`` knob
            that bounds each ``read_some`` call. OpenOCD's ``rtt server``
            streams continuously over TCP and we read whatever
            ``socket.recv(4096)`` returns, so ``chunk_size`` has no
            equivalent here. ``DebugNet.rtt()`` silently drops the
            argument when dispatching to this backend.
            """
            self.channel = channel
            self._port = rtt_telnet_port + channel
            self._tcl_port = tcl_port
            self._search_addr = search_addr if search_addr is not None else 0x20000000
            self._search_size = search_size if search_size is not None else 0x10000
            self._socket = None

        def __enter__(self):
            import socket
            import time as _time

            rpc = OpenOcdRpc(port=self._tcl_port)
            try:
                rpc.rtt_setup(search_addr=self._search_addr, search_size=self._search_size)
                rpc.rtt_start()
                try:
                    rpc.rtt_server_stop(self._port)
                except OpenOcdRpcError:
                    pass  # Server wasn't running yet — fine.
                rpc.rtt_server_start(self._port, channel=self.channel)
            except OpenOcdRpcError as exc:
                raise RuntimeError(f'OpenOCD RTT setup failed: {exc}') from exc

            # Wait a little for the listener to come up before connecting.
            last_err = None
            for _ in range(10):
                try:
                    self._socket = socket.create_connection(('127.0.0.1', self._port), timeout=2.0)
                    return self
                except OSError as exc:
                    last_err = exc
                    _time.sleep(0.2)
            raise RuntimeError(
                f'Failed to connect to OpenOCD RTT port {self._port}: {last_err}'
            )

        def __exit__(self, exc_type, exc_val, exc_tb):
            if self._socket:
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None
            return False

        def read_some(self, timeout=1.0):
            import select
            if not self._socket:
                raise RuntimeError('RTT not connected')
            ready = select.select([self._socket], [], [], timeout)
            if not ready[0]:
                return None
            try:
                data = self._socket.recv(4096)
                return data if data else None
            except OSError:
                return None

        def write(self, data):
            if not self._socket:
                raise RuntimeError('RTT not connected')
            if isinstance(data, str):
                data = data.encode('utf-8')
            return self._socket.send(data)

    class DebugNet:
        """Wrapper for debug operations via Net API.

        Dispatches each operation to either the J-Link or OpenOCD backend
        depending on the probe's USB VID. The public surface is identical
        across backends so scripts written against ``Net.get(..., NetType.Debug)``
        keep working when you swap a J-Link probe for an ST-Link.
        """

        def __init__(self, name, net_info):
            self.name = name
            self.type = NetType.Debug
            self._net_info = net_info
            instrument = net_info.get('instrument', '').lower()
            device = net_info.get('channel') or net_info.get('pin')
            if not device:
                raise ValueError(
                    f"Debug net '{name}' has no device type configured. "
                    f"Please specify a target device (e.g., 'NRF52840_XXAA', 'STM32F446RE') "
                    f"when creating the debug net."
                )
            # ``device`` may carry an ``@<channel>`` suffix for multi-channel
            # FTDI adapters (e.g. ``STM32F4x@A``). Split so the target name
            # passed to OpenOCD's target.cfg lookup is clean, and remember the
            # channel for OpenOCD's ``ftdi channel`` command. The suffix is
            # ignored on non-FTDI backends — J-Link and ST-Link probes ignore
            # the parsed channel.
            parsed_device, parsed_channel = parse_device_field(device)
            self.device = parsed_device
            # Net-level ``probe_channel`` explicitly overrides the @suffix.
            explicit_channel = net_info.get('probe_channel')
            if explicit_channel is None:
                self.probe_channel = parsed_channel
            else:
                try:
                    self.probe_channel = int(explicit_channel)
                except (TypeError, ValueError):
                    self.probe_channel = parsed_channel
            self.speed = '4000'  # default speed
            self.transport = 'SWD'  # default transport
            self.backend = resolve_backend(net_info)
            self.serial = resolve_serial_from_net(net_info)
            # Slot 0 is the legacy/single-probe path; the *box service* may
            # promote this probe to a higher slot via the shared NetsCache
            # allocator, but the Python API path (used inside tests running
            # on the box) sticks with slot 0 to preserve existing behaviour.
            self.slot = 0
            self.gdb_port = gdb_port_for_slot(self.slot)
            self.rtt_telnet_port = rtt_port_for_slot(self.slot)
            self.openocd_telnet_port = openocd_telnet_port_for_slot(self.slot)
            self.openocd_tcl_port = openocd_tcl_port_for_slot(self.slot)
            self._openocd_config_path = net_info.get('openocd_config_path')

        # ---- OpenOCD helpers (in-process Python API) ------------------------

        def _openocd_rpc(self, timeout=10.0):
            return OpenOcdRpc(port=self.openocd_tcl_port, timeout=timeout)

        def _ensure_openocd_running(self):
            if not get_openocd_status(serial=self.serial)['running']:
                raise RuntimeError(
                    f"OpenOCD is not running for debug net '{self.name}'. Call connect() first."
                )

        # ---- Public API -----------------------------------------------------

        def connect(self, speed=None, transport=None):
            """Start the gdbserver for this probe (backend chosen automatically)."""
            speed = speed or self.speed
            transport = transport or self.transport
            if self.backend == BACKEND_OPENOCD:
                return start_openocd_gdbserver(
                    device=self.device,
                    address=self._net_info.get('address'),
                    speed=speed,
                    transport=transport,
                    halt=False,
                    gdb_port=self.gdb_port,
                    telnet_port=self.openocd_telnet_port,
                    tcl_port=self.openocd_tcl_port,
                    rtt_telnet_port=self.rtt_telnet_port,
                    serial=self.serial,
                    openocd_config=self._openocd_config_path,
                    probe_channel=self.probe_channel,
                )
            return connect_jlink(
                speed=speed,
                device=self.device,
                transport=transport,
                serial=self.serial,
                gdb_port=self.gdb_port,
                rtt_telnet_port=self.rtt_telnet_port,
            )

        def disconnect(self):
            """Stop the gdbserver for this probe."""
            if self.backend == BACKEND_OPENOCD:
                return stop_openocd(serial=self.serial, tcl_port=self.openocd_tcl_port)
            return disconnect(serial=self.serial, gdb_port=self.gdb_port)

        def reset(self, halt=False):
            """Reset the device — same return shape (newline-joined output) for both backends."""
            if self.backend == BACKEND_OPENOCD:
                self._ensure_openocd_running()
                return self._openocd_rpc().reset(halt=halt) or ''
            results = []
            for line in reset_device(halt=halt, serial=self.serial, gdb_port=self.gdb_port):
                results.append(line)
            return '\n'.join(results)

        def flash(self, firmware_path):
            """Flash firmware to device. Returns combined output as a string."""
            import os
            ext = os.path.splitext(firmware_path)[1].lower()
            if self.backend == BACKEND_OPENOCD:
                self._ensure_openocd_running()
                address = None
                if ext == '.bin':
                    address = 0
                elif ext not in ('.hex', '.elf'):
                    raise ValueError(f"Unsupported firmware file type: {ext}")
                return self._openocd_rpc(timeout=300).program(
                    firmware_path, verify=True, reset_after=True, address=address,
                ) or ''
            if ext == '.hex':
                files = ([firmware_path], [], [])
            elif ext == '.bin':
                files = ([], [(firmware_path, 0x00000000)], [])
            elif ext == '.elf':
                files = ([], [], [firmware_path])
            else:
                raise ValueError(f"Unsupported firmware file type: {ext}")

            results = []
            for line in flash_device(
                files, mcu=self.device, serial=self.serial,
                gdb_port=self.gdb_port, rtt_telnet_port=self.rtt_telnet_port,
            ):
                results.append(line)
            return '\n'.join(results)

        def erase(self):
            """Erase flash (whole chip on most targets)."""
            if self.backend == BACKEND_OPENOCD:
                self._ensure_openocd_running()
                return self._openocd_rpc(timeout=120).flash_erase_all() or ''
            results = []
            for line in chip_erase(
                device=self.device, speed=self.speed, transport=self.transport,
                serial=self.serial,
            ):
                results.append(line)
            return '\n'.join(results)

        def read_memory(self, address, length):
            """Read memory from target device."""
            if self.backend == BACKEND_OPENOCD:
                self._ensure_openocd_running()
                return self._openocd_rpc(timeout=30).read_memory(address, length)
            return debug_read_memory(
                address, length, mcu=self.device,
                serial=self.serial, gdb_port=self.gdb_port,
            )

        def status(self):
            """Get connection status."""
            if self.backend == BACKEND_OPENOCD:
                return get_openocd_status(serial=self.serial)
            status = get_jlink_status(serial=self.serial, gdb_port=self.gdb_port)
            gdbserver_status = get_jlink_gdbserver_status(serial=self.serial)
            if gdbserver_status['running'] and not status['running']:
                return gdbserver_status
            return status

        def rtt(self, channel=0, search_addr=None, search_size=None, chunk_size=None):
            """Open an RTT (Real-Time Transfer) session.

            Returns a context manager exposing ``read_some(timeout=...)``
            and ``write(data)``. The OpenOCD and J-Link implementations
            behave identically from the caller's perspective.
            """
            if self.backend == BACKEND_OPENOCD:
                return _OpenOcdRtt(
                    channel=channel,
                    rtt_telnet_port=self.rtt_telnet_port,
                    tcl_port=self.openocd_tcl_port,
                    search_addr=search_addr,
                    search_size=search_size,
                )
            return RTT(
                device=self.device, channel=channel,
                search_addr=search_addr, search_size=search_size, chunk_size=chunk_size,
                serial=self.serial, rtt_telnet_port=self.rtt_telnet_port,
                gdb_port=self.gdb_port,
            )

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

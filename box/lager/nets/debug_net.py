# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Debug net classes for embedded device debugging via Net API.

This module provides the DebugNet class for debug operations and a fallback
_NullDebug class when the debug module is not available.
"""
from __future__ import annotations

from .constants import NetType


# Shared temp paths the HTTP debug service writes user scripts to.
# DebugNet writes the same files on the in-box Python API path so
# downstream J-Link helpers (``api._get_script_file``) and OpenOCD
# (``-f /tmp/lager_openocd_user.cfg``) pick them up without changes.
# Keep in lockstep with the constants in ``box/lager/debug/service.py``.
_JLINK_SCRIPT_TEMP_PATH = '/tmp/lager_jlink_script.JLinkScript'
_OPENOCD_CONFIG_TEMP_PATH = '/tmp/lager_openocd_user.cfg'

_SHARED_PATH_FOR_SUFFIX = {
    '.JLinkScript': _JLINK_SCRIPT_TEMP_PATH,
    '.cfg':         _OPENOCD_CONFIG_TEMP_PATH,
}


def materialise_user_script(net_info, *, explicit_key, b64_key, suffix):
    """Decode an inline base64 script/cfg to the shared temp path.

    Resolution order:

    1. An explicit ``*_path`` field on the net record — the file is already
       on the box, just use it.
    2. A base64 blob under *b64_key* — decode and write to the shared temp
       path for *suffix*.
    3. None — no user override; the backend uses its built-in defaults.

    We use the shared paths (not per-net) for two reasons:

    * J-Link's ``reset_device`` / ``read_memory`` internally call
      ``api._get_script_file()`` which *only* checks the shared path; a
      per-net path wouldn't be picked up.
    * OpenOCD reads the cfg once at daemon startup, so subsequent writes
      to the shared path don't affect an already-running daemon.

    Caveat: two debug nets with different scripts that connect concurrently
    will clobber the shared path. In practice the in-box Python API is used
    sequentially against a single net per test, and the HTTP service path
    has the same limitation (it handles concurrency at the request boundary
    by rewriting the file on every endpoint).
    """
    import base64
    import os

    net = net_info or {}

    explicit = net.get(explicit_key)
    if isinstance(explicit, str) and explicit and os.path.exists(explicit):
        return explicit

    encoded = net.get(b64_key)
    if not isinstance(encoded, str) or not encoded.strip():
        return None

    shared_path = _SHARED_PATH_FOR_SUFFIX[suffix]
    try:
        with open(shared_path, 'wb') as f:
            f.write(base64.b64decode(encoded))
        return shared_path
    except Exception:  # noqa: BLE001 — surface as "no user cfg"
        return None


def allocate_probe_slot(serial, get_nets_cache_fn=None, parse_probe_serial_fn=None,
                        compute_slot_fn=None):
    """Pick a probe's slot index using the shared NetsCache.

    Mirrors ``service._resolve_probe``: probes share the slot pool keyed by
    USB serial so concurrent debug nets on the same box get distinct
    GDB/TCL/RTT port windows.

    Returns 0 when any of the following hold (in priority order):

    * ``serial`` is falsy (legacy single-probe path),
    * ``lager.cache`` isn't importable (tests / minimal envs),
    * any unexpected error happens while walking the cache (defensive —
      a slot-allocator failure must never block a debug session).

    The injectable ``*_fn`` arguments exist so tests can substitute their
    own implementations without monkeypatching globals.
    """
    if not serial:
        return 0
    if get_nets_cache_fn is None:
        try:
            from ..cache import get_nets_cache as get_nets_cache_fn  # type: ignore
        except Exception:
            return 0
    if parse_probe_serial_fn is None or compute_slot_fn is None:
        try:
            from ..debug.probes import (
                parse_probe_serial as _parse_probe_serial,
                compute_slot as _compute_slot,
            )
            parse_probe_serial_fn = parse_probe_serial_fn or _parse_probe_serial
            compute_slot_fn = compute_slot_fn or _compute_slot
        except Exception:
            return 0
    try:
        all_serials = []
        for n in get_nets_cache_fn().get_nets():
            if not isinstance(n, dict) or n.get('role') != 'debug':
                continue
            s = parse_probe_serial_fn(n.get('address'))
            if s:
                all_serials.append(s)
        return compute_slot_fn(serial, all_serials)
    except Exception:  # noqa: BLE001 — slot=0 is always safe
        return 0


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
        parse_probe_serial,
        compute_slot,
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
            # Slot allocation mirrors the HTTP service path
            # (``service._resolve_probe``): probes share the slot pool keyed
            # by USB serial so concurrent debug nets running on the same box
            # get distinct GDB/TCL/RTT port windows instead of colliding on
            # slot-0 ports. Falls back to slot 0 for nets without a parseable
            # serial or when the cache isn't reachable (preserves
            # single-probe legacy behaviour).
            self.slot = allocate_probe_slot(self.serial)
            self.gdb_port = gdb_port_for_slot(self.slot)
            self.rtt_telnet_port = rtt_port_for_slot(self.slot)
            self.openocd_telnet_port = openocd_telnet_port_for_slot(self.slot)
            self.openocd_tcl_port = openocd_tcl_port_for_slot(self.slot)
            # Materialise the user-supplied debug scripts to disk so the
            # underlying backends can ``-f`` / ``-JLinkScriptFile`` them. The
            # CLI stores them as base64-encoded content on the net record
            # (``openocd_config`` / ``jlink_script``); an explicit
            # ``*_path`` field wins when the file is already on the box.
            self._openocd_config_path = materialise_user_script(
                net_info,
                explicit_key='openocd_config_path',
                b64_key='openocd_config',
                suffix='.cfg',
            )
            self._jlink_script_path = materialise_user_script(
                net_info,
                explicit_key='jlink_script_path',
                b64_key='jlink_script',
                suffix='.JLinkScript',
            )

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
                script_file=self._jlink_script_path,
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

        def flash(self, firmware_path, flash_address=None):
            """Flash firmware to device. Returns combined output as a string.

            ``.hex`` and ``.elf`` files carry their own load addresses in the
            file format and the ``flash_address`` argument is ignored for
            them. ``.bin`` files have no embedded address, so callers MUST
            pass ``flash_address`` — silently defaulting to ``0x0`` writes
            to the wrong location on every Cortex-M part whose flash base
            isn't aliased to 0 (STM32: ``0x08000000``, nRF52/53:
            ``0x00000000`` *is* flash but only by coincidence). Pass the
            target's flash base explicitly to avoid foot-guns.
            """
            import os
            ext = os.path.splitext(firmware_path)[1].lower()
            if ext not in ('.hex', '.bin', '.elf'):
                raise ValueError(f"Unsupported firmware file type: {ext}")
            if ext == '.bin' and flash_address is None:
                raise ValueError(
                    f"Flashing a .bin file requires an explicit "
                    f"flash_address (e.g. 0x08000000 for STM32). "
                    f"Pass flash_address=... to flash()."
                )
            if self.backend == BACKEND_OPENOCD:
                self._ensure_openocd_running()
                # OpenOCD's ``program`` reads the load address from the file
                # for .hex/.elf and uses the trailing address for .bin.
                address = flash_address if ext == '.bin' else None
                return self._openocd_rpc(timeout=300).program(
                    firmware_path, verify=True, reset_after=True, address=address,
                ) or ''
            if ext == '.hex':
                files = ([firmware_path], [], [])
            elif ext == '.bin':
                files = ([], [(firmware_path, flash_address)], [])
            else:  # .elf
                files = ([], [], [firmware_path])

            results = []
            for line in flash_device(
                files, mcu=self.device, serial=self.serial,
                gdb_port=self.gdb_port, rtt_telnet_port=self.rtt_telnet_port,
                script_file=self._jlink_script_path,
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
                serial=self.serial, script_file=self._jlink_script_path,
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


__all__ = [
    'DebugNet', '_NullDebug', 'make_debug', '_debug_available',
    'materialise_user_script', 'allocate_probe_slot',
]

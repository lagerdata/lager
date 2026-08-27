# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Lager Debug Service Client

Client library for communicating with the persistent debug service.

This is the CLI-side client: it runs on the user's machine and reaches the
box over the network, so it crosses the authenticating gateway and must
attach a bearer token (see :meth:`DebugServiceClient._request`).

``box/lager/debug/service_client.py`` is a separate, deliberately divergent
client for the same service. It runs ON the box against ``127.0.0.1``, is
therefore always on the far side of the gateway, and ``box/`` ships no
``gateway_auth`` module at all — importing one there would break every
on-box ``lager python`` script. Do not "sync" these two files.
"""
import json
import base64
import requests
from typing import Dict, Any, Optional
from pathlib import Path


class DebugServiceClient:
    """Client for interacting with lager-debug-service."""

    def __init__(self, box_host: str, service_port: int = 8765, ssh_tunnel: bool = True):
        """
        Initialize debug service client.

        Args:
            box_host: Lager Box IP address. MUST be the resolved IP, not a
                saved box name: the gateway auth store is keyed by IP, so a
                name here would silently resolve no token. Every CLI caller
                arrives via ``_get_service_client`` after
                ``_resolve_box_with_username``, which guarantees this.
            service_port: Service port (default: 8765)
            ssh_tunnel: If True, use SSH tunnel to reach service
        """
        self.box_host = box_host
        self.service_port = service_port
        self.ssh_tunnel = ssh_tunnel

        if ssh_tunnel:
            # Service is only accessible via SSH tunnel
            # CLI should establish tunnel: ssh -L 8765:127.0.0.1:8765 <username>@box
            # Username is determined from box storage (defaults to 'lagerdata' if not set)
            self.base_url = f'http://127.0.0.1:{service_port}'
        else:
            # Direct connection (only works if service binds to 0.0.0.0)
            self.base_url = f'http://{box_host}:{service_port}'

        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'lager-cli/1.0',
        })

    # NOTE: auth is attached per request in _request(), not here. This client
    # is long-lived — a `gdbserver --rtt` session issues its first request and
    # its last hours apart — so a token cached on the session at construction
    # would be replayed after expiry. See _request()'s docstring.

    def _request(self, method: str, path: str, *, timeout=None, stream=False,
                 **kwargs) -> requests.Response:
        """One round trip to the debug service, with gateway auth applied.

        On a box behind an authenticating gateway, the bearer token for
        ``self.box_host`` is attached to the FIRST attempt (contract §6.2)
        and a first-contact 401 is recorded and retried once in-call
        (§6.3). ``docs/reference/gateway-auth-contract.md`` §6.4 requires
        this for the debug service explicitly, streaming RTT included.

        Plain Lager boxes are unaffected: ``auth_headers_for_box`` returns
        ``{}`` and ``_check_gateway`` is a passthrough, so no token is
        stored, sent, or looked up.

        The token is resolved per call and never cached on the session.
        ``auth_headers_for_box`` refreshes a near-expiry token only when it
        is called, and this client is long-lived — a ``gdbserver --rtt``
        session issues its first request and its last hours apart. Caching
        the header at construction would replay an expired token.

        Known limit: a single request that outlives its token cannot be
        re-authenticated mid-flight, so a multi-hour RTT stream may end on
        an expired token. The contract does not require mid-stream refresh;
        fixing it would mean chunked reconnection.
        """
        from ....gateway_auth import auth_headers_for_box
        from ....box_storage import _check_gateway
        from ....errors import LagerError

        headers = dict(kwargs.pop('headers', None) or {})
        headers.update(auth_headers_for_box(self.box_host))

        response = self.session.request(
            method, f'{self.base_url}{path}',
            headers=headers, timeout=timeout, stream=stream, **kwargs,
        )
        try:
            # timeout/stream/session mirror the call above: the retry re-sends
            # this exact prepared request, and replaying an RTT POST with
            # stream=False would block forever buffering an endless body.
            response = _check_gateway(response, self.box_host, timeout=timeout,
                                      stream=stream, session=self.session)
        except LagerError as denial:
            # Every debug subcommand wraps its client calls in a broad
            # `except Exception`, which would flatten the styled "run
            # `lager login`" block into one bare line. die() exits via
            # SystemExit, which those handlers do not catch (cli/errors.py).
            denial.die()
        response.raise_for_status()
        return response

    def health_check(self) -> Dict[str, Any]:
        """Check if service is healthy."""
        return self._request('GET', '/health', timeout=5).json()

    def get_status(self) -> Dict[str, Any]:
        """Get debug status."""
        return self._request('GET', '/status', timeout=5).json()

    def connect(self, net: Dict[str, Any], speed: Optional[str] = None,
                force: bool = False, halt: bool = False, gdb: bool = False,
                gdb_port: Optional[int] = None,
                jlink_script: Optional[str] = None,
                openocd_config: Optional[str] = None) -> Dict[str, Any]:
        """
        Connect to debugger.

        Args:
            net: Debug net configuration
            speed: SWD/JTAG speed (e.g., '4000', 'adaptive')
            force: Force new connection (default: False for connection reuse)
            halt: Halt device after connect
            gdb: Start GDB server (default: False)
            gdb_port: GDB server port. None lets the box pick from the probe's
                slot (2331, 2334, 2337, 2340 for slots 0-3). Pass an integer
                only when you specifically need to override.
            jlink_script: Base64-encoded J-Link script file content. The box
                uses this when the probe resolves to the J-Link backend
                (VID 0x1366). Ignored for OpenOCD-backed probes.
            openocd_config: Base64-encoded OpenOCD ``.cfg``/``.tcl`` content.
                The box loads it via ``-f <user.cfg>`` *after* the built-in
                interface + target configs, so it can override transport,
                halt mode, etc. Used only for OpenOCD-backed probes
                (ST-Link, CMSIS-DAP, FTDI).

        Returns:
            Connection status dict — includes ``backend`` (``jlink`` or
            ``openocd``) so callers can branch on which gdbserver type
            actually started.
        """
        data = {
            'net': net,
            'speed': speed or 'adaptive',
            'force': force,
            'halt': halt,
            'gdb': gdb,
        }
        if gdb_port is not None:
            # Only forward an explicit override; otherwise the box uses its
            # per-slot allocator.
            data['gdb_port'] = gdb_port

        if jlink_script:
            data['jlink_script'] = jlink_script
        if openocd_config:
            data['openocd_config'] = openocd_config

        return self._request('POST', '/debug/connect', json=data, timeout=30).json()

    def disconnect(self, net: Dict[str, Any], keep_jlink_running: bool = False) -> Dict[str, Any]:
        """
        Disconnect from debugger.

        Args:
            net: Debug net configuration
            keep_jlink_running: If True, only disconnect GDB client but leave J-Link running
        """
        data = {
            'net': net,
            'keep_jlink_running': keep_jlink_running
        }

        return self._request('POST', '/debug/disconnect', json=data, timeout=10).json()

    def reset(self, net: Dict[str, Any], halt: bool = False) -> Dict[str, Any]:
        """Reset target device."""
        data = {
            'net': net,
            'halt': halt
        }

        return self._request('POST', '/debug/reset', json=data, timeout=10).json()

    def flash(self, firmware_file: Path, file_type: str = 'hex',
              address: Optional[int] = None, verbose: bool = False, net: Optional[Dict[str, Any]] = None,
              jlink_script: Optional[str] = None, openocd_config: Optional[str] = None) -> Dict[str, Any]:
        """Flash firmware to target.

        `jlink_script` / `openocd_config` are sent for the same reason
        `connect` sends them: without them the box resolves the script itself,
        and its last resort is whatever this net wrote on a previous connect --
        which may be older than the caller thinks. Sending it makes the flash
        use the script this invocation actually resolved.
        """
        # Read and base64-encode file content
        with open(firmware_file, 'rb') as f:
            content = base64.b64encode(f.read()).decode('ascii')

        data = {'verbose': verbose}
        if net:
            data['net'] = net
        if jlink_script:
            data['jlink_script'] = jlink_script
        if openocd_config:
            data['openocd_config'] = openocd_config
        if file_type == 'hex':
            data['hexfile'] = {'content': content}
        elif file_type == 'elf':
            data['elffile'] = {'content': content}
        elif file_type == 'bin':
            data['binfile'] = {
                'content': content,
                'address': address or 0x08000000,
            }
        else:
            raise ValueError(f"Unknown file type: {file_type}")

        return self._request(
            'POST', '/debug/flash',
            json=data,
            timeout=180,  # Flash can take a while
        ).json()

    def erase(self, net: Dict[str, Any], speed: str = '4000',
              transport: str = 'SWD') -> Dict[str, Any]:
        """Erase flash memory."""
        data = {
            'net': net,
            'speed': speed,
            'transport': transport,
        }

        return self._request(
            'POST', '/debug/erase',
            json=data,
            timeout=120,  # Erase can take a while
        ).json()

    def read_memory(self, net: Dict[str, Any], start_addr: int,
                    length: int = 256, no_reset: bool = False) -> bytes:
        """Read memory from target.

        no_reset: DA1469x only — skip the box-side reset+halt before the read.
        """
        data = {
            'net': net,
            'start_addr': start_addr,
            'length': length,
        }
        if no_reset:
            data['no_reset'] = True

        result = self._request(
            'POST', '/debug/memrd',
            json=data,
            timeout=30,  # Increased timeout for GDB memory reads
        ).json()
        hex_data = result['data']
        return bytes.fromhex(hex_data)

    def get_info(self, net: Dict[str, Any], probe: bool = False) -> Dict[str, Any]:
        """Get debug net information.

        `probe` carries the same meaning and cost as in `get_debug_status`.
        """
        data = {'net': net, 'probe': bool(probe)}

        timeout = 20 if probe else 5
        return self._request('POST', '/debug/info', json=data, timeout=timeout).json()

    def get_debug_status(self, net: Optional[Dict[str, Any]] = None, probe: bool = False) -> Dict[str, Any]:
        """Get debugger status for `net`'s probe.

        `net` is what makes the answer trustworthy, and omitting it is not a
        harmless default. The box resolves the probe serial from the net and
        checks `/tmp/jlink_gdbserver_<serial>.pid`; with no net it falls back
        to the legacy un-suffixed pidfile, which a serial-aware box never
        writes. The status then comes back `connected: False` while a
        gdbserver is very much running -- and callers act on that by tearing
        down the session and reconnecting, which is what wedged the probe.

        `probe` asks the box to read the target rather than only its own
        pidfile and logfile. That costs a GDB round trip, so it is opt-in: the
        callers that gate an operation on attachment ask for it, and the ones
        that only want to know whether a server is up do not. The timeout
        widens with it, because the cheap path must not inherit the cost.
        """
        payload = {'net': net or {}, 'probe': bool(probe)}
        timeout = 20 if probe else 5
        return self._request('POST', '/debug/status', json=payload, timeout=timeout).json()

    def get_service_health(self, detailed: bool = False) -> Dict[str, Any]:
        """Get service health information."""
        endpoint = '/health/detailed' if detailed else '/health'
        return self._request('GET', endpoint, timeout=5).json()

    def rtt(self, net: Optional[Dict[str, Any]] = None, channel: int = 0, timeout: Optional[int] = None,
            search_addr: Optional[int] = None, search_size: Optional[int] = None, chunk_size: Optional[int] = None):
        """
        Stream RTT logs from target.

        Args:
            net: Debug net configuration
            channel: RTT channel (0 or 1)
            timeout: Timeout in seconds (None = stream until interrupted)
            search_addr: RAM start address for RTT control block search
            search_size: Size of RAM region to search in bytes
            chunk_size: Size of each read chunk in bytes

        Yields:
            Bytes from RTT stream
        """
        data = {
            'net': net,
            'channel': channel,
            'timeout': timeout,
        }
        if search_addr is not None:
            data['search_addr'] = search_addr
        if search_size is not None:
            data['search_size'] = search_size
        if chunk_size is not None:
            data['chunk_size'] = chunk_size

        # Use streaming response to handle chunked transfer encoding
        response = self._request(
            'POST', '/debug/rtt',
            json=data,
            timeout=None,  # No timeout - stream until done
            stream=True,   # Enable streaming mode
        )

        # Stream chunks as they arrive
        for chunk in response.iter_content(chunk_size=4096):
            if chunk:
                yield chunk

    def close(self):
        """Close client session."""
        self.session.close()

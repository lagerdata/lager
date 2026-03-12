# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
Lager Debug Service Client

Client library for communicating with the persistent debug service.
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
            box_host: Lagerbox IP address
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

    def health_check(self) -> Dict[str, Any]:
        """Check if service is healthy."""
        response = self.session.get(f'{self.base_url}/health', timeout=5)
        response.raise_for_status()
        return response.json()

    def get_status(self) -> Dict[str, Any]:
        """Get debug status."""
        response = self.session.get(f'{self.base_url}/status', timeout=5)
        response.raise_for_status()
        return response.json()

    def connect(self, net: Dict[str, Any], speed: Optional[str] = None,
                force: bool = False, halt: bool = False, gdb: bool = False,
                gdb_port: int = 2331, jlink_script: Optional[str] = None) -> Dict[str, Any]:
        """
        Connect to debugger.

        Args:
            net: Debug net configuration
            speed: SWD/JTAG speed (e.g., '4000', 'adaptive')
            force: Force new connection (default: False for connection reuse)
            halt: Halt device after connect
            gdb: Start GDB server (default: False)
            gdb_port: GDB server port (default: 2331)
            jlink_script: Base64-encoded J-Link script file content (optional)

        Returns:
            Connection status dict
        """
        data = {
            'net': net,
            'speed': speed or 'adaptive',
            'force': force,
            'halt': halt,
            'gdb': gdb,
            'gdb_port': gdb_port,
        }

        if jlink_script:
            data['jlink_script'] = jlink_script

        response = self.session.post(
            f'{self.base_url}/debug/connect',
            json=data,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

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

        response = self.session.post(
            f'{self.base_url}/debug/disconnect',
            json=data,
            timeout=10
        )
        response.raise_for_status()
        return response.json()

    def reset(self, net: Dict[str, Any], halt: bool = False) -> Dict[str, Any]:
        """Reset target device."""
        data = {
            'net': net,
            'halt': halt
        }

        response = self.session.post(
            f'{self.base_url}/debug/reset',
            json=data,
            timeout=10
        )
        response.raise_for_status()
        return response.json()

    def flash(self, firmware_file: Path, file_type: str = 'hex',
              address: Optional[int] = None, verbose: bool = False, net: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Flash firmware to target."""
        # Read and base64-encode file content
        with open(firmware_file, 'rb') as f:
            content = base64.b64encode(f.read()).decode('ascii')

        data = {'verbose': verbose}
        if net:
            data['net'] = net
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

        response = self.session.post(
            f'{self.base_url}/debug/flash',
            json=data,
            timeout=180  # Flash can take a while
        )
        response.raise_for_status()
        return response.json()

    def erase(self, net: Dict[str, Any], speed: str = '4000',
              transport: str = 'SWD') -> Dict[str, Any]:
        """Erase flash memory."""
        data = {
            'net': net,
            'speed': speed,
            'transport': transport,
        }

        response = self.session.post(
            f'{self.base_url}/debug/erase',
            json=data,
            timeout=120  # Erase can take a while
        )
        response.raise_for_status()
        return response.json()

    def read_memory(self, net: Dict[str, Any], start_addr: int,
                    length: int = 256) -> bytes:
        """Read memory from target."""
        data = {
            'net': net,
            'start_addr': start_addr,
            'length': length,
        }

        response = self.session.post(
            f'{self.base_url}/debug/memrd',
            json=data,
            timeout=30  # Increased timeout for GDB memory reads
        )
        response.raise_for_status()

        result = response.json()
        hex_data = result['data']
        return bytes.fromhex(hex_data)

    def get_info(self, net: Dict[str, Any]) -> Dict[str, Any]:
        """Get debug net information."""
        data = {'net': net}

        response = self.session.post(
            f'{self.base_url}/debug/info',
            json=data,
            timeout=5
        )
        response.raise_for_status()
        return response.json()

    def get_debug_status(self) -> Dict[str, Any]:
        """Get debugger status."""
        response = self.session.post(
            f'{self.base_url}/debug/status',
            json={},
            timeout=5
        )
        response.raise_for_status()
        return response.json()

    def get_service_health(self, detailed: bool = False) -> Dict[str, Any]:
        """Get service health information."""
        endpoint = '/health/detailed' if detailed else '/health'
        response = self.session.get(
            f'{self.base_url}{endpoint}',
            timeout=5
        )
        response.raise_for_status()
        return response.json()

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
        response = self.session.post(
            f'{self.base_url}/debug/rtt',
            json=data,
            timeout=None,  # No timeout - stream until done
            stream=True  # Enable streaming mode
        )
        response.raise_for_status()

        # Stream chunks as they arrive
        for chunk in response.iter_content(chunk_size=4096):
            if chunk:
                yield chunk

    def close(self):
        """Close client session."""
        self.session.close()

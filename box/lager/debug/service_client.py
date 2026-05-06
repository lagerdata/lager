# Copyright 2024-2026 Lager Data
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
            box_host: Box IP address
            service_port: Service port (default: 8765)
            ssh_tunnel: If True, use SSH tunnel to reach service
        """
        self.box_host = box_host
        self.service_port = service_port
        self.ssh_tunnel = ssh_tunnel

        if ssh_tunnel:
            # Service is only accessible via SSH tunnel
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
                force: bool = True, halt: bool = False) -> Dict[str, Any]:
        """Connect to debugger."""
        data = {
            'net': net,
            'speed': speed or 'adaptive',
            'force': force,
            'halt': halt,
        }

        response = self.session.post(
            f'{self.base_url}/debug/connect',
            json=data,
            timeout=30
        )
        response.raise_for_status()
        return response.json()

    def disconnect(self, net: Dict[str, Any]) -> Dict[str, Any]:
        """Disconnect from debugger."""
        data = {'net': net}

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
              address: Optional[int] = None) -> Dict[str, Any]:
        """Flash firmware to target."""
        # Read and base64-encode file content
        with open(firmware_file, 'rb') as f:
            content = base64.b64encode(f.read()).decode('ascii')

        data = {}
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
            timeout=10
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

    def close(self):
        """Close client session."""
        self.session.close()

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MikroTik RouterOS driver

Communicates with MikroTik routers via the RouterOS REST API (RouterOS v7.x+).
Uses the `requests` library which is already available on the box.

REST API base: http://<ip>/rest
Docs: https://help.mikrotik.com/docs/display/ROS/REST+API
"""

import requests
from requests.auth import HTTPBasicAuth


class MikroTikRouter:
    """Driver for MikroTik routers via the RouterOS REST API."""

    def __init__(self, name, address, username='admin', password='', use_ssl=False):
        self._name = name
        self._address = address
        self._username = username
        self._password = password
        self._use_ssl = use_ssl
        scheme = 'https' if use_ssl else 'http'
        self._base_url = f'{scheme}://{address}/rest'
        self._auth = HTTPBasicAuth(username, password)
        self._session = requests.Session()
        self._session.auth = self._auth
        if use_ssl:
            self._session.verify = False

    def _get(self, path, params=None):
        url = f'{self._base_url}/{path.lstrip("/")}'
        resp = self._session.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, data=None):
        url = f'{self._base_url}/{path.lstrip("/")}'
        resp = self._session.post(url, json=data or {}, timeout=10)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def connect(self):
        """Verify connectivity by fetching system identity."""
        identity = self._get('/system/identity')
        resource = self._get('/system/resource')
        return {
            'connected': True,
            'identity': identity.get('name', ''),
            'version': resource.get('version', ''),
            'board': resource.get('board-name', ''),
            'uptime': resource.get('uptime', ''),
        }

    def get_interfaces(self):
        """List all network interfaces."""
        interfaces = self._get('/interface')
        return [
            {
                'name': iface.get('name'),
                'type': iface.get('type'),
                'running': iface.get('running') == 'true',
                'disabled': iface.get('disabled') == 'true',
                'mac_address': iface.get('mac-address'),
                'tx_byte': iface.get('tx-byte'),
                'rx_byte': iface.get('rx-byte'),
            }
            for iface in interfaces
        ]

    def get_wireless_clients(self):
        """List currently connected wireless clients."""
        clients = self._get('/interface/wireless/registration-table')
        return [
            {
                'interface': client.get('interface'),
                'mac_address': client.get('mac-address'),
                'signal_strength': client.get('signal-strength'),
                'tx_rate': client.get('tx-rate'),
                'rx_rate': client.get('rx-rate'),
                'uptime': client.get('uptime'),
                'last_ip': client.get('last-ip'),
            }
            for client in clients
        ]

    def get_wireless_interfaces(self):
        """List wireless interfaces and their configuration."""
        interfaces = self._get('/interface/wireless')
        return [
            {
                'name': iface.get('name'),
                'ssid': iface.get('ssid'),
                'band': iface.get('band'),
                'frequency': iface.get('frequency'),
                'channel_width': iface.get('channel-width'),
                'security_profile': iface.get('security-profile'),
                'disabled': iface.get('disabled') == 'true',
                'running': iface.get('running') == 'true',
            }
            for iface in interfaces
        ]

    def set_wireless_ssid(self, interface, ssid):
        """Change the SSID of a wireless interface."""
        interfaces = self._get('/interface/wireless')
        target = next((i for i in interfaces if i.get('name') == interface), None)
        if not target:
            raise ValueError(f"Wireless interface '{interface}' not found")
        iface_id = target['.id']
        self._post(f'/interface/wireless/{iface_id}', {'ssid': ssid})
        return {'interface': interface, 'ssid': ssid, 'updated': True}

    def set_interface_disabled(self, interface, disabled):
        """Enable or disable a network interface."""
        interfaces = self._get('/interface')
        target = next((i for i in interfaces if i.get('name') == interface), None)
        if not target:
            raise ValueError(f"Interface '{interface}' not found")
        iface_id = target['.id']
        self._post(f'/interface/{iface_id}', {'disabled': 'true' if disabled else 'false'})
        return {'interface': interface, 'disabled': disabled}

    def get_dhcp_leases(self):
        """List DHCP leases (clients that have received IP addresses)."""
        leases = self._get('/ip/dhcp-server/lease')
        return [
            {
                'address': lease.get('address'),
                'mac_address': lease.get('mac-address'),
                'hostname': lease.get('host-name'),
                'status': lease.get('status'),
                'expires_after': lease.get('expires-after'),
            }
            for lease in leases
        ]

    def get_system_info(self):
        """Get system resource information."""
        resource = self._get('/system/resource')
        identity = self._get('/system/identity')
        return {
            'name': identity.get('name'),
            'version': resource.get('version'),
            'board': resource.get('board-name'),
            'architecture': resource.get('architecture-name'),
            'uptime': resource.get('uptime'),
            'cpu_load': resource.get('cpu-load'),
            'free_memory': resource.get('free-memory'),
            'total_memory': resource.get('total-memory'),
            'free_hdd_space': resource.get('free-hdd-space'),
        }

    def reboot(self):
        """Reboot the router."""
        try:
            self._post('/system/reboot')
        except Exception:
            pass  # Router will drop the connection during reboot
        return {'rebooting': True}

    def run(self, path, params=None):
        """Run an arbitrary REST API GET call. Path is relative to /rest."""
        return self._get(path, params=params)

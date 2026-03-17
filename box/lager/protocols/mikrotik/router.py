# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
MikroTik RouterOS driver

Communicates with MikroTik routers via the RouterOS REST API (RouterOS v7.x+).
Uses the `requests` library which is already available on the box.

REST API base: http://<ip>/rest
Docs: https://help.mikrotik.com/docs/display/ROS/REST+API
"""

import time
import requests
from requests.auth import HTTPBasicAuth


class MikroTikRouter:
    """Driver for MikroTik routers via the RouterOS REST API.

    Args:
        name: Logical name for this router (used in logging).
        address: IP address of the router.
        username: RouterOS username (default: 'admin').
        password: RouterOS password (default: empty string).
        use_ssl: Use HTTPS instead of HTTP (default: False).
        test_tag: Comment tag applied to all test-created firewall rules,
            queues, and access list entries. Used for bulk cleanup via
            reset_to_defaults(). (default: 'lager-test')
    """

    def __init__(self, name, address, username='admin', password='', use_ssl=False,
                 test_tag='lager-test'):
        self._name = name
        self._address = address
        self._username = username
        self._password = password
        self._use_ssl = use_ssl
        self._test_tag = test_tag
        scheme = 'https' if use_ssl else 'http'
        self._base_url = f'{scheme}://{address}/rest'
        self._auth = HTTPBasicAuth(username, password)
        self._session = requests.Session()
        self._session.auth = self._auth
        self._session.headers.update({'content-type': 'application/json'})
        if use_ssl:
            self._session.verify = False

    # ──────────────────────────────────────────────
    # HTTP helpers
    # ──────────────────────────────────────────────

    def _new_session(self):
        session = requests.Session()
        session.auth = self._auth
        session.headers.update({'content-type': 'application/json'})
        if self._use_ssl:
            session.verify = False
        return session

    def _request(self, method, path, **kwargs):
        """Execute an HTTP request against the RouterOS REST API.

        Retries up to 3 times with increasing backoff on connection errors.
        RouterOS closes idle TCP connections after ~20-30s; when that happens
        urllib3 raises ConnectionError on the next reuse attempt. Recreating
        the session establishes a fresh TCP connection and recovers cleanly.
        """
        url = f'{self._base_url}/{path.lstrip("/")}'
        kwargs.setdefault('timeout', 10)
        last_exc = None
        for attempt, delay in enumerate([0, 3, 6]):
            try:
                if delay:
                    self._session.close()
                    self._session = self._new_session()
                    time.sleep(delay)
                resp = getattr(self._session, method)(url, **kwargs)
                resp.raise_for_status()
                return resp
            except requests.exceptions.ConnectionError as e:
                last_exc = e
        raise last_exc

    def _get(self, path, params=None):
        return self._request('get', path, params=params).json()

    def _put(self, path, data=None):
        """Create a new entry (RouterOS: PUT = add)."""
        resp = self._request('put', path, json=data or {})
        return resp.json() if resp.content else {}

    def _patch(self, path, data=None):
        """Update an existing entry by ID (RouterOS: PATCH = set)."""
        resp = self._request('patch', path, json=data or {})
        return resp.json() if resp.content else {}

    def _delete(self, path):
        """Delete an entry by ID."""
        self._request('delete', path)

    def _post(self, path, data=None):
        """Execute a command (RouterOS: POST = action)."""
        resp = self._request('post', path, json=data or {})
        return resp.json() if resp.content else {}

    def _find_wireless_id(self, interface):
        """Return the .id for a wireless interface by name."""
        interfaces = self._get('/interface/wireless')
        for iface in interfaces:
            if iface.get('name') == interface or iface.get('default-name') == interface:
                return iface['.id']
        raise ValueError(f"Wireless interface '{interface}' not found")

    # ──────────────────────────────────────────────
    # System
    # ──────────────────────────────────────────────

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
            pass  # Router drops the connection during reboot
        return {'rebooting': True}

    def wait_for_ready(self, timeout=120):
        """Poll until the router is responsive after a reboot.

        Args:
            timeout: Maximum seconds to wait (default: 120).

        Raises:
            TimeoutError: If the router does not respond within timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self._get('/system/resource')
                return True
            except Exception:
                time.sleep(3)
        raise TimeoutError(f"Router did not come back online within {timeout}s")

    # ──────────────────────────────────────────────
    # Interfaces
    # ──────────────────────────────────────────────

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

    def set_interface_disabled(self, interface, disabled):
        """Enable or disable a network interface.

        Args:
            interface: Interface name (e.g., 'ether1', 'wlan1').
            disabled: True to disable, False to enable.
        """
        interfaces = self._get('/interface')
        target = next((i for i in interfaces if i.get('name') == interface), None)
        if not target:
            raise ValueError(f"Interface '{interface}' not found")
        self._patch(f'/interface/{target[".id"]}', {'disabled': 'true' if disabled else 'false'})
        return {'interface': interface, 'disabled': disabled}

    # ──────────────────────────────────────────────
    # Security Profiles
    # ──────────────────────────────────────────────

    def get_security_profiles(self):
        """List all wireless security profiles."""
        return self._get('/interface/wireless/security-profiles')

    def create_security_profile(self, name, mode='dynamic-keys',
                                authentication_types='wpa2-psk',
                                unicast_ciphers='aes-ccm',
                                wpa2_pre_shared_key='',
                                wpa_pre_shared_key='',
                                **kwargs):
        """Create a wireless security profile.

        Args:
            name: Profile name.
            mode: Security mode — 'dynamic-keys' (WPA/WPA2) or 'none' (open).
            authentication_types: Comma-separated auth types, e.g. 'wpa2-psk'
                or 'wpa-psk,wpa2-psk'.
            unicast_ciphers: Cipher suite, e.g. 'aes-ccm' or 'tkip,aes-ccm'.
            wpa2_pre_shared_key: WPA2 passphrase.
            wpa_pre_shared_key: WPA passphrase (for mixed-mode profiles).
            **kwargs: Any additional RouterOS security-profile properties.
        """
        data = {
            'name': name,
            'mode': mode,
            'authentication-types': authentication_types,
            'unicast-ciphers': unicast_ciphers,
        }
        if wpa2_pre_shared_key:
            data['wpa2-pre-shared-key'] = wpa2_pre_shared_key
        if wpa_pre_shared_key:
            data['wpa-pre-shared-key'] = wpa_pre_shared_key
        data.update(kwargs)
        return self._put('/interface/wireless/security-profiles', data)

    def create_open_security_profile(self, name='open'):
        """Create an open (no encryption) security profile.

        Args:
            name: Profile name (default: 'open').
        """
        return self._put('/interface/wireless/security-profiles', {
            'name': name,
            'mode': 'none',
        })

    def update_security_profile_password(self, name, new_password):
        """Update the WPA2 pre-shared key of an existing security profile.

        Args:
            name: Profile name.
            new_password: New WPA2 passphrase.

        Returns:
            True if the profile was found and updated, False if not found.
        """
        profiles = self.get_security_profiles()
        for p in profiles:
            if p.get('name') == name:
                self._patch(f'/interface/wireless/security-profiles/{p[".id"]}',
                            {'wpa2-pre-shared-key': new_password})
                return True
        return False

    def delete_security_profile(self, name):
        """Delete a security profile by name.

        Args:
            name: Profile name. The built-in 'default' profile cannot be deleted.

        Returns:
            True if deleted, False if not found.
        """
        profiles = self.get_security_profiles()
        for p in profiles:
            if p.get('name') == name:
                self._delete(f'/interface/wireless/security-profiles/{p[".id"]}')
                return True
        return False

    # ──────────────────────────────────────────────
    # Wireless Interfaces
    # ──────────────────────────────────────────────

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

    def configure_wireless(self, interface, **kwargs):
        """Configure a wireless interface.

        Accepts any RouterOS wireless interface property as a keyword argument.
        Underscores in parameter names are converted to hyphens automatically.

        Common parameters:
            ssid (str): Network name.
            band (str): e.g. '2ghz-b/g/n', '5ghz-a/n/ac'.
            frequency (str): Channel frequency in MHz, e.g. '2437', '5180'.
            channel_width (str): e.g. '20mhz', '20/40mhz-Ce'.
            security_profile (str): Name of a security profile.
            tx_power (str): Transmit power in dBm, e.g. '5'.
            hide_ssid (str): 'yes' or 'no'.
            disabled (str): 'yes' or 'no'.

        Example::

            router.configure_wireless('wlan1',
                ssid='MyNetwork',
                band='2ghz-b/g/n',
                security_profile='test-wpa2',
                frequency='2437')
        """
        iface_id = self._find_wireless_id(interface)
        data = {k.replace('_', '-'): v for k, v in kwargs.items()}
        return self._patch(f'/interface/wireless/{iface_id}', data)

    def set_wireless_ssid(self, interface, ssid):
        """Change the SSID of a wireless interface.

        For full wireless configuration use configure_wireless().

        Args:
            interface: Interface name (e.g., 'wlan1').
            ssid: New SSID.
        """
        iface_id = self._find_wireless_id(interface)
        self._patch(f'/interface/wireless/{iface_id}', {'ssid': ssid})
        return {'interface': interface, 'ssid': ssid, 'updated': True}

    def enable_interface(self, interface):
        """Enable a wireless interface.

        Args:
            interface: Wireless interface name (e.g., 'wlan1').
        """
        iface_id = self._find_wireless_id(interface)
        self._patch(f'/interface/wireless/{iface_id}', {'disabled': 'false'})

    def disable_interface(self, interface):
        """Disable a wireless interface.

        Args:
            interface: Wireless interface name (e.g., 'wlan1').
        """
        iface_id = self._find_wireless_id(interface)
        self._patch(f'/interface/wireless/{iface_id}', {'disabled': 'true'})

    def wait_for_wireless_ready(self, interface, timeout=30):
        """Poll until a wireless interface is enabled and running.

        Useful after calling enable_interface() or configure_wireless() to
        confirm the AP is broadcasting before continuing.

        Args:
            interface: Wireless interface name (e.g., 'wlan1').
            timeout: Maximum seconds to wait (default: 30).

        Raises:
            TimeoutError: If the interface is not ready within timeout.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                iface_id = self._find_wireless_id(interface)
                status = self._get(f'/interface/wireless/{iface_id}')
                if status.get('disabled') == 'false':
                    return True
            except Exception:
                pass
            time.sleep(2)
        raise TimeoutError(f"Wireless interface '{interface}' not ready within {timeout}s")

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

    def is_client_connected(self, mac_address=None):
        """Check if a wireless client is currently associated.

        Args:
            mac_address: MAC address to check (colon or no-separator format).
                If None, returns True if any client is connected.

        Returns:
            True if the client (or any client) is connected.
        """
        clients = self._get('/interface/wireless/registration-table')
        if mac_address is None:
            return len(clients) > 0
        mac_upper = mac_address.upper().replace(':', '').replace('-', '')
        for client in clients:
            client_mac = client.get('mac-address', '').upper().replace(':', '').replace('-', '')
            if client_mac == mac_upper:
                return True
        return False

    def set_client_isolation(self, interface, enabled=True):
        """Enable or disable AP client isolation on a wireless interface.

        When enabled, clients on the same AP cannot communicate with each other.

        Args:
            interface: Wireless interface name.
            enabled: True to enable isolation, False to disable.
        """
        self.configure_wireless(interface,
                                **{'default-forwarding': 'no' if enabled else 'yes'})

    # ──────────────────────────────────────────────
    # Access List (MAC filtering)
    # ──────────────────────────────────────────────

    def get_access_list(self):
        """List all wireless access list entries."""
        return self._get('/interface/wireless/access-list')

    def add_access_list_entry(self, mac_address, authentication=True,
                              interface='', signal_range='', **kwargs):
        """Add a wireless access list entry (allow or deny a specific client).

        Args:
            mac_address: Client MAC address.
            authentication: True to allow, False to deny (default: True).
            interface: Restrict entry to a specific interface (optional).
            signal_range: Minimum signal strength, e.g. '-70..-40' (optional).
            **kwargs: Additional RouterOS access-list properties.
        """
        data = {
            'mac-address': mac_address,
            'authentication': 'yes' if authentication else 'no',
            'comment': self._test_tag,
        }
        if interface:
            data['interface'] = interface
        if signal_range:
            data['signal-range'] = signal_range
        data.update(kwargs)
        return self._put('/interface/wireless/access-list', data)

    def remove_access_list_entry(self, mac_address):
        """Remove all access list entries for a specific MAC address.

        Args:
            mac_address: Client MAC address.
        """
        entries = self.get_access_list()
        for entry in entries:
            if entry.get('mac-address', '').upper() == mac_address.upper():
                self._delete(f'/interface/wireless/access-list/{entry[".id"]}')

    def clear_access_list(self):
        """Remove all test-tagged access list entries."""
        entries = self.get_access_list()
        for entry in entries:
            if entry.get('comment') == self._test_tag:
                self._delete(f'/interface/wireless/access-list/{entry[".id"]}')

    # ──────────────────────────────────────────────
    # DHCP
    # ──────────────────────────────────────────────

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

    def enable_dhcp(self):
        """Enable all DHCP servers."""
        servers = self._get('/ip/dhcp-server')
        for server in servers:
            self._patch(f'/ip/dhcp-server/{server[".id"]}', {'disabled': 'false'})

    def disable_dhcp(self):
        """Disable all DHCP servers.

        Useful for testing device behaviour when no IP address can be obtained.
        """
        servers = self._get('/ip/dhcp-server')
        for server in servers:
            self._patch(f'/ip/dhcp-server/{server[".id"]}', {'disabled': 'true'})

    def set_dhcp_lease_time(self, lease_time='10m'):
        """Set the DHCP lease time on all DHCP servers.

        Args:
            lease_time: RouterOS time string, e.g. '1m', '10m', '1h' (default: '10m').
        """
        servers = self._get('/ip/dhcp-server')
        for server in servers:
            self._patch(f'/ip/dhcp-server/{server[".id"]}', {'lease-time': lease_time})

    # ──────────────────────────────────────────────
    # Traffic Shaping
    # ──────────────────────────────────────────────

    def add_bandwidth_limit(self, target, max_limit, name=None):
        """Add a simple queue to limit bandwidth for a target IP or subnet.

        Args:
            target: IP address or subnet to limit, e.g. '192.168.88.0/24'.
            max_limit: Upload/download rate, e.g. '1M/1M', '256k/2M'.
            name: Queue name (auto-generated from test tag if None).

        Example::

            router.add_bandwidth_limit('192.168.88.0/24', '1M/1M')
        """
        data = {
            'name': name or f'limit-{self._test_tag}',
            'target': target,
            'max-limit': max_limit,
            'comment': self._test_tag,
        }
        return self._put('/queue/simple', data)

    def remove_bandwidth_limits(self):
        """Remove all test-tagged bandwidth limit queues."""
        queues = self._get('/queue/simple')
        for q in queues:
            if q.get('comment') == self._test_tag:
                self._delete(f'/queue/simple/{q[".id"]}')

    # ──────────────────────────────────────────────
    # Firewall
    # ──────────────────────────────────────────────

    def add_firewall_rule(self, chain='forward', action='drop', **kwargs):
        """Add a firewall filter rule.

        All rules added via this method are tagged with the test tag and can
        be removed in bulk with remove_firewall_rules().

        Args:
            chain: Firewall chain — 'forward', 'input', or 'output' (default: 'forward').
            action: Rule action — 'drop', 'accept', 'reject' (default: 'drop').
            **kwargs: Additional RouterOS firewall filter properties, e.g.
                protocol='tcp', **{'dst-port': '443'}.

        Example::

            router.add_firewall_rule(chain='forward', action='drop',
                                     protocol='udp', **{'dst-port': '53'})
        """
        data = {
            'chain': chain,
            'action': action,
            'comment': self._test_tag,
        }
        data.update(kwargs)
        return self._put('/ip/firewall/filter', data)

    def remove_firewall_rules(self):
        """Remove all test-tagged firewall filter rules."""
        rules = self._get('/ip/firewall/filter')
        for rule in rules:
            if rule.get('comment') == self._test_tag:
                self._delete(f'/ip/firewall/filter/{rule[".id"]}')

    def block_internet(self):
        """Block all internet access by dropping forwarded traffic.

        Call remove_firewall_rules() or reset_to_defaults() to restore access.
        """
        self.add_firewall_rule(chain='forward', action='drop')

    def block_dns(self):
        """Block DNS resolution by dropping port 53 traffic (UDP and TCP)."""
        self.add_firewall_rule(chain='forward', action='drop',
                               protocol='udp', **{'dst-port': '53'})
        self.add_firewall_rule(chain='forward', action='drop',
                               protocol='tcp', **{'dst-port': '53'})

    def block_port(self, port, protocol='tcp'):
        """Block a specific port for all forwarded traffic.

        Args:
            port: Port number to block.
            protocol: 'tcp' or 'udp' (default: 'tcp').
        """
        self.add_firewall_rule(chain='forward', action='drop',
                               protocol=protocol, **{'dst-port': str(port)})

    # ──────────────────────────────────────────────
    # Test Reset
    # ──────────────────────────────────────────────

    def reset_to_defaults(self, baseline_ssid=None, baseline_pass=None,
                          wireless_interfaces=None):
        """Restore the router to a known baseline state for test isolation.

        Removes all test-tagged configuration (firewall rules, bandwidth limits,
        access list entries), re-enables DHCP, re-enables wireless interfaces,
        and removes custom security profiles. If baseline WiFi credentials are
        provided, a fresh baseline security profile is created and applied.

        Call this at the start of each test case to ensure a clean slate.

        Args:
            baseline_ssid: SSID to restore on all wireless interfaces after
                cleanup. If None, SSIDs are not changed.
            baseline_pass: WPA2 passphrase for the baseline network. Required
                if baseline_ssid is provided.
            wireless_interfaces: List of wireless interface names to reset,
                e.g. ['wlan1', 'wlan2']. If None, all wireless interfaces
                are re-enabled but their SSID is only updated if baseline_ssid
                is provided.
        """
        # Remove test artifacts
        self.remove_firewall_rules()
        self.remove_bandwidth_limits()
        self.clear_access_list()

        # Remove all custom security profiles (keep built-in 'default')
        for p in self.get_security_profiles():
            if p.get('name') != 'default':
                try:
                    self._delete(f'/interface/wireless/security-profiles/{p[".id"]}')
                except Exception:
                    pass

        # Re-enable DHCP and reset lease time
        try:
            self.enable_dhcp()
            self.set_dhcp_lease_time('10m')
        except Exception:
            pass

        # Determine which wireless interfaces to touch
        if wireless_interfaces is None:
            try:
                wireless_interfaces = [
                    i['name'] for i in self.get_wireless_interfaces()
                ]
            except Exception:
                wireless_interfaces = []

        # Rebuild baseline wireless config if credentials were supplied
        if baseline_ssid and baseline_pass:
            try:
                self.create_security_profile(
                    name='baseline',
                    mode='dynamic-keys',
                    authentication_types='wpa2-psk',
                    unicast_ciphers='aes-ccm',
                    wpa2_pre_shared_key=baseline_pass,
                )
            except Exception:
                pass

            for iface in wireless_interfaces:
                try:
                    self.configure_wireless(
                        iface,
                        ssid=baseline_ssid,
                        disabled='no',
                        **{'security-profile': 'baseline',
                           'hide-ssid': 'no',
                           'default-forwarding': 'yes'},
                    )
                except Exception:
                    pass
        else:
            # Just re-enable interfaces without changing their config
            for iface in wireless_interfaces:
                try:
                    self.enable_interface(iface)
                except Exception:
                    pass

    # ──────────────────────────────────────────────
    # Raw API access
    # ──────────────────────────────────────────────

    def run(self, path, params=None):
        """Run an arbitrary REST API GET call.

        Args:
            path: API path relative to /rest, e.g. '/ip/address'.
            params: Optional query parameters dict.
        """
        return self._get(path, params=params)

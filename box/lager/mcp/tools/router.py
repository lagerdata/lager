# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP tools for MikroTik router control via direct on-box Net API.

Used for network-level DUT testing: WiFi AP management, firewall rules,
bandwidth shaping, DHCP control, and test-state reset.
"""

import json

from ..server import mcp


@mcp.tool()
def router_info(net: str) -> str:
    """Get router system information (identity, version, uptime, resources).

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    info = Net.get(net, type=NetType.Router).get_system_info()
    return json.dumps({"status": "ok", "net": net, **info}, default=str)


@mcp.tool()
def router_reboot(net: str) -> str:
    """Reboot the router.

    WARNING: The router will be unreachable for ~60-120 seconds.

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).reboot()
    return json.dumps({"status": "ok", "net": net, "action": "reboot"})


@mcp.tool()
def router_interfaces(net: str) -> str:
    """List all network interfaces on the router.

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    interfaces = Net.get(net, type=NetType.Router).get_interfaces()
    return json.dumps({"status": "ok", "net": net, "interfaces": interfaces}, default=str)


@mcp.tool()
def router_set_interface(net: str, interface: str, disabled: bool) -> str:
    """Enable or disable a network interface on the router.

    Args:
        net: Router net name (e.g., 'router1')
        interface: Interface name (e.g., 'ether1', 'wlan1')
        disabled: True to disable, False to enable
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).set_interface_disabled(interface, disabled)
    return json.dumps({"status": "ok", "net": net, "interface": interface, "disabled": disabled})


@mcp.tool()
def router_wireless_interfaces(net: str) -> str:
    """List wireless interfaces and their configuration (SSID, band, security).

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    interfaces = Net.get(net, type=NetType.Router).get_wireless_interfaces()
    return json.dumps({"status": "ok", "net": net, "wireless_interfaces": interfaces}, default=str)


@mcp.tool()
def router_configure_wireless(net: str, interface: str, ssid: str = "", security_profile: str = "", **kwargs) -> str:
    """Configure a wireless interface (SSID, band, security, etc.).

    Args:
        net: Router net name (e.g., 'router1')
        interface: Wireless interface name (e.g., 'wlan1')
        ssid: Network SSID (omit to keep current)
        security_profile: Security profile name (omit to keep current)
    """
    from lager import Net, NetType

    cfg = {}
    if ssid:
        cfg["ssid"] = ssid
    if security_profile:
        cfg["security_profile"] = security_profile
    cfg.update(kwargs)

    Net.get(net, type=NetType.Router).configure_wireless(interface, **cfg)
    return json.dumps({"status": "ok", "net": net, "interface": interface, "config": cfg})


@mcp.tool()
def router_wireless_clients(net: str) -> str:
    """List currently connected wireless clients.

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    clients = Net.get(net, type=NetType.Router).get_wireless_clients()
    return json.dumps({"status": "ok", "net": net, "clients": clients}, default=str)


@mcp.tool()
def router_is_client_connected(net: str, mac_address: str = "") -> str:
    """Check if a wireless client is currently associated.

    Args:
        net: Router net name (e.g., 'router1')
        mac_address: MAC address to check (omit to check if any client is connected)
    """
    from lager import Net, NetType

    connected = Net.get(net, type=NetType.Router).is_client_connected(
        mac_address=mac_address or None
    )
    return json.dumps({"status": "ok", "net": net, "mac_address": mac_address, "connected": connected})


@mcp.tool()
def router_dhcp_leases(net: str) -> str:
    """List DHCP leases (clients that have received IP addresses).

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    leases = Net.get(net, type=NetType.Router).get_dhcp_leases()
    return json.dumps({"status": "ok", "net": net, "leases": leases}, default=str)


@mcp.tool()
def router_enable_dhcp(net: str) -> str:
    """Enable all DHCP servers on the router.

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).enable_dhcp()
    return json.dumps({"status": "ok", "net": net, "dhcp": "enabled"})


@mcp.tool()
def router_disable_dhcp(net: str) -> str:
    """Disable all DHCP servers on the router.

    Useful for testing device behaviour when no IP address can be obtained.

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).disable_dhcp()
    return json.dumps({"status": "ok", "net": net, "dhcp": "disabled"})


@mcp.tool()
def router_block_internet(net: str) -> str:
    """Block all internet access by dropping forwarded traffic.

    Call router_reset() to restore access.

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).block_internet()
    return json.dumps({"status": "ok", "net": net, "action": "block_internet"})


@mcp.tool()
def router_block_dns(net: str) -> str:
    """Block DNS resolution by dropping port 53 traffic.

    Args:
        net: Router net name (e.g., 'router1')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).block_dns()
    return json.dumps({"status": "ok", "net": net, "action": "block_dns"})


@mcp.tool()
def router_block_port(net: str, port: int, protocol: str = "tcp") -> str:
    """Block a specific port for all forwarded traffic.

    Args:
        net: Router net name (e.g., 'router1')
        port: Port number to block
        protocol: 'tcp' or 'udp' (default: 'tcp')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).block_port(port, protocol=protocol)
    return json.dumps({"status": "ok", "net": net, "port": port, "protocol": protocol, "action": "block"})


@mcp.tool()
def router_add_bandwidth_limit(net: str, target: str, max_limit: str) -> str:
    """Add a bandwidth limit for a target IP or subnet.

    Args:
        net: Router net name (e.g., 'router1')
        target: IP address or subnet (e.g., '192.168.88.0/24')
        max_limit: Upload/download rate (e.g., '1M/1M', '256k/2M')
    """
    from lager import Net, NetType

    Net.get(net, type=NetType.Router).add_bandwidth_limit(target, max_limit)
    return json.dumps({"status": "ok", "net": net, "target": target, "max_limit": max_limit})


@mcp.tool()
def router_reset(net: str, baseline_ssid: str = "", baseline_pass: str = "") -> str:
    """Reset the router to a clean test baseline.

    Removes all test-tagged configuration (firewall rules, bandwidth limits,
    access list entries), re-enables DHCP, and re-enables wireless interfaces.
    Optionally restores baseline WiFi credentials.

    Args:
        net: Router net name (e.g., 'router1')
        baseline_ssid: SSID to restore (omit to keep current)
        baseline_pass: WPA2 passphrase for baseline network
    """
    from lager import Net, NetType

    kwargs = {}
    if baseline_ssid:
        kwargs["baseline_ssid"] = baseline_ssid
    if baseline_pass:
        kwargs["baseline_pass"] = baseline_pass

    Net.get(net, type=NetType.Router).reset_to_defaults(**kwargs)
    return json.dumps({"status": "ok", "net": net, "action": "reset_to_defaults"})

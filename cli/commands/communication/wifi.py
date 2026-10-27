# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.wifi.commands

    Commands for controlling WiFi
"""
from __future__ import annotations

import json
import re
import shlex

import click

# Import consolidated helpers from cli.core.net_helpers
from ...core.group_usage import LagerGroup
from ...core.net_helpers import resolve_box, resolve_box_locked, post_box_command
from ...errors import LagerError, ssh_error
from ..box._ssh import default_ssh_runner, resolve_box_user, strip_ssh_banner

# WiFi constraints
MAX_SSID_LENGTH = 32  # IEEE 802.11 maximum SSID length
MIN_WPA_PASSWORD_LENGTH = 8  # WPA/WPA2 minimum passphrase length
MAX_WPA_PASSWORD_LENGTH = 63  # WPA/WPA2 maximum passphrase length

# Common wireless interface names
COMMON_INTERFACES = ['wlan0', 'wlan1', 'wlp2s0', 'wlp3s0', 'wifi0']


def _validate_ssid(ctx: click.Context, ssid: str) -> None:
    """Validate SSID format and length."""
    if not ssid:
        click.secho("Error: SSID cannot be empty", fg='red', err=True)
        ctx.exit(1)

    if len(ssid) > MAX_SSID_LENGTH:
        click.secho(f"Error: SSID too long ({len(ssid)} characters)", fg='red', err=True)
        click.secho(f"Maximum SSID length is {MAX_SSID_LENGTH} characters.", err=True)
        ctx.exit(1)

    # Check for non-printable characters
    if not ssid.isprintable():
        click.secho("Error: SSID contains non-printable characters", fg='red', err=True)
        ctx.exit(1)


def _validate_password(ctx: click.Context, password: str) -> None:
    """Validate WPA/WPA2 password length (if provided)."""
    if not password:
        return  # Empty password is allowed for open networks

    if len(password) < MIN_WPA_PASSWORD_LENGTH:
        click.secho(f"Error: Password too short ({len(password)} characters)", fg='red', err=True)
        click.secho(f"WPA/WPA2 passwords must be at least {MIN_WPA_PASSWORD_LENGTH} characters.", err=True)
        ctx.exit(1)

    if len(password) > MAX_WPA_PASSWORD_LENGTH:
        click.secho(f"Error: Password too long ({len(password)} characters)", fg='red', err=True)
        click.secho(f"WPA/WPA2 passwords can be at most {MAX_WPA_PASSWORD_LENGTH} characters.", err=True)
        ctx.exit(1)


def _validate_interface(ctx: click.Context, interface: str) -> None:
    """Validate wireless interface name format."""
    if not interface:
        click.secho("Error: Interface name cannot be empty", fg='red', err=True)
        ctx.exit(1)

    # Interface names should be alphanumeric and not too long
    if len(interface) > 15:  # Linux IFNAMSIZ - 1
        click.secho(f"Error: Interface name too long: {interface}", fg='red', err=True)
        click.secho("Interface names must be 15 characters or less.", err=True)
        ctx.exit(1)

    # Warn if using non-standard interface name
    if interface not in COMMON_INTERFACES:
        click.secho(f"Note: '{interface}' is not a common wireless interface name.", fg='yellow', err=True)
        click.secho(f"Common names: {', '.join(COMMON_INTERFACES)}", err=True)


# nmcli connect can take up to ~30s on the box, plus scan latency.
_WIFI_HTTP_TIMEOUT = 60.0


def _post_wifi(ctx: click.Context, box_ip: str, action: str, **params) -> dict:
    """POST one action to :9000/wifi/command and return the response."""
    return post_box_command(
        ctx, box_ip, "/wifi/command", action,
        quiet=True, http_timeout=_WIFI_HTTP_TIMEOUT, **params,
    )


def _format_networks_table(networks: list[dict]) -> str:
    """Format scan results in the same table shape the old impl printed."""
    if not networks:
        return "No networks found!"

    lines = []
    lines.append(f"{'SSID':<25} {'Security':<10} {'Strength'}")
    lines.append("-" * 50)
    for net in networks:
        ssid = (net.get('ssid') or 'Unknown')[:24]
        security = net.get('security', 'Unknown')
        strength = net.get('strength', 0)
        lines.append(f"{ssid:<25} {security:<10} {strength}%")
    return '\n'.join(lines)


# `status` and `access-points` read the box host's Wi-Fi over SSH. The lager
# container cannot: on its default network it has no view of the host's
# wireless interface, and the image has no nmcli, so both lookups failed there
# and `status` still exited 0. They only read, so they take no box lock.
_HOST_SSH_TIMEOUT = 45

_MISSING = '!missing'

# Both tools, each optional on the host. A tool that is absent, or whose
# daemon is not running, prints the marker instead of its output.
_STATUS_SCRIPT = (
    "echo '--nmcli--'; "
    f"nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null || echo '{_MISSING}'; "
    "echo '--iw--'; "
    f"iw dev 2>/dev/null || echo '{_MISSING}'"
)

_NMCLI_STATES = {
    'connected': 'Connected',
    'disconnected': 'Disconnected',
    'unavailable': 'Unavailable (radio off or no driver)',
    'unmanaged': 'Not managed by NetworkManager',
}


def _nmcli_fields(line: str) -> list[str]:
    """Split one `nmcli -t` line on its unescaped colons."""
    return [f.replace('\\:', ':').replace('\\\\', '\\')
            for f in re.split(r'(?<!\\):', line)]


def _parse_host_status(out: str) -> tuple[dict, bool]:
    """Turn the status script's output into interfaces, and whether any tool ran.

    nmcli gives the state; `iw dev` gives the SSID, which nmcli only knows as
    a connection name that need not match it.
    """
    sections, current = {}, None
    for line in out.splitlines():
        if line in ('--nmcli--', '--iw--'):
            current = line.strip('-')
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    nmcli = sections.get('nmcli', [_MISSING])
    iw = sections.get('iw', [_MISSING])
    have_nmcli = _MISSING not in nmcli
    have_iw = _MISSING not in iw

    interfaces = {}
    if have_iw:
        name = None
        for line in iw:
            words = line.split()
            if len(words) >= 2 and words[0] == 'Interface':
                name = words[1]
                interfaces[name] = {'interface': name, 'ssid': 'Not Connected',
                                    'state': 'Disconnected'}
            elif name and len(words) >= 2 and words[0] == 'ssid':
                interfaces[name]['ssid'] = line.strip()[len('ssid '):]
                interfaces[name]['state'] = 'Connected'
    if have_nmcli:
        for line in nmcli:
            fields = _nmcli_fields(line)
            if len(fields) < 4 or fields[1] != 'wifi':
                continue
            device, _type, state, connection = fields[:4]
            info = interfaces.setdefault(device, {
                'interface': device, 'ssid': 'Not Connected', 'state': ''})
            base = state.split(' ')[0]
            info['state'] = _NMCLI_STATES.get(base, state)
            if base == 'connected' and info['ssid'] == 'Not Connected' and connection:
                info['ssid'] = connection
    return interfaces, have_nmcli or have_iw


def _run_on_host(box_ip: str, cmd: str) -> tuple[int, str, str]:
    """Run a read-only command on the box host; SSH's own failures exit here."""
    rc, out, err = default_ssh_runner(box_ip, cmd, timeout=_HOST_SSH_TIMEOUT)
    if rc == 255:
        raise ssh_error(strip_ssh_banner(err), box_ip, resolve_box_user(box_ip))
    return rc, out, err


@click.group(name='wifi', cls=LagerGroup, hidden=True)
def _wifi():
    """Manage WiFi network settings"""
    pass


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
def status(ctx, box):
    """
        Get the current WiFi Status of the box
    """
    box_ip = resolve_box(ctx, box, read_only=True)

    _rc, out, _err = _run_on_host(box_ip, _STATUS_SCRIPT)
    found, readable = _parse_host_status(out)
    if not readable:
        raise LagerError(
            "Cannot read the box's Wi-Fi state.",
            cause="Neither nmcli (with NetworkManager running) nor iw is available on the box host.",
            fixes=["Install one of them on the box host, e.g. sudo apt install iw"],
        )
    if not found:
        raise LagerError("The box has no wireless interface.")

    click.secho("WiFi Status:", fg='green')
    click.echo("=" * 40)
    for info in found.values():
        connected = info.get('state', '').startswith('Connected')
        click.secho(f"Interface: {info.get('interface')}", fg='green')
        click.secho(f"    SSID:  {info.get('ssid')}",
                    fg='green' if connected else 'red')
        click.secho(f"    State: {info.get('state')}",
                    fg='green' if connected else 'red')
        click.echo()


def _scan_on_host(box_ip: str, interface: str) -> list[dict]:
    """List the access points the box host sees on ``interface``, strongest first.

    nmcli lists one row per access point, so a network with several shows up
    several times; each SSID is kept once, at its strongest.
    """
    cmd = (f"command -v nmcli >/dev/null 2>&1 || {{ echo '{_MISSING}'; exit 0; }}; "
           f"nmcli -t -f SSID,SECURITY,SIGNAL device wifi list ifname {shlex.quote(interface)}")
    rc, out, err = _run_on_host(box_ip, cmd)
    if out.strip() == _MISSING:
        raise LagerError(
            "Cannot scan for Wi-Fi networks.",
            cause="nmcli is not installed on the box host.",
            fixes=["Install NetworkManager on the box host, e.g. sudo apt install network-manager"],
        )
    if rc != 0:
        raise LagerError(
            f"The Wi-Fi scan on {interface} failed.",
            cause=strip_ssh_banner(err) or f"nmcli exited {rc} on the box host.",
        )

    best = {}
    for line in out.splitlines():
        fields = _nmcli_fields(line)
        if len(fields) < 3 or not fields[0]:
            continue
        ssid, security, signal = fields[0], fields[1], fields[2]
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        if ssid not in best or strength > best[ssid]['strength']:
            best[ssid] = {'ssid': ssid, 'security': security or 'Open',
                          'strength': strength}
    return sorted(best.values(), key=lambda n: n['strength'], reverse=True)


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--interface', required=False, help='Wireless interface to use', default='wlan0')
def access_points(ctx, box, interface='wlan0'):
    """
        Get WiFi access points visible to the box
    """
    # Validate interface name
    _validate_interface(ctx, interface)

    box_ip = resolve_box(ctx, box, read_only=True)

    click.secho(f"Scanning for WiFi networks on {interface}...", fg='green')
    networks = _scan_on_host(box_ip, interface)

    click.secho(f"\nFound {len(networks)} network(s):", fg='green')
    click.secho(_format_networks_table(networks), fg='green')

    click.secho("\nJSON Output:", fg='green')
    click.echo(json.dumps({'access_points': networks}, indent=2))


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--ssid', required=True, help='SSID of the network to connect to')
@click.option('--interface', help='Wireless interface to use', default='wlan0', show_default=True)
@click.option('--password', required=False, help='Password of the network to connect to', default='')
def connect(ctx, box, ssid, interface, password=''):
    """
        Connect the box to a new network
    """
    # Validate inputs
    _validate_ssid(ctx, ssid)
    _validate_password(ctx, password)
    _validate_interface(ctx, interface)

    box_ip = resolve_box_locked(ctx, box, 'wifi')

    click.secho(f"Connecting to WiFi network: {ssid}", fg='green')
    result = _post_wifi(ctx, box_ip, 'connect',
                        ssid=ssid, password=password, interface=interface)

    click.secho(f"[OK] Successfully connected to {ssid}", fg='green')
    click.echo("\nJSON Output:")
    click.echo(json.dumps(result.get('value') or {}, indent=2))


@_wifi.command()
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--yes', is_flag=True, help='Confirm the action without prompting')
@click.argument('SSID', required=True)
def delete_connection(ctx, box, yes, ssid):
    """
        Delete the specified network from the box
    """
    # Validate SSID format
    _validate_ssid(ctx, ssid)

    if not yes and not click.confirm('An ethernet connection will be required to bring the box back online. Proceed?', default=False):
        click.echo("Aborting")
        return

    box_ip = resolve_box_locked(ctx, box, 'wifi')

    click.secho(f"Deleting WiFi connection: {ssid}", fg='green')
    result = _post_wifi(ctx, box_ip, 'delete', ssid=ssid, connection_name=ssid)

    click.secho(f"[OK] Successfully deleted connection: {ssid}", fg='green')
    click.echo("\nJSON Output:")
    click.echo(json.dumps(result.get('value') or {}, indent=2))

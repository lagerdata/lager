# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.debug.commands

    Debug an elf file - Updated for direct SSH execution
"""
import itertools
import click
from click.exceptions import Abort, Exit
import json
import re
import requests
import signal
import sys
from texttable import Texttable
from ....context import get_default_box, get_default_net
from ....core.param_types import MemoryAddressType, HexArrayType, BinfileType, ByteSizeType
from ....box_storage import get_box_ip, get_box_name_by_ip, get_box_user
from ....core.net_group import NetGroupHelpMixin, NetSubCommand
from ....errors import LagerError
from ....gateway_auth import auth_server_for_box
from ....gateway_tunnel import ROUTE_DIRECT, ROUTE_TUNNEL, GatewayTunnel, choose_route
from .service_client import DebugServiceClient
from .net_cache import get_net_cache

DEBUG_ROLE = "debug"


def _get_jlink_script_content(ctx, net_name, debug_net):
    """
    Get base64-encoded J-Link script for /debug/connect.

    Resolution order:
    1. Local .lager DEBUG section (project override)
    2. ``jlink_script`` on the debug net dict (from ``net list`` / saved_nets on the box)
    3. None — the debug service can still load from NetsCache by net name

    Args:
        ctx: Click context
        net_name: Name of the debug net
        debug_net: Debug net configuration dict from ``_get_debug_net``

    Returns:
        Base64-encoded script content, or None
    """
    import base64
    from ....config import get_debug_script_for_net

    script_path = get_debug_script_for_net(net_name)
    if script_path:
        # The .lager DEBUG section historically only carried J-Link scripts,
        # so we treat anything ending in .JLinkScript as J-Link. Files for
        # the OpenOCD backend (.cfg / .tcl) are handled by
        # ``_get_openocd_config_content`` below.
        suffix = str(script_path).lower()
        if suffix.endswith('.jlinkscript'):
            try:
                with open(script_path, 'rb') as f:
                    return base64.b64encode(f.read()).decode('ascii')
            except Exception as e:
                click.secho(f"Warning: the CLI did not read the J-Link script from config: {e}", fg='yellow', err=True)

    if debug_net:
        embedded = debug_net.get('jlink_script')
        if isinstance(embedded, str) and embedded.strip():
            return embedded

    return None


def _get_openocd_config_content(ctx, net_name, debug_net):
    """Get base64-encoded OpenOCD ``.cfg`` content for /debug/connect.

    Mirrors ``_get_jlink_script_content`` for the OpenOCD backend. Resolution
    order:

    1. Local .lager DEBUG section (project override), only when the file
       extension looks like an OpenOCD config (``.cfg`` / ``.tcl``).
    2. ``openocd_config`` on the debug net dict (from saved_nets on the box).
    3. None — the box falls back to built-in interface + target configs.
    """
    import base64
    from ....config import get_debug_script_for_net

    script_path = get_debug_script_for_net(net_name)
    if script_path:
        suffix = str(script_path).lower()
        if suffix.endswith('.cfg') or suffix.endswith('.tcl'):
            try:
                with open(script_path, 'rb') as f:
                    return base64.b64encode(f.read()).decode('ascii')
            except Exception as e:
                click.secho(
                    f"Warning: the CLI did not read the OpenOCD config from .lager: {e}",
                    fg='yellow', err=True,
                )

    if debug_net:
        embedded = debug_net.get('openocd_config')
        if isinstance(embedded, str) and embedded.strip():
            return embedded

    return None


def _resolve_box_with_username(ctx, box):
    """
    Resolve box parameter to (IP, username) tuple.
    Handles both box names and direct IPs, looking up username from storage.
    Also acquires an ephemeral lock for the duration of the debug session.

    Args:
        ctx: Click context
        box: Box name or IP address

    Returns:
        Tuple of (ip_address, username)
    """
    from ....core.net_helpers import resolve_box_locked

    # Resolve, validate, and auto-lock the box
    box_ip = resolve_box_locked(ctx, box, 'debug')

    # Determine box name for username lookup
    # If box was provided and is not an IP, it's the box name
    if box and not box.replace('.', '').isdigit():
        box_name = box
    else:
        # It was an IP or None, try reverse lookup
        box_name = get_box_name_by_ip(box_ip)

    # Get username (defaults to 'lagerdata' if not found)
    username = get_box_user(box_name) if box_name else 'lagerdata'
    if not username:
        username = 'lagerdata'

    return (box_ip, username)


def validate_speed_param(ctx, param, value):
    """
    Validate speed parameter at CLI level for immediate user feedback.

    Args:
        ctx: Click context
        param: Click parameter
        value: Speed value from user

    Returns:
        Validated speed value

    Raises:
        click.BadParameter: If speed is invalid
    """
    if value is None or value == 'adaptive':
        return value

    try:
        speed_int = int(value)
    except (ValueError, TypeError):
        raise click.BadParameter(
            f"Invalid speed value: '{value}'. "
            f"Speed must be a positive integer (in kHz) or 'adaptive'"
        )

    if speed_int <= 0:
        raise click.BadParameter(
            f"Invalid speed: {speed_int} kHz. "
            f"Speed must be a positive integer greater than 0"
        )

    if speed_int > 50000:  # 50 MHz is unrealistically high for SWD/JTAG
        raise click.BadParameter(
            f"Invalid speed: {speed_int} kHz. "
            f"Maximum supported speed is 50000 kHz (50 MHz). "
            f"Typical speeds: 100-4000 kHz"
        )

    return value

def _get_debug_net(ctx, box, net_name=None):
    """
    Get debug net information for the box with caching.
    If net_name is provided, use that specific net.
    Otherwise, find the first available debug net.
    """
    # Check cache first
    cache = get_net_cache()
    cached_net = cache.get(box, net_name)
    if cached_net:
        return cached_net

    # Cache miss - fetch from the box's :9000 HTTP API
    from ....core.net_helpers import fetch_nets

    nets = fetch_nets(box)
    debug_nets = [n for n in nets if n.get("role") == "debug"]

    if net_name:
        # Find specific debug net
        target_net = next((n for n in debug_nets if n.get("name") == net_name), None)
        if not target_net:
            click.secho(f"Debug net '{net_name}' not found.", fg='red', err=True)
            ctx.exit(1)
    else:
        # Find first available debug net
        if not debug_nets:
            click.secho("No debug nets found. Create one with: lager nets add [NAME] debug [DEVICE_TYPE] [ADDRESS]", fg='red', err=True)
            ctx.exit(1)
        target_net = debug_nets[0]

    # Cache the result before returning
    cache.set(box, net_name, target_net)
    return target_net


def _debug_net_jlink_device(debug_net):
    """
    J-Link device / MCU name from saved net config.

    Matches ``DebugNet`` resolution: ``channel`` if set, else legacy ``pin``.
    """
    if not debug_net:
        return ''
    ch = debug_net.get('channel')
    if ch is not None and str(ch).strip():
        return str(ch).strip()
    pin = debug_net.get('pin')
    if pin is not None and str(pin).strip():
        return str(pin).strip()
    return ''


def _get_service_client(box):
    """
    Create and return a debug service client for the given box.
    Uses DirectHTTP (port 8765) to connect to python container debug service.

    Args:
        box: Box name or IP address

    Returns:
        DebugServiceClient instance or None on failure
    """
    try:
        # Use DirectHTTP: connect to port 8765 (python container debug service), no SSH tunnel needed
        client = DebugServiceClient(box, service_port=8765, ssh_tunnel=False)
        return client
    except ConnectionRefusedError:
        click.secho(f"Error: Connection refused to debug service on {box}:8765", fg='red', err=True)
        click.secho("Possible causes:", err=True)
        click.secho("  - The debug service does not run on the box", err=True)
        click.secho("  - The Docker container 'lager' is not up", err=True)
        click.secho(f"Check with: lager ssh --box {box} -- docker ps", err=True)
        return None
    except TimeoutError:
        click.secho(f"Error: Connection timed out to debug service on {box}:8765", fg='red', err=True)
        click.secho("Possible causes:", err=True)
        click.secho("  - Box is offline or unreachable", err=True)
        click.secho("  - Firewall blocking port 8765", err=True)
        click.secho(f"Check connectivity with: ping {box}", err=True)
        return None
    except Exception as e:
        error_str = str(e).lower()
        if "connection refused" in error_str:
            click.secho(f"Error: Connection refused to debug service on {box}:8765", fg='red', err=True)
            click.secho("Check the Docker status on the box. The debug service can be down.", err=True)
        elif "timeout" in error_str or "timed out" in error_str:
            click.secho(f"Error: Connection timed out to debug service on {box}:8765", fg='red', err=True)
            click.secho("Check that the box is online and reachable.", err=True)
        elif "name or service not known" in error_str or "nodename nor servname" in error_str:
            click.secho(f"Error: The hostname '{box}' did not resolve", fg='red', err=True)
            click.secho("Check that the box name or IP address is correct.", err=True)
        else:
            click.secho(f"Error: Failed to create debug service client: {e}", fg='red', err=True)
        return None

def _is_connected(client, debug_net=None):
    """
    Check if a debugger is currently connected for `debug_net`'s probe.

    Passing the net is what makes this answer correct on a box with more than
    one probe: the box resolves the probe serial from it and checks that
    probe's pidfile. Called without a net it reports on the legacy
    un-suffixed pidfile instead, which a serial-aware box never writes -- so
    a running gdbserver read as "not connected", and callers responded by
    erasing and reconnecting, killing the live session and wedging the probe.

    Args:
        client: DebugServiceClient instance
        debug_net: the debug net dict, so the box can resolve the probe

    Returns:
        True if connected, False otherwise
    """
    try:
        status = client.get_debug_status(debug_net)
        # `gdbserver_running` is the explicit name for what this has always
        # meant. `connected` is its deprecated alias, still sent by the box and
        # still the only field an older box sends at all.
        if 'gdbserver_running' in status:
            return bool(status['gdbserver_running'])
        return status.get('connected', False)
    except Exception:
        return False


def _target_attached(client, debug_net=None):
    """Whether the part answers, as opposed to whether a server is alive.

    Tri-state, and the None matters. An older box does not send the field at
    all; a probe can be refused or time out. Neither is evidence the target is
    absent, so callers must test `is True` and must not treat None as failure.
    Reading None as "not attached" would tear down working sessions.
    """
    try:
        status = client.get_debug_status(debug_net, probe=True)
    except Exception:
        return None
    if 'target_attached' not in status:
        return None
    value = status['target_attached']
    return value if value is None else bool(value)

def _resolve_debug_scripts(ctx, net_name, debug_net):
    """Return (jlink_script, openocd_config) for a debug net.

    Both fields are computed unconditionally — the box ignores the one that
    doesn't match the resolved backend. Callers should forward both to
    ``client.connect(...)`` so the CLI stays backend-agnostic.

    If the local ``.lager`` config points at a debug script whose extension
    doesn't match either backend (J-Link ``.JLinkScript`` / OpenOCD
    ``.cfg``/``.tcl``), warn the user — otherwise the script is silently
    dropped and the resulting connect uses the box's defaults, which is
    almost never what the user intended.
    """
    j = _get_jlink_script_content(ctx, net_name, debug_net)
    o = _get_openocd_config_content(ctx, net_name, debug_net)
    if j is None and o is None:
        from ....config import get_debug_script_for_net
        configured = get_debug_script_for_net(net_name)
        if configured:
            click.secho(
                f"Warning: ignoring debug script {configured!r} for net "
                f"{net_name!r}: extension not recognized. Use "
                f"`.JLinkScript` for J-Link probes or `.cfg`/`.tcl` for "
                f"OpenOCD probes.",
                fg='yellow', err=True,
            )
    return (j, o)


def _auto_connect_if_needed(client, debug_net, ctx, quiet=False,
                            jlink_script=None, openocd_config=None):
    """
    Auto-connect to debugger if not already connected.
    Does NOT reconnect if already connected.

    Args:
        client: DebugServiceClient instance
        debug_net: Debug net configuration
        ctx: Click context
        quiet: Suppress informational messages
        jlink_script: Optional base64-encoded J-Link script (used when the
            box resolves the probe to the J-Link backend).
        openocd_config: Optional base64-encoded OpenOCD ``.cfg`` content
            (used when the box resolves the probe to the OpenOCD backend).

    Returns:
        True if connected (either already or newly), False on failure
    """
    # A live gdbserver is not the same thing as an attached part, and this
    # gate used to conflate them: on a box where the server outlives the
    # target, every caller below proceeded against hardware that was not
    # there. Ask whether the target answers, and short-circuit only on a
    # confirmed yes.
    #
    # The box returns None when it could not establish an answer -- an older
    # box that omits the field, a refused probe, a timeout. None is not False:
    # treating it as "absent" would tear down working sessions, so the branch
    # below handles it explicitly and falls back to server liveness. That
    # explicit branch is what protects the behaviour; `is True` here is
    # defensive style on top of it, not the guard itself.
    attached = _target_attached(client, debug_net)
    if attached is True:
        return True

    if attached is None:
        # Nothing was established about the target, so behave exactly as this
        # did before the distinction existed: trust the server's liveness.
        if _is_connected(client, debug_net):
            return True
    elif _is_connected(client, debug_net):
        # The server is up and the target demonstrably is not. Reconnecting is
        # the only thing that can fix that; proceeding is what the old code did
        # and is what let a flash report success against an absent part.
        if not quiet:
            click.secho(
                "The debug session is up, but the target does not answer; reconnecting...",
                fg='yellow', err=True,
            )
        try:
            client.connect(
                debug_net, speed=None, force=True, halt=False,
                jlink_script=jlink_script, openocd_config=openocd_config,
            )
            if not quiet:
                click.secho("Reconnected!", fg='cyan', dim=True)
            return True
        except Exception as exc:
            click.secho(f"Error: the CLI did not reconnect to the target: {exc}", fg='red', err=True)
            return False

    # Not connected, auto-connect
    if not quiet:
        click.secho("Auto-connecting to debugger...", fg='cyan', dim=True)

    try:
        client.connect(
            debug_net, speed=None, force=False, halt=False,
            jlink_script=jlink_script, openocd_config=openocd_config,
        )
        if not quiet:
            click.secho("Auto-connected!", fg='cyan', dim=True)
        return True
    except requests.exceptions.Timeout:
        click.secho("Error: Connection timed out while auto-connecting to debugger", fg='red', err=True)
        click.secho("The debug service can be unresponsive. Try again, or check the box.", err=True)
        return False
    except requests.exceptions.ConnectionError as e:
        click.secho("Error: Connection failed while auto-connecting to debugger", fg='red', err=True)
        error_str = str(e).lower()
        if "connection refused" in error_str:
            click.secho("The debug service can be down.", err=True)
        elif "name or service not known" in error_str:
            click.secho("The box hostname did not resolve.", err=True)
        else:
            click.secho(f"Details: {e}", err=True)
        return False
    except requests.exceptions.HTTPError as e:
        err_detail = str(e)
        try:
            if e.response is not None:
                body = e.response.json()
                if isinstance(body, dict) and body.get('error'):
                    err_detail = body['error']
        except Exception:
            pass
        click.secho("Error: Failed to auto-connect to debugger", fg='red', err=True)
        click.secho(f"Details: {err_detail}", fg='red', err=True)
        click.secho("\nTroubleshooting steps:", fg='cyan', err=True)
        click.secho("  1. Check physical debug cable connection", fg='cyan', err=True)
        click.secho("  2. Verify target device is powered on", fg='cyan', err=True)
        click.secho("  3. Check debug probe LED status", fg='cyan', err=True)
        return False
    except Exception as e:
        click.secho("Error: Failed to auto-connect to debugger", fg='red', err=True)
        click.secho(f"Details: {e}", fg='red', err=True)
        click.secho("\nTroubleshooting steps:", fg='cyan', err=True)
        click.secho("  1. Check physical debug cable connection", fg='cyan', err=True)
        click.secho("  2. Verify target device is powered on", fg='cyan', err=True)
        click.secho("  3. Check debug probe LED status", fg='cyan', err=True)
        return False

def _auto_disconnect(client, debug_net, no_disconnect=False, quiet=False):
    """
    Auto-disconnect from debugger to free resources.
    Respects --no-disconnect flag.

    Args:
        client: DebugServiceClient instance
        debug_net: Debug net configuration
        no_disconnect: If True, skip disconnect
        quiet: Suppress informational messages
    """
    if no_disconnect:
        return

    try:
        client.disconnect(debug_net)
        if not quiet:
            click.secho("Auto-disconnected debugger", fg='cyan', dim=True)
    except Exception:
        pass  # Ignore disconnect errors

def _resolve_box(ctx, box):
    """Resolve box name to IP address if it's a local box."""
    from ....box_storage import resolve_and_validate_box
    return resolve_and_validate_box(ctx, box)


def _list_debug_nets(ctx, box):
    """Get list of debug nets from the box's :9000 HTTP API."""
    from ....core.net_helpers import fetch_nets

    recs = fetch_nets(box)
    return [r for r in recs if r.get("role") == DEBUG_ROLE]


def _display_debug_nets(ctx, box):
    """Display debug nets in a table."""
    nets = _list_debug_nets(ctx, box)
    if not nets:
        click.echo("No debug nets found on this box.")
        return

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_dtype(["t", "t", "t", "t", "t"])
    table.set_cols_align(["l", "l", "l", "l", "l"])
    table.header(["Name", "Net Type", "Instrument", "Channel", "Address"])

    for rec in nets:
        table.add_row([
            rec.get("name", ""),
            rec.get("role", ""),
            rec.get("instrument", ""),
            rec.get("pin", ""),
            rec.get("address", "")
        ])

    click.echo(table.draw())


class NetDebugGroup(NetGroupHelpMixin, click.MultiCommand):
    """Custom multi-command that treats first argument as net name"""

    net_examples = [
        "lager debug SWD reset --box <BOX>",
        "lager debug SWD flash --elf firmware.elf --box <BOX>",
        "lager debug SWD memrd 0x20000000 256 --box <BOX>",
        "lager debug status --box <BOX>        (uses default debug net)",
        "lager debug SWD gdbserver --rtt --interactive --box <BOX> 2>/dev/null | defmt-print -e app.elf",
    ]

    def list_commands(self, ctx):
        """List all available debug subcommands"""
        return ['gdbserver', 'disconnect', 'flash', 'reset', 'erase', 'memrd', 'status', 'health']

    def get_command(self, ctx, name):
        """Get the command for a given subcommand name"""
        commands = {
            'gdbserver': gdbserver,
            'disconnect': disconnect,
            'flash': flash,
            'reset': reset,
            'erase': erase,
            'memrd': memrd,
            'status': status,
            'health': health,
        }
        return commands.get(name)

    def resolve_command(self, ctx, args):
        """Override to handle net_name extraction before command resolution"""
        # List of known subcommands
        subcommands = self.list_commands(ctx)

        # Check if first argument is a subcommand
        if args and args[0] in subcommands:
            # First arg is a subcommand, no net_name provided
            # Check if we have a default net_name
            if not hasattr(ctx.obj, 'net_name') or ctx.obj.net_name is None:
                default_net = get_default_net(ctx, 'debug')
                if default_net:
                    ctx.obj.net_name = default_net
                # If still no net_name, subcommands will handle the error

            # Return the command and remaining args
            cmd_name = args[0]
            return cmd_name, self.get_command(ctx, cmd_name), args[1:]

        # First arg might be net_name, second arg should be command
        if len(args) >= 2 and args[1] in subcommands:
            # Set the net_name from first arg
            ctx.obj.net_name = args[0]
            cmd_name = args[1]
            return cmd_name, self.get_command(ctx, cmd_name), args[2:]

        # Fall back to default behavior
        return super().resolve_command(ctx, args)

    def invoke(self, ctx):
        """Override invoke to handle --box without subcommand (list nets)"""
        # Check if --box was provided and no subcommand is being invoked
        box = ctx.params.get('box')

        # If we have args that are subcommands, proceed normally
        # But if no args (or only options), and box is set, list nets
        if not ctx.protected_args and not ctx.invoked_subcommand:
            if box:
                # List debug nets for the specified box
                resolved_box = _resolve_box(ctx, box)
                _display_debug_nets(ctx, resolved_box)
                return
            else:
                # Show help if no --box and no subcommand
                click.echo(ctx.get_help())
                return

        return super().invoke(ctx)


@click.command(name='debug', cls=NetDebugGroup, invoke_without_command=True)
@click.option("--box", required=False, help="Lager Box name or IP")
@click.pass_context
def _debug(ctx, box):
    """
    Debug firmware and manage debug sessions
    """
    # Net name extraction is handled by NetDebugGroup.resolve_command()
    # Listing nets when --box is provided without subcommand is handled by NetDebugGroup.invoke()
    pass


@click.command(cls=NetSubCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--force/--no-force', is_flag=True, default=False,
              help='Force new connection (default: reuse existing)', show_default=True)
@click.option('--halt/--no-halt', is_flag=True, default=False,
              help='Halt the device when connecting', show_default=True)
@click.option('--speed', type=str, default=None, callback=validate_speed_param,
              help='SWD/JTAG speed in kHz (e.g., 100, 4000) or "adaptive"')
@click.option('--quiet', is_flag=True, default=False,
              help='Suppress informational messages')
@click.option('--json', 'json_output', is_flag=True, default=False,
              help='Output results in JSON format')
@click.option('--rtt', is_flag=True, default=False,
              help='Automatically stream RTT logs after starting GDB server')
@click.option('--rtt-reset', is_flag=True, default=False,
              help='Start GDB server, reset device, then stream RTT logs (captures boot sequence)')
@click.option('-i', '--interactive', is_flag=True, default=False,
              help='Bi-directional RTT: forward stdin to the target\'s RTT down-channel '
                   'while streaming the up-channel to stdout (requires --rtt or --rtt-reset). '
                   'Pipeable: lager debug NET gdbserver --rtt --interactive 2>/dev/null | defmt-print -e app.elf')
@click.option('--rtt-channel', type=int, default=0, show_default=True,
              help='RTT channel to stream (up and down)')
@click.option('--reset', is_flag=True, default=False,
              help='Reset the device after starting GDB server')
@click.option('--gdb-port', type=int, default=None,
              help='Override the auto-allocated GDB server port. By default the box '
                   'picks a port based on the probe\'s slot (2331 for the first probe, '
                   '2334 for the second, etc.). Pass this flag only if you need a '
                   'specific port — and don\'t use it on multi-probe boxes.')
@click.option('--rtt-search-addr', type=str, default=None,
              help='RAM start address for RTT control block search (hex, e.g., 0x20020000)')
@click.option('--rtt-search-size', type=str, default=None,
              help='Size of RAM region to search for RTT control block (hex, e.g., 0x4000)')
@click.option('--rtt-chunk-size', type=str, default=None,
              help='Read chunk size for RTT search (hex, e.g., 0x1000)')
@click.option('--local-port', type=click.IntRange(1, 65535), default=None,
              help='Local port for the tunnel on a box behind a gateway '
                   '(default: the same port as the GDB server on the box)')
@click.option('--no-tunnel', is_flag=True, default=False,
              help='On a box behind a gateway, start the GDB server and return '
                   'without opening a local tunnel to it')
def gdbserver(ctx, box, force, halt, speed, quiet, json_output, rtt, rtt_reset, interactive,
              rtt_channel, reset, gdb_port, rtt_search_addr, rtt_search_size, rtt_chunk_size,
              local_port, no_tunnel):
    """Start the GDB server for the probe (JLinkGDBServer or OpenOCD)

    On a box behind an authenticating gateway, the GDB port is reachable
    only through the gateway. The command then opens a tunnel on
    localhost and stays in the foreground until Ctrl-C; the GDB server
    keeps running on the box afterwards.
    """
    # --interactive only makes sense with an RTT stream to attach to.
    if interactive and not (rtt or rtt_reset):
        click.secho("Error: --interactive requires --rtt or --rtt-reset", fg='red', err=True)
        ctx.exit(1)

    # Validate GDB port range only when the user explicitly passed --gdb-port.
    if gdb_port is not None:
        if gdb_port < 1 or gdb_port > 65535:
            click.secho(f"Error: GDB port must be between 1 and 65535, got {gdb_port}", fg='red', err=True)
            ctx.exit(1)
        if gdb_port < 1024:
            click.secho(f"Warning: Port {gdb_port} is a privileged port (< 1024). Binding it requires root privileges.", fg='yellow', err=True)

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    # Resolve per-backend customisation files. Both are computed; only the
    # one matching the resolved backend takes effect on the box.
    jlink_script, openocd_config = _resolve_debug_scripts(
        ctx, net_name or debug_net.get('name'), debug_net
    )

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Check if already connected and disconnect if so
    # Try to get debug info which will fail if not connected
    already_connected = False
    try:
        # Try to get info - if this succeeds and shows connected, we're connected
        info_result = client.get_info(debug_net)
        if info_result and info_result.get('connected', False):
            already_connected = True
    except Exception:
        # Not connected or error - treat as not connected
        already_connected = False

    if already_connected:
        if not quiet and not json_output:
            click.echo("Already connected. Disconnecting before reconnecting...", err=True)
        try:
            client.disconnect(debug_net, keep_jlink_running=False)
        except Exception as e:
            # J-Link doesn't maintain persistent connections, so disconnect may fail
            # This is expected and can be safely ignored
            pass

        # Wait for GDB server to fully shut down before reconnecting
        # This prevents "No debugger connection found" errors on reconnect
        import time
        time.sleep(1.0)

    # Connect to debugger and start GDB server
    try:
        result = client.connect(
            debug_net, speed=speed, force=force, halt=halt, gdb=True,
            gdb_port=gdb_port,
            jlink_script=jlink_script, openocd_config=openocd_config,
        )
    except requests.exceptions.HTTPError as e:
        # Surface the box's structured error first; the generic checklist
        # below is just a fallback for transport-level failures (timeout,
        # bad gateway) where the server didn't get to format a response.
        error_detail = None
        try:
            error_json = e.response.json()
            error_detail = error_json.get('error') or error_json.get('message')
        except Exception:  # noqa: BLE001 — non-JSON body, treated as empty
            error_detail = None

        if error_detail:
            click.secho(f"Error: {error_detail}", fg='red', err=True)
        else:
            click.secho("Error: Failed to connect to debugger", fg='red', err=True)

        # Keep the canned troubleshooting hints on 500s but only when we
        # had no specific server error to show — otherwise they bury the
        # real cause under boilerplate.
        if not error_detail and ("500" in str(e) or "Internal Server Error" in str(e)):
            click.secho("\nPossible causes:", fg='yellow', err=True)
            click.secho("  • Debug probe not connected to target device", fg='yellow', err=True)
            click.secho("  • Target device not powered", fg='yellow', err=True)
            click.secho("  • Incorrect device type in net configuration", fg='yellow', err=True)
            click.secho("  • Debug interface disabled on target", fg='yellow', err=True)
            click.secho("\nTroubleshooting steps:", fg='cyan', err=True)
            click.secho("  1. Check physical debug cable connection", fg='cyan', err=True)
            click.secho("  2. Verify target device is powered on", fg='cyan', err=True)
            click.secho("  3. Check debug probe LED status", fg='cyan', err=True)
            click.secho(f"  4. Review net configuration: lager nets --box {target_box}", fg='cyan', err=True)

        client.close()
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Error: {e}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Effective port may differ from --gdb-port when the box assigns a per-probe slot.
    effective_gdb_port = result.get('gdb_server', {}).get('gdb_port', gdb_port) if isinstance(result, dict) else gdb_port

    # Pick a backend-appropriate name for the "X started!" line. The
    # box-side response carries ``backend`` at the top level (one of
    # ``jlink`` / ``openocd``); fall back to ``J-Link`` so legacy boxes
    # that don't yet emit ``backend`` keep their existing wording.
    backend_label = _backend_server_label(result)

    gdb_info = result.get('gdb_server') if isinstance(result, dict) else None
    server_up = gdb_info is None or (
        isinstance(gdb_info, dict)
        and gdb_info.get('status') in ('started', 'already_running'))
    box_label = box if box is not None else target_box
    net_label = net_name or debug_net.get('name')
    streaming = rtt or rtt_reset

    # A box behind a gateway does not publish its GDB port, so the address
    # the box reports is one this machine cannot reach. Route through a
    # local tunnel instead; a plain box keeps today's direct address.
    route, route_error = ROUTE_DIRECT, None
    if server_up and effective_gdb_port:
        try:
            route = choose_route(target_box, effective_gdb_port)
        except LagerError as err:
            route_error = err
    if route_error is not None and not streaming:
        # The server is up but no debugger here can reach it. RTT does not
        # need the GDB port (it streams over HTTP), so it goes on below.
        client.close()
        raise route_error

    tunnel = None
    if route == ROUTE_TUNNEL and not no_tunnel:
        tunnel = GatewayTunnel(target_box, effective_gdb_port,
                               local_port=local_port, box_label=box_label)
        try:
            tunnel.bind()
        except LagerError:
            client.close()
            raise

    if not quiet:
        if json_output:
            if tunnel is not None:
                result['tunnel'] = {'local_host': '127.0.0.1',
                                    'local_port': tunnel.local_port,
                                    'box_port': effective_gdb_port}
            click.echo(json.dumps(result, indent=2))
            # A script reading the JSON must see it now, not when the
            # tunnel below finally returns.
            sys.stdout.flush()
        else:
            # Display GDB server info
            if 'gdb_server' in result:
                if gdb_info.get('status') == 'started':
                    click.secho(f"{backend_label} started!", fg='green', err=True)
                    _echo_gdb_address(target_box, box_label, effective_gdb_port, route, tunnel)
                elif gdb_info.get('status') == 'already_running':
                    click.secho(f"{backend_label} already running!", fg='green', err=True)
                    _echo_gdb_address(target_box, box_label, effective_gdb_port, route, tunnel)
                elif 'error' in gdb_info:
                    click.secho(f"Error: GDB server failed to start: {gdb_info.get('message', 'Unknown error')}", fg='red', err=True)
                    ctx.exit(1)
            else:
                click.secho(f"{backend_label} started!", fg='green', err=True)
                _echo_gdb_address(target_box, box_label, effective_gdb_port, route, tunnel)
    if route_error is not None:
        # Streaming mode: the RTT stream still works, the GDB port does not.
        route_error.show()

    try:
        _gdbserver_post_connect(
            ctx, client, debug_net, target_box, quiet=quiet, rtt=rtt,
            rtt_reset=rtt_reset, interactive=interactive,
            rtt_channel=rtt_channel, reset=reset,
            rtt_search_addr=rtt_search_addr, rtt_search_size=rtt_search_size,
            rtt_chunk_size=rtt_chunk_size, tunnel=tunnel,
        )
        if tunnel is not None and not streaming:
            _serve_tunnel_foreground(tunnel, box_label, net_label, quiet=quiet)
    finally:
        if tunnel is not None:
            tunnel.close()


def _echo_gdb_address(target_box, box_label, port, route, tunnel):
    """Tell the user where their debugger connects.

    Only ever prints an address that works from this machine: the box's own
    on a plain box, the local tunnel's on a gated one.
    """
    if tunnel is not None:
        click.secho(f"GDB server running on {box_label}. Connect to localhost:{tunnel.local_port}", fg='cyan', err=True)
        click.secho(f"Connect with: arm-none-eabi-gdb -ex 'target remote localhost:{tunnel.local_port}'", fg='cyan', err=True)
    elif route == ROUTE_TUNNEL:
        # --no-tunnel on a gated box: the box's address would not connect.
        click.secho(f"GDB server running on {box_label}, port {port}. The box's "
                    "gateway is the only way to reach it.", fg='cyan', err=True)
        click.secho("Run this command without --no-tunnel to open a local tunnel to it.",
                    fg='cyan', err=True)
    else:
        click.secho(f"GDB server listening on {target_box}:{port}", fg='cyan', err=True)
        click.secho(f"Connect with: arm-none-eabi-gdb -ex 'target remote {target_box}:{port}'", fg='cyan', err=True)


def _serve_tunnel_foreground(tunnel, box_label, net_label, *, quiet):
    """Keep the GDB tunnel open until Ctrl-C.

    Ctrl-C closes the tunnel only. The GDB server stays up on the box, as it
    does when this command returns on a plain box.
    """
    if not quiet:
        click.secho("Tunnelling through the box's gateway. Press Ctrl-C to "
                    "close the tunnel.", err=True)
    try:
        tunnel.serve_forever()
    except KeyboardInterrupt:
        tunnel.close()
        if not quiet:
            click.echo(err=True)
            click.secho(f"Tunnel closed. The GDB server continues to run on {box_label}.", err=True)
            click.secho(f"Stop it with: lager debug {net_label} disconnect --box {box_label}", err=True)


def _gdbserver_post_connect(ctx, client, debug_net, target_box, *, quiet, rtt,
                            rtt_reset, interactive, rtt_channel, reset,
                            rtt_search_addr, rtt_search_size, rtt_chunk_size,
                            tunnel):
    """What `gdbserver` does once the server is up: RTT, a reset, or nothing.

    ``tunnel``, when set, serves the GDB port from a background thread for
    as long as an RTT stream runs, so a debugger can attach alongside it.
    """
    # Parse RTT search parameters (hex strings to integers)
    rtt_search_params = {}
    if rtt_search_addr is not None:
        try:
            rtt_search_params['search_addr'] = int(rtt_search_addr, 0)
        except ValueError:
            click.secho(f"Error: Invalid --rtt-search-addr value: {rtt_search_addr}", fg='red', err=True)
            client.close()
            ctx.exit(1)
    if rtt_search_size is not None:
        try:
            rtt_search_params['search_size'] = int(rtt_search_size, 0)
        except ValueError:
            click.secho(f"Error: Invalid --rtt-search-size value: {rtt_search_size}", fg='red', err=True)
            client.close()
            ctx.exit(1)
    if rtt_chunk_size is not None:
        try:
            rtt_search_params['chunk_size'] = int(rtt_chunk_size, 0)
        except ValueError:
            click.secho(f"Error: Invalid --rtt-chunk-size value: {rtt_chunk_size}", fg='red', err=True)
            client.close()
            ctx.exit(1)

    # Handle post-connect actions
    if rtt or rtt_reset:
        if tunnel is not None:
            tunnel.start()
        # Wait for GDB server to fully initialize before attempting reset/RTT
        # This prevents "No debugger connection found" errors
        # The server takes ~2-3s to fully initialize:
        # 1. Start the JLinkGDBServer process (~500ms)
        # 2. Write PID file and detect running status (~500ms)
        # 3. Initialize GDB server connection (~500ms)
        # 4. Establish target connection and detect device (~1s)
        # Note: Time varies based on probe type, target, and system load
        import time
        time.sleep(3.0)
        # Ignore SIGPIPE to prevent "Exception ignored in: <_io.TextIOWrapper>" messages
        # when the pipe is broken (e.g., defmt-print exits before we finish)
        # On Windows, SIGPIPE doesn't exist, so we need to handle that
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        except AttributeError:
            # Windows doesn't have SIGPIPE
            pass

        # If --rtt-reset, reset the device first to capture boot sequence
        if rtt_reset:
            if not quiet:
                click.echo("Resetting device to capture boot sequence...", err=True)
            try:
                client.reset(debug_net, halt=False)
            except requests.exceptions.HTTPError as e:
                # Parse error response
                error_msg = "Unknown error"
                try:
                    error_json = e.response.json()
                    error_msg = error_json.get('error', str(e))
                except:
                    error_msg = str(e)

                click.secho(f"Error: Failed to reset device: {error_msg}", fg='red', err=True)

                # Provide troubleshooting steps
                if "No debugger connection found" in error_msg or "400" in str(e):
                    click.secho("\nThis can happen if:", fg='yellow', err=True)
                    click.secho("  • GDB server didn't fully initialize (timing issue)", fg='yellow', err=True)
                    click.secho("  • No physical target device connected", fg='yellow', err=True)
                    click.secho("  • Target device is not powered", fg='yellow', err=True)
                    click.secho("\nTry:", fg='cyan', err=True)
                    click.secho("  • Run the command again — the failure can be a timing issue", fg='cyan', err=True)
                    click.secho("  • Use --rtt instead of --rtt-reset when the device already runs", fg='cyan', err=True)
                    click.secho("  • Verifying target is connected and powered", fg='cyan', err=True)

                client.close()
                ctx.exit(1)
            except Exception as e:
                click.secho(f"Error: Failed to reset device: {e}", fg='red', err=True)
                client.close()
                ctx.exit(1)

            # Wait for device to reset and firmware to boot
            # This delay ensures:
            # 1. Device completes reset cycle (~500ms)
            # 2. Firmware boots and initializes RTT control block (~1-2s)
            # 3. J-Link detects RTT control block in RAM (~500ms)
            # 4. J-Link RTT telnet server becomes available (~500ms)
            import time
            if not quiet:
                click.echo("Waiting for RTT initialization...", err=True)
            time.sleep(3.5)  # Increased from 2.0s to 3.5s for better reliability

        if interactive:
            # Bi-directional session over the box's /rtt WebSocket namespace
            # (:9000): the up-channel streams raw to stdout (defmt-pipeable)
            # while stdin is forwarded to the target's RTT down-channel. The
            # gdbserver was already started above via /debug/connect on :8765.
            if not quiet:
                click.echo("Starting interactive RTT session...", err=True)
            client.close()
            from .rtt_websocket_client import connect_rtt_interactive
            exit_code = connect_rtt_interactive(
                f'http://{target_box}:9000',
                debug_net.get('name'),
                channel=rtt_channel,
                search_params=rtt_search_params,
            )
            ctx.exit(exit_code)

        # Stream RTT logs using the service endpoint (fast!)
        if not quiet:
            click.echo("Starting RTT stream...", err=True)

        try:
            # Stream RTT data to stdout
            for chunk in client.rtt(net=debug_net, channel=rtt_channel, timeout=None, **rtt_search_params):
                # Write directly to stdout in binary mode for maximum performance
                import sys
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except BrokenPipeError:
                    # Pipe closed (e.g., defmt-print exited) - exit gracefully
                    if not quiet:
                        click.echo("\nRTT stream stopped (pipe closed)", err=True)
                    break
        except KeyboardInterrupt:
            # User pressed Ctrl+C - graceful exit
            if not quiet:
                click.echo("\nRTT stream stopped", err=True)
        except BrokenPipeError:
            # Pipe closed - exit gracefully without error message
            # This happens when piped command (e.g., defmt-print) exits first
            if not quiet:
                click.echo("\nRTT stream stopped (pipe closed)", err=True)
        except Exception as e:
            click.secho(f"\nRTT stream error: {e}", fg='red', err=True)
        finally:
            client.close()
    elif reset:
        client.reset(debug_net, halt=False)
        if not quiet:
            click.secho("Reset complete", fg='green')
        client.close()
    else:
        client.close()

@click.command(cls=NetSubCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--keep-server', is_flag=True, default=False,
              help="Keep the GDB server running for external GDB client connections")
def disconnect(ctx, box, keep_server):
    """Stop the GDB server for the probe (JLinkGDBServer or OpenOCD)"""
    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Stop the GDB server (JLinkGDBServer or OpenOCD, per the net's backend)
    disc_result = client.disconnect(debug_net, keep_jlink_running=keep_server)
    server_label = _backend_server_label(disc_result)

    if keep_server:
        # Effective port comes from the box (per-probe slot for multi-J-Link).
        running_port = (disc_result or {}).get('gdb_port', 2331)
        # Only the recorded gateway mapping decides this, never a probe: the
        # server is being kept for a debugger that may still be attached,
        # and a probe would open a second connection to it.
        if auth_server_for_box(target_box):
            box_label = box if box is not None else target_box
            click.secho(f"{server_label} still running on {box_label}, port {running_port}", fg='green')
            click.secho("This box is behind a gateway, so its GDB port is not "
                        "reachable directly.", fg='cyan')
            click.secho(f"To debug from here, run: lager debug {net_name or debug_net.get('name')} "
                        f"gdbserver --box {box_label}", fg='cyan')
            click.secho("It restarts the GDB server and opens a local tunnel to it.", fg='cyan')
        else:
            click.secho(f"{server_label} still running on {target_box}:{running_port}", fg='green')
            click.secho(f"You can connect with: arm-none-eabi-gdb firmware.elf -ex 'target extended-remote {target_box}:{running_port}'", fg='cyan')
    else:
        click.secho(f"{server_label} stopped", fg='green')

    client.close()


# Probe-side failures that /debug/flash reports inside a 200 response.
#
# flash_device() (box/lager/debug/api.py) is a generator that only yields the
# programmer's stdout -- it has no success channel, and the endpoint answers 200
# whether or not anything was programmed. So the returned text is the only
# evidence the CLI has, and without this check `lager debug flash` cannot fail
# short of an HTTP error: it printed "Flashed!" over a log saying
# "Could not connect to target", leaving the caller believing a blank part was
# programmed.
#
# A connect failure alone does NOT mean the flash failed. flash_device()
# re-establishes a gdbserver *after* programming, and that reconnect can fail on
# a part that was just programmed correctly -- so evidence of programming wins
# over any later connect noise. Only when there is no such evidence does a
# connect failure decide the verdict; anything else keeps its existing meaning,
# so an uncharacterised backend is never called bad.
#
# Match on whole stripped lines, never as a substring: J-Link's API trace prints
# `- 10.056ms returns "O.K."` in the middle of a FAILED session, so substring
# matching on success text reports success on exactly the run this exists to
# catch. Lines can carry a log prefix, hence the endswith.
#
# `Downloading file` is here as evidence that the Commander session attached
# and reached `loadfile` -- which is all this tuple is for: a connect error
# after it belongs to the post-flash reconnect. It does NOT prove anything was
# programmed. J-Link prints it before it downloads its flash RAMCode, and that
# download can still fail; `_FLASH_PROGRAMMING_FAILURE_SIGNATURES` below is
# checked first and decides that case.
_FLASH_PROGRAMMED_SIGNATURES = (
    'J-Link: Flash download:',   # J-Link, one line per programmed range
    'Downloading file',          # J-Link loadfile (attached, not programmed)
    'wrote ',                    # OpenOCD flash write_image
)

# J-Link lines that mean `loadfile` programmed nothing. These win over
# everything above, including `J-Link: Flash download:`: a failed RAMCode
# download is followed by `Unspecified error -1` and nothing else, and the box
# still answers 200.
#
# Mirrors `_PROGRAMMING_FAILED_RE` in box/lager/debug/api.py; keep the two in
# step. The texts are J-Link's own (JLinkExe / libjlinkarm). J-Link can wrap a
# line in a `****** Error: ` banner, which `_line_matches_programming_failure`
# drops before matching.
#
# Exact lines: J-Link also prints `Failed to download RAMCode for indirect
# memory access!` and `... used to read FPU registers.`, which are not flash
# programming failures, so `Failed to download RAMCode` is never a prefix.
#
# Verify failures (`Verification failed @ address ...`, `ERROR: Verify
# failed.`) are deliberately absent. On a DA1469x the cached-XIP compare
# reports a false one on a correctly programmed part unless the box ran its
# uncached read-back (LAGER_DA1469_UNCACHED_VERIFY, default off).
_FLASH_PROGRAMMING_FAILURE_LINES = (
    'Failed to download RAMCode!',
    'Failed to download RAMCode.',
    'Failed to prepare for programming.',
    'Error while programming flash: Programming failed.',
)
_FLASH_PROGRAMMING_FAILURE_PREFIXES = (
    'Verification of RAMCode failed',       # ... @ address 0x0080073C.
    'Error while determining flash info',   # ... (Bank @ 0x16000000)
)

# Mirrors `_CONNECT_FAILED_RE` in box/lager/debug/api.py, which the box already
# trusts to decide a J-Link session never attached. The two are deliberately
# kept in step: the CLI and the box must not disagree about what happened on
# the same wire.
#
# `Could not read CPUID register` is in the box's retry regex and NOT here, on
# purpose. J-Link emits it per access port -- `AP[0]: Skipped. Could not read
# CPUID register` -- while scanning, so on its own it does not establish that
# the session never attached. As a retry trigger that costs one extra attempt;
# here it would decide a command's exit code. Nothing is lost by excluding it:
# in every captured failure it appears alongside `Could not connect to
# target.`, which is matched below.
_CONNECT_FAILURE_SIGNATURES = (
    'ERROR: Could not connect to target.',
    'Could not connect to target.',
    'Could not connect to the target device.',
    'Cannot connect to target.',
    'Failed to power up DAP',
)

# Commander failing to use the probe at all, seen when a second J-Link client
# was driving the same probe: every later command is refused and the session
# exits normally, so an erase or flash that touched nothing looked complete.
# Unlike a connect failure these win over `Downloading file` in a flash --
# they come from the flash's own Commander session, never from the post-flash
# reconnect. Mirrors the tail of `_ATTACH_FAILED_RE` in box/lager/debug/api.py.
_PROBE_UNUSABLE_SIGNATURES = (
    'is not supported by the connected probe.',   # Selected interface (SWD) ...
    'Target connection not established yet but required for command.',
    'J-Link connection not established yet but required for command.',
    'Connecting to J-Link via USB...FAILED',
    'JLinkExe exited',   # the box: JLinkCommanderExited, the probe went away mid-session
)


# The GDB server a debug net's backend runs, keyed by the `backend` field the
# box returns. A box too old to send that field predates OpenOCD support, so
# it ran JLinkGDBServer.
_BACKEND_SERVER_LABELS = {'openocd': 'OpenOCD', 'jlink': 'JLinkGDBServer'}


def _backend_server_label(result):
    backend = result.get('backend') if isinstance(result, dict) else None
    return _BACKEND_SERVER_LABELS.get(backend, 'JLinkGDBServer')


def _http_error_detail(exc, prefix=None):
    """The box's `error` text for a failed request, else the exception text.

    `service_client._request` raises `requests.HTTPError`, whose own text is
    the status line; the box's message is in the JSON body. With `prefix`, a
    copy of it that the box already put at the front is dropped, so the CLI's
    own prefix is not printed twice.
    """
    detail = str(exc)
    # `is not None`: a Response is falsy for any 4xx/5xx status.
    response = getattr(exc, 'response', None)
    if response is not None:
        try:
            body = response.json()
        except Exception:
            body = None
        if isinstance(body, dict) and body.get('error'):
            detail = str(body['error'])
    if prefix and detail.startswith(prefix):
        detail = detail[len(prefix):].lstrip()
    return detail


def _joined_output(output):
    """Box output is a str, or a list of lines in verbose mode."""
    if isinstance(output, list):
        return '\n'.join(str(line) for line in output)
    return output or ''


def _line_matches(line, signatures):
    stripped = line.strip()
    return any(stripped == sig or stripped.endswith(sig) or stripped.startswith(sig)
               for sig in signatures)


_JLINK_ERROR_BANNER_RE = re.compile(r'^[*\s]*(?:error:\s*)?', re.IGNORECASE)


def _line_matches_programming_failure(line):
    """True if `line` is one of J-Link's "nothing was programmed" lines.

    Drops J-Link's `****** Error: ` / `ERROR: ` banner, then matches the whole
    remaining line or its start -- never a substring, like `_line_matches`.
    """
    text = _JLINK_ERROR_BANNER_RE.sub('', line.strip(), count=1)
    return (text in _FLASH_PROGRAMMING_FAILURE_LINES
            or text.startswith(_FLASH_PROGRAMMING_FAILURE_PREFIXES))


_NO_FLASH_DOWNLOAD = ('J-Link printed `Downloading file` but no `Flash download` '
                      'line after it: nothing was programmed')
_NO_LOADFILE = ('J-Link printed no `Downloading file` line: `loadfile` never ran, '
                'so nothing was programmed')
_NO_ERASE_DONE = 'J-Link printed no `Erasing done.` line'

# The box's first line of every J-Link flash.
_JLINK_FLASH_BANNER = ' via JLinkExe...'


def _downloaded_without_flash_download(lines):
    """True if a J-Link `Downloading file` has no `Flash download` line (or
    bare `O.K.`) after it, before the next one or the end: `loadfile` never
    reached flash.

    J-Link prints `J-Link: Flash download: Bank ...` for every bank it touches,
    `Skipped. Contents already match` included, so a flash that did anything
    always has one. Mirrors `_flash_failure` in box/lager/debug/api.py.
    """
    downloading = False
    for line in lines:
        text = line.strip()
        if text.startswith('Downloading file'):
            if downloading:
                return True
            downloading = True
        elif downloading and (text.startswith('J-Link: Flash download:')
                              or text.startswith('Flash download:')
                              or text == 'O.K.'):
            downloading = False
    return downloading


def _jlink_flash_without_loadfile(lines):
    """True for a J-Link flash (the box's `... via JLinkExe...` line) that
    never printed `Downloading file`. A J-Link that dropped off USB
    mid-session left exactly this: the banner, then nothing."""
    stripped = [line.strip() for line in lines]
    return (any(text.endswith(_JLINK_FLASH_BANNER) for text in stripped)
            and not any(text.startswith('Downloading file') for text in stripped))


def _flash_failure_line(output):
    """Return the programmer's failure line from flash output, else None.

    `output` is the joined /debug/flash text. A programming failure (a failed
    RAMCode download, say) or an unusable probe is returned whatever else the
    output says, and so is a `Downloading file` with no `Flash download` after
    it. Short of that, returns None whenever the output shows the session
    reached programming, even if a later line reports a connect failure --
    that is the post-flash gdbserver, not the flash. Last, a J-Link flash that
    never ran `loadfile` at all fails: that is what a probe dropping off USB
    mid-session leaves.
    """
    lines = (output or '').splitlines()
    for line in lines:
        if (_line_matches_programming_failure(line)
                or _line_matches(line, _PROBE_UNUSABLE_SIGNATURES)):
            return line.strip()
    if _downloaded_without_flash_download(lines):
        return _NO_FLASH_DOWNLOAD
    if any(_line_matches(line, _FLASH_PROGRAMMED_SIGNATURES) for line in lines):
        return None
    for line in lines:
        if _line_matches(line, _CONNECT_FAILURE_SIGNATURES):
            return line.strip()
    if _jlink_flash_without_loadfile(lines):
        return _NO_LOADFILE
    return None


def _flash_verdict(result, output):
    """Return why /debug/flash programmed nothing, else None.

    A box that reports `programmed` decided that from the programming session
    alone, which is more precise than reading the whole log: it knows where
    programming ends and the post-flash reconnect begins. Older boxes do not
    send it, so their text is read instead.
    """
    if isinstance(result, dict) and 'programmed' in result:
        if result['programmed']:
            return None
        return result.get('error') or 'the box reported that nothing was programmed'
    return _flash_failure_line(output)


def _erase_failure_line(output):
    """Return the programmer's failure line from erase output, else None.

    `output` is the joined /debug/erase text. Unlike `_flash_failure_line`
    there is no programmed-evidence short-circuit: chip_erase() runs `connect`
    then `erase` and nothing after (box/lager/debug/jlink.py), so a connect
    failure in this text is always THIS erase's, never a later reconnect's.

    Output matching nothing keeps its existing meaning, so an older box or a
    backend we have not characterised is never newly reported as failing.
    """
    lines = (output or '').splitlines()
    for line in lines:
        if _line_matches(line, _CONNECT_FAILURE_SIGNATURES + _PROBE_UNUSABLE_SIGNATURES):
            return line.strip()
    # J-Link started an erase and never confirmed it. (A newer box refuses
    # this itself; this covers an older one.) Mirrors `_erase_failure` in
    # box/lager/debug/api.py.
    stripped = [line.strip() for line in lines]
    started = any(text in ('Erasing device...', 'Erasing selected range...')
                  for text in stripped)
    confirmed = any(text in ('Erasing done.', 'Mass erase done.')
                    or (text.startswith('Flash sectors within Range') and text.endswith('deleted.'))
                    for text in stripped)
    if started and not confirmed:
        return _NO_ERASE_DONE
    return None


_MIB = 1 << 20
# The DA1469x QSPI XIP window: the one `memrd` and the box's flash_loader use.
_DA1469X_XIP_START = 0x16000000
_DA1469X_XIP_END = 0x18000000  # exclusive

# The box's debug service lists `erase_range` under /health `features` once it
# reads erase_start/erase_size. A box that predates the keys ignores them and
# erases its default range instead -- the under-erase a deploy script cannot
# see -- so the flags are refused before the request is ever sent.
_ERASE_RANGE_UNSUPPORTED = (
    "Error: this box does not support --erase-start/--erase-size "
    "(requires box version 0.50.0 or later). Update it with: lager update --box {box}"
)


def _format_erase_range(start, size):
    """`0x16000000-0x161FFFFF (2 MiB)`, the form the box reports a range in."""
    end = start + size - 1
    if size % _MIB == 0:
        human = f'{size // _MIB} MiB'
    elif size % 1024 == 0:
        human = f'{size // 1024} KiB'
    else:
        human = f'{size} bytes'
    return f'0x{start:08X}-0x{end:08X} ({human})'


def _erase_range_error(device_type, erase_start, erase_size, *, no_erase=False):
    """Why this --erase-start/--erase-size pair is refused, or None.

    Checked before any box traffic: one flag without the other, either flag
    with --no-erase, a negative start, a range past the 32-bit address
    space, and on a DA1469x a range outside its QSPI XIP window. The box
    checks the same rules again for callers that bypass the CLI.
    """
    if erase_start is None and erase_size is None:
        return None
    if erase_start is None or erase_size is None:
        return "Error: --erase-start and --erase-size must be given together"
    if no_erase:
        return "Error: --no-erase cannot be combined with --erase-start/--erase-size"
    if erase_start < 0:
        return f"Error: --erase-start must not be negative, got {erase_start}"
    if erase_start + erase_size > 1 << 32:
        return (f"Error: erase range {_format_erase_range(erase_start, erase_size)} "
                f"runs past the end of the 32-bit address space")
    if 'DA1469' in str(device_type).upper():
        if erase_start < _DA1469X_XIP_START or erase_start + erase_size > _DA1469X_XIP_END:
            return (f"Error: erase range {_format_erase_range(erase_start, erase_size)} "
                    f"is outside the DA1469x QSPI XIP window "
                    f"(0x{_DA1469X_XIP_START:08X}-0x{_DA1469X_XIP_END - 1:08X})")
    return None


def _box_supports_erase_range(client):
    """True when the box's debug service lists `erase_range` under /health `features`.

    An older box answers with no `features` at all, and a box that cannot be
    reached reads the same: either way the flags are refused rather than sent
    to a box that would ignore them.
    """
    try:
        health = client.get_service_health()
    except Exception:
        return False
    features = health.get('features') if isinstance(health, dict) else None
    return 'erase_range' in (features or [])


def _erase_complete_line(result):
    """The success line, naming the range the box reports it erased.

    A box that predates `erase_range` sends no such key and keeps the old
    line. `None` is a full-chip erase.
    """
    if not isinstance(result, dict) or 'erase_range' not in result:
        return "Erase complete!"
    erased = result['erase_range']
    if erased is None:
        return "Erase complete: full chip"
    text = erased.get('text') if isinstance(erased, dict) else None
    return f"Erase complete: {text}" if text else "Erase complete!"


@click.command(cls=NetSubCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--hex', type=click.Path(exists=True))
@click.option('--elf', type=click.Path(exists=True))
@click.option('--bin', multiple=True, type=BinfileType(exists=True))
@click.option('--verbose', is_flag=True, default=False,
              help='Show detailed J-Link connection and flash output (slower)')
@click.option('--force-reconnect', is_flag=True, default=False,
              help='Force disconnect and reconnect before flash for clean state')
@click.option('--no-erase', is_flag=True, default=False,
              help='Skip erasing flash before flashing')
@click.option('--erase', is_flag=True, default=False, hidden=True,
              help='(Deprecated) Erase before programming — now the default behavior. '
                   'DA1469x erases external QSPI XIP range only, not full chip.')
@click.option('--erase-start', type=MemoryAddressType(), default=None, metavar='ADDR',
              help='First address of the pre-erase, hex (0x16000000) or decimal. '
                   'Given together with --erase-size.')
@click.option('--erase-size', type=ByteSizeType(), default=None, metavar='BYTES',
              help='Bytes to erase from --erase-start: decimal, 0x hex, or a K/M suffix (2M). '
                   'On a DA1469x the range must lie inside 0x16000000-0x17FFFFFF.')
@click.option('--halt/--no-halt', is_flag=True, default=False,
              help='Halt the device after flashing (keeps debugger connected)', show_default=True)
def flash(ctx, box, hex, elf, bin, verbose, force_reconnect, no_erase, erase,
          erase_start, erase_size, halt):
    """Flash firmware to target"""

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    jlink_script, openocd_config = _resolve_debug_scripts(
        ctx, net_name or debug_net.get('name'), debug_net
    )

    device_type = str(_debug_net_jlink_device(debug_net) or '').upper()

    # An explicit erase range is checked before any box traffic.
    range_error = _erase_range_error(device_type, erase_start, erase_size, no_erase=no_erase)
    if range_error:
        click.secho(range_error, fg='red', err=True)
        ctx.exit(1)
    erase_range_requested = erase_start is not None

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    if erase_range_requested and not _box_supports_erase_range(client):
        click.secho(_ERASE_RANGE_UNSUPPORTED.format(box=box or target_box), fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Auto-connect if not already connected
    if not _auto_connect_if_needed(
        client, debug_net, ctx,
        jlink_script=jlink_script, openocd_config=openocd_config,
    ):
        client.close()
        ctx.exit(1)

    # Erase flash before flashing (default behavior; skip with --no-erase).
    #
    # Nothing reconnects between the erase and the flash, for either backend.
    #
    # This used to disconnect and `connect(force=True)` here for non-DA1469x
    # parts, inside this try -- so a failed connect hit the handler below:
    # "Flash erase failed", exit 1, with the chip already erased and
    # /debug/flash never called. Against a just-erased nRF5340 that connect
    # answers 500 every time, and because `flash` erases by default, a plain
    # `lager debug NET flash --hex fw.hex` bricked the part it was asked to
    # program.
    #
    # The reconnect was never load-bearing:
    #
    #   J-Link  -- /debug/flash runs its own JLinkExe session. flash_device()
    #     opens with stop_jlink() + stop_jlink_gdbserver(), tearing down
    #     anything we start here ~0.5s later, then re-establishes a gdbserver
    #     itself after programming.
    #   OpenOCD -- /debug/erase leaves the daemon running and /debug/flash
    #     programs over that same daemon, answering 400 when it is gone. The
    #     disconnect actively removed the session the flash needed.
    #
    # The waits went with it: only a box-side delay can serialise the probe's
    # USB handle, and chip_erase() and flash_device() each already sleep after
    # releasing it. --force-reconnect still asks for a clean session, and that
    # path warns and continues rather than aborting.
    if not no_erase:
        try:
            if erase_range_requested:
                click.echo(
                    f"Erasing flash memory ({_format_erase_range(erase_start, erase_size)})...",
                    err=True,
                )
            else:
                click.echo("Erasing flash memory...", err=True)
            erase_result = client.erase(debug_net, speed='4000', transport='SWD',
                                        erase_start=erase_start, erase_size=erase_size)
            # /debug/erase answers 200 on the J-Link path whether or not the
            # probe ever attached, so the returned text is the only evidence
            # that anything was erased.
            erase_failure = _erase_failure_line(
                _joined_output(erase_result.get('output', '')))
            if erase_failure:
                click.secho(f"Flash erase failed: {erase_failure}", fg='red', err=True)
                client.close()
                ctx.exit(1)
            click.secho(_erase_complete_line(erase_result), fg='green', err=True)
        except click.exceptions.Exit:
            raise
        except Exception as e:
            click.secho(f"Flash erase failed: {_http_error_detail(e, 'Erase failed:')}",
                        fg='red', err=True)
            client.close()
            ctx.exit(1)

    # Force reconnect if requested for clean state
    if force_reconnect:
        try:
            click.echo("Forcing clean reconnect...", err=True)
            # Disconnect
            client.disconnect(debug_net)
            import time
            time.sleep(0.5)
            # Reconnect with force
            client.connect(
                debug_net, force=True, halt=False,
                jlink_script=jlink_script, openocd_config=openocd_config,
            )
            click.echo("Reconnect complete", err=True)
        except Exception as e:
            click.secho(f"Warning: Force reconnect failed: {e}", fg='yellow', err=True)
            # Continue anyway - user explicitly requested this

    # Flash firmware
    from pathlib import Path

    try:
        # Validate that only one file type is specified
        file_types_specified = sum([bool(hex), bool(elf), bool(bin)])
        if file_types_specified > 1:
            click.secho('Error: Cannot specify multiple file types (--hex, --elf, --bin)', fg='red', err=True)
            click.secho('Please specify only one file type option.', fg='red', err=True)
            ctx.exit(1)
        elif file_types_specified == 0:
            click.secho('Provide --hex, --elf, or --bin.', fg='red')
            ctx.exit(1)

        # Flash the appropriate file type
        if hex:
            result = client.flash(Path(hex), file_type='hex', verbose=verbose, net=debug_net,
                                  jlink_script=jlink_script, openocd_config=openocd_config)
        elif elf:
            result = client.flash(Path(elf), file_type='elf', verbose=verbose, net=debug_net,
                                  jlink_script=jlink_script, openocd_config=openocd_config)
        elif bin:
            if len(bin) > 1:
                click.secho("Multiple binary files not supported yet", fg='red', err=True)
                ctx.exit(1)
            bf = bin[0]
            result = client.flash(Path(bf.path), file_type='bin', address=bf.address, verbose=verbose,
                                      net=debug_net, jlink_script=jlink_script,
                                      openocd_config=openocd_config)

        # Display flash output if available
        output = result.get('output', '')
        if isinstance(output, list):
            # Output is a list of lines (verbose mode)
            output = '\n'.join(output)
        if output:
            click.echo(output)

        # /debug/flash answers 200 even when nothing was programmed. A newer
        # box says so in `programmed` / `error`; for an older one the returned
        # text is the only evidence.
        failure = _flash_verdict(result, output)
        if failure:
            click.secho(f"\nFlash failed: {failure}", fg='red', err=True)
            click.secho(
                "The target was NOT programmed. If this ran without --no-erase "
                "it is now erased.",
                fg='red', err=True,
            )
            client.close()
            ctx.exit(1)

        click.secho("\nFlashed!", fg='green')
        if erase and 'DA1469' in device_type:
            click.secho(
                "DA1469x: after erase, a cold halted attach before loadfile can fail. "
                "If boot fails after erase+flash, power cycle and flash again.",
                fg='cyan',
                dim=True,
                err=True,
            )
    except requests.exceptions.HTTPError as e:
        error_detail = _http_error_detail(e, 'Flash failed:')

        click.secho(f"Flash failed: {error_detail}", fg='red', err=True)
        client.close()
        ctx.exit(1)
    except (Exit, Abort):
        # Control flow, not a flash failure -- but the client still owns a
        # session on the box, so close it on the way out.
        client.close()
        raise
    except Exception as e:
        click.secho(f"Flash failed: {e}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Halt device if requested
    if halt:
        try:
            client.reset(debug_net, halt=True)
            click.secho("Flashed and halted!", fg='green')
        except Exception as e:
            click.secho(f"Warning: Failed to halt after flash: {e}", fg='yellow', err=True)

    # Keep debugger connected (no auto-disconnect)
    client.close()

@click.command(cls=NetSubCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--speed', type=str, default='4000', callback=validate_speed_param,
              help='SWD/JTAG speed in kHz (default: 4000)')
@click.option('--yes', is_flag=True, default=False,
              help='Skip confirmation prompt')
@click.option('--quiet', is_flag=True, default=False,
              help='Suppress warning messages')
@click.option('--json', 'json_output', is_flag=True, default=False,
              help='Output results in JSON format')
@click.option('--erase-start', type=MemoryAddressType(), default=None, metavar='ADDR',
              help='First address to erase, hex (0x16000000) or decimal. '
                   'Given together with --erase-size.')
@click.option('--erase-size', type=ByteSizeType(), default=None, metavar='BYTES',
              help='Bytes to erase from --erase-start: decimal, 0x hex, or a K/M suffix (2M). '
                   'On a DA1469x the range must lie inside 0x16000000-0x17FFFFFF.')
@click.option('--halt/--no-halt', is_flag=True, default=False,
              help='Halt the device after erase (keeps debugger connected)', show_default=True)
def erase(ctx, box, speed, yes, quiet, json_output, erase_start, erase_size, halt):
    """Erase flash memory on target"""

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)
    jlink_script, openocd_config = _resolve_debug_scripts(
        ctx, net_name or debug_net.get('name'), debug_net
    )
    device_type = _debug_net_jlink_device(debug_net) or 'unknown'

    # An explicit erase range is checked before any box traffic.
    range_error = _erase_range_error(device_type, erase_start, erase_size)
    if range_error:
        click.secho(range_error, fg='red', err=True)
        ctx.exit(1)
    erase_range_requested = erase_start is not None

    # Confirm the erase operation (skip if quiet or json mode)
    if not yes and not quiet and not json_output:
        if erase_range_requested:
            click.echo(
                f"WARNING: This will erase {_format_erase_range(erase_start, erase_size)} "
                f"on {device_type}"
            )
        elif 'DA1469' in str(device_type).upper():
            click.echo(
                f"WARNING: On {device_type} this erases the external QSPI XIP range "
                f"(J-Link address-range erase), not internal flash."
            )
        else:
            click.echo(f"WARNING: This will erase ALL flash memory on {device_type}")
        click.echo("This operation cannot be undone!")
        if not click.confirm("Do you want to continue?"):
            click.echo("Chip erase cancelled.")
            ctx.exit(0)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    if erase_range_requested and not _box_supports_erase_range(client):
        click.secho(_ERASE_RANGE_UNSUPPORTED.format(box=box or target_box), fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Auto-connect if not already connected
    if not _auto_connect_if_needed(
        client, debug_net, ctx, quiet=quiet,
        jlink_script=jlink_script, openocd_config=openocd_config,
    ):
        client.close()
        ctx.exit(1)

    # Execute erase
    if not quiet:
        if erase_range_requested:
            click.echo(
                f"Erasing flash memory ({_format_erase_range(erase_start, erase_size)})..."
            )
        else:
            click.echo("Erasing flash memory...")

    try:
        result = client.erase(debug_net, speed=speed, transport='SWD',
                              erase_start=erase_start, erase_size=erase_size)
    except requests.exceptions.HTTPError as e:
        error_detail = _http_error_detail(e, 'Erase failed:')

        click.secho(f"Erase failed: {error_detail}", fg='red', err=True)
        client.close()
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Erase failed: {_http_error_detail(e, 'Erase failed:')}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # /debug/erase answers 200 on the J-Link path whether or not the probe ever
    # attached -- the box's chip_erase() is a generator that yields JLinkExe's
    # output and carries no success channel -- so the returned text is the only
    # evidence that anything was erased. The OpenOCD path already raises.
    erase_output = _joined_output(result.get('output', ''))
    failure = _erase_failure_line(erase_output)
    if failure:
        if json_output:
            click.echo(json.dumps(result, indent=2))
        elif erase_output:
            click.echo(erase_output)
        click.secho(f"\nErase failed: {failure}", fg='red', err=True)
        click.secho(
            "The target was NOT erased. Check that it is connected and powered.",
            fg='red', err=True,
        )
        client.close()
        ctx.exit(1)

    # Output results
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif not quiet:
        click.secho(_erase_complete_line(result), fg='green')

    # Erase internally disconnects (requires exclusive hardware access via JLinkExe)
    # Always reconnect to restore debugger connection (force=True so gdbserver + script
    # re-init cleanly; avoids stale session after JLinkExe on DA1469x).
    import time
    time.sleep(0.5)  # Give hardware time to be released
    if not quiet:
        click.secho("Reconnecting debugger after erase...", fg='cyan', dim=True)
    try:
        try:
            client.disconnect(debug_net)
        except Exception:
            pass
        time.sleep(0.3)
        client.connect(
            debug_net, speed=None, force=True, halt=halt,
            jlink_script=jlink_script, openocd_config=openocd_config,
        )
        if halt:
            if not quiet:
                click.secho("Reconnected and halted!", fg='cyan', dim=True)
        else:
            if not quiet:
                click.secho("Reconnected!", fg='cyan', dim=True)
    except Exception as e:
        click.secho(f"Warning: Failed to reconnect after erase: {e}", fg='yellow', err=True)

    # Keep debugger connected (no auto-disconnect)
    client.close()

@click.command(cls=NetSubCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--halt/--no-halt', is_flag=True, default=False,
              help='Halt the device after reset (keeps debugger connected)', show_default=True)
@click.option('--force-reconnect', is_flag=True, default=False,
              help='Force disconnect and reconnect before reset for clean state')
def reset(ctx, box, halt, force_reconnect):
    """Reset target"""

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    jlink_script, openocd_config = _resolve_debug_scripts(
        ctx, net_name or debug_net.get('name'), debug_net
    )

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Auto-connect if not already connected (unless force-reconnect, which handles its own connection)
    if not force_reconnect:
        if not _auto_connect_if_needed(
            client, debug_net, ctx,
            jlink_script=jlink_script, openocd_config=openocd_config,
        ):
            client.close()
            ctx.exit(1)

    # Force reconnect if requested for clean state
    if force_reconnect:
        try:
            click.echo("Forcing clean reconnect...", err=True)
            client.disconnect(debug_net)
            import time
            time.sleep(0.5)
            client.connect(
                debug_net, force=True, halt=False,
                jlink_script=jlink_script, openocd_config=openocd_config,
            )
            click.echo("Reconnect complete", err=True)
        except Exception as e:
            click.secho(f"Warning: Force reconnect failed: {e}", fg='yellow', err=True)

    # Reset device
    try:
        result = client.reset(debug_net, halt=halt)
        if halt:
            click.secho("Reset complete (halted, debugger connected)", fg='green')
        else:
            click.secho("Reset complete (running, debugger connected)", fg='green')
    except Exception as e:
        error_msg = str(e)
        if "400" in error_msg or "No debugger connection" in error_msg:
            click.secho("Error: No debugger connection found. Start GDB server first with: lager debug [NET_NAME] gdbserver --box [BOX_NAME]", fg='red', err=True)
        else:
            click.secho(f"Error: {e}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Keep debugger connected (no auto-disconnect)
    client.close()

@click.command(cls=NetSubCommand, short_help="Read memory from target")
@click.pass_context
@click.argument('start_addr', type=MemoryAddressType())
@click.argument('length', type=MemoryAddressType())
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--json', 'json_output', is_flag=True, default=False,
              help='Output results in JSON format')
@click.option('--halt', is_flag=True, default=False,
              help='Halt the device during memory read (keeps debugger connected).')
@click.option('--no-halt', 'no_halt', is_flag=True, default=False,
              help='Leave the target running; overrides auto-halt for DA1469 QSPI XIP.')
@click.option('--no-reset', 'no_reset', is_flag=True, default=False,
              help='DA1469x only: skip the reset+halt the box performs before the read. '
                   'A running DA1469x has SWD disabled, so without reset the read fails; '
                   'use this only on a blank/awake part to avoid rebooting it.')
def memrd(ctx, start_addr, length, box, json_output, halt, no_halt, no_reset):
    """Read memory from target.

    DA1469x QSPI XIP auto-halts unless --no-halt. For any DA1469x device the box
    also resets+halts the target before reading (real firmware disables SWD and
    deep-sleeps, so a plain read fails) — this REBOOTS the DUT. Pass --no-reset
    to skip that step (only safe on a blank/awake part).
    """

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    jlink_script, openocd_config = _resolve_debug_scripts(
        ctx, net_name or debug_net.get('name'), debug_net
    )

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    if halt and no_halt:
        click.secho("Error: use only one of --halt or --no-halt", fg='red', err=True)
        client.close()
        ctx.exit(1)

    device_jlink = str(_debug_net_jlink_device(debug_net) or '').upper()
    da1469_qspi_xip = (
        'DA1469' in device_jlink
        and 0x16000000 <= start_addr < 0x18000000
    )
    if halt:
        effective_halt = True
    elif no_halt:
        effective_halt = False
    else:
        # Unhalted XIP reads often show 0xfc / garbage; default to halt for verify-style reads.
        effective_halt = da1469_qspi_xip

    if da1469_qspi_xip and effective_halt and not halt and not no_halt:
        click.secho(
            "DA1469x QSPI XIP: auto-halting for this read (use --no-halt to leave CPU running).",
            fg='cyan',
            dim=True,
            err=True,
        )

    # Ensure halted attach when we need it (reconnect if already connected running).
    if effective_halt and _is_connected(client, debug_net):
        try:
            client.connect(
                debug_net, speed=None, force=True, halt=True,
                jlink_script=jlink_script, openocd_config=openocd_config,
            )
        except Exception as e:
            click.secho(f"Warning: the CLI did not re-connect halted for memrd: {e}", fg='yellow', err=True)

    # Auto-connect if the target is not answering. A live gdbserver is not an
    # attached part; see _auto_connect_if_needed for why this tests `is True`
    # and treats None as "fall back to the old behaviour".
    _memrd_attached = _target_attached(client, debug_net)
    _memrd_needs_connect = (
        _memrd_attached is False
        or (_memrd_attached is None and not _is_connected(client, debug_net))
    )
    if _memrd_needs_connect:
        if effective_halt:
            click.secho("Auto-connecting to debugger (with halt for memory read)...", fg='cyan', dim=True)
        else:
            click.secho("Auto-connecting to debugger...", fg='cyan', dim=True)
        try:
            client.connect(
                debug_net, speed=None, force=(_memrd_attached is False),
                halt=effective_halt,
                jlink_script=jlink_script, openocd_config=openocd_config,
            )
            if effective_halt:
                click.secho("Auto-connected and halted!", fg='cyan', dim=True)
            else:
                click.secho("Auto-connected!", fg='cyan', dim=True)
        except Exception as e:
            click.secho(f"Error: Failed to auto-connect to debugger", fg='red', err=True)
            click.secho(f"Details: {e}", fg='red', err=True)
            client.close()
            ctx.exit(1)

    # Validate memory address range (32-bit systems)
    max_address = 0xFFFFFFFF
    if start_addr > max_address or (start_addr + length) > max_address + 1:
        click.secho(f"Warning: the read range from 0x{start_addr:x} passes the 32-bit maximum", fg='yellow', err=True)
        click.secho(f"Maximum valid address is 0x{max_address:x}", fg='yellow', err=True)
        if not click.confirm("Continue anyway?", default=False):
            client.close()
            ctx.exit(0)

    try:
        memory_data = client.read_memory(debug_net, start_addr, length, no_reset=no_reset)
    except Exception as e:
        error_msg = str(e)
        if "400" in error_msg or "No debugger connection" in error_msg:
            click.secho("Error: No debugger connection found. Start GDB server first with: lager debug [NET_NAME] gdbserver --box [BOX_NAME] --halt", fg='red', err=True)
            click.secho("Note: Device must be halted for memory reads to work reliably", fg='yellow', err=True)
        else:
            click.secho(f"Error reading memory: {e}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Check if memory read returned empty data (silent failure)
    if not memory_data or len(memory_data) == 0:
        click.secho(f"Error: Memory read returned no data from address 0x{start_addr:08x}", fg='red', err=True)
        click.secho("Possible causes:", fg='yellow', err=True)
        click.secho(
            "  • Device is not halted (DA1469 QSPI XIP auto-halts; else use --halt)",
            fg='yellow',
            err=True,
        )
        click.secho("  • Invalid memory address for this device", fg='yellow', err=True)
        click.secho("  • Memory region is not accessible or not mapped", fg='yellow', err=True)
        client.close()
        ctx.exit(1)

    # Format output
    if json_output:
        result = {
            "start_addr": hex(start_addr),
            "length": length,
            "data": []
        }
        for i in range(0, len(memory_data), 8):
            chunk = memory_data[i:i+8]
            hex_values = '\t'.join([f'0x{b:02x}' for b in chunk])
            result["data"].append(f'{hex(start_addr + i)}:\t{hex_values}')
        click.echo(json.dumps(result, indent=2))
    else:
        for i in range(0, len(memory_data), 8):
            chunk = memory_data[i:i+8]
            hex_values = '\t'.join([f'0x{b:02x}' for b in chunk])
            click.echo(f'{hex(start_addr + i)}:\t{hex_values}')

    # Keep debugger connected (no auto-disconnect)
    client.close()

# Note: gdbserver command removed as it relies on WebSocket tunneling
# For direct debugging, users should use gdb directly with the J-Link GDB server
# running on the box, which can be accessed via SSH port forwarding if needed

@click.command(cls=NetSubCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
def status(ctx, box):
    """Show debug net status and information"""
    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Get info from service
    info_data = client.get_info(debug_net, probe=True)

    running = info_data.get('gdbserver_running', info_data.get('connected'))
    attached = info_data.get('target_attached')
    if attached is True:
        attached_text = 'Yes'
    elif attached is False:
        attached_text = 'No'
    else:
        # An older box does not report it, and a probe can be inconclusive.
        # Say so rather than printing a confident No.
        attached_text = 'Unknown'

    click.echo(f"Debug Net Information:")
    click.echo(f"  Name: {info_data.get('net_name')}")
    click.echo(f"  Device Type: {info_data.get('device')}")
    click.echo(f"  Architecture: {info_data.get('arch')}")
    click.echo(f"  Probe: {info_data.get('probe')}")
    click.echo(f"  GDB server running: {running}")
    click.echo(f"  Target attached: {attached_text}")
    click.echo()

    client.close()


@click.command(cls=NetSubCommand)
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--verbose', is_flag=True, default=False,
              help='Show detailed health information')
def health(ctx, box, verbose):
    """
    Check debug service health
    """

    target_box = box

    # Get net_name from parent context (though health doesn't need it)
    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    try:
        # Get health information
        health_data = client.get_service_health(detailed=verbose)

        # Display health information
        click.echo(f"Debug Service Health:")
        click.echo(f"  Status: ", nl=False)
        if health_data.get('status') == 'healthy':
            click.secho(f"{health_data['status']}", fg='green')
        else:
            click.secho(f"{health_data['status']}", fg='red')

        click.echo(f"  Version: {health_data.get('version', 'unknown')}")
        # What the service can do beyond its original request shapes; a box
        # that predates the list reports none, and the CLI refuses the flags
        # that depend on a feature it does not see.
        features = health_data.get('features')
        click.echo(f"  Features: {', '.join(features) if features else 'none reported'}")

        if verbose:
            # Detailed information
            uptime_days = health_data.get('service_uptime_days', 0)
            click.echo(f"  Uptime: {uptime_days:.2f} days ({health_data.get('service_uptime_seconds', 0):.0f}s)")
            click.echo(f"  J-Link Running: {health_data.get('jlink_running', False)}")
            if health_data.get('jlink_pid'):
                click.echo(f"  J-Link PID: {health_data['jlink_pid']}")
            click.echo(f"  GDB Controllers Cached: {health_data.get('gdb_controllers_cached', 0)}")
            click.echo(f"  GDB Max Use Count: {health_data.get('gdb_max_use_count', 0)}")
            click.echo(f"  Active Connections: {health_data.get('active_connections', 0)}")

            # Display warnings if any
            warnings = health_data.get('warnings', [])
            if warnings:
                click.echo()
                click.secho("Warnings:", fg='yellow')
                for warning in warnings:
                    click.secho(f"  [WARNING] {warning}", fg='yellow')
        else:
            # Basic information
            uptime_seconds = health_data.get('uptime', 0)
            uptime_hours = uptime_seconds / 3600
            if uptime_hours < 1:
                click.echo(f"  Uptime: {uptime_seconds / 60:.1f} minutes")
            elif uptime_hours < 48:
                click.echo(f"  Uptime: {uptime_hours:.1f} hours")
            else:
                click.echo(f"  Uptime: {uptime_hours / 24:.1f} days")

    except Exception as e:
        click.secho(f"Error getting health: {e}", fg='red', err=True)
        ctx.exit(1)
    finally:
        client.close()

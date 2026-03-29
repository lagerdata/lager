# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.debug.commands

    Debug an elf file - Updated for direct SSH execution
"""
import itertools
import click
import json
import io
import requests
import signal
import sys
from contextlib import redirect_stdout
from texttable import Texttable
from ....context import get_default_box, get_impl_path, get_default_net
from ..python import run_python_internal
from ....core.param_types import MemoryAddressType, HexArrayType, BinfileType
from ....box_storage import get_box_ip, get_box_name_by_ip, get_box_user
from .service_client import DebugServiceClient
from .net_cache import get_net_cache

DEBUG_ROLE = "debug"


def _get_jlink_script_content(ctx, net_name, debug_net):
    """
    Get base64-encoded J-Link script content only if a local override exists.

    The box debug service now reads the stored script directly from NetsCache
    (saved_nets.json), so the CLI only needs to send the script when there's
    a local .lager config override.

    Args:
        ctx: Click context
        net_name: Name of the debug net
        debug_net: Debug net configuration dict (unused, kept for API compat)

    Returns:
        Base64-encoded script content, or None if no local override
    """
    import base64
    from ....config import get_debug_script_for_net

    script_path = get_debug_script_for_net(net_name)
    if script_path:
        try:
            with open(script_path, 'rb') as f:
                return base64.b64encode(f.read()).decode('ascii')
        except Exception as e:
            click.secho(f"Warning: Could not read J-Link script from config: {e}", fg='yellow', err=True)

    return None


def _resolve_box_with_username(ctx, box):
    """
    Resolve box parameter to (IP, username) tuple.
    Handles both box names and direct IPs, looking up username from storage.

    Args:
        ctx: Click context
        box: Box name or IP address

    Returns:
        Tuple of (ip_address, username)
    """
    from ....box_storage import resolve_and_validate_box

    # Resolve and validate the box name/IP
    box_ip = resolve_and_validate_box(ctx, box)

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

    # Cache miss - fetch from box
    # Run net.py list to get available nets
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_python_internal(
                ctx, get_impl_path("net.py"), box,
                env={}, passenv=(), kill=False, download=(),
                allow_overwrite=False, signum="SIGTERM", timeout=0,
                detach=False, port=(), org=None, args=("list",)
            )
    except SystemExit:
        pass

    try:
        nets = json.loads(buf.getvalue() or "[]")
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
                click.secho("No debug nets found. Create one with: lager nets create <name> debug <device_type> <address>", fg='red', err=True)
                ctx.exit(1)
            target_net = debug_nets[0]

        # Cache the result before returning
        cache.set(box, net_name, target_net)
        return target_net

    except json.JSONDecodeError:
        click.secho("Failed to parse nets information.", fg='red', err=True)
        ctx.exit(1)

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
        click.secho("  - Debug service is not running on the box", err=True)
        click.secho("  - Docker container 'lager' is not running", err=True)
        click.secho(f"Check with: ssh lagerdata@{box} 'docker ps | grep lager'", err=True)
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
            click.secho("The debug service may not be running. Check Docker status on the box.", err=True)
        elif "timeout" in error_str or "timed out" in error_str:
            click.secho(f"Error: Connection timed out to debug service on {box}:8765", fg='red', err=True)
            click.secho("The box may be offline or unreachable.", err=True)
        elif "name or service not known" in error_str or "nodename nor servname" in error_str:
            click.secho(f"Error: Could not resolve hostname '{box}'", fg='red', err=True)
            click.secho("Check that the box name or IP address is correct.", err=True)
        else:
            click.secho(f"Error: Failed to create debug service client: {e}", fg='red', err=True)
        return None

def _is_connected(client):
    """
    Check if debugger is currently connected.

    Args:
        client: DebugServiceClient instance

    Returns:
        True if connected, False otherwise
    """
    try:
        status = client.get_debug_status()
        return status.get('connected', False)
    except Exception:
        return False

def _auto_connect_if_needed(client, debug_net, ctx, quiet=False, jlink_script=None):
    """
    Auto-connect to debugger if not already connected.
    Does NOT reconnect if already connected.

    Args:
        client: DebugServiceClient instance
        debug_net: Debug net configuration
        ctx: Click context
        quiet: Suppress informational messages
        jlink_script: Optional base64-encoded J-Link script (local .lager override)

    Returns:
        True if connected (either already or newly), False on failure
    """
    # Check if already connected
    if _is_connected(client):
        return True

    # Not connected, auto-connect
    if not quiet:
        click.secho("Auto-connecting to debugger...", fg='cyan', dim=True)

    try:
        client.connect(debug_net, speed=None, force=False, halt=False, jlink_script=jlink_script)
        if not quiet:
            click.secho("Auto-connected!", fg='cyan', dim=True)
        return True
    except requests.exceptions.Timeout:
        click.secho("Error: Connection timed out while auto-connecting to debugger", fg='red', err=True)
        click.secho("The debug service may be unresponsive. Try again or check the box.", err=True)
        return False
    except requests.exceptions.ConnectionError as e:
        click.secho("Error: Connection failed while auto-connecting to debugger", fg='red', err=True)
        error_str = str(e).lower()
        if "connection refused" in error_str:
            click.secho("The debug service may not be running.", err=True)
        elif "name or service not known" in error_str:
            click.secho("Could not resolve the box hostname.", err=True)
        else:
            click.secho(f"Details: {e}", err=True)
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


def _run_net_py(ctx: click.Context, box: str, *args: str) -> list[dict]:
    """Run net.py to get list of nets."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_python_internal(
                ctx,
                get_impl_path("net.py"),
                box,
                env={},
                passenv=(),
                kill=False,
                download=(),
                allow_overwrite=False,
                signum="SIGTERM",
                timeout=0,
                detach=False,
                port=(),
                org=None,
                args=args or ("list",),
            )
    except SystemExit:
        pass
    raw = buf.getvalue() or "[]"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _list_debug_nets(ctx, box):
    """Get list of debug nets from box."""
    recs = _run_net_py(ctx, box, "list")
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


class NetDebugGroup(click.MultiCommand):
    """Custom multi-command that treats first argument as net name"""

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
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def _debug(ctx, box):
    """
    Debug firmware and manage debug sessions
    """
    # Net name extraction is handled by NetDebugGroup.resolve_command()
    # Listing nets when --box is provided without subcommand is handled by NetDebugGroup.invoke()
    pass


@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
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
@click.option('--reset', is_flag=True, default=False,
              help='Reset the device after starting GDB server')
@click.option('--gdb-port', type=int, default=2331,
              help='GDB server port (default: 2331)')
@click.option('--rtt-search-addr', type=str, default=None,
              help='RAM start address for RTT control block search (hex, e.g., 0x20020000)')
@click.option('--rtt-search-size', type=str, default=None,
              help='Size of RAM region to search for RTT control block (hex, e.g., 0x4000)')
@click.option('--rtt-chunk-size', type=str, default=None,
              help='Read chunk size for RTT search (hex, e.g., 0x1000)')
def gdbserver(ctx, box, force, halt, speed, quiet, json_output, rtt, rtt_reset, reset, gdb_port,
              rtt_search_addr, rtt_search_size, rtt_chunk_size):
    """Start JLinkGDBServer for debugging"""
    # Validate GDB port range
    if gdb_port < 1 or gdb_port > 65535:
        click.secho(f"Error: GDB port must be between 1 and 65535, got {gdb_port}", fg='red', err=True)
        ctx.exit(1)
    if gdb_port < 1024:
        click.secho(f"Warning: Port {gdb_port} is a privileged port (< 1024). May require root privileges.", fg='yellow', err=True)

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    # Get J-Link script content if configured
    jlink_script = _get_jlink_script_content(ctx, net_name or debug_net.get('name'), debug_net)

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
        result = client.connect(debug_net, speed=speed, force=force, halt=halt, gdb=True, gdb_port=gdb_port, jlink_script=jlink_script)
    except requests.exceptions.HTTPError as e:
        # Parse error response for more details
        error_detail = "Unknown error"
        try:
            error_json = e.response.json()
            error_detail = error_json.get('error', str(e))
        except:
            error_detail = str(e)

        click.secho("Error: Failed to connect to debugger", fg='red', err=True)

        # Check for common connection issues
        if "500" in str(e) or "Internal Server Error" in str(e):
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

    if not quiet:
        if json_output:
            click.echo(json.dumps(result, indent=2))
        else:
            # Display GDB server info
            if 'gdb_server' in result:
                gdb_info = result['gdb_server']
                if gdb_info.get('status') == 'started':
                    click.secho("JLinkGDBServer started!", fg='green', err=True)
                    click.secho(f"GDB server listening on {target_box}:{gdb_port}", fg='cyan', err=True)
                    click.secho(f"Connect with: arm-none-eabi-gdb -ex 'target remote {target_box}:{gdb_port}'", fg='cyan', err=True)
                elif gdb_info.get('status') == 'already_running':
                    click.secho("JLinkGDBServer already running!", fg='green', err=True)
                    click.secho(f"GDB server listening on {target_box}:{gdb_port}", fg='cyan', err=True)
                    click.secho(f"Connect with: arm-none-eabi-gdb -ex 'target remote {target_box}:{gdb_port}'", fg='cyan', err=True)
                elif 'error' in gdb_info:
                    click.secho(f"Error: GDB server failed to start: {gdb_info.get('message', 'Unknown error')}", fg='red', err=True)
                    ctx.exit(1)
            else:
                click.secho("JLinkGDBServer started!", fg='green', err=True)
                click.secho(f"GDB server listening on {target_box}:{gdb_port}", fg='cyan', err=True)
                click.secho(f"Connect with: arm-none-eabi-gdb -ex 'target remote {target_box}:{gdb_port}'", fg='cyan', err=True)

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
                    click.secho("  • Running the command again (may be a timing issue)", fg='cyan', err=True)
                    click.secho("  • Using --rtt instead of --rtt-reset if device is already running", fg='cyan', err=True)
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

        # Stream RTT logs using the service endpoint (fast!)
        if not quiet:
            click.echo("Starting RTT stream...", err=True)

        try:
            # Stream RTT data to stdout
            for chunk in client.rtt(net=debug_net, channel=0, timeout=None, **rtt_search_params):
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

@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--keep-server', is_flag=True, default=False,
              help="Keep JLinkGDBServer running for external GDB client connections")
def disconnect(ctx, box, keep_server):
    """Stop JLinkGDBServer"""
    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Stop JLinkGDBServer
    client.disconnect(debug_net, keep_jlink_running=keep_server)

    if keep_server:
        click.secho(f"JLinkGDBServer still running on {target_box}:2331", fg='green')
        click.secho(f"You can connect with: arm-none-eabi-gdb firmware.elf -ex 'target extended-remote {target_box}:2331'", fg='cyan')
    else:
        click.secho("JLinkGDBServer stopped", fg='green')

    client.close()

@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--hex', type=click.Path(exists=True))
@click.option('--elf', type=click.Path(exists=True))
@click.option('--bin', multiple=True, type=BinfileType(exists=True))
@click.option('--verbose', is_flag=True, default=False,
              help='Show detailed J-Link connection and flash output (slower)')
@click.option('--force-reconnect', is_flag=True, default=False,
              help='Force disconnect and reconnect before flash for clean state')
@click.option('--erase', is_flag=True, default=False,
              help='Erase all flash before flashing (ensures clean boot state for RTT)')
@click.option('--halt/--no-halt', is_flag=True, default=False,
              help='Halt the device after flashing (keeps debugger connected)', show_default=True)
def flash(ctx, box, hex, elf, bin, verbose, force_reconnect, erase, halt):
    """Flash firmware to target"""

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    jlink_script = _get_jlink_script_content(ctx, net_name or debug_net.get('name'), debug_net)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Auto-connect if not already connected
    if not _auto_connect_if_needed(client, debug_net, ctx, jlink_script=jlink_script):
        client.close()
        ctx.exit(1)

    # Erase flash if requested (ensures clean state for RTT and firmware initialization)
    if erase:
        try:
            click.echo("Erasing flash memory...", err=True)
            client.erase(debug_net, speed='4000', transport='SWD')
            click.secho("Erase complete!", fg='green', err=True)

            # Reconnect after erase (erase auto-disconnects)
            # Always reconnect here to continue with flash operation
            import time
            time.sleep(0.3)
            client.connect(debug_net, force=False, halt=False, jlink_script=jlink_script)
        except Exception as e:
            click.secho(f"Flash erase failed: {e}", fg='red', err=True)
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
            client.connect(debug_net, force=True, halt=False, jlink_script=jlink_script)
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
            result = client.flash(Path(hex), file_type='hex', verbose=verbose, net=debug_net)
        elif elf:
            result = client.flash(Path(elf), file_type='elf', verbose=verbose, net=debug_net)
        elif bin:
            if len(bin) > 1:
                click.secho("Multiple binary files not supported yet", fg='red', err=True)
                ctx.exit(1)
            bf = bin[0]
            result = client.flash(Path(bf.path), file_type='bin', address=bf.address, verbose=verbose, net=debug_net)

        # Display flash output if available
        output = result.get('output', '')
        if isinstance(output, list):
            # Output is a list of lines (verbose mode)
            output = '\n'.join(output)
        if output:
            click.echo(output)

        click.secho("\nFlashed!", fg='green')
    except requests.exceptions.HTTPError as e:
        # Extract error message from response if available
        try:
            error_detail = e.response.json().get('error', str(e))
        except Exception:
            error_detail = str(e)

        click.secho(f"Flash failed: {error_detail}", fg='red', err=True)
        client.close()
        ctx.exit(1)
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

@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--speed', type=str, default='4000', callback=validate_speed_param,
              help='SWD/JTAG speed in kHz (default: 4000)')
@click.option('--yes', is_flag=True, default=False,
              help='Skip confirmation prompt')
@click.option('--quiet', is_flag=True, default=False,
              help='Suppress warning messages')
@click.option('--json', 'json_output', is_flag=True, default=False,
              help='Output results in JSON format')
@click.option('--halt/--no-halt', is_flag=True, default=False,
              help='Halt the device after erase (keeps debugger connected)', show_default=True)
def erase(ctx, box, speed, yes, quiet, json_output, halt):
    """Erase all flash memory on target"""

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)
    jlink_script = _get_jlink_script_content(ctx, net_name or debug_net.get('name'), debug_net)
    device_type = debug_net.get('pin', 'unknown')

    # Confirm the erase operation (skip if quiet or json mode)
    if not yes and not quiet and not json_output:
        click.echo(f"WARNING: This will erase ALL flash memory on {device_type}")
        click.echo("This operation cannot be undone!")
        if not click.confirm("Do you want to continue?"):
            click.echo("Chip erase cancelled.")
            ctx.exit(0)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Auto-connect if not already connected
    if not _auto_connect_if_needed(client, debug_net, ctx, quiet=quiet, jlink_script=jlink_script):
        client.close()
        ctx.exit(1)

    # Execute erase
    if not quiet:
        click.echo("Erasing flash memory...")

    try:
        result = client.erase(debug_net, speed=speed, transport='SWD')
    except requests.exceptions.HTTPError as e:
        # Extract error message from response if available
        try:
            error_detail = e.response.json().get('error', str(e))
        except Exception:
            error_detail = str(e)

        click.secho(f"Erase failed: {error_detail}", fg='red', err=True)
        client.close()
        ctx.exit(1)
    except Exception as e:
        click.secho(f"Erase failed: {e}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Output results
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif not quiet:
        click.secho("Erase complete!", fg='green')

    # Erase internally disconnects (requires exclusive hardware access via JLinkExe)
    # Always reconnect to restore debugger connection
    import time
    time.sleep(0.5)  # Give hardware time to be released
    if not quiet:
        click.secho("Reconnecting debugger after erase...", fg='cyan', dim=True)
    try:
        client.connect(debug_net, speed=None, force=False, halt=halt, jlink_script=jlink_script)
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

@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
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

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Auto-connect if not already connected (unless force-reconnect, which handles its own connection)
    if not force_reconnect:
        if not _auto_connect_if_needed(client, debug_net, ctx):
            client.close()
            ctx.exit(1)

    # Force reconnect if requested for clean state
    if force_reconnect:
        try:
            click.echo("Forcing clean reconnect...", err=True)
            client.disconnect(debug_net)
            import time
            time.sleep(0.5)
            client.connect(debug_net, force=True, halt=False)
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
            click.secho("Error: No debugger connection found. Start GDB server first with: lager debug <net> gdbserver --box <box>", fg='red', err=True)
        else:
            click.secho(f"Error: {e}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Keep debugger connected (no auto-disconnect)
    client.close()

@click.command()
@click.pass_context
@click.argument('start_addr', type=MemoryAddressType())
@click.argument('length', type=MemoryAddressType())
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.option('--json', 'json_output', is_flag=True, default=False,
              help='Output results in JSON format')
@click.option('--halt/--no-halt', is_flag=True, default=False,
              help='Halt the device during memory read (keeps debugger connected)', show_default=True)
def memrd(ctx, start_addr, length, box, json_output, halt):
    """Read memory from target"""

    target_box = box

    net_name = getattr(ctx.obj, 'net_name', None)

    target_box, username = _resolve_box_with_username(ctx, target_box)

    debug_net = _get_debug_net(ctx, target_box, net_name)

    client = _get_service_client(target_box)
    if not client:
        click.secho("Error: Failed to create debug service client", fg='red', err=True)
        ctx.exit(1)

    # Auto-connect if not already connected
    if not _is_connected(client):
        # Not connected - auto-connect (with halt if --halt specified)
        if halt:
            click.secho("Auto-connecting to debugger (with halt for memory read)...", fg='cyan', dim=True)
        else:
            click.secho("Auto-connecting to debugger...", fg='cyan', dim=True)
        try:
            client.connect(debug_net, speed=None, force=False, halt=halt)
            if halt:
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
        click.secho(f"Warning: Memory address 0x{start_addr:x} may be invalid for 32-bit system", fg='yellow', err=True)
        click.secho(f"Maximum valid address is 0x{max_address:x}", fg='yellow', err=True)
        if not click.confirm("Continue anyway?", default=False):
            client.close()
            ctx.exit(0)

    try:
        memory_data = client.read_memory(debug_net, start_addr, length)
    except Exception as e:
        error_msg = str(e)
        if "400" in error_msg or "No debugger connection" in error_msg:
            click.secho("Error: No debugger connection found. Start GDB server first with: lager debug <net> gdbserver --box <box> --halt", fg='red', err=True)
            click.secho("Note: Device must be halted for memory reads to work reliably", fg='yellow', err=True)
        else:
            click.secho(f"Error reading memory: {e}", fg='red', err=True)
        client.close()
        ctx.exit(1)

    # Check if memory read returned empty data (silent failure)
    if not memory_data or len(memory_data) == 0:
        click.secho(f"Error: Memory read returned no data from address 0x{start_addr:08x}", fg='red', err=True)
        click.secho("Possible causes:", fg='yellow', err=True)
        click.secho("  • Device is not halted (use: lager debug <net> memrd <addr> <len> --box <box> --halt)", fg='yellow', err=True)
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

@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
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
    info_data = client.get_info(debug_net)

    click.echo(f"Debug Net Information:")
    click.echo(f"  Name: {info_data.get('net_name')}")
    click.echo(f"  Device Type: {info_data.get('device')}")
    click.echo(f"  Architecture: {info_data.get('arch')}")
    click.echo(f"  Probe: {info_data.get('probe')}")
    click.echo(f"  Connected: {info_data.get('connected')}")
    click.echo()

    client.close()


@click.command()
@click.pass_context
@click.option("--box", required=False, help="Lagerbox name or IP")
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

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.uart.commands

Commands for box UART interaction
"""
from __future__ import annotations

import sys
import json

import click
from click.exceptions import Abort, Exit
import requests
from texttable import Texttable

# Import consolidated helpers from cli.core.net_helpers
from ...core.net_group import NetCommand, HiddenArgument
from ...core.net_helpers import resolve_box, resolve_box_locked
from ...context import get_default_net
from ...errors import net_not_specified_error

UART_ROLE = "uart"

# Baudrate limits - common UART rates range from 300 to 3,000,000 baud
MIN_BAUDRATE = 300
MAX_BAUDRATE = 3_000_000

# Standard baudrates for validation hints
STANDARD_BAUDRATES = [300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 2000000, 3000000]


# ---------- helpers ----------

def _resolve_box_with_name(ctx, box):
    """
    Resolve box parameter to IP address.
    Returns tuple of (ip_address, box_name) where box_name is used for username lookup.
    """
    from ...box_storage import get_box_name_by_ip

    # Use the shared resolve_box_locked helper (auto-acquires ephemeral lock)
    resolved_ip = resolve_box_locked(ctx, box, 'uart')

    # Try to find box name for username lookup
    # If box was provided and is not an IP, it's the box name
    if box and not box.replace('.', '').isdigit():
        resolved_name = box
    else:
        # It was an IP, try reverse lookup
        resolved_name = get_box_name_by_ip(resolved_ip)

    return (resolved_ip, resolved_name)


def _fetch_uart_nets(ctx: click.Context, box_ip: str) -> list[dict]:
    """
    Fetch nets list from the box via HTTP endpoint.
    Uses the HTTP endpoint on port 9000 (Python container).
    """
    try:
        # This endpoint returns all saved nets; UART nets are not filtered here.
        from ...gateway_auth import auth_headers_for_box
        from ...box_storage import _check_gateway
        box_url = f'http://{box_ip}:9000/uart/nets/list'
        response = requests.get(box_url, timeout=5,
                                headers=auth_headers_for_box(box_ip))
        response = _check_gateway(response, box_ip)
        if response.status_code == 200:
            data = response.json()
            return data.get('nets', [])
        else:
            click.echo(f"Error: Box returned status {response.status_code}", err=True)
            return []
    except (requests.RequestException, json.JSONDecodeError) as e:
        click.echo(f"Error: The box at {box_ip}:9000 did not return its nets: {e}", err=True)
        return []


def _list_uart_nets(ctx, box):
    recs = _fetch_uart_nets(ctx, box)
    return [r for r in recs if r.get("role") == UART_ROLE]


def _get_uart_net(ctx, box, netname):
    """Get a specific UART net by name"""
    nets = _list_uart_nets(ctx, box)
    for net in nets:
        if net.get("name") == netname:
            return net
    return None


def _run_query_instruments(ctx: click.Context, box_ip: str) -> list[dict]:
    """Query instruments on the box (:9000/instruments/list) for device paths.

    Returns the same records as the old :5000 ``query_instruments.py`` exec
    (name/address/channels/tty_path), served warm by the box HTTP server.
    """
    try:
        from ...gateway_auth import auth_headers_for_box
        from ...box_storage import _check_gateway
        resp = requests.get(f'http://{box_ip}:9000/instruments/list', timeout=15,
                            headers=auth_headers_for_box(box_ip))
        # A gateway denial raises the actionable LagerError (not caught
        # below): an auth problem must not be reported as "no instruments".
        resp = _check_gateway(resp, box_ip)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return data
    except (requests.RequestException, json.JSONDecodeError):
        pass
    return []


def _fetch_uart_sessions(ctx: click.Context, box_ip: str):
    """List the UART sessions currently holding a net on the box.

    Returns (sessions, supported). ``supported`` is False when the box predates
    the endpoint, so callers can say "this box is too old" instead of reporting
    an empty list as "nothing is holding your net".
    """
    from ...gateway_auth import auth_headers_for_box
    from ...box_storage import _check_gateway
    try:
        resp = requests.get(f'http://{box_ip}:9000/uart/sessions', timeout=10,
                            headers=auth_headers_for_box(box_ip))
        resp = _check_gateway(resp, box_ip)
        if resp.status_code in (404, 405):
            return [], False
        if resp.status_code == 200:
            return resp.json().get('sessions', []), True
        click.echo(f"Error: Box returned status {resp.status_code}", err=True)
        return [], True
    except (requests.RequestException, json.JSONDecodeError) as e:
        click.echo(f"Error: The box at {box_ip}:9000 did not return its UART "
                   f"sessions: {e}", err=True)
        return [], True


def _release_uart_session(ctx: click.Context, box_ip: str, netname: str) -> bool:
    """Force-release whatever session is holding *netname*. True if one was.

    Used by `--force`. A 404 means nothing held the net, which is a fine
    outcome for a take-over -- the caller just proceeds to connect.
    """
    from ...gateway_auth import auth_headers_for_box
    from ...box_storage import _check_gateway
    try:
        resp = requests.delete(f'http://{box_ip}:9000/uart/sessions/{netname}',
                               timeout=10,
                               headers=auth_headers_for_box(box_ip))
        resp = _check_gateway(resp, box_ip)
        if resp.status_code == 200:
            return True
        if resp.status_code == 404:
            return False
        if resp.status_code == 405:
            click.secho(
                "Warning: this box is too old to support --force; update it "
                "with `lager update`.", fg='yellow', err=True)
            return False
        click.secho(f"Warning: the box refused to release net '{netname}' "
                    f"(HTTP {resp.status_code})", fg='yellow', err=True)
        return False
    except (requests.RequestException, json.JSONDecodeError) as e:
        click.secho(f"Warning: the box did not release net '{netname}': {e}",
                    fg='yellow', err=True)
        return False


def display_uart_sessions(ctx, box_ip: str) -> None:
    """Print who is holding which UART net."""
    sessions, supported = _fetch_uart_sessions(ctx, box_ip)
    if not supported:
        click.echo("This box does not report UART sessions; update it with "
                   "`lager update`.")
        return
    if not sessions:
        click.echo("No UART sessions are active on this box.")
        return

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_dtype(["t", "t", "t", "t"])
    table.set_cols_align(["l", "l", "l", "l"])
    table.header(["Net", "Device Path", "Client", "Reader"])
    for sess in sessions:
        connected = sess.get('client_connected')
        # None means the box could not introspect its connection manager.
        client = {True: "connected", False: "gone", None: "unknown"}[connected]
        alive = sess.get('reader_alive')
        reader = {True: "running", False: "stopped", None: "starting"}[alive]
        table.add_row([
            sess.get('netname') or "",
            sess.get('device_path') or "",
            client,
            reader,
        ])
    click.echo(table.draw())


def _shorten_identity(identity: str, limit: int = 56) -> str:
    """Fit a net's `pin` into one banner line without making it unreadable.

    `pin` holds either a USB serial number or — for a net added by device path
    — something like /dev/serial/by-id/usb-Prolific_USB-Serial_0001-if00.
    This used to be a bare `identity[:10]`, which rendered that path as
    "/dev/seria": every by-id path truncates to the same meaningless prefix,
    and the result is indistinguishable from a corrupted net record. It cost a
    real debugging session.

    So: ellipsise the middle. The prefix says what kind of identity it is and
    the tail carries the part that actually distinguishes one device from
    another, which is exactly what a reader needs.
    """
    if not isinstance(identity, str) or len(identity) <= limit:
        return identity
    # Split the budget so the distinguishing tail keeps the larger share.
    head = (limit - 3) // 3
    tail = limit - 3 - head
    return f"{identity[:head]}...{identity[-tail:]}"


def _find_device_path(usb_serial: str, inst_list: list[dict]) -> str | None:
    """Find the /dev/tty* path for a given USB serial number."""
    if usb_serial and isinstance(usb_serial, str) and usb_serial.startswith("/dev/"):
        # Net was created using a direct device path instead of a USB serial number.
        return usb_serial

    for inst in inst_list:
        # Check if this is a UART device
        channels = inst.get("channels", {})
        uart_channels = channels.get("uart", [])

        # If this device's UART channels include our serial number
        if usb_serial in uart_channels:
            # Return the tty_path if available
            return inst.get("tty_path")

    return None


def display_nets(ctx, box, netname: str | None):
    """Display UART nets with their configuration parameters."""
    uart_nets = _list_uart_nets(ctx, box)

    # Check if there are any UART nets to display
    if not uart_nets:
        click.echo("No UART nets found on this box.")
        return

    # Query instruments to get current device paths
    inst_list = _run_query_instruments(ctx, box)

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_dtype(["t", "t", "t", "t", "t", "t", "t"])
    table.set_cols_align(["l", "l", "l", "l", "l", "l", "l"])
    table.header(["Name", "Bridge Type", "Device Path", "Port", "Baudrate", "Format", "Flow Control"])

    for rec in uart_nets:
        if netname is None or netname == rec.get("name"):
            name = rec.get("name", "")
            bridge_type = rec.get("instrument", "Unknown")
            usb_serial = rec.get("pin", "")
            port = rec.get("channel", "0")
            params = rec.get("params", {})

            # Look up current device path from instruments
            device_path = _find_device_path(usb_serial, inst_list)
            display_path = device_path if device_path else f"{usb_serial} (disconnected)"

            # Extract parameters with defaults
            baudrate = params.get("baudrate", "115200")
            bytesize = params.get("bytesize", "8")
            parity = params.get("parity", "none")
            stopbits = params.get("stopbits", "1")

            # Build format string (e.g., "8N1")
            parity_char = {"none": "N", "even": "E", "odd": "O", "mark": "M", "space": "S"}.get(parity, "N")
            format_str = f"{bytesize}{parity_char}{stopbits}"

            # Build flow control string
            flow_parts = []
            if params.get("xonxoff"):
                flow_parts.append("XON/XOFF")
            if params.get("rtscts"):
                flow_parts.append("RTS/CTS")
            if params.get("dsrdtr"):
                flow_parts.append("DSR/DTR")
            flow_control = ", ".join(flow_parts) if flow_parts else "None"

            table.add_row([name, bridge_type, display_path, port, str(baudrate), format_str, flow_control])

    result = table.draw()
    click.echo(result)


def _connect_uart_http(ctx, box_ip, netname, overrides, interactive, box_label=None):
    """
    Connect to UART via WebSocket (both read-only and interactive modes).

    Both modes now use WebSocket for reliability - no more HTTP streaming crashes!

    Args:
        ctx: Click context
        box_ip: Box IP address
        netname: Name of the UART net to connect to
        overrides: Dictionary of serial port parameter overrides
        interactive: Whether to use interactive mode (bidirectional with keyboard input)
        box_label: The --box the user typed, so an "in use" error can name the
            exact take-over command
    """
    import time

    # Both interactive and read-only modes now use WebSocket
    if interactive:
        from .websocket_client import connect_uart_interactive
        box_url = f'http://{box_ip}:9000'
        connect_func = connect_uart_interactive
    else:
        from .websocket_client import connect_uart_readonly
        box_url = f'http://{box_ip}:9000'
        connect_func = connect_uart_readonly

    # Retry logic for WebSocket connection
    max_retries = 2
    last_error = None
    for attempt in range(max_retries):
        try:
            exit_code = connect_func(box_url, netname, overrides, box_label=box_label)
            ctx.exit(exit_code)
            return
        except (Exit, Abort):
            # ctx.exit() above is how this function returns. Catching it as a
            # connection error retried the whole session and then rewrote the
            # session's exit code to 1.
            raise
        except Exception as e:
            last_error = e
            error_str = str(e)
            if attempt < max_retries - 1:
                if "Connection refused" in error_str:
                    click.secho(f"Connection refused, retrying in 2 seconds... (attempt {attempt + 1}/{max_retries})", fg='yellow', err=True)
                    time.sleep(2)
                    continue
                elif "Failed to establish" in error_str or "Connection reset" in error_str:
                    click.secho(f"Connection failed, retrying in 2 seconds... (attempt {attempt + 1}/{max_retries})", fg='yellow', err=True)
                    time.sleep(2)
                    continue
            # Final attempt failed - provide detailed error
            break

    # All retries exhausted - show helpful error message
    error_str = str(last_error) if last_error else "Unknown error"
    if "Connection refused" in error_str:
        click.secho(f"Error: Connection refused to {box_ip}:9000", fg='red', err=True)
        click.secho("Possible causes:", err=True)
        click.secho("  - The UART service does not run on the box", err=True)
        click.secho("  - Docker container is not started", err=True)
        click.secho("  - Firewall blocking port 9000", err=True)
        click.secho(f"Try: lager hello --box {box_ip}", err=True)
    elif "timed out" in error_str.lower() or "timeout" in error_str.lower():
        click.secho(f"Error: Connection timed out to {box_ip}:9000", fg='red', err=True)
        click.secho("The box answers, but the UART service does not.", err=True)
        click.secho("Try restarting the Docker container on the box.", err=True)
    elif "No route to host" in error_str:
        click.secho(f"Error: No route to host {box_ip}", fg='red', err=True)
        click.secho("Check your network connection and VPN status.", err=True)
    elif "Name or service not known" in error_str or "getaddrinfo failed" in error_str:
        click.secho(f"Error: The hostname {box_ip} did not resolve", fg='red', err=True)
        click.secho("Check that the box name is spelled correctly.", err=True)
    else:
        # Reaching here means a genuine exception, not a session exit: the
        # `except (Exit, Abort)` above re-raises those. This used to compare
        # `str(last_error) != "0"` -- matching str(Exit(0)) -- to recover a
        # clean disconnect that had been captured as an error.
        click.secho(f"Error: WebSocket connection failed: {last_error}", fg='red', err=True)
        ctx.exit(1)


# ---------- CLI ----------

@click.command(cls=NetCommand)
@click.argument("NETNAME", required=False, metavar="[NET_NAME]")
@click.argument("ACTION", required=False, cls=HiddenArgument,
                metavar="serial-port", type=click.Choice(["serial-port"]))
@click.pass_context
# Target options
@click.option('--box', required=False, help="Lager Box name or IP")
# Serial parameter overrides
@click.option('--baudrate', type=int, help='Baudrate in baud (e.g., 9600, 115200)')
@click.option('--bytesize', type=click.Choice(['5', '6', '7', '8']), help='Number of data bits')
@click.option('--parity', type=click.Choice(['none', 'even', 'odd', 'mark', 'space']), help='Parity checking mode')
@click.option('--stopbits', type=click.Choice(['1', '1.5', '2']), help='Number of stop bits')
# Flow control options
@click.option('--xonxoff/--no-xonxoff', default=None, help='Software flow control (XON/XOFF)')
@click.option('--rtscts/--no-rtscts', default=None, help='Hardware flow control (RTS/CTS)')
@click.option('--dsrdtr/--no-dsrdtr', default=None, help='Hardware flow control (DSR/DTR)')
# Session options
@click.option('--sessions', 'list_sessions', is_flag=True,
              help='List the UART sessions that hold a net, then exit')
@click.option('--force', is_flag=True,
              help='Take over the net if another session holds it')
@click.option('-i', '--interactive', is_flag=True, help='Enable input mode for typing to serial port', show_default=True)
@click.option('--opost/--no-opost', default=False, help=r'Convert \n to \r\n on output', show_default=True)
@click.option('--line-ending', type=click.Choice(['lf', 'crlf', 'cr']), default='lf', help='Line ending format (lf=\\n, crlf=\\r\\n, cr=\\r)', show_default=True)
def uart(ctx, netname, action, box, baudrate, bytesize, parity, stopbits, xonxoff, rtscts, dsrdtr,
         list_sessions, force, interactive, opost, line_ending):
    """Connect to UART serial port.

    With no NET_NAME, lists the UART nets saved on the box. Passing
    'serial-port' after the net name prints the /dev path currently backing
    the net instead of connecting. --sessions reports which nets are currently
    held, and --force takes a held net over.
    """
    # Resolve box to box IP
    target_box, box_name = _resolve_box_with_name(ctx, box)

    # Session listing needs no netname: it is a question about the box.
    if list_sessions:
        display_uart_sessions(ctx, target_box)
        return

    # If no netname provided, try to use default
    if not netname:
        netname = get_default_net(ctx, 'uart')

    # Handle sub-action to report the current serial port in use
    # (ACTION is validated by click.Choice; 'serial-port' is the only value)
    if action:
        if not netname:
            net_not_specified_error('UART', 'uart', default_flag='uart-net').die()

        net_config = _get_uart_net(ctx, target_box, netname)
        if not net_config:
            click.secho(f"Error: UART net '{netname}' not found", fg='red', err=True)
            ctx.exit(1)

        inst_list = _run_query_instruments(ctx, target_box)
        usb_serial = net_config.get("pin", "")
        device_path = _find_device_path(usb_serial, inst_list)

        if device_path:
            click.echo(device_path)
        else:
            click.secho(f"Serial port for net '{netname}' ({usb_serial}) is not connected.", fg="yellow", err=True)
            ctx.exit(1)
        return

    # If still no netname, list all UART nets
    if not netname:
        display_nets(ctx, target_box, None)
        return

    # Validate baudrate range
    if baudrate is not None:
        if baudrate < MIN_BAUDRATE or baudrate > MAX_BAUDRATE:
            click.secho(f"Error: Baudrate must be between {MIN_BAUDRATE} and {MAX_BAUDRATE:,}, got {baudrate}", fg='red', err=True)
            click.secho("Common baudrates: 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600", err=True)
            ctx.exit(1)

    # Validate flow control options - cannot use multiple simultaneously
    if xonxoff and rtscts:
        click.secho("Error: Cannot use --xonxoff and --rtscts simultaneously", fg='red', err=True)
        click.secho("XON/XOFF is software flow control, RTS/CTS is hardware flow control.", err=True)
        click.secho("Choose one or the other, not both.", err=True)
        ctx.exit(1)
    if xonxoff and dsrdtr:
        click.secho("Error: Cannot use --xonxoff and --dsrdtr simultaneously", fg='red', err=True)
        click.secho("XON/XOFF is software flow control, DSR/DTR is hardware flow control.", err=True)
        click.secho("Choose one or the other, not both.", err=True)
        ctx.exit(1)

    # Load net configuration
    net_config = _get_uart_net(ctx, target_box, netname)
    if not net_config:
        click.secho(f"Error: UART net '{netname}' not found", fg='red', err=True)
        click.echo(f"\nRun 'lager uart' to see available UART nets on {target_box}", err=True)
        click.echo(f"\nTo create a new UART net:", err=True)
        click.echo(f"  1. Find available UART devices: lager instruments --box {target_box}", err=True)
        click.echo(f"  2a. Standard: lager nets add {netname} uart [DEVICE_SERIAL] [ADDRESS]", err=True)
        click.echo(f"  2b. No serial on adapter: lager nets add {netname} uart /dev/ttyUSB0 [LABEL]", err=True)
        ctx.exit(1)

    # Validate TTY for interactive mode
    if interactive:
        if not sys.stdin.isatty():
            click.secho('Error: stdin is not a TTY (cannot use --interactive)', fg='red', err=True)
            ctx.exit(1)
        if not sys.stdout.isatty():
            click.secho('Error: stdout is not a TTY (cannot use --interactive)', fg='red', err=True)
            ctx.exit(1)

    # Build parameter overrides
    overrides = {}
    if baudrate is not None:
        overrides['baudrate'] = baudrate
    if bytesize is not None:
        overrides['bytesize'] = int(bytesize)
    if parity is not None:
        overrides['parity'] = parity
    if stopbits is not None:
        overrides['stopbits'] = stopbits
    if xonxoff is not None:
        overrides['xonxoff'] = xonxoff
    if rtscts is not None:
        overrides['rtscts'] = rtscts
    if dsrdtr is not None:
        overrides['dsrdtr'] = dsrdtr

    # Always include opost setting
    overrides['opost'] = opost

    # Always include line_ending setting
    overrides['line_ending'] = line_ending

    # Show connection info. The box's own `uart_connected` line that follows
    # carries the resolved device path, baudrate and mode, so this one stays a
    # short pre-flight "what we are about to open".
    bridge_type = net_config.get("instrument", "unknown")
    identity = net_config.get("pin", "unknown")

    click.echo(
        f"Connecting to {netname}: {bridge_type} ({_shorten_identity(identity)})",
        err=True,
    )

    # Take the net over before connecting, so start_uart does not race the
    # holder's teardown. Silent when nothing held it -- --force is routinely
    # used pre-emptively, and "nothing to release" is not worth a line.
    if force and _release_uart_session(ctx, target_box, netname):
        click.secho(f"Released the session that held '{netname}'",
                    fg='yellow', err=True)

    # Connect to UART over the box's WebSocket API on :9000 (see
    # _connect_uart_http); serial-port discovery/listing also goes through
    # the :9000 HTTP endpoints, so UART no longer touches the :5000 exec path.
    _connect_uart_http(
        ctx, target_box, netname, overrides, interactive, box_label=box_name or box
    )


uart.net_examples = [
    "lager uart uart1 --box <BOX>",
    "lager uart uart1 --baudrate 115200 --box <BOX>",
    "lager uart uart1 --interactive --box <BOX>",
    "lager uart --box <BOX>         (list UART nets)",
]

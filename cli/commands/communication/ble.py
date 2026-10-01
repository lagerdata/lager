# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.ble.commands

    Commands for BLE
"""
from __future__ import annotations

import os
import re
import json
import signal
import sys
import threading
import time

import click

from ...core.group_usage import LagerGroup
from ...core.net_helpers import resolve_box, resolve_box_locked, post_box_command, _box_error_text


@click.group(name='ble', cls=LagerGroup)
def ble():
    """Scan and connect to Bluetooth Low Energy devices"""
    pass


ADDRESS_NAME_RE = re.compile(r'\A([0-9A-F]{2}-){5}[0-9A-F]{2}\Z')
# BLE address format: XX:XX:XX:XX:XX:XX (colon-separated) or XX-XX-XX-XX-XX-XX (dash-separated)
BLE_ADDRESS_RE = re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$')


def check_name(device):
    return 0 if ADDRESS_NAME_RE.search(device['name']) else 1


def _post_ble(ctx: click.Context, box_ip: str, action: str,
              http_timeout: float, **params) -> dict:
    """POST one action to :9000/ble/command and return the response."""
    return post_box_command(
        ctx, box_ip, "/ble/command", action,
        quiet=True, http_timeout=http_timeout, **params,
    )


def _validate_ble_address(ctx: click.Context, address: str) -> None:
    """Validate BLE address format."""
    if not BLE_ADDRESS_RE.match(address):
        click.secho(f"Error: Invalid BLE address format: {address}", fg='red', err=True)
        click.secho("Expected format: XX:XX:XX:XX:XX:XX (e.g., 00:11:22:33:44:55)", err=True)
        ctx.exit(1)


def _address_kind(device: dict) -> str:
    """`public`, `static`, `resolvable` or `non-resolvable`; `-` when unknown."""
    if device.get('address_type') == 'public':
        return 'public'
    return device.get('random_type') or '-'


def _format_device_table(devices: list[dict], verbose: bool = False) -> str:
    """Format scan results in the same table shape the old impl printed."""
    lines = []
    if verbose:
        lines.append(f"{'Name':<20} {'Address':<17} {'Type':<14} {'RSSI':<6} {'UUIDs'}")
        lines.append("-" * 95)
    else:
        lines.append(f"{'Name':<20} {'Address':<17} {'RSSI'}")
        lines.append("-" * 50)

    for device in devices:
        name = device.get('name') or device.get('address', '')
        address = device.get('address', '')
        rssi = device.get('rssi', -100)
        if verbose:
            uuids = device.get('uuids', [])
            uuids_str = ', '.join(str(u)[:8] + '...' for u in uuids[:3])
            if len(uuids) > 3:
                uuids_str += f" (+{len(uuids)-3} more)"
            lines.append(f"{name:<20} {address:<17} {_address_kind(device):<14} "
                         f"{rssi:<6} {uuids_str}")
        else:
            lines.append(f"{name:<20} {address:<17} {rssi}")

    return '\n'.join(lines)


def _print_services(services: list[dict]) -> None:
    """Print a service/characteristic summary for info/connect output."""
    for i, service in enumerate(services):
        desc = service.get('description') or 'Unknown Service'
        click.secho(f"  {i+1}. {service['uuid']}", fg='green')
        click.secho(f"     Description: {desc}", fg='green')
        chars = service.get('characteristics', [])
        click.secho(f"     Characteristics: {len(chars)}", fg='green')
        for char in chars[:3]:
            props = ', '.join(char.get('properties', []))
            click.secho(f"       - {char['uuid'][:8]}... [{props}]", fg='green')
        if len(chars) > 3:
            click.secho(f"       ... and {len(chars)-3} more", fg='green')


@ble.command('scan')
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--timeout', required=False, help='Total time box will spend scanning for devices', default=5.0, type=click.FLOAT, show_default=True)
@click.option('--name-contains', required=False, help='Filter devices to those whose name contains this string')
@click.option('--name-exact', required=False, help='Filter devices to those whose name matches this string')
@click.option('--verbose', required=False, is_flag=True, default=False, help='Verbose output (includes UUIDs)')
def scan(ctx, box, timeout, name_contains, name_exact, verbose):
    """
        Scan for BLE devices
    """
    # Validate timeout range
    MIN_TIMEOUT, MAX_TIMEOUT = 0.1, 300.0
    if timeout < MIN_TIMEOUT or timeout > MAX_TIMEOUT:
        click.secho(f"Error: Timeout must be between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds, got {timeout}", fg='red', err=True)
        ctx.exit(1)

    box_ip = resolve_box_locked(ctx, box, 'ble')

    click.secho(f"Scanning for BLE devices for {timeout} seconds...", fg='green')
    result = _post_ble(
        ctx, box_ip, 'scan',
        http_timeout=timeout + 30.0,
        timeout=timeout,
        name_contains=name_contains,
        name_exact=name_exact,
    )

    devices = (result.get('value') or {}).get('devices', [])
    click.secho(f"Found {len(devices)} device(s)", fg='green')

    if not devices:
        if name_exact or name_contains:
            click.secho("No devices found matching filter criteria!", fg='red')
        else:
            click.secho("No BLE devices found!", fg='red')
        return

    click.secho("\n" + _format_device_table(devices, verbose), fg='green')

    # Structured data for programmatic use, matching the old script's output.
    device_data = []
    for device in devices:
        item = {
            'name': device.get('name'),
            'address': device.get('address'),
            # From BlueZ: "public" or "random"; for a random address,
            # random_type says "static", "resolvable" or "non-resolvable".
            # None from a box that predates them.
            'address_type': device.get('address_type'),
            'random_type': device.get('random_type'),
            'rssi': device.get('rssi', -100),
        }
        if verbose:
            item['uuids'] = device.get('uuids', [])
        device_data.append(item)

    click.echo("\nJSON Output:")
    click.echo(json.dumps(device_data, indent=2))


def _info_or_connect(ctx, box, address, connect_style: bool):
    """Shared body for the info and connect commands (same box action)."""
    _validate_ble_address(ctx, address)
    box_ip = resolve_box_locked(ctx, box, 'ble')

    verb = "Connecting to" if connect_style else "Getting info for"
    click.secho(f"{verb} BLE device: {address}", fg='green')

    result = _post_ble(ctx, box_ip, 'connect' if connect_style else 'info',
                       http_timeout=45.0, address=address)
    value = result.get('value') or {}
    services = value.get('services', [])

    if connect_style:
        click.secho(f"[OK] Connected to {address}", fg='green')
        click.secho("\nConnection successful!", fg='green')
        click.secho(f"Device: {address}", fg='green')
        click.secho(f"Services: {len(services)}", fg='green')
        if services:
            click.secho("\nServices found:", fg='green')
            for i, service in enumerate(services[:3]):
                chars = service.get('characteristics', [])
                click.secho(f"  {i+1}. {service['uuid'][:8]}... ({len(chars)} characteristics)", fg='green')
            if len(services) > 3:
                click.secho(f"  ... and {len(services)-3} more services", fg='green')
    else:
        click.secho("\nDevice Information:", fg='green')
        click.secho(f"Address: {address}", fg='green')
        click.secho(f"Services: {len(services)}", fg='green')
        if services:
            click.secho("\nServices:", fg='green')
            _print_services(services)

    click.echo("\nJSON Output:")
    click.echo(json.dumps(value, indent=2))


@ble.command('info')
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.argument('address', required=True)
def info(ctx, box, address):
    """
        Get BLE device information
    """
    _info_or_connect(ctx, box, address, connect_style=False)


@ble.command('connect')
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.argument('address', required=True)
def connect(ctx, box, address):
    """
        Connect to a BLE device
    """
    _info_or_connect(ctx, box, address, connect_style=True)


@ble.command('disconnect')
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.argument('address', required=True)
def disconnect(ctx, box, address):
    """
        Disconnect from a BLE device
    """
    _validate_ble_address(ctx, address)
    box_ip = resolve_box_locked(ctx, box, 'ble')

    click.secho(f"Disconnecting from BLE device: {address}", fg='green')
    result = _post_ble(ctx, box_ip, 'disconnect', http_timeout=45.0, address=address)
    value = result.get('value') or {}

    click.secho(f"[OK] Disconnected from {address}", fg='green')
    if value.get('note'):
        click.secho(f"  Note: {_box_error_text(value['note'])}", fg='green')

    click.echo("\nJSON Output:")
    click.echo(json.dumps(value, indent=2))


# ---------------------------------------------------------------------------
# Persistent GATT sessions (`lager ble session` / `lager ble sessions`)
# ---------------------------------------------------------------------------

def _box_flag(box):
    return f" --box {box}" if box else ""


def _session_error(exc, address, box):
    """Turn a BLESessionError into an actionable LagerError."""
    from ...errors import LagerError
    from ...core.net_helpers import BLUEZ_UNAVAILABLE_MESSAGE

    code = exc.code
    if code == 'bluez_unavailable':
        return LagerError(BLUEZ_UNAVAILABLE_MESSAGE)
    if code == 'device_not_found':
        return LagerError(exc.message, fixes=[
            f"lager ble scan{_box_flag(box)}"])
    if code == 'adapter_busy':
        return LagerError(exc.message, fixes=[
            f"lager ble sessions{_box_flag(box)}",
            f"lager ble session {address}{_box_flag(box)} --force  (ends the other session)"])
    if code in ('disconnected', 'connection_lost'):
        return LagerError(f"The BLE session with {address} ended: {exc.message}",
                          cause=f"reason: {code}")
    if code in ('idle_timeout', 'overflow', 'released', 'shutdown',
                'protocol_error', 'timeout'):
        return LagerError(f"The BLE session with {address} ended: {exc.message}",
                          cause=f"reason: {code}")
    return LagerError(exc.message, cause=f"box error code: {code}")


def _fetch_ble_sessions(box_ip):
    """GET /ble/sessions -> list, or None when the box predates sessions."""
    from ...gateway_auth import auth_headers_for_box
    from ...box_storage import _check_gateway
    from ...errors import connection_error
    import requests

    try:
        resp = requests.get(f'http://{box_ip}:9000/ble/sessions', timeout=10,
                            headers=auth_headers_for_box(box_ip))
        resp = _check_gateway(resp, box_ip)
    except requests.RequestException as e:
        raise connection_error(e, host=box_ip)
    if resp.status_code in (404, 405):
        return None
    return resp.json().get('sessions', [])


def _release_ble_sessions(box_ip, address=None):
    """POST /ble/sessions/release -> list of released sessions ([] if none)."""
    from ...gateway_auth import auth_headers_for_box
    from ...box_storage import _check_gateway
    from ...errors import LagerError, connection_error
    import requests

    body = {'address': address} if address else {}
    try:
        resp = requests.post(f'http://{box_ip}:9000/ble/sessions/release', json=body,
                             timeout=20, headers=auth_headers_for_box(box_ip))
        resp = _check_gateway(resp, box_ip)
    except requests.RequestException as e:
        raise connection_error(e, host=box_ip)
    if resp.status_code == 200:
        return resp.json().get('released', [])
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if resp.status_code == 404 and 'No open BLE session' in str(payload.get('error', '')):
        return []
    if resp.status_code in (404, 405):
        raise LagerError("This box does not support BLE sessions.",
                         fixes=["lager update --box <BOX>"])
    raise LagerError(f"The box did not release the BLE session: "
                     f"{payload.get('error') or 'HTTP %d' % resp.status_code}")


def _connect_session_client(box_ip):
    """Open the /ble Socket.IO connection, handling gateway auth."""
    from .ble_session_client import BLESessionClient
    from ...gateway_auth import auth_headers_for_url, ws_handshake_recovery
    from ...errors import LagerError, connection_error

    box_url = f'http://{box_ip}:9000'
    client = BLESessionClient(box_url)
    headers = auth_headers_for_url(box_url)
    for attempt in (0, 1):
        try:
            client.connect(headers=headers)
            return client
        except Exception as e:  # noqa: BLE001 — classified below
            if 'namespace' in str(e).lower():
                raise LagerError(
                    "This box does not support BLE sessions.",
                    cause="Its software predates the /ble session namespace.",
                    fixes=["lager update --box <BOX>"], raw=e)
            retry_headers, denial = ws_handshake_recovery(box_url, headers)
            if denial is not None:
                raise denial
            if attempt == 0 and retry_headers:
                headers = retry_headers
                continue
            raise connection_error(e, host=box_ip)


def _parse_write_spec(ctx, spec):
    """'UUID:HEX' -> (uuid, bytes)."""
    uuid, sep, hexdata = spec.rpartition(':')
    if not sep or not uuid:
        raise click.BadParameter(f"expected UUID:HEX, got {spec!r}", ctx=ctx,
                                 param_hint="'--write'")
    try:
        return uuid, bytes.fromhex(hexdata)
    except ValueError:
        raise click.BadParameter(f"invalid hex in {spec!r}", ctx=ctx,
                                 param_hint="'--write'")


class _SessionPrinter:
    """Human or JSON-lines output for a session."""

    def __init__(self, as_json):
        self.as_json = as_json
        self.lock = threading.Lock()

    def event(self, name, value, text):
        with self.lock:
            if self.as_json:
                click.echo(json.dumps(dict(value, event=name)))
            else:
                click.echo(text)

    def opened(self, value):
        if value.get('mtu_source') == 'bluez':
            mtu = f"MTU {value['mtu']} (negotiated)"
        else:
            mtu = f"MTU {value['mtu']} (assumed: the box did not report the negotiated MTU)"
        self.event('open', value,
                   f"[OK] Session open with {value['address']}: {mtu}, "
                   f"{len(value.get('services', []))} service(s)")

    def notification(self, item):
        value = dict(item, data=item['data'].hex())
        stamp = time.strftime('%H:%M:%S', time.localtime(item['ts']))
        stamp += '.%03d' % int((item['ts'] % 1) * 1000)
        self.event('notify', value,
                   f"[{stamp}] #{item['n']} {item['char']} {item['data'].hex()}")


def _print_session_services(printer, value):
    if printer.as_json:
        return
    for service in value.get('services', []):
        click.echo(f"  service {service['uuid']}")
        for char in service.get('characteristics', []):
            props = ', '.join(char.get('properties', []))
            click.echo(f"    [{char.get('handle')}] {char['uuid']}  ({props})")


def _drain_notifications(client, printer, timeout=None):
    """Print queued notifications; return how many were printed."""
    count = 0
    while True:
        item = client.get_notification(timeout)
        if item is None:
            return count
        printer.notification(item)
        count += 1
        timeout = None


def _run_one_shot(client, printer, subscribes, writes, reads, response, chunk, listen,
                  chunk_size=None):
    """Subscribe, write, read, then print notifications for `listen` seconds."""
    for uuid in subscribes:
        value = client.subscribe(uuid)
        printer.event('subscribe', value,
                      f"[OK] Subscribed to {value['char']} ({value['mode']})")
    last_write = None
    for uuid, data in writes:
        value = client.write(uuid, data, response=response, chunk=chunk,
                             chunk_size=chunk_size)
        last_write = time.monotonic()
        printer.event('write', value,
                      f"[OK] Wrote {value['bytes']} byte(s) to {value['char']} "
                      f"in {value['chunks']} write(s)")
    for uuid in reads:
        data = client.read(uuid)
        printer.event('read', {'char': uuid, 'data': data.hex()},
                      f"[OK] Read {uuid}: {data.hex() or '(empty)'}")

    deadline = time.monotonic() + listen
    first_notify = None
    while True:
        remaining = deadline - time.monotonic()
        # Short slices, so a session that ends mid-listen is noticed promptly.
        item = client.get_notification(timeout=min(remaining, 0.2) if remaining > 0 else None)
        if item is not None:
            if first_notify is None:
                first_notify = time.monotonic()
            printer.notification(item)
            continue
        if client.closed is not None or remaining <= 0:
            break
    if last_write is not None and first_notify is not None and first_notify >= last_write:
        ms = round((first_notify - last_write) * 1000)
        printer.event('latency', {'first_notify_ms': ms},
                      f"First notification {ms} ms after the last write result")


_REPL_HELP = """Commands:
  info                                  show MTU and services
  mtu                                   show the negotiated MTU
  sub <uuid|handle>                     subscribe (write the CCCD)
  unsub <uuid|handle>                   unsubscribe
  write <uuid|handle> <hex> [--no-response] [--chunk]
  read <uuid|handle>
  ping                                  reset the idle timer
  close | quit                          close the session and exit"""


def _target_arg(token):
    """A REPL characteristic argument: a decimal handle or a UUID."""
    return (None, int(token)) if token.isdigit() else (token, None)


def _wake_prompt():
    """Interrupt the main thread's blocked input() so the REPL can exit.

    Only a real signal wakes a blocked read; _thread.interrupt_main() would
    not raise until the user pressed Enter. On Windows os.kill(SIGINT) would
    terminate the process instead, so there the user is told to press Enter.
    """
    if os.name == 'posix':
        os.kill(os.getpid(), signal.SIGINT)
        return
    click.secho('\nThe session ended. Press Enter to exit.', fg='yellow', err=True)


def _run_repl(client, printer, opened):
    """Interactive session: commands on stdin, notifications as they arrive."""
    from .ble_session_client import BLESessionError

    stop = threading.Event()
    # Set while the main thread is blocked in input(): a session that ends
    # then (released, link lost, idle) must wake it, or the prompt sits there
    # with a dead session until the user happens to press Enter.
    at_prompt = threading.Event()

    def pump():
        while not stop.is_set():
            item = client.get_notification(timeout=0.2)
            if item is not None:
                with printer.lock:
                    click.echo('\r', nl=False)
                printer.notification(item)
            elif client.closed is not None:
                if at_prompt.is_set() and not stop.is_set():
                    _wake_prompt()
                return

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    click.echo(_REPL_HELP)
    try:
        while client.closed is None:
            try:
                at_prompt.set()
                line = input('ble> ').strip()
            except (EOFError, KeyboardInterrupt):
                # Ctrl-D, Ctrl-C, or the pump waking us because the session
                # ended: the caller closes it or reports why it ended.
                click.echo()
                break
            finally:
                at_prompt.clear()
            if not line:
                continue
            words = line.split()
            cmd, args = words[0].lower(), words[1:]
            try:
                if cmd in ('close', 'quit', 'exit'):
                    break
                if cmd == 'help':
                    click.echo(_REPL_HELP)
                elif cmd == 'info':
                    value = client.info()
                    printer.opened(value)
                    _print_session_services(printer, value)
                elif cmd == 'mtu':
                    click.echo(f"MTU {opened['mtu']} ({opened['mtu_source']}); "
                               f"largest single write {min(opened['mtu'] - 3, 512)} bytes")
                elif cmd == 'ping':
                    client.ping()
                    click.echo('[OK]')
                elif cmd in ('sub', 'unsub') and len(args) == 1:
                    char, handle = _target_arg(args[0])
                    call = client.subscribe if cmd == 'sub' else client.unsubscribe
                    value = call(char, handle=handle)
                    click.echo(f"[OK] {cmd} {value['char']} (handle {value['handle']})")
                elif cmd == 'write' and len(args) >= 2:
                    char, handle = _target_arg(args[0])
                    flags = set(args[2:])
                    unknown = flags - {'--no-response', '--chunk'}
                    if unknown:
                        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}")
                    value = client.write(char, bytes.fromhex(args[1]), handle=handle,
                                         response='--no-response' not in flags,
                                         chunk='--chunk' in flags)
                    click.echo(f"[OK] Wrote {value['bytes']} byte(s) in {value['chunks']} write(s)")
                elif cmd == 'read' and len(args) == 1:
                    char, handle = _target_arg(args[0])
                    click.echo(client.read(char, handle=handle).hex() or '(empty)')
                else:
                    click.echo(f"Unrecognized command: {line}  (type 'help')")
            except ValueError as e:
                click.secho(f"Error: {e}", fg='red', err=True)
            except BLESessionError as e:
                click.secho(f"Error ({e.code}): {e.message}", fg='red', err=True)
                if client.closed is not None:
                    break
    finally:
        stop.set()
        thread.join(timeout=1)
    _drain_notifications(client, printer)


@ble.command('session')
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.argument('address', required=True)
@click.option('--subscribe', 'subscribes', multiple=True, metavar='UUID',
              help='Subscribe to notifications on UUID before any write (repeatable)')
@click.option('--write', 'writes', multiple=True, metavar='UUID:HEX',
              help='Write hex bytes to UUID, in the order given (repeatable)')
@click.option('--read', 'reads', multiple=True, metavar='UUID',
              help='Read UUID after the writes (repeatable)')
@click.option('--no-response', is_flag=True, default=False,
              help='Use write-without-response for --write')
@click.option('--chunk', is_flag=True, default=False,
              help='Split each --write into writes of at most MTU-3 (and 512) bytes')
@click.option('--chunk-size', type=click.IntRange(1, 512), default=None, metavar='N',
              help='Split each --write into writes of at most N bytes (implies --chunk)')
@click.option('--listen', type=click.FloatRange(min=0), default=None, metavar='SECONDS',
              help='Print notifications for SECONDS after the writes and reads, then close')
@click.option('--connect-timeout', type=click.FloatRange(1, 120), default=10.0,
              show_default=True, help='Seconds to find and connect to the device')
@click.option('--idle-timeout', type=click.FloatRange(5, 3600), default=300.0,
              show_default=True, help='Close the session after this many seconds with no operation')
@click.option('--json', 'as_json', is_flag=True, default=False,
              help='Print one JSON object per line (one-shot mode)')
@click.option('--force', is_flag=True, default=False,
              help='End any BLE session already open on the box first')
def session(ctx, box, address, subscribes, writes, reads, no_response, chunk, chunk_size,
            listen, connect_timeout, idle_timeout, as_json, force):
    """
        Open a persistent GATT session with a BLE device

        With --subscribe/--write/--read/--listen, runs them once (subscribes,
        then writes, then reads, then listens) and closes. Otherwise starts
        an interactive prompt.
    """
    from .ble_session_client import BLESessionError
    from ...errors import LagerError
    from ...box_storage import get_lock_holder

    _validate_ble_address(ctx, address)
    parsed_writes = [_parse_write_spec(ctx, spec) for spec in writes]
    one_shot = bool(subscribes or writes or reads or listen is not None)
    if not one_shot and not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise LagerError(
            "Interactive `lager ble session` needs a terminal.",
            fixes=[f"lager ble session {address}{_box_flag(box)} --subscribe <UUID> --listen 10"])

    box_ip = resolve_box_locked(ctx, box, 'ble session')
    if force:
        released = _release_ble_sessions(box_ip)
        if released and not as_json:
            click.secho(f"Ended the session with {released[0].get('address')}", fg='yellow',
                        err=True)

    printer = _SessionPrinter(as_json)
    client = _connect_session_client(box_ip)
    try:
        try:
            opened = client.open(address, connect_timeout=connect_timeout,
                                 idle_timeout=idle_timeout, holder=get_lock_holder())
        except BLESessionError as e:
            raise _session_error(e, address, box)
        printer.opened(opened)
        if one_shot:
            try:
                _run_one_shot(client, printer, subscribes, parsed_writes, reads,
                              not no_response, chunk, listen or 0.0, chunk_size)
            except BLESessionError as e:
                _drain_notifications(client, printer)
                raise _session_error(e, address, box)
        else:
            _print_session_services(printer, opened)
            _run_repl(client, printer, opened)

        closed = client.closed
        if closed is not None and closed.get('reason') != 'client':
            if as_json:
                printer.event('closed', closed, '')
            raise _session_error(BLESessionError(closed.get('reason', 'unknown'),
                                                 closed.get('message', '')), address, box)
        client.close()
        _drain_notifications(client, printer)
        printer.event('closed', client.closed or {'reason': 'client'},
                      "[OK] Session closed")
    finally:
        client.disconnect()


@ble.command('sessions')
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--release', is_flag=True, default=False,
              help='End the open session(s) on the box')
@click.option('--address', required=False,
              help='With --release: only end the session with this device')
def sessions(ctx, box, release, address):
    """
        List (or end) the BLE sessions open on the box
    """
    from ...errors import LagerError

    if address:
        _validate_ble_address(ctx, address)
    # Ending a session is a change to the box; listing is read-only.
    box_ip = (resolve_box_locked(ctx, box, 'ble sessions') if release
              else resolve_box(ctx, box, read_only=True))
    if release:
        released = _release_ble_sessions(box_ip, address)
        if not released:
            click.secho("No BLE session was open.", fg='green')
        for s in released:
            click.secho(f"[OK] Ended the session with {s.get('address')}", fg='green')
        return

    found = _fetch_ble_sessions(box_ip)
    if found is None:
        raise LagerError("This box does not support BLE sessions.",
                         fixes=[f"lager update{_box_flag(box) or ' --box <BOX>'}"])
    if not found:
        click.secho("No BLE sessions open.", fg='green')
        return
    click.echo(f"{'Address':<17}  {'Holder':<30}  {'Open':>7}  {'Idle':>7}  {'MTU':>4}  Subscriptions")
    for s in found:
        subs = ', '.join(s.get('subscriptions') or []) or '-'
        click.echo(f"{s.get('address', ''):<17}  {(s.get('holder') or '-')[:30]:<30}  "
                   f"{s.get('opened_s', 0):>6.0f}s  {s.get('idle_s', 0):>6.0f}s  "
                   f"{s.get('mtu', 0):>4}  {subs}")


@ble.command('adapter')
@click.pass_context
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option('--json', 'as_json', is_flag=True, default=False,
              help='Print the result as JSON')
def adapter(ctx, box, as_json):
    """
        Check that the box has a working Bluetooth adapter

        Exits with status 0 when BLE is available and 1 when it is not, so a
        test script can skip on a box without a radio.
    """
    from ...errors import LagerError

    # Read-only: it answers even while a BLE session holds the adapter.
    box_ip = resolve_box(ctx, box, read_only=True)
    value = _post_ble(ctx, box_ip, 'adapter', http_timeout=30.0).get('value') or {}
    if as_json:
        click.echo(json.dumps(value, indent=2))
    for a in value.get('adapters') or []:
        if not as_json:
            state = 'powered' if a.get('powered') else 'powered off'
            click.echo(f"{a.get('name')}  {a.get('address')}  {state}")
    if value.get('available'):
        if not as_json:
            click.secho("[OK] BLE is available on this box", fg='green')
        return
    raise LagerError("BLE is not available on this box.",
                     cause=_box_error_text(value.get('reason') or 'unknown reason'))

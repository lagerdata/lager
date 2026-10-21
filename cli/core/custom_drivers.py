# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Custom drivers on the CLI side: discovery, caching, and command building.

A custom driver is installed on the box (``lager drivers install``) and its
code never reaches this machine. The box serves each driver's manifest at
GET :9000/drivers; this module turns a manifest into a click group so that

    lager <type> <NET> <action> --<arg> <value> --box <BOX>

works with the stock CLI. Manifests are cached per box in
~/.lager_cache/drivers_manifest.json for five minutes, so resolving a
custom command does not cost a round-trip every time.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import click

NET_HTTP_PORT = 9000
DRIVERS_HTTP_TIMEOUT = 30
# Discovery runs while click resolves the command name, before any output.
# Keep it short: on an unreachable box the user gets "No such command" fast.
DISCOVERY_TIMEOUT = 2.0

CACHE_FILE = Path.home() / '.lager_cache' / 'drivers_manifest.json'
CACHE_TTL = 300  # 5 minutes
# A mistyped command name also goes through discovery, so remember the
# answers that mean "not a driver": a box that did not answer, and a name the
# box's fresh list lacks. Without these, every typo against an offline box
# waited out the timeout.
UNREACHABLE_TTL = 30
MISS_TTL = 10

_TYPE_RE = re.compile(r'^[a-z][a-z0-9_-]{1,31}$')


# ---------------------------------------------------------------------------
# Manifest cache
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, 'w') as f:
            json.dump(cache, f)
    except OSError:
        pass  # A cache that cannot be written must not break a command.


def cached_drivers(box_ip: str) -> list | None:
    entry = _load_cache().get(box_ip)
    if not isinstance(entry, dict) or time.time() - entry.get('timestamp', 0) > CACHE_TTL:
        return None
    drivers = entry.get('drivers')
    return drivers if isinstance(drivers, list) else None


def remember_drivers(box_ip: str, drivers: list) -> None:
    cache = _load_cache()
    now = time.time()
    entry = {'timestamp': now, 'drivers': drivers}
    # Recent misses stay true while the fresh list still lacks those names.
    old = cache.get(box_ip)
    missed = old.get('missed') if isinstance(old, dict) else None
    if isinstance(missed, dict):
        types = {d.get('type') for d in drivers if isinstance(d, dict)}
        kept = {k: t for k, t in missed.items()
                if k not in types and isinstance(t, (int, float)) and now - t < MISS_TTL}
        if kept:
            entry['missed'] = kept
    cache[box_ip] = entry
    _save_cache(cache)


def _mark_box(box_ip: str, update) -> None:
    cache = _load_cache()
    entry = cache.get(box_ip)
    if not isinstance(entry, dict):
        entry = cache[box_ip] = {}
    update(entry, time.time())
    _save_cache(cache)


def _mark_unreachable(box_ip: str) -> None:
    _mark_box(box_ip, lambda e, now: e.__setitem__('unreachable_at', now))


def _mark_miss(box_ip: str, name: str) -> None:
    def update(entry, now):
        missed = entry.get('missed')
        missed = {k: t for k, t in missed.items() if now - t < MISS_TTL} \
            if isinstance(missed, dict) else {}
        missed[name] = now
        entry['missed'] = missed
    _mark_box(box_ip, update)


def _recently(entry: dict, key: str, ttl: float, name: str | None = None) -> bool:
    value = entry.get(key)
    if name is not None:
        value = value.get(name) if isinstance(value, dict) else None
    return isinstance(value, (int, float)) and time.time() - value < ttl


def forget_box_drivers(box_ip: str) -> None:
    cache = _load_cache()
    if cache.pop(box_ip, None) is not None:
        _save_cache(cache)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def driver_request(ctx: click.Context, box_ip: str, method: str, path: str,
                   params=None, files=None, timeout: float = DRIVERS_HTTP_TIMEOUT) -> Any:
    """One round-trip to the box's /drivers API; prints the error and exits on failure."""
    import requests
    from ..gateway_auth import auth_headers_for_box
    from ..box_storage import _check_gateway
    from .net_helpers import echo_box_request_failure

    url = f"http://{box_ip}:{NET_HTTP_PORT}{path}"
    try:
        resp = requests.request(method, url, params=params, files=files,
                                headers=auth_headers_for_box(box_ip), timeout=timeout)
        resp = _check_gateway(resp, box_ip)
    except (requests.ConnectionError, requests.Timeout) as e:
        echo_box_request_failure(box_ip, e, timeout=timeout)
        ctx.exit(1)
    except requests.RequestException as e:
        click.secho(f"Error: request to box failed: {e}", fg='red', err=True)
        ctx.exit(1)

    try:
        body = resp.json()
    except ValueError:
        body = None

    if 200 <= resp.status_code < 300 and body is not None:
        return body

    # A box that predates /drivers answers its generic 404 ({"error": "Not
    # found"}); the drivers routes' own 404s name the driver instead.
    generic = not isinstance(body, dict) or body.get('error') in (None, 'Not found')
    if resp.status_code in (404, 405) and generic:
        click.secho("Error: this box does not support custom drivers. Update it with "
                    "`lager update --box <BOX>`.", fg='red', err=True)
        ctx.exit(1)
    error = (body.get('error') if isinstance(body, dict) else None) \
        or f"HTTP {resp.status_code}"
    click.secho(f"Error: {error}", fg='red', err=True)
    ctx.exit(1)


def fetch_drivers(box_ip: str, timeout: float = DISCOVERY_TIMEOUT,
                  use_cache: bool = True) -> list | None:
    """The box's installed drivers, or None when it cannot be asked.

    Never prints and never exits: discovery failing must leave the caller
    free to report the ordinary "No such command". A box that predates
    custom drivers answers 404 and has none.
    """
    if use_cache:
        drivers = cached_drivers(box_ip)
        if drivers is not None:
            return drivers
    try:
        import requests
        from ..gateway_auth import auth_headers_for_box
        resp = requests.get(f"http://{box_ip}:{NET_HTTP_PORT}/drivers",
                            headers=auth_headers_for_box(box_ip), timeout=timeout)
    except Exception:
        return None
    if resp.status_code in (404, 405):
        drivers = []
    elif resp.status_code != 200:
        return None
    else:
        try:
            drivers = resp.json().get('drivers')
        except (ValueError, AttributeError):
            return None
        if not isinstance(drivers, list):
            return None
    remember_drivers(box_ip, drivers)
    return drivers


def find_driver(drivers: list | None, dtype: str) -> dict | None:
    for entry in drivers or []:
        if isinstance(entry, dict) and entry.get('type') == dtype \
                and entry.get('status') == 'ok' and isinstance(entry.get('manifest'), dict):
            return entry
    return None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def echo_result(result: dict, as_json: bool) -> None:
    """Print an action's result: the value as JSON, or the message plus fields."""
    value = result.get('value')
    if as_json:
        click.echo(json.dumps(value, indent=2, sort_keys=True, default=str))
        return
    click.secho(result.get('message') or 'done', fg='green')
    if isinstance(value, dict):
        for key in sorted(k for k in value if k != 'message'):
            click.echo(f"  {key}: {value[key]}")


def describe_actions(manifest: dict) -> list[str]:
    """Human-readable lines: one per action, with its args."""
    lines = []
    for name, spec in sorted(manifest.get('actions', {}).items()):
        lines.append(f"  {name:<16} {spec.get('help', '')}".rstrip())
        for arg, aspec in sorted(spec.get('args', {}).items()):
            flag = '--' + arg.replace('_', '-')
            kind = aspec.get('type', 'str')
            if kind == 'choice':
                kind = '|'.join(aspec.get('choices', []))
            extra = []
            if aspec.get('required') and 'default' not in aspec:
                extra.append('required')
            if 'default' in aspec:
                extra.append(f"default {aspec['default']}")
            if 'min' in aspec or 'max' in aspec:
                extra.append(f"{aspec.get('min', '')}..{aspec.get('max', '')}")
            tail = f" ({', '.join(extra)})" if extra else ''
            lines.append(f"      {flag} <{kind}>{tail}  {aspec.get('help', '')}".rstrip())
    return lines


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------

def _click_option(name: str, spec: dict) -> click.Option:
    flag = '--' + name.replace('_', '-')
    kind = spec.get('type', 'str')
    has_default = 'default' in spec
    kwargs: dict[str, Any] = {
        'help': spec.get('help') or None,
        'required': bool(spec.get('required')) and not has_default,
        'show_default': has_default,
    }
    if has_default:
        kwargs['default'] = spec['default']

    if kind == 'bool':
        no_flag = '--no-' + name.replace('_', '-')
        kwargs.setdefault('default', False)
        return click.Option([f"{flag}/{no_flag}", name], **kwargs)
    if kind == 'int':
        kwargs['type'] = click.IntRange(spec.get('min'), spec.get('max')) \
            if ('min' in spec or 'max' in spec) else click.INT
    elif kind == 'float':
        kwargs['type'] = click.FloatRange(spec.get('min'), spec.get('max')) \
            if ('min' in spec or 'max' in spec) else click.FLOAT
    elif kind == 'choice':
        kwargs['type'] = click.Choice(spec.get('choices') or [])
    else:
        kwargs['type'] = click.STRING
    return click.Option([flag, name], **kwargs)


def _action_command(dtype: str, action: str, spec: dict) -> click.Command:
    from .net_helpers import (post_net_command, require_netname, resolve_box_locked,
                              validate_net_exists)

    arg_names = list(spec.get('args', {}))
    http_timeout = int(spec.get('timeout_s', 60)) + 15

    def callback(box=None, as_json=False, **values):
        ctx = click.get_current_context()
        netname = require_netname(ctx, f"{dtype} {action}")
        if box is None:
            box = getattr(ctx.obj, 'box', None)
        box_ip = resolve_box_locked(ctx, box, f"{dtype} {action}")
        validate_net_exists(ctx, box_ip, netname, dtype)
        args = {k: v for k, v in values.items() if k in arg_names and v is not None}
        result = post_net_command(ctx, box_ip, netname, action, role=dtype, quiet=True,
                                  http_timeout=http_timeout, **args)
        echo_result(result, as_json)

    params: list[click.Parameter] = [
        _click_option(name, aspec) for name, aspec in spec.get('args', {}).items()]
    params += [
        click.Option(['--box'], required=False, help='Lager Box name or IP'),
        click.Option(['--json', 'as_json'], is_flag=True,
                     help="Print the action's result as JSON"),
    ]
    from .net_group import NetSubCommand
    return NetSubCommand(action, callback=callback, params=params,
                         help=spec.get('help') or f"Run the {action} action")


def build_driver_group(entry: dict) -> click.Group:
    """A ``lager <type>`` group built from a driver's manifest."""
    from .net_group import NetGroup
    from .net_helpers import display_nets, resolve_box

    manifest = entry['manifest']
    dtype = manifest['type']
    version = manifest.get('version', '')

    def callback(netname=None, box=None):
        ctx = click.get_current_context()
        if ctx.obj is None:
            class _Obj:
                pass
            ctx.obj = _Obj()
        setattr(ctx.obj, 'netname', netname)
        setattr(ctx.obj, 'box', box)
        if ctx.invoked_subcommand is None:
            box_ip = resolve_box(ctx, box, read_only=True)
            display_nets(ctx, box_ip, netname, dtype, dtype)

    description = manifest.get('description') or f"Custom driver '{dtype}'"
    group = NetGroup(
        dtype,
        callback=callback,
        invoke_without_command=True,
        params=[
            click.Argument(['netname'], required=False, metavar='[NET_NAME]'),
            click.Option(['--box'], required=False, help='Lager Box name or IP'),
        ],
        help=f"{description}\n\nCustom driver {dtype} {version}, installed on the box.",
        short_help=description,
    )
    for action, spec in manifest.get('actions', {}).items():
        group.add_command(_action_command(dtype, action, spec))
    first = next(iter(manifest.get('actions', {})), 'ACTION')
    group.net_examples = [
        f"lager {dtype} <NET> {first} --box <BOX>",
        f"lager {dtype} --box <BOX>                (list {dtype} nets)",
    ]
    return group


# ---------------------------------------------------------------------------
# Top-level resolution: `lager <type> ...` for a type the CLI never shipped
# ---------------------------------------------------------------------------

def _box_from_args(args: list[str]) -> str | None:
    for i, token in enumerate(args):
        if token == '--box' and i + 1 < len(args):
            return args[i + 1]
        if token.startswith('--box='):
            return token.split('=', 1)[1]
    return None


def _default_box_name() -> str | None:
    name = os.getenv('LAGER_BOX')
    if name:
        return name
    try:
        from ..config import read_config_file
        return read_config_file()['LAGER'].get('gateway_id') or None
    except Exception:
        return None


def _box_ip(name: str) -> str | None:
    from ..box_storage import get_box_ip
    ip = get_box_ip(name)
    if ip:
        return ip
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        return None


def discover_driver(box_ip: str, name: str) -> dict | None:
    """The box's driver entry for ``name``, asking the box at most once.

    A cached list that has the name answers at once. Otherwise the box is
    asked for a fresh list, since the cached one can predate an install made
    from another machine, unless it did not answer in the last
    UNREACHABLE_TTL seconds or already lacked this name in the last MISS_TTL.
    """
    cached = _load_cache().get(box_ip)
    cached = cached if isinstance(cached, dict) else {}
    entry = find_driver(cached_drivers(box_ip), name)
    if entry is not None:
        return entry
    if _recently(cached, 'unreachable_at', UNREACHABLE_TTL) \
            or _recently(cached, 'missed', MISS_TTL, name):
        return None
    drivers = fetch_drivers(box_ip, use_cache=False)
    if drivers is None:
        _mark_unreachable(box_ip)
        return None
    entry = find_driver(drivers, name)
    if entry is None:
        _mark_miss(box_ip, name)
    return entry


def custom_driver_command(name: str, args: list[str]) -> click.Group | None:
    """The ``lager <name>`` group for a custom driver on the target box, or None.

    The target box is the --box in ``args``, else the default box. Any
    failure (no box, unreachable, no such driver) returns None, and click
    then reports its ordinary "No such command".
    """
    if not _TYPE_RE.match(name or ''):
        return None
    box = _box_from_args(args) or _default_box_name()
    if not box:
        return None
    box_ip = _box_ip(box)
    if not box_ip:
        return None
    entry = discover_driver(box_ip, name)
    if entry is None:
        return None
    try:
        return build_driver_group(entry)
    except Exception:
        return None

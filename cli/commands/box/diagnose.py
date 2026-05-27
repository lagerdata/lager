# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager diagnose <net> --box <box> [--type <type>]`

Classifies a misbehaving instrument net into actionable buckets — host-side,
instrument-wedged, or healthy — by polling three box-side endpoints in
parallel and synthesizing the result. Built after a 2026-05-26 debug
session where it took ~2 hours to walk through `lsof`, `dmesg`, bare
pyvisa probes, and hw_service introspection by hand to root-cause one
EBUSY incident. This command collapses that workflow.

Endpoints used (all introduced in 0.20.0):
  - GET http://<box>:5000/diagnose/usb?address=...      (USB enum + lsof + dmesg + lsmod)
  - GET http://<box>:5000/diagnose/visa?address=...     (bare pyvisa *IDN?)
  - GET http://<box>:8080/diagnose/dispatcher?address=...  (hw_service in-process cache)

Older boxes return 404 per endpoint; the CLI falls back per-section and
prints the available bits with a note that the box is on a pre-0.20 image.
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import click
import requests

from ...box_storage import resolve_and_validate_box_with_name


# Net-role choices for --type. Mirrors the role strings produced by
# NetType.from_role() on the box (box/lager/nets/constants.py). 'auto'
# triggers an HTTP lookup against the box's /nets/list.
NET_ROLE_CHOICES = [
    'auto', 'battery', 'power-supply', 'eload', 'scope', 'debug', 'usb',
    'uart', 'webcam', 'adc', 'dac', 'gpio', 'thermocouple', 'i2c', 'spi',
    'watt-meter', 'energy-analyzer', 'router', 'rotation', 'arm', 'actuate',
    'power-supply-2q', 'wifi', 'analog', 'logic', 'waveform',
]


def _fetch_net_info(
    box_ip: str, net: str, requested_type: str, display_name: str,
) -> tuple[str | None, str | None]:
    """Return (address, role) for the named net by querying /nets/list on
    the box. If requested_type != 'auto' and matches the net's role, use
    that — otherwise the returned role wins."""
    try:
        r = requests.get(f'http://{box_ip}:5000/nets/list', timeout=5)
        r.raise_for_status()
        nets = r.json()
    except requests.exceptions.ConnectionError:
        click.echo(click.style(
            f'Box {display_name!r} unreachable at {box_ip}:5000 (connection refused). '
            f'The lager container may be stopped. Check with:\n'
            f'  lager ssh --box {display_name} -- "sudo docker ps"',
            fg='red'), err=True)
        return None, None
    except requests.exceptions.Timeout:
        click.echo(click.style(
            f'Box {display_name!r} did not respond within 5s at {box_ip}:5000. '
            f'Check network/Tailscale connectivity, then `lager hello --box {display_name}`.',
            fg='red'), err=True)
        return None, None
    except Exception as e:
        click.echo(click.style(f'Could not fetch net list from box: {e}', fg='red'), err=True)
        return None, None

    candidates = [n for n in nets if n.get('name') == net]
    if not candidates:
        click.echo(click.style(f'Net {net!r} not found on box. Available: '
                               f'{", ".join(n.get("name", "") for n in nets[:10])}{"..." if len(nets) > 10 else ""}',
                               fg='red'), err=True)
        return None, None

    if requested_type != 'auto':
        match = [n for n in candidates if n.get('role') == requested_type]
        if not match:
            click.echo(click.style(
                f'Net {net!r} exists but no entry has role={requested_type!r}. '
                f'Available roles: {", ".join(n.get("role", "") for n in candidates)}',
                fg='yellow'), err=True)
        candidates = match or candidates

    chosen = candidates[0]
    # /nets/list shape: 'address' is a top-level field (a VISA-or-other
    # address string). 'instrument' is the human-readable instrument
    # name (e.g. 'Keithley_2281S'), not a nested dict.
    return chosen.get('address'), chosen.get('role')


def _call(url: str, timeout: float = 8.0) -> dict:
    """Fetch a diagnose endpoint. Returns the JSON body on success, or a
    dict with 'unavailable' / 'transport_error' keys on failure so callers
    don't have to retry-catch. Endpoint-returned JSON (including endpoints
    that report their own structured 'error' field) is passed through
    unchanged so the section renderer can show all fields."""
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 404:
            return {'unavailable': 'endpoint not on this box (pre-0.20 image)'}
        if r.status_code >= 400:
            return {'transport_error': f'HTTP {r.status_code}: {r.text[:200]}'}
        return r.json()
    except Exception as e:
        return {'transport_error': str(e)}


def _classify(usb_info: dict, visa_info: dict, disp_info: dict) -> tuple[str, str]:
    """Return (color, headline) based on the three endpoints' state.
    Order matters — most specific actionable diagnosis wins."""
    if usb_info.get('usbtmc_loaded'):
        return ('red',
                'HOST-SIDE: usbtmc kernel module loaded — `lager box update` to install the blacklist.')

    visa_err_class = visa_info.get('error_class')
    if visa_err_class == 'busy':
        lsof_holders = usb_info.get('lsof') or []
        if len(lsof_holders) >= 2:
            who = ', '.join(f"{h.get('command')}({h.get('pid')})" for h in lsof_holders[:4])
            return ('red',
                    f'HOST-SIDE: USB device claimed by multiple processes ({who}). '
                    'A second pyvisa/libusb client is racing the hw service.')
        return ('red',
                'HOST-SIDE: USB device busy — another process holds it. '
                'Inspect with `lager ssh <box>` then `sudo lsof /dev/bus/usb/...`.')

    if visa_err_class == 'nodev':
        return ('yellow',
                'TRANSIENT: device disappeared from USB (re-enumeration). '
                'Hw service should auto-recover on next call (0.20.0+). '
                'If it doesn\'t, `lager ssh <box> && sudo docker restart lager`.')

    if visa_err_class == 'timeout':
        return ('red',
                'INSTRUMENT WEDGED: device enumerates and accepts session open, but won\'t '
                'respond to *IDN?. The instrument firmware is stuck — a mains-side '
                'power-cycle of the instrument itself is required. Software can\'t fix this.')

    if usb_info.get('enumerated') is False:
        return ('red',
                'NOT ENUMERATED: device does not show up on USB. Check power, cable, '
                'and (if behind an Acroname hub) the upstream port.')

    if visa_info.get('idn'):
        return ('green', f'HEALTHY — IDN: {visa_info["idn"]}')

    if visa_info.get('skipped'):
        # hw_service holds a shared session, so the visa probe was skipped.
        # Use the dispatcher info to judge instead.
        if disp_info.get('cached_session') and disp_info.get('cached_drivers'):
            return ('green',
                    'HEALTHY (hw_service has an active shared session for this address; '
                    'fresh pyvisa probe skipped to avoid colliding with it).')

    # Probe couldn't open the device. Two distinct cases here that the
    # endpoint's `is_usbtmc` flag disambiguates:
    #
    #   1. Device IS a USB-TMC instrument (Keithley, Keysight, Rigol, ...)
    #      that pyvisa SHOULD be able to talk to. Fresh-open failure with
    #      "no device found" / "invalid resource" almost always means
    #      box_http_server's libusb context went stale after a USB
    #      re-enumeration (power-cycle, hub toggle). hw_service runs in a
    #      separate process with a separate libusb context and will recover
    #      transparently on the next /invoke. Classify as TRANSIENT with
    #      the recovery hint.
    #   2. Device is a vendor-SDK instrument (LabJack/LJM, Picoscope/Pico
    #      SDK, Acroname/BrainStem) that pyvisa cannot reach by design.
    #      `lager diagnose` doesn't cover these — point the user at the
    #      role-specific CLI subcommand. Same error strings, gated on
    #      `is_usbtmc: False` to disambiguate.
    visa_err_raw = (visa_info.get('error') or '').lower()
    no_device_keywords = (
        'invalid resource', 'no device found', 'parsing error',
        'vi_error_inv_rsrc_name', 'vi_error_rsrc_nfound',
    )
    if any(s in visa_err_raw for s in no_device_keywords):
        if usb_info.get('enumerated') and usb_info.get('is_usbtmc'):
            return ('yellow',
                    "TRANSIENT: device is enumerated as USB-TMC but the fresh "
                    "pyvisa probe couldn't reach it — most often a stale libusb "
                    "context in box_http_server after a USB re-enumeration. "
                    "Run any command for this net (e.g. `lager battery <net> "
                    "state`) so hw_service caches a session, then diagnose will "
                    "report HEALTHY. If it persists, reset libusb state with: "
                    "lager ssh <box> -- 'sudo docker exec lager pkill -f box_http_server'.")
        if usb_info.get('is_usbtmc') is not True:
            return ('yellow',
                    'NOT USB-TMC: this instrument uses a vendor SDK (LabJack/LJM, '
                    'Picoscope/Pico SDK, Acroname/BrainStem, etc.), not pyvisa. '
                    '`lager diagnose` only covers USB-TMC instruments today; for '
                    'this net, check `lager <role> <netname> ...` directly.')

    return ('yellow', 'UNCLEAR — review the per-section output above and rerun if needed.')


def _print_section(title: str, data: dict, fmt_lines):
    click.echo()
    click.echo(click.style(f'== {title} ==', bold=True))
    if 'unavailable' in data:
        click.echo(click.style(f'   {data["unavailable"]}', fg='yellow'))
        return
    if 'transport_error' in data:
        click.echo(click.style(f'   transport error: {data["transport_error"]}', fg='red'))
        return
    for line in fmt_lines(data):
        click.echo(f'   {line}')


@click.command()
@click.argument('net')
@click.option('--box', required=False, help='Lagerbox name or IP')
@click.option('--type', 'net_type', type=click.Choice(NET_ROLE_CHOICES), default='auto',
              help='Net role; auto-detected from saved nets if omitted')
@click.pass_context
def diagnose(ctx, net, box, net_type):
    """Diagnose a misbehaving instrument net.

    Polls the box's diagnose endpoints in parallel (USB enum + lsof + dmesg,
    bare pyvisa *IDN?, hw_service in-process cache) and classifies the net's
    state as host-side, instrument-wedged, healthy, or unclear — with the
    specific action to take next.

    Example:

        lager diagnose battery1 --box PRD-1
        lager diagnose supply1 --box PRD-1 --type power-supply
    """
    resolved_box, box_name = resolve_and_validate_box_with_name(ctx, box)
    display_name = box_name or resolved_box

    address, role = _fetch_net_info(resolved_box, net, net_type, display_name)
    if not address:
        click.echo(click.style('Cannot diagnose without a VISA address.', fg='red'), err=True)
        ctx.exit(1)

    click.echo(click.style(f'lager diagnose — {display_name} → {net}', bold=True))
    click.echo(f'  NetType: {role}    address: {address}')

    # Fire the three endpoints in parallel.
    # Port mapping inside the box container:
    #   5000 — lager.python.service (legacy /cli-version, /status, /nets/list)
    #   8080 — hardware_service.py  (/invoke, /diagnose/dispatcher)
    #   9000 — box_http_server.py   (Flask+SocketIO; /diagnose/usb, /diagnose/visa)
    urls = {
        'usb': f'http://{resolved_box}:9000/diagnose/usb?address={address}',
        'visa': f'http://{resolved_box}:9000/diagnose/visa?address={address}',
        'dispatcher': f'http://{resolved_box}:8080/diagnose/dispatcher?address={address}',
    }
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_call, url): name for name, url in urls.items()}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    _print_section('USB (host-side)', results['usb'], lambda d: [
        f'enumerated:   {d.get("enumerated")}',
        f'sysfs:        {d.get("sysfs_path") or "—"}',
        f'device:       {d.get("device_path") or "—"}',
        f'usb-tmc class:{" yes" if d.get("is_usbtmc") else " no" if "is_usbtmc" in d else " —"}',
        f'usbtmc kmod:  {"LOADED (problem)" if d.get("usbtmc_loaded") else "not loaded (good)"}',
        f'lsof:         {", ".join(f"{h.get('command')}({h.get('pid')})" for h in (d.get("lsof") or [])) or "no holders"}',
        f'dmesg tail:   {d.get("dmesg_tail", "")[:300] or "(empty)"}',
    ])

    _print_section('VISA (instrument-side)', results['visa'], lambda d: [
        f'idn:         {d.get("idn") or "—"}',
        f'elapsed:     {d.get("elapsed_ms", "?")} ms',
        f'error:       {d.get("error") or "—"}',
        f'error_class: {d.get("error_class") or "—"}',
        f'skipped:     {d.get("reason") if d.get("skipped") else "—"}',
    ])

    _print_section('Dispatcher (hw_service in-process)', results['dispatcher'], lambda d: [
        f'cached_session:  {d.get("cached_session")}',
        f'cached_drivers:  {", ".join(f"{c.get("device_name")}({c.get("driver_class")})" for c in (d.get("cached_drivers") or [])) or "—"}',
        f'shared_pool:     {d.get("shared_pool_size")} entry/entries',
    ])

    color, headline = _classify(results['usb'], results['visa'], results['dispatcher'])
    click.echo()
    click.echo(click.style(f'Classification: {headline}', fg=color, bold=True))

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
  - GET http://<box>:9000/diagnose/usb?address=...      (USB enum + lsof + dmesg + lsmod)
  - GET http://<box>:9000/diagnose/visa?address=...     (bare pyvisa *IDN?)
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
        r = requests.get(f'http://{box_ip}:9000/nets/list', timeout=5)
        r.raise_for_status()
        nets = r.json()
        # Older box images wrap the list: {"nets": [...]}. Unwrap so the
        # comprehensions below always iterate dicts.
        if isinstance(nets, dict):
            nets = nets.get('nets', [])
    except requests.exceptions.ConnectionError:
        click.echo(click.style(
            f'Box {display_name!r} unreachable at {box_ip}:9000 (connection refused). '
            f'The lager container may be stopped. Check with:\n'
            f'  lager ssh --box {display_name} -- "sudo docker ps"',
            fg='red'), err=True)
        return None, None
    except requests.exceptions.Timeout:
        click.echo(click.style(
            f'Box {display_name!r} did not respond within 5s at {box_ip}:9000. '
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


def _fmt_usb_lines(d: dict):
    """Render the host-side USB section. Shared by the USB-TMC and debug
    (J-Link) paths — the `/diagnose/usb` endpoint is instrument-agnostic."""
    return [
        f'enumerated:   {d.get("enumerated")}',
        f'sysfs:        {d.get("sysfs_path") or "—"}',
        f'device:       {d.get("device_path") or "—"}',
        f'usb-tmc class:{" yes" if d.get("is_usbtmc") else " no" if "is_usbtmc" in d else " —"}',
        f'usbtmc kmod:  {"LOADED (problem)" if d.get("usbtmc_loaded") else "not loaded (good)"}',
        f"lsof:         {', '.join(h.get('command', '?') + '(' + h.get('pid', '?') + ')' for h in (d.get('lsof') or [])) or 'no holders'}",
        f'dmesg tail:   {d.get("dmesg_tail", "")[:300] or "(empty)"}',
    ]


def _holders_str(d: dict) -> str:
    """Comma-joined `command(pid)` for the probe's USB holders, or 'none'."""
    holders = d.get('holders') or []
    return ', '.join(f"{h.get('command', '?')}({h.get('pid', '?')})" for h in holders) or 'none'


def _vtref_str(connect: dict) -> str:
    """Format VTref (stored in mV) as 'X.XXXV', or 'unknown'."""
    vt = (connect or {}).get('vtref_mv')
    return f'{vt / 1000:.3f}V' if isinstance(vt, (int, float)) else 'unknown'


def _fmt_jlink_lines(d: dict):
    """Render the J-Link / debug-probe section from `/diagnose/jlink`."""
    if d.get('mode') == 'openocd-basic':
        oo = d.get('openocd_gdbserver') or {}
        return [
            f"backend:        {d.get('backend')}",
            f"probe enum:     {d.get('probe_enumerated')}",
            f"holders:        {_holders_str(d)}",
            f"openocd server: running={oo.get('running')} pid={oo.get('pid')}",
            "note:           deep target diagnosis is J-Link-only for now",
        ]
    emu = d.get('emu_list') or []
    visible_str = ', '.join(
        f"{p.get('product', '?')}/{p.get('serial') or '?'}" for p in emu
    ) or 'none'
    g = d.get('gdbserver') or {}
    lines = [
        f"backend:        {d.get('backend')}",
        f"jlink software: {'installed' if d.get('jlink_installed') else 'NOT INSTALLED'}",
        f"probe enum:     {d.get('probe_enumerated')}",
        f"probe visible:  {d.get('probe_visible')}  (emus: {visible_str})",
        f"holders:        {_holders_str(d)}",
        f"gdbserver:      running={g.get('running')} pid={g.get('pid')} log_ok={g.get('logfile_ok')}",
    ]
    if d.get('connect_skipped'):
        lines.append(f"connect:        skipped ({d.get('connect_skip_reason')})")
    elif d.get('connect'):
        c = d['connect']
        lines.append(
            f"connect:        ok={c.get('connect_ok')} class={c.get('connect_error_class')} "
            f"VTref={_vtref_str(c)} core={c.get('core') or '—'}"
        )
        # When we couldn't classify the connect, show the raw JLinkExe text so
        # an unrecognized failure is debuggable instead of a dead-end.
        if c.get('connect_error_class') == 'other' and d.get('connect_output'):
            raw = ' / '.join(s for s in str(d['connect_output']).splitlines() if s.strip())
            lines.append(f"connect output: {raw[:400]}")
    if d.get('connect_error'):
        lines.append(f"connect error:  {str(d.get('connect_error'))[:200]}")
    return lines


def _classify_jlink(usb_info: dict, jlink_info: dict) -> tuple[str, str]:
    """Return (color, headline) for a debug net from the USB + J-Link payloads.

    Walks the J-Link stack outside-in (software → USB → probe-visible →
    gdbserver → target connect) so the most specific actionable fault wins."""
    # Endpoint reachability first — an old box won't have /diagnose/jlink.
    if 'unavailable' in jlink_info:
        return ('yellow',
                'J-Link diagnose endpoint not on this box — deploy this build '
                '(`lager update --box <box> --version <branch>`) to enable it.')
    if 'transport_error' in jlink_info:
        return ('red', f'Could not reach the J-Link diagnose endpoint: {jlink_info["transport_error"]}')
    if jlink_info.get('error'):
        return ('red', f'J-Link diagnose error on box: {jlink_info["error"]}')

    if jlink_info.get('mode') == 'openocd-basic':
        if jlink_info.get('probe_enumerated') is False:
            return ('red',
                    'PROBE NOT ON USB: OpenOCD/ST-Link probe not enumerated. '
                    'Check cable, probe power, and (behind a hub) the upstream port.')
        oo = jlink_info.get('openocd_gdbserver') or {}
        return ('yellow',
                f'OPENOCD ({jlink_info.get("backend")}): probe enumerated, gdbserver '
                f'{"running" if oo.get("running") else "idle"}. Deep target diagnosis is '
                'J-Link-only for now — use `lager debug <net> status` and the gdbserver log.')

    if jlink_info.get('jlink_installed') is False:
        return ('red',
                'J-LINK SOFTWARE MISSING on box — install the SEGGER J-Link tools '
                '(`lager update --box <box>` installs them).')

    if jlink_info.get('probe_enumerated') is False:
        return ('red',
                'PROBE NOT ON USB: J-Link not enumerated. Check the USB cable, probe '
                'power, and (behind an Acroname hub) the upstream port.')

    if jlink_info.get('probe_visible') is False:
        holders = jlink_info.get('holders') or usb_info.get('lsof') or []
        if holders:
            who = ', '.join(f"{h.get('command')}({h.get('pid')})" for h in holders[:4])
            return ('red',
                    f'PROBE CLAIMED: J-Link is on USB but JLinkExe can\'t see it — another '
                    f'process holds it ({who}). Usually a stale gdbserver: '
                    '`lager debug <net> disconnect`, or restart the debug service.')
        return ('red',
                'PROBE WEDGED: J-Link is on USB but JLinkExe enumeration is empty — the '
                'probe firmware is likely stuck. USB power-cycle the probe (unplug/replug it).')

    g = jlink_info.get('gdbserver') or {}
    if g.get('running'):
        if g.get('logfile_ok'):
            return ('green',
                    f'HEALTHY: J-Link gdbserver running (PID {g.get("pid")}) and listening — '
                    'active debug session.')
        return ('red',
                'GDBSERVER WEDGED: server process is up but its log shows a target-connection '
                'failure. `lager debug <net> disconnect` then reconnect; if it persists, '
                'restart the debug service.')

    if jlink_info.get('connect_skipped'):
        return ('yellow',
                f'INCONCLUSIVE: connect probe skipped ({jlink_info.get("connect_skip_reason")}). '
                'Probe is visible and idle — rerun, or `lager debug <net> gdbserver` to attach.')

    c = jlink_info.get('connect')
    if c:
        klass = c.get('connect_error_class')
        vt = _vtref_str(c)
        device = jlink_info.get('device') or '<device>'
        if klass == 'ok':
            # JLinkExe doesn't emit a parseable VTref on every firmware/REPL
            # path, so only surface it when we actually got a number.
            vt_part = f', VTref={vt}' if vt != 'unknown' else ''
            return ('green',
                    f'HEALTHY: J-Link connected to {device} '
                    f'({c.get("core") or "core identified"}{vt_part}).')
        if klass == 'no_target_power':
            vt_part = f'VTref={vt}' if vt != 'unknown' else 'J-Link reports target voltage too low'
            return ('red',
                    f'TARGET UNPOWERED: probe is fine but {vt_part} — the target board has no '
                    'power on the debug header (or VTref isn\'t wired). Check target power and '
                    'the VTref pin.')
        if klass == 'locked':
            return ('red',
                    'TARGET LOCKED: debug access is blocked by readout/IDCODE/AP protection. '
                    'A mass-erase/unlock is required (e.g. `nrfjprog --recover` for nRF, or the '
                    'vendor unlock flow).')
        if klass == 'wrong_device':
            return ('yellow',
                    f'DEVICE NAME: J-Link rejected device {device!r}. Fix the net\'s device/MCU '
                    'field (e.g. NRF5340_XXAA_APP for an nRF7002-DK).')
        if klass == 'no_target_comms':
            return ('red',
                    f'NO TARGET COMMS: probe + target power OK (VTref={vt}) but SWD/JTAG connect '
                    'failed. Check SWDIO/SWCLK wiring, nRST pull-up (not held low), SWD-vs-JTAG, '
                    'and try a lower speed.')
        return ('yellow',
                f'UNCLEAR: connect returned class={klass}. See the J-Link section and connect '
                'output above.')

    return ('yellow', 'UNCLEAR — review the J-Link section above and rerun if needed.')


def _diagnose_debug(box_ip: str, net: str, address: str) -> None:
    """Debug-net (J-Link) branch of `lager diagnose`.

    Fetches the instrument-agnostic host-side USB section plus the new
    `/diagnose/jlink` endpoint (both on box_http_server, port 9000) in
    parallel, renders them, and prints the J-Link classification."""
    urls = {
        'usb': f'http://{box_ip}:9000/diagnose/usb?address={address}',
        'jlink': f'http://{box_ip}:9000/diagnose/jlink?net={net}',
    }
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_call, url): name for name, url in urls.items()}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    _print_section('USB (host-side)', results['usb'], _fmt_usb_lines)
    _print_section('J-Link / debug probe', results['jlink'], _fmt_jlink_lines)

    color, headline = _classify_jlink(results['usb'], results['jlink'])
    click.echo()
    click.echo(click.style(f'Classification: {headline}', fg=color, bold=True))


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

        lager diagnose battery1 --box <BOX>
        lager diagnose supply1 --box <BOX> --type power-supply
    """
    resolved_box, box_name = resolve_and_validate_box_with_name(ctx, box)
    display_name = box_name or resolved_box

    address, role = _fetch_net_info(resolved_box, net, net_type, display_name)
    if not address:
        click.echo(click.style('Cannot diagnose without a VISA address.', fg='red'), err=True)
        ctx.exit(1)

    click.echo(click.style(f'lager diagnose — {display_name} → {net}', bold=True))
    click.echo(f'  NetType: {role}    address: {address}')

    # Debug nets (J-Link / OpenOCD) are not USB-TMC — the pyvisa *IDN? probe
    # below can't reach them. Route them to the J-Link-aware path instead.
    if role == 'debug':
        _diagnose_debug(resolved_box, net, address)
        return

    # Fire the three endpoints in parallel.
    # Port mapping inside the box container:
    #   8080 — hardware_service.py  (/invoke, /diagnose/dispatcher)
    #   9000 — box_http_server.py   (Flask+SocketIO; /diagnose/usb, /diagnose/visa, /nets/list)
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

    _print_section('USB (host-side)', results['usb'], _fmt_usb_lines)

    _print_section('VISA (instrument-side)', results['visa'], lambda d: [
        f'idn:         {d.get("idn") or "—"}',
        f'elapsed:     {d.get("elapsed_ms", "?")} ms',
        f'error:       {d.get("error") or "—"}',
        f'error_class: {d.get("error_class") or "—"}',
        f'skipped:     {d.get("reason") if d.get("skipped") else "—"}',
    ])

    _print_section('Dispatcher (hw_service in-process)', results['dispatcher'], lambda d: [
        f'cached_session:  {d.get("cached_session")}',
        f"cached_drivers:  {', '.join(c.get('device_name', '?') + '(' + c.get('driver_class', '?') + ')' for c in (d.get('cached_drivers') or [])) or '—'}",
        f'shared_pool:     {d.get("shared_pool_size")} entry/entries',
    ])

    color, headline = _classify(results['usb'], results['visa'], results['dispatcher'])
    click.echo()
    click.echo(click.style(f'Classification: {headline}', fg=color, bold=True))

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Bluetooth LE sniffer commands.

``lager ble-sniffer NET capture`` records the Bluetooth LE radio traffic a
sniffer net hears for a fixed time and saves it here as a pcap file. It starts
a capture job on the box ("start"), polls it ("job"), sends "stop" on Ctrl+C,
then downloads the file from /tmp/lager-output through GET /download-file and
deletes the box copy unless ``--keep`` is given.
"""
from __future__ import annotations

import gzip
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import click

from ...context import get_default_net
from ...core.net_group import NetGroup
from ...core.net_helpers import (
    NET_HTTP_PORT,
    display_nets,
    post_net_command,
    resolve_box,
    resolve_box_locked,
    validate_net_exists,
)

SNIFFER_ROLE = "ble-sniffer"

# Same limits as box/lager/protocols/ble_sniffer/ble_sniffer_net.py.
MIN_DURATION_S = 1.0
MAX_DURATION_S = 86400.0
RSSI_MIN_DBM = -255
RSSI_MAX_DBM = -10

_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
# Seconds between job polls while a capture runs, and the shortest wait (near
# the end, and after Ctrl+C, when a stop takes effect within one 50 ms read).
_POLL_S = 1.0
_MIN_POLL_S = 0.2
# (connect, read) timeout for the pcap download.
_DOWNLOAD_TIMEOUT_S = (7, 120)
# HTTP timeout for "start". The box waits up to 10 s for the sniffer to begin
# recording (_START_TIMEOUT_S in capture_jobs.py), after locking and scanning
# USB, then returns the job. A client timeout at or under that gives up on a
# job that still starts, and nothing would stop or download it.
_START_HTTP_TIMEOUT_S = 30.0
# After Ctrl+C during "start": how many times, a second apart, to send "stop"
# while the box may still be registering the capture.
_STOP_AFTER_START_TRIES = 3


def _require_netname(ctx: click.Context) -> str | None:
    netname = getattr(ctx.obj, "ble_sniffer_netname", None)
    if netname is None:
        netname = get_default_net(ctx, "ble_sniffer")
    return netname


def _resolve_net(ctx: click.Context, box: str | None, lock: bool) -> tuple[str, str] | None:
    """Box address and net name for a subcommand, or None after listing nets."""
    if box is None:
        box = getattr(ctx.obj, "ble_sniffer_box", None)
    box_ip = resolve_box_locked(ctx, box, "ble-sniffer") if lock else resolve_box(
        ctx, box, read_only=True)
    netname = _require_netname(ctx)
    if netname is None:
        display_nets(ctx, box_ip, None, SNIFFER_ROLE, "BLE sniffer")
        return None
    netname = netname.strip()
    if validate_net_exists(ctx, box_ip, netname, SNIFFER_ROLE) is None:
        return None
    return box_ip, netname


def _download(box_ip: str, box_path: str) -> bytes:
    """Fetch a capture file from the box.

    Raises:
        click.ClickException: The box did not return the file.
    """
    import requests

    from ...box_storage import _check_gateway
    from ...gateway_auth import auth_headers_for_box

    url = f"http://{box_ip}:{NET_HTTP_PORT}/download-file"
    try:
        resp = requests.get(url, params={"filename": box_path},
                            headers=auth_headers_for_box(box_ip),
                            timeout=_DOWNLOAD_TIMEOUT_S)
        resp = _check_gateway(resp, box_ip)
    except requests.RequestException as e:
        raise click.ClickException(f"Download of {box_path} failed: {e}") from e
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error") or resp.text
        except ValueError:
            detail = resp.text
        raise click.ClickException(
            f"Download of {box_path} failed: HTTP {resp.status_code} {detail}".strip())
    content = resp.content
    # A box behind the gateway can answer gzipped; a pcap never starts 1f 8b.
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    return content


@click.group(name="ble-sniffer", cls=NetGroup, invoke_without_command=True,
             help="Capture Bluetooth LE radio traffic to pcap with a sniffer net")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.argument("netname", required=False, metavar="[NET_NAME]")
@click.pass_context
def ble_sniffer(ctx, box, netname):
    """BLE sniffer group. Usage: lager ble-sniffer [NET_NAME] [COMMAND] --box [BOX_NAME]"""
    if netname is None:
        netname = get_default_net(ctx, "ble_sniffer")
    if netname is not None:
        ctx.obj.ble_sniffer_netname = netname
    ctx.obj.ble_sniffer_box = box

    if ctx.invoked_subcommand is None:
        box_ip = resolve_box(ctx, box, read_only=True)
        display_nets(ctx, box_ip, None, SNIFFER_ROLE, "BLE sniffer")


ble_sniffer.net_examples = [
    "lager ble-sniffer sniffer1 capture --duration 10 --box <BOX>",
    "lager ble-sniffer sniffer1 capture -d 30 --follow AA:BB:CC:DD:EE:FF --out dut.pcap --box <BOX>",
    "lager ble-sniffer sniffer1 capture --rssi-cut-off -70 --box <BOX>",
    "lager ble-sniffer sniffer1 status --box <BOX>",
    "lager ble-sniffer sniffer1 stop --box <BOX>",
    "lager ble-sniffer --box <BOX>             (list BLE sniffer nets)",
]


def _check_options(duration, follow, address_type, follow_by_name, only_advertising, coded,
                   rssi_cut_off):
    # The same rules as CaptureOptions.validate() on the box, so a bad
    # combination fails here before the box is locked.
    if not MIN_DURATION_S <= duration <= MAX_DURATION_S:
        raise click.BadParameter(
            f"must be between {MIN_DURATION_S:g} and {MAX_DURATION_S:g} seconds",
            param_hint="--duration")
    if follow is not None and not _ADDRESS_RE.match(follow):
        raise click.BadParameter(f"'{follow}' is not an address like AA:BB:CC:DD:EE:FF",
                                 param_hint="--follow")
    if follow is not None and follow_by_name is not None:
        raise click.UsageError("--follow and --follow-by-name cannot be combined")
    if address_type is not None and follow is None:
        raise click.UsageError("--public and --random apply only to --follow")
    if only_advertising and follow is None and follow_by_name is None:
        raise click.UsageError("--only-advertising needs --follow or --follow-by-name")
    if coded and follow_by_name is not None:
        raise click.UsageError("--follow-by-name cannot be combined with --coded: names are "
                               "read from legacy advertising, which is not sent on the LE "
                               "Coded PHY")
    if rssi_cut_off is not None and not RSSI_MIN_DBM <= rssi_cut_off <= RSSI_MAX_DBM:
        raise click.BadParameter(f"must be between {RSSI_MIN_DBM} and {RSSI_MAX_DBM} dBm",
                                 param_hint="--rssi-cut-off")


def _give_up(netname: str) -> NoReturn:
    click.secho(f"Not downloaded. 'lager ble-sniffer {netname} status' shows "
                "the file on the box, which is kept for a day.", fg="yellow", err=True)
    raise click.exceptions.Exit(130)


def _stop_after_interrupted_start(ctx, box_ip: str, netname: str) -> NoReturn:
    """Ctrl+C while "start" was in flight: stop whatever capture it began, then exit 130.

    The box keeps handling a request the client gave up on, so the capture
    can start after the Ctrl+C. Without a stop it would hold the sniffer for
    its whole duration with no client to end it or download the file.
    """
    click.secho("Stopping the capture on the Lager Box...", fg="yellow", err=True)
    stopped = False
    try:
        for attempt in range(_STOP_AFTER_START_TRIES):
            if attempt:
                time.sleep(1.0)
            try:
                value = post_net_command(ctx, box_ip, netname, "stop", role=SNIFFER_ROLE,
                                         quiet=True).get("value") or {}
            except SystemExit:
                continue
            if value.get("stopped"):
                stopped = True
                break
    except KeyboardInterrupt:
        pass
    if not stopped:
        click.secho(f"If a capture started, 'lager ble-sniffer {netname} stop' ends it.",
                    fg="yellow", err=True)
    raise click.exceptions.Exit(130)


def _poll(ctx, box_ip: str, netname: str, job_id: str, duration: float) -> tuple[dict, bool]:
    """Poll a capture job until it ends; ``(job, interrupted)``.

    The first Ctrl+C asks the box to stop the capture and keeps polling, so
    what was recorded is still downloaded; a second one exits with status
    130 and leaves the file on the box.
    """
    interrupted = False
    started = time.monotonic()
    while True:
        try:
            left = duration - (time.monotonic() - started)
            time.sleep(min(_POLL_S, max(left, _MIN_POLL_S)) if not interrupted else _MIN_POLL_S)
            job = post_net_command(ctx, box_ip, netname, "job", role=SNIFFER_ROLE, quiet=True,
                                   job=job_id).get("value") or {}
        except KeyboardInterrupt:
            if interrupted:
                _give_up(netname)
            interrupted = True
            click.secho("Stopping the capture on the Lager Box...", fg="yellow", err=True)
            try:
                post_net_command(ctx, box_ip, netname, "stop", role=SNIFFER_ROLE, quiet=True)
            except KeyboardInterrupt:
                _give_up(netname)
            continue
        if job.get("state") in ("done", "failed"):
            return job, interrupted


@ble_sniffer.command(name="capture", help="Capture Bluetooth LE traffic for a fixed time to a pcap file")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--duration", "-d", type=float, default=10.0, show_default=True,
              help=f"Capture time in seconds ({MIN_DURATION_S:g}-{MAX_DURATION_S:g})")
@click.option("--out", "-o", "out_path", type=click.Path(dir_okay=False, writable=True),
              default=None, help="Local pcap file to write (default: <NET>-<UTC time>.pcap)")
@click.option("--follow", default=None, metavar="ADDRESS",
              help="Follow the device with this address (AA:BB:CC:DD:EE:FF) and its connections")
@click.option("--public", "address_type", flag_value="public", default=None,
              help="The --follow address is public (default: taken from its advertising)")
@click.option("--random", "address_type", flag_value="random",
              help="The --follow address is random (default: taken from its advertising)")
@click.option("--follow-by-name", default=None, metavar="NAME",
              help="Follow the first device whose legacy advertising carries this exact name")
@click.option("--only-advertising", is_flag=True, default=False,
              help="With --follow or --follow-by-name, record the device's advertising only")
@click.option("--coded", is_flag=True, default=False,
              help="Scan and follow on the LE Coded PHY")
@click.option("--rssi-cut-off", type=int, default=None, metavar="DBM",
              help=f"Drop advertising packets weaker than this RSSI "
                   f"({RSSI_MIN_DBM} to {RSSI_MAX_DBM} dBm)")
@click.option("--keep", is_flag=True, default=False,
              help="Keep the capture file on the Lager Box (for a day) after the download")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.pass_context
def capture(ctx, box, duration, out_path, follow, address_type, follow_by_name,
            only_advertising, coded, rssi_cut_off, keep, as_json):
    _check_options(duration, follow, address_type, follow_by_name, only_advertising, coded,
                   rssi_cut_off)
    resolved = _resolve_net(ctx, box, lock=True)
    if resolved is None:
        return
    box_ip, netname = resolved

    if out_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = f"{netname}-{stamp}.pcap"
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        started = post_net_command(
            ctx, box_ip, netname, "start", role=SNIFFER_ROLE, quiet=True,
            http_timeout=_START_HTTP_TIMEOUT_S,
            duration=duration, follow=follow, address_type=address_type,
            follow_by_name=follow_by_name, only_advertising=only_advertising,
            coded=coded, rssi_cut_off=rssi_cut_off,
        ).get("value") or {}
    except KeyboardInterrupt:
        _stop_after_interrupted_start(ctx, box_ip, netname)
    job_id = started.get("job")
    if not job_id:
        raise click.ClickException("The Lager Box returned no capture job")
    if not as_json:
        click.secho(f"Capturing on '{netname}' for {duration:g} s (Ctrl+C stops early)...",
                    fg="green")
    job, interrupted = _poll(ctx, box_ip, netname, job_id, duration)

    failed = job.get("state") == "failed"
    if failed and not job.get("file_saved"):
        raise click.ClickException(job.get("error") or "The capture failed")
    value = job.get("result") or {}
    box_path = value.get("file") or job.get("file")
    try:
        data = _download(box_ip, box_path)
    except KeyboardInterrupt:
        # The capture has ended; only the download stopped. Say where the file is.
        _give_up(netname)
    out.write_bytes(data)
    packets = value.get("packets", _count_records(data))

    deleted = False
    if not keep:
        try:
            post_net_command(ctx, box_ip, netname, "delete", role=SNIFFER_ROLE, quiet=True,
                             file=box_path)
            deleted = True
        except SystemExit:
            # The capture is saved here; only the cleanup on the box failed,
            # and post_net_command printed why.
            click.secho(f"Warning: the box copy {box_path} was not deleted", fg="yellow",
                        err=True)

    if follow or follow_by_name:
        _warn_follow(value, follow, follow_by_name)
    stopped = interrupted or bool(value.get("stopped"))
    if as_json:
        click.echo(json.dumps({
            "netname": netname,
            "job": job_id,
            "file": str(out),
            "box_file": None if deleted else box_path,
            "bytes": len(data),
            "packets": packets,
            "duration": value.get("duration", job.get("elapsed")),
            "port": value.get("port"),
            "firmware": value.get("firmware"),
            "followed": value.get("followed"),
            "follow_seen": value.get("follow_seen"),
            "stopped": stopped,
            "error": job.get("error"),
        }))
    else:
        how = "stopped early, " if stopped else ""
        took = value.get("duration", job.get("elapsed", 0.0))
        click.secho(f"Saved {out} ({how}{len(data)} bytes, {packets} packets, {took:g} s)",
                    fg="yellow" if failed else "green")
        if value.get("followed") and value.get("follow_seen"):
            click.echo(f"Followed {value['followed']}")
        if not deleted:
            click.echo(f"Box copy: {box_path}")
    if failed:
        raise click.ClickException(job.get("error") or "The capture failed")
    if interrupted:
        ctx.exit(130)


def _warn_follow(value: dict, follow: str | None, follow_by_name: str | None) -> None:
    if value.get("follow_seen") is not False:
        return
    what = f"'{follow_by_name}'" if follow_by_name else follow
    if value.get("followed") is None:
        hint = ("no advertisement with that name was heard" if follow_by_name
                else "it was not heard advertising, so its address type was not known")
    else:
        hint = "check the address and --public/--random"
    click.secho(f"Warning: nothing from {what} was captured; {hint}", fg="yellow", err=True)


def _count_records(data: bytes) -> int:
    # Records in a little-endian classic pcap, for a capture whose result was
    # lost to Ctrl+C; the box writes only that format.
    count, offset = 0, 24
    while offset + 16 <= len(data):
        length = int.from_bytes(data[offset + 8:offset + 12], "little")
        if offset + 16 + length > len(data):
            break
        count += 1
        offset += 16 + length
    return count


@ble_sniffer.command(name="stop", help="Stop the capture on the sniffer")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.pass_context
def stop(ctx, box, as_json):
    # Locked like capture: stopping ends a capture, so it must not reach one
    # running on a box another user has locked.
    resolved = _resolve_net(ctx, box, lock=True)
    if resolved is None:
        return
    box_ip, netname = resolved
    result = post_net_command(ctx, box_ip, netname, "stop", role=SNIFFER_ROLE, quiet=True)
    value = result.get("value") or {}
    if as_json:
        click.echo(json.dumps({"netname": netname, **value}))
        return
    click.echo(result.get("message", ""))
    if value.get("file"):
        click.echo(f"The capture ends within a second; its file on the box is "
                   f"{value['file']} (kept for a day)")


@ble_sniffer.command(name="status", help="Show the sniffer's serial port and whether it is busy")
@click.option("--box", required=False, help="Lager Box name or IP")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit a machine-readable JSON object instead of formatted text")
@click.pass_context
def status(ctx, box, as_json):
    resolved = _resolve_net(ctx, box, lock=False)
    if resolved is None:
        return
    box_ip, netname = resolved
    result = post_net_command(ctx, box_ip, netname, "status", role=SNIFFER_ROLE, quiet=True)
    value = result.get("value") or {}
    if as_json:
        click.echo(json.dumps({"netname": netname, **value}))
        return
    click.echo(result.get("message", ""))
    job = value.get("job")
    if job:
        state = job["state"]
        if (job.get("result") or {}).get("stopped"):
            state += " (stopped early)"
        click.echo(f"Last capture: {state}, {job['packets']} packets in "
                   f"{job['elapsed']:g} of {job['duration']:g} s, {job['file']}")

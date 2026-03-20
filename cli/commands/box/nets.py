# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
nets.py – "lager nets …" CLI group
-------------------------------------------
List all saved nets
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from typing import Any, List, Optional
from collections import defaultdict

import click
from texttable import Texttable
import shutil

from ...context import get_default_box, get_impl_path
from ...sort_utils import natural_sort_key as _natural_sort_key
from ..development.python import run_python_internal
from .net_tui import launch_tui
from ...options import force_command_option


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

def _parse_backend_json(raw: str) -> Any:
    """
    Parse JSON response from backend, handling duplicate output from double execution.

    Args:
        raw: Raw output from backend

    Returns:
        Parsed JSON data

    Raises:
        json.JSONDecodeError: If JSON cannot be parsed
    """
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        # Handle duplicate JSON output if present
        if raw and raw.count('[') >= 2:
            # Try to extract the first JSON array
            depth = 0
            first_array_end = -1
            for i, char in enumerate(raw):
                if char == '[':
                    depth += 1
                elif char == ']':
                    depth -= 1
                    if depth == 0:
                        first_array_end = i + 1
                        break

            if first_array_end > 0:
                first_json = raw[:first_array_end]
                return json.loads(first_json)
            else:
                raise json.JSONDecodeError("Could not find complete JSON array", raw, 0)
        else:
            # Handle duplicate JSON objects (e.g., {"ok": true}{"ok": true})
            if raw and raw.count('{') >= 2:
                depth = 0
                first_obj_end = -1
                for i, char in enumerate(raw):
                    if char == '{':
                        depth += 1
                    elif char == '}':
                        depth -= 1
                        if depth == 0:
                            first_obj_end = i + 1
                            break

                if first_obj_end > 0:
                    first_json = raw[:first_obj_end]
                    return json.loads(first_json)

            raise  # Re-raise original exception

_MULTI_HUBS = {"LabJack_T7", "Acroname_8Port", "Acroname_4Port"}
_SINGLE_CHANNEL_INST = {
    "Keithley_2281S": ("batt", "supply"),
    "EA_PSB_10060_60": ("solar", "supply"),
    "EA_PSB_10080_60": ("solar", "supply"),
}
INSTRUMENT_NET_MAP: dict[str, list[str]] = {
    # supply
    "Rigol_DP811": ["supply"],
    "Rigol_DP821": ["supply"],
    "Rigol_DP831": ["supply"],
    "EA_PSB_10080_60": ["supply", "solar"],
    "EA_PSB_10060_60": ["supply", "solar"],
    "KEYSIGHT_E36233A": ["supply"],
    "KEYSIGHT_E36313A": ["supply"],

    # batt
    "Keithley_2281S": ["batt", "supply"],

    # scope
    "Rigol_MS05204": ["scope"],
    "Picoscope_2000": ["scope"],

    # adc / gpio / dac / spi
    "LabJack_T7": ["gpio", "adc", "dac", "spi", "i2c"],
    "Aardvark": ["spi", "i2c", "gpio"],
    "FTDI_FT232H": ["spi", "i2c", "gpio"],

    # debug
    "J-Link": ["debug"],
    "J-Link_Plus": ["debug"],
    "Flasher_ARM": ["debug"],

    # usb
    "Acroname_8Port": ["usb"],
    "Acroname_4Port": ["usb"],
    "YKUSH_Hub": ["usb"],

    # eload
    "Rigol_DL3021": ["eload"],

    # webcam
    "Logitech_BRIO_HD": ["webcam"],
    "Logitech_BRIO": ["webcam"],
    "Logitech_C930e": ["webcam"],

    # (robot) arm
    "Rotrix_Dexarm": ["arm"],

    # watt-meter
    "Yocto_Watt": ["watt-meter"],

    # uart
    "Prolific_USB_Serial": ["uart"],
    "SiLabs_CP210x": ["uart"],
    "FTDI_FT232R": ["uart"],
    "FTDI_FT4232H": ["uart"],
    "ESP32_JTAG_Serial": ["uart"],
}

def _run_net_py(ctx: click.Context, box: str, *net_args: str) -> str:
    """
    Run `net.py …` via run_python_internal and capture stdout.
    """
    from ..development.python import run_python_internal_get_output

    try:
        output = run_python_internal_get_output(
            ctx,
            get_impl_path("net.py"),
            box,
            env=(),
            passenv=(),
            kill=False,
            download=(),
            allow_overwrite=False,
            signum="SIGTERM",
            timeout=30,  # 30 second timeout to prevent hanging
            detach=False,
            port=(),
            org=None,
            args=net_args,
        )
        return output.decode('utf-8') if isinstance(output, bytes) else output
    except SystemExit as e:
        # Re-raise non-zero exits (actual errors), return empty for success exits
        if e.code != 0:
            raise
        return ""


def _resolve_box(ctx: click.Context, box_opt: Optional[str] = None) -> str:
    """
    Resolve box precedence:
    1. explicit --box given to this sub-command (check local boxes first)
    2. --box passed to the *parent* ("nets …") command (check local boxes first)
    3. get_default_box(ctx) (automatically resolves local box names)
    """
    import ipaddress
    from ...box_storage import get_box_ip, list_boxes

    target_box = None
    if box_opt:
        target_box = box_opt
    elif ctx.parent is not None and "box" in ctx.parent.params and ctx.parent.params["box"]:
        target_box = ctx.parent.params["box"]

    if target_box:
        # Check if this is a local box name first
        local_ip = get_box_ip(target_box)
        if local_ip:
            return local_ip

        # Check if it looks like an IP address
        try:
            ipaddress.ip_address(target_box)
            # It's a valid IP address, use it directly
            return target_box
        except ValueError:
            # Not a valid IP and not in local boxes
            # Show helpful error message
            click.secho(f"Error: Box '{target_box}' is not recorded in the system.", fg='red', err=True)
            click.echo("", err=True)

            saved_boxes = list_boxes()
            if saved_boxes:
                click.echo("Available boxes:", err=True)
                for name, ip in sorted(saved_boxes.items(), key=lambda x: _natural_sort_key(x[0])):
                    if isinstance(ip, dict):
                        ip = ip.get('ip', 'unknown')
                    click.echo(f"  - {name} ({ip})", err=True)
            else:
                click.echo("No boxes are currently saved.", err=True)

            click.echo("", err=True)
            click.echo("To add a new box, use:", err=True)
            click.echo(f"  lager boxes add --name {target_box} --ip <IP_ADDRESS>", err=True)
            ctx.exit(1)

    # get_default_box already handles local box resolution
    return get_default_box(ctx)


def _display_table(records):

    if not records:
        click.secho("No saved nets found.", fg="yellow")
        return

    # ----- group records by instrument|address ------------------------------
    by_instrument: dict[str, list[dict]] = {}
    for rec in records:
        instrument = rec.get("instrument", "") or ""
        address = rec.get("address", "") or ""
        key = f"{instrument}|{address}"
        by_instrument.setdefault(key, []).append(rec)

    # ----- check if any net has a jlink_script attached --------------------
    has_any_script = any(rec.get("jlink_script") for rec in records)

    # ----- gather all rows for column width computation --------------------
    headers = ["Name", "Net Type", "Channel"]
    if has_any_script:
        headers.append("Script")
    all_rows = []
    grouped_rows: list[tuple[str, list[list[str]]]] = []

    for key in sorted(by_instrument.keys(), key=_natural_sort_key):
        instrument, addr = key.split("|", 1)
        display_name = instrument.replace("_", " ")
        if addr and addr != "NA":
            addr_display = addr if len(addr) <= 50 else addr[:45] + "..."
            group_label = (display_name, f" [{addr_display}]")
        else:
            group_label = (display_name, "")

        nets = sorted(
            by_instrument[key],
            key=lambda r: (r.get("role", ""), _natural_sort_key(r.get("name", ""))),
        )
        rows = []
        for rec in nets:
            pin = rec.get("pin", "") or ""
            if rec.get("role") == "uart" and len(pin) > 10:
                pin = pin[:10]
            row = [
                rec.get("name", ""),
                rec.get("role", ""),
                pin,
            ]
            if has_any_script:
                row.append("yes" if rec.get("jlink_script") else "")
            rows.append(row)
        all_rows.extend(rows)
        grouped_rows.append((group_label, rows))

    # ----- compute column widths --------------------------------------------
    term_w = shutil.get_terminal_size((120, 24)).columns
    min_w = [8, 10, 7]
    if has_any_script:
        min_w.append(6)
    col_w = [
        max(min_w[i], len(headers[i]), max(len(str(r[i])) for r in all_rows))
        for i in range(len(headers))
    ]

    # ----- helper to format one row -----------------------------------------
    def fmt(row):
        return "  ".join(f"{row[i]:<{col_w[i]}}" for i in range(len(col_w)))

    # ----- output ------------------------------------------------------------
    total_width = sum(col_w) + 2 * (len(col_w) - 1)
    separator_width = min(total_width, term_w)

    indent = "    "
    click.echo(indent + fmt(headers))
    click.echo("=" * (separator_width + len(indent)))

    for i, (group_label, rows) in enumerate(grouped_rows):
        if i > 0:
            click.echo()
        label_bold, label_rest = group_label
        click.secho(label_bold, bold=True, nl=False)
        click.echo(label_rest)
        for j, row in enumerate(rows):
            is_last = j == len(rows) - 1
            prefix = "└── " if is_last else "├── "
            click.echo(prefix, nl=False)
            click.secho(f"{row[0]:<{col_w[0]}}", bold=True, nl=False)
            click.echo("  " + "  ".join(f"{row[i]:<{col_w[i]}}" for i in range(1, len(col_w))))

def _list_nets(ctx: click.Context, box: str) -> None:
    """
    Fetch nets via net.py and print the table.
    """
    raw = _run_net_py(ctx, box, "list")
    try:
        records: List[dict[str, Any]] = _parse_backend_json(raw)
    except json.JSONDecodeError:
        click.secho("Failed to parse response from backend.", fg="red", err=True)
        if not raw:
            click.secho("No output received from backend. Check box connectivity with 'lager hello'.", fg="yellow", err=True)
        else:
            click.secho(f"Raw output: {repr(raw)}", fg="yellow", err=True)
        ctx.exit(1)

    _display_table(records)

def _save_nets_batch(ctx: click.Context, box: str, nets_data: List[dict]) -> None:
    """
    Save multiple nets using batch save functionality, with fallback to individual saves.
    """
    if not nets_data:
        return

    # Try batch save first
    try:
        raw = _run_net_py(ctx, box, "save-batch", json.dumps(nets_data))

        if raw and raw.strip():
            response = _parse_backend_json(raw)
            # Check if response is a dict with expected format
            if isinstance(response, dict) and response.get("ok", False):
                count = response.get("count", len(nets_data))
                click.secho(f"Successfully saved {count} nets using batch save on box {box}.", fg="green")
                return
        else:
            pass
    except (json.JSONDecodeError, Exception) as e:
        click.secho(f"Batch save failed, falling back to individual saves: {e}", fg="yellow", err=True)

    # Fallback to individual saves
    click.secho(f"Using individual saves for {len(nets_data)} nets...", fg="yellow", err=True)
    saved_count = 0

    for net_data in nets_data:
        try:
            raw = _run_net_py(ctx, box, "save", json.dumps(net_data))
            saved_count += 1
        except Exception as e:
            click.secho(f"Failed to save net '{net_data.get('name', 'unknown')}': {e}", fg="red", err=True)

    click.secho(f"Successfully saved {saved_count} of {len(nets_data)} nets on box {box}.", fg="green")

# --------------------------------------------------------------------------- #
# Top-level group                                                             #
# --------------------------------------------------------------------------- #
@click.group(
    name="nets",
    invoke_without_command=True,
    help="List all saved nets.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
@force_command_option
def nets(ctx: click.Context, box: str | None) -> None:  # noqa: D401
    """
    If no sub-command is supplied, default to "list".
    """
    if ctx.invoked_subcommand is None:
        _list_nets(ctx, _resolve_box(ctx, box))


# --------------------------------------------------------------------------- #
# Sub-commands                                                                #
# --------------------------------------------------------------------------- #

@nets.command("delete", help="Delete one saved net by name and type.")
@click.argument("name")
@click.argument("net_type")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def delete_cmd(
    ctx: click.Context, name: str, net_type: str, box: str | None, yes: bool
) -> None:
    resolved_box = _resolve_box(ctx, box)
    raw = _run_net_py(ctx, resolved_box, "list")
    try:
        recs = _parse_backend_json(raw)
    except json.JSONDecodeError:
        click.secho("Failed to parse response from backend.", fg="red", err=True)
        if not raw:
            click.secho("No output received from backend. Check box connectivity with 'lager hello'.", fg="yellow", err=True)
        else:
            click.secho(f"Raw output: {repr(raw)}", fg="yellow", err=True)
        ctx.exit(1)

    match = [r for r in recs if r.get("name") == name and r.get("role") == net_type]
    if not match:
        click.secho(f"Net '{name}' ({net_type}) not found on {resolved_box}.", fg="yellow")
        ctx.exit(1)

    if not yes and not click.confirm(
        f"Delete net '{name}' ({net_type}) on box {resolved_box}?"
    ):
        click.secho("Aborted.", fg="yellow")
        return

    _run_net_py(ctx, resolved_box, "delete", name, net_type)
    click.secho(f"Deleted '{name}' ({net_type}) on box {resolved_box}.", fg="green")


@nets.command("delete-all", help="Dangerous – delete every saved net.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def delete_all_cmd(ctx: click.Context, box: str | None, yes: bool) -> None:
    resolved_box = _resolve_box(ctx, box)

    if not yes and not click.confirm(
        f"Delete ALL saved nets on box {resolved_box}? This cannot be undone."
    ):
        click.secho("Aborted.", fg="yellow")
        return

    _run_net_py(ctx, resolved_box, "delete-all")
    click.secho(f"Deleted all nets on box {resolved_box}.", fg="green")


@nets.command("tui", help="Launch the interactive Net-Manager TUI.")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def tui_cmd(ctx: click.Context, box: str | None) -> None:
    launch_tui(ctx, _resolve_box(ctx, box))


@nets.command("rename", help="Rename a saved net.")
@click.argument("name")
@click.argument("new_name")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def rename_cmd(
    ctx: click.Context,
    name: str,
    new_name: str,
    box: str | None,
) -> None:
    """
    Rename a net. Prevent duplicate net names (regardless of type).
    """
    resolved_box = _resolve_box(ctx, box)

    raw = _run_net_py(ctx, resolved_box, "list")
    try:
        recs = _parse_backend_json(raw)
    except json.JSONDecodeError:
        click.secho("Failed to parse response from backend.", fg="red", err=True)
        if not raw:
            click.secho("No output received from backend. Check box connectivity with 'lager hello'.", fg="yellow", err=True)
        else:
            click.secho(f"Raw output: {repr(raw)}", fg="yellow", err=True)
        ctx.exit(1)

    src = next((r for r in recs if r.get("name") == name), None)
    if not src:
        click.secho(f"Net '{name}' not found on {resolved_box}.", fg="yellow")
        ctx.exit(1)

    duplicate = next((r for r in recs if r.get("name") == new_name), None)
    if duplicate:
        click.secho(
            f"Cannot rename: a net named '{new_name}' already exists on box {resolved_box}.",
            fg="red",
        )
        ctx.exit(1)

    _run_net_py(ctx, resolved_box, "rename", name, new_name)
    click.secho(
        f"Renamed '{name}' → '{new_name}' on box {resolved_box}.", fg="green"
    )

@nets.command("add")
@click.argument("name")
@click.argument("role")
@click.argument("channel")
@click.argument("address")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--jlink-script", type=click.Path(exists=True),
              help="J-Link script file for debug nets (stored on box)")
@click.pass_context
def add_cmd(ctx, name, role, channel, address, box, jlink_script):
    """
    Add a net using inferred instrument from VISA address.
    """
    from ...box_storage import resolve_and_validate_box

    # Resolve and validate the box name
    resolved_box = resolve_and_validate_box(ctx, box)

    def _run_and_json(path: str, args: tuple[str, ...] = ()) -> list:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                run_python_internal(
                    ctx, path, resolved_box,
                    env={}, passenv=(), kill=False, download=(),
                    allow_overwrite=False, signum="SIGTERM", timeout=30,
                    detach=False, port=(), org=None, args=args,
                )
        except SystemExit as e:
            # Re-raise non-zero exits (actual errors)
            if e.code != 0:
                raw_output = buf.getvalue()
                if raw_output:
                    click.secho("Error from backend:", fg="red", err=True)
                    click.echo(raw_output, err=True)
                raise
        raw_output = buf.getvalue()
        try:
            return json.loads(raw_output or "[]")
        except json.JSONDecodeError:
            if raw_output:
                click.secho(f"Warning: Could not parse backend response: {repr(raw_output[:200])}", fg="yellow", err=True)
            return []

    def _get_instrument_from_address(address: str, allow_unknown: bool = False) -> str:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                run_python_internal(
                    ctx, get_impl_path("query_instruments.py"), resolved_box,
                    env={}, passenv=(), kill=False, download=(),
                    allow_overwrite=False, signum="SIGTERM", timeout=30,
                    detach=False, port=(), org=None,
                    args=("get_instrument", address),
                )
        except SystemExit as e:
            # Re-raise non-zero exits (actual errors)
            if e.code != 0:
                raw_output = buf.getvalue()
                if raw_output:
                    click.secho("Error querying instruments:", fg="red", err=True)
                    click.echo(raw_output, err=True)
                raise

        raw_output = buf.getvalue()
        try:
            result = json.loads(raw_output)
        except json.JSONDecodeError:
            if not allow_unknown:
                click.secho("Error: Invalid instrument info returned for address", fg="red", err=True)
                if raw_output:
                    click.secho(f"Raw output: {repr(raw_output[:200])}", fg="yellow", err=True)
                ctx.exit(1)
            return "Unknown_UART_Device"

        if isinstance(result, list):
            for inst in result:
                if inst.get("address") == address:
                    return inst.get("name", "Unknown")
            click.secho(f"Error: No instrument found for address {address}", fg="red", err=True)
            if not allow_unknown:
                ctx.exit(1)
            return "Unknown_UART_Device"
        elif isinstance(result, dict):
            if "name" in result:
                return result["name"]
            # Empty dict means instrument not found at address
            if not result:
                click.secho(f"Error: No instrument found at address {address}", fg="red", err=True)
                if not allow_unknown:
                    ctx.exit(1)
                return "Unknown_Device"

        if not allow_unknown:
            click.secho("Error: Unexpected result format from query_instruments.py", fg="red", err=True)
            ctx.exit(1)
        return "Unknown_UART_Device"


    # ─────────── resolve instrument ─────────────
    # For UART nets, allow a direct device path (e.g., /dev/ttyUSB0) when a USB serial is unavailable.
    is_uart_device_path = (
        role == "uart" and isinstance(channel, str) and channel.startswith("/dev/")
    )

    instrument = _get_instrument_from_address(address, allow_unknown=is_uart_device_path)

    # ─────────── load devices and nets ──────────
    devs       = _run_and_json(get_impl_path("query_instruments.py"))
    saved_nets = _run_and_json(get_impl_path("net.py"), ("list",))

    # ─────────── multiple hubs restriction ──────
    if not is_uart_device_path:
        if instrument in _MULTI_HUBS:
            hub_count = sum(1 for d in devs if d.get("name") == instrument)
            if hub_count > 1:
                click.secho(
                    f"Multiple {instrument} devices detected – unplug extras before adding nets.",
                    fg="red",
                )
                ctx.exit(1)

        # ─────────── tuple must exist ───────────────
        dev_match = next((d for d in devs if d.get("address") == address), None)
        if not dev_match:
            click.secho(
                f"No instrument with address {address} is present on {resolved_box}.",
                fg="red",
            )
            ctx.exit(1)

        chan_map = dev_match.get("channels") or {}
        role_chans = chan_map.get(role)
    else:
        chan_map = {}
        role_chans = None

    if role == "debug":
        for net in saved_nets:
            if (
                net["role"] == "debug"
                and net["instrument"] == instrument
                and net["address"] == address
            ):
                click.secho(
                    f"A debug net already exists for instrument {instrument} at {address}.",
                    fg="red",
                )
                ctx.exit(1)
    else:
        # UART device-path mode skips channel validation because the tty path is supplied directly.
        if is_uart_device_path:
            role_chans = None
        # Normal validation for channel availability on the device
        if role_chans == "NA":
            click.secho(
                f"The role '{role}' is not available for the instrument at {address}.",
                fg="red",
            )
            ctx.exit(1)

        if role_chans:
            if isinstance(role_chans, str):
                role_chans = [s.strip() for s in role_chans.split(",")]
            elif not isinstance(role_chans, list):
                role_chans = [role_chans]

            if str(channel) not in [str(ch) for ch in role_chans]:
                click.secho(
                    f"The channel '{channel}' is not valid for role '{role}' on the instrument at {address}.",
                    fg="red",
                )
                ctx.exit(1)

    # ─────────── unique net name (regardless of type) ────────────────
    if any(n["name"] == name for n in saved_nets):
        click.secho(
            f"A net named '{name}' already exists. Net names must be globally unique.",
            fg="red",
        )
        ctx.exit(1)

    # ─────────── unique role/instrument/channel/address ──────────────
    if any(
        n["role"] == role
        and n["instrument"] == instrument
        and str(n["pin"]) == str(channel)
        and n["address"] == address
        for n in saved_nets
    ):
        click.secho(
            "A net with the same role / instrument / channel / address already exists.",
            fg="red",
        )
        ctx.exit(1)

    # ─────────── single-channel restriction ──────────────────────────
    if instrument in _SINGLE_CHANNEL_INST:
        if any(n["instrument"] == instrument and n["address"] == address for n in saved_nets):
            click.secho(
                f"Only one net may reference {instrument} at {address}.",
                fg="red",
            )
            ctx.exit(1)

    if role not in INSTRUMENT_NET_MAP.get(instrument, []) and not is_uart_device_path:
        supported_types = INSTRUMENT_NET_MAP.get(instrument, [])
        click.secho(
            f"Error: Instrument '{instrument}' does not support net type '{role}'",
            fg="red",
            err=True,
        )
        if supported_types:
            click.secho(f"Supported net types for {instrument}: {', '.join(supported_types)}", err=True)
        else:
            click.secho(f"No net types are defined for instrument '{instrument}'", fg="yellow", err=True)
        ctx.exit(1)

    # ─────────── persist new net ─────────────────────────────────────
    net_data = {
        "name":       name,
        "role":       role,
        "address":    address,
        "instrument": instrument,
        "pin":        channel,
    }
    if is_uart_device_path:
        net_data["device_path"] = channel

    # Handle J-Link script for debug nets
    if role == "debug" and jlink_script:
        import base64
        try:
            with open(jlink_script, 'rb') as f:
                jlink_script_content = base64.b64encode(f.read()).decode('ascii')
            net_data["jlink_script"] = jlink_script_content
        except Exception as e:
            click.secho(f"Error reading J-Link script file: {e}", fg='red', err=True)
            ctx.exit(1)
    elif jlink_script and role != "debug":
        click.secho("Warning: --jlink-script is only applicable for debug nets, ignoring.", fg='yellow', err=True)

    try:
        _buf = io.StringIO()
        with redirect_stdout(_buf):
            run_python_internal(
                ctx,
                get_impl_path("net.py"),
                resolved_box,
                env={}, passenv=(), kill=False, download=(),
                allow_overwrite=False, signum="SIGTERM", timeout=30,
                detach=False, port=(), org=None,
                args=(
                    "save",
                    json.dumps(net_data),
                ),
            )
    except SystemExit as e:
        # Re-raise non-zero exits (actual errors)
        if e.code != 0:
            raw_output = _buf.getvalue()
            if raw_output:
                click.secho("Error saving net:", fg="red", err=True)
                click.echo(raw_output, err=True)
            raise

    click.secho(f"Saved new net '{name}' on {resolved_box}.", fg="green")


@nets.command("add-all", help="Add all possible nets that can be created on the box.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def create_all_cmd(ctx: click.Context, box: str | None, yes: bool) -> None:
    """
    Create all possible nets that can be created on a box.
    This command replicates the functionality of the 'Add Nets' page in the TUI.
    """
    resolved_box = _resolve_box(ctx, box)

    def _run_and_json(script: str, *args: str) -> list:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                run_python_internal(
                    ctx, get_impl_path(script), resolved_box,
                    env={}, passenv=(), kill=False, download=(),
                    allow_overwrite=False, signum="SIGTERM", timeout=30,
                    detach=False, port=(), org=None, args=args,
                )
        except SystemExit as e:
            # Re-raise non-zero exits (actual errors)
            if e.code != 0:
                raw_output = buf.getvalue()
                if raw_output:
                    click.secho("Error from backend:", fg="red", err=True)
                    click.echo(raw_output, err=True)
                raise
        raw_output = buf.getvalue()
        try:
            return _parse_backend_json(raw_output or "[]")
        except json.JSONDecodeError:
            if raw_output:
                click.secho(f"Warning: Could not parse backend response: {repr(raw_output[:200])}", fg="yellow", err=True)
            return []

    def _first_word(role: str) -> str:
        """Return the first part of a hyphenated role name."""
        # Special case: power-supply nets use 'supply' prefix instead of 'power'
        if role == "power-supply":
            return "supply"
        return role.split("-")[0]

    # Get available instruments and existing nets
    inst_list = _run_and_json("query_instruments.py")
    saved_nets = _run_and_json("net.py", "list")

    if not inst_list:
        click.secho("No instruments found on the box.", fg="yellow")
        return

    # Generate all possible nets from instruments (without names yet)
    all_possible_nets: list[dict] = []

    for dev in inst_list:
        instr = dev.get("name", "Unknown")
        addr = dev.get("address", "NA")
        channel_map = dev.get("channels", {})

        for role, channels in (channel_map or {}).items():
            for ch in channels:
                # Special handling for UART devices:
                # For UART, the 'channels' list contains USB serial numbers
                # We store: instrument=device_name, chan=port, pin=usb_serial
                if role == "uart":
                    net_data = {
                        "instrument": instr,   # Device name (e.g., "Prolific_USB_Serial")
                        "chan": "0",          # Default port number
                        "pin": ch,            # USB serial number (e.g., "ABCD12345")
                        "type": role,
                        "net": None,  # Will assign name after filtering
                        "addr": addr,
                        "saved": False,
                    }
                else:
                    net_data = {
                        "instrument": instr,
                        "chan": ch,
                        "type": role,
                        "net": None,  # Will assign name after filtering
                        "addr": addr,
                        "saved": False,
                    }
                all_possible_nets.append(net_data)

    # Apply filtering logic similar to TUI's _get_addable_nets
    warnings = []

    # Check for multiple hubs of same type
    chan_seen: dict[str, set[str]] = defaultdict(set)
    duplicate_hubs: set[str] = set()
    for net in all_possible_nets:
        if net["instrument"] in _MULTI_HUBS:
            if net["chan"] in chan_seen[net["instrument"]]:
                duplicate_hubs.add(net["instrument"])
            chan_seen[net["instrument"]].add(net["chan"])

    # Filter out blocked instrument families
    filtered_nets = []
    dup_single: set[tuple[str, str]] = set()

    for net in all_possible_nets:
        # Skip if instrument family is blocked due to duplicates
        if net["instrument"] in duplicate_hubs:
            continue

        # Skip if single-channel instrument already has a net at this address
        if net["instrument"] in _SINGLE_CHANNEL_INST:
            if any(s.get("instrument") == net["instrument"] and s.get("address") == net["addr"] for s in saved_nets):
                dup_single.add((net["instrument"], net["addr"]))
                continue

        # Skip if duplicate debug net for same instrument/address (check BEFORE prompting)
        if net["type"] == "debug":
            if any(
                s.get("role") == "debug" and
                s.get("instrument") == net["instrument"] and
                s.get("address") == net["addr"]
                for s in saved_nets
            ):
                continue

        # Skip if exact duplicate of saved net exists
        # For UART nets, check against USB serial number (pin field)
        if net["type"] == "uart":
            if any(
                s.get("role") == "uart" and
                s.get("pin") == net["pin"]  # Match USB serial number
                for s in saved_nets
            ):
                continue
        else:
            if any(
                s.get("role") == net["type"] and
                s.get("instrument") == net["instrument"] and
                str(s.get("pin")) == str(net["chan"]) and
                s.get("address") == net["addr"]
                for s in saved_nets
            ):
                continue

        # Handle debug nets - prompt for device type if channel is DEVICE_TYPE
        # (only after we've confirmed this net will actually be created)
        if net["type"] == "debug" and net["chan"] == "DEVICE_TYPE":
            device_type = click.prompt(f"Enter device type for debug net on {net['instrument']} at {net['addr']}", type=str)
            net["chan"] = device_type

        filtered_nets.append(net)

    # Assign names to filtered nets (only now that we know which will be created)
    idx_re = re.compile(r"^([A-Za-z]+)(\d+)$")
    used_indices: dict[str, set[int]] = defaultdict(set)

    # Collect used indices from existing nets
    for saved_net in saved_nets:
        m = idx_re.match(saved_net.get("name", ""))
        if m and _first_word(saved_net.get("role", "")) == m.group(1):
            used_indices[saved_net.get("role", "")].add(int(m.group(2)))

    # Assign names to new nets
    for net in filtered_nets:
        role = net["type"]
        # Find lowest unused index for this role
        idx = 1
        while idx in used_indices[role]:
            idx += 1
        used_indices[role].add(idx)
        net["net"] = f"{_first_word(role)}{idx}"

    # Generate warnings
    for inst in sorted(duplicate_hubs, key=_natural_sort_key):
        warnings.append(f"Multiple {inst} devices detected – unplug extras before adding nets.")
    for inst, addr in sorted(dup_single, key=lambda x: _natural_sort_key(x[0])):
        warnings.append(f"{inst} at {addr} already has a net.")

    # Display warnings
    for warning in warnings:
        click.secho(f"Warning: {warning}", fg="yellow")

    if not filtered_nets:
        click.secho("No new nets can be created. All possible nets already exist or are blocked.", fg="yellow")
        return

    # Show what would be created
    click.secho(f"\nFound {len(filtered_nets)} nets that can be created:", fg="green")
    for net in filtered_nets:
        # For UART nets, show the device path instead of port number
        if net['type'] == 'uart':
            # Find the device path from inst_list
            device_path = None
            for dev in inst_list:
                uart_channels = dev.get("channels", {}).get("uart", [])
                if net.get('pin') in uart_channels:
                    device_path = dev.get("tty_path")
                    break
            path_display = f" ({device_path})" if device_path else ""
            click.echo(f"  - {net['net']} ({net['type']}) on {net['instrument']}{path_display}")
        else:
            click.echo(f"  - {net['net']} ({net['type']}) on {net['instrument']} channel {net['chan']}")

    # Confirm before proceeding
    if not yes:
        if not click.confirm(f"\nCreate all {len(filtered_nets)} nets on box {resolved_box}?"):
            click.secho("Aborted.", fg="yellow")
            return

    # Prepare nets for batch save
    nets_to_save = []
    for net in filtered_nets:
        net_record = {
            "name": net["net"],
            "role": net["type"],
            "address": net["addr"],
            "instrument": net["instrument"],
            "pin": net.get("pin", net["chan"]),  # Use 'pin' if present (UART), else 'chan'
        }
        # For UART nets, also include the channel (port number)
        if net["type"] == "uart" and "chan" in net:
            net_record["channel"] = net["chan"]
        nets_to_save.append(net_record)

    # Use batch save for better performance
    _save_nets_batch(ctx, resolved_box, nets_to_save)


@nets.command("add-batch", help="Add multiple nets from a JSON file.")
@click.argument("json_file", type=click.File("r"))
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def create_batch_cmd(ctx: click.Context, json_file, box: str | None) -> None:
    """
    Create multiple nets from a JSON file containing an array of net definitions.

    JSON format:
    [
        {
            "name": "net1",
            "role": "gpio",
            "channel": "1",
            "address": "192.168.1.100"
        },
        {
            "name": "net2",
            "role": "adc",
            "channel": "2",
            "address": "192.168.1.100"
        }
    ]
    """
    resolved_box = _resolve_box(ctx, box)

    try:
        nets_data = json.load(json_file)
    except json.JSONDecodeError as e:
        click.secho(f"Invalid JSON in file: {e}", fg="red", err=True)
        ctx.exit(1)

    if not isinstance(nets_data, list):
        click.secho("JSON file must contain an array of net definitions", fg="red", err=True)
        ctx.exit(1)

    if not nets_data:
        click.secho("No nets found in JSON file", fg="yellow", err=True)
        return

    # Helper function to get instrument from address (reuse from create_cmd)
    def _get_instrument_from_address(address: str, fallback_instrument: str = "Unknown") -> str:
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                run_python_internal(
                    ctx, get_impl_path("query_instruments.py"), resolved_box,
                    env={}, passenv=(), kill=False, download=(),
                    allow_overwrite=False, signum="SIGTERM", timeout=0,
                    detach=False, port=(), org=None,
                    args=("get_instrument", address),
                )
        except SystemExit:
            pass

        try:
            result = json.loads(buf.getvalue())
        except json.JSONDecodeError:
            return fallback_instrument

        if isinstance(result, list):
            for inst in result:
                if inst.get("address") == address:
                    return inst.get("name", "Unknown")
            return fallback_instrument
        elif isinstance(result, dict) and "name" in result:
            return result["name"]

        return fallback_instrument

    # Validate and normalize each net in the batch
    normalized_nets = []

    for i, net_data in enumerate(nets_data):
        if not isinstance(net_data, dict):
            click.secho(f"Net {i+1}: must be an object", fg="red", err=True)
            ctx.exit(1)

        required_fields = ["name", "role", "channel", "address"]
        for field in required_fields:
            if field not in net_data:
                click.secho(f"Net {i+1}: missing required field '{field}'", fg="red", err=True)
                ctx.exit(1)

        # Look up instrument if not provided
        instrument = net_data.get("instrument")
        if not instrument:
            instrument = _get_instrument_from_address(net_data["address"], "Unknown")

        normalized_net = {
            "name": net_data["name"],
            "role": net_data["role"],
            "address": net_data["address"],
            "pin": net_data["channel"],
            "instrument": instrument
        }
        normalized_nets.append(normalized_net)

    # Use batch save for better performance
    _save_nets_batch(ctx, resolved_box, normalized_nets)


@nets.command("set-script", help="Set or update a J-Link script on an existing debug net.")
@click.argument("name")
@click.argument("script_path", type=click.Path(exists=True))
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def set_script_cmd(
    ctx: click.Context, name: str, script_path: str, box: str | None
) -> None:
    """
    Attach a JLinkScript file to an existing debug net.

    The script is stored on the box and used automatically during
    connect, flash, erase, and reset operations.
    """
    import base64

    resolved_box = _resolve_box(ctx, box)
    raw = _run_net_py(ctx, resolved_box, "list")
    try:
        recs = _parse_backend_json(raw)
    except json.JSONDecodeError:
        click.secho("Failed to parse response from backend.", fg="red", err=True)
        ctx.exit(1)

    target = next((r for r in recs if r.get("name") == name), None)
    if not target:
        click.secho(f"Net '{name}' not found on {resolved_box}.", fg="yellow")
        ctx.exit(1)

    if target.get("role") != "debug":
        click.secho(
            f"Net '{name}' is a '{target.get('role')}' net. --jlink-script is only applicable for debug nets.",
            fg="red",
        )
        ctx.exit(1)

    try:
        with open(script_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("ascii")
    except Exception as e:
        click.secho(f"Error reading script file: {e}", fg="red", err=True)
        ctx.exit(1)

    target["jlink_script"] = encoded
    _run_net_py(ctx, resolved_box, "save", json.dumps(target))
    click.secho(
        f"J-Link script set on debug net '{name}' on box {resolved_box}.", fg="green"
    )


@nets.command("remove-script", help="Remove a J-Link script from an existing debug net.")
@click.argument("name")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def remove_script_cmd(
    ctx: click.Context, name: str, box: str | None
) -> None:
    """
    Remove a JLinkScript file from an existing debug net.
    """
    resolved_box = _resolve_box(ctx, box)
    raw = _run_net_py(ctx, resolved_box, "list")
    try:
        recs = _parse_backend_json(raw)
    except json.JSONDecodeError:
        click.secho("Failed to parse response from backend.", fg="red", err=True)
        ctx.exit(1)

    target = next((r for r in recs if r.get("name") == name), None)
    if not target:
        click.secho(f"Net '{name}' not found on {resolved_box}.", fg="yellow")
        ctx.exit(1)

    if target.get("role") != "debug":
        click.secho(
            f"Net '{name}' is a '{target.get('role')}' net, not a debug net.",
            fg="red",
        )
        ctx.exit(1)

    if "jlink_script" not in target:
        click.secho(f"Net '{name}' does not have a J-Link script attached.", fg="yellow")
        return

    del target["jlink_script"]
    _run_net_py(ctx, resolved_box, "save", json.dumps(target))
    click.secho(
        f"J-Link script removed from debug net '{name}' on box {resolved_box}.", fg="green"
    )


@nets.command("show-script", help="Display the J-Link script attached to a debug net.")
@click.argument("name")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def show_script_cmd(
    ctx: click.Context, name: str, box: str | None
) -> None:
    """
    Print the contents of a JLinkScript attached to a debug net.

    Output goes to stdout so it can be piped or redirected:
        lager nets show-script my-debug --box mybox > script.JLinkScript
    """
    import base64

    resolved_box = _resolve_box(ctx, box)
    raw = _run_net_py(ctx, resolved_box, "list")
    try:
        recs = _parse_backend_json(raw)
    except json.JSONDecodeError:
        click.secho("Failed to parse response from backend.", fg="red", err=True)
        ctx.exit(1)

    target = next((r for r in recs if r.get("name") == name), None)
    if not target:
        click.secho(f"Net '{name}' not found on {resolved_box}.", fg="yellow", err=True)
        ctx.exit(1)

    if target.get("role") != "debug":
        click.secho(
            f"Net '{name}' is a '{target.get('role')}' net, not a debug net.",
            fg="red", err=True,
        )
        ctx.exit(1)

    if not target.get("jlink_script"):
        click.secho(
            f"Net '{name}' does not have a J-Link script attached.",
            fg="yellow", err=True,
        )
        ctx.exit(1)

    decoded = base64.b64decode(target["jlink_script"]).decode("utf-8")
    click.echo(decoded)

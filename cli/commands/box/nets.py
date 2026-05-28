# Copyright 2024-2026 Lager Data
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

def _debug_channel_suffix(value) -> str:
    """Return the ``@<channel>`` portion of a debug net's device field.

    Multi-channel FTDIs encode the interface index in the device field as
    ``STM32F4x@A``; this helper extracts the trailing ``@A``/``@0``/``@B``
    (lower-cased, leading ``@`` retained) so two saved nets on the same
    physical probe can be compared by channel. Returns ``""`` when no
    suffix is present — that's the implicit channel A.
    """
    if not value:
        return ""
    s = str(value)
    if "@" not in s:
        return ""
    return f"@{s.rpartition('@')[2].lower()}"


_VISA_SERIAL_RE = re.compile(
    r'USB\d*::0x[0-9A-Fa-f]+::0x[0-9A-Fa-f]+::([^:]+)::INSTR',
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Debug-script backend detection (mirror of box/lager/debug/probes.py).       #
#                                                                             #
# Kept duplicated client-side on purpose: detection has to work against any   #
# box version, including ones older than the smart `set-script` change. The   #
# only state we need is the net record (which `_run_net_py list` always       #
# returns) plus the file the user just handed us.                             #
# --------------------------------------------------------------------------- #

_DEBUG_VISA_RE = re.compile(
    # Serial slot may be empty for FTDI chips with an un-programmed
    # EEPROM (the box scanner emits ``USB0::0x0403::0x6011::::INSTR``
    # in that case). Match the relaxed shape so backend detection
    # still works for serial-less probes; keep this in sync with
    # ``_VISA_RE`` in ``box/lager/debug/probes.py``.
    r'USB\d*::0x([0-9A-Fa-f]+)::0x[0-9A-Fa-f]+::[^:]*::INSTR',
    re.IGNORECASE,
)


# ``pin`` is overloaded by role; the label needs to track that so users
# don't read "Channel: 2" and assume the value is an FT4232H interface
# index when it's actually being interpreted as a USB serial by the
# UART dispatcher.
_PIN_LABEL_BY_ROLE = {
    "uart": "Pin/serial:",
    "debug": "Device:",
}


def _pin_label_for_role(role: str | None) -> str:
    return _PIN_LABEL_BY_ROLE.get(role or "", "Channel:")
# Keep these in sync with `_JLINK_VIDS` / `_OPENOCD_VIDS` in
# `box/lager/debug/probes.py`.
_DEBUG_JLINK_VIDS = {'1366'}
_DEBUG_OPENOCD_VIDS = {'0483', '2e8a', '0403', '0d28', '03eb', '15ba'}

_JLINK_EXTS = {'.jlinkscript', '.jlinkscriptfile'}
_OPENOCD_EXTS = {'.cfg', '.tcl', '.ocd'}

_BACKEND_JLINK = 'jlink'
_BACKEND_OPENOCD = 'openocd'

_FIELD_FOR_BACKEND = {
    _BACKEND_JLINK: 'jlink_script',
    _BACKEND_OPENOCD: 'openocd_config',
}
_BACKEND_LABEL = {
    _BACKEND_JLINK: 'J-Link script',
    _BACKEND_OPENOCD: 'OpenOCD config',
}


def _probe_backend_for_net(net: Any) -> Optional[str]:
    """Return ``'jlink'``/``'openocd'`` for *net*, or ``None`` if unknown.

    Mirrors ``box/lager/debug/probes.py::resolve_backend`` but never defaults
    to J-Link — callers want to know when the probe signal is absent so they
    can fall back to file sniffing without being misled by the default.
    """
    if not isinstance(net, dict):
        return None
    explicit = (net.get('debug_backend') or '').strip().lower()
    if explicit in (_BACKEND_JLINK, _BACKEND_OPENOCD):
        return explicit
    address = net.get('address') or ''
    m = _DEBUG_VISA_RE.match(str(address).strip())
    if not m:
        return None
    vid = m.group(1).lower().lstrip('0').zfill(4) or '0000'
    if vid in _DEBUG_OPENOCD_VIDS:
        return _BACKEND_OPENOCD
    if vid in _DEBUG_JLINK_VIDS:
        return _BACKEND_JLINK
    return None


def _sniff_script_backend(filename: str, content: bytes) -> Optional[str]:
    """Return ``'jlink'``/``'openocd'``/``None`` from filename + content.

    Extension is the dominant signal. Content sniff is only consulted when
    the extension is unrecognised (e.g. stdin or extensionless paths) and
    abstains when both/neither family of markers is present so we don't
    guess silently.
    """
    import os
    _, ext = os.path.splitext((filename or '').lower())
    if ext in _JLINK_EXTS:
        return _BACKEND_JLINK
    if ext in _OPENOCD_EXTS:
        return _BACKEND_OPENOCD
    try:
        head = content[:4096].decode('utf-8', errors='replace').lower()
    except Exception:
        return None
    openocd_markers = (
        'adapter driver', 'transport select', 'ftdi vid_pid',
        'source [find', 'target create', 'swj_newdap', 'dap create',
        'jtag newtap', 'flash bank',
    )
    jlink_markers = (
        'reset()', 'inittarget()', 'mem_writeu32', 'jlink_executecommand',
        'beforetargetreset', 'aftertargetreset', 'aftertargetdownload',
    )
    has_openocd = any(m in head for m in openocd_markers)
    has_jlink = any(m in head for m in jlink_markers)
    if has_openocd and not has_jlink:
        return _BACKEND_OPENOCD
    if has_jlink and not has_openocd:
        return _BACKEND_JLINK
    return None


def _choose_script_backend(
    *,
    explicit: Optional[str],
    probe: Optional[str],
    file: Optional[str],
) -> tuple[Optional[str], str, bool]:
    """Reconcile the three backend signals.

    Returns ``(backend, reason, mismatch)``:

    * ``backend`` is the chosen backend or ``None`` when undetermined.
    * ``reason`` is a one-line human explanation for status messages.
    * ``mismatch`` is True when probe and file both have a value and
      disagree; callers must surface a clear error so the user can pick
      a side with ``--backend``.
    """
    if explicit:
        return explicit, f"--backend {explicit} (explicit)", False
    signals = [(k, v) for k, v in (('probe', probe), ('file', file)) if v]
    if not signals:
        return None, (
            "no backend signal — pass --backend, or use a recognised "
            "file extension (.JLinkScript / .cfg / .tcl)"
        ), False
    values = {v for _k, v in signals}
    if len(values) == 1:
        chosen = next(iter(values))
        sources = '+'.join(k for k, _v in signals)
        return chosen, f"{chosen} (matched {sources})", False
    return None, (
        f"probe says '{probe}', file says '{file}' "
        "— pass --backend to override"
    ), True


def _read_debug_script_input(script_path: str) -> tuple[str, bytes]:
    """Return ``(display_name, raw_bytes)`` for *script_path*.

    ``script_path == '-'`` reads from stdin as binary; otherwise reads the
    given path. Raises ``click.ClickException`` on read failure so the
    caller can render a consistent error.
    """
    import sys
    if script_path == '-':
        try:
            return ('<stdin>', sys.stdin.buffer.read())
        except Exception as e:
            raise click.ClickException(f"Failed to read script from stdin: {e}")
    try:
        with open(script_path, 'rb') as f:
            return (script_path, f.read())
    except FileNotFoundError:
        raise click.ClickException(f"Script file not found: {script_path}")
    except Exception as e:
        raise click.ClickException(f"Failed to read script file: {e}")


def _clear_other_script_field(
    target: dict, chosen_field: str,
) -> tuple[Optional[str], int]:
    """If the net has the *other* debug-script field set, delete it.

    Returns ``(cleared_field, decoded_byte_count)`` or ``(None, 0)`` when
    nothing was cleared. ``decoded_byte_count`` is the size of the
    base64-decoded content, or ``-1`` if it couldn't be decoded.
    """
    import base64
    other = 'openocd_config' if chosen_field == 'jlink_script' else 'jlink_script'
    if not target.get(other):
        return (None, 0)
    try:
        n = len(base64.b64decode(target[other]))
    except Exception:
        n = -1
    del target[other]
    return (other, n)


def _serial_from_visa_address(address) -> str:
    """Extract the USB serial segment from a VISA-style address.

    Returns ``""`` for non-VISA strings. Used by the UART dedup pass to
    recognise legacy saved nets where the ``pin`` field stores the USB
    serial (pre-multi-tty enumeration) rather than a ``/dev/tty*`` path.
    """
    if not address:
        return ""
    m = _VISA_SERIAL_RE.match(str(address).strip())
    return m.group(1).strip() if m else ""


_MULTI_HUBS = {"LabJack_T7", "Acroname_8Port", "Acroname_4Port"}
_SINGLE_CHANNEL_INST = {
    "Keithley_2281S": ("batt", "supply"),
    "EA_PSB_10060_60": ("solar", "supply"),
    "EA_PSB_10080_60": ("solar", "supply"),
}
# Chips that can run in exactly one mode at a time, across ALL roles. The
# canonical case is the FT232H: one physical channel, hardware-multiplexed
# between MPSSE (spi/i2c/gpio/debug) and async-serial (uart). Once the user
# claims any role on one of these chips, the other roles must disappear
# from the "add nets" menu. Multi-channel FTDIs (FT2232H, FT4232H) are NOT
# in this set — they get one role per channel via the @A/@B/... suffix.
_MODE_EXCLUSIVE_INST = {"FTDI_FT232H"}
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
    "FTDI_FT232H": ["spi", "i2c", "gpio", "debug", "uart"],
    # FT2232H / FT4232H carry the new multi-channel debug role plus UART.
    # The OpenOCD backend reads the FTDI interface index off the net's
    # device field (``STM32F4x@A``); single-channel FTDIs default to A.
    "FTDI_FT2232H": ["spi", "i2c", "gpio", "debug", "uart"],
    "FTDI_FT4232H": ["debug", "uart"],

    # debug — J-Link family
    "J-Link": ["debug"],
    "J-Link_Plus": ["debug"],
    "Flasher_ARM": ["debug"],
    "J-Link_Flasher_Pro": ["debug"],
    # debug — OpenOCD-backed probes
    "STLink_v2": ["debug"],
    "STLink_v2_1": ["debug"],
    "STLink_v3_Mini": ["debug"],
    "STLink_v3": ["debug"],
    "STLink_v3_2VCP": ["debug"],
    "RP2040_Picoprobe": ["debug"],
    "Atmel_EDBG": ["debug"],
    "DAPLink": ["debug"],

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
            from ...box_storage import acquire_command_lock_with_cleanup
            acquire_command_lock_with_cleanup(ctx, local_ip, target_box, ctx.info_name or 'nets')
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
            click.echo(f"  lager boxes add --name {target_box} --ip [IP_ADDRESS]", err=True)
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

    # ----- check which optional columns are needed -------------------------
    has_any_script = any(rec.get("jlink_script") for rec in records)
    has_any_openocd = any(rec.get("openocd_config") for rec in records)

    # ----- gather all rows for column width computation --------------------
    headers = ["Name", "Net Type", "Channel"]
    if has_any_script:
        headers.append("Script")
    if has_any_openocd:
        headers.append("OpenOCD")
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
            if has_any_openocd:
                row.append("yes" if rec.get("openocd_config") else "")
            rows.append(row)
        all_rows.extend(rows)
        grouped_rows.append((group_label, rows))

    # ----- compute column widths --------------------------------------------
    term_w = shutil.get_terminal_size((120, 24)).columns
    min_w = [8, 10, 7]
    if has_any_script:
        min_w.append(6)
    if has_any_openocd:
        min_w.append(7)
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
    help="List and manage saved nets",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def nets(ctx: click.Context, box: str | None) -> None:  # noqa: D401
    """
    If no sub-command is supplied, default to "list".
    """
    if ctx.invoked_subcommand is None:
        _list_nets(ctx, _resolve_box(ctx, box))


# --------------------------------------------------------------------------- #
# Sub-commands                                                                #
# --------------------------------------------------------------------------- #

@nets.command("delete", help="Delete one saved net by name and type")
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


@nets.command("delete-all", help="Dangerous – delete every saved net")
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


@nets.command("tui", help="Launch the interactive Net-Manager TUI")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def tui_cmd(ctx: click.Context, box: str | None) -> None:
    launch_tui(ctx, _resolve_box(ctx, box))


@nets.command("rename", help="Rename a saved net")
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
@click.option("--openocd-config", type=click.Path(exists=True),
              help="OpenOCD .cfg/.tcl file for debug nets (stored on box). "
                   "Replaces the auto-detected interface cfg.")
@click.pass_context
def add_cmd(ctx, name, role, channel, address, box, jlink_script, openocd_config):
    """
    Add a net using inferred instrument from VISA address
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

    # Handle OpenOCD config for debug nets
    if role == "debug" and openocd_config:
        import base64
        try:
            with open(openocd_config, 'rb') as f:
                openocd_config_content = base64.b64encode(f.read()).decode('ascii')
            net_data["openocd_config"] = openocd_config_content
        except Exception as e:
            click.secho(f"Error reading OpenOCD config file: {e}", fg='red', err=True)
            ctx.exit(1)
    elif openocd_config and role != "debug":
        click.secho("Warning: --openocd-config is only applicable for debug nets, ignoring.", fg='yellow', err=True)

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


@nets.command("add-all", help="Add all possible nets that can be created on the box")
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

    # Mode-exclusive chips (FT232H): if a chip+addr has no saved net yet AND
    # the scanner produced candidates for more than one role, ``add-all``
    # can't pick for the user — refuse the whole chip and tell them to use
    # ``lager nets add`` or the TUI to choose a single role explicitly.
    mode_excl_roles: dict[tuple[str, str], set[str]] = defaultdict(set)
    for net in all_possible_nets:
        if net["instrument"] in _MODE_EXCLUSIVE_INST:
            mode_excl_roles[(net["instrument"], net["addr"])].add(net["type"])
    ambiguous_mode_excl: set[tuple[str, str]] = set()
    for key, roles in mode_excl_roles.items():
        instrument, addr = key
        if len(roles) > 1 and not any(
            s.get("instrument") == instrument and s.get("address") == addr
            for s in saved_nets
        ):
            ambiguous_mode_excl.add(key)
            roles_str = ", ".join(sorted(roles))
            warnings.append(
                f"{instrument} at {addr} supports multiple modes ({roles_str}); "
                f"run `lager nets add` (or the TUI) to pick one — "
                f"this chip only runs one mode at a time."
            )

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

        # Mode-exclusive chips (FT232H): once ANY role is saved on this
        # chip+address, every other role becomes unavailable because the
        # underlying hardware can only run one mode at a time. Also skip
        # chips flagged ambiguous in the pre-pass above (multiple candidate
        # roles, no saved net yet — user must pick interactively).
        if net["instrument"] in _MODE_EXCLUSIVE_INST:
            key = (net["instrument"], net["addr"])
            if key in ambiguous_mode_excl:
                continue
            if any(
                s.get("instrument") == net["instrument"]
                and s.get("address") == net["addr"]
                for s in saved_nets
            ):
                dup_single.add(key)
                continue

        # Skip if duplicate debug net for same instrument/address.
        # Multi-channel FTDI: one debug net per (address, probe_channel),
        # since the user may want channel A for JTAG and channel B for a
        # second debug session. We compare the @suffix portion if present.
        if net["type"] == "debug":
            chan_suffix = _debug_channel_suffix(net["chan"])
            if any(
                s.get("role") == "debug" and
                s.get("instrument") == net["instrument"] and
                s.get("address") == net["addr"] and
                _debug_channel_suffix(s.get("pin") or s.get("channel")) == chan_suffix
                for s in saved_nets
            ):
                continue

        # Skip if exact duplicate of saved net exists.
        # UART dedup needs to handle two ``pin`` formats coexisting in the
        # same saved_nets file:
        #   * Legacy (pre per-tty enumeration): ``pin`` == USB serial, one
        #     net per chip — even multi-channel FT4232H got collapsed to a
        #     single entry. Treat any such net as claiming the WHOLE chip,
        #     so we don't double-add when the new scanner expands to one
        #     net per tty.
        #   * Current: ``pin`` == ``/dev/ttyUSB<N>`` path; one saved net per
        #     interface. Strict ``pin == pin`` match.
        if net["type"] == "uart":
            duplicate = False
            for s in saved_nets:
                if s.get("role") != "uart":
                    continue
                if s.get("instrument") != net["instrument"]:
                    continue
                if s.get("address") != net["addr"]:
                    continue
                saved_pin = s.get("pin")
                # Legacy form: pin == USB serial extracted from address.
                if saved_pin and saved_pin == _serial_from_visa_address(s.get("address")):
                    duplicate = True
                    break
                if saved_pin == net["pin"]:
                    duplicate = True
                    break
            if duplicate:
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

        # Handle debug nets — prompt for device type. The chan starts as one
        # of ``"DEVICE_TYPE"`` (single-channel) or ``"DEVICE_TYPE@A"``/``@B``
        # (multi-channel FTDI). We replace the ``DEVICE_TYPE`` portion while
        # preserving the channel suffix so the OpenOCD backend can route it.
        if net["type"] == "debug" and "DEVICE_TYPE" in str(net["chan"]):
            chan_str = str(net["chan"])
            suffix = ""
            if "@" in chan_str:
                _, _, suffix = chan_str.partition("@")
                suffix = f"@{suffix}"
            channel_hint = f" (channel {suffix[1:]})" if suffix else ""
            device_type = click.prompt(
                f"Enter device type for debug net on {net['instrument']} at {net['addr']}{channel_hint}",
                type=str,
            )
            net["chan"] = f"{device_type}{suffix}"

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


@nets.command("add-batch", help="Add multiple nets from a JSON file")
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


# --------------------------------------------------------------------------- #
# Debug-script commands.                                                      #
#                                                                             #
# `set-script` / `show-script` / `remove-script` handle both J-Link scripts   #
# and OpenOCD .cfg/.tcl files: the backend is auto-detected from the probe   #
# VID and the file extension/content, with `--backend jlink|openocd` as the  #
# explicit override.                                                          #
#                                                                             #
# Mutual exclusivity (KISS): a debug net carries *either* `jlink_script` or  #
# `openocd_config`, never both. `set-script` clears the other field when it  #
# writes; the caller sees a yellow notice so no data goes missing silently.  #
# --------------------------------------------------------------------------- #


def _load_debug_net(ctx: click.Context, resolved_box: str, name: str) -> dict:
    """Fetch the named debug net or click-exit with a useful message."""
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

    return target


def _set_debug_script_impl(
    ctx: click.Context,
    name: str,
    script_path: str,
    box: Optional[str],
    backend: Optional[str],
) -> None:
    """Shared body of ``set-script``.

    ``backend`` is the user's explicit override (``'jlink'`` / ``'openocd'``
    / ``None``). When unset, the backend is derived from the probe VID and
    the file's extension/content; if the two disagree the caller is forced
    to pick one with ``--backend`` so we never silently misroute the write.
    """
    import base64

    resolved_box = _resolve_box(ctx, box)
    target = _load_debug_net(ctx, resolved_box, name)

    display_name, raw_bytes = _read_debug_script_input(script_path)

    probe_be = _probe_backend_for_net(target)
    file_be = _sniff_script_backend(display_name, raw_bytes)
    chosen, reason, _mismatch = _choose_script_backend(
        explicit=backend, probe=probe_be, file=file_be,
    )

    if chosen is None:
        click.secho(reason, fg="red", err=True)
        ctx.exit(1)

    field = _FIELD_FOR_BACKEND[chosen]
    cleared_field, cleared_bytes = _clear_other_script_field(target, field)

    target[field] = base64.b64encode(raw_bytes).decode("ascii")
    _run_net_py(ctx, resolved_box, "save", json.dumps(target))

    if cleared_field:
        size = f"{cleared_bytes} bytes" if cleared_bytes >= 0 else "unknown size"
        click.secho(
            f"Cleared existing {cleared_field} ({size}) — a debug net holds "
            f"only one of jlink_script / openocd_config at a time.",
            fg="yellow",
        )
    click.secho(
        f"{_BACKEND_LABEL[chosen]} set on debug net '{name}' on box "
        f"{resolved_box} ({reason}).",
        fg="green",
    )


def _show_debug_script_impl(
    ctx: click.Context,
    name: str,
    box: Optional[str],
    backend: Optional[str],
) -> None:
    """Shared body of ``show-script``.

    If ``backend`` is set, only that backend's field is considered. Without
    a backend filter we show whichever field is populated.
    """
    import base64

    resolved_box = _resolve_box(ctx, box)
    target = _load_debug_net(ctx, resolved_box, name)

    has_jlink = bool(target.get("jlink_script"))
    has_openocd = bool(target.get("openocd_config"))

    if backend == _BACKEND_JLINK:
        candidates = [(_BACKEND_JLINK, has_jlink)]
    elif backend == _BACKEND_OPENOCD:
        candidates = [(_BACKEND_OPENOCD, has_openocd)]
    else:
        candidates = [(_BACKEND_JLINK, has_jlink), (_BACKEND_OPENOCD, has_openocd)]

    available = [be for be, present in candidates if present]
    if not available:
        wanted = _BACKEND_LABEL.get(backend, "debug script")
        click.secho(
            f"Net '{name}' does not have a {wanted} attached.",
            fg="yellow", err=True,
        )
        ctx.exit(1)

    if len(available) > 1:
        # Defensive: should never happen after set-script enforces exclusivity,
        # but legacy records may have both fields. Ask the user to disambiguate
        # rather than silently picking one.
        click.secho(
            f"Net '{name}' has both jlink_script and openocd_config set "
            "(legacy record). Pass --backend jlink|openocd to choose.",
            fg="red", err=True,
        )
        ctx.exit(1)

    chosen = available[0]
    field = _FIELD_FOR_BACKEND[chosen]
    raw = base64.b64decode(target[field])
    click.echo(
        f"# {_BACKEND_LABEL[chosen]}, {len(raw)} bytes", err=True,
    )
    try:
        click.echo(raw.decode("utf-8"))
    except UnicodeDecodeError:
        # Binary content (shouldn't happen for .cfg/.JLinkScript but be safe):
        # write raw bytes to stdout so redirection still produces an exact copy.
        import sys
        sys.stdout.buffer.write(raw)


def _remove_debug_script_impl(
    ctx: click.Context,
    name: str,
    box: Optional[str],
    backend: Optional[str],
) -> None:
    """Shared body of ``remove-script``."""
    resolved_box = _resolve_box(ctx, box)
    target = _load_debug_net(ctx, resolved_box, name)

    has_jlink = bool(target.get("jlink_script"))
    has_openocd = bool(target.get("openocd_config"))

    if backend == _BACKEND_JLINK:
        wanted = [("jlink_script", has_jlink)]
    elif backend == _BACKEND_OPENOCD:
        wanted = [("openocd_config", has_openocd)]
    else:
        wanted = [("jlink_script", has_jlink), ("openocd_config", has_openocd)]

    present = [f for f, exists in wanted if exists]
    if not present:
        label = _BACKEND_LABEL.get(backend, "debug script")
        click.secho(
            f"Net '{name}' does not have a {label} attached.",
            fg="yellow",
        )
        return

    for field in present:
        del target[field]
    _run_net_py(ctx, resolved_box, "save", json.dumps(target))

    removed = ", ".join(present)
    click.secho(
        f"Removed {removed} from debug net '{name}' on box {resolved_box}.",
        fg="green",
    )


_BACKEND_CLICK_CHOICE = click.Choice([_BACKEND_JLINK, _BACKEND_OPENOCD])


@nets.command(
    "set-script",
    short_help="Attach a J-Link/OpenOCD script to a debug net",
    help="Attach a J-Link script or OpenOCD .cfg/.tcl to a debug net. "
    "Backend is auto-detected from the probe VID and the file extension; "
    "use SCRIPT_PATH='-' to read from stdin.",
)
@click.argument("name")
@click.argument("script_path")
@click.option(
    "--backend", "backend", type=_BACKEND_CLICK_CHOICE, default=None,
    help="Force a specific backend instead of auto-detecting. Required when "
    "the detected probe and file backends disagree.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def set_script_cmd(
    ctx: click.Context, name: str, script_path: str,
    backend: Optional[str], box: Optional[str],
) -> None:
    """Attach a J-Link script or OpenOCD .cfg/.tcl to an existing debug net.

    The file is stored on the box and used automatically during connect,
    flash, erase, and reset operations. A debug net only carries one script
    at a time; if the other field is already populated, it is cleared (with
    a yellow notice on stderr so nothing disappears silently).

    Pass ``SCRIPT_PATH='-'`` to read from stdin, e.g.::

        cat custom.cfg | lager nets set-script SWD - --box JUL-5
    """
    _set_debug_script_impl(ctx, name, script_path, box, backend)


@nets.command(
    "remove-script",
    short_help="Remove a debug net's J-Link/OpenOCD script",
    help="Remove the J-Link script or OpenOCD config attached to a debug net.",
)
@click.argument("name")
@click.option(
    "--backend", "backend", type=_BACKEND_CLICK_CHOICE, default=None,
    help="Only remove the named backend's script (default: remove whichever is set).",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def remove_script_cmd(
    ctx: click.Context, name: str, backend: Optional[str], box: Optional[str],
) -> None:
    _remove_debug_script_impl(ctx, name, box, backend)


@nets.command(
    "show-script",
    short_help="Show a debug net's J-Link/OpenOCD script",
    help="Display the J-Link script or OpenOCD config attached to a debug net.",
)
@click.argument("name")
@click.option(
    "--backend", "backend", type=_BACKEND_CLICK_CHOICE, default=None,
    help="Only show the named backend's script (default: show whichever is set).",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def show_script_cmd(
    ctx: click.Context, name: str, backend: Optional[str], box: Optional[str],
) -> None:
    """Print the script attached to a debug net.

    Script content goes to stdout (so ``> out.cfg`` works); a one-line
    "# OpenOCD config, N bytes" banner goes to stderr so interactive use
    tells you what you're looking at without polluting redirects.
    """
    _show_debug_script_impl(ctx, name, box, backend)


@nets.command("describe", short_help="Set metadata on a saved net",
              help="Set metadata on a saved net (description, DUT connection, hints, tags).")
@click.argument("name")
@click.option("--description", "-d", default=None, help="Human-readable description of the net")
@click.option("--dut-connection", default=None, help="How the net connects to the DUT (e.g., 'MCU SPI1 peripheral')")
@click.option("--hint", "-h", "hints", multiple=True, help="Test hint (repeatable)")
@click.option("--tag", "-t", "tags", multiple=True, help="Tag for categorisation (repeatable)")
@click.option("--clear-hints", is_flag=True, help="Remove all existing test hints before adding new ones")
@click.option("--clear-tags", is_flag=True, help="Remove all existing tags before adding new ones")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def describe_cmd(
    ctx: click.Context,
    name: str,
    description: str | None,
    dut_connection: str | None,
    hints: tuple[str, ...],
    tags: tuple[str, ...],
    clear_hints: bool,
    clear_tags: bool,
    box: str | None,
) -> None:
    """Set metadata fields on a saved net for agent-assisted testing."""
    resolved_box = _resolve_box(ctx, box)

    if description is None and dut_connection is None and not hints and not tags and not clear_hints and not clear_tags:
        click.secho("Nothing to update. Provide at least one of --description, --dut-connection, --hint, or --tag.", fg="yellow")
        return

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

    if description is not None:
        target["description"] = description
    if dut_connection is not None:
        target["dut_connection"] = dut_connection

    if clear_hints:
        target["test_hints"] = []
    if hints:
        existing = target.get("test_hints", [])
        target["test_hints"] = existing + list(hints)

    if clear_tags:
        target["tags"] = []
    if tags:
        existing = target.get("tags", [])
        merged = list(dict.fromkeys(existing + list(tags)))
        target["tags"] = merged

    _run_net_py(ctx, resolved_box, "save", json.dumps(target))
    click.secho(f"Updated metadata for net '{name}' on box {resolved_box}.", fg="green")


@nets.command("show", help="Show full details of a saved net, including metadata")
@click.argument("name")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output as raw JSON")
@click.pass_context
def show_cmd(
    ctx: click.Context,
    name: str,
    box: str | None,
    as_json: bool,
) -> None:
    """Display all fields of a net, including user-provided metadata."""
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

    if as_json:
        click.echo(json.dumps(target, indent=2))
        return

    click.secho(f"Net: {target.get('name', '')}", bold=True)
    click.echo(f"  Type:       {target.get('role', '')}")
    click.echo(f"  Instrument: {target.get('instrument', '')}")
    # ``pin`` is overloaded across roles: device@channel for debug, USB
    # serial or /dev path for uart, GPIO/ADC pin name elsewhere. The old
    # blanket "Channel:" label hid real misconfigurations (e.g. a UART
    # net whose ``pin`` was the integer "2" instead of an FTDI USB
    # serial). Show the role-appropriate label so misuse is visible.
    click.echo(f"  {_pin_label_for_role(target.get('role')):<12}{target.get('pin', '')}")
    click.echo(f"  Address:    {target.get('address', '')}")

    if target.get("jlink_script"):
        click.echo("  Script:     (J-Link script attached)")

    if target.get("openocd_config"):
        click.echo("  OpenOCD:    (OpenOCD config attached)")

    desc = target.get("description", "")
    dut_conn = target.get("dut_connection", "")
    test_hints = target.get("test_hints", [])
    net_tags = target.get("tags", [])

    if desc or dut_conn or test_hints or net_tags:
        click.echo()
        click.secho("  Metadata:", bold=True)
        if desc:
            click.echo(f"    Description:    {desc}")
        if dut_conn:
            click.echo(f"    DUT Connection: {dut_conn}")
        if test_hints:
            click.echo("    Test Hints:")
            for h in test_hints:
                click.echo(f"      - {h}")
        if net_tags:
            click.echo(f"    Tags:           {', '.join(net_tags)}")

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.instruments

    Instruments commands
"""
import click
import shutil
import requests
from texttable import Texttable
from ...box_storage import resolve_and_validate_box
from ...core.net_group import BoxCommand
from collections import defaultdict
from ...sort_utils import natural_sort_key

_MULTI_HUBS = {"LabJack_T7", "Acroname_8Port", "Acroname_4Port"}

@click.command(cls=BoxCommand)
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def instruments(ctx, box: str | None) -> None:
    """List attached instruments"""
    # Resolve and validate the box name
    resolved_box = resolve_and_validate_box(ctx, box)

    # The box HTTP server scans USB in-process (same records the old
    # query_instruments.py exec printed: name/address/channels/tty_path).
    from ...gateway_auth import auth_headers_for_box
    from ...box_storage import _check_gateway
    try:
        resp = requests.get(
            f'http://{resolved_box}:9000/instruments/list', timeout=30,
            headers=auth_headers_for_box(resolved_box),
        )
        _check_gateway(resp, resolved_box)
    except requests.exceptions.RequestException as e:
        click.secho(f"Error querying instruments: {e}", fg="red", err=True)
        click.secho(
            "Check box connectivity with 'lager hello'.", fg="yellow", err=True,
        )
        ctx.exit(1)
        return

    instruments_data = None
    if resp.status_code == 200:
        try:
            instruments_data = resp.json()
        except ValueError:
            instruments_data = None

    if not isinstance(instruments_data, list):
        click.secho(
            "Error: Could not parse instrument data from box",
            fg="red",
            err=True,
        )
        detail = resp.text or ""
        if detail:
            click.secho(f"Box response (HTTP {resp.status_code}): {detail[:500]}", fg="yellow", err=True)
        ctx.exit(1)

    if not instruments_data:
        click.echo("No instruments detected.")
        return

    inst_counts: dict[str, int] = defaultdict(int)
    for dev in instruments_data:
        inst_counts[dev.get("name")] += 1

    duplicated: set[str] = {
        name for name, cnt in inst_counts.items()
        if name in _MULTI_HUBS and cnt > 1
    }

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_align(["l", "l", "l"])
    table.set_cols_dtype(["t", "t", "t"])
    table.set_cols_width([22, 60, 45])

    table.add_row(["Name", "Channels", "VISA Address"])

    for dev in instruments_data:
        if dev.get("name") in duplicated:
            continue

        chan_map = dev.get("channels", {})
        if chan_map:
            lines = []
            for role, chs in sorted(chan_map.items(), key=lambda x: natural_sort_key(x[0])):
                if chs:
                    lines.append(f"{role}: {', '.join(chs)}")
                else:
                    lines.append(f"{role}: —")
            channels_str = "\n".join(lines)
        else:
            channels_str = "—"

        table.add_row(
            [
                dev.get("name", "—"),
                channels_str,
                dev.get("address", "—"),
            ]
        )

    rendered = table.draw().splitlines()
    if len(rendered) > 1:
        # Calculate separator width, limited to terminal width
        term_width = shutil.get_terminal_size((120, 24)).columns
        header_width = len(rendered[0])
        separator_width = min(header_width, term_width)
        rendered.insert(1, "=" * separator_width)
    click.echo("\n".join(rendered))

    for name in sorted(duplicated, key=natural_sort_key):
        click.secho(
            f"Multiple {name} devices detected – unplug extras before adding nets.",
            fg="yellow",
        )

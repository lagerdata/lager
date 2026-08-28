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
from ...sort_utils import natural_sort_key
from ._device_identity import ambiguous_addresses, describe_ambiguity


@click.command(cls=BoxCommand)
@click.option("--box", required=False, help="Lager Box name or IP")
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
        resp = _check_gateway(resp, resolved_box)
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
            "Error: The instrument data from the box did not parse",
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

    # Hide only what cannot be addressed: two devices reporting ONE address.
    # Two devices of a model with distinct serials are listed normally -- they
    # are separately drivable, and hiding them meant you could not read the
    # addresses needed to create their nets.
    ambiguous: set[str] = ambiguous_addresses(instruments_data)
    duplicated: set[tuple[str, str]] = {
        (dev.get("name"), dev.get("address"))
        for dev in instruments_data
        if dev.get("address") in ambiguous
    }

    table = Texttable()
    table.set_deco(Texttable.HEADER)
    table.set_cols_align(["l", "l", "l"])
    table.set_cols_dtype(["t", "t", "t"])
    table.set_cols_width([22, 60, 45])

    table.add_row(["Name", "Channels", "VISA Address"])

    for dev in instruments_data:
        if dev.get("address") in ambiguous:
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

    for name, addr in sorted(duplicated, key=lambda x: natural_sort_key(x[0])):
        click.secho(describe_ambiguity(name, addr), fg="yellow")

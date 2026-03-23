# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    lager.commands.box.instruments

    Instruments commands
"""
import click
import json
import shutil
from texttable import Texttable
from ...context import get_impl_path
from ..development.python import run_python_internal
from ...context import get_default_box
from ...box_storage import resolve_and_validate_box
from collections import defaultdict
from ...sort_utils import natural_sort_key

import io
from contextlib import redirect_stdout

_MULTI_HUBS = {"LabJack_T7", "Acroname_8Port", "Acroname_4Port"}

@click.command()
@click.option("--box", required=False, help="Lagerbox name or IP")
@click.pass_context
def instruments(ctx, box: str | None) -> None:
    """List attached instruments"""
    # Resolve and validate the box name
    resolved_box = resolve_and_validate_box(ctx, box)

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            run_python_internal(
                ctx,
                get_impl_path("query_instruments.py"),
                resolved_box,
                env={},
                passenv=(),
                kill=False,
                download=(),
                allow_overwrite=False,
                signum="SIGTERM",
                timeout=30,  # 30 second timeout
                detach=False,
                port=(),
                org=None,
                args=(),
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
        instruments_data = json.loads(raw_output or "[]")
    except json.JSONDecodeError:
        click.secho(
            "Error: Could not parse instrument data from box",
            fg="red",
            err=True,
        )
        if not raw_output:
            click.secho(
                "No output received from backend. Check box connectivity with 'lager hello'.",
                fg="yellow",
                err=True,
            )
        else:
            click.secho(f"Raw output: {repr(raw_output[:500])}", fg="yellow", err=True)
            if len(raw_output) > 500:
                click.secho("(output truncated)", fg="yellow", err=True)
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
                    # Truncate UART serial numbers to 10 chars to reduce clutter
                    if role == "uart":
                        chs_display = [ch[:10] if len(ch) > 10 else ch for ch in chs]
                    else:
                        chs_display = chs
                    lines.append(f"{role}: {', '.join(chs_display)}")
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

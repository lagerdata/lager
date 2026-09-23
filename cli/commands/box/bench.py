# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager bench` -- the bench manifest a Lager Box publishes for AI tooling.

A box describes itself to an AI agent through its MCP server one question
at a time. ``GET /bench`` on the box's :9000 API serves the whole
description at once -- nets with their metadata, detected instruments, DUT
context, capabilities, and which API reference documents each net -- as one
versioned JSON document with a content hash. ``lager bench export`` fetches
that document, for a control plane, a fleet-level MCP host, or a person who
wants to diff two boxes.
"""
from __future__ import annotations

import json
from typing import Optional

import click
import requests

from .config import _resolve_box
from ...box_storage import _check_gateway
from ...core.group_usage import LagerGroup
from ...gateway_auth import auth_headers_for_box

# The box release that first serves GET /bench. A 404 from an older box is
# not "no bench"; it is "no route", and the fix is an update.
_FIRST_MANIFEST_VERSION = "0.50.0"


@click.group(
    name="bench",
    cls=LagerGroup,
    help="Read the bench manifest a Lager Box publishes for AI tooling.",
)
def box_bench() -> None:
    pass


@box_bench.command(
    "export",
    help=(
        "Fetch the box's bench manifest (nets, instruments, DUT context, "
        "capabilities) as JSON."
    ),
)
@click.option("--box", help="Lager Box name or IP")
@click.option(
    "--out", "out_path",
    type=click.Path(dir_okay=False, writable=True),
    help="Write the manifest to this file instead of standard output.",
)
@click.option("--compact", is_flag=True, help="One line of JSON instead of an indented document.")
@click.pass_context
def export_cmd(ctx: click.Context, box: Optional[str], out_path: Optional[str], compact: bool) -> None:
    box_ip = _resolve_box(ctx, box)

    try:
        resp = requests.get(
            f"http://{box_ip}:9000/bench",
            timeout=30,
            headers=auth_headers_for_box(box_ip),
        )
        resp = _check_gateway(resp, box_ip)
    except requests.exceptions.RequestException as e:
        click.secho(f"Error fetching the bench manifest from {box_ip}: {e}", fg="red", err=True)
        click.secho("Check box connectivity with 'lager hello'.", fg="yellow", err=True)
        ctx.exit(1)
        return

    if resp.status_code == 404:
        click.secho(
            f"Box {box_ip} does not serve a bench manifest. It needs lager "
            f"{_FIRST_MANIFEST_VERSION} or later; run 'lager update --box {box_ip}'.",
            fg="red", err=True,
        )
        ctx.exit(1)
        return
    if resp.status_code != 200:
        click.secho(
            f"Box {box_ip} answered HTTP {resp.status_code} for the bench manifest.",
            fg="red", err=True,
        )
        detail = (resp.text or "").strip()
        if detail:
            click.secho(f"Box response: {detail[:500]}", fg="yellow", err=True)
        ctx.exit(1)
        return

    try:
        manifest = resp.json()
    except ValueError:
        manifest = None
    if not isinstance(manifest, dict) or "schema_version" not in manifest:
        click.secho(
            f"The bench manifest from {box_ip} did not parse as a manifest document.",
            fg="red", err=True,
        )
        ctx.exit(1)
        return

    text = json.dumps(manifest, indent=None if compact else 2, sort_keys=True)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.write("\n")
        click.echo(
            f"Wrote the bench manifest for {manifest.get('box_id') or box_ip} to {out_path} "
            f"(schema {manifest['schema_version']}, hash {str(manifest.get('content_hash', ''))[:12]}).",
            err=True,
        )
    else:
        click.echo(text)

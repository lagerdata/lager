# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager box config` — declarative per-box provisioning.

Reads/writes /etc/lager/box_config.json on the box (via the
in-container shim cli/impl/box_config.py) and ships an `apply` verb
that bounces the container to put the new config into effect.

Mirrors the shape of `lager nets`: same --box resolution, same shim
plumbing via run_python_internal, same JSON-on-stdout protocol.
"""
from __future__ import annotations

import ipaddress
import json
import subprocess
from typing import Any, Optional

import click

from ...context import get_default_box, get_impl_path


def _resolve_box(ctx: click.Context, box_opt: Optional[str] = None) -> str:
    from ...box_storage import get_box_ip, list_boxes

    target_box = None
    if box_opt:
        target_box = box_opt
    elif ctx.parent is not None and "box" in ctx.parent.params and ctx.parent.params["box"]:
        target_box = ctx.parent.params["box"]

    if target_box:
        local_ip = get_box_ip(target_box)
        if local_ip:
            return local_ip
        try:
            ipaddress.ip_address(target_box)
            return target_box
        except ValueError:
            click.secho(f"Error: Box '{target_box}' is not recorded in the system.", fg="red", err=True)
            saved_boxes = list_boxes()
            if saved_boxes:
                click.echo("Available boxes:", err=True)
                for name, info in sorted(saved_boxes.items()):
                    ip = info.get("ip", "unknown") if isinstance(info, dict) else info
                    click.echo(f"  - {name} ({ip})", err=True)
            ctx.exit(1)

    return get_default_box(ctx)


def _run_box_config_py(ctx: click.Context, box: str, *args: str) -> str:
    from ..development.python import run_python_internal_get_output

    try:
        output = run_python_internal_get_output(
            ctx,
            get_impl_path("box_config.py"),
            box,
            env=(),
            passenv=(),
            kill=False,
            download=(),
            allow_overwrite=False,
            signum="SIGTERM",
            timeout=30,
            detach=False,
            port=(),
            org=None,
            args=args,
        )
        return output.decode("utf-8") if isinstance(output, bytes) else output
    except SystemExit as e:
        if e.code != 0:
            raise
        return ""


def _parse_response(raw: str, ctx: click.Context) -> Any:
    raw = (raw or "").strip()
    if not raw:
        click.secho("No response from box. Check connectivity with 'lager hello'.", fg="red", err=True)
        ctx.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        click.secho(f"Failed to parse box response: {raw!r}", fg="red", err=True)
        ctx.exit(1)


def _print_errors(errors: list[str]) -> None:
    for i, err in enumerate(errors, start=1):
        click.secho(f"  {i}. {err}", fg="red", err=True)


@click.group(name="config", invoke_without_command=True, help="Manage declarative box provisioning.")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def box_config(ctx: click.Context, box: Optional[str]) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(show_cmd, box=box, as_json=False)


@box_config.command("show", help="Print the current box_config.json.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def show_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx)
    if payload is None:
        click.secho(f"No box_config.json on {resolved}. Run `lager box config init`.", fg="yellow")
        return
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    _render_human(payload)


def _render_human(payload: dict) -> None:
    click.secho(f"Box config (version {payload.get('version', '?')})", bold=True)

    mounts = payload.get("mounts") or []
    click.echo()
    click.secho("Mounts:", bold=True)
    if not mounts:
        click.echo("  (none)")
    for m in mounts:
        ro = " (ro)" if m.get("readonly") else ""
        click.echo(f"  {m.get('host', '?')} -> {m.get('container', '?')}{ro}")

    volumes = payload.get("volumes") or []
    click.echo()
    click.secho("Volumes:", bold=True)
    if not volumes:
        click.echo("  (none)")
    for v in volumes:
        click.echo(f"  {v.get('name', '?')} -> {v.get('container', '?')}")

    env = payload.get("env") or {}
    click.echo()
    click.secho("Env:", bold=True)
    if not env:
        click.echo("  (none)")
    for k, v in env.items():
        click.echo(f"  {k}={v}")

    extras = {k: v for k, v in payload.items() if k not in {"version", "mounts", "volumes", "env"}}
    if extras:
        click.echo()
        click.secho("Extras (round-tripped, not yet applied):", bold=True)
        for k in extras:
            click.echo(f"  {k}")


@box_config.command("init", help="Create /etc/lager/box_config.json with defaults.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--force", is_flag=True, help="Overwrite if file already exists")
@click.pass_context
def init_cmd(ctx: click.Context, box: Optional[str], force: bool) -> None:
    resolved = _resolve_box(ctx, box)
    args = ["init"]
    if force:
        args.append("--force")
    raw = _run_box_config_py(ctx, resolved, *args)
    payload = _parse_response(raw, ctx)
    if payload.get("created"):
        click.secho(f"Created /etc/lager/box_config.json on {resolved}.", fg="green")
    else:
        click.secho(
            f"/etc/lager/box_config.json already exists on {resolved}. Use --force to overwrite.",
            fg="yellow",
        )


@box_config.command("validate", help="Validate the current box_config.json.")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def validate_cmd(ctx: click.Context, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "validate")
    payload = _parse_response(raw, ctx)
    if not payload.get("exists", True):
        click.secho(f"No box_config.json on {resolved}; nothing to validate.", fg="yellow")
        return
    if payload.get("ok"):
        click.secho("Config is valid.", fg="green")
        return
    click.secho("Config has errors:", fg="red", err=True)
    _print_errors(payload.get("errors") or [])
    ctx.exit(1)


@box_config.command(
    "apply",
    help="Validate, then bounce the container so the new config takes effect.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--force", is_flag=True, help="Restart even if config hash is unchanged")
@click.option(
    "--skip-restart",
    is_flag=True,
    help="Validate and update applied-hash only; do not bounce the container",
)
@click.pass_context
def apply_cmd(
    ctx: click.Context,
    box: Optional[str],
    yes: bool,
    force: bool,
    skip_restart: bool,
) -> None:
    resolved = _resolve_box(ctx, box)

    raw = _run_box_config_py(ctx, resolved, "validate")
    payload = _parse_response(raw, ctx)
    if not payload.get("exists", True):
        click.secho(
            f"No box_config.json on {resolved}. Run `lager box config init` first.",
            fg="yellow",
        )
        ctx.exit(1)
    if not payload.get("ok"):
        click.secho("Refusing to apply: config has errors:", fg="red", err=True)
        _print_errors(payload.get("errors") or [])
        ctx.exit(1)

    raw = _run_box_config_py(ctx, resolved, "hash")
    cur_hash = _parse_response(raw, ctx).get("hash")
    raw = _run_box_config_py(ctx, resolved, "applied-hash")
    applied_hash = _parse_response(raw, ctx).get("hash")

    unchanged = cur_hash and applied_hash and cur_hash == applied_hash
    if unchanged and not force:
        click.secho("Config unchanged since last apply; skipping restart.", fg="green")
        return

    if skip_restart:
        _run_box_config_py(ctx, resolved, "set-applied-hash", cur_hash)
        click.secho("Config validated; restart skipped (--skip-restart).", fg="yellow")
        return

    if not yes and not click.confirm(
        f"Apply box config and restart the lager container on {resolved}?",
        default=True,
    ):
        click.secho("Aborted.", fg="yellow")
        return

    if not _bounce_container(ctx, resolved):
        click.secho("Container restart failed; not updating applied-hash.", fg="red", err=True)
        ctx.exit(1)

    _run_box_config_py(ctx, resolved, "set-applied-hash", cur_hash)
    click.secho(f"Applied box config on {resolved}.", fg="green")


def _bounce_container(ctx: click.Context, resolved_box: str) -> bool:
    from ...box_storage import get_box_user
    user = get_box_user(resolved_box) or "lagerdata"
    ssh_host = f"{user}@{resolved_box}"
    cmd = "cd ~/box && ./start_box.sh"
    click.echo(f"Restarting lager container on {resolved_box} via SSH...")
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", ssh_host, cmd],
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        click.secho("SSH command timed out after 5 minutes.", fg="red", err=True)
        return False
    except FileNotFoundError:
        click.secho("ssh not found on local machine.", fg="red", err=True)
        return False
    return result.returncode == 0


@box_config.group("mount", help="Manage host-to-container bind mounts.")
def mount_group() -> None:
    pass


@mount_group.command("list", help="List configured mounts.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def mount_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx) or {}
    mounts = payload.get("mounts") or []
    if as_json:
        click.echo(json.dumps(mounts, indent=2))
        return
    if not mounts:
        click.echo("No mounts configured.")
        return
    for m in mounts:
        ro = " (ro)" if m.get("readonly") else ""
        click.echo(f"{m.get('host', '?')} -> {m.get('container', '?')}{ro}")


@mount_group.command("add", help="Add a host-to-container bind mount.")
@click.argument("host")
@click.argument("container")
@click.option("--readonly", is_flag=True, help="Mount as read-only")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def mount_add_cmd(
    ctx: click.Context,
    host: str,
    container: str,
    readonly: bool,
    box: Optional[str],
) -> None:
    resolved = _resolve_box(ctx, box)
    payload_json = json.dumps({"host": host, "container": container, "readonly": readonly})
    raw = _run_box_config_py(ctx, resolved, "mount-add", payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to add mount:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    click.secho(
        f"Added mount {host} -> {container}{' (ro)' if readonly else ''} on {resolved}.",
        fg="green",
    )
    click.echo("Run `lager box config apply` to restart the container.")


@mount_group.command("remove", help="Remove a bind mount by host+container path.")
@click.argument("host")
@click.argument("container")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def mount_remove_cmd(
    ctx: click.Context,
    host: str,
    container: str,
    yes: bool,
    box: Optional[str],
) -> None:
    resolved = _resolve_box(ctx, box)
    if not yes and not click.confirm(f"Remove mount {host} -> {container} on {resolved}?"):
        click.secho("Aborted.", fg="yellow")
        return
    raw = _run_box_config_py(ctx, resolved, "mount-remove", host, container)
    payload = _parse_response(raw, ctx)
    if payload.get("removed"):
        click.secho(f"Removed mount {host} -> {container} on {resolved}.", fg="green")
        click.echo("Run `lager box config apply` to restart the container.")
    else:
        click.secho(f"No matching mount on {resolved}.", fg="yellow")


@box_config.group("volume", help="Manage named docker volumes attached to the container.")
def volume_group() -> None:
    pass


@volume_group.command("list", help="List configured volumes.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def volume_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx) or {}
    volumes = payload.get("volumes") or []
    if as_json:
        click.echo(json.dumps(volumes, indent=2))
        return
    if not volumes:
        click.echo("No volumes configured.")
        return
    for v in volumes:
        click.echo(f"{v.get('name', '?')} -> {v.get('container', '?')}")


@volume_group.command("add", help="Add a named docker volume.")
@click.argument("name")
@click.argument("container")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def volume_add_cmd(
    ctx: click.Context,
    name: str,
    container: str,
    box: Optional[str],
) -> None:
    resolved = _resolve_box(ctx, box)
    payload_json = json.dumps({"name": name, "container": container})
    raw = _run_box_config_py(ctx, resolved, "volume-add", payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to add volume:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    click.secho(f"Added volume {name} -> {container} on {resolved}.", fg="green")
    click.echo("Run `lager box config apply` to restart the container.")


@volume_group.command("remove", help="Remove a named docker volume by name.")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def volume_remove_cmd(
    ctx: click.Context,
    name: str,
    yes: bool,
    box: Optional[str],
) -> None:
    resolved = _resolve_box(ctx, box)
    if not yes and not click.confirm(f"Remove volume {name} on {resolved}?"):
        click.secho("Aborted.", fg="yellow")
        return
    raw = _run_box_config_py(ctx, resolved, "volume-remove", name)
    payload = _parse_response(raw, ctx)
    if payload.get("removed"):
        click.secho(f"Removed volume {name} on {resolved}.", fg="green")
        click.echo("Run `lager box config apply` to restart the container.")
    else:
        click.secho(f"No volume named {name} on {resolved}.", fg="yellow")

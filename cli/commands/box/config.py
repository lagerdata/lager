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
import time
from typing import Any, Optional

import click

from ...context import get_default_box, get_impl_path

# How long we'll wait for the box's HTTP API to come up after start_box.sh
# returns 0. The container itself starts in ~3-5s on a healthy box; the
# generous cap covers slow boxes and pip-install steps that run inline.
_API_READY_DEADLINE_SECONDS = 30
_API_READY_POLL_INTERVAL_SECONDS = 1
_BOX_API_PORT = 5000


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


_FIRST_CLASS_KEYS = frozenset({
    "version", "mounts", "volumes", "env",
    "pip_packages", "apt_packages", "sysctl", "cargo_packages",
})


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

    pip_packages = payload.get("pip_packages") or []
    click.echo()
    click.secho("Pip packages:", bold=True)
    if not pip_packages:
        click.echo("  (none)")
    for p in pip_packages:
        click.echo(f"  {p}")

    apt_packages = payload.get("apt_packages") or []
    click.echo()
    click.secho("Apt packages (host):", bold=True)
    if not apt_packages:
        click.echo("  (none)")
    for p in apt_packages:
        click.echo(f"  {p}")

    sysctl = payload.get("sysctl") or {}
    click.echo()
    click.secho("Sysctl:", bold=True)
    if not sysctl:
        click.echo("  (none)")
    for k, v in sysctl.items():
        click.echo(f"  {k} = {v}")

    cargo_packages = payload.get("cargo_packages") or []
    click.echo()
    click.secho("Cargo packages:", bold=True)
    if not cargo_packages:
        click.echo("  (none)")
    for p in cargo_packages:
        click.echo(f"  {p}")

    extras = {k: v for k, v in payload.items() if k not in _FIRST_CLASS_KEYS}
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
        imported = payload.get("imported") or []
        if imported:
            click.secho(
                f"Migrated {len(imported)} pip package(s) from /etc/lager/user_requirements.txt: "
                + ", ".join(imported),
                fg="green",
            )
            click.echo("Run `lager box config apply` to (re)install them in the container.")
        skipped = payload.get("skipped") or []
        if skipped:
            click.secho(f"Skipped {len(skipped)} legacy package(s):", fg="yellow")
            for s in skipped:
                pkg = s.get("package") if isinstance(s, dict) else s
                reason = s.get("reason") if isinstance(s, dict) else "unknown"
                click.secho(f"  - {pkg!r}: {reason}", fg="yellow")
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
@click.option(
    "--no-auto-prep",
    is_flag=True,
    help="Skip host-path re-verification before restart.",
)
@click.option(
    "--recursive-chown",
    is_flag=True,
    help=(
        "For any configured mount whose host path is wrong-owned and populated, "
        "recursively chown it to uid 33 (www-data)."
    ),
)
@click.pass_context
def apply_cmd(
    ctx: click.Context,
    box: Optional[str],
    yes: bool,
    force: bool,
    skip_restart: bool,
    no_auto_prep: bool,
    recursive_chown: bool,
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

    if not no_auto_prep:
        if not _preflight_mounts(ctx, resolved, recursive=recursive_chown):
            ctx.exit(1)

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

    # Host-side provisioning that has to happen BEFORE the container bounce:
    # apt packages may be needed by services the container talks to, and
    # sysctl values must be in place so first-packet routing works the
    # moment the container comes up.
    current_show = _parse_response(_run_box_config_py(ctx, resolved, "show"), ctx) or {}
    applied_snapshot = _parse_response(
        _run_box_config_py(ctx, resolved, "applied-show"), ctx
    )
    if not _ensure_apt_packages(resolved, current_show, applied_snapshot):
        ctx.exit(1)
    if not _ensure_sysctl(resolved, current_show, applied_snapshot):
        ctx.exit(1)

    if not _bounce_container(ctx, resolved):
        # Bounce of the new config failed. The container may be down (start_box.sh
        # exits between `docker stop` and a successful `docker run` when, e.g.,
        # a mount entry is malformed). Try to restore the last applied snapshot
        # and bring the box back up on the previous good config.
        if _attempt_rollback(ctx, resolved):
            click.secho(
                "Rolled back to the previously applied config; the new config "
                "was rejected. The container is up on the previous config — fix "
                "/etc/lager/box_config.json and re-run `lager box config apply`.",
                fg="yellow",
                err=True,
            )
        else:
            click.secho(
                "Container restart failed and rollback was not possible. The "
                "container may be down. SSH into the box, fix /etc/lager/box_config.json, "
                "and run `~/box/start_box.sh` manually.",
                fg="red",
                err=True,
            )
        ctx.exit(1)

    if not _wait_for_box_api(resolved):
        click.secho(
            f"Container restarted but the box API didn't come up within "
            f"{_API_READY_DEADLINE_SECONDS}s; not updating applied-hash. The "
            "bounce succeeded, but next `apply` will re-bounce unnecessarily. "
            "Check `lager hello` and the container logs.",
            fg="yellow",
            err=True,
        )
        ctx.exit(1)

    _run_box_config_py(ctx, resolved, "set-applied-hash", cur_hash)
    click.secho(f"Applied box config on {resolved}.", fg="green")


def _ensure_apt_packages(
    resolved_box: str,
    current: dict,
    applied: Optional[dict],
) -> bool:
    """Install apt packages declared in current config. No-op when the field
    hasn't changed since the last applied snapshot — apt-get is fast for
    already-installed packages but the SSH round-trip is still ~seconds and
    re-running apply with no apt changes shouldn't pay that cost."""
    from ._host_ops import apt_install

    pkgs = current.get("apt_packages") or []
    prev = (applied or {}).get("apt_packages") or []
    if list(pkgs) == list(prev):
        if pkgs:
            click.secho(f"Apt packages unchanged ({len(pkgs)}); skipping install.", fg="blue")
        return True
    if not pkgs:
        # Field was emptied — we don't auto-uninstall. Apt packages tend to be
        # things customers want to keep around (tcpdump etc.) even when no
        # longer declared. Surface the no-op explicitly.
        click.secho(
            "apt_packages is empty; not uninstalling previously-declared "
            "packages (manual `sudo apt-get remove` if you need that).",
            fg="yellow",
        )
        return True
    click.echo(f"Installing apt packages on {resolved_box}: {', '.join(pkgs)}")
    result = apt_install(resolved_box, list(pkgs))
    if not result.ok:
        click.secho(f"apt install failed: {result.message}", fg="red", err=True)
        if result.manual_fix:
            click.echo(f"  Manual fix on the box: {result.manual_fix}", err=True)
        return False
    click.secho(result.message, fg="green")
    return True


def _ensure_sysctl(
    resolved_box: str,
    current: dict,
    applied: Optional[dict],
) -> bool:
    """Persist sysctl values declared in current config. No-op when unchanged."""
    from ._host_ops import sysctl_apply

    sysctl = current.get("sysctl") or {}
    prev = (applied or {}).get("sysctl") or {}
    if dict(sysctl) == dict(prev):
        if sysctl:
            click.secho(f"Sysctl unchanged ({len(sysctl)}); skipping reload.", fg="blue")
        return True
    if sysctl:
        click.echo(f"Applying {len(sysctl)} sysctl key(s) on {resolved_box}...")
    else:
        click.echo(f"Clearing sysctl conf on {resolved_box}...")
    result = sysctl_apply(resolved_box, dict(sysctl))
    if not result.ok:
        click.secho(f"sysctl apply failed: {result.message}", fg="red", err=True)
        if result.manual_fix:
            click.echo(f"  Manual fix on the box: {result.manual_fix}", err=True)
        return False
    click.secho(result.message, fg="green")
    return True


def _attempt_rollback(ctx: click.Context, resolved_box: str) -> bool:
    """Restore the last applied snapshot and re-bounce. Returns True iff the
    box is back up on the previous good config.

    First-apply boxes have no snapshot to fall back to; in that case there's
    nothing we can do remotely and the user has to recover by hand.
    """
    raw = _run_box_config_py(ctx, resolved_box, "restore-applied")
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        return False
    click.secho(
        "New config rejected; rolling back to last applied config and "
        "restarting...",
        fg="yellow",
        err=True,
    )
    if not _bounce_container(ctx, resolved_box):
        return False
    # Don't gate the rollback message on API readiness — the bounce returning
    # 0 is a strong enough signal that docker run accepted the previous config.
    # If the API doesn't come up, that's a separate problem the user will see
    # via `lager hello`, not a rollback failure.
    return True


def _preflight_mounts(ctx: click.Context, resolved: str, *, recursive: bool) -> bool:
    """Re-verify each configured mount's host path before bouncing the container.

    Catches the case where a mount was added by editing /etc/lager/box_config.json
    directly (skipping `mount add`'s auto-prep). Returns False on any failure that
    should abort apply.
    """
    from ._mount_prep import ensure_host_path_owned

    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx) or {}
    mounts = payload.get("mounts") or []
    if not mounts:
        return True

    failed = False
    for m in mounts:
        host = m.get("host")
        container = m.get("container")
        if not isinstance(host, str):
            continue
        result = ensure_host_path_owned(
            resolved,
            host,
            readonly=bool(m.get("readonly")),
            recursive=recursive,
        )
        if result.ok:
            if result.action not in ("ok", "ok_readonly"):
                click.secho(f"Mount {host} -> {container}: {result.message}", fg="green")
            continue
        failed = True
        click.secho(
            f"Mount {host} -> {container}: {result.message}",
            fg="red",
            err=True,
        )
        if result.manual_fix:
            click.echo(f"  Manual fix: {result.manual_fix}", err=True)

    if failed:
        click.secho(
            "One or more mounts failed pre-flight; aborting before container restart. "
            "Use --recursive-chown to fix populated wrong-owner directories, or "
            "--no-auto-prep to skip the check.",
            fg="red",
            err=True,
        )
        return False
    return True


def _box_api_responding(box_ip: str, *, timeout: float = 2.0) -> bool:
    """Single probe of the box's Python execution service. True iff the box
    answers `/hello` with any HTTP status (i.e., the service is bound and
    accepting connections). Catches every transport-level failure so the
    caller can poll cheaply."""
    import requests
    try:
        r = requests.get(f"http://{box_ip}:{_BOX_API_PORT}/hello", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def _wait_for_box_api(
    box_ip: str,
    *,
    deadline_seconds: float = _API_READY_DEADLINE_SECONDS,
    poll_interval: float = _API_READY_POLL_INTERVAL_SECONDS,
    sleeper=time.sleep,
    clock=time.monotonic,
    is_responding=None,
) -> bool:
    """Block until the box's API responds, or the deadline passes.

    `sleeper`, `clock`, and `is_responding` are injected so unit tests can
    drive the polling loop without real time or real HTTP. Production calls
    use the defaults.
    """
    probe = is_responding or _box_api_responding
    deadline = clock() + deadline_seconds
    while True:
        if probe(box_ip):
            return True
        if clock() >= deadline:
            return False
        sleeper(poll_interval)


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
@click.option(
    "--no-auto-prep",
    is_flag=True,
    help="Skip the auto mkdir/chown of the host path. Use when the directory is provisioned externally.",
)
@click.option(
    "--recursive-chown",
    is_flag=True,
    help=(
        "If the host path already exists with the wrong owner and contains files, recursively "
        "chown it to uid 33 (www-data). Required to opt in to modifying existing contents."
    ),
)
@click.pass_context
def mount_add_cmd(
    ctx: click.Context,
    host: str,
    container: str,
    readonly: bool,
    box: Optional[str],
    no_auto_prep: bool,
    recursive_chown: bool,
) -> None:
    resolved = _resolve_box(ctx, box)

    # Run host-path prep BEFORE persisting to box_config.json. If the prep
    # fails (refused-populated, sudo not configured, etc.), the JSON stays
    # untouched and the user can re-run `mount add` after fixing the issue
    # rather than fight the duplicate-container validator on retry.
    prep_result = None
    if not no_auto_prep:
        from ._mount_prep import ensure_host_path_owned
        prep_result = ensure_host_path_owned(
            resolved, host, readonly=readonly, recursive=recursive_chown,
        )
        if not prep_result.ok:
            click.secho(
                f"Host path prep failed for {host} -> {container}: {prep_result.message}",
                fg="red",
                err=True,
            )
            if prep_result.manual_fix:
                click.echo("Manual fix (SSH into the box and run):", err=True)
                click.echo(f"  {prep_result.manual_fix}", err=True)
            click.secho(
                "Mount NOT added to box_config.json. Fix the host path and re-run.",
                fg="yellow",
                err=True,
            )
            ctx.exit(1)

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

    if no_auto_prep:
        from ._mount_prep import manual_fix_command
        click.secho(
            f"Skipped host-path prep (--no-auto-prep). If {host} doesn't exist or isn't "
            f"writable by uid 33, run on the box: {manual_fix_command(host)}",
            fg="yellow",
        )
    else:
        if prep_result.action in ("ok", "ok_readonly"):
            click.echo(f"Host path: {prep_result.message}")
        else:
            click.secho(f"Host path: {prep_result.message}", fg="green")

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


@box_config.group("pip", help="Manage user-installed Python packages.")
def pip_group() -> None:
    pass


@pip_group.command("list", help="List user-installed pip packages.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def pip_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx) or {}
    pkgs = payload.get("pip_packages") or []
    if as_json:
        click.echo(json.dumps(pkgs, indent=2))
        return
    if not pkgs:
        click.echo("No pip packages configured.")
        return
    for p in pkgs:
        click.echo(p)


@pip_group.command("add", help="Add one or more pip packages to the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.option("--no-validate-pypi", is_flag=True, help="Skip PyPI existence check")
@click.pass_context
def pip_add_cmd(
    ctx: click.Context,
    packages: tuple,
    box: Optional[str],
    no_validate_pypi: bool,
) -> None:
    from ._pip_validation import validate_format, validate_on_pypi, is_direct_ref

    resolved = _resolve_box(ctx, box)

    fmt_errors = []
    for p in packages:
        ok, reason = validate_format(p)
        if not ok:
            fmt_errors.append((p, reason))
    if fmt_errors:
        click.secho("Invalid package specification(s):", fg="red", err=True)
        for p, r in fmt_errors:
            click.secho(f"  - {p!r}: {r}", fg="red", err=True)
        ctx.exit(1)

    if not no_validate_pypi:
        to_check = [p for p in packages if not is_direct_ref(p)]
        skipped_direct = [p for p in packages if is_direct_ref(p)]
        if to_check:
            click.secho("Validating packages on PyPI...", fg="blue")
            invalid, network_errors = validate_on_pypi(to_check)
            if invalid:
                click.secho("The following packages could not be validated:", fg="red", err=True)
                for p, r in invalid:
                    click.secho(f"  - {p}: {r}", fg="red", err=True)
                click.secho(
                    "No changes made. Use --no-validate-pypi to skip the PyPI check.",
                    fg="yellow", err=True,
                )
                ctx.exit(1)
            if network_errors:
                click.secho("Could not validate some packages (network):", fg="yellow", err=True)
                for p, r in network_errors:
                    click.secho(f"  - {p}: {r}", fg="yellow", err=True)
                click.echo("Proceeding anyway; install will fail later if the package is bad.", err=True)
            click.secho("PyPI validation passed.", fg="green")
        if skipped_direct:
            click.echo(f"Skipped PyPI check for {len(skipped_direct)} direct reference(s).")

    payload_json = json.dumps({"packages": list(packages)})
    raw = _run_box_config_py(ctx, resolved, "pip-add", payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to add pip packages:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    added = payload.get("added") or list(packages)
    click.secho(f"Added {len(added)} pip package(s) on {resolved}: " + ", ".join(added), fg="green")
    click.echo("Run `lager box config apply` to install them in the container.")


@pip_group.command("remove", help="Remove one or more pip packages from the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def pip_remove_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "pip-remove", *packages)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to remove pip packages:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    removed = payload.get("removed") or []
    if removed:
        click.secho(f"Removed {len(removed)} pip package(s): " + ", ".join(removed), fg="green")
        click.echo("Run `lager box config apply` to update the running container.")
    else:
        click.secho("No matching pip packages were configured.", fg="yellow")


@pip_group.command(
    "import-legacy",
    help="Import packages from /etc/lager/user_requirements.txt into pip_packages.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def pip_import_legacy_cmd(ctx: click.Context, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "pip-import-legacy")
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to import legacy pip packages:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    imported = payload.get("imported") or []
    skipped = payload.get("skipped") or []
    if imported:
        click.secho(f"Imported {len(imported)} package(s): " + ", ".join(imported), fg="green")
        click.echo("Run `lager box config apply` to (re)install them.")
    else:
        click.secho("No new packages to import.", fg="yellow")
    if skipped:
        click.secho(f"Skipped {len(skipped)}:", fg="yellow")
        for s in skipped:
            pkg = s.get("package") if isinstance(s, dict) else s
            reason = s.get("reason") if isinstance(s, dict) else "unknown"
            click.secho(f"  - {pkg!r}: {reason}", fg="yellow")


# ---------------------------------------------------------------------------
# apt_packages: host-side Debian packages installed during `apply`
# ---------------------------------------------------------------------------

# Debian package name format. Mirror of validate_apt_format in
# box/lager/box_config/config.py — duplicated host-side so we can fail fast
# without an SSH round-trip.
import re as _re
_APT_NAME_RE = _re.compile(r'^[a-z0-9][a-z0-9+\-.]*$')


def _validate_apt_name_host(pkg: str) -> Optional[str]:
    if not isinstance(pkg, str) or not pkg.strip():
        return "package name cannot be empty"
    if not _APT_NAME_RE.match(pkg):
        return "invalid Debian package name (must match [a-z0-9][a-z0-9+-.]*)"
    return None


@box_config.group("apt", help="Manage host-side apt packages.")
def apt_group() -> None:
    pass


@apt_group.command("list", help="List configured apt packages.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def apt_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx) or {}
    pkgs = payload.get("apt_packages") or []
    if as_json:
        click.echo(json.dumps(pkgs, indent=2))
        return
    if not pkgs:
        click.echo("No apt packages configured.")
        return
    for p in pkgs:
        click.echo(p)


@apt_group.command("add", help="Add one or more apt packages to the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def apt_add_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    fmt_errors = []
    for p in packages:
        reason = _validate_apt_name_host(p)
        if reason:
            fmt_errors.append((p, reason))
    if fmt_errors:
        click.secho("Invalid apt package name(s):", fg="red", err=True)
        for p, r in fmt_errors:
            click.secho(f"  - {p!r}: {r}", fg="red", err=True)
        ctx.exit(1)

    payload_json = json.dumps({"packages": list(packages)})
    raw = _run_box_config_py(ctx, resolved, "apt-add", payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to add apt packages:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    added = payload.get("added") or list(packages)
    click.secho(f"Added {len(added)} apt package(s) on {resolved}: " + ", ".join(added), fg="green")
    click.echo("Run `lager box config apply` to install them on the box host.")


@apt_group.command("remove", help="Remove one or more apt packages from the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def apt_remove_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "apt-remove", *packages)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to remove apt packages:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    removed = payload.get("removed") or []
    if removed:
        click.secho(f"Removed {len(removed)} apt package(s): " + ", ".join(removed), fg="green")
        click.echo(
            "Run `lager box config apply` to record the change. Note: existing "
            "installs are not auto-uninstalled."
        )
    else:
        click.secho("No matching apt packages were configured.", fg="yellow")


# ---------------------------------------------------------------------------
# sysctl: host-side kernel parameters persisted across reboot
# ---------------------------------------------------------------------------

_SYSCTL_KEY_RE_HOST = _re.compile(r'^[a-zA-Z][a-zA-Z0-9_.]*$')


def _validate_sysctl_key_host(key: str) -> Optional[str]:
    if not isinstance(key, str) or not key.strip():
        return "sysctl key cannot be empty"
    if not _SYSCTL_KEY_RE_HOST.match(key):
        return "invalid sysctl key (must match [a-zA-Z][a-zA-Z0-9_.]*)"
    return None


@box_config.group("sysctl", help="Manage host sysctl values persisted across reboots.")
def sysctl_group() -> None:
    pass


@sysctl_group.command("list", help="List configured sysctl values.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def sysctl_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx) or {}
    sysctl = payload.get("sysctl") or {}
    if as_json:
        click.echo(json.dumps(sysctl, indent=2))
        return
    if not sysctl:
        click.echo("No sysctl values configured.")
        return
    for k, v in sysctl.items():
        click.echo(f"{k} = {v}")


@sysctl_group.command(
    "set",
    help="Set one or more sysctl key=value pairs. Upsert; safe to re-run.",
)
@click.argument("entries", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def sysctl_set_cmd(ctx: click.Context, entries: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    parsed: dict = {}
    fmt_errors = []
    for entry in entries:
        if "=" not in entry:
            fmt_errors.append((entry, "expected key=value"))
            continue
        key, value = entry.split("=", 1)
        reason = _validate_sysctl_key_host(key)
        if reason:
            fmt_errors.append((entry, reason))
            continue
        parsed[key] = value
    if fmt_errors:
        click.secho("Invalid sysctl entries:", fg="red", err=True)
        for e, r in fmt_errors:
            click.secho(f"  - {e!r}: {r}", fg="red", err=True)
        ctx.exit(1)

    payload_json = json.dumps({"entries": parsed})
    raw = _run_box_config_py(ctx, resolved, "sysctl-set", payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to set sysctl values:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    set_keys = payload.get("set") or list(parsed.keys())
    click.secho(
        f"Set {len(set_keys)} sysctl key(s) on {resolved}: " + ", ".join(set_keys),
        fg="green",
    )
    click.echo("Run `lager box config apply` to write /etc/sysctl.d/ and reload.")


@sysctl_group.command("unset", help="Remove one or more sysctl keys from the box config.")
@click.argument("keys", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def sysctl_unset_cmd(ctx: click.Context, keys: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "sysctl-unset", *keys)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to unset sysctl values:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    removed = payload.get("removed") or []
    if removed:
        click.secho(f"Removed {len(removed)} sysctl key(s): " + ", ".join(removed), fg="green")
        click.echo("Run `lager box config apply` to update /etc/sysctl.d/ and reload.")
    else:
        click.secho("No matching sysctl keys were configured.", fg="yellow")


# ---------------------------------------------------------------------------
# cargo_packages: in-container Rust crates installed during container start
# ---------------------------------------------------------------------------

_CARGO_SPEC_RE_HOST = _re.compile(r'^[a-z0-9][a-z0-9_\-]*(?:@[a-zA-Z0-9.+\-]+)?$')


def _validate_cargo_spec_host(pkg: str) -> Optional[str]:
    if not isinstance(pkg, str) or not pkg.strip():
        return "package name cannot be empty"
    if not _CARGO_SPEC_RE_HOST.match(pkg):
        return "invalid cargo crate spec (must match [a-z0-9][a-z0-9_-]*(@version)?)"
    return None


@box_config.group("cargo", help="Manage in-container cargo crates.")
def cargo_group() -> None:
    pass


@cargo_group.command("list", help="List configured cargo crates.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def cargo_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "show")
    payload = _parse_response(raw, ctx) or {}
    pkgs = payload.get("cargo_packages") or []
    if as_json:
        click.echo(json.dumps(pkgs, indent=2))
        return
    if not pkgs:
        click.echo("No cargo crates configured.")
        return
    for p in pkgs:
        click.echo(p)


@cargo_group.command("add", help="Add one or more cargo crates to the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def cargo_add_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    fmt_errors = []
    for p in packages:
        reason = _validate_cargo_spec_host(p)
        if reason:
            fmt_errors.append((p, reason))
    if fmt_errors:
        click.secho("Invalid cargo crate spec(s):", fg="red", err=True)
        for p, r in fmt_errors:
            click.secho(f"  - {p!r}: {r}", fg="red", err=True)
        ctx.exit(1)

    payload_json = json.dumps({"packages": list(packages)})
    raw = _run_box_config_py(ctx, resolved, "cargo-add", payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to add cargo crates:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    added = payload.get("added") or list(packages)
    click.secho(f"Added {len(added)} cargo crate(s) on {resolved}: " + ", ".join(added), fg="green")
    click.echo("Run `lager box config apply` to install them in the container.")


@cargo_group.command("remove", help="Remove one or more cargo crates from the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def cargo_remove_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, "cargo-remove", *packages)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to remove cargo crates:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    removed = payload.get("removed") or []
    if removed:
        click.secho(f"Removed {len(removed)} cargo crate(s): " + ", ".join(removed), fg="green")
        click.echo(
            "Run `lager box config apply` to record the change. Note: existing "
            "installs in the container are not auto-uninstalled."
        )
    else:
        click.secho("No matching cargo crates were configured.", fg="yellow")

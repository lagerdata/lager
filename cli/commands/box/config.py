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
import requests

from ...box_storage import get_box_ip, list_boxes
from ...context import get_default_box, get_impl_path
from ..development.python import run_python_internal_get_output
from . import _shim_verbs as verbs
from ._host_ops import apt_install, sysctl_apply
from ._mount_prep import ensure_host_path_owned, manual_fix_command
from ._pip_validation import is_direct_ref, validate_on_pypi
from ._ssh import default_ssh_runner

# How long we'll wait for the box's HTTP API to come up after start_box.sh
# returns 0. The container itself starts in ~3-5s on a healthy box; the
# generous cap covers slow boxes and pip-install steps that run inline.
_API_READY_DEADLINE_SECONDS = 30
_API_READY_POLL_INTERVAL_SECONDS = 1
_BOX_API_PORT = 5000


def _resolve_boxes(ctx: click.Context, box_opt: Optional[str]) -> list:
    """Resolve `--box` to a list of IPs. Comma-separated values fan out
    across boxes for the commands that support it (show / apply / status).
    A bare value behaves identically to `_resolve_box`."""
    if box_opt and "," in box_opt:
        names = [b.strip() for b in box_opt.split(",") if b.strip()]
        if not names:
            click.secho("Error: --box value is empty after splitting on commas.", fg="red", err=True)
            ctx.exit(1)
        return [_resolve_box(ctx, n) for n in names]
    return [_resolve_box(ctx, box_opt)]


def _resolve_box(ctx: click.Context, box_opt: Optional[str] = None) -> str:
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


def _list_field(
    ctx: click.Context,
    box: Optional[str],
    *,
    key: str,
    empty_msg: str,
    formatter,
    as_json: bool,
) -> None:
    """Resolve box, fetch `show`, pluck `key`, print formatted entries.

    Shared by every `lager box config <group> list` command. List-valued
    fields (mounts/volumes/pip_packages/...) iterate items; the one dict
    field (sysctl) iterates key/value pairs — the formatter takes a tuple
    in that case, mirroring the diff printer's convention.
    """
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, verbs.SHOW)
    payload = _parse_response(raw, ctx) or {}
    items = payload.get(key)
    if items is None:
        items = {} if key == "sysctl" else []
    if as_json:
        click.echo(json.dumps(items, indent=2))
        return
    if not items:
        click.echo(empty_msg)
        return
    if isinstance(items, dict):
        for k, v in items.items():
            click.echo(formatter((k, v)))
    else:
        for item in items:
            click.echo(formatter(item))


@click.group(name="config", invoke_without_command=True, help="Manage declarative box provisioning")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def box_config(ctx: click.Context, box: Optional[str]) -> None:
    if ctx.invoked_subcommand is None:
        ctx.invoke(show_cmd, box=box, as_json=False)


@box_config.command("show", help="Print the current box_config.json.")
@click.option("--box", help="Lagerbox name or IP (comma-separated for fanout)")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def show_cmd(
    ctx: click.Context,
    box: Optional[str],
    as_json: bool,
) -> None:
    from ...box_storage import get_box_name_by_ip

    targets = _resolve_boxes(ctx, box)
    if as_json and len(targets) > 1:
        out = {}
        for resolved in targets:
            raw = _run_box_config_py(ctx, resolved, verbs.SHOW)
            out[resolved] = _parse_response(raw, ctx)
        click.echo(json.dumps(out, indent=2))
        return
    for i, resolved in enumerate(targets):
        raw = _run_box_config_py(ctx, resolved, verbs.SHOW)
        payload = _parse_response(raw, ctx)
        if payload is None:
            if i > 0:
                click.echo()
            click.secho(
                f"No box_config.json on {resolved}. Run `lager box config init`.",
                fg="yellow",
            )
            continue
        if as_json:
            click.echo(json.dumps(payload, indent=2))
            continue
        # Resolve a display label: prefer the box's stored name; fall back
        # to the resolved IP. The original --box arg might be "<BOX>,<BOX>";
        # we already split + resolved per-box.
        name = get_box_name_by_ip(resolved)
        label = name or resolved
        # clean/DRIFT marker: two extra shim round-trips, but ~50ms each
        # and high signal value. Skip the marker if either call returns
        # None (means we can't compute "clean" reliably).
        cur_hash = _parse_response(
            _run_box_config_py(ctx, resolved, verbs.HASH), ctx,
        ).get("hash")
        applied_hash = _parse_response(
            _run_box_config_py(ctx, resolved, verbs.APPLIED_HASH), ctx,
        ).get("hash")
        clean = None
        if cur_hash is not None:
            clean = cur_hash == applied_hash
        if i > 0:
            click.echo()
        _render_human(payload, box_label=label, clean_state=clean)


def _fmt_mount(m: dict) -> str:
    ro = " (ro)" if m.get("readonly") else ""
    return f"{m.get('host', '?')} -> {m.get('container', '?')}{ro}"


def _fmt_volume(v: dict) -> str:
    return f"{v.get('name', '?')} -> {v.get('container', '?')}"


# Fields grouped by where they take effect — host OS vs in-container.
# `show`'s human renderer iterates this for the two-section layout; the
# flat `_FIRST_CLASS_FIELDS` is derived for the diff renderer (which is
# field-agnostic). Order within each group matches typical operator
# scanning: package-managers first, then "what's mounted/configured."
_FIRST_CLASS_FIELDS_GROUPED = [
    ("Host", [
        ("apt_packages",   "Apt packages",          str),
        ("sysctl",         "Sysctl settings",       lambda kv: f"{kv[0]} = {kv[1]}"),
        ("mounts",         "Mounts",                _fmt_mount),
    ]),
    ("Container", [
        ("volumes",        "Volumes",               _fmt_volume),
        ("env",            "Environment variables", lambda kv: f"{kv[0]}={kv[1]}"),
        ("pip_packages",   "Pip packages",          str),
        ("cargo_packages", "Cargo packages",        str),
        ("npm_packages",   "Npm packages",          str),
    ]),
]
_FIRST_CLASS_FIELDS = [field for _, fields in _FIRST_CLASS_FIELDS_GROUPED for field in fields]
_FIRST_CLASS_KEYS = frozenset(["version"] + [f[0] for f in _FIRST_CLASS_FIELDS])


def _diff_list(cur: list, prev: list) -> dict:
    cur_set, prev_set = set(cur), set(prev)
    return {"added": sorted(cur_set - prev_set), "removed": sorted(prev_set - cur_set)}


def _diff_dict(cur: dict, prev: dict) -> dict:
    cur_keys, prev_keys = set(cur), set(prev)
    return {
        "added": {k: cur[k] for k in sorted(cur_keys - prev_keys)},
        "removed": {k: prev[k] for k in sorted(prev_keys - cur_keys)},
        "changed": {
            k: {"from": prev[k], "to": cur[k]}
            for k in sorted(cur_keys & prev_keys) if cur[k] != prev[k]
        },
    }


def _diff_keyed_list(cur: list, prev: list, *, key: str) -> dict:
    def _idx(items):
        return {m[key]: m for m in items if isinstance(m, dict) and isinstance(m.get(key), str)}

    cur_idx, prev_idx = _idx(cur), _idx(prev)
    added = sorted(set(cur_idx) - set(prev_idx))
    removed = sorted(set(prev_idx) - set(cur_idx))
    changed = sorted(k for k in set(cur_idx) & set(prev_idx) if cur_idx[k] != prev_idx[k])
    return {
        "added": [cur_idx[k] for k in added],
        "removed": [prev_idx[k] for k in removed],
        "changed": [{"from": prev_idx[k], "to": cur_idx[k]} for k in changed],
    }


def _compute_diff(current: dict, applied: Optional[dict]) -> dict:
    """Per-field diff of the current config against the last-applied snapshot.

    Mounts diff by container path (the validator's dedupe key) so an upsert
    is a single `changed` entry, not paired add+remove. Volumes by name.
    Env/sysctl as flat key→value dicts. Pip/apt/cargo/npm as sets of strings.
    Missing snapshot is treated as empty: everything in `current` is `added`.
    """
    prev = applied or {}
    return {
        "mounts":         _diff_keyed_list(current.get("mounts") or [],         prev.get("mounts") or [],         key="container"),
        "volumes":        _diff_keyed_list(current.get("volumes") or [],        prev.get("volumes") or [],        key="name"),
        "env":            _diff_dict     (current.get("env") or {},             prev.get("env") or {}),
        "sysctl":         _diff_dict     (current.get("sysctl") or {},          prev.get("sysctl") or {}),
        "pip_packages":   _diff_list     (current.get("pip_packages") or [],    prev.get("pip_packages") or []),
        "apt_packages":   _diff_list     (current.get("apt_packages") or [],    prev.get("apt_packages") or []),
        "cargo_packages": _diff_list     (current.get("cargo_packages") or [],  prev.get("cargo_packages") or []),
        "npm_packages":   _diff_list     (current.get("npm_packages") or [],    prev.get("npm_packages") or []),
    }


def _diff_is_empty(diff: dict) -> bool:
    return not any(any(parts.values()) for parts in diff.values())


def _print_diff_human(diff: dict) -> None:
    # Single source of truth: derive labels + formatters from the
    # grouped registry so renames don't have to land in two places.
    field_labels = {key: label for key, label, _ in _FIRST_CLASS_FIELDS}
    field_formatters = {key: fmt for key, _, fmt in _FIRST_CLASS_FIELDS}
    for field, parts in diff.items():
        added = parts.get("added") or []
        removed = parts.get("removed") or []
        changed = parts.get("changed") or []
        if not (added or removed or changed):
            continue
        fmt = field_formatters[field]
        click.secho(f"{field_labels[field]}:", bold=True)
        # dicts (env / sysctl): added/removed are dicts; iterate items.
        if isinstance(added, dict):
            for k, v in added.items():
                click.secho(f"  + {fmt((k, v))}", fg="green")
            for k, v in (removed or {}).items():
                click.secho(f"  - {fmt((k, v))}", fg="red")
            for k, c in (changed or {}).items():
                click.secho(f"  ~ {k}: {c['from']} -> {c['to']}", fg="yellow")
        else:
            for item in added:
                click.secho(f"  + {fmt(item)}", fg="green")
            for item in removed:
                click.secho(f"  - {fmt(item)}", fg="red")
            for c in changed:
                click.secho(f"  ~ {fmt(c['from'])} -> {fmt(c['to'])}", fg="yellow")
        click.echo()


def _render_human(
    payload: dict,
    *,
    box_label: Optional[str] = None,
    clean_state: Optional[bool] = None,
) -> None:
    """Pretty-print box_config in a nets-derived layout:
    - title with horizontal rule (matches nets's column-header `===` rule)
    - HOST and CONTAINER group headings, bold uppercase, underlined
    - within each group: bold section labels with `├── /└── ` branches
      under each (matches nets's `instrument [addr]` + branches shape)
    - empty sections still render with a `(none)` leaf so operators see
      what's available to add

    box_label: name/IP for the header (None → bare "Box config").
    clean_state: True → green "clean", False → yellow "DRIFT", None → omitted.
    """
    # Header — matches nets's listing intro (title + horizontal rule).
    header_left = f"Box config: {box_label}" if box_label else "Box config"
    status = None
    if clean_state is True:
        status = click.style("[Up To Date]", fg="green")
    elif clean_state is False:
        status = click.style("[Unapplied Changes!]", fg="yellow")
    header_line = f"{header_left}  {status}" if status else header_left
    click.secho(header_line, bold=True)
    import re as _re_local
    visible_len = len(_re_local.sub(r"\033\[[0-9;]*m", "", header_line))
    click.echo("─" * visible_len)

    # Linearize into blocks (group headers + sections), each separated by
    # a single blank line. Uniform spacing across the entire listing.
    blocks = []
    for group_label, sections in _FIRST_CLASS_FIELDS_GROUPED:
        blocks.append(("group", group_label))
        for key, label, fmt in sections:
            items = payload.get(key)
            if items is None:
                items = {} if key in ("env", "sysctl") else []
            blocks.append(("section", key, label, fmt, items))

    # 2-space indent under group headers so section membership is visually
    # obvious. Group headers themselves sit flush-left.
    SECTION_INDENT = "  "
    for i, block in enumerate(blocks):
        click.echo()  # blank line before every block (including the first
                      # after the rule)
        if block[0] == "group":
            upper = block[1].upper()
            click.secho(upper, bold=True)
            click.echo("─" * len(upper))
        else:
            _, key, label, fmt, items = block
            click.secho(f"{SECTION_INDENT}{label}", bold=True)
            entries = _format_tree_entries(key, items, fmt) if items else ["(none)"]
            for ei, entry in enumerate(entries):
                is_last = ei == len(entries) - 1
                branch = "└── " if is_last else "├── "
                click.echo(f"{SECTION_INDENT}{branch}{entry}")

    extras = {k: v for k, v in payload.items() if k not in _FIRST_CLASS_KEYS}
    if extras:
        click.echo()
        click.secho("Extras (round-tripped, not yet applied)", bold=True)
        for k in extras:
            click.echo(f"  {k}")


def _format_tree_entries(key: str, items, fmt) -> list:
    """Format entries for one section into a list of strings with arrow /
    equals column alignment inside the section. Returned strings have no
    leading whitespace — `_render_human` prepends tree-prefix per entry."""
    if isinstance(items, dict):
        max_key = max((len(str(k)) for k in items), default=0)
        sep = " = "
        return [f"{str(k):<{max_key}}{sep}{v}" for k, v in items.items()]
    if key == "mounts":
        max_host = max(
            (len(m.get("host", "")) for m in items if isinstance(m, dict)),
            default=0,
        )
        out = []
        for m in items:
            if not isinstance(m, dict):
                out.append(str(m))
                continue
            host = m.get("host", "?")
            container = m.get("container", "?")
            ro = " (ro)" if m.get("readonly") else ""
            out.append(f"{host:<{max_host}} -> {container}{ro}")
        return out
    if key == "volumes":
        max_name = max(
            (len(v.get("name", "")) for v in items if isinstance(v, dict)),
            default=0,
        )
        out = []
        for v in items:
            if not isinstance(v, dict):
                out.append(str(v))
                continue
            name = v.get("name", "?")
            container = v.get("container", "?")
            out.append(f"{name:<{max_name}} -> {container}")
        return out
    return [fmt(item) for item in items]


@box_config.command("init", help="Create /etc/lager/box_config.json with defaults.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--force", is_flag=True, help="Overwrite if file already exists")
@click.pass_context
def init_cmd(ctx: click.Context, box: Optional[str], force: bool) -> None:
    resolved = _resolve_box(ctx, box)
    args = [verbs.INIT]
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
    raw = _run_box_config_py(ctx, resolved, verbs.VALIDATE)
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


_SINCE_RE = __import__("re").compile(r"^\s*(\d+)\s*([smhdw])\s*$")


def _parse_since(since_str: str):
    """Parse a relative time like `1h`, `30m`, `1d`, `2w` to a UTC cutoff
    datetime. Units: s=second, m=minute, h=hour, d=day, w=week."""
    import datetime as _dt
    m = _SINCE_RE.match(since_str)
    if not m:
        raise click.BadParameter(
            f"unsupported --since format: {since_str!r}; expected e.g. `30m`, `1h`, `2d`, `1w`.",
            param_hint="--since",
        )
    n = int(m.group(1))
    unit = m.group(2)
    secs = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit] * n
    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=secs)


def _entry_timestamp(entry: dict):
    import datetime as _dt
    ts = entry.get("ts")
    if not isinstance(ts, str) or not ts:
        return None
    # fromisoformat in stdlib doesn't accept the literal `Z` suffix on
    # versions < 3.11; normalize it to a UTC offset so this works
    # consistently across boxes that may run different Python versions.
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return _dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


@box_config.command(
    "audit",
    help="Show recent box_config mutations recorded on the box.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.option(
    "--tail", "tail_n", type=int, default=20,
    help="Number of most-recent entries to fetch from the box (default 20; 0 for all).",
)
@click.option(
    "--since", "since",
    help="Show only entries newer than the given duration (e.g., `30m`, `1h`, `2d`, `1w`).",
)
@click.option(
    "--verb", "verb_filter",
    help="Show only entries matching this verb exactly (e.g., `apt-add`, `set-applied-hash`).",
)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def audit_cmd(
    ctx: click.Context,
    box: Optional[str],
    tail_n: int,
    since: Optional[str],
    verb_filter: Optional[str],
    as_json: bool,
) -> None:
    resolved = _resolve_box(ctx, box)
    # Fetch the tail from the box, then filter host-side. The audit log is
    # small enough that doing this client-side is cheaper than expanding
    # the shim's wire protocol for every new filter dimension.
    raw = _run_box_config_py(ctx, resolved, verbs.AUDIT_TAIL, str(tail_n))
    payload = _parse_response(raw, ctx) or {}
    entries = payload.get("entries", [])
    if since:
        cutoff = _parse_since(since)
        entries = [
            e for e in entries
            if (t := _entry_timestamp(e)) is not None and t >= cutoff
        ]
    if verb_filter:
        entries = [e for e in entries if e.get("verb") == verb_filter]
    if as_json:
        click.echo(json.dumps(entries, indent=2))
        return
    if not entries:
        click.echo("No audit entries." if not (since or verb_filter)
                   else "No matching audit entries.")
        return
    for e in entries:
        ts = e.get("ts", "?")
        verb = e.get("verb", "?")
        args = e.get("args", {})
        click.echo(f"{ts}  {verb:<18}  {json.dumps(args, separators=(',', ':'))}")


@box_config.command(
    "diff",
    help="Show pending changes vs. the last applied config.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def diff_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    resolved = _resolve_box(ctx, box)
    current = _parse_response(_run_box_config_py(ctx, resolved, verbs.SHOW), ctx) or {}
    applied = _parse_response(_run_box_config_py(ctx, resolved, verbs.APPLIED_SHOW), ctx)
    diff = _compute_diff(current, applied)
    if as_json:
        click.echo(json.dumps(diff, indent=2))
        return
    if _diff_is_empty(diff):
        click.echo("No pending changes since last apply.")
        return
    if applied is None:
        click.secho(
            "No applied snapshot on this box — showing the full current config "
            "as pending additions.",
            fg="blue",
        )
    _print_diff_human(diff)


@box_config.command("status", help="One-line summary of box config state.")
@click.option("--box", help="Lagerbox name or IP (comma-separated for fanout)")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def status_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    """Quick health check: clean vs. drifted, field counts, last audit entry.

    Designed for `lager box config status --box <BOX>,<BOX>,<BOX>` —
    one line of signal per box without needing to read full `show` output.
    """
    targets = _resolve_boxes(ctx, box)
    summaries = []
    for resolved in targets:
        summaries.append(_status_one(ctx, resolved))
    if as_json:
        out = summaries[0] if len(summaries) == 1 else summaries
        click.echo(json.dumps(out, indent=2))
        return
    for s in summaries:
        _print_status_human(s)


def _status_one(ctx: click.Context, resolved: str) -> dict:
    show = _parse_response(_run_box_config_py(ctx, resolved, verbs.SHOW), ctx)
    if show is None:
        return {"box": resolved, "exists": False}
    cur_hash = _parse_response(_run_box_config_py(ctx, resolved, verbs.HASH), ctx).get("hash")
    applied_hash = _parse_response(_run_box_config_py(ctx, resolved, verbs.APPLIED_HASH), ctx).get("hash")
    audit = _parse_response(_run_box_config_py(ctx, resolved, verbs.AUDIT_TAIL, "1"), ctx) or {}
    entries = audit.get("entries", [])
    last = entries[0] if entries else None
    clean = bool(cur_hash) and cur_hash == applied_hash
    counts = {key: len(show.get(key) or []) for key, _, _ in _FIRST_CLASS_FIELDS}
    return {
        "box": resolved, "exists": True, "clean": clean,
        "current_hash": cur_hash, "applied_hash": applied_hash,
        "counts": counts, "last_change": last,
    }


def _print_status_human(s: dict) -> None:
    box = s["box"]
    if not s.get("exists"):
        click.echo(f"{box}: no box_config.json")
        return
    state = click.style("clean", fg="green") if s["clean"] else click.style("DRIFT", fg="yellow")
    click.echo(f"{box}: {state}")
    if not s["clean"]:
        click.echo(f"  run `lager box config diff --box {box}` to see pending changes")
    nonzero = {k: n for k, n in s["counts"].items() if n}
    if nonzero:
        # Compact rendering: "2 mounts, 3 pip, 1 cargo"
        parts = []
        for key, n in nonzero.items():
            label = key.replace("_packages", "").replace("_", " ")
            parts.append(f"{n} {label}")
        click.echo(f"  field counts: {', '.join(parts)}")
    if s["last_change"]:
        ts = s["last_change"].get("ts", "?")
        verb = s["last_change"].get("verb", "?")
        click.echo(f"  last change: {ts} ({verb})")


@box_config.command("export", help="Write the box's config to a local JSON file.")
@click.argument("path", type=click.Path(dir_okay=False, writable=True))
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def export_cmd(ctx: click.Context, path: str, box: Optional[str]) -> None:
    """Save box_config.json from the box to a local file. Pair with
    `import` for gitops workflows — check the file into a repo, edit, then
    push back with `import`."""
    resolved = _resolve_box(ctx, box)
    payload = _parse_response(_run_box_config_py(ctx, resolved, verbs.SHOW), ctx)
    if payload is None:
        click.secho(f"No box_config.json on {resolved}.", fg="yellow", err=True)
        ctx.exit(1)
    body = json.dumps(payload, indent=2) + "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    click.secho(f"Wrote {path} ({len(body)} bytes from {resolved}).", fg="green")


@box_config.command("import", help="Replace the box's config with a local JSON file.")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option("--box", help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def import_cmd(ctx: click.Context, path: str, box: Optional[str], yes: bool) -> None:
    """Replace /etc/lager/box_config.json on the box with the local file's
    contents. The shim validates before writing; on validation failure the
    on-disk file is untouched."""
    resolved = _resolve_box(ctx, box)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        json.loads(content)  # local syntax check; better error before SSH round-trip
    except json.JSONDecodeError as e:
        click.secho(f"Invalid JSON in {path}: {e}", fg="red", err=True)
        ctx.exit(1)
    if not yes and not click.confirm(
        f"Replace box_config.json on {resolved} with {path}?", default=False,
    ):
        click.secho("Aborted.", fg="yellow")
        return
    response = _parse_response(
        _run_box_config_py(ctx, resolved, verbs.SET_RAW, content), ctx,
    )
    if not response.get("ok"):
        click.secho("Failed to import config:", fg="red", err=True)
        _print_errors(response.get("errors") or [response.get("error", "unknown error")])
        ctx.exit(1)
    click.secho(f"Imported {path} to {resolved}.", fg="green")
    click.echo("Run `lager box config apply` to put the new config into effect.")


@box_config.command("edit", help="Open the box's config in $EDITOR for live editing.")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def edit_cmd(ctx: click.Context, box: Optional[str]) -> None:
    """Round-trip the config through $EDITOR. On save, the shim validates;
    on failure the user is re-prompted with errors and the editor is
    reopened so edits are not lost. Aborting the editor (or refusing to
    retry on errors) leaves the on-disk config unchanged.
    """
    import os
    import shutil
    import subprocess
    import tempfile

    resolved = _resolve_box(ctx, box)
    payload = _parse_response(_run_box_config_py(ctx, resolved, verbs.SHOW), ctx) or {}
    # $EDITOR / $VISUAL win when set. Otherwise prefer nano (modeless, no
    # arcane modal commands required) when it's on PATH, falling back to
    # vi which is universally available on POSIX boxes.
    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or ("nano" if shutil.which("nano") else "vi")
    )

    body = json.dumps(payload, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="lager-box-config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        original_body = body
        while True:
            rc = subprocess.call([editor, tmp_path])
            with open(tmp_path, "r", encoding="utf-8") as f:
                new_body = f.read()
            # Don't trust the editor's exit code — vim plugins, swap-file
            # warnings, and odd modal exits can return non-zero even when
            # :wq succeeded. The real signal is whether the file content
            # changed.
            if new_body == original_body:
                if rc != 0:
                    click.secho(
                        f"Editor exited with rc={rc} and no changes were saved.",
                        fg="yellow", err=True,
                    )
                else:
                    click.secho("No changes saved.", fg="yellow")
                ctx.exit(rc or 0)
            body = new_body
            try:
                json.loads(body)
            except json.JSONDecodeError as e:
                click.secho(f"Invalid JSON: {e}", fg="red", err=True)
                if not click.confirm("Re-open editor?", default=True):
                    click.secho("Aborted; on-disk config unchanged.", fg="yellow")
                    ctx.exit(1)
                continue
            response = _parse_response(
                _run_box_config_py(ctx, resolved, verbs.SET_RAW, body), ctx,
            )
            if response.get("ok"):
                click.secho(f"Saved on {resolved}.", fg="green")
                click.echo("Run `lager box config apply` to put the new config into effect.")
                return
            click.secho("Config has errors:", fg="red", err=True)
            _print_errors(response.get("errors") or [])
            if not click.confirm("Re-open editor?", default=True):
                click.secho("Aborted; on-disk config unchanged.", fg="yellow")
                ctx.exit(1)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@box_config.command("copy", help="Copy a box's config to another box.")
@click.option("--from", "src", required=True, metavar="BOX",
              help="Source box name or IP")
@click.option("--to", "dst", required=True, metavar="BOX",
              help="Destination box name or IP")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def copy_cmd(ctx: click.Context, src: str, dst: str, yes: bool) -> None:
    """Clone config from source to destination box. Useful for fleet
    bring-up: stand up one box, perfect its config, replicate to the rest.
    Does NOT copy applied-hash or the rollback snapshot — destination
    rebuilds those on its own next `apply`.
    """
    src_resolved = _resolve_box(ctx, src)
    dst_resolved = _resolve_box(ctx, dst)
    if src_resolved == dst_resolved:
        click.secho(
            f"Source and destination resolve to the same box ({src_resolved}); refusing.",
            fg="red", err=True,
        )
        ctx.exit(1)
    payload = _parse_response(_run_box_config_py(ctx, src_resolved, verbs.SHOW), ctx)
    if payload is None:
        click.secho(f"No box_config.json on {src_resolved}.", fg="red", err=True)
        ctx.exit(1)
    if not yes and not click.confirm(
        f"Copy config from {src_resolved} to {dst_resolved} (overwrites destination)?",
        default=False,
    ):
        click.secho("Aborted.", fg="yellow")
        return
    body = json.dumps(payload)
    response = _parse_response(
        _run_box_config_py(ctx, dst_resolved, verbs.SET_RAW, body), ctx,
    )
    if not response.get("ok"):
        click.secho("Failed to copy config:", fg="red", err=True)
        _print_errors(response.get("errors") or [response.get("error", "unknown error")])
        ctx.exit(1)
    click.secho(
        f"Copied config from {src_resolved} to {dst_resolved}.", fg="green",
    )
    click.echo(f"Run `lager box config apply --box {dst}` to put the new config into effect.")


@box_config.command(
    "repair",
    help="Restore box_config.json from the last applied snapshot and bounce.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def repair_cmd(ctx: click.Context, box: Optional[str], yes: bool) -> None:
    """Recover a box whose box_config.json is broken or whose container
    is wedged on a bad config.

    Does what `lager box config apply`'s rollback path does automatically
    when a bounce fails — but exposed as a manual verb for situations
    that don't trigger automatic rollback:
    - Operator hand-edited /etc/lager/box_config.json into invalid JSON
      (bypassing `lager box config edit`'s validation)
    - First-apply bounce failed AND there's no snapshot yet (rollback
      can't fire) — in that case this command also exits with no-snapshot
    - Container is up but the renderer soft-failed on a stale config and
      the operator wants to revert to last-known-good without an apply

    Uses SSH `sudo cp` (via the cp clause in the sudoers rule installed by
    `lager install`/`lager update`), so it works even when the in-container
    shim is unreachable.
    """
    resolved = _resolve_box(ctx, box)

    # Confirm snapshot exists before announcing repair.
    rc, _stdout, _stderr = default_ssh_runner(
        resolved,
        "test -f /etc/lager/box_config.applied.json",
    )
    if rc != 0:
        click.secho(
            f"No applied snapshot on {resolved}; cannot repair. This box has "
            "never had a successful `lager box config apply`. Manual fix: ssh "
            "in, delete or hand-edit /etc/lager/box_config.json, then re-run "
            "`lager box config init`.",
            fg="red", err=True,
        )
        ctx.exit(1)

    if not yes and not click.confirm(
        f"Restore /etc/lager/box_config.json from the applied snapshot on "
        f"{resolved} and bounce?",
        default=False,
    ):
        click.secho("Aborted.", fg="yellow")
        return

    rc, _stdout, stderr = default_ssh_runner(
        resolved,
        "sudo -n cp /etc/lager/box_config.applied.json /etc/lager/box_config.json",
    )
    if rc != 0:
        click.secho(
            f"Failed to restore snapshot: {(stderr or '').strip()}",
            fg="red", err=True,
        )
        # Specific hint for the most likely cause.
        if "password is required" in (stderr or "").lower() or "sudo:" in (stderr or "").lower():
            click.secho(
                "Hint: the box's sudoers rule may not have the cp clause yet. "
                "Run `lager update --box X` to upgrade the sudoers bootstrap, "
                "then retry repair.",
                fg="yellow", err=True,
            )
        ctx.exit(1)
    click.secho(f"Snapshot restored on {resolved}.", fg="green")

    if not _bounce_container(ctx, resolved):
        click.secho(
            f"Snapshot restored but container failed to start on {resolved}. "
            "SSH in and check `docker logs lager` and `docker ps -a`.",
            fg="red", err=True,
        )
        ctx.exit(1)

    if not _wait_for_box_api(resolved):
        click.secho(
            f"Container started but the box API didn't respond within "
            f"{_API_READY_DEADLINE_SECONDS}s. Check `lager hello --box {box or resolved}` "
            "and the container logs.",
            fg="yellow", err=True,
        )
        ctx.exit(1)

    click.secho(f"Repair complete on {resolved}.", fg="green", bold=True)


@box_config.command(
    "apply",
    help="Validate, then bounce the container so the new config takes effect.",
)
@click.option("--box", help="Lagerbox name or IP (comma-separated for fanout)")
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
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Validate and report pending changes, but make no SSH writes — no "
        "mount auto-prep, no apt/sysctl, no bounce. Useful to preview what "
        "`apply` would do."
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
    dry_run: bool,
) -> None:
    targets = _resolve_boxes(ctx, box)
    failed = []
    for i, resolved in enumerate(targets):
        if len(targets) > 1:
            if i > 0:
                click.echo()
            click.secho(f"=== {resolved} ===", bold=True)
        ok = _apply_one(
            ctx, resolved,
            yes=yes, force=force, skip_restart=skip_restart,
            no_auto_prep=no_auto_prep, recursive_chown=recursive_chown,
            dry_run=dry_run,
        )
        if not ok:
            failed.append(resolved)
    if failed:
        if len(targets) > 1:
            click.secho(
                f"\nApply failed on {len(failed)}/{len(targets)} box(es): "
                + ", ".join(failed),
                fg="red", err=True,
            )
        ctx.exit(1)


def _apply_one(
    ctx: click.Context,
    resolved: str,
    *,
    yes: bool,
    force: bool,
    skip_restart: bool,
    no_auto_prep: bool,
    recursive_chown: bool,
    dry_run: bool,
) -> bool:
    """Apply box config to a single resolved box. Returns True on success.

    Returns False (rather than ctx.exit(1)) so the caller can iterate over
    multiple targets and continue past a failure on one box, reporting the
    aggregate at the end instead of bailing on the first error.
    """
    raw = _run_box_config_py(ctx, resolved, verbs.VALIDATE)
    payload = _parse_response(raw, ctx)
    if not payload.get("exists", True):
        click.secho(
            f"No box_config.json on {resolved}. Run `lager box config init` first.",
            fg="yellow",
        )
        return False
    if not payload.get("ok"):
        click.secho("Refusing to apply: config has errors:", fg="red", err=True)
        _print_errors(payload.get("errors") or [])
        return False

    raw = _run_box_config_py(ctx, resolved, verbs.HASH)
    cur_hash = _parse_response(raw, ctx).get("hash")
    raw = _run_box_config_py(ctx, resolved, verbs.APPLIED_HASH)
    applied_hash = _parse_response(raw, ctx).get("hash")

    unchanged = cur_hash and applied_hash and cur_hash == applied_hash

    if dry_run:
        # Read-only preview: no preflight (which mkdirs/chowns), no apt/sysctl,
        # no bounce, no set-applied-hash. Same `show`/`applied-show` round-trips
        # the `diff` command uses.
        current = _parse_response(_run_box_config_py(ctx, resolved, verbs.SHOW), ctx) or {}
        applied = _parse_response(_run_box_config_py(ctx, resolved, verbs.APPLIED_SHOW), ctx)
        diff = _compute_diff(current, applied)
        if _diff_is_empty(diff):
            click.secho(
                "Config unchanged since last apply; apply would be a no-op.",
                fg="green",
            )
            return True
        click.secho(
            "Dry run — no changes made. apply would perform:",
            bold=True,
        )
        _print_diff_human(diff)
        click.echo("Re-run without --dry-run to apply.")
        return True

    if unchanged and not force:
        click.secho("Config unchanged since last apply; skipping restart.", fg="green")
        return True

    if not no_auto_prep:
        if not _preflight_mounts(ctx, resolved, recursive=recursive_chown):
            return False

    if skip_restart:
        _run_box_config_py(ctx, resolved, verbs.SET_APPLIED_HASH, cur_hash)
        click.secho("Config validated; restart skipped (--skip-restart).", fg="yellow")
        return True

    # Host-side provisioning that has to happen BEFORE the container bounce:
    # apt packages may be needed by services the container talks to, and
    # sysctl values must be in place so first-packet routing works the
    # moment the container comes up.
    current_show = _parse_response(_run_box_config_py(ctx, resolved, verbs.SHOW), ctx) or {}
    applied_snapshot = _parse_response(
        _run_box_config_py(ctx, resolved, verbs.APPLIED_SHOW), ctx
    )

    if not yes:
        # Show the operator what's about to change so they can confirm with
        # full context (no need to run `diff` separately first).
        diff = _compute_diff(current_show, applied_snapshot)
        if not _diff_is_empty(diff):
            click.echo()
            click.secho("Pending changes:", bold=True)
            _print_diff_human(diff)
        if not click.confirm(
            f"Apply box config and restart the lager container on {resolved}?",
            default=True,
        ):
            click.secho("Aborted.", fg="yellow")
            return False
    if not _ensure_apt_packages(resolved, current_show, applied_snapshot):
        return False
    if not _ensure_sysctl(resolved, current_show, applied_snapshot):
        return False

    if not _bounce_container(ctx, resolved):
        # Bounce of the new config failed. The container may be down (start_box.sh
        # exits between `docker stop` and a successful `docker run` when, e.g.,
        # a mount entry is malformed). Try to restore the last applied snapshot
        # and bring the box back up on the previous good config.
        if _attempt_rollback(
            ctx, resolved,
            failed_config=current_show,
            previous_config=applied_snapshot,
        ):
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
        return False

    if not _wait_for_box_api(resolved):
        click.secho(
            f"Container restarted but the box API didn't come up within "
            f"{_API_READY_DEADLINE_SECONDS}s; not updating applied-hash. The "
            "bounce succeeded, but next `apply` will re-bounce unnecessarily. "
            "Check `lager hello` and the container logs.",
            fg="yellow",
            err=True,
        )
        return False

    # Post-bounce safety net. Catches the rare race where /etc/lager/box_config.json
    # was hand-edited between apply's pre-bounce read and start_box.sh's renderer
    # pass. We bounced into a container whose docker-args came from current_show;
    # if the on-disk file is different now, the container is up but the operator's
    # latest edits never landed.
    if not _post_apply_consistency_ok(ctx, resolved, expected=current_show, cur_hash=cur_hash):
        return False

    _run_box_config_py(ctx, resolved, verbs.SET_APPLIED_HASH, cur_hash)
    click.secho(f"Applied box config on {resolved}.", fg="green")
    return True


def _post_apply_consistency_ok(
    ctx: click.Context,
    resolved_box: str,
    *,
    expected: dict,
    cur_hash: Optional[str],
) -> bool:
    """Re-validate and re-show after a successful bounce; warn loudly on drift.

    Two failure shapes:
      1. validate now reports errors — someone wrote a malformed JSON over the
         file mid-bounce. Don't update applied-hash; the on-disk file isn't a
         valid applied state.
      2. show differs from `expected` (the snapshot we bounced) — file was
         edited to a *valid* but different shape. The container is running
         the older content; warn and skip applied-hash so the next apply
         picks up the new edits.
    """
    post_validate = _parse_response(
        _run_box_config_py(ctx, resolved_box, verbs.VALIDATE), ctx,
    )
    if not post_validate.get("ok", True):
        click.secho(
            "Warning: box_config.json no longer validates (edited during apply?). "
            "Container is running on the pre-edit version. Errors:",
            fg="yellow",
            err=True,
        )
        _print_errors(post_validate.get("errors") or [])
        click.secho(
            "Not updating applied-hash. Fix the file and re-run `lager box config apply`.",
            fg="yellow",
            err=True,
        )
        return False

    post_show = _parse_response(
        _run_box_config_py(ctx, resolved_box, verbs.SHOW), ctx,
    ) or {}
    if post_show != expected:
        click.secho(
            "Warning: box_config.json was modified during apply. Container is "
            "running with the snapshot captured at the start of apply; the "
            "latest on-disk changes have NOT been applied. Re-run "
            "`lager box config apply` to pick them up.",
            fg="yellow",
            err=True,
        )
        # Container is up on a valid (older) config — don't update applied-hash,
        # so the user's next apply re-bounces with the latest version.
        return False
    return True


def _ensure_apt_packages(
    resolved_box: str,
    current: dict,
    applied: Optional[dict],
) -> bool:
    """Install apt packages declared in current config. No-op when the field
    hasn't changed since the last applied snapshot — apt-get is fast for
    already-installed packages but the SSH round-trip is still ~seconds and
    re-running apply with no apt changes shouldn't pay that cost."""
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


def _attempt_rollback(
    ctx: click.Context,
    resolved_box: str,
    *,
    failed_config: dict,
    previous_config: Optional[dict],
) -> bool:
    """Restore the last applied snapshot and re-bounce. Returns True iff the
    box is back up on the previous good config.

    Critical detail: when start_box.sh fails mid-bounce (typically because
    docker run rejected something — duplicate mount, malformed flag), the
    container is GONE — `docker stop && docker rm` ran successfully before
    the failed `docker run`. The in-container shim is therefore unreachable
    by HTTP. So this restore goes through direct SSH file ops against the
    box host, not through the shim's restore-applied verb.

    First-apply boxes have no snapshot to fall back to; in that case there's
    nothing we can do remotely and the user has to recover by hand.

    Also reverts host-side sysctl: the failed apply may have written values
    to /etc/sysctl.d/99-lager-box-config.conf that are still live on the
    kernel, and if one of those keys is what broke the bounce (broken
    routing, bad shmmax, ...) the rolled-back container will come up into
    the same broken host. apt_packages are NOT reverted because the apply
    path never auto-uninstalls — the failed apply may have *added* packages,
    which is harmless to leave in place.
    """
    # Confirm the snapshot exists before announcing rollback. test rc=0 iff
    # the snapshot file is present; if missing this is a first-apply box and
    # rollback isn't possible.
    rc, _stdout, _stderr = default_ssh_runner(
        resolved_box,
        "test -f /etc/lager/box_config.applied.json",
    )
    if rc != 0:
        return False

    click.secho(
        "New config rejected; rolling back to last applied config and "
        "restarting...",
        fg="yellow",
        err=True,
    )
    # Atomic-ish restore via sudo cp. The shim's restore-applied verb uses
    # tmp+rename, but it's not reachable here (container down); the source
    # file is a previously-validated snapshot, so atomicity of the write is
    # the only loss versus the shim path. `sudo` is needed because
    # /etc/lager/box_config.json is owned by www-data (uid 33, the
    # container user) — lagerdata can't overwrite it without root.
    rc, _stdout, stderr = default_ssh_runner(
        resolved_box,
        "sudo -n cp /etc/lager/box_config.applied.json /etc/lager/box_config.json",
    )
    if rc != 0:
        click.secho(
            f"Failed to restore snapshot: {(stderr or '').strip()}",
            fg="red", err=True,
        )
        return False

    # Reverse-diff sysctl: previous values become "current", failed values
    # become "applied". When neither config touched sysctl this short-circuits
    # to a no-op inside _ensure_sysctl. A failure here is logged but doesn't
    # block the bounce — getting the box back up beats leaving it down.
    if not _ensure_sysctl(resolved_box, previous_config or {}, failed_config):
        click.secho(
            "Sysctl rollback failed; the host may still hold the failed "
            "config's kernel parameters. Inspect "
            "/etc/sysctl.d/99-lager-box-config.conf manually.",
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
    raw = _run_box_config_py(ctx, resolved, verbs.SHOW)
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
    """Single probe of the box's Python execution service. True iff `/hello`
    returns 200 — a 404 means the server is up but the route is gone, which
    is still a regression we want to surface, not paper over. Transport
    failures (connection refused, timeout) return False so the caller can
    poll cheaply."""
    try:
        r = requests.get(f"http://{box_ip}:{_BOX_API_PORT}/hello", timeout=timeout)
        return r.status_code == 200
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


_BOUNCE_TIMEOUT_SECONDS = 900


def _bounce_container(ctx: click.Context, resolved_box: str) -> bool:
    click.echo(f"Restarting lager container on {resolved_box} via SSH...")
    try:
        rc, _stdout, _stderr = default_ssh_runner(
            resolved_box,
            "cd ~/box && ./start_box.sh",
            timeout=_BOUNCE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        click.secho(
            f"SSH command timed out after {_BOUNCE_TIMEOUT_SECONDS // 60} minutes. "
            "start_box.sh may still be running on the box (cargo build, etc.); "
            "re-run `lager box config apply` once it finishes.",
            fg="red", err=True,
        )
        return False
    except FileNotFoundError:
        click.secho("ssh not found on local machine.", fg="red", err=True)
        return False
    return rc == 0


@box_config.group("mount", help="Manage host-to-container bind mounts.")
def mount_group() -> None:
    pass


@mount_group.command("list", help="List configured mounts.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def mount_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    _list_field(
        ctx, box, key="mounts", empty_msg="No mounts configured.",
        formatter=_fmt_mount, as_json=as_json,
    )


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
    raw = _run_box_config_py(ctx, resolved, verbs.MOUNT_ADD, payload_json)
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
    raw = _run_box_config_py(ctx, resolved, verbs.MOUNT_REMOVE, host, container)
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
    _list_field(
        ctx, box, key="volumes", empty_msg="No volumes configured.",
        formatter=_fmt_volume, as_json=as_json,
    )


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
    raw = _run_box_config_py(ctx, resolved, verbs.VOLUME_ADD, payload_json)
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
    raw = _run_box_config_py(ctx, resolved, verbs.VOLUME_REMOVE, name)
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
    _list_field(
        ctx, box, key="pip_packages", empty_msg="No pip packages configured.",
        formatter=str, as_json=as_json,
    )


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
    resolved = _resolve_box(ctx, box)

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
    raw = _run_box_config_py(ctx, resolved, verbs.PIP_ADD, payload_json)
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
    raw = _run_box_config_py(ctx, resolved, verbs.PIP_REMOVE, *packages)
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
    raw = _run_box_config_py(ctx, resolved, verbs.PIP_IMPORT_LEGACY)
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

@box_config.group("apt", help="Manage host-side apt packages.")
def apt_group() -> None:
    pass


@apt_group.command("list", help="List configured apt packages.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def apt_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    _list_field(
        ctx, box, key="apt_packages", empty_msg="No apt packages configured.",
        formatter=str, as_json=as_json,
    )


@apt_group.command("add", help="Add one or more apt packages to the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def apt_add_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    payload_json = json.dumps({"packages": list(packages)})
    raw = _run_box_config_py(ctx, resolved, verbs.APT_ADD, payload_json)
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
    raw = _run_box_config_py(ctx, resolved, verbs.APT_REMOVE, *packages)
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

@box_config.group("sysctl", help="Manage host sysctl values persisted across reboots.")
def sysctl_group() -> None:
    pass


@sysctl_group.command("list", help="List configured sysctl values.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def sysctl_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    _list_field(
        ctx, box, key="sysctl", empty_msg="No sysctl values configured.",
        formatter=lambda kv: f"{kv[0]} = {kv[1]}", as_json=as_json,
    )


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
    # The `key=value` split itself is host-side input parsing, not regex
    # validation — kept here because the shim takes a JSON object, not raw
    # `key=value` strings. The key-format check is shim-side.
    for entry in entries:
        if "=" not in entry:
            fmt_errors.append((entry, "expected key=value"))
            continue
        key, value = entry.split("=", 1)
        parsed[key] = value
    if fmt_errors:
        click.secho("Invalid sysctl entries:", fg="red", err=True)
        for e, r in fmt_errors:
            click.secho(f"  - {e!r}: {r}", fg="red", err=True)
        ctx.exit(1)

    payload_json = json.dumps({"entries": parsed})
    raw = _run_box_config_py(ctx, resolved, verbs.SYSCTL_SET, payload_json)
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
    raw = _run_box_config_py(ctx, resolved, verbs.SYSCTL_UNSET, *keys)
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
# env: container environment variables (passed via docker run --env)
# ---------------------------------------------------------------------------

@box_config.group("env", help="Manage container environment variables.")
def env_group() -> None:
    pass


@env_group.command("list", help="List configured env vars.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def env_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    _list_field(
        ctx, box, key="env", empty_msg="No env vars configured.",
        formatter=lambda kv: f"{kv[0]}={kv[1]}", as_json=as_json,
    )


@env_group.command(
    "set",
    help="Set one or more env KEY=VALUE pairs. Upsert; safe to re-run.",
)
@click.argument("entries", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def env_set_cmd(ctx: click.Context, entries: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    parsed: dict = {}
    fmt_errors = []
    # `KEY=VALUE` input split is host-side because the shim takes a JSON
    # object, not raw entries. Value-format and key-format checks happen
    # shim-side via cfg.validate_env_key.
    for entry in entries:
        if "=" not in entry:
            fmt_errors.append((entry, "expected KEY=VALUE"))
            continue
        key, value = entry.split("=", 1)
        parsed[key] = value
    if fmt_errors:
        click.secho("Invalid env entries:", fg="red", err=True)
        for e, r in fmt_errors:
            click.secho(f"  - {e!r}: {r}", fg="red", err=True)
        ctx.exit(1)

    payload_json = json.dumps({"entries": parsed})
    raw = _run_box_config_py(ctx, resolved, verbs.ENV_SET, payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to set env vars:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    set_keys = payload.get("set") or list(parsed.keys())
    click.secho(
        f"Set {len(set_keys)} env var(s) on {resolved}: " + ", ".join(set_keys),
        fg="green",
    )
    click.echo("Run `lager box config apply` to put the new env vars into effect.")


@env_group.command("unset", help="Remove one or more env vars from the box config.")
@click.argument("keys", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def env_unset_cmd(ctx: click.Context, keys: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, verbs.ENV_UNSET, *keys)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to unset env vars:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    removed = payload.get("removed") or []
    if removed:
        click.secho(f"Removed {len(removed)} env var(s): " + ", ".join(removed), fg="green")
        click.echo("Run `lager box config apply` to update the running container.")
    else:
        click.secho("No matching env vars were configured.", fg="yellow")


# ---------------------------------------------------------------------------
# cargo_packages: in-container Rust crates installed during container start
# ---------------------------------------------------------------------------

@box_config.group("cargo", help="Manage in-container cargo crates.")
def cargo_group() -> None:
    pass


@cargo_group.command("list", help="List configured cargo crates.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def cargo_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    _list_field(
        ctx, box, key="cargo_packages", empty_msg="No cargo crates configured.",
        formatter=str, as_json=as_json,
    )


@cargo_group.command("add", help="Add one or more cargo crates to the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def cargo_add_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    payload_json = json.dumps({"packages": list(packages)})
    raw = _run_box_config_py(ctx, resolved, verbs.CARGO_ADD, payload_json)
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
    raw = _run_box_config_py(ctx, resolved, verbs.CARGO_REMOVE, *packages)
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


# ---------------------------------------------------------------------------
# npm_packages: in-container Node.js packages installed during container start
# ---------------------------------------------------------------------------

@box_config.group("npm", help="Manage in-container npm packages.")
def npm_group() -> None:
    pass


@npm_group.command("list", help="List configured npm packages.")
@click.option("--box", help="Lagerbox name or IP")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
@click.pass_context
def npm_list_cmd(ctx: click.Context, box: Optional[str], as_json: bool) -> None:
    _list_field(
        ctx, box, key="npm_packages", empty_msg="No npm packages configured.",
        formatter=str, as_json=as_json,
    )


@npm_group.command("add", help="Add one or more npm packages to the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def npm_add_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    payload_json = json.dumps({"packages": list(packages)})
    raw = _run_box_config_py(ctx, resolved, verbs.NPM_ADD, payload_json)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to add npm packages:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    added = payload.get("added") or list(packages)
    click.secho(f"Added {len(added)} npm package(s) on {resolved}: " + ", ".join(added), fg="green")
    click.echo("Run `lager box config apply` to install them in the container.")


@npm_group.command("remove", help="Remove one or more npm packages from the box config.")
@click.argument("packages", nargs=-1, required=True)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def npm_remove_cmd(ctx: click.Context, packages: tuple, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    raw = _run_box_config_py(ctx, resolved, verbs.NPM_REMOVE, *packages)
    payload = _parse_response(raw, ctx)
    if not payload.get("ok"):
        click.secho("Failed to remove npm packages:", fg="red", err=True)
        _print_errors(payload.get("errors") or [payload.get("error", "unknown error")])
        ctx.exit(1)
    removed = payload.get("removed") or []
    if removed:
        click.secho(f"Removed {len(removed)} npm package(s): " + ", ".join(removed), fg="green")
        click.echo(
            "Run `lager box config apply` to record the change. Note: existing "
            "installs in the container are not auto-uninstalled."
        )
    else:
        click.secho("No matching npm packages were configured.", fg="yellow")

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager dut` -- author the DUT context that the MCP server hands to
AI agents.

The DUT context lives in ``/etc/lager/bench.json`` under the
``dut_slots`` (or ``dut_context`` short-form) key. It tells the agent
what this box tests: purpose, MCU, key peripherals, schematic /
datasheet / firmware references, and subsystem groupings.

This module ships three verbs:

- ``show``   -- print the current DUT context as JSON.
- ``edit``   -- round-trip the DUT context through ``$EDITOR``.
- ``add-doc``-- append a single DocRef to one of the doc-ref lists.

All three operate on ``/etc/lager/bench.json`` over SSH so the
authoring workflow mirrors ``lager box-config``.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Optional

import click

from .config import _resolve_box
from ._host_ops import is_valid_unix_username
from ._ssh import (
    default_ssh_runner,
    resolve_box_user,
    strip_ssh_banner,
    sudo_error_message,
)
from ...core.group_usage import LagerGroup
from ...errors import ssh_error

_BENCH_JSON_PATH = "/etc/lager/bench.json"
# Staged in /tmp (world-writable, 1777) rather than inside /etc/lager, which on a
# normally-installed box is owned by www-data (setup_and_deploy_box.sh chowns it
# to uid 33), so the login user cannot create files there. Fixed name (not
# mktemp) so the passwordless-sudo grant can be an exact literal path, mirroring
# /tmp/lager_version_tmp. Both paths must stay free of shell-special chars so
# shlex.quote is a no-op and the quoted command still matches the sudoers spec.
_BENCH_JSON_TMP_PATH = "/tmp/lager-bench.json.tmp"

_BENCH_SUDO_BASE = (
    "writing /etc/lager/bench.json needs passwordless sudo — a normally-"
    "installed box owns /etc/lager as www-data, so the login user can't write "
    "it directly."
)

def _bench_sudoers_bootstrap(user: str = "lagerdata") -> str:
    """Manual-fix text for a box missing the bench.json sudo grant. Names
    the box's actual login user so the pasted rule matches it (a hardcoded
    `lagerdata` never matched on boxes with a different login user)."""
    # Error-path text renderer: never raise, never interpolate a non-plain
    # username (it comes from local box storage unvalidated) into a
    # paste-into-root-shell snippet. Same rule as _host_ops.sudoers_bootstrap.
    if not is_valid_unix_username(user):
        user = "lagerdata"
    return (
        "This box is missing the bench.json sudo grant (older box, or not yet "
        "re-provisioned). Re-provision with `lager update --box <BOX>` (or "
        "`lager install`), or add it ONCE on the box:\n"
        "\n"
        "  sudo tee /etc/sudoers.d/lager-bench-json >/dev/null <<'SUDOERS'\n"
        f"  {user} ALL=(ALL) NOPASSWD: /bin/cp /tmp/lager-bench.json.tmp /etc/lager/bench.json\n"
        f"  {user} ALL=(ALL) NOPASSWD: /bin/chmod 644 /etc/lager/bench.json\n"
        "  SUDOERS\n"
        "  sudo chmod 440 /etc/sudoers.d/lager-bench-json\n"
        "\n"
        "Then re-run the command."
    )


def _read_bench_json(box_ip: str) -> dict:
    """Read /etc/lager/bench.json from the box; return {} if missing."""
    rc, stdout, stderr = default_ssh_runner(
        box_ip,
        f"cat {_BENCH_JSON_PATH} 2>/dev/null || true",
    )
    if rc != 0:
        # The remote cmd ends in `|| true`, so a nonzero rc is always a
        # transport failure — continuing with {} could let `edit` write
        # back a bench.json missing all its other keys.
        ssh_error(stderr, box_ip, user=resolve_box_user(box_ip)).die()
    body = (stdout or "").strip()
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        click.secho(
            f"bench.json on {box_ip} is not valid JSON: {e}",
            fg="red", err=True,
        )
        return {}


def _write_bench_json(box_ip: str, payload: dict) -> bool:
    """Replace /etc/lager/bench.json with ``payload``.

    The JSON body is piped over SSH stdin so it never goes through the
    shell, and staged in ``/tmp`` (world-writable) — never inside
    ``/etc/lager``, which on a normally-installed box is owned by
    ``www-data`` (mode 755; see setup_and_deploy_box.sh), where the login
    user cannot create files. We then try an unprivileged ``mv`` into
    place — which succeeds on legacy boxes where the login user owns
    ``/etc/lager`` — and fall back to the pre-provisioned passwordless
    ``sudo -n /bin/cp`` + ``chmod 644`` grant that setup_and_deploy_box.sh
    installs (mirroring the ``saved_nets.json`` / ``version`` write grants
    and the udev grant). The ``/tmp`` temp is always removed. On a box that
    predates the grant the write fails with an actionable "re-provision"
    message rather than a bare "Permission denied".

    Returns True on success.
    """
    body = json.dumps(payload, indent=2) + "\n"
    q_dst = shlex.quote(_BENCH_JSON_PATH)      # /etc/lager/bench.json
    q_tmp = shlex.quote(_BENCH_JSON_TMP_PATH)  # /tmp/lager-bench.json.tmp
    # Stage in /tmp, try an unprivileged atomic move, then fall back to the
    # passwordless-sudo grant when /etc/lager is www-data-owned. Absolute binary
    # paths (/bin/cp, /bin/chmod) so they match the NOPASSWD command specs
    # exactly. rc is captured before the /tmp cleanup so `rm` can't mask it; the
    # unprivileged mv's stderr is dropped (expected EACCES on a www-data box)
    # so only the real diagnostic from the sudo branch reaches the user.
    cmd = (
        f"cat > {q_tmp} && chmod 644 {q_tmp} && "
        f"{{ mv -f {q_tmp} {q_dst} 2>/dev/null "
        f"|| {{ sudo -n /bin/cp {q_tmp} {q_dst} && sudo -n /bin/chmod 644 {q_dst}; }}; }}; "
        f"rc=$?; rm -f {q_tmp}; exit $rc"
    )
    rc, _stdout, stderr = default_ssh_runner(box_ip, cmd, stdin=body)
    stderr = strip_ssh_banner(stderr)
    if rc == 255:
        ssh_error(stderr, box_ip, user=resolve_box_user(box_ip)).die()
    if rc != 0:
        msg = sudo_error_message(
            stderr,
            base_text=_BENCH_SUDO_BASE,
            bootstrap_text=_bench_sudoers_bootstrap(resolve_box_user(box_ip)),
        )
        click.secho(f"Failed to write {_BENCH_JSON_PATH}: {msg}", fg="red", err=True)
        return False
    return True


def _extract_dut_block(payload: dict) -> tuple[str, Any]:
    """Return (key, value) for the DUT-shaped block in bench.json.

    Prefers ``dut_slots`` (the canonical list form). Falls back to the
    single-DUT ``dut_context`` shape if that's what's on disk.  When
    nothing exists yet, defaults to a single empty slot.
    """
    if "dut_slots" in payload and isinstance(payload["dut_slots"], list):
        return "dut_slots", payload["dut_slots"]
    if "dut_context" in payload and isinstance(payload["dut_context"], dict):
        return "dut_context", payload["dut_context"]
    return "dut_slots", [{
        "name": "main",
        "active": True,
        "purpose": "",
        "summary": "",
        "mcu": None,
        "key_peripherals": [],
        "schematic_refs": [],
        "datasheet_refs": [],
        "firmware_refs": [],
        "extra_docs": [],
        "subsystems": [],
    }]


def _primary_slot(payload: dict) -> tuple[dict, Optional[int]]:
    """Return (slot_dict, list_index) for the first active DUT slot.

    The slot is returned by reference so callers can mutate it and write
    the whole payload back. When the on-disk shape is the single-DUT
    ``dut_context`` short-form, ``list_index`` is None and callers should
    re-assign ``payload['dut_context']`` after mutating.
    """
    key, value = _extract_dut_block(payload)
    if key == "dut_slots":
        slots: list = value
        # _extract_dut_block may synthesize a default list that isn't yet
        # attached to payload; ensure the write-back path can find it.
        payload["dut_slots"] = slots
        for i, slot in enumerate(slots):
            if isinstance(slot, dict) and slot.get("active", True):
                return slot, i
        if slots and isinstance(slots[0], dict):
            return slots[0], 0
        new_slot: dict = {"name": "main", "active": True}
        slots.append(new_slot)
        return new_slot, len(slots) - 1
    # dut_context short-form
    return value, None


@click.group(name="dut", cls=LagerGroup, help="Author the DUT context exposed to AI agents via MCP.")
def box_dut() -> None:
    """Manage /etc/lager/bench.json's DUT context block."""


@box_dut.command("show", help="Print the current DUT context as JSON.")
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def show_cmd(ctx: click.Context, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    payload = _read_bench_json(resolved)
    _, value = _extract_dut_block(payload)
    click.echo(json.dumps(value, indent=2))


@box_dut.command(
    "edit",
    help="Open the DUT context in $EDITOR. On save, writes back to /etc/lager/bench.json.",
)
@click.option("--box", help="Lagerbox name or IP")
@click.pass_context
def edit_cmd(ctx: click.Context, box: Optional[str]) -> None:
    resolved = _resolve_box(ctx, box)
    payload = _read_bench_json(resolved)
    key, value = _extract_dut_block(payload)

    editor = (
        os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or ("nano" if shutil.which("nano") else "vi")
    )
    # $EDITOR commonly carries flags (e.g. "subl -w", "code -w", "vim -p").
    # Split into argv so the flags aren't treated as part of the program name.
    editor_argv = shlex.split(editor)

    body = json.dumps(value, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="lager-dut-context-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        original = body
        while True:
            rc = subprocess.call([*editor_argv, tmp_path])
            with open(tmp_path, "r", encoding="utf-8") as f:
                new_body = f.read()
            if new_body == original:
                msg = "No changes saved." if rc == 0 else f"Editor exited with rc={rc}; no changes saved."
                click.secho(msg, fg="yellow")
                ctx.exit(rc or 0)
            try:
                new_value = json.loads(new_body)
            except json.JSONDecodeError as e:
                click.secho(f"Invalid JSON: {e}", fg="red", err=True)
                if not click.confirm("Re-open editor?", default=True):
                    click.secho("Aborted; bench.json unchanged.", fg="yellow")
                    ctx.exit(1)
                continue

            payload[key] = new_value
            if _write_bench_json(resolved, payload):
                click.secho(f"Saved DUT context on {resolved}.", fg="green")
                click.echo(
                    "The MCP server auto-reloads on its next request, so a "
                    "connected agent will see the change without a restart."
                )
                return
            # Write failure path — give the user a chance to retry
            if not click.confirm("Re-open editor?", default=True):
                click.secho("Aborted; bench.json unchanged.", fg="yellow")
                ctx.exit(1)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


_DOC_KIND_CHOICES = [
    "schematic", "layout", "datasheet", "firmware", "manual", "errata", "other",
]

_DOC_LIST_KEYS = {
    "schematic": "schematic_refs",
    "layout": "extra_docs",
    "datasheet": "datasheet_refs",
    "firmware": "firmware_refs",
    "manual": "extra_docs",
    "errata": "extra_docs",
    "other": "extra_docs",
}


@box_dut.command(
    "add-doc",
    help=(
        "Attach a schematic / datasheet / firmware reference to the active "
        "DUT. The box does NOT host the file; this just records a pointer "
        "(URL or repo-relative path) that the agent will fetch with its "
        "own file tools."
    ),
)
@click.option("--box", help="Lagerbox name or IP")
@click.option("--kind", type=click.Choice(_DOC_KIND_CHOICES), default="schematic", show_default=True)
@click.option("--title", required=True, help="Human label for the document.")
@click.option("--url", help="External URL (https://...).")
@click.option("--repo-path", help="Path relative to the user's test project (e.g. docs/schematic.pdf).")
@click.option("--pages", help='Optional page/sheet hint (e.g. "3-5" or "POWER sheet").')
@click.option("--notes", help="Optional free-form note about this document.")
@click.pass_context
def add_doc_cmd(
    ctx: click.Context,
    box: Optional[str],
    kind: str,
    title: str,
    url: Optional[str],
    repo_path: Optional[str],
    pages: Optional[str],
    notes: Optional[str],
) -> None:
    if not url and not repo_path:
        click.secho(
            "Must supply at least one of --url or --repo-path so the "
            "agent has somewhere to fetch the document from.",
            fg="red", err=True,
        )
        ctx.exit(1)

    resolved = _resolve_box(ctx, box)
    payload = _read_bench_json(resolved)
    slot, slot_idx = _primary_slot(payload)

    doc_ref: dict[str, Any] = {"title": title, "kind": kind}
    if url:
        doc_ref["url"] = url
    if repo_path:
        doc_ref["repo_path"] = repo_path
    if pages:
        doc_ref["pages"] = pages
    if notes:
        doc_ref["notes"] = notes

    list_key = _DOC_LIST_KEYS[kind]
    existing = slot.get(list_key)
    if not isinstance(existing, list):
        existing = []
    existing.append(doc_ref)
    slot[list_key] = existing

    if slot_idx is not None:
        payload["dut_slots"][slot_idx] = slot
    else:
        payload["dut_context"] = slot

    if not _write_bench_json(resolved, payload):
        ctx.exit(1)

    click.secho(
        f"Attached {kind} reference '{title}' to DUT '{slot.get('name', 'main')}' on {resolved}.",
        fg="green",
    )
    click.echo(
        "The MCP server auto-reloads on its next request, so a connected "
        "agent will see the new doc ref without a restart."
    )

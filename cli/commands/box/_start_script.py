# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Which start_box.sh a `lager box-config` bounce runs, and how it runs it.

The box checkout at ~/box is a sparse clone of the whole repository, so the
tracked script lives at ~/box/box/start_box.sh. `lager install` and
`lager update` flatten that tree after every checkout (`box/*` moves up to
~/box, see `_flatten_shell_cmd` in utility/update.py), which leaves
~/box/start_box.sh canonical and no ~/box/box at all.

A git update that does not flatten (a plain `git reset --hard` in ~/box, as
some box-management tooling does) restores the complete tracked tree under
~/box/box and leaves the earlier flattened copy at ~/box untouched. From then
on the top-level start_box.sh, and the top-level lager/ tree it builds from,
are older than what was just deployed. A bounce that hardcodes
`cd ~/box && ./start_box.sh` then starts the container from that older script
and source: one that predates `--no-publish` publishes ports another container
already holds, and `docker run` fails.

The rule here is the one such tooling already uses: when ~/box/box holds a
start_box.sh, it is the newer copy and it wins. Anything under ~/box/box came
from a checkout later than the last flatten, because a completed flatten
removes ~/box/box. It additionally requires ~/box/box/lager, because
start_box.sh builds and mounts from its own directory (`SCRIPT_DIR/lager`);
a flatten that stopped partway can leave a nested start_box.sh whose lager/
already moved up, and running that copy would fail at its first `cd`. That
case falls back to the top-level copy, which is where lager/ went.

The script is run with `bash`, never `./start_box.sh`. It is tracked without
the executable bit, so a checkout that was never flattened by a path that
runs `chmod +x` cannot be executed directly. `bash ./start_box.sh` is also
the same process shape that `./start_box.sh` produced through its
`#!/bin/bash` line, which start_box.sh's own leftover-poller sweep is written
around.

POSIX only: the remote login shell may be dash.
"""

from __future__ import annotations

from collections.abc import Iterable

# Set by the snippet; the directory the chosen start_box.sh lives in.
START_DIR_VAR = "_lager_start_dir"

RESOLVE_START_DIR = (
    f'{START_DIR_VAR}="$HOME/box"; '
    'if [ -f "$HOME/box/box/start_box.sh" ] && [ -d "$HOME/box/box/lager" ]; '
    f'then {START_DIR_VAR}="$HOME/box/box"; fi'
)


SKIP_BUILD_VAR = "_lager_skip_build"


def skip_build_if_current() -> str:
    """Shell snippet: set SKIP_BUILD_VAR to 1 when the image is current.

    A bounce never changes what the image is built from. box_config only
    feeds `docker run` arguments and the post-start pip/cargo/npm steps, so
    rebuilding on every bounce bought nothing: about a minute with a warm
    Docker cache, and a cold build can outlast the bounce's SSH timeout.

    "Current" means a `lager` image exists AND the tree this bounce will run
    hashes to /etc/lager/build-hash, which `lager install` and `lager update`
    write from the same hasher once their image is in place. The hash is taken
    over the resolved directory, so a ~/box/box tree is measured as itself.
    Anything else builds as before: no image, no recorded hash, or a tree that
    changed since the last install or update (for example a git update that
    did not go through `lager update`). A wrong skip would start new code on
    an old image, so every doubt resolves to building.
    """
    # Lazy: utility.update is large and imports from commands.box.
    from ..utility.update import _build_hash_shell_cmd

    current = _build_hash_shell_cmd(f'"${START_DIR_VAR}"')
    return (
        f'{SKIP_BUILD_VAR}=""; '
        "if docker image inspect lager >/dev/null 2>&1; then "
        f"_lager_cur=$({current}); "
        # Overridable only so unit tests need not write /etc/lager.
        '_lager_rec=$(cat "${LAGER_BUILD_HASH_FILE:-/etc/lager/build-hash}" '
        "2>/dev/null); "
        'if [ -n "$_lager_cur" ] && [ "$_lager_cur" = "$_lager_rec" ]; then '
        f"{SKIP_BUILD_VAR}=1; fi; fi"
    )


def start_box_command(
    env: Iterable[tuple[str, str]] = (),
    args: str = "",
    *,
    skip_build_when_current: bool = False,
) -> str:
    """Shell command that runs the deployed start_box.sh.

    `env` pairs are prefixed to the `bash` invocation as `NAME=value`, so they
    reach start_box.sh and nothing else. Values are inserted verbatim: pass
    literals or `"$var"` references set earlier in the same command.

    `skip_build_when_current` adds `skip_build_if_current` and passes its
    verdict as LAGER_SKIP_BUILD. start_box.sh treats an empty value as unset.
    """
    env = list(env)
    steps = [RESOLVE_START_DIR]
    if skip_build_when_current:
        steps.append(skip_build_if_current())
        env.append(("LAGER_SKIP_BUILD", f'"${SKIP_BUILD_VAR}"'))
    prefix = "".join(f"{name}={value} " for name, value in env)
    suffix = f" {args}" if args else ""
    return (
        "; ".join(steps)
        + f'; cd "${START_DIR_VAR}" && {prefix}bash ./start_box.sh{suffix}'
    )


# start_box.sh's line when LAGER_SKIP_BUILD is set; the bounce reports it.
SKIPPED_BUILD_MARKER = "Skipping build (LAGER_SKIP_BUILD set"


# For operator-facing hints: where to look when running it by hand.
MANUAL_START_HINT = (
    "`~/box/box/start_box.sh` if that directory exists, otherwise "
    "`~/box/start_box.sh` (run it with `bash`)"
)

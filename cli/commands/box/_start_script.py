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


def start_box_command(env: Iterable[tuple[str, str]] = (), args: str = "") -> str:
    """Shell command that runs the deployed start_box.sh.

    `env` pairs are prefixed to the `bash` invocation as `NAME=value`, so they
    reach start_box.sh and nothing else. Values are inserted verbatim: pass
    literals or `"$var"` references set earlier in the same command.
    """
    prefix = "".join(f"{name}={value} " for name, value in env)
    suffix = f" {args}" if args else ""
    return (
        f'{RESOLVE_START_DIR}; cd "${START_DIR_VAR}" && '
        f"{prefix}bash ./start_box.sh{suffix}"
    )


# For operator-facing hints: where to look when running it by hand.
MANUAL_START_HINT = (
    "`~/box/box/start_box.sh` if that directory exists, otherwise "
    "`~/box/start_box.sh` (run it with `bash`)"
)

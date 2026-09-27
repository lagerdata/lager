#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Renderer for /etc/lager/box_config.json -> sourceable bash arg file.

Writes a single file declaring three bash arrays that start_box.sh
sources and expands as docker-run arguments:

    BOX_CONFIG_MOUNTS      -v flags (mounts + volumes)
    BOX_CONFIG_ENV         --env flags
    BOX_CONFIG_HOST_PATHS  bind-mount host paths to mkdir -p before run
    BOX_CONFIG_NETWORK     value for --network (scalar, not an array)
    BOX_CONFIG_NETWORK_PENDING
                           the configured network mode when this start
                           withholds it, else empty (scalar)

BOX_CONFIG_NETWORK is always written, including by the empty/degraded body
below, so start_box.sh can expand it unconditionally. A box whose config is
missing or malformed still gets the default network rather than an empty
--network argument, which docker would reject. It is the network this start may
use, which is not always the one configured: see _effective_network.

Why a sourceable file instead of stdout-parsed-into-vars: the previous
contract emitted `--env 'KEY=hello world'` on stdout, and start_box.sh
captured + unquoted-expanded that string into the docker invocation.
Bash variable expansion does not re-parse quotes, so the value got
word-split into `--env 'KEY=hello` and `world'` with literal quotes
attached. Sourcing a bash array assignment preserves elements verbatim
because the parser sees `shlex.quote`'s output as proper bash syntax.

Exits non-zero on JSON parse / validation failure, but always writes the
output file (with empty arrays) so start_box.sh degrades cleanly to
"no box-config" rather than skipping the source.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys


_CONFIG_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
_spec = importlib.util.spec_from_file_location("lager_box_config_renderer_cfg", _CONFIG_PY)
cfg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cfg
_spec.loader.exec_module(cfg)


_HEADER = "# Rendered by render_docker_args.py - do not edit by hand\n"

# Set by `lager box-config apply` on the start_box.sh it runs, once its
# reachability check has passed or been overridden. Mirrors
# _NETWORK_SWITCH_CONFIRM_ENV in cli/commands/box/config.py; the two ship in
# separate trees, and test/unit/box/test_network_mode.py asserts they agree.
_CONFIRM_ENV = "LAGER_APPLY_NETWORK_SWITCH"


def _bash_array(name: str, items: list) -> str:
    body = " ".join(shlex.quote(x) for x in items)
    return f"{name}=({body})\n"


def _bash_scalar(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}\n"


def _applied_network_mode(applied_path: str) -> str:
    """The network mode the last successful apply recorded, or the default.

    Any failure to read the snapshot counts as the default. That withholds a
    switch to host rather than letting an unreadable file make it.
    """
    try:
        snap = cfg.read_applied_snapshot(applied_path)
    except Exception:
        return cfg.DEFAULT_NETWORK_MODE
    return snap.network_mode if snap is not None else cfg.DEFAULT_NETWORK_MODE


def _effective_network(desired: str, applied_path: str) -> tuple:
    """(network this start runs on, configured mode it withholds or "").

    A switch to host happens only through `lager box-config apply`, which first
    checks that the switch will not cut the operator's route to the box. Every
    container start renders from the same box_config.json -- `lager update`,
    `lager install`, `box-config restart`, a hand-run start_box.sh -- so without
    this a refused apply left `host` in the config for the next routine start to
    apply unchecked. Those starts keep the mode the last successful apply
    recorded. A return to lagernet is honored from any start, because it
    restores the published ports rather than removing them.
    """
    if desired != "host" or os.environ.get(_CONFIRM_ENV) == "1":
        return desired, ""
    if _applied_network_mode(applied_path) == "host":
        return desired, ""
    return cfg.DEFAULT_NETWORK_MODE, desired


def _render_body(c, applied_path: str | None = None) -> str:
    mount_args: list = []
    for m in c.mounts:
        spec = f"{m.host}:{m.container}"
        if m.readonly:
            spec += ":ro"
        mount_args.extend(["-v", spec])
    for v in c.volumes:
        mount_args.extend(["-v", f"{v.name}:{v.container}"])

    env_args: list = []
    for k, v in c.env.items():
        env_args.extend(["--env", f"{k}={v}"])

    host_paths = [m.host for m in c.mounts]

    network, pending = _effective_network(
        c.network_mode, applied_path or cfg.APPLIED_CONFIG_PATH)

    return (
        _HEADER
        + _bash_array("BOX_CONFIG_MOUNTS", mount_args)
        + _bash_array("BOX_CONFIG_ENV", env_args)
        + _bash_array("BOX_CONFIG_HOST_PATHS", host_paths)
        + _bash_scalar("BOX_CONFIG_NETWORK", network)
        + _bash_scalar("BOX_CONFIG_NETWORK_PENDING", pending)
    )


def _empty_body() -> str:
    return (
        _HEADER
        + "BOX_CONFIG_MOUNTS=()\n"
        + "BOX_CONFIG_ENV=()\n"
        + "BOX_CONFIG_HOST_PATHS=()\n"
        + _bash_scalar("BOX_CONFIG_NETWORK", cfg.DEFAULT_NETWORK_MODE)
        + _bash_scalar("BOX_CONFIG_NETWORK_PENDING", "")
    )


_atomic_write = cfg.write_atomic


def _render(argv: list) -> int:
    if len(argv) < 3:
        print("usage: render_docker_args.py <box_config.json> <out.sh> "
              "[<applied_snapshot.json>]", file=sys.stderr)
        return 2

    config_path, out_path = argv[1], argv[2]
    applied_path = argv[3] if len(argv) > 3 else cfg.APPLIED_CONFIG_PATH

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        _atomic_write(out_path, _empty_body())
        return 0
    except json.JSONDecodeError as e:
        print(f"box_config.json: invalid JSON: {e}", file=sys.stderr)
        _atomic_write(out_path, _empty_body())
        return 1

    try:
        c = cfg.BoxConfig.from_dict(raw)
    except cfg.ValidationError as e:
        print(str(e), file=sys.stderr)
        _atomic_write(out_path, _empty_body())
        return 1

    _atomic_write(out_path, _render_body(c, applied_path))
    return 0


def main(argv: list) -> int:
    # A write failure here used to surface as a raw traceback that start_box.sh
    # swallowed into a one-line warning, so the container came up with none of
    # the box_config mounts, volumes, or env vars while the CLI reported a
    # successful apply.
    try:
        return _render(argv)
    except cfg.RenderWriteError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

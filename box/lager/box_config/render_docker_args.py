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


def _bash_array(name: str, items: list) -> str:
    body = " ".join(shlex.quote(x) for x in items)
    return f"{name}=({body})\n"


def _render_body(c) -> str:
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

    return (
        _HEADER
        + _bash_array("BOX_CONFIG_MOUNTS", mount_args)
        + _bash_array("BOX_CONFIG_ENV", env_args)
        + _bash_array("BOX_CONFIG_HOST_PATHS", host_paths)
    )


def _empty_body() -> str:
    return (
        _HEADER
        + "BOX_CONFIG_MOUNTS=()\n"
        + "BOX_CONFIG_ENV=()\n"
        + "BOX_CONFIG_HOST_PATHS=()\n"
    )


def _atomic_write(path: str, body: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)


def main(argv: list) -> int:
    if len(argv) < 3:
        print("usage: render_docker_args.py <box_config.json> <out.sh>", file=sys.stderr)
        return 2

    config_path, out_path = argv[1], argv[2]

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

    _atomic_write(out_path, _render_body(c))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

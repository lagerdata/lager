#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Standalone renderer invoked from start_box.sh.

Reads box_config.json, validates it, and writes shell-safe docker arg
strings to stdout in three lines:
    line 1: -v / --mount args (mounts + volumes)
    line 2: --env args
    line 3: bind-mount HOST paths only, shell-quoted, space-separated
            (start_box.sh mkdir -p's these so missing dirs don't break
             `docker run`)

Exits non-zero with errors on stderr if the config is malformed. The
caller (start_box.sh) treats this as a soft failure and proceeds with
empty args, so a broken config never blocks the container start.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sys

# Load config.py directly without triggering lager/__init__.py — start_box.sh
# runs this on the box's host Python, which lacks the hardware deps that the
# package's __init__ chain pulls in (simplejson, pyvisa, numpy, ...).
_CONFIG_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
_spec = importlib.util.spec_from_file_location("lager_box_config_renderer_cfg", _CONFIG_PY)
cfg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cfg
_spec.loader.exec_module(cfg)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: render_docker_args.py <path>", file=sys.stderr)
        return 2

    path = argv[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        print("", flush=True)
        print("", flush=True)
        print("", flush=True)
        return 0
    except json.JSONDecodeError as e:
        print(f"box_config.json: invalid JSON: {e}", file=sys.stderr)
        return 1

    try:
        c = cfg.BoxConfig.from_dict(raw)
    except cfg.ValidationError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(" ".join(c.docker_mount_args()), flush=True)
    print(" ".join(c.docker_env_args()), flush=True)
    print(" ".join(shlex.quote(m.host) for m in c.mounts), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

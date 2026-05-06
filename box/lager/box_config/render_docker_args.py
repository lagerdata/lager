#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Standalone renderer invoked from start_box.sh.

Reads box_config.json, validates it, and writes shell-safe docker arg
strings to stdout in two lines:
    line 1: -v / --mount args (mounts + volumes)
    line 2: --env args

Exits non-zero with errors on stderr if the config is malformed. The
caller (start_box.sh) treats this as a soft failure and proceeds with
empty args, so a broken config never blocks the container start.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lager.box_config import config as cfg  # noqa: E402


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
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

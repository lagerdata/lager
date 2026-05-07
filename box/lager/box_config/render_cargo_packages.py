#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Render cargo_packages from box_config.json to /etc/lager/cargo_packages.txt.

Invoked by start_box.sh before the docker run so the post-run
`cargo install` step has the latest list. One spec per line, comments
allowed. Soft-fails on missing config or empty cargo_packages so the
container always comes up.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys


_CONFIG_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
_spec = importlib.util.spec_from_file_location("lager_box_config_cargo_renderer_cfg", _CONFIG_PY)
cfg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cfg
_spec.loader.exec_module(cfg)


_HEADER = (
    "# User-installed cargo crates, rendered from /etc/lager/box_config.json\n"
    "# Edit via `lager box config cargo add/remove`, then `lager box config apply`.\n"
    "# Manual edits to this file are overwritten on the next apply.\n"
    "\n"
)


def _write(path: str, body: str) -> None:
    tmp = f"{path}.tmp"
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, path)


def main(argv: list) -> int:
    if len(argv) < 3:
        print("usage: render_cargo_packages.py <box_config.json> <out_path>", file=sys.stderr)
        return 2

    config_path, out_path = argv[1], argv[2]

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        _write(out_path, _HEADER)
        return 0
    except json.JSONDecodeError as e:
        print(f"box_config.json: invalid JSON: {e}", file=sys.stderr)
        return 1

    try:
        c = cfg.BoxConfig.from_dict(raw)
    except cfg.ValidationError as e:
        print(str(e), file=sys.stderr)
        return 1

    body = _HEADER + "".join(f"{p}\n" for p in sorted(c.cargo_packages))
    _write(out_path, body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

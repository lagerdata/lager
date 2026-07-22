#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Render pip_packages from box_config.json to /etc/lager/user_requirements.txt.

Invoked by start_box.sh before the docker run so the in-container
`pip install -r` step has the latest list. Soft-fails on missing config
or empty pip_packages so the container always comes up.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys


_CONFIG_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.py")
_spec = importlib.util.spec_from_file_location("lager_box_config_pip_renderer_cfg", _CONFIG_PY)
cfg = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cfg
_spec.loader.exec_module(cfg)


_HEADER = (
    "# User-installed packages, rendered from /etc/lager/box_config.json\n"
    "# Edit via `lager box-config pip add/remove`, then `lager box-config apply`.\n"
    "# Manual edits to this file are overwritten on the next apply.\n"
    "\n"
)


_write = cfg.write_atomic


def _render(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: render_pip_requirements.py <box_config.json> <out_path>", file=sys.stderr)
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

    body = _HEADER + "".join(f"{p}\n" for p in sorted(c.pip_packages))
    _write(out_path, body)
    return 0


def main(argv: list[str]) -> int:
    # A write failure here used to surface as a raw traceback that start_box.sh
    # swallowed into a one-line warning, leaving the container running the old
    # package set while the CLI reported a successful apply.
    try:
        return _render(argv)
    except cfg.RenderWriteError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

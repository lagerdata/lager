# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
CLI interface for net.py - standalone command-line tool for managing nets.

This module provides the command-line interface that enables net.py to run
as a standalone CLI tool. It handles commands like list, create, delete,
rename, and save operations for nets.
"""
from __future__ import annotations

import sys
import json
import traceback
from typing import Any

from .net import Net


# ----------------------------- JSON-only CLI -----------------------------

def _stdout_json(obj: Any) -> None:
    # ALWAYS print a single JSON value to stdout (no logs/traces here)
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _cmd_list() -> None:
    # TUI uses the "saved" list on the box
    _stdout_json(Net.list_saved())


def _cmd_delete(name: str) -> None:
    ok = Net.delete_local_net(name)
    _stdout_json({"ok": bool(ok)})


def _cmd_delete_all() -> None:
    ok = Net.delete_all_local_nets()
    _stdout_json({"ok": bool(ok)})


def _cmd_rename(old: str, new: str) -> None:
    ok = Net.rename_local_net(old, new)
    _stdout_json({"ok": bool(ok)})


def _cmd_save(payload: str) -> None:
    """Save a single net from JSON payload"""
    try:
        data = json.loads(payload)
        Net.save_local_net(data)
        _stdout_json({"ok": True})
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _stdout_json({"ok": False, "error": str(e)})


def _cmd_save_batch(payload: str) -> None:
    """Save multiple nets from JSON array payload"""
    try:
        nets = json.loads(payload)
        if not isinstance(nets, list):
            raise ValueError("save-batch requires a JSON array of net definitions")

        saved_count = 0
        for net_data in nets:
            try:
                Net.save_local_net(net_data)
                saved_count += 1
            except Exception as e:
                traceback.print_exc(file=sys.stderr)

        _stdout_json({"ok": True, "count": saved_count})
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        _stdout_json({"ok": False, "error": str(e)})


def _cli():
    """
    Minimal CLI for box backend. Outputs ONLY JSON on stdout.
    Any diagnostics go to stderr via traceback or prints.
    """
    try:
        args = sys.argv[1:]
        cmd = args[0] if args else "list"

        if cmd == "list":
            _cmd_list()
        elif cmd == "save":
            if len(args) < 2:
                raise ValueError("save requires JSON payload")
            _cmd_save(args[1])
        elif cmd == "save-batch":
            if len(args) < 2:
                raise ValueError("save-batch requires JSON payload")
            _cmd_save_batch(args[1])
        elif cmd == "delete":
            if len(args) < 2:
                raise ValueError("delete requires NETNAME")
            _cmd_delete(args[1])
        elif cmd == "delete-all":
            _cmd_delete_all()
        elif cmd == "rename":
            if len(args) < 3:
                raise ValueError("rename requires OLD_NAME NEW_NAME")
            _cmd_rename(args[1], args[2])
        else:
            # unknown command → never break the TUI
            _stdout_json([])

    except SystemExit:
        # honor exits, but keep stdout valid JSON
        _stdout_json([])

    except Exception:
        traceback.print_exc(file=sys.stderr)
        _stdout_json([])


if __name__ == "__main__":
    _cli()

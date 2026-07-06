# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""General box-control PRIMITIVES — arbitrary command execution and file I/O.

These are the workhorses an AI composes to repair the box's test environment
when a lager test reveals an infrastructure problem: run a shell / lager CLI
command, read a file, edit a file, list a directory. Together they cover
arbitrary recovery (restart a service, reinstall a dependency, fix a config or
source file, power-cycle hardware via the CLI, ...) without us pre-enumerating
fault-specific tools.

This is a deliberately powerful, NON-read-only surface, so it sits behind its
own gate (``LAGER_MCP_ALLOW_EXEC``, default off — see
``config.exec_tools_enabled``) separate from the scoped control helpers, and it
is registered only when an operator opts in (the gated import in ``server.py``).

Every tool does one thing, returns structured JSON (errors as ``{"error": ...}``
rather than raised exceptions), and logs one audit line before acting.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)

# Cap captured output / file bodies so a runaway command or huge file can't
# blow up the MCP response.
_MAX_OUTPUT_BYTES = 8192
_DEFAULT_READ_BYTES = 65536


def _tail(text: str, limit: int = _MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Return (possibly-truncated text, truncated?) keeping the tail."""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def box_exec(command: str, timeout_s: int = 60, cwd: str | None = None) -> str:
    """Run an arbitrary shell command on the box and capture its result.

    The general-purpose recovery primitive: lager CLI, ``systemctl``/``docker``
    restarts, ``pip install``, ``git``, file surgery — anything the failing test
    output warrants. Runs through the shell; blocks up to ``timeout_s`` seconds.

    Args:
        command: Shell command line to execute.
        timeout_s: Hard timeout in seconds (default 60).
        cwd: Optional working directory.

    Returns JSON: {command, exit_code, stdout, stderr, truncated, timed_out}.
    """
    logger.info("box_exec: %r (timeout=%ss, cwd=%r)", command, timeout_s, cwd)
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        out, out_trunc = _tail((exc.stdout or b"").decode("utf-8", errors="replace"))
        err, err_trunc = _tail((exc.stderr or b"").decode("utf-8", errors="replace"))
        return json.dumps({
            "command": command,
            "exit_code": None,
            "stdout": out,
            "stderr": err,
            "truncated": out_trunc or err_trunc,
            "timed_out": True,
        })
    except Exception as exc:
        return json.dumps({"error": f"box_exec failed to launch: {exc}"})

    out, out_trunc = _tail((proc.stdout or b"").decode("utf-8", errors="replace"))
    err, err_trunc = _tail((proc.stderr or b"").decode("utf-8", errors="replace"))
    return json.dumps({
        "command": command,
        "exit_code": proc.returncode,
        "stdout": out,
        "stderr": err,
        "truncated": out_trunc or err_trunc,
        "timed_out": False,
    })


def read_file(path: str, max_bytes: int = _DEFAULT_READ_BYTES) -> str:
    """Read a file on the box (for inspecting config, source, logs).

    Args:
        path: Absolute or box-relative file path.
        max_bytes: Cap on how many bytes to return (default 64 KiB).

    Returns JSON: {path, content, bytes, truncated} or {"error": ...}.
    """
    logger.info("read_file: %r (max_bytes=%s)", path, max_bytes)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(max_bytes + 1)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as exc:
        return json.dumps({"error": f"Cannot read '{path}': {exc}"})

    truncated = len(data) > max_bytes
    content = data[:max_bytes] if truncated else data
    return json.dumps({
        "path": path,
        "content": content,
        "bytes": len(content),
        "truncated": truncated,
    })


def write_file(path: str, content: str) -> str:
    """Atomically write a file, backing up any prior version.

    Writes to a temp file then ``os.replace`` for atomicity (mirrors
    ``lager.nets.net._atomic_write_json``). If the file already exists, its
    prior contents are saved to a timestamped ``.bak`` and a unified diff is
    returned for the audit trail.

    Args:
        path: Destination file path.
        content: Full new file contents.

    Returns JSON: {path, bytes_written, backup, diff} or {"error": ...}.
    """
    logger.info("write_file: %r (%d bytes)", path, len(content))
    backup: str | None = None
    old = ""
    tmp: str | None = None
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                old = fh.read()
            backup = f"{path}.{int(time.time())}.bak"
            with open(backup, "w", encoding="utf-8") as fh:
                fh.write(old)

        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
        tmp = None
    except (PermissionError, IsADirectoryError, OSError) as exc:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return json.dumps({"error": f"Cannot write '{path}': {exc}"})

    diff = "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"{path} (old)",
            tofile=f"{path} (new)",
        )
    )
    diff_text, diff_trunc = _tail(diff)
    return json.dumps({
        "path": path,
        "bytes_written": len(content),
        "backup": backup,
        "diff": diff_text,
        "diff_truncated": diff_trunc,
    })


def list_dir(path: str) -> str:
    """List the entries of a directory on the box.

    Args:
        path: Directory path to list.

    Returns JSON: {path, entries:[{name, type, size}]} or {"error": ...}.
    """
    logger.info("list_dir: %r", path)
    try:
        entries = []
        with os.scandir(path) as it:
            for entry in it:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = None
                kind = (
                    "dir" if entry.is_dir(follow_symlinks=False)
                    else "symlink" if entry.is_symlink()
                    else "file"
                )
                entries.append({"name": entry.name, "type": kind, "size": size})
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        return json.dumps({"error": f"Cannot list '{path}': {exc}"})

    entries.sort(key=lambda e: e["name"])
    return json.dumps({"path": path, "entries": entries})


def register(mcp) -> None:
    """Register the general box-control primitives on *mcp*.

    Called from ``server.py`` only when ``LAGER_MCP_ALLOW_EXEC`` is set, so the
    default tool surface stays read-only.
    """
    mcp.add_tool(box_exec)
    mcp.add_tool(read_file)
    mcp.add_tool(write_file)
    mcp.add_tool(list_dir)

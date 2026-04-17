# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Cross-platform process introspection utilities.

The Lager box needs to read process command lines (to inspect J-Link debugger
state) and find processes by environment variable (to kill Python scripts by
their LAGER_PROCESS_ID). On Linux this was done by reading /proc/{pid}/cmdline
and /proc/*/environ directly. macOS has no /proc filesystem, so we use psutil
on both platforms for a single code path.

If psutil is not installed, the module falls back to /proc on Linux and to
`ps` on macOS. The psutil path is preferred because it handles edge cases
(zombie processes, permission errors) more robustly.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_IS_LINUX = sys.platform.startswith("linux")

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


def get_process_cmdline(pid: int) -> List[str]:
    """Return the command-line arguments of a process as a list of strings.

    Returns an empty list if the process doesn't exist or can't be read.
    """
    if _HAS_PSUTIL:
        try:
            return psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return []
        except Exception:
            return []

    # Fallback: /proc on Linux
    if _IS_LINUX:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return [part.decode(errors="replace") for part in f.read().split(b"\x00") if part]
        except (OSError, IOError):
            return []

    # Fallback: ps on macOS
    import subprocess
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()
    except Exception:
        pass
    return []


def find_processes_by_env(env_key: str, env_value: Optional[str] = None) -> List[Dict]:
    """Find processes that have a specific environment variable set.

    Args:
        env_key: Environment variable name to search for (e.g. 'LAGER_PROCESS_ID')
        env_value: If provided, only match processes where the env var equals this value.
                   If None, match any process that has the env var set.

    Returns:
        List of dicts with keys: 'pid', 'cmdline' (list[str]), 'env_value' (str)
    """
    if _HAS_PSUTIL:
        return _find_by_env_psutil(env_key, env_value)
    if _IS_LINUX:
        return _find_by_env_proc(env_key, env_value)
    # No fallback on macOS without psutil — ps doesn't show env vars.
    logger.warning(
        "psutil is not installed; cannot search processes by environment variable on macOS. "
        "Install psutil: pip install psutil"
    )
    return []


def _find_by_env_psutil(env_key: str, env_value: Optional[str]) -> List[Dict]:
    results: List[Dict] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            env = proc.environ()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except Exception:
            continue
        val = env.get(env_key)
        if val is None:
            continue
        if env_value is not None and val != env_value:
            continue
        try:
            cmdline = proc.cmdline()
        except Exception:
            cmdline = []
        results.append({"pid": proc.pid, "cmdline": cmdline, "env_value": val})
    return results


def _find_by_env_proc(env_key: str, env_value: Optional[str]) -> List[Dict]:
    import glob
    search_prefix = f"{env_key}=".encode()
    if env_value is not None:
        search_exact = f"{env_key}={env_value}".encode()
    else:
        search_exact = None

    results: List[Dict] = []
    for environ_path in glob.glob("/proc/*/environ"):
        try:
            pid = int(environ_path.split("/")[2])
        except (ValueError, IndexError):
            continue
        if pid == os.getpid():
            continue
        try:
            with open(environ_path, "rb") as f:
                environ_data = f.read()
        except (OSError, PermissionError):
            continue

        if search_exact is not None:
            if search_exact not in environ_data:
                continue
            matched_value = env_value
        else:
            if search_prefix not in environ_data:
                continue
            # Extract the value
            for chunk in environ_data.split(b"\x00"):
                if chunk.startswith(search_prefix):
                    matched_value = chunk[len(search_prefix):].decode(errors="replace")
                    break
            else:
                continue

        cmdline = get_process_cmdline(pid)
        results.append({"pid": pid, "cmdline": cmdline, "env_value": matched_value})

    return results


def kill_process_tree(pid: int, sig: int = signal.SIGTERM) -> bool:
    """Send a signal to a process. Returns True if the signal was sent."""
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False

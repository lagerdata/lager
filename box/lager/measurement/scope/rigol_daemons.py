# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
One oscilloscope daemon per Rigol scope, started on demand.

The daemon is the only process that talks to the scope: it holds the USB-TMC
interface and the HDMI capture card for as long as it runs, and everything
else -- the CLI, the web UI, Python scripts, the MCP server -- goes through
it. A box can have several Rigols, and a PicoScope besides, so a Rigol's
daemon is not the box's fixed one on :8085: it is started for that scope's
VISA address the first time something asks for it, on a port of its own, and
found again by address after that.

Keyed by address rather than by net because a scope's channels are separate
nets, and all of them are the same instrument.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import socket
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)

DAEMON_BINARY_ENV = "LAGER_SCOPE_DAEMON_BIN"
DEFAULT_DAEMON_BINARY = "/usr/local/bin/oscilloscope-daemon"

# The daemon listens once the scope has opened, or after its own 10 s
# OPEN_TIMEOUT. An open that pairs a capture card takes 4.5-6 s.
START_TIMEOUT_SECONDS = 12.0
# Inside the daemon's own five seconds to stop and close the scope.
STOP_TIMEOUT_SECONDS = 7.0


class RigolDaemonError(RuntimeError):
    """The daemon for a Rigol could not be started."""


def state_dir() -> str:
    path = os.path.join(tempfile.gettempdir(), "lager-scope-daemons")
    os.makedirs(path, exist_ok=True)
    return path


def _slug(address: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", address)


def _paths(address: str) -> dict:
    base = os.path.join(state_dir(), _slug(address))
    return {
        "state": base + ".json",
        "lock": base + ".lock",
        "log": base + ".log",
        "socket": base + ".sock",
    }


def capture_spec(rec: dict) -> str | None:
    """Where a net's capture card is, as the daemon takes it, or None.

    A net names its card by USB port, because two capture cards of one model
    are indistinguishable otherwise and a ``/dev/videoN`` number moves when
    devices are plugged in a different order.
    """
    capture = (rec or {}).get("capture")
    if not isinstance(capture, dict):
        return None
    if capture.get("port_path"):
        return "usb:%s" % capture["port_path"]
    if capture.get("device"):
        return str(capture["device"])
    return None


def capture_for_address(address: str, nets=None) -> str | None:
    """The capture card of whichever of this scope's nets names one."""
    if nets is None:
        from lager.nets.net import Net
        nets = Net.get_local_nets()
    for rec in nets:
        if rec.get("role") not in ("scope", "scope-channel", "analog", "logic"):
            continue
        if _address_of(rec) != address:
            continue
        spec = capture_spec(rec)
        if spec:
            return spec
    return None


def _address_of(rec: dict):
    for mapping in rec.get("mappings") or []:
        if mapping.get("device_override"):
            return mapping["device_override"]
    return rec.get("address")


def _url(port: int) -> str:
    # The trailing slash is required; see daemon_client.daemon_url.
    return "ws://127.0.0.1:%d/" % port


def _read_state(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def _is_daemon(pid) -> bool:
    """Whether ``pid`` is a live oscilloscope daemon.

    The command line is checked as well as the PID: state files live in /tmp,
    which outlives the container, and after a restart the PID can belong to
    something else entirely.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            cmdline = f.read()
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return False
        # No procfs, as on a laptop running the tests.
        try:
            cmdline = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, timeout=2.0).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    except OSError:
        return False
    return b"oscilloscope-daemon" in cmdline


def _listening(port, timeout: float = 0.5) -> bool:
    if not isinstance(port, int):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _locked(path: str):
    with open(path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _stop(pid: int) -> None:
    """Stop a daemon, giving it time to stop and close the scope first."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not _is_daemon(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def _spawn(binary: str, env: dict, log_path: str) -> int:
    """Start the daemon detached, so it outlives whichever process asked.

    Through ``sh -c '... &'``: the shell exits at once and the daemon is
    reparented, so it never lingers as a zombie of a long-running caller.
    """
    script = 'exec "$0" >>"$1" 2>&1 </dev/null & echo $!'
    out = subprocess.run(
        ["/bin/sh", "-c", script, binary, log_path],
        env=env, capture_output=True, text=True, timeout=5.0, check=True,
        start_new_session=True,
    )
    return int(out.stdout.strip())


def ensure(address: str, video: str | None = None) -> str:
    """WebSocket URL of the daemon driving the Rigol at ``address``.

    Starts one if none is running, and restarts it if it was started with a
    different capture card, so a net edited to name its card takes effect on
    the next command rather than at the next box restart.
    """
    if not address:
        raise RigolDaemonError(
            "the scope net names no instrument address; recreate it with "
            "`lager nets add-all` or set its address")
    paths = _paths(address)
    with _locked(paths["lock"]):
        state = _read_state(paths["state"]) or {}
        running = _is_daemon(state.get("pid"))
        if running and state.get("video") == video and _listening(state.get("port")):
            return _url(state["port"])
        if running:
            logger.info("restarting the scope daemon for %s: capture card %s -> %s",
                        address, state.get("video"), video)
            _stop(state["pid"])

        binary = os.environ.get(DAEMON_BINARY_ENV, DEFAULT_DAEMON_BINARY)
        if not os.access(binary, os.X_OK):
            raise RigolDaemonError(
                "the oscilloscope daemon is not installed at %s; run `lager update` "
                "to build it" % binary)
        port = _free_port()
        env = dict(os.environ)
        env.update({
            "LAGER_SCOPE_SCPI": address,
            "LAGER_SCOPE_DATA_PORT": str(port),
            "LAGER_SCOPE_SOCKET": paths["socket"],
            "LAGER_SCOPE_LOG": env.get("LAGER_SCOPE_LOG", "info"),
        })
        if video:
            env["LAGER_SCOPE_VIDEO"] = video
        else:
            env.pop("LAGER_SCOPE_VIDEO", None)
        pid = _spawn(binary, env, paths["log"])
        _write_state(paths["state"], {
            "pid": pid,
            "port": port,
            "address": address,
            "video": video,
            "socket": paths["socket"],
        })
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _listening(port, timeout=0.2):
                logger.info("started the scope daemon for %s on port %d (pid %d)",
                            address, port, pid)
                return _url(port)
            if not _is_daemon(pid):
                break
            time.sleep(0.1)
        raise RigolDaemonError(
            "the oscilloscope daemon for %s did not start; see %s"
            % (address, paths["log"]))


def stop(address: str) -> bool:
    """Stop the daemon for ``address``, if one is running."""
    paths = _paths(address)
    with _locked(paths["lock"]):
        state = _read_state(paths["state"]) or {}
        if not _is_daemon(state.get("pid")):
            return False
        _stop(state["pid"])
        with contextlib.suppress(OSError):
            os.remove(paths["state"])
        return True

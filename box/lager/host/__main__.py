# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Command line for the native host agent.

``python -m lager.host run``     supervise the host services (what the
                                 service manager starts)
``python -m lager.host init``    create the endpoint record, if absent
``python -m lager.host worker``  the hardware worker (started by ``run``)
``python -m lager.host debug``   the debug service (started by ``run``)

The supervisor exists for the same reason the container's start script wraps
every service in a restart loop: the hardware service recovers from a wedged
USB session by exiting, and something has to start it again.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time

from lager.hostlink import config

logger = logging.getLogger("lager.host")

RESTART_DELAY_S = 2.0
STOP_GRACE_S = 10.0
# Path of a natively built oscilloscope daemon; without it none is run.
SCOPE_DAEMON_ENV = "LAGER_HOST_SCOPE_DAEMON"


def _endpoint_file(config_dir: str) -> str:
    return os.path.join(config_dir, config.ENDPOINT_FILENAME)


def cmd_init(args) -> int:
    # 0700: the endpoint record holds the link token.
    os.makedirs(args.config_dir, mode=0o700, exist_ok=True)
    endpoint = config.ensure_endpoint(_endpoint_file(args.config_dir), port=args.port)
    print(f"Host agent endpoint: {endpoint.bind}:{endpoint.port} "
          f"(record: {_endpoint_file(args.config_dir)})")
    return 0


def cmd_worker(args) -> int:
    from . import worker

    worker.serve(args.config_dir)
    return 0


def cmd_debug(args) -> int:
    from . import darwin_debug

    darwin_debug.serve(config.HOST_DEBUG_PORT)
    return 0


class _Child:
    """One supervised process, restarted whenever it exits."""

    def __init__(self, name, command, env=None):
        self.name = name
        self.command = command
        self.env = env
        self.process = None

    def run(self, stopping: threading.Event) -> None:
        while not stopping.is_set():
            logger.info("Starting %s", self.name)
            try:
                self.process = subprocess.Popen(self.command, env=self.env)
            except OSError as exc:
                logger.error("Cannot start %s: %s", self.name, exc)
                return
            code = self.process.wait()
            if stopping.is_set():
                break
            logger.warning("%s exited with status %s; restarting in %.0fs",
                           self.name, code, RESTART_DELAY_S)
            stopping.wait(RESTART_DELAY_S)

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            logger.warning("%s did not stop; killing it", self.name)
            self.process.kill()


def _children(args):
    me = [sys.executable, "-m", "lager.host"]
    children = [
        _Child("hardware worker", me + ["worker", "--config-dir", args.config_dir]),
        _Child("debug service", me + ["debug"]),
    ]
    scope_daemon = args.scope_daemon or os.environ.get(SCOPE_DAEMON_ENV)
    if scope_daemon:
        env = dict(os.environ)
        env["LAGER_SCOPE_DATA_BIND"] = config.DEFAULT_BIND
        env["LAGER_SCOPE_DATA_PORT"] = str(config.HOST_SCOPE_PORT)
        children.append(_Child("oscilloscope daemon", [scope_daemon], env))
    return children


def cmd_run(args) -> int:
    cmd_init(args)
    stopping = threading.Event()
    children = _children(args)

    def _stop(signum, _frame):
        stopping.set()
        for child in children:
            child.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    threads = [threading.Thread(target=child.run, args=(stopping,), name=child.name)
               for child in children]
    for thread in threads:
        thread.start()
    # Wait in slices: a blocking join would hold off the signal handler.
    deadline = None
    while any(thread.is_alive() for thread in threads):
        time.sleep(0.2)
        if stopping.is_set():
            deadline = deadline or time.monotonic() + STOP_GRACE_S
            if time.monotonic() > deadline:
                # A child that ignores the request must not outlive the
                # agent: it would keep its port and block the next start.
                for child in children:
                    child.kill()
    logger.info("Host agent stopped")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lager-host", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", default=None,
                        help="directory holding the endpoint record "
                             f"(default {config.CONTAINER_CONFIG_DIR}); the Box's other "
                             "configuration is always read from the default")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("run", cmd_run), ("init", cmd_init), ("worker", cmd_worker),
                     ("debug", cmd_debug)):
        p = sub.add_parser(name)
        p.add_argument("--config-dir", default=argparse.SUPPRESS)
        if name == "run":
            p.add_argument("--scope-daemon", default=None,
                           help="path of the oscilloscope daemon binary to supervise")
        if name in ("run", "init"):
            p.add_argument("--port", type=int, default=None,
                           help="loopback port (default: pick a free one)")
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    if getattr(args, "config_dir", None) is None:
        args.config_dir = config.CONTAINER_CONFIG_DIR

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Command line for the native host agent.

``python -m lager.host run``     supervise the hardware worker (what the
                                 service manager starts)
``python -m lager.host init``    create the endpoint record, if absent
``python -m lager.host worker``  the worker itself (started by ``run``)

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
import time

from lager.hostlink import config

from . import paths

logger = logging.getLogger("lager.host")

RESTART_DELAY_S = 2.0
STOP_GRACE_S = 10.0


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


def cmd_run(args) -> int:
    cmd_init(args)
    child = None
    stopping = False

    def _stop(signum, _frame):
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    env = dict(os.environ)
    env[paths.CONFIG_DIR_ENV] = args.config_dir
    command = [sys.executable, "-m", "lager.host", "worker", "--config-dir", args.config_dir]
    while not stopping:
        logger.info("Starting hardware worker")
        child = subprocess.Popen(command, env=env)
        code = child.wait()
        if stopping:
            break
        logger.warning("Hardware worker exited with status %s; restarting in %.0fs",
                       code, RESTART_DELAY_S)
        # Sleep in slices so a stop request is not held up by the delay.
        deadline = time.monotonic() + RESTART_DELAY_S
        while not stopping and time.monotonic() < deadline:
            time.sleep(0.1)

    if child is not None and child.poll() is None:
        try:
            child.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            child.kill()
    logger.info("Host agent stopped")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lager-host", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", default=None,
                        help="Box configuration directory on this machine")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("run", cmd_run), ("init", cmd_init), ("worker", cmd_worker)):
        p = sub.add_parser(name)
        p.add_argument("--config-dir", default=argparse.SUPPRESS)
        if name != "worker":
            p.add_argument("--port", type=int, default=None,
                           help="loopback port (default: pick a free one)")
        p.set_defaults(fn=fn)
    args = parser.parse_args(argv)
    if getattr(args, "config_dir", None) is None:
        args.config_dir = paths.default_config_dir()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

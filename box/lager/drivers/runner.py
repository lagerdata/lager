# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
lager.drivers.runner - Run one custom-driver action in its own process.

Each action runs in a fresh child process, so a driver that crashes, hangs
or leaks a port takes only that process with it, and no state carries over
from one call to the next.

The children fork from a multiprocessing forkserver that has already
imported lager. Spawning a new interpreter per call instead cost about 1.6 s
of imports on a box before the driver ran, on every call. The driver's own
driver.py is still imported fresh in each child, after the fork, so a
reinstalled bundle takes effect on the next call.

    request = {"type", "net", "params", "base_nets", "action", "args"}
              (already validated against the manifest by the handler)
    result  = {"ok": true, "value": <the action's return value>}
              {"ok": false, "error": "...", "exc_type": "SerialException"}

The child's stderr is the HTTP server's stderr, so print() output and
self.log lines reach the box log while the action runs. While the driver
runs, sys.stdout points at stderr too.
"""

import json
import logging
import multiprocessing
import os
import sys
import threading
import traceback

from lager.drivers.errors import DriverFailed, DriverTimeout

# Imported once by the forkserver, so each child starts with them loaded.
# lager.nets.net brings in the built-in net classes a base_net() returns.
# A module that fails to import here is skipped and imported by the child.
PRELOAD = ['lager.drivers.loader', 'lager.nets.net', 'serial']

# How long to wait for a killed or finished child to exit.
_REAP_S = 5.0

_ctx = None
_ctx_lock = threading.Lock()


def _context():
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            ctx = multiprocessing.get_context('forkserver')
            ctx.set_forkserver_preload(PRELOAD)
            _ctx = ctx
        return _ctx


def warm():
    """Start the forkserver now, so the first action does not pay for it."""
    from multiprocessing import forkserver
    _context()
    forkserver.ensure_running()


def run(request):
    from lager.drivers import loader

    cls = loader.load_driver_class(request['type'])
    instance = cls(request['net'], params=request.get('params') or {},
                   base_nets=request.get('base_nets') or {})
    method = loader.bound_action(instance, request['action'])
    instance.setup()
    try:
        return method(**(request.get('args') or {}))
    finally:
        instance.teardown()


def _child(conn, request, env, drivers_dir, cwd):
    from lager.drivers import registry

    os.environ.update(env)
    registry.DRIVERS_DIR = drivers_dir
    os.chdir(cwd)
    sys.stdout = sys.stderr
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True,
                        format='[drivers] %(name)s: %(message)s')
    try:
        value = run(request)
        line = json.dumps({'ok': True, 'value': value}, default=str)
    except BaseException as e:  # noqa: B902 - report everything, incl. SystemExit
        traceback.print_exc(file=sys.stderr)
        line = json.dumps({'ok': False, 'error': str(e) or type(e).__name__,
                           'exc_type': type(e).__name__})
    sys.stderr.flush()
    conn.send(line)
    conn.close()


def call(request, env, drivers_dir, cwd, timeout_s):
    """Run one action in a child process; return its result dict.

    Raises DriverTimeout if the action outlives timeout_s (the child is
    killed), or DriverFailed if the child dies without a result.
    """
    ctx = _context()
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(send, request, env, drivers_dir, cwd),
                       name='lager-driver-%s' % request['type'])
    try:
        proc.start()
    finally:
        send.close()
    try:
        if not recv.poll(timeout_s):
            proc.kill()
            raise DriverTimeout("Action '%s' of driver %s did not finish within %d s "
                                "and was stopped" % (request['action'], request['type'],
                                                     timeout_s))
        try:
            line = recv.recv()
        except EOFError:
            line = None
    finally:
        recv.close()
        proc.join(_REAP_S)

    if line is None:
        raise DriverFailed("Driver %s exited (code %s) without a result; see the box log"
                           % (request['type'], proc.exitcode))
    return json.loads(line)

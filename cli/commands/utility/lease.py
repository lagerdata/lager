# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
`lager lease` -- lease any free box from a pool, run a command on it, give it back.
"""
import os
import signal
import subprocess
import threading
import time

import click

from ...errors import LagerError
from ... import lease_client
from ...lease_client import (
    DEFAULT_TTL_SECONDS,
    EXIT_NO_LEASE,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    LeaseClient,
    LeaseEnded,
)

#: How long a command gets to exit after SIGTERM before it is killed, when
#: the lease ends under it.
TERMINATE_GRACE_SECONDS = 10


class LeaseHeartbeat(threading.Thread):
    """Renews the lease every ``interval`` seconds until stopped.

    A failed renewal is not fatal: the lease lives ``ttl`` seconds past the
    last good one, so a short network outage costs nothing. A warning is
    printed once half the TTL has passed without a good renewal. A renewal
    the service refuses because the lease has ended sets ``ended`` and stops.
    """

    def __init__(self, client, lease_id, interval, ttl_seconds):
        super().__init__(daemon=True, name='lager-lease-heartbeat')
        self._client = client
        self._lease_id = lease_id
        self._interval = interval
        self._ttl = ttl_seconds
        self._stop_event = threading.Event()
        self.ended = None
        self._last_ok = time.monotonic()
        self._warned = False

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.wait(self._interval):
            try:
                self._client.heartbeat(self._lease_id)
            except LeaseEnded as exc:
                self.ended = exc.end_reason
                return
            except LagerError as exc:
                unrenewed = time.monotonic() - self._last_ok
                if not self._warned and unrenewed >= self._ttl / 2:
                    self._warned = True
                    click.secho(
                        f'Warning: no renewal of the lease in the last {int(unrenewed)} s '
                        f'(it ends after {self._ttl} s): {exc.problem}',
                        fg='yellow', err=True,
                    )
                continue
            self._last_ok = time.monotonic()
            self._warned = False


def _exit_status(returncode):
    """A child's status as a shell reports it: 128+N for death by signal N."""
    return 128 - returncode if returncode < 0 else returncode


def _forwarded_signals():
    # SIGINT is not forwarded: Ctrl-C at a terminal already reaches the
    # command, which is in the same process group; a second copy would make
    # pytest and similar tools abort without cleaning up.
    return [s for s in (getattr(signal, 'SIGTERM', None), getattr(signal, 'SIGHUP', None)) if s]


def run_under_lease(client, pool, command, *, ttl_seconds=DEFAULT_TTL_SECONDS,
                    context=None, popen=subprocess.Popen):
    """Lease a box from ``pool``, run ``command`` on it, release. Returns the exit status."""
    lease = client.acquire(pool, ttl_seconds=ttl_seconds, context=context)
    box = lease.get('box') or {}
    lease_id = lease['id']
    box_label = box.get('name') or box.get('ipAddress')
    click.secho(f'Leased box {box_label} ({box.get("ipAddress")}) from pool {pool!r}.',
                fg='green', err=True)

    env = dict(os.environ)
    env['LAGER_BOX'] = box['ipAddress']
    env['LAGER_LOCK_HOLDER'] = lease['holder']
    env['LAGER_LEASE_ID'] = lease_id

    interval = lease.get('heartbeatIntervalSeconds') or max(10, ttl_seconds // 3)
    heartbeat = LeaseHeartbeat(client, lease_id, interval, lease.get('ttlSeconds') or ttl_seconds)

    previous = {}
    child = None
    try:
        try:
            child = popen(list(command), env=env)
        except OSError as exc:
            raise LagerError(f'Cannot start {command[0]!r}.', cause=str(exc),
                             exit_code=127) from exc

        def forward(signum, _frame):
            if child.poll() is None:
                child.send_signal(signum)

        for signum in _forwarded_signals():
            previous[signum] = signal.signal(signum, forward)
        previous[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)

        heartbeat.start()
        while True:
            try:
                returncode = child.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            if heartbeat.ended:
                _stop_child(child)
                raise LagerError(
                    f'The lease on box {box_label} ended before the command stopped.',
                    cause=f'Reason: {heartbeat.ended}. Another job can now use the box, '
                          'so lager stopped the command.',
                    exit_code=EXIT_NO_LEASE,
                )
        if heartbeat.ended:
            raise LagerError(
                f'The lease on box {box_label} ended before the command finished.',
                cause=f'Reason: {heartbeat.ended}.',
                exit_code=EXIT_NO_LEASE,
            )
        return _exit_status(returncode)
    finally:
        heartbeat.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            _stop_child(child)
        _release(client, lease_id, box_label, lease.get('ttlSeconds') or ttl_seconds)


def _stop_child(child):
    child.terminate()
    try:
        child.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _release(client, lease_id, box_label, ttl_seconds):
    try:
        client.release(lease_id)
    except LagerError as exc:
        click.secho(
            f'Warning: lager did not release box {box_label}: {exc.problem} '
            f'It is released automatically within {ttl_seconds} s.',
            fg='yellow', err=True,
        )
        return
    click.secho(f'Released box {box_label}.', err=True)


class _LeaseCommand(click.Command):
    def format_usage(self, ctx, formatter):
        formatter.write_usage(ctx.command_path, '[OPTIONS] -- COMMAND [ARGS]...')


def _make_client(server, org):
    return LeaseClient(lease_client.resolve_server(server), org=org)


@click.command(
    'lease',
    cls=_LeaseCommand,
    context_settings={'ignore_unknown_options': True, 'allow_interspersed_args': False},
    epilog=(
        'Exit status: the exit status of COMMAND. 75 if lager cannot lease a box '
        '(COMMAND does not run), or if the lease ends while COMMAND runs.'
    ),
)
@click.option('--pool', required=True, metavar='NAME',
              help='Name of the pool. Get the name from the Pools page in Stout.')
@click.option('--ttl', 'ttl_seconds', type=click.IntRange(MIN_TTL_SECONDS, MAX_TTL_SECONDS),
              default=DEFAULT_TTL_SECONDS, show_default=True, metavar='SECONDS',
              help='Time that the lease stays active after the last renewal.')
@click.option('--context', 'context_pairs', multiple=True, metavar='KEY=VALUE',
              help='Information that Stout shows with the lease, for example the '
                   'URL of the CI run. Use up to 10 times.')
@click.option('--server', metavar='URL',
              help='Lease server. Default: STOUT_URL, or the server of your one '
                   '`lager login` session.')
@click.option('--org', metavar='ORG',
              help='Organization (id, slug or name), if your account is in more than one.')
@click.argument('command', nargs=-1, required=True, type=click.UNPROCESSED)
@click.pass_context
def lease(ctx, pool, ttl_seconds, context_pairs, server, org, command):
    """Lease an available box from a pool, and run COMMAND on that box.

    \b
    Stout finds a box in the pool that is online and not in use, and locks
    it for this run. COMMAND runs with LAGER_BOX set to that box, so every
    lager command in it uses the leased box. Do not pass --box to them.
    While COMMAND runs, lager renews the lease. When COMMAND stops, for any
    reason, lager releases the box.

    \b
    Credentials: STOUT_TOKEN (for CI), or your `lager login` session.

    \b
    Examples:
      lager lease --pool nrf52 -- lager python flash_and_test.py
      lager lease --pool nrf52 --ttl 600 -- pytest tests/hardware
    """
    context = lease_client.parse_context(context_pairs)
    client = _make_client(server, org)
    ctx.exit(run_under_lease(client, pool, command, ttl_seconds=ttl_seconds, context=context))

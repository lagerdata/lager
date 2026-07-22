# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.errors

    User-facing error presentation for the Lager CLI.

    A new user who hits an error should see two things: *what went wrong*
    and *what to do about it*. This module is the one place that turns a
    failure into that shape.

    The unit is :class:`LagerError` — a structured, actionable error with a
    one-line ``problem`` headline, an optional ``cause`` explanation, and a
    list of concrete ``fixes`` (the next commands/steps to try). Because it
    subclasses ``click.ClickException``, simply ``raise``-ing one anywhere in
    a command gives the styled output for free; Click catches it, calls
    :meth:`LagerError.show`, and exits with ``exit_code``.

    For raw failures that bubble up from libraries (a dropped box connection,
    a USB errno), use the classifiers — :func:`connection_error` and
    :func:`system_error` — to translate them into a ``LagerError`` instead of
    dumping a Python traceback at the user. The full traceback is never lost:
    it is one ``--debug`` / ``LAGER_DEBUG=1`` away.
"""
import os
import sys

import click


def _debug_enabled():
    """True when the user asked for raw/technical output.

    Checks both the ``--debug`` flag (which may not have been parsed yet when
    a failure happens very early) and the ``LAGER_DEBUG`` env var, so the
    escape hatch works no matter where in the lifecycle the error occurs.
    """
    if os.environ.get('LAGER_DEBUG'):
        return True
    # `--debug` is a flag on the root group; sniff argv directly so the
    # top-level handler can honor it before/independent of Click parsing.
    return '--debug' in sys.argv


def render_error(problem, cause=None, fixes=None, *, raw=None, debug_hint=True):
    """Build the styled, multi-line error string written to stderr.

    Layout::

        Error: <problem>                         (red, bold)

          <cause>                                (optional explanation)

          Try:                                   (only when fixes given)
            → <fix 1>
            → <fix 2>

          Run with --debug for the full ...      (dim hint, see below)

    ``raw`` is the original exception/text. It is appended verbatim only
    when debug output is enabled; otherwise a one-line hint points the user
    at ``--debug`` (suppressed when ``debug_hint`` is False or there's
    nothing more to show).
    """
    lines = [click.style(f'Error: {problem}', fg='red', bold=True)]

    if cause:
        lines.append('')
        lines.append(f'  {cause}')

    if fixes:
        lines.append('')
        lines.append('  Try:')
        for fix in fixes:
            lines.append(f'    → {fix}')

    if raw is not None and _debug_enabled():
        lines.append('')
        lines.append(click.style('  --- raw error ---', dim=True))
        for raw_line in str(raw).splitlines() or ['']:
            lines.append(f'  {raw_line}')
    elif debug_hint and raw is not None:
        lines.append('')
        lines.append(click.style(
            '  Run with --debug (or LAGER_DEBUG=1) for the full technical details.',
            dim=True,
        ))

    return '\n'.join(lines)


class LagerError(click.ClickException):
    """An actionable, user-facing error.

    Raise this from any command to show a clean "problem + how to fix"
    message and exit. Prefer it over ``click.echo(..., err=True)`` +
    ``ctx.exit(1)`` so the styling and ``--debug`` behavior stay consistent.

    Args:
        problem:   One-line statement of what went wrong (the headline).
        cause:     Optional sentence explaining *why* it happened.
        fixes:     Optional list of concrete next steps (commands to run).
        exit_code: Process exit status (default 1).
        raw:       The underlying exception/text, shown only under --debug.
    """

    def __init__(self, problem, *, cause=None, fixes=None, exit_code=1, raw=None):
        super().__init__(problem)
        self.problem = problem
        self.cause = cause
        self.fixes = list(fixes) if fixes else []
        self.exit_code = exit_code
        self.raw = raw

    def format_message(self):
        # Used by Click in a few code paths that stringify the exception.
        return render_error(self.problem, self.cause, self.fixes, raw=self.raw)

    def show(self, file=None):
        # Click calls this in standalone mode; write the full styled block.
        click.echo(self.format_message(), err=True)

    def die(self):
        """Print this error and exit via ``sys.exit``.

        Use instead of ``raise`` when the call site sits inside a broad
        ``except Exception`` (e.g. the SSH subprocess handlers) that would
        otherwise swallow the exception. ``SystemExit`` is a ``BaseException``,
        so it passes straight through those handlers.
        """
        self.show()
        sys.exit(self.exit_code)


def die(problem, *, cause=None, fixes=None, exit_code=1, raw=None):
    """Print an actionable error to stderr and exit the process.

    A convenience for the many call sites that today do a red ``secho``
    followed by ``ctx.exit(1)``. Equivalent to raising :class:`LagerError`,
    but usable where raising would be awkward (e.g. deep in a callback).
    """
    click.echo(render_error(problem, cause, fixes, raw=raw), err=True)
    sys.exit(exit_code)


# --------------------------------------------------------------------------
# Classifiers: turn raw library failures into actionable LagerErrors.
# --------------------------------------------------------------------------

def is_connection_error(exc):
    """True when ``exc`` is a network connection/timeout failure.

    Recognizes both the Python built-ins (``ConnectionError``,
    ``TimeoutError`` and their subclasses) and the ``requests``/``urllib3``
    connection and timeout exceptions, without importing those libraries.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    module = type(exc).__module__ or ''
    if 'requests' in module or 'urllib3' in module:
        name = type(exc).__name__
        return 'Connection' in name or 'Timeout' in name
    return False


def connection_error(exc, host=None):
    """Translate a ``requests``/socket connection failure into a LagerError.

    Distinguishes the failure modes a new user actually hits — service
    down, bad hostname, no network route, timeout — and gives each a
    specific next step. Consolidates the branching that used to live
    inline in ``lager hello``.

    Args:
        exc:  The caught exception (requests ConnectionError/Timeout, OSError).
        host: The box IP or name we were trying to reach, if known.
    """
    text = str(exc).lower()
    box = host or '[BOX_NAME]'
    at = f' at {host}' if host else ''

    # Timeout: connected (or tried to) but no response in time.
    is_timeout = (
        'timed out' in text or 'timeout' in text
        or exc.__class__.__name__ == 'Timeout'
    )
    if is_timeout and 'refused' not in text:
        return LagerError(
            f'Timed out connecting to the box{at}.',
            cause='The box did not respond in time — it may be slow, overloaded, or still starting up.',
            fixes=[
                'Wait a few seconds and try again.',
                f'Confirm it is reachable: lager hello {box}'.rstrip(),
            ],
            raw=exc,
        )

    if 'connection refused' in text or 'errno 111' in text or 'errno 61' in text:
        return LagerError(
            f'Connection refused — the box{at} is reachable but the Lager service is not responding.',
            cause='The Lager service (Docker container) is probably not running on the box.',
            fixes=[
                f'Check the box: lager hello {box}'.rstrip(),
                f'Restart the service: lager ssh {box} then sudo docker restart lager',
            ],
            raw=exc,
        )

    if ('name or service not known' in text
            or 'nodename nor servname' in text
            or 'failed to resolve' in text
            or 'name resolution' in text):
        return LagerError(
            f'Could not resolve "{host}".' if host else 'Could not resolve the box hostname.',
            cause='The box name or address could not be looked up.',
            fixes=[
                'Check the spelling of the box name or IP.',
                'List your saved boxes: lager boxes',
                'If using a Tailscale/VPN hostname, make sure the VPN is connected.',
            ],
            raw=exc,
        )

    if 'no route to host' in text or 'network is unreachable' in text:
        return LagerError(
            f'No network route to the box{at}.',
            cause='The box is on a network this machine cannot currently reach.',
            fixes=[
                'Confirm the box is powered on and on the same network.',
                'If the box is remote, connect your VPN/Tailscale and retry.',
            ],
            raw=exc,
        )

    # Generic fallback: still better than a bare traceback.
    return LagerError(
        f'Could not connect to the box{at}.',
        cause='The box may be offline, on a different network, or unreachable from here.',
        fixes=[
            f'Verify it is online: lager hello {box}'.rstrip(),
            'Check your network/VPN connection and the box IP (lager boxes).',
        ],
        raw=exc,
    )


def net_not_specified_error(net_label, command, *, default_flag=None):
    """No net was given and no default is configured for an instrument command.

    The net analog of ``box_storage.box_not_found_error``. Listing the nets
    requires a live box connection, so instead of listing them we point the
    user at the three ways forward: name it, list them, or set a default.

    Args:
        net_label:    Human label, e.g. ``'I2C'`` / ``'SPI'`` / ``'UART'``.
        command:      The CLI command, e.g. ``'i2c'`` (used in the example).
        default_flag: ``lager defaults add`` flag if one exists for this net
                      (e.g. ``'uart-net'``); omit for nets with no such flag.
    """
    fixes = [
        f'Name the net as the first argument: lager {command} [NET_NAME] ...',
        'See the nets on your box: lager nets',
    ]
    if default_flag:
        fixes.append(f'Or set a default: lager defaults add --{default_flag} [NET_NAME]')
    return LagerError(
        f'No {net_label} net specified, and no default is set.',
        cause='Lager needs to know which net to use for this command.',
        fixes=fixes,
    )


def ssh_error(stderr, ip, user=None):
    """Translate the stderr of a failed ``ssh`` subprocess into a LagerError.

    The same handful of SSH failure modes (key not authorized, refused, no
    route, bad hostname, changed host key) recur across `lager logs`,
    `install`, and `uninstall`. This is the single place that maps them to
    actionable guidance. ``user`` is the box's SSH user when the caller
    knows it (boxes with custom users); it only refines the manual
    ssh-copy-id fix text.

    Note callers inside a broad ``except Exception`` should use
    ``ssh_error(...).die()`` rather than ``raise`` — see :meth:`LagerError.die`.
    """
    text = (stderr or '').lower()

    if 'permission denied' in text or 'publickey' in text:
        return LagerError(
            'SSH key authentication failed — the box rejected your key.',
            cause='Your SSH key has not been authorized on this box yet.',
            fixes=[
                f'Authorize it (enter the box password once): lager ssh-setup --box {ip}',
                f'Or manually: ssh-copy-id {user or "lagerdata"}@{ip}',
                'Then re-run this command.',
            ],
            raw=stderr or None,
        )

    if 'connection refused' in text:
        return LagerError(
            f'SSH connection refused by {ip} on port 22.',
            cause='The box is reachable, but its SSH service is not accepting connections.',
            fixes=[
                'Give the box a moment to finish booting, then retry.',
                'Confirm SSH is running and port 22 is not firewalled.',
            ],
            raw=stderr or None,
        )

    if 'no route to host' in text or 'network is unreachable' in text:
        return LagerError(
            f'No network route to {ip}.',
            cause='The box is on a network this machine cannot currently reach.',
            fixes=['Confirm the box is powered on and on the same network (or connect your VPN).'],
            raw=stderr or None,
        )

    if ('could not resolve' in text
            or 'name or service not known' in text
            or 'nodename nor servname' in text):
        return LagerError(
            f'Could not resolve "{ip}".',
            cause='The box hostname or address could not be looked up.',
            fixes=['Check the spelling, or use the box IP address. List boxes: lager boxes'],
            raw=stderr or None,
        )

    if 'host key verification failed' in text:
        return LagerError(
            f'SSH host key verification failed for {ip}.',
            cause="The box's SSH host key changed — it was likely reimaged, or a different "
                  'device now has this IP.',
            fixes=[
                f'If you trust this box, drop the old key then retry: ssh-keygen -R {ip}',
            ],
            raw=stderr or None,
        )

    message = (stderr or '').strip()
    return LagerError(
        f'Could not connect to {ip} over SSH.',
        cause=message or None,
        fixes=['Verify the box is online (lager hello) and reachable over SSH.'],
        raw=stderr or None,
    )


def system_error(exc):
    """Translate a low-level USB/pyvisa/libusb errno into a LagerError, or
    return ``None`` if there's no curated mapping for it.

    Reuses the errno → guidance table in
    :mod:`cli.context.error_handlers` so the instrument-side mappings
    (EBUSY / ENODEV / ETIMEDOUT) live in exactly one place.
    """
    # Imported lazily to avoid a circular import at module load.
    from .context.error_handlers import map_system_error

    mapped = map_system_error(str(exc))
    if not mapped:
        return None
    headline, actions = mapped
    return LagerError(headline, fixes=actions, raw=exc)

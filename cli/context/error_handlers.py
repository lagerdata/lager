# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    cli.context.error_handlers

    Error handling utilities for CLI context
"""
import json
import re
import click


# Error code sets for categorizing different error types
DOCKER_ERROR_CODES = set()

CANBUS_ERROR_CODES = {
    'canbus_up_failed',
}


# Raw-errno → actionable-message mapping (0.20.0+).
#
# Three errnos surface on every "instrument net misbehaving" bug class and
# previously dumped raw to the user: [Errno 16], [Errno 19], [Errno 110].
# Each maps to a single concrete next step — surface that instead of the
# bare errno + traceback. Raw error stays available via LAGER_DEBUG=1.
#
# 16  EBUSY     — libusb interface claim race (another process or kernel)
# 19  ENODEV    — USB re-enumeration (instrument power cycle / unplug)
# 110 ETIMEDOUT — SCPI command sent but no response (firmware wedged)
_SYSTEM_ERROR_MAP = (
    # (errno, [substring matches for fallback], headline, action lines)
    (16,
     ('resource busy', 'errno 16'),
     'USB device busy — another process holds the libusb interface.',
     ['Run: `lager diagnose <net> --box <box>` to identify the conflicting process.',
      'If the conflict persists, `lager ssh <box>` then `sudo lsof /dev/bus/usb/...` to inspect.']),
    (19,
     ('no such device', 'errno 19', 'enodev', 'cannot find'),
     'Instrument disappeared from USB (re-enumeration).',
     ['The hardware service should auto-recover on the next call (0.20.0+).',
      'If it does not, `lager ssh <box>` then `sudo docker restart lager`.']),
    (110,
     ('timed out', 'errno 110', 'etimedout', 'operation timed out'),
     'Instrument did not respond to SCPI within the timeout — firmware may be wedged.',
     ['A mains-side power-cycle of the instrument is usually required (software cannot recover this).',
      'Use `lager diagnose <net> --box <box>` to confirm before power-cycling.']),
)

_ERRNO_RE = re.compile(r'\[Errno\s+(\d+)\]', re.IGNORECASE)


def map_system_error(error_text):
    """Translate a raw system / pyvisa / libusb error string into an
    actionable (headline, action_lines) tuple, or None if no mapping
    applies. Detection prefers an explicit `[Errno N]` substring match
    over message-based heuristics.
    """
    if not error_text:
        return None
    text = str(error_text)
    lower = text.lower()

    # Explicit errno match wins.
    m = _ERRNO_RE.search(text)
    if m:
        errno_n = int(m.group(1))
        for entry_errno, _subs, headline, actions in _SYSTEM_ERROR_MAP:
            if entry_errno == errno_n:
                return headline, list(actions)

    # Fall back to message-substring match.
    for _entry_errno, subs, headline, actions in _SYSTEM_ERROR_MAP:
        if any(s in lower for s in subs):
            return headline, list(actions)

    return None


def format_system_error_for_user(error_text):
    """Return a multi-line, click-styled string ready for stderr — or the
    original error verbatim if no mapping applies. Honors `LAGER_DEBUG=1`
    by appending the raw error below the actionable text."""
    import os
    mapped = map_system_error(error_text)
    if not mapped:
        return None
    headline, actions = mapped
    lines = [click.style(headline, fg='red', bold=True)]
    for action in actions:
        lines.append('  ' + action)
    if os.environ.get('LAGER_DEBUG'):
        lines.append('')
        lines.append(click.style('--- raw error ---', dim=True))
        lines.append(str(error_text))
    return '\n'.join(lines)


class ElfHashMismatch(Exception):
    """Exception raised when ELF file hash doesn't match expected value"""
    pass


def print_docker_error(ctx, error):
    """
    Parse a docker error and print the output
    """
    if not error:
        return
    parsed = json.loads(error)
    stdout = parsed['stdout']
    stderr = parsed['stderr']
    click.echo(stdout, nl=False)
    click.secho(stderr, fg='red', err=True, nl=False)
    ctx.exit(parsed['returncode'])


def print_canbus_error(ctx, error):
    """
    Parse a CAN bus error and print helpful messages
    """
    if not error:
        return
    parsed = json.loads(error)
    if parsed['stdout']:
        click.secho(parsed['stdout'], fg='red', nl=False)
    if parsed['stderr']:
        click.secho(parsed['stderr'], fg='red', err=True, nl=False)
        if parsed['stderr'] == 'Cannot find device "can0"\n':
            click.secho('Please check adapter connection', fg='red', err=True)

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Version-skew warning between CLI and box.

The 2026-05-26 incident started with CLI 0.19.2 talking to box
0.18.3 and the first error was opaque. A simple one-line warning at the
start of the session would have cut diagnosis time by hours. This module
implements that:

  When the CLI's minor version is ahead of the box's minor version by
  one or more, print a single stderr warning recommending `lager box
  update --box <name>`. Cache the check per-process by box IP so we
  don't refetch on every command in the same session.

Fail-open by design — any error fetching or parsing the box version
silently skips the warning. We never break a working command on a
version-check failure.
"""

from __future__ import annotations

import sys
import logging
import requests

logger = logging.getLogger(__name__)

# Per-process cache: box_ip -> bool (already warned / already checked-clean).
# Lives for the lifetime of the CLI process; long-running flows like
# `lager update` and the TUIs each get one check per IP.
_checked_boxes: set[str] = set()


def _parse_minor(version_str: str) -> tuple[int, int] | None:
    """Return (major, minor) from a 'X.Y.Z' (or 'X.Y') string, or None
    if unparseable. Tolerant of leading 'v' and trailing -suffixes."""
    if not version_str:
        return None
    s = version_str.lstrip('v').split('-', 1)[0].split('+', 1)[0]
    parts = s.split('.')
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return None


def check_and_warn(box_ip: str, box_name: str | None = None) -> None:
    """Fetch the box's reported version and print a stderr warning if the
    CLI's minor version is ahead. No-op if we've already checked this IP
    in this process, or on any error."""
    if not box_ip or box_ip in _checked_boxes:
        return
    _checked_boxes.add(box_ip)

    try:
        # /status on port 9000 (the box HTTP API) reports the box version;
        # it's the same endpoint `lager box hello` uses. 1.5s timeout
        # keeps the latency penalty bounded if the box is briefly slow.
        r = requests.get(f'http://{box_ip}:9000/status', timeout=1.5)
        if r.status_code == 404:
            # The :9000 server answered but has no /status route — the box
            # image predates the :9000 API surface this CLI requires. That is
            # exactly the skew this module exists to warn about, so don't
            # fail silent here (unreachable boxes still skip quietly: the
            # command itself will produce its own error).
            display = box_name or box_ip
            print(
                f'\n[warning] Box {display} does not report a version on its '
                f':9000 API — it is likely running an image too old for this '
                f'CLI.\n          Some commands may fail. To update the box:\n'
                f'          lager box update --box {display}\n',
                file=sys.stderr,
            )
            return
        if r.status_code != 200:
            return
        body = r.json()
        box_version = body.get('version') or body.get('box_version')
        if box_version == 'unknown':
            box_version = None
    except Exception as e:
        logger.debug('version-skew check: fetch failed: %s', e)
        return

    try:
        # Import lazily so the CLI startup path doesn't pay for it on
        # commands that don't talk to a box.
        from .. import __version__ as cli_version
    except Exception:
        return

    cli_parts = _parse_minor(cli_version)
    box_parts = _parse_minor(box_version)
    if not cli_parts or not box_parts:
        return

    cli_major, cli_minor = cli_parts
    box_major, box_minor = box_parts

    # Same major, CLI ahead by one or more minor versions → warn.
    if cli_major == box_major and cli_minor > box_minor:
        display = box_name or box_ip
        msg = (
            f'\n[warning] Box {display} is on lager {box_version}; CLI is on {cli_version}.\n'
            f'          Some commands may behave unexpectedly. To update the box:\n'
            f'          lager box update --box {display}\n'
        )
        print(msg, file=sys.stderr)


def reset_cache_for_tests() -> None:
    """Test helper — clear the per-process check cache so unit tests can
    re-exercise the warning logic with different mocked responses."""
    _checked_boxes.clear()

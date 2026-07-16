# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Getting a token for a box that asks for one.

Most boxes do not. A box only wants a token if someone has configured it to, and
until then everything here returns None and the CLI behaves exactly as it always
has.

Lager does not know how to obtain a token and deliberately does not learn. It
knows only how to *ask*: point ``token_helper`` at a command, and Lager runs it
and uses what it prints. Whoever runs the box's control plane supplies that
command; how it decides you are you -- a browser login, SSO, a key on disk, a
kerberos ticket -- is between you and them, and none of it lands here.

That boundary is a deliberate re-learning, not an oversight. Lager once had a
``lager boxes connect`` command and, by 0.12.0, a specific vendor's URL compiled
into an open-source project. 0.15.0 removed all of it. A login command here
would be the same mistake with a new name, so the shape is inverted: Lager
defines an interface, someone else implements it, and no vendor is named. Git
solved the same problem the same way.

Resolution order, mirroring get_lager_user() in box_storage.py:

  1. ``LAGER_TOKEN``          -- an explicit token. Wins over everything; good
                                 for CI, a wrapper script, or debugging.
  2. ``LAGER_TOKEN_HELPER``   -- a command to run, overriding the config file.
  3. ``token_helper`` in ~/.lager

Nothing configured means no token, which is the default and not an error.
"""

import json
import logging
import os
import shlex
import subprocess
import threading
import time

logger = logging.getLogger(__name__)

__all__ = [
    'BoxTokenAuth',
    'clear_token_cache',
    'get_token_helper',
    'resolve_token',
]

# Give up on a helper that hangs: it may be waiting on a login prompt we cannot
# see, and a CLI that blocks forever with no output is worse than one that fails.
_HELPER_TIMEOUT_SECONDS = 60

# Re-run the helper this long before a token expires.
_REFRESH_MARGIN_SECONDS = 30

_lock = threading.Lock()
_cache = {}  # box_ip -> (token, expires_at | None)


def get_token_helper():
    """The configured helper command, or None."""
    if env_helper := os.getenv('LAGER_TOKEN_HELPER'):
        return env_helper
    try:
        from .config import read_config_file
        config = read_config_file()
        if config.has_option('LAGER', 'token_helper'):
            return config.get('LAGER', 'token_helper')
    except Exception:
        pass
    return None


def _decode_expiry(token):
    """Best-effort read of a JWT's exp, so we can cache it. None if unreadable.

    The token is not verified here -- that is the box's job, and it holds the
    key to do it. This only reads a hint about when to bother asking for a new
    one. A token we misread the expiry of still gets rejected by the box.
    """
    try:
        import base64
        payload_b64 = token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        expiry = payload.get('exp')
        return float(expiry) if isinstance(expiry, (int, float)) else None
    except Exception:
        return None


def _run_helper(helper, box_ip):
    """Run the helper and return the token it printed, or None."""
    env = dict(os.environ)
    # The helper is told which box the token is for. It maps that to whatever
    # identity its control plane uses -- Lager has no idea what a box id is.
    env['LAGER_BOX'] = box_ip or ''

    try:
        result = subprocess.run(
            shlex.split(helper),
            capture_output=True,
            text=True,
            timeout=_HELPER_TIMEOUT_SECONDS,
            env=env,
        )
    except FileNotFoundError:
        logger.warning(
            'token_helper %r is not an executable this system can find', helper,
        )
        return None
    except subprocess.TimeoutExpired:
        logger.warning(
            'token_helper %r did not finish within %ds', helper, _HELPER_TIMEOUT_SECONDS,
        )
        return None
    except (OSError, ValueError) as e:
        logger.warning('token_helper %r could not be run: %s', helper, e)
        return None

    if result.returncode != 0:
        stderr = (result.stderr or '').strip()
        logger.warning(
            'token_helper %r exited %d%s',
            helper, result.returncode, f': {stderr}' if stderr else '',
        )
        return None

    token = (result.stdout or '').strip()
    if not token:
        logger.warning('token_helper %r printed no token', helper)
        return None
    return token


def resolve_token(box_ip=None):
    """Return a token for ``box_ip``, or None if none is configured.

    None is the normal case. It means nobody has set this up, which is how every
    box works until someone does.
    """
    if env_token := os.getenv('LAGER_TOKEN'):
        return env_token

    helper = get_token_helper()
    if not helper:
        return None

    now = time.time()
    with _lock:
        cached = _cache.get(box_ip)
        if cached is not None:
            token, expires_at = cached
            if expires_at is None or now < expires_at - _REFRESH_MARGIN_SECONDS:
                return token

    token = _run_helper(helper, box_ip)
    if token is None:
        return None

    with _lock:
        _cache[box_ip] = (token, _decode_expiry(token))
    return token


def clear_token_cache():
    """Forget cached tokens. For tests, and after switching identity."""
    with _lock:
        _cache.clear()


class BoxTokenAuth:
    """requests auth hook that attaches a token, if there is one to attach.

    An auth hook rather than a session header because sessions here are pooled
    and long-lived while tokens are short-lived: a header set once at pool
    construction would go stale and start failing. This runs per request, so it
    always sends a current token -- and sends nothing at all when none is
    configured, which is what keeps this invisible to boxes that do not use it.
    """

    def __init__(self, box_ip):
        self.box_ip = box_ip

    def __call__(self, request):
        token = resolve_token(self.box_ip)
        if token:
            request.headers['Authorization'] = f'Bearer {token}'
        return request

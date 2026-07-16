# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Optional bearer-token verification for the box HTTP services.

Off unless configured. With no ``/etc/lager-auth/auth.json`` this module reports
that it is not enforcing and every service behaves exactly as it did before it
existed -- which is the state a box ships in and stays in unless something
deliberately provisions it.

When configured, the box verifies that a request carries a token signed by a key
it has been told to trust, and nothing else. It does not know who the caller is,
what they are allowed to do, which organization they belong to, or how to revoke
them. Those are questions for whoever issues the tokens; the box only checks a
signature and an expiry. This is what keeps the box's side small, and it is why
this file names no vendor and opens no network connection: any issuer able to
sign with the configured key works, and the box never phones anyone to ask.

Verification is offline and deliberately so. The alternative -- asking an issuer
about every request -- would couple the box's availability to that issuer's, add
a round trip to every call, and put a URL in an open-source project. Tokens are
short-lived instead, so the issuer's decisions expire on their own.

Config shape (a JWK Set, plus what to check the token against)::

    {
      "schema_version": 1,
      "issuer": "https://...",       # compared literally against the iss claim
      "audience": "<box-id>",        # defaults to the contents of /etc/lager/box_id
      "keys": [{"kid": "...", "kty": "OKP", "crv": "Ed25519", "x": "..."}]
    }

A key set rather than a single key, so keys can be rotated by overlap: publish
the new key alongside the old, switch signing, then drop the old one. A box that
was offline for the switch keeps working.

Absent config means off. Malformed config means **refuse everything** -- a
half-written trust anchor must never quietly mean "let everyone in".

On algorithms: this only ever verifies, never signs, and only ever with Ed25519.
The algorithm is not read from the token to decide how to check it; the token's
declared ``alg`` must equal "EdDSA" or it is rejected out of hand. That is what
makes the classic JWT forgeries ("alg": "none", or an HS256 token signed with the
public key as its HMAC secret) structurally impossible here rather than merely
guarded against.
"""

import base64
import binascii
import hmac
import json
import logging
import pathlib
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .constants import AUTH_CONFIG_PATH, BOX_ID_PATH

logger = logging.getLogger(__name__)

__all__ = [
    'AuthConfigError',
    'AuthError',
    'OPEN_PATHS',
    'guard',
    'is_enforcing',
    'load_auth_config',
    'verify_bearer',
]

# Paths that stay reachable without a token even when auth is on. Deliberately
# tiny, and deliberately here rather than in each service, so the exceptions are
# reviewable in one place.
#
#   /health        The way out of a locked-out box. If verification starts
#                  failing -- a drifted clock, a key rotation that missed this
#                  box -- an operator needs to be able to ask why without
#                  physical access. It returns no secrets.
#   /authorize-key Already authenticated, by its own shared secret
#                  (http_handlers/ssh_handler.py), and it runs while a box is
#                  being provisioned -- before it necessarily has a key set at
#                  all. Gating it here would mean setup required the very thing
#                  setup installs.
OPEN_PATHS = frozenset({'/health', '/authorize-key'})

# Tokens are checked against the box's clock. Boxes are not guaranteed to have
# a good one -- lager does not manage time sync -- so allow a little drift in
# both directions. Generous on purpose: the cost of being strict is a box that
# rejects every token and has no console to fix it from.
CLOCK_LEEWAY_SECONDS = 120

# Rate limit failed attempts per IP, mirroring the /authorize-key handler.
# Signatures are not guessable, but this keeps a flood from costing us a
# signature verification per request.
_RATE_LIMIT_MAX_ATTEMPTS = 10
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_PRUNE_THRESHOLD = 1024
_rate_limit_lock = threading.Lock()
_rate_limit_attempts = {}  # ip -> (window_start, count)

_UNAUTHORIZED = 401
_FORBIDDEN = 403
_SERVICE_UNAVAILABLE = 503

_AUTH_CONFIG_PATH = pathlib.Path(AUTH_CONFIG_PATH)
_BOX_ID_PATH = pathlib.Path(BOX_ID_PATH)

# Parsed config, cached on the file's identity so an updated key set is picked up
# without a restart but is not re-parsed per request.
_config_lock = threading.Lock()
_config_cache = None  # (stat_key, AuthConfig)


class AuthError(Exception):
    """A request's token is missing, malformed, or not acceptable."""


class AuthConfigError(Exception):
    """The auth config exists but cannot be trusted. Callers must fail closed."""


class AuthConfig:
    """A parsed, validated auth config."""

    __slots__ = ('issuer', 'audience', 'keys')

    def __init__(self, issuer, audience, keys):
        self.issuer = issuer
        self.audience = audience
        self.keys = keys  # kid -> Ed25519PublicKey


def _b64url_decode(value):
    """Decode base64url without padding. Raises AuthError on anything invalid."""
    if not isinstance(value, str):
        raise AuthError('Malformed token encoding')
    padding = '=' * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (binascii.Error, ValueError) as e:
        raise AuthError('Malformed token encoding') from e


def _load_jwk(entry):
    """Turn one JWK Set entry into (kid, Ed25519PublicKey). Raises AuthConfigError."""
    if not isinstance(entry, dict):
        raise AuthConfigError('key entry is not an object')
    kid = entry.get('kid')
    if not isinstance(kid, str) or not kid:
        raise AuthConfigError('key entry has no usable "kid"')
    if entry.get('kty') != 'OKP' or entry.get('crv') != 'Ed25519':
        raise AuthConfigError(
            f'key {kid!r} is not an Ed25519 OKP key '
            f'(kty={entry.get("kty")!r}, crv={entry.get("crv")!r})'
        )
    x = entry.get('x')
    if not isinstance(x, str) or not x:
        raise AuthConfigError(f'key {kid!r} has no "x"')
    padding = '=' * (-len(x) % 4)
    try:
        raw = base64.urlsafe_b64decode(x + padding)
    except (binascii.Error, ValueError) as e:
        raise AuthConfigError(f'key {kid!r} has an undecodable "x"') from e
    try:
        return kid, Ed25519PublicKey.from_public_bytes(raw)
    except ValueError as e:
        raise AuthConfigError(f'key {kid!r} is not a valid Ed25519 public key') from e


def _path_is_open(path):
    """True if ``path`` is one of the few that never require a token."""
    if not path:
        return False
    # Compare the path alone: a query string must not be able to smuggle an
    # exemption ('/python?x=/health'), and a trailing slash must not defeat one.
    bare = path.split('?', 1)[0].split('#', 1)[0].rstrip('/') or '/'
    return bare in OPEN_PATHS


def _read_box_id():
    try:
        return _BOX_ID_PATH.read_text().strip() or None
    except OSError:
        return None


def _parse_config(text):
    """Parse and validate config text. Raises AuthConfigError."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise AuthConfigError(f'not valid JSON: {e}') from e
    if not isinstance(data, dict):
        raise AuthConfigError('top level is not an object')

    version = data.get('schema_version')
    if version != 1:
        raise AuthConfigError(f'unsupported schema_version {version!r} (expected 1)')

    issuer = data.get('issuer')
    if not isinstance(issuer, str) or not issuer:
        raise AuthConfigError('missing "issuer"')

    audience = data.get('audience') or _read_box_id()
    if not isinstance(audience, str) or not audience:
        raise AuthConfigError(
            'missing "audience" and no box id readable from ' + str(_BOX_ID_PATH)
        )

    entries = data.get('keys')
    if not isinstance(entries, list) or not entries:
        raise AuthConfigError('missing a non-empty "keys" array')

    keys = {}
    for entry in entries:
        kid, key = _load_jwk(entry)
        if kid in keys:
            raise AuthConfigError(f'duplicate kid {kid!r}')
        keys[kid] = key

    return AuthConfig(issuer=issuer, audience=audience, keys=keys)


def load_auth_config():
    """Return the parsed AuthConfig, or None if the box has no auth configured.

    Raises:
        AuthConfigError: the file exists but is unusable. Callers must treat this
            as "refuse everything", never as "no auth configured".
    """
    global _config_cache
    try:
        stat = _AUTH_CONFIG_PATH.stat()
    except FileNotFoundError:
        with _config_lock:
            _config_cache = None
        return None
    except OSError as e:
        # Present but unreadable is a broken trust anchor, not an absent one.
        raise AuthConfigError(f'cannot read {_AUTH_CONFIG_PATH}: {e}') from e

    stat_key = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _config_lock:
        if _config_cache is not None and _config_cache[0] == stat_key:
            return _config_cache[1]

    try:
        text = _AUTH_CONFIG_PATH.read_text()
    except OSError as e:
        raise AuthConfigError(f'cannot read {_AUTH_CONFIG_PATH}: {e}') from e

    config = _parse_config(text)  # raises AuthConfigError

    with _config_lock:
        _config_cache = (stat_key, config)
    return config


def is_enforcing():
    """True if this box has auth configured.

    A malformed config counts as enforcing: it must not read as "off".
    """
    try:
        return load_auth_config() is not None
    except AuthConfigError:
        return True


def _rate_limited(ip):
    """Record a failed attempt from ip; True if it exceeded the window limit."""
    if not ip:
        return False
    now = time.monotonic()
    with _rate_limit_lock:
        if len(_rate_limit_attempts) > _RATE_LIMIT_PRUNE_THRESHOLD:
            expired = [
                addr for addr, (start, _) in _rate_limit_attempts.items()
                if now - start >= _RATE_LIMIT_WINDOW_SECONDS
            ]
            for addr in expired:
                del _rate_limit_attempts[addr]
            # A many-unique-IP flood can keep the dict full of live windows,
            # making the prune scan O(N) per request under the lock. Dropping all
            # state resets limits mid-attack -- per-IP limits do not constrain a
            # distributed attacker anyway -- but keeps CPU and memory bounded.
            if len(_rate_limit_attempts) > _RATE_LIMIT_PRUNE_THRESHOLD * 2:
                _rate_limit_attempts.clear()
        window_start, count = _rate_limit_attempts.get(ip, (now, 0))
        if now - window_start >= _RATE_LIMIT_WINDOW_SECONDS:
            window_start, count = now, 0
        count += 1
        _rate_limit_attempts[ip] = (window_start, count)
        return count > _RATE_LIMIT_MAX_ATTEMPTS


def _rate_limit_reset(ip):
    if not ip:
        return
    with _rate_limit_lock:
        _rate_limit_attempts.pop(ip, None)


def verify_bearer(auth_header, config=None, now=None):
    """Verify an Authorization header value and return the token's claims.

    Args:
        auth_header: the raw Authorization header, or None.
        config: an AuthConfig; loaded if not supplied.
        now: unix seconds, for tests.

    Returns:
        The claims dict.

    Raises:
        AuthError: the token is absent, malformed, or unacceptable.
        AuthConfigError: the box's auth config is unusable.
    """
    if config is None:
        config = load_auth_config()
    if config is None:
        raise AuthError('Box has no auth configured')
    if now is None:
        now = time.time()

    if not auth_header:
        raise AuthError('Missing Authorization header')
    # Scheme is case-insensitive per RFC 7235.
    scheme, _, raw_token = auth_header.partition(' ')
    if scheme.lower() != 'bearer' or not raw_token.strip():
        raise AuthError('Authorization header is not a bearer token')
    token = raw_token.strip()

    parts = token.split('.')
    if len(parts) != 3:
        raise AuthError('Malformed token')
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
    except (json.JSONDecodeError, ValueError) as e:
        raise AuthError('Malformed token header') from e
    if not isinstance(header, dict):
        raise AuthError('Malformed token header')

    # Not a dispatch on alg -- a refusal of anything that is not the one
    # algorithm we verify. "none" and HS256-signed-with-our-public-key both die
    # here, and would die below anyway since we only ever call Ed25519.verify.
    if header.get('alg') != 'EdDSA':
        raise AuthError(f'Unsupported token algorithm {header.get("alg")!r}')

    kid = header.get('kid')
    if not isinstance(kid, str) or not kid:
        raise AuthError('Token does not name a key')
    key = config.keys.get(kid)
    if key is None:
        raise AuthError(f'Token signed by unknown key {kid!r}')

    signature = _b64url_decode(signature_b64)
    signing_input = (header_b64 + '.' + payload_b64).encode('ascii')
    try:
        key.verify(signature, signing_input)
    except InvalidSignature as e:
        raise AuthError('Token signature is not valid') from e

    # Only now are the claims worth reading.
    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except (json.JSONDecodeError, ValueError) as e:
        raise AuthError('Malformed token payload') from e
    if not isinstance(claims, dict):
        raise AuthError('Malformed token payload')

    issuer = claims.get('iss')
    if not isinstance(issuer, str) or not hmac.compare_digest(issuer, config.issuer):
        raise AuthError('Token was not issued by this box\'s issuer')

    # A token is for one box. A token captured in flight cannot be replayed
    # anywhere but the box it was already going to, which is what makes a short
    # lifetime sufficient and a replay cache unnecessary.
    audience = claims.get('aud')
    if isinstance(audience, str):
        audience_ok = hmac.compare_digest(audience, config.audience)
    elif isinstance(audience, list):
        audience_ok = any(
            isinstance(a, str) and hmac.compare_digest(a, config.audience)
            for a in audience
        )
    else:
        audience_ok = False
    if not audience_ok:
        raise AuthError('Token is not addressed to this box')

    expiry = claims.get('exp')
    if not isinstance(expiry, (int, float)) or isinstance(expiry, bool):
        raise AuthError('Token has no expiry')
    if now > expiry + CLOCK_LEEWAY_SECONDS:
        raise AuthError('Token has expired')

    not_before = claims.get('nbf')
    if not_before is not None:
        if not isinstance(not_before, (int, float)) or isinstance(not_before, bool):
            raise AuthError('Token has a malformed nbf')
        if now < not_before - CLOCK_LEEWAY_SECONDS:
            raise AuthError('Token is not valid yet')

    # iat is deliberately not checked. It says when a token was made, not whether
    # it is still good, and treating it as freshness would reject valid tokens on
    # a box whose clock is behind.
    return claims


def guard(auth_header, remote_addr=None, path=None):
    """Decide whether a request may proceed.

    Returns:
        None if the request may proceed -- including when this box has no auth
        configured at all -- otherwise an ``(status, message)`` tuple.
    """
    try:
        config = load_auth_config()
    except AuthConfigError as e:
        # Fail closed. A trust anchor we cannot read is not permission to skip it.
        # /health stays reachable so this is diagnosable: see OPEN_PATHS.
        if _path_is_open(path):
            return None
        logger.error(
            'Refusing all requests: auth config at %s is unusable: %s',
            _AUTH_CONFIG_PATH, e,
        )
        return _SERVICE_UNAVAILABLE, 'Box auth is misconfigured'

    if config is None:
        return None  # Not configured: unchanged behavior.

    if _path_is_open(path):
        return None

    if _rate_limited(remote_addr):
        logger.warning('Rate-limited auth attempts from %r (path=%r)', remote_addr, path)
        return _FORBIDDEN, 'Too many failed authentication attempts'

    try:
        claims = verify_bearer(auth_header, config=config)
    except AuthError as e:
        logger.warning(
            'Rejected request: %s (path=%r, from=%r)', e, path, remote_addr,
        )
        return _UNAUTHORIZED, str(e)

    _rate_limit_reset(remote_addr)
    # An audit breadcrumb, not an authorization decision. Uniqueness is not
    # enforced: see the module docstring on why a replay cache buys nothing here.
    logger.info(
        'Authenticated request (sub=%r, jti=%r, path=%r, from=%r)',
        claims.get('sub'), claims.get('jti'), path, remote_addr,
    )
    return None

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.gateway_auth

    Optional bearer-token auth for boxes fronted by an authenticating gateway.

    A plain Lager box needs none of this: no token is stored, no header is
    sent, and no code path here ever runs unless a box answers with the
    discovery header. When a box *is* gated, its gateway rejects
    unauthenticated traffic with 401 + ``X-Gateway-Auth-Url: <url>``; we
    record that box→auth-server mapping, and after ``lager login <url>``
    every subsequent request to that box carries ``Authorization: Bearer``.

    The primitive is generic: any control plane that fronts a box with a
    reverse proxy and honours the small HTTP contract below can drive it.

        POST <url>/api/auth/login       {email, password}  -> {accessToken, ...}
        POST <url>/api/auth/login/mfa   {mfaToken, code}   -> {accessToken, ...}
        POST <url>/api/auth/refresh     (login cookies)    -> {accessToken}

    Tokens live in ``~/.lager_gateway_auth`` (mode 0600), keyed by auth
    server URL so one machine can talk to boxes gated by different
    deployments. Access tokens are short-lived; they are refreshed
    transparently, replaying the cookies the auth server set at login.
"""
import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .errors import LagerError

DISCOVERY_HEADER = 'X-Gateway-Auth-Url'
# Public troubleshooting page linked from every gateway auth error.
ACCESS_DOCS_URL = 'https://docs.lagerdata.com/source/reference/cli/login'
# Refresh the access token when it expires within this many seconds.
EXPIRY_MARGIN_SECONDS = 60
AUTH_SERVER_TIMEOUT = 10


def _store_path():
    override = os.environ.get('LAGER_GATEWAY_AUTH_FILE')
    if override:
        return Path(override)
    return Path.home() / '.lager_gateway_auth'


def _load_store():
    try:
        with open(_store_path(), 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_store(store):
    path = _store_path()
    with open(path, 'w') as f:
        json.dump(store, f, indent=2)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# Box → auth server mapping (learned from gateway 401 discovery headers)
# ---------------------------------------------------------------------------

def record_box_auth_server(box_ip, url):
    store = _load_store()
    boxes = store.setdefault('boxes', {})
    if boxes.get(box_ip) != url:
        boxes[box_ip] = url
        _save_store(store)


def auth_server_for_box(box_ip):
    return _load_store().get('boxes', {}).get(box_ip)


# ---------------------------------------------------------------------------
# Token storage + refresh
# ---------------------------------------------------------------------------

def _token_expires_at(access_token):
    """Read `exp` from a JWT without verifying it (the gateway verifies)."""
    try:
        payload_b64 = access_token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload['exp'])
    except (IndexError, KeyError, ValueError, TypeError):
        return 0.0


def save_login(url, access_token, cookies):
    """Store a session: the bearer token plus whatever cookies the auth
    server set at login (typically an httpOnly refresh token)."""
    store = _load_store()
    store.setdefault('authServers', {})[url] = {
        'accessToken': access_token,
        'cookies': dict(cookies or {}),
    }
    _save_store(store)


def clear_login(url=None):
    """Forget stored tokens for one auth server, or all of them."""
    store = _load_store()
    if url is None:
        store.pop('authServers', None)
    else:
        store.get('authServers', {}).pop(url, None)
    _save_store(store)


def _refresh_access_token(url, entry):
    """Ask the auth server for a fresh access token, or return None."""
    cookies = entry.get('cookies') or {}
    if not cookies:
        return None
    try:
        resp = requests.post(
            f'{url}/api/auth/refresh',
            cookies=cookies,
            timeout=AUTH_SERVER_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        access_token = resp.json().get('accessToken')
    except (requests.RequestException, ValueError):
        return None
    if not access_token:
        return None
    # Replay-safe: merge any rotated cookies the server handed back.
    rotated = {**cookies, **requests.utils.dict_from_cookiejar(resp.cookies)}
    save_login(url, access_token, rotated)
    return access_token


def access_token_for(url):
    """Stored access token for an auth server, refreshed if near expiry."""
    entry = _load_store().get('authServers', {}).get(url)
    if not entry:
        return None
    access_token = entry.get('accessToken')
    if access_token and _token_expires_at(access_token) > time.time() + EXPIRY_MARGIN_SECONDS:
        return access_token
    return _refresh_access_token(url, entry)


def auth_headers_for_box(box_ip):
    """Authorization header for a box known to be gated, else {}."""
    url = auth_server_for_box(box_ip)
    if not url:
        return {}
    token = access_token_for(url)
    if not token:
        return {}
    return {'Authorization': f'Bearer {token}'}


def _token_claim(access_token, claim):
    """Read a claim from a JWT payload without verifying it."""
    try:
        payload_b64 = access_token.split('.')[1]
        payload_b64 += '=' * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get(claim)
    except (IndexError, ValueError, TypeError):
        return None


def auth_status():
    """Snapshot of the stored auth state, for `lager whoami`.

    Returns a list of per-server dicts: ``url``, ``email`` (from the token),
    ``expires_in`` (seconds until the access token expires; may be negative),
    ``refreshable`` (a refresh cookie is stored), and ``boxes`` (box IPs known
    to be gated by that server). Never raises; reads local state only.
    """
    store = _load_store()
    servers = store.get('authServers', {})
    box_map = store.get('boxes', {})
    now = time.time()
    out = []
    for url, entry in servers.items():
        token = entry.get('accessToken') or ''
        out.append({
            'url': url,
            'email': _token_claim(token, 'email'),
            'expires_in': (_token_expires_at(token) - now) if token else None,
            'refreshable': bool(entry.get('cookies')),
            'boxes': sorted(ip for ip, u in box_map.items() if u == url),
        })
    return out


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login(url, email, password, mfa_code_prompt=None):
    """Log in to an auth server and store the session.

    Returns the auth server's user object. ``mfa_code_prompt`` is called
    (no args) to collect a TOTP/backup code when the account has MFA.
    """
    url = url.rstrip('/')
    try:
        resp = requests.post(
            f'{url}/api/auth/login',
            json={'email': email, 'password': password},
            timeout=AUTH_SERVER_TIMEOUT,
        )
    except requests.RequestException as e:
        raise LagerError(
            f'Could not reach the auth server at {url}.',
            cause=str(e),
        )

    data = _json_or_raise(resp, url)

    if data.get('mfaRequired'):
        if mfa_code_prompt is None:
            raise LagerError('This account requires MFA.',
                             fixes=['Re-run interactively so the code can be entered.'])
        code = mfa_code_prompt()
        resp = requests.post(
            f'{url}/api/auth/login/mfa',
            json={'mfaToken': data['mfaToken'], 'code': code},
            timeout=AUTH_SERVER_TIMEOUT,
        )
        data = _json_or_raise(resp, url)

    access_token = data.get('accessToken')
    if not access_token:
        raise LagerError(f'Login to {url} did not return an access token.')
    save_login(url, access_token, requests.utils.dict_from_cookiejar(resp.cookies))
    return data.get('user', {})


def _json_or_raise(resp, url):
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200:
        message = data.get('message') or data.get('error') or f'HTTP {resp.status_code}'
        raise LagerError(f'Login failed: {message}', cause=f'Auth server: {url}')
    return data


# ---------------------------------------------------------------------------
# Gateway denial handling (requests response hook)
# ---------------------------------------------------------------------------

def handle_gateway_denial(response, box_ip):
    """Turn a gateway rejection into an actionable error.

    Only ever called for responses carrying the discovery header, so plain
    Lager boxes (and ordinary application 401/403s) are never affected.
    """
    url = response.headers[DISCOVERY_HEADER]
    record_box_auth_server(box_ip, url)

    if response.status_code == 401:
        sent_auth = 'Authorization' in getattr(response.request, 'headers', {})
        if access_token_for(url):
            if not sent_auth:
                # First contact with this box after logging in proactively:
                # we hold a valid session but didn't attach it because the
                # box→auth-server link was only learned from this very
                # response (recorded above). The next attempt authenticates.
                raise LagerError(
                    f'Box {box_ip} requires sign-in. Your existing login for '
                    f'{url} is now linked to this box.',
                    fixes=['Re-run this command.', f'Details: {ACCESS_DOCS_URL}'],
                )
            # We sent a token and the gateway rejected it (e.g. revoked).
            raise LagerError(
                f'Your session for {url} was rejected by box {box_ip}.',
                cause='The session may have expired or been revoked.',
                fixes=[f'lager login {url}', f'Details: {ACCESS_DOCS_URL}'],
            )
        raise LagerError(
            f'Box {box_ip} requires sign-in.',
            fixes=[f'lager login {url}',
                   'Then re-run this command.',
                   f'Details: {ACCESS_DOCS_URL}'],
        )
    if response.status_code == 403:
        raise LagerError(
            f'You are signed in but not authorized to use box {box_ip}.',
            cause='Your account has no access grant for this box.',
            fixes=['Ask an org admin to grant you access to this box.',
                   f'Details: {ACCESS_DOCS_URL}'],
        )
    if response.status_code == 503:
        raise LagerError(
            f'Box {box_ip} could not verify your access right now — '
            'its auth server is unreachable.',
            fixes=['Try again shortly; if it persists, contact your admin.',
                   f'Details: {ACCESS_DOCS_URL}'],
        )


def gateway_response_hook(box_ip):
    """requests response hook that intercepts gateway denials."""
    def hook(response, *_args, **_kwargs):
        if response.status_code in (401, 403, 503) and DISCOVERY_HEADER in response.headers:
            handle_gateway_denial(response, box_ip)
        return response
    return hook


def auth_headers_for_url(box_url):
    """Convenience for WebSocket clients holding a full box URL."""
    host = urlparse(box_url).hostname
    if not host:
        return {}
    return auth_headers_for_box(host)

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""HTTP to a box, with a token attached when one is configured.

Many commands reach a box with a bare ``requests.get`` / ``requests.post``. Once
a box can require a signed token, every one of those needs to carry it, and a
call site that forgets simply stops working against a secured box. Routing box
requests through here removes the chance to forget: the token is attached from
the URL's host, so a caller cannot supply the wrong one or leave it off.

A box that requires no token -- the default -- is unaffected: nothing is
attached, because there is nothing to attach. See lager.box_token.

Use this for calls to a box. Do not use it for anything else: a token is scoped
to the box it is for, so sending one to PyPI or another host would hand that host
a credential it has no business seeing. Loopback is skipped for the same reason
(a local helper daemon is not the box), so those calls can stay on plain
``requests`` or come through here harmlessly.
"""

import ipaddress
from urllib.parse import urlsplit

import requests

from .box_token import BoxTokenAuth, resolve_token

__all__ = ['get', 'post', 'put', 'delete', 'request', 'session_for']


def _box_host(url):
    """The host to attach a token for, or None if this is not a box request.

    A box is always addressed by IP literal: the CLI resolves box names to IPs
    before making a request, and the box's own Host check accepts only IP
    literals (plus a couple of on-box names). So a token is attached only for a
    non-loopback IP, never a DNS name -- which means a stray call to a hostname
    like pypi.org can never be handed a box credential, even by mistake.
    """
    host = urlsplit(url).hostname
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None  # a DNS name is not a box
    if ip.is_loopback:
        return None  # a local helper daemon is not the box
    return host


def _with_token(url, kwargs):
    """Add an Authorization header for the URL's box, if a token is configured.

    When nothing is configured -- the default -- this is a no-op and the kwargs
    pass through untouched, so a box that needs no token is reached exactly as
    before. An explicit ``headers`` Authorization or ``auth=`` from the caller is
    left alone.
    """
    if 'auth' in kwargs:
        return kwargs
    host = _box_host(url)
    if host is None:
        return kwargs
    token = resolve_token(host)
    if not token:
        return kwargs
    headers = dict(kwargs.get('headers') or {})
    headers.setdefault('Authorization', f'Bearer {token}')
    kwargs['headers'] = headers
    return kwargs


def request(method, url, **kwargs):
    """Like ``requests.request`` but carries a box token for the URL's host."""
    return requests.request(method, url, **_with_token(url, kwargs))


def get(url, **kwargs):
    """Like ``requests.get`` but carries a box token for the URL's host."""
    return requests.get(url, **_with_token(url, kwargs))


def post(url, **kwargs):
    """Like ``requests.post`` but carries a box token for the URL's host."""
    return requests.post(url, **_with_token(url, kwargs))


def put(url, **kwargs):
    """Like ``requests.put`` but carries a box token for the URL's host."""
    return requests.put(url, **_with_token(url, kwargs))


def delete(url, **kwargs):
    """Like ``requests.delete`` but carries a box token for the URL's host."""
    return requests.delete(url, **_with_token(url, kwargs))


def session_for(box_ip):
    """A ``requests.Session`` that carries a token for ``box_ip`` on every call.

    For code that wants a session it can reuse. The auth hook runs per request,
    so a token that expires mid-session is refreshed rather than going stale.
    """
    s = requests.Session()
    s.auth = BoxTokenAuth(box_ip)
    return s

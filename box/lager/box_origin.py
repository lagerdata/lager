# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Browser-request rejection for the box HTTP services.

The box HTTP services are machine APIs. The CLI, a control plane, and the
on-box services talk to them; a web browser never should. This module lets each
service refuse requests that came from a browser, which closes off attacks where
a page a user happens to have open drives the box on their behalf.

Two checks, both cheap and both header-only:

1. Origin. A browser attaches an ``Origin`` header to every request whose method
   is not GET or HEAD -- including form posts and ``no-cors`` fetches -- and page
   JavaScript can neither forge nor remove it. Non-browser clients never send
   one. So an ``Origin`` that is not this box's own is positive evidence of a
   browser on another site, and is rejected. Requests with no ``Origin`` are
   unaffected, which is why the CLI and control-plane paths do not notice this
   module exists.

2. Host. DNS rebinding gets a browser to send a request to this box's IP while
   believing it is talking to the attacker's domain, which makes the request
   same-origin and hides it from check 1. Rebinding needs a resolvable name, so
   requiring ``Host`` to be an IP literal or a name this box actually answers to
   removes the technique.

``Host`` is validated structurally rather than against a configured list: a box's
address is not stable (DHCP, VPN reassignment, multiple interfaces, and port 5000
is published on two ports), and a list that drifts out of date fails closed on a
machine with no console attached.

This module is deliberately free of framework imports so the same logic can serve
the stdlib ``BaseHTTPRequestHandler`` services, the Flask services, the Starlette
MCP app, and the generated webcam streaming server.

This is not authentication -- it does not identify the caller, and it is always
on. Caller identity is a separate, opt-in concern.
"""

import ipaddress
import logging
import pathlib
import socket

logger = logging.getLogger(__name__)

__all__ = [
    'check_request',
    'is_host_allowed',
    'is_origin_allowed',
    'self_origins',
]

# Names this box answers to besides its own hostname. 'lager' is the container's
# alias on the lagernet Docker network, used by co-resident containers.
_STATIC_HOSTNAMES = frozenset({'localhost', 'lager'})

# The host's /etc/hostname, bind-mounted read-only by start_box.sh.
_HOST_HOSTNAME_PATH = pathlib.Path('/host/etc/hostname')
_CONTAINER_HOSTNAME_PATH = pathlib.Path('/etc/hostname')

_FORBIDDEN = 403


def _split_host_port(value):
    """Split a Host/authority value into (host, port). Port may be None.

    Handles bracketed IPv6 ('[::1]:8080'), bare IPv6 ('::1'), and the ordinary
    'name:port' and 'name' forms.
    """
    value = value.strip()
    if value.startswith('['):
        # Bracketed IPv6, optionally with a port.
        end = value.find(']')
        if end == -1:
            return value, None
        host = value[1:end]
        rest = value[end + 1:]
        port = rest[1:] if rest.startswith(':') else None
        return host, port
    if value.count(':') > 1:
        # Bare IPv6 with no brackets and therefore no port.
        return value, None
    if ':' in value:
        host, _, port = value.partition(':')
        return host, port
    return value, None


def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _read_hostname(path):
    try:
        return path.read_text().strip().lower() or None
    except OSError:
        return None


def _known_hostnames():
    """Every name this box legitimately answers to, lowercased."""
    names = set(_STATIC_HOSTNAMES)
    try:
        names.add(socket.gethostname().strip().lower())
    except OSError:
        pass
    for path in (_HOST_HOSTNAME_PATH, _CONTAINER_HOSTNAME_PATH):
        name = _read_hostname(path)
        if name:
            names.add(name)
            # /etc/hostname may hold an FQDN; accept the short form too.
            names.add(name.partition('.')[0])
    names.discard('')
    return names


def is_host_allowed(host):
    """True if ``host`` (a raw Host header value) is one this box answers to.

    Accepts any IP literal, since an attacker cannot rebind a name to our address
    without using a name. Accepts localhost, the docker network alias, and this
    box's own hostname. Everything else -- notably an attacker-controlled domain
    pointed at our IP -- is rejected.
    """
    if not host:
        # HTTP/1.1 requires Host. Its absence is not a browser, but it is also
        # not something we need to serve.
        return False
    name, _ = _split_host_port(host)
    if not name:
        return False
    if _is_ip_literal(name):
        return True
    return name.lower() in _known_hostnames()


def self_origins(host):
    """The origins that count as this box itself, for a request to ``host``."""
    if not host:
        return frozenset()
    return frozenset({'http://' + host, 'https://' + host})


def is_origin_allowed(origin, host):
    """True if ``origin`` is absent or is this box's own origin.

    An absent Origin means a non-browser client (or a top-level navigation, which
    cannot be used to attack us). A matching Origin means a page this box itself
    served -- /web_oscilloscope.html is served from port 8080 -- calling back to
    its own origin. Anything else is a browser on someone else's site.
    """
    if not origin:
        return True
    return origin.strip() in self_origins(host)


def check_request(host, origin, path=None, remote_addr=None):
    """Decide whether a request may proceed.

    Args:
        host: raw Host header value, or None.
        origin: raw Origin header value, or None.
        path: request path, for logging only.
        remote_addr: client address, for logging only.

    Returns:
        None if the request may proceed, otherwise an ``(status, message)``
        tuple the caller should send back.
    """
    if not is_host_allowed(host):
        logger.warning(
            'Rejected request with unrecognized Host %r (path=%r, from=%r)',
            host, path, remote_addr,
        )
        return _FORBIDDEN, 'Unrecognized Host header'
    if not is_origin_allowed(origin, host):
        logger.warning(
            'Rejected cross-origin browser request from Origin %r '
            '(host=%r, path=%r, from=%r)',
            origin, host, path, remote_addr,
        )
        return _FORBIDDEN, 'Cross-origin requests are not accepted'
    return None

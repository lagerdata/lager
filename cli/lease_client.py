# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
    lager.lease_client

    Client for a box lease service: ask for any free box in a named pool, keep
    the lease alive while a command runs, and give the box back.

    Lager never decides which box matches a pool. It sends a pool name and
    gets one box back. The service owns pools, matching, queueing policy and
    the record of who held what. A lease also holds the box's own lock, under
    a holder string the service chooses; the command run under the lease uses
    that same string (``LAGER_LOCK_HOLDER``), so every ``lager`` command in it
    sees the lock as its own.

    HTTP contract (all paths under ``<server>/api``):

    - ``POST /orgs/<org>/pools/<pool>/leases`` with ``{ttlSeconds, context}``:
      201 ``{lease}``, or 409 ``{error, reason}`` where ``reason`` is
      ``no_match``, ``none_online`` or ``all_busy``.
    - ``POST /orgs/<org>/leases/<id>/heartbeat``: 200 ``{lease, boxReachable}``,
      or 409 ``{reason: "lease_ended", endReason}``.
    - ``DELETE /orgs/<org>/leases/<id>``: 200, also for an ended lease.
"""
import os
from urllib.parse import quote

import requests

from . import gateway_auth
from .errors import LagerError

SERVER_ENV = 'STOUT_URL'
TOKEN_ENV = 'STOUT_TOKEN'
# A long-lived token minted for CI starts with this. A token that does not is
# a user session from `lager login`.
SCOPED_TOKEN_PREFIX = 'stout_agt_'

#: Exit status when no box could be leased, or the lease ended mid-run.
#: EX_TEMPFAIL from sysexits.h: CI can tell "no box" apart from "test failed".
EXIT_NO_LEASE = 75

REQUEST_TIMEOUT = 15

MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 1800
DEFAULT_TTL_SECONDS = 300
MAX_CONTEXT_ENTRIES = 10

_UNAVAILABLE = {
    'no_match': 'No box matches pool {pool!r}.',
    'none_online': 'Boxes match pool {pool!r}, but none of them is online.',
    'all_busy': 'Every box in pool {pool!r} is in use.',
}
_UNAVAILABLE_FIXES = {
    'no_match': ['Check the requirements of the pool in Stout.'],
    'none_online': ['Check that the matching boxes are powered and online.'],
    'all_busy': ['Try again later.'],
}


def _env(name):
    """An env var, with whitespace-only read as unset.

    ``STOUT_TOKEN: ${{ secrets.X }}`` expands to the empty string when the
    secret is missing; that must read as "no token", not as a token.
    """
    return (os.environ.get(name) or '').strip() or None


class LeaseUnavailable(LagerError):
    """No box could be leased. Carries the service's machine-readable reason."""

    def __init__(self, pool, reason, *, matching=None, online=None, message=None):
        problem = _UNAVAILABLE.get(reason, 'Lager cannot lease a box from pool {pool!r}.').format(pool=pool)
        cause = None
        if matching is not None and online is not None:
            cause = f'{matching} box(es) match the pool; {online} of them online.'
        elif message:
            cause = message
        super().__init__(problem, cause=cause, fixes=_UNAVAILABLE_FIXES.get(reason),
                         exit_code=EXIT_NO_LEASE)
        self.reason = reason


class LeaseEnded(Exception):
    """The service ended the lease while it was still in use."""

    def __init__(self, end_reason):
        super().__init__(end_reason)
        self.end_reason = end_reason


def resolve_server(server=None):
    """The lease service URL: ``--server``, then ``STOUT_URL``, then a login.

    A stored ``lager login`` session names its server, so a developer who has
    logged in to exactly one server needs no flag. Two or more is ambiguous.
    """
    url = (server or '').strip() or _env(SERVER_ENV)
    if url:
        return url.rstrip('/')
    logins = [s['url'] for s in gateway_auth.auth_status()]
    if len(logins) == 1:
        return logins[0].rstrip('/')
    if not logins:
        raise LagerError(
            'No lease server is configured.',
            fixes=[
                'Log in once: lager login <server-url>',
                f'Or set {SERVER_ENV} and {TOKEN_ENV} (for CI).',
            ],
        )
    raise LagerError(
        'You are logged in to more than one server.',
        cause='Logged in to: ' + ', '.join(sorted(logins)),
        fixes=['Pick one: lager lease --server <server-url> ...',
               f'Or set {SERVER_ENV}.'],
    )


class LeaseClient:
    """One lease service, one credential, one organization."""

    def __init__(self, server, *, org=None, session=None):
        self.server = server.rstrip('/')
        self._http = session or requests.Session()
        self._token = _env(TOKEN_ENV)
        self._org_hint = org
        self.org_id = None

    # -- credentials -------------------------------------------------------

    def _bearer(self):
        """The token for the next request.

        A ``STOUT_TOKEN`` is used as-is. A login session is read from the
        store every time, because an access token is short-lived and
        ``access_token_for`` refreshes it; a lease outlives several of them.
        """
        if self._token:
            return self._token
        token = gateway_auth.access_token_for(self.server)
        if not token:
            raise LagerError(
                f'You are not logged in to {self.server}.',
                fixes=[f'Log in: lager login {self.server}',
                       f'Or set {TOKEN_ENV} (for CI).'],
            )
        return token

    def _request(self, method, path, **kwargs):
        url = f'{self.server}/api{path}'
        headers = {'Authorization': f'Bearer {self._bearer()}'}
        try:
            resp = self._http.request(method, url, headers=headers,
                                      timeout=REQUEST_TIMEOUT, **kwargs)
        except requests.RequestException as exc:
            raise LagerError(f'Cannot reach the lease server at {self.server}.',
                             cause=str(exc)) from exc
        if resp.status_code == 401:
            raise LagerError(
                f'The lease server at {self.server} did not accept your credentials.',
                cause=_error_text(resp),
                fixes=[f'Log in again: lager login {self.server}',
                       f'Or check {TOKEN_ENV}.'],
            )
        return resp

    # -- organization ------------------------------------------------------

    def resolve_org(self):
        """Find the organization that the leases belong to.

        A scoped CI token belongs to exactly one organization. A login session
        can belong to several; ``--org`` (id, slug or name) picks one.
        """
        if self.org_id:
            return self.org_id
        if self._token and self._token.startswith(SCOPED_TOKEN_PREFIX):
            resp = self._request('GET', '/pool-tokens/self')
            data = _json_or_raise(resp, 'look up the token')
            self.org_id = data['orgId']
            return self.org_id

        resp = self._request('GET', '/auth/me')
        data = _json_or_raise(resp, 'look up your account')
        orgs = data.get('memberships') or []
        hint = (self._org_hint or '').strip()
        if hint:
            for m in orgs:
                if hint in (m.get('orgId'), m.get('orgSlug'), m.get('orgName')):
                    self.org_id = m['orgId']
                    return self.org_id
            raise LagerError(f'You are not a member of an organization named {hint!r}.',
                             cause=_org_list(orgs))
        if len(orgs) == 1:
            self.org_id = orgs[0]['orgId']
            return self.org_id
        if not orgs:
            raise LagerError('Your account is not a member of any organization.')
        raise LagerError('Your account is a member of more than one organization.',
                         cause=_org_list(orgs),
                         fixes=['Pick one: lager lease --org <slug> ...'])

    # -- leases ------------------------------------------------------------

    def acquire(self, pool, *, ttl_seconds=DEFAULT_TTL_SECONDS, context=None):
        org = self.resolve_org()
        body: dict = {'ttlSeconds': int(ttl_seconds)}
        if context:
            body['context'] = dict(context)
        resp = self._request('POST', f'/orgs/{org}/pools/{quote(pool, safe="")}/leases', json=body)
        if resp.status_code == 409:
            data = _json(resp)
            raise LeaseUnavailable(pool, data.get('reason'),
                                   matching=data.get('matching'),
                                   online=data.get('online'),
                                   message=data.get('error'))
        if resp.status_code == 404:
            raise LagerError(f'There is no pool named {pool!r}.',
                             cause=_error_text(resp),
                             fixes=['Check the pool name on the Pools page in Stout.'],
                             exit_code=EXIT_NO_LEASE)
        return _json_or_raise(resp, 'lease a box')['lease']

    def heartbeat(self, lease_id):
        """Keep the lease alive. Returns the reply; raises LeaseEnded.

        Network errors propagate as LagerError; the caller decides whether a
        missed heartbeat matters (it does not, until the TTL runs out).
        """
        org = self.resolve_org()
        resp = self._request('POST', f'/orgs/{org}/leases/{lease_id}/heartbeat')
        if resp.status_code == 409:
            data = _json(resp)
            raise LeaseEnded(data.get('endReason') or 'ended')
        return _json_or_raise(resp, 'renew the lease')

    def release(self, lease_id):
        org = self.resolve_org()
        resp = self._request('DELETE', f'/orgs/{org}/leases/{lease_id}')
        return _json_or_raise(resp, 'release the lease')


def parse_context(pairs):
    """``KEY=VALUE`` strings to a dict, refusing malformed or too many."""
    out = {}
    for pair in pairs or ():
        key, sep, value = pair.partition('=')
        key = key.strip()
        if not sep or not key:
            raise LagerError(f'--context must be KEY=VALUE, not {pair!r}.')
        out[key] = value
    if len(out) > MAX_CONTEXT_ENTRIES:
        raise LagerError(f'--context can be given at most {MAX_CONTEXT_ENTRIES} times.')
    return out


def _json(resp):
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error_text(resp):
    return _json(resp).get('error') or _json(resp).get('message') or f'HTTP {resp.status_code}'


def _json_or_raise(resp, action):
    if 200 <= resp.status_code < 300:
        return _json(resp)
    raise LagerError(f'The lease server did not {action}.',
                     cause=f'HTTP {resp.status_code}: {_error_text(resp)}')


def _org_list(orgs):
    names = [m.get('orgSlug') or m.get('orgName') or m.get('orgId') for m in orgs]
    return 'Your organizations: ' + ', '.join(sorted(n for n in names if n))

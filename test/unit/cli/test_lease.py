# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for `lager lease` and lease_client.py, against an in-process fake lease
server. The command under the lease is a real child process.
"""
import base64
import importlib
import json
import sys
import time

import pytest
import requests
from click.testing import CliRunner

from cli import gateway_auth, lease_client
from cli.errors import LagerError
from cli.lease_client import EXIT_NO_LEASE, LeaseClient, LeaseUnavailable

# The package re-exports the click command under the module's name.
lease_cmd = importlib.import_module('cli.commands.utility.lease')

SERVER = 'https://cp.example.com'
ORG = '11111111-1111-1111-1111-111111111111'
HOLDER = 'stout:L1:Dev Person (pool lease):dev@example.com'


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (lease_client.SERVER_ENV, lease_client.TOKEN_ENV,
                 gateway_auth.PINNED_TOKEN_ENV, 'LAGER_BOX', 'LAGER_LOCK_HOLDER'):
        monkeypatch.delenv(name, raising=False)


def make_jwt(**claims):
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b'=')
    payload = base64.urlsafe_b64encode(
        json.dumps({'exp': time.time() + 3600, **claims}).encode()).rstrip(b'=')
    return f'{header.decode()}.{payload.decode()}.sig'


def response(status, payload=None):
    resp = requests.Response()
    resp.status_code = status
    resp._content = json.dumps(payload or {}).encode()
    resp.headers['Content-Type'] = 'application/json'
    return resp


class FakeLeaseServer:
    """Answers the lease contract in-process and records every call."""

    def __init__(self, *, memberships=None, heartbeat_interval=0.05):
        self.calls = []
        self.memberships = memberships if memberships is not None else [
            {'orgId': ORG, 'orgSlug': 'acme', 'orgName': 'Acme'}]
        self.acquire = lambda body: response(201, {'lease': self.lease(body)})
        self.heartbeat = lambda: response(200, {'lease': {}, 'boxReachable': True})
        self.heartbeat_interval = heartbeat_interval
        self.released = []

    def lease(self, body):
        return {
            'id': 'L1',
            'pool': 'nrf52',
            'status': 'active',
            'box': {'id': 'B1', 'name': 'bench-b', 'ipAddress': '10.0.0.12'},
            'holder': HOLDER,
            'ttlSeconds': body.get('ttlSeconds', 300),
            'heartbeatIntervalSeconds': self.heartbeat_interval,
        }

    def request(self, method, url, headers=None, timeout=None, json=None):
        path = url[len(SERVER) + len('/api'):]
        self.calls.append((method, path, (headers or {}).get('Authorization'), json))
        if method == 'GET' and path == '/auth/me':
            return response(200, {'memberships': self.memberships})
        if method == 'GET' and path == '/pool-tokens/self':
            return response(200, {'orgId': ORG})
        if method == 'POST' and path.endswith('/leases'):
            return self.acquire(json or {})
        if method == 'POST' and path.endswith('/heartbeat'):
            return self.heartbeat()
        if method == 'DELETE':
            self.released.append(path)
            return response(200, {'lease': {}})
        return response(404, {'error': 'no route'})

    def paths(self, method=None):
        return [p for m, p, _, _ in self.calls if method in (None, m)]


@pytest.fixture
def server():
    return FakeLeaseServer()


@pytest.fixture
def token_client(server, monkeypatch):
    monkeypatch.setenv(lease_client.TOKEN_ENV, 'stout_agt_' + 'a' * 64)
    return LeaseClient(SERVER, session=server)


def child(code):
    """A command line that runs `code` in a fresh Python."""
    return [sys.executable, '-c', code]


# ---------------------------------------------------------------------------
# Server, credential and organization
# ---------------------------------------------------------------------------

def test_server_flag_wins_over_env_and_login(monkeypatch):
    monkeypatch.setenv(lease_client.SERVER_ENV, 'https://env.example.com')
    gateway_auth.save_login('https://login.example.com', make_jwt(), {})
    assert lease_client.resolve_server('https://flag.example.com/') == 'https://flag.example.com'


def test_server_env_wins_over_login(monkeypatch):
    monkeypatch.setenv(lease_client.SERVER_ENV, 'https://env.example.com/')
    gateway_auth.save_login('https://login.example.com', make_jwt(), {})
    assert lease_client.resolve_server(None) == 'https://env.example.com'


def test_server_from_the_one_login():
    gateway_auth.save_login('https://login.example.com', make_jwt(), {})
    assert lease_client.resolve_server(None) == 'https://login.example.com'


def test_server_missing_says_how_to_configure():
    with pytest.raises(LagerError) as err:
        lease_client.resolve_server(None)
    assert 'No lease server' in err.value.problem
    assert any('STOUT_URL' in f for f in err.value.fixes)


def test_server_ambiguous_with_two_logins():
    gateway_auth.save_login('https://a.example.com', make_jwt(), {})
    gateway_auth.save_login('https://b.example.com', make_jwt(), {})
    with pytest.raises(LagerError) as err:
        lease_client.resolve_server(None)
    assert 'more than one server' in err.value.problem


def test_blank_token_env_reads_as_unset(server, monkeypatch):
    # `STOUT_TOKEN: ${{ secrets.MISSING }}` expands to an empty string.
    monkeypatch.setenv(lease_client.TOKEN_ENV, '   ')
    client = LeaseClient(SERVER, session=server)
    with pytest.raises(LagerError) as err:
        client.resolve_org()
    assert 'not logged in' in err.value.problem


def test_scoped_token_finds_its_org_without_listing_memberships(token_client, server):
    assert token_client.resolve_org() == ORG
    assert server.paths() == ['/pool-tokens/self']
    assert server.calls[0][2] == 'Bearer stout_agt_' + 'a' * 64


def test_login_session_uses_the_one_org(server):
    token = make_jwt(email='dev@example.com')
    gateway_auth.save_login(SERVER, token, {})
    client = LeaseClient(SERVER, session=server)
    assert client.resolve_org() == ORG
    assert server.calls[0][:3] == ('GET', '/auth/me', f'Bearer {token}')


def test_login_session_in_two_orgs_needs_org():
    two = FakeLeaseServer(memberships=[
        {'orgId': ORG, 'orgSlug': 'acme', 'orgName': 'Acme'},
        {'orgId': 'other', 'orgSlug': 'beta', 'orgName': 'Beta'},
    ])
    gateway_auth.save_login(SERVER, make_jwt(), {})
    with pytest.raises(LagerError) as err:
        LeaseClient(SERVER, session=two).resolve_org()
    assert 'more than one organization' in err.value.problem
    assert LeaseClient(SERVER, org='beta', session=two).resolve_org() == 'other'


def test_unknown_org_is_refused():
    gateway_auth.save_login(SERVER, make_jwt(), {})
    with pytest.raises(LagerError) as err:
        LeaseClient(SERVER, org='nope', session=FakeLeaseServer()).resolve_org()
    assert 'nope' in err.value.problem


def test_rejected_credentials_say_how_to_fix(monkeypatch):
    class Denied(FakeLeaseServer):
        def request(self, *a, **k):
            return response(401, {'error': 'Invalid or revoked agent token'})
    monkeypatch.setenv(lease_client.TOKEN_ENV, 'stout_agt_' + 'b' * 64)
    with pytest.raises(LagerError) as err:
        LeaseClient(SERVER, session=Denied()).resolve_org()
    assert 'did not accept' in err.value.problem
    assert err.value.cause == 'Invalid or revoked agent token'


# ---------------------------------------------------------------------------
# Acquire
# ---------------------------------------------------------------------------

def test_acquire_sends_ttl_and_context(token_client, server):
    lease = token_client.acquire('nrf52', ttl_seconds=600, context={'run': 'https://ci/1'})
    assert lease['box']['ipAddress'] == '10.0.0.12'
    method, path, _, body = server.calls[-1]
    assert (method, path) == ('POST', f'/orgs/{ORG}/pools/nrf52/leases')
    assert body == {'ttlSeconds': 600, 'context': {'run': 'https://ci/1'}}


@pytest.mark.parametrize('reason, text', [
    ('all_busy', 'in use'),
    ('no_match', 'No box matches'),
    ('none_online', 'none of them is online'),
])
def test_acquire_unavailable_exits_75_with_reason(token_client, server, reason, text):
    server.acquire = lambda body: response(409, {'error': 'x', 'reason': reason,
                                                 'matching': 3, 'online': 2})
    with pytest.raises(LeaseUnavailable) as err:
        token_client.acquire('nrf52')
    assert err.value.exit_code == EXIT_NO_LEASE
    assert err.value.reason == reason
    assert text in err.value.problem
    assert '3 box(es)' in err.value.cause


def test_acquire_unknown_pool_exits_75(token_client, server):
    server.acquire = lambda body: response(404, {'error': 'Pool not found'})
    with pytest.raises(LagerError) as err:
        token_client.acquire('nope')
    assert err.value.exit_code == EXIT_NO_LEASE
    assert 'nope' in err.value.problem


def test_pool_name_is_url_encoded(token_client, server):
    token_client.acquire('a/b')
    assert server.paths('POST')[-1] == f'/orgs/{ORG}/pools/a%2Fb/leases'


# ---------------------------------------------------------------------------
# Running a command under a lease
# ---------------------------------------------------------------------------

def test_command_gets_the_box_and_the_lease_holder(token_client, server, tmp_path):
    out = tmp_path / 'env.json'
    code = (f'import json, os; json.dump({{k: os.environ.get(k) for k in '
            f'("LAGER_BOX", "LAGER_LOCK_HOLDER", "LAGER_LEASE_ID")}}, open({str(out)!r}, "w"))')
    status = lease_cmd.run_under_lease(token_client, 'nrf52', child(code))
    assert status == 0
    assert json.loads(out.read_text()) == {
        'LAGER_BOX': '10.0.0.12', 'LAGER_LOCK_HOLDER': HOLDER, 'LAGER_LEASE_ID': 'L1'}
    assert server.released == [f'/orgs/{ORG}/leases/L1']


def test_the_lease_holder_is_one_lager_treats_as_its_own():
    # The command's `lager` calls must see the lease's box lock as theirs
    # (`already_ours`), never as someone else's, and never release it.
    from cli.box_storage import holder_is_ours
    assert holder_is_ours(HOLDER, HOLDER, 'someone-else')


def test_command_exit_status_is_passed_through_and_box_released(token_client, server):
    status = lease_cmd.run_under_lease(token_client, 'nrf52', child('raise SystemExit(3)'))
    assert status == 3
    assert server.released


def test_heartbeats_while_the_command_runs(token_client, server):
    lease_cmd.run_under_lease(token_client, 'nrf52', child('import time; time.sleep(0.4)'))
    assert len(server.paths('POST')) >= 3  # acquire + at least two renewals
    assert server.paths('POST')[1] == f'/orgs/{ORG}/leases/L1/heartbeat'


def test_failed_renewals_do_not_stop_the_command(token_client, server):
    def flaky():
        raise requests.ConnectionError('network down')
    server.heartbeat = flaky
    status = lease_cmd.run_under_lease(token_client, 'nrf52',
                                       child('import time; time.sleep(0.3)'))
    assert status == 0
    assert server.released


def test_lease_ended_mid_run_stops_the_command_and_exits_75(token_client, server):
    server.heartbeat = lambda: response(409, {'reason': 'lease_ended',
                                              'endReason': 'force_released'})
    started = time.monotonic()
    with pytest.raises(LagerError) as err:
        lease_cmd.run_under_lease(token_client, 'nrf52', child('import time; time.sleep(30)'))
    assert time.monotonic() - started < 10
    assert err.value.exit_code == EXIT_NO_LEASE
    assert 'force_released' in (err.value.cause or '')
    assert server.released  # releasing an ended lease is a harmless no-op


def test_command_that_cannot_start_exits_127_and_releases(token_client, server):
    with pytest.raises(LagerError) as err:
        lease_cmd.run_under_lease(token_client, 'nrf52', ['/nonexistent/command'])
    assert err.value.exit_code == 127
    assert server.released


def test_failed_release_warns_and_keeps_the_exit_status(token_client, server, capsys):
    server.request_orig = server.request

    def no_delete(method, url, **kw):
        if method == 'DELETE':
            raise requests.ConnectionError('gone')
        return server.request_orig(method, url, **kw)
    server.request = no_delete
    status = lease_cmd.run_under_lease(token_client, 'nrf52', child('pass'))
    assert status == 0
    assert 'released automatically within 300 s' in capsys.readouterr().err


def test_death_by_signal_reports_128_plus_n():
    assert lease_cmd._exit_status(-15) == 143
    assert lease_cmd._exit_status(2) == 2


def test_parse_context():
    assert lease_client.parse_context(['run=https://x/1?a=b', 'sha=abc']) == {
        'run': 'https://x/1?a=b', 'sha': 'abc'}
    with pytest.raises(LagerError):
        lease_client.parse_context(['novalue'])
    with pytest.raises(LagerError):
        lease_client.parse_context([f'k{i}=v' for i in range(11)])


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_with(server, monkeypatch):
    monkeypatch.setenv(lease_client.TOKEN_ENV, 'stout_agt_' + 'c' * 64)
    monkeypatch.setattr(lease_cmd, '_make_client',
                        lambda srv, org: LeaseClient(SERVER, org=org, session=server))
    return lambda *args: CliRunner().invoke(lease_cmd.lease, list(args))


def test_cli_passes_everything_after_dashdash_to_the_command(cli_with, server):
    result = cli_with('--pool', 'nrf52', '--', sys.executable, '-c',
                      'import sys; raise SystemExit(len(sys.argv))', '--box', 'x')
    # argv = ['-c', '--box', 'x']: --box reached the command, not lager lease.
    assert result.exit_code == 3, result.output
    assert server.released


def test_cli_no_box_exits_75(cli_with, server):
    server.acquire = lambda body: response(409, {'error': 'busy', 'reason': 'all_busy',
                                                 'matching': 2, 'online': 2})
    result = cli_with('--pool', 'nrf52', '--', 'true')
    assert result.exit_code == EXIT_NO_LEASE
    assert 'in use' in result.output
    assert server.released == []


def test_cli_ttl_out_of_range_is_a_usage_error(cli_with):
    assert cli_with('--pool', 'nrf52', '--ttl', '10', '--', 'true').exit_code == 2


def test_cli_requires_a_command(cli_with):
    assert cli_with('--pool', 'nrf52').exit_code == 2


def test_cli_sends_context(cli_with, server):
    cli_with('--pool', 'nrf52', '--context', 'run=https://ci/9', '--', sys.executable, '-c', 'pass')
    acquire = [c for c in server.calls if c[1].endswith('/leases')][0]
    assert acquire[3]['context'] == {'run': 'https://ci/9'}


def test_help_shows_the_dashdash_usage():
    result = CliRunner().invoke(lease_cmd.lease, ['--help'])
    assert 'Usage: lease [OPTIONS] -- COMMAND [ARGS]...' in result.output
    assert '75' in result.output

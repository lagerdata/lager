# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for gateway_auth.py -- bearer-token auth for boxes behind an
authenticating gateway.
"""
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from cli import gateway_auth
from cli.errors import LagerError


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(tmp_path / 'gateway_auth.json'))
    # A pinned token outranks the store, so one exported in the developer's
    # own shell would silently rewrite what every test below exercises.
    monkeypatch.delenv(gateway_auth.PINNED_TOKEN_ENV, raising=False)
    return tmp_path / 'gateway_auth.json'


def make_jwt(exp):
    """Unsigned JWT-shaped token with the given exp; only exp is read."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b'=')
    payload = base64.urlsafe_b64encode(
        json.dumps({'exp': exp}).encode()).rstrip(b'=')
    return f'{header.decode()}.{payload.decode()}.sig'


def make_jwt_with(exp, **claims):
    """Unsigned JWT-shaped token carrying exp plus arbitrary claims."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b'=')
    payload = base64.urlsafe_b64encode(
        json.dumps({'exp': exp, **claims}).encode()).rstrip(b'=')
    return f'{header.decode()}.{payload.decode()}.sig'


def make_response(status, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return response


# ---------------------------------------------------------------------------
# Store + token expiry
# ---------------------------------------------------------------------------

def test_box_mapping_round_trip():
    assert gateway_auth.auth_server_for_box('10.0.0.5') is None
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'


def test_auth_headers_empty_for_unknown_box():
    assert gateway_auth.auth_headers_for_box('10.0.0.99') == {}


def test_auth_headers_for_gated_box_with_fresh_token():
    token = make_jwt(time.time() + 900)
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')
    gateway_auth.save_login('http://cp:3001', token, {'refresh': 'r1'})

    assert gateway_auth.auth_headers_for_box('10.0.0.5') == {
        'Authorization': f'Bearer {token}'}


def test_logout_clears_tokens():
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r'})
    gateway_auth.clear_login('http://cp:3001')
    assert gateway_auth.access_token_for('http://cp:3001') is None


def test_auth_headers_for_url_parses_host():
    token = make_jwt(time.time() + 900)
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')
    gateway_auth.save_login('http://cp:3001', token, {'refresh': 'r1'})

    assert 'Authorization' in gateway_auth.auth_headers_for_url('http://10.0.0.5:9000')


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

class FakeAuthServer:
    """Auth server stub implementing the login/refresh contract."""

    def __init__(self):
        self.refresh_calls = []
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path == '/api/auth/refresh':
                    cookie = self.headers.get('Cookie', '')
                    server_ref.refresh_calls.append(cookie)
                    body = json.dumps(
                        {'accessToken': make_jwt(time.time() + 900)}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header(
                        'Set-Cookie',
                        'refresh_token=rotated-token; Path=/api/auth; HttpOnly')
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == '/api/auth/login':
                    length = int(self.headers.get('Content-Length', 0))
                    payload = json.loads(self.rfile.read(length))
                    if payload.get('password') == 'correct':
                        body = json.dumps({
                            'accessToken': make_jwt(time.time() + 900),
                            'user': {'email': payload['email'], 'displayName': 'Ada'},
                        }).encode()
                        self.send_response(200)
                        self.send_header(
                            'Set-Cookie',
                            'refresh_token=fresh-refresh; Path=/api/auth; HttpOnly')
                    else:
                        body = json.dumps({'message': 'Invalid credentials'}).encode()
                        self.send_response(401)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def auth_server():
    server = FakeAuthServer()
    yield server
    server.stop()


def test_expired_token_is_refreshed(auth_server):
    gateway_auth.save_login(
        auth_server.url, make_jwt(time.time() - 10), {'refresh_token': 'old-refresh'})

    token = gateway_auth.access_token_for(auth_server.url)

    assert token is not None
    assert gateway_auth._token_expires_at(token) > time.time()
    assert auth_server.refresh_calls == ['refresh_token=old-refresh']
    # Rotated cookie was persisted
    store = gateway_auth._load_store()
    cookies = store['authServers'][auth_server.url]['cookies']
    assert cookies['refresh_token'] == 'rotated-token'


def test_refresh_failure_returns_none(auth_server):
    auth_server.stop()
    gateway_auth.save_login(
        auth_server.url, make_jwt(time.time() - 10), {'refresh_token': 'old-refresh'})

    assert gateway_auth.access_token_for(auth_server.url) is None


def test_login_stores_tokens(auth_server):
    user = gateway_auth.login(auth_server.url, 'ada@example.com', 'correct')

    assert user['displayName'] == 'Ada'
    store = gateway_auth._load_store()
    entry = store['authServers'][auth_server.url]
    assert entry['cookies']['refresh_token'] == 'fresh-refresh'
    assert gateway_auth.access_token_for(auth_server.url) is not None


def test_login_rejects_bad_password(auth_server):
    with pytest.raises(LagerError):
        gateway_auth.login(auth_server.url, 'ada@example.com', 'wrong')


# ---------------------------------------------------------------------------
# Gateway denial handling
# ---------------------------------------------------------------------------

def test_denial_records_mapping_and_instructs_login():
    response = make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    assert 'lager login http://cp:3001' in ' '.join(excinfo.value.fixes)
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'


def test_denial_first_contact_with_stored_login_says_rerun():
    # Logged in proactively, then touched the box before any mapping existed:
    # the request carried no token, so the fix is a re-run, not a re-login.
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    response = make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    assert 'now linked' in excinfo.value.problem
    assert 'Re-run this command.' in excinfo.value.fixes
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'


def _first_contact_401(box_ip='10.0.0.5', url='http://cp:3001'):
    """A 401 carrying the discovery header, with a prepared request that sent
    no Authorization — i.e. genuine first contact with a gated box."""
    resp = make_response(401, {gateway_auth.DISCOVERY_HEADER: url})
    prepared = requests.PreparedRequest()
    prepared.method, prepared.url, prepared.headers, prepared.body = (
        'POST', f'http://{box_ip}:9000/nets/list', {}, None)
    resp.request = prepared
    return resp


def test_check_gateway_retries_transparently_on_first_contact(monkeypatch):
    from cli import box_storage
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    resp = _first_contact_401()

    sent = {}
    def fake_send(self, prepared, **kwargs):
        sent['auth'] = prepared.headers.get('Authorization')
        return make_response(200)
    monkeypatch.setattr(requests.Session, 'send', fake_send)

    out = box_storage._check_gateway(resp, '10.0.0.5')

    # Seamless: the token was attached on the retry, and the caller gets the
    # authenticated 200 — no exception, no "re-run" message.
    assert out.status_code == 200
    assert sent['auth'].startswith('Bearer ')
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'


def test_check_gateway_retry_still_denied_raises_not_authorized(monkeypatch):
    from cli import box_storage
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    resp = _first_contact_401()

    # Token attached, but this user has no grant → gateway answers 403.
    def fake_send(self, prepared, **kwargs):
        return make_response(403, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})
    monkeypatch.setattr(requests.Session, 'send', fake_send)

    with pytest.raises(LagerError) as excinfo:
        box_storage._check_gateway(resp, '10.0.0.5')
    assert 'not authorized' in excinfo.value.problem.lower()


def test_auth_status_reports_server_user_and_boxes():
    token = make_jwt_with(exp=time.time() + 600, email='dev@example.com')
    gateway_auth.save_login('http://cp:3001', token, {'refresh': 'r1'})
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')
    gateway_auth.record_box_auth_server('10.0.0.6', 'http://cp:3001')
    gateway_auth.record_box_auth_server('10.0.0.9', 'http://other:3001')

    status = gateway_auth.auth_status()

    assert len(status) == 1
    s = status[0]
    assert s['url'] == 'http://cp:3001'
    assert s['email'] == 'dev@example.com'
    assert s['expires_in'] > 0
    assert s['refreshable'] is True
    assert s['boxes'] == ['10.0.0.5', '10.0.0.6']   # other server excluded


def test_auth_status_empty_when_signed_out():
    assert gateway_auth.auth_status() == []


def test_check_gateway_passes_through_plain_box():
    from cli import box_storage
    # No discovery header → plain Lager → untouched passthrough.
    resp = make_response(200)
    assert box_storage._check_gateway(resp, '10.0.0.5') is resp


def test_denial_with_sent_token_reports_rejected_session():
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    response = make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})
    prepared = requests.PreparedRequest()
    prepared.headers = {'Authorization': 'Bearer something'}
    response.request = prepared

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    assert 'was rejected' in excinfo.value.problem
    assert 'lager login http://cp:3001' in ' '.join(excinfo.value.fixes)


def test_denial_403_explains_missing_access():
    response = make_response(403, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    assert 'not authorized' in excinfo.value.problem.lower()


# ---------------------------------------------------------------------------
# check_gateway_status — non-raising variant for fan-out commands
# ---------------------------------------------------------------------------

def test_check_gateway_status_first_contact_retries_transparently(monkeypatch):
    from cli import box_storage
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    resp = _first_contact_401()

    sent = {}
    def fake_send(self, prepared, **kwargs):
        sent['auth'] = prepared.headers.get('Authorization')
        return make_response(200)
    monkeypatch.setattr(requests.Session, 'send', fake_send)

    out, verdict = box_storage.check_gateway_status(resp, '10.0.0.5')

    assert verdict is None
    assert out.status_code == 200
    assert sent['auth'].startswith('Bearer ')
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'


def test_check_gateway_status_sign_in_required_without_token(monkeypatch):
    from cli import box_storage
    resp = _first_contact_401()

    def fail_send(self, prepared, **kwargs):
        raise AssertionError('no retry should be attempted without a token')
    monkeypatch.setattr(requests.Session, 'send', fail_send)

    out, verdict = box_storage.check_gateway_status(resp, '10.0.0.5')

    assert verdict == 'sign-in required'
    assert out is resp
    # Discovery still recorded so the next attempt (post-login) authenticates.
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'


def test_check_gateway_status_403_and_503_do_not_retry(monkeypatch):
    from cli import box_storage
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})

    def fail_send(self, prepared, **kwargs):
        raise AssertionError('denials must not trigger a retry loop')
    monkeypatch.setattr(requests.Session, 'send', fail_send)

    resp_403 = make_response(403, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})
    _, verdict = box_storage.check_gateway_status(resp_403, '10.0.0.5')
    assert verdict == 'no access'

    resp_503 = make_response(503, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})
    _, verdict = box_storage.check_gateway_status(resp_503, '10.0.0.5')
    assert verdict == 'auth server down'


def test_check_gateway_status_rejected_session_label():
    from cli import box_storage
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    resp = make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})
    prepared = requests.PreparedRequest()
    prepared.headers = {'Authorization': 'Bearer revoked'}
    resp.request = prepared

    _, verdict = box_storage.check_gateway_status(resp, '10.0.0.5')
    assert verdict == 'session rejected'


def test_check_gateway_status_plain_401_untouched(monkeypatch):
    from cli import box_storage
    # An application 401 with no discovery header must pass through with no
    # token sent and no mapping written — plain boxes are unaffected.
    def fail_send(self, prepared, **kwargs):
        raise AssertionError('plain-box responses must never be re-sent')
    monkeypatch.setattr(requests.Session, 'send', fail_send)

    resp = make_response(401)
    out, verdict = box_storage.check_gateway_status(resp, '10.0.0.5')

    assert out is resp
    assert verdict is None
    assert gateway_auth.auth_server_for_box('10.0.0.5') is None


# ---------------------------------------------------------------------------
# ws_handshake_recovery — discovery equivalent for WebSocket handshakes
# ---------------------------------------------------------------------------

def _fake_probe(monkeypatch, response=None, exc=None):
    """Route gateway_auth's /health probe to a canned response, capturing the
    headers it carried."""
    seen = {}
    def fake_get(url, headers=None, timeout=None):
        seen['url'] = url
        seen['headers'] = dict(headers or {})
        if exc is not None:
            raise exc
        prepared = requests.PreparedRequest()
        prepared.headers = dict(headers or {})
        response.request = prepared
        return response
    monkeypatch.setattr(gateway_auth.requests, 'get', fake_get)
    return seen


def test_ws_recovery_first_contact_returns_retry_headers(monkeypatch):
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    probe = _fake_probe(monkeypatch, make_response(
        401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'}))

    headers, error = gateway_auth.ws_handshake_recovery('http://10.0.0.5:9000')

    assert error is None
    assert headers['Authorization'].startswith('Bearer ')
    assert probe['url'] == 'http://10.0.0.5:9000/health'
    assert probe['headers'] == {}     # probe mirrors the unauthenticated handshake
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'


def test_ws_recovery_without_token_returns_actionable_error(monkeypatch):
    _fake_probe(monkeypatch, make_response(
        401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'}))

    headers, error = gateway_auth.ws_handshake_recovery('http://10.0.0.5:9000')

    assert headers == {}
    assert isinstance(error, LagerError)
    assert 'lager login http://cp:3001' in ' '.join(error.fixes)


def test_ws_recovery_rejected_session_no_second_retry(monkeypatch):
    # The retried handshake carried the token and was still refused: the
    # recovery must not hand back headers again (no retry loop).
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    _fake_probe(monkeypatch, make_response(
        401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'}))

    headers, error = gateway_auth.ws_handshake_recovery(
        'http://10.0.0.5:9000', {'Authorization': 'Bearer revoked'})

    assert headers == {}
    assert isinstance(error, LagerError)
    assert 'was rejected' in error.problem


def test_ws_recovery_plain_box_untouched(monkeypatch):
    _fake_probe(monkeypatch, make_response(200))

    headers, error = gateway_auth.ws_handshake_recovery('http://10.0.0.5:9000')

    assert (headers, error) == ({}, None)
    assert gateway_auth.auth_server_for_box('10.0.0.5') is None


def test_ws_recovery_unreachable_box_defers_to_original_error(monkeypatch):
    _fake_probe(monkeypatch, exc=requests.exceptions.ConnectionError('refused'))

    assert gateway_auth.ws_handshake_recovery('http://10.0.0.5:9000') == ({}, None)


def test_hook_ignores_plain_401_without_discovery_header():
    hook = gateway_auth.gateway_response_hook('10.0.0.5')
    response = make_response(401)

    assert hook(response) is response


def test_hook_passes_through_success():
    hook = gateway_auth.gateway_response_hook('10.0.0.5')
    response = make_response(200, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    assert hook(response) is response


# ---------------------------------------------------------------------------
# Pinned token (contract §6.1) — the credential a CI job is given
# ---------------------------------------------------------------------------

PINNED = 'opaque-machine-token'


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setenv(gateway_auth.PINNED_TOKEN_ENV, PINNED)


def test_pinned_token_attaches_to_a_box_the_store_never_heard_of(pinned):
    # No mapping, no session, no prior contact: the very first request
    # already carries the token, so a CI job needs no discovery round trip.
    assert gateway_auth.auth_headers_for_box('10.0.0.99') == {
        'Authorization': f'Bearer {PINNED}'}


def test_pinned_token_leaves_no_store_behind(pinned, isolated_store):
    gateway_auth.auth_headers_for_box('10.0.0.5')
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')
    assert not isolated_store.exists()

    with pytest.raises(LagerError):
        gateway_auth.handle_gateway_denial(
            make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'}),
            '10.0.0.5')

    # A self-hosted runner keeps its filesystem between jobs; a mapping
    # written here would outlive the box address it names.
    assert not isolated_store.exists()
    assert gateway_auth.auth_server_for_box('10.0.0.5') is None


def test_pinned_token_beats_a_stored_session_for_the_same_server(pinned):
    gateway_auth.save_login('http://cp:3001', make_jwt(time.time() + 900), {'refresh': 'r1'})
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')

    assert gateway_auth.access_token_for('http://cp:3001') == PINNED
    assert gateway_auth.auth_headers_for_box('10.0.0.5') == {
        'Authorization': f'Bearer {PINNED}'}


def test_pinned_token_is_not_parsed_as_a_jwt(pinned, auth_server):
    # An opaque token has no `exp` to read. Returning it as-is is the point:
    # the old expiry test would call it stale and try to refresh it.
    gateway_auth.save_login(auth_server.url, make_jwt(time.time() - 10),
                            {'refresh_token': 'old-refresh'})

    assert gateway_auth.access_token_for(auth_server.url) == PINNED
    assert auth_server.refresh_calls == []


def test_pinned_denial_names_the_refusing_server_and_does_not_say_login(pinned):
    response = make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    err = excinfo.value
    text = ' '.join([err.problem, err.cause or '', *err.fixes])
    assert gateway_auth.PINNED_TOKEN_ENV in err.problem
    assert 'http://cp:3001' in text
    assert 'lager login' not in text


def test_pinned_denial_403_reports_the_missing_grant_not_a_sign_in(pinned):
    response = make_response(403, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    assert gateway_auth.PINNED_TOKEN_ENV in excinfo.value.problem
    assert 'signed in' not in excinfo.value.problem


def test_pinned_denial_503_still_blames_the_gateway(pinned):
    # 503 describes the gateway, not the credential, so the shared message
    # is the right one whatever the token's source.
    response = make_response(503, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    assert 'auth server is unreachable' in excinfo.value.problem


def test_pinned_denial_label_says_token_not_session(pinned):
    resp = make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})
    prepared = requests.PreparedRequest()
    prepared.headers = {'Authorization': f'Bearer {PINNED}'}
    resp.request = prepared

    assert gateway_auth.denial_label(resp) == 'token rejected'


@pytest.mark.parametrize('value', ['', '   ', '\t\n'])
def test_empty_pinned_token_is_the_same_as_unset(monkeypatch, value):
    # `LAGER_GATEWAY_TOKEN: ${{ secrets.MISSING }}` expands to the empty
    # string. It must fall through to the store, not send `Bearer `.
    monkeypatch.setenv(gateway_auth.PINNED_TOKEN_ENV, value)
    token = make_jwt(time.time() + 900)
    gateway_auth.save_login('http://cp:3001', token, {'refresh': 'r1'})
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')

    assert gateway_auth.pinned_token() is None
    assert gateway_auth.access_token_for('http://cp:3001') == token
    assert gateway_auth.auth_headers_for_box('10.0.0.5') == {
        'Authorization': f'Bearer {token}'}
    assert gateway_auth.auth_headers_for_box('10.0.0.99') == {}


def test_empty_pinned_token_still_records_discovery(monkeypatch, isolated_store):
    monkeypatch.setenv(gateway_auth.PINNED_TOKEN_ENV, '  ')
    response = make_response(401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    with pytest.raises(LagerError) as excinfo:
        gateway_auth.handle_gateway_denial(response, '10.0.0.5')

    assert 'lager login http://cp:3001' in ' '.join(excinfo.value.fixes)
    assert gateway_auth.auth_server_for_box('10.0.0.5') == 'http://cp:3001'
    assert isolated_store.exists()


def test_pinned_token_survives_surrounding_whitespace(monkeypatch):
    # Secret stores and YAML block scalars both add a trailing newline.
    monkeypatch.setenv(gateway_auth.PINNED_TOKEN_ENV, f'  {PINNED}\n')

    assert gateway_auth.auth_headers_for_box('10.0.0.5') == {
        'Authorization': f'Bearer {PINNED}'}


def test_ws_recovery_with_pinned_token_fails_without_recording(monkeypatch, pinned,
                                                               isolated_store):
    # The handshake already carried the pinned token, so there is no second
    # credential to retry with: report the refusal and write nothing.
    _fake_probe(monkeypatch, make_response(
        401, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'}))

    headers, error = gateway_auth.ws_handshake_recovery(
        'http://10.0.0.5:9000', {'Authorization': f'Bearer {PINNED}'})

    assert headers == {}
    assert gateway_auth.PINNED_TOKEN_ENV in error.problem
    assert not isolated_store.exists()

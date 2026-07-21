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


def make_jwt(exp):
    """Unsigned JWT-shaped token with the given exp; only exp is read."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b'=')
    payload = base64.urlsafe_b64encode(
        json.dumps({'exp': exp}).encode()).rstrip(b'=')
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


def test_hook_ignores_plain_401_without_discovery_header():
    hook = gateway_auth.gateway_response_hook('10.0.0.5')
    response = make_response(401)

    assert hook(response) is response


def test_hook_passes_through_success():
    hook = gateway_auth.gateway_response_hook('10.0.0.5')
    response = make_response(200, {gateway_auth.DISCOVERY_HEADER: 'http://cp:3001'})

    assert hook(response) is response

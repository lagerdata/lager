# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for browser sign-in (`lager login --web`, contract §3.4).

A fake auth server implements the two CLI-facing endpoints, and a fake
"browser" follows the link the CLI would open: it requests the loopback
callback exactly as the auth server's approval page redirects to it.
"""
import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from cli import gateway_auth
from cli.errors import LagerError


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(tmp_path / 'gateway_auth.json'))
    monkeypatch.delenv(gateway_auth.PINNED_TOKEN_ENV, raising=False)


class FakeWebAuthServer:
    """Auth server stub for §3.4: hands out one code per approved challenge
    and redeems it only with the matching PKCE verifier."""

    def __init__(self, supported=True):
        self.supported = supported
        self.codes = {}  # code -> challenge
        self.exchanges = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/api/auth/cli/config' and owner.supported:
                    self._json(200, {'authorizeUrl': f'{owner.url}/cli/authorize'})
                else:
                    self._json(404, {'message': 'Not Found'})

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length) or b'{}')
                if self.path != '/api/auth/cli/exchange':
                    self._json(404, {'message': 'Not Found'})
                    return
                owner.exchanges.append(body)
                challenge = owner.codes.pop(body.get('code'), None)
                verifier = body.get('codeVerifier', '')
                expected = base64.urlsafe_b64encode(
                    hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                if challenge is None or challenge != expected:
                    self._json(400, {'message': 'Invalid or expired login code'})
                    return
                self._json(200, {'accessToken': 'at-web', 'user': {'email': 'dev@example.com'}},
                           cookie='refresh_token=rt-web; Path=/api/auth; HttpOnly')

            def _json(self, status, payload, cookie=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                if cookie:
                    self.send_header('Set-Cookie', cookie)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def approve(self, link):
        """What the approval page does: mint a code for the link's challenge."""
        challenge = parse_qs(urlparse(link).query)['code_challenge'][0]
        code = f'code-{len(self.codes) + 1}'
        self.codes[code] = challenge
        return code

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def auth_server():
    server = FakeWebAuthServer()
    yield server
    server.stop()


def browser(monkeypatch, act):
    """Replace webbrowser.open with a 'browser' that runs act(link) on a
    thread, as a real one would load the page while the CLI waits."""
    opened = []

    def fake_open(link):
        opened.append(link)
        threading.Thread(target=act, args=(link,), daemon=True).start()
        return True

    monkeypatch.setattr(gateway_auth.webbrowser, 'open', fake_open)
    return opened


def callback(link, **query):
    params = parse_qs(urlparse(link).query)
    port = params['port'][0]
    query.setdefault('state', params['state'][0])
    return requests.get(f'http://127.0.0.1:{port}/callback', params=query, timeout=5)


def stored_session(url):
    with open(gateway_auth._store_path()) as f:
        return json.load(f)['authServers'][url]


def test_browser_sign_in_stores_the_session(auth_server, monkeypatch):
    replies = []
    browser(monkeypatch, lambda link: replies.append(
        callback(link, code=auth_server.approve(link))))
    shown = []

    user = gateway_auth.login_web(auth_server.url, show_link=lambda *a: shown.append(a),
                                  timeout=10)

    assert user == {'email': 'dev@example.com'}
    assert stored_session(auth_server.url) == {
        'accessToken': 'at-web', 'cookies': {'refresh_token': 'rt-web'}}
    assert replies[0].status_code == 200
    assert 'You can close this tab' in replies[0].text
    link, opened = shown[0]
    assert opened is True
    assert link.startswith(f'{auth_server.url}/cli/authorize?')


def test_link_carries_an_s256_challenge_never_the_verifier(auth_server, monkeypatch):
    opened = browser(monkeypatch, lambda link: callback(link, code=auth_server.approve(link)))

    gateway_auth.login_web(auth_server.url, timeout=10)

    params = parse_qs(urlparse(opened[0]).query)
    verifier = auth_server.exchanges[0]['codeVerifier']
    assert len(params['code_challenge'][0]) == 43
    assert verifier not in opened[0]
    assert len(params['state'][0]) >= 16


def test_request_with_the_wrong_state_is_ignored(auth_server, monkeypatch):
    forged = []

    def act(link):
        forged.append(callback(link, code='attacker-code', state='not-the-state'))
        callback(link, code=auth_server.approve(link))

    browser(monkeypatch, act)

    gateway_auth.login_web(auth_server.url, timeout=10)

    assert forged[0].status_code == 404
    assert [e['code'] for e in auth_server.exchanges] == ['code-1']


def test_cancel_in_the_browser_stops_the_wait(auth_server, monkeypatch):
    browser(monkeypatch, lambda link: callback(link, error='access_denied'))

    with pytest.raises(LagerError, match='cancelled'):
        gateway_auth.login_web(auth_server.url, timeout=10)
    assert auth_server.exchanges == []


def test_no_answer_from_the_browser_times_out(auth_server, monkeypatch):
    browser(monkeypatch, lambda link: None)

    with pytest.raises(LagerError, match='timed out') as err:
        gateway_auth.login_web(auth_server.url, timeout=1)
    assert any('--no-browser' in fix for fix in err.value.fixes)


def test_a_browser_that_cannot_open_still_prints_the_link(auth_server, monkeypatch):
    monkeypatch.setattr(gateway_auth.webbrowser, 'open', lambda link: False)
    shown = []

    with pytest.raises(LagerError, match='timed out'):
        gateway_auth.login_web(auth_server.url, show_link=lambda *a: shown.append(a), timeout=1)
    assert shown[0][1] is False


def test_paste_mode_prints_a_link_without_a_listener(auth_server, monkeypatch):
    monkeypatch.setattr(gateway_auth.webbrowser, 'open',
                        lambda link: pytest.fail('paste mode must not open a browser'))
    shown = []

    def paste():
        return '  ' + auth_server.approve(shown[0][0]) + '\n'

    user = gateway_auth.login_web(auth_server.url, open_browser=False,
                                  show_link=lambda *a: shown.append(a), paste_prompt=paste)

    assert user == {'email': 'dev@example.com'}
    params = parse_qs(urlparse(shown[0][0]).query)
    assert set(params) == {'code_challenge'}
    assert stored_session(auth_server.url)['accessToken'] == 'at-web'


def test_pasting_the_challenge_from_the_link_is_explained(auth_server):
    shown = []

    def paste():
        return parse_qs(urlparse(shown[0][0]).query)['code_challenge'][0]

    with pytest.raises(LagerError, match='not the sign-in code'):
        gateway_auth.login_web(auth_server.url, open_browser=False,
                               show_link=lambda *a: shown.append(a), paste_prompt=paste)
    assert auth_server.exchanges == []


def test_rejected_code_is_a_login_failure(auth_server):
    with pytest.raises(LagerError, match='Invalid or expired login code'):
        gateway_auth.login_web(auth_server.url, open_browser=False,
                               paste_prompt=lambda: 'wrong-code')
    assert not gateway_auth._store_path().exists()


def test_server_without_browser_sign_in_says_so():
    server = FakeWebAuthServer(supported=False)
    try:
        with pytest.raises(LagerError, match='does not support browser sign-in') as err:
            gateway_auth.login_web(server.url, open_browser=False, paste_prompt=lambda: 'x')
        assert err.value.fixes == [f'Sign in with a password: lager login {server.url}']
    finally:
        server.stop()


def test_unreachable_server_is_reported():
    with pytest.raises(LagerError, match='did not answer'):
        gateway_auth.login_web('http://127.0.0.1:9', open_browser=False,
                               paste_prompt=lambda: 'x')

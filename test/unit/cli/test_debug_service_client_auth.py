# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Gateway auth on the debug service client.

``DebugServiceClient`` reaches the box directly over ``http://<box>:8765``,
so on a gated box it crosses the gateway and must carry a bearer token.
``docs/reference/gateway-auth-contract.md`` §6.4 names this path
explicitly, streaming RTT included.

These tests patch ``requests.Session.send``, which sits below both
``Session.request`` (the client's own path) and the in-call retry in
``box_storage._resend_with_auth``. One seam therefore exercises the whole
flow and lets each test assert on the outgoing ``PreparedRequest``.
"""
import base64
import io
import json
import time

import pytest
import requests

from cli import gateway_auth
from cli.commands.development.debug.service_client import DebugServiceClient

BOX_IP = '10.0.0.5'
AUTH_URL = 'http://cp:3001'


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(tmp_path / 'gateway_auth.json'))


def make_jwt(exp):
    """Unsigned JWT-shaped token with the given exp; only exp is read."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b'=')
    payload = base64.urlsafe_b64encode(
        json.dumps({'exp': exp}).encode()).rstrip(b'=')
    return f'{header.decode()}.{payload.decode()}.sig'


def gated_session(token=None):
    """Record BOX_IP as gated and store a live session for its auth server."""
    gateway_auth.record_box_auth_server(BOX_IP, AUTH_URL)
    token = token or make_jwt(time.time() + 900)
    gateway_auth.save_login(AUTH_URL, token, {'refresh': 'r1'})
    return token


def build_response(prepared, status=200, body=b'{}', headers=None):
    """A response shaped like one requests would hand back from send().

    ``resp.request`` matters: ``_check_gateway`` reads it to decide whether
    the attempt carried an Authorization header, and therefore whether a
    401 is first contact (retry) or a rejected token (raise).
    """
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp.raw = io.BytesIO(body)
    resp.request = prepared
    return resp


class Recorder:
    """Records each prepared request and the send() kwargs it was sent with."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []
        self.kwargs = []

    # No `self`-binding happens: a Recorder instance patched onto the class is
    # not a descriptor, so it is called without the Session it replaced a
    # method on. The signature is (prepared, **kwargs), not (session, prepared).
    def __call__(self, prepared, **kwargs):
        self.sent.append(prepared)
        self.kwargs.append(kwargs)
        make = self.responses[min(len(self.sent) - 1, len(self.responses) - 1)]
        return make(prepared)

    def auth_on(self, index):
        return self.sent[index].headers.get('Authorization')


def install(monkeypatch, recorder):
    monkeypatch.setattr(requests.Session, 'send', recorder)
    return recorder


def ok(body=b'{}'):
    return lambda prepared: build_response(prepared, 200, body)


def denial(status, body=b'{}'):
    return lambda prepared: build_response(
        prepared, status, body, {gateway_auth.DISCOVERY_HEADER: AUTH_URL})


def client():
    return DebugServiceClient(BOX_IP, ssh_tunnel=False)


# ---------------------------------------------------------------------------
# Proactive attach (contract §6.2)
# ---------------------------------------------------------------------------

def test_plain_box_sends_no_authorization(monkeypatch):
    """An un-gated box stays zero-overhead: nothing stored, nothing sent."""
    rec = install(monkeypatch, Recorder(ok()))

    client().health_check()

    assert len(rec.sent) == 1
    assert rec.auth_on(0) is None


def test_gated_box_attaches_bearer_on_first_request(monkeypatch):
    token = gated_session()
    rec = install(monkeypatch, Recorder(ok()))

    client().get_info({'name': 'debug1'})

    assert len(rec.sent) == 1, 'should not need a denial round trip'
    assert rec.auth_on(0) == f'Bearer {token}'


def test_token_resolved_per_call_not_at_construction(monkeypatch):
    """The client is long-lived; the token must be read at request time.

    A gdbserver session constructs the client once and issues its last
    request hours later, so a header cached in __init__ would go stale.
    Here the login happens after construction and must still be picked up.
    """
    rec = install(monkeypatch, Recorder(ok()))
    c = client()                      # constructed before any login exists

    token = gated_session()           # ... login happens afterwards
    c.get_status()

    assert rec.auth_on(0) == f'Bearer {token}'


def test_loopback_client_keys_auth_on_box_host(monkeypatch):
    """base_url may be 127.0.0.1, but the store is keyed by box IP."""
    token = gated_session()
    rec = install(monkeypatch, Recorder(ok()))

    c = DebugServiceClient(BOX_IP, ssh_tunnel=True)
    assert c.base_url.startswith('http://127.0.0.1')
    c.health_check()

    assert rec.auth_on(0) == f'Bearer {token}'


def test_authorization_is_never_cached_on_the_session(monkeypatch):
    gated_session()
    install(monkeypatch, Recorder(ok()))

    c = client()
    c.health_check()

    assert 'Authorization' not in c.session.headers


# ---------------------------------------------------------------------------
# First-contact retry (contract §6.3)
# ---------------------------------------------------------------------------

def test_first_contact_401_records_mapping_and_retries_in_call(monkeypatch):
    """Holding a session but not yet knowing the box is gated.

    The mapping is learned from this very 401's discovery header, so the
    first attempt necessarily goes out bare. The caller should never see
    the round trip.
    """
    token = make_jwt(time.time() + 900)
    gateway_auth.save_login(AUTH_URL, token, {'refresh': 'r1'})
    assert gateway_auth.auth_server_for_box(BOX_IP) is None

    rec = install(monkeypatch, Recorder(denial(401), ok(b'{"ok": true}')))

    result = client().get_info({'name': 'debug1'})

    assert result == {'ok': True}
    assert len(rec.sent) == 2
    assert rec.auth_on(0) is None
    assert rec.auth_on(1) == f'Bearer {token}'
    assert gateway_auth.auth_server_for_box(BOX_IP) == AUTH_URL


def test_genuine_denial_exits_with_actionable_error(monkeypatch, capsys):
    """403 means signed in but not granted -- no retry, actionable error.

    _check_gateway raises LagerError, which _request converts to SystemExit
    via die(): the debug subcommands wrap client calls in broad
    `except Exception` handlers that would otherwise flatten the styled
    error into a single bare line.
    """
    gated_session()
    install(monkeypatch, Recorder(denial(403)))

    with pytest.raises(SystemExit):
        client().get_status()

    assert 'not authorized' in capsys.readouterr().err


def test_plain_401_without_discovery_header_still_raises_http_error(monkeypatch):
    """An ordinary box 401 is not a gateway denial and must stay an HTTPError.

    Four handlers in debug/commands.py catch HTTPError to pull the box's
    JSON error body out; _check_gateway must not intercept those.
    """
    install(monkeypatch, Recorder(lambda p: build_response(p, 401, b'{}')))

    with pytest.raises(requests.HTTPError):
        client().get_status()


# ---------------------------------------------------------------------------
# Streaming (contract §6.4 -- "including streaming (RTT) requests")
# ---------------------------------------------------------------------------

def test_rtt_stream_carries_bearer_and_stream_kwargs(monkeypatch):
    token = gated_session()
    rec = install(monkeypatch, Recorder(ok(b'log output')))

    chunks = b''.join(client().rtt({'name': 'debug1'}))

    assert chunks == b'log output'
    assert rec.auth_on(0) == f'Bearer {token}'
    assert rec.kwargs[0]['stream'] is True
    assert rec.kwargs[0]['timeout'] is None


def test_rtt_first_contact_retry_stays_streaming(monkeypatch):
    """The retry must mirror the original call's stream/timeout.

    _resend_with_auth used to hardcode stream=False, timeout=30. Replaying
    an RTT POST that way blocks inside send() buffering a body that never
    ends, and consumes it so iter_content yields nothing.
    """
    token = make_jwt(time.time() + 900)
    gateway_auth.save_login(AUTH_URL, token, {'refresh': 'r1'})

    rec = install(monkeypatch, Recorder(denial(401), ok(b'log output')))

    chunks = b''.join(client().rtt({'name': 'debug1'}))

    assert chunks == b'log output'
    assert len(rec.sent) == 2
    assert rec.auth_on(1) == f'Bearer {token}'
    assert rec.kwargs[1]['stream'] is True, 'retry dropped streaming'
    assert rec.kwargs[1]['timeout'] is None, 'retry imposed a timeout'


# ---------------------------------------------------------------------------
# Coverage of the remaining box-contacting methods
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('call', [
    lambda c: c.health_check(),
    lambda c: c.get_status(),
    lambda c: c.get_debug_status(),
    lambda c: c.get_service_health(),
    lambda c: c.get_service_health(detailed=True),
    lambda c: c.get_info({'name': 'debug1'}),
    lambda c: c.connect({'name': 'debug1'}),
    lambda c: c.disconnect({'name': 'debug1'}),
    lambda c: c.reset({'name': 'debug1'}),
    lambda c: c.erase({'name': 'debug1'}),
], ids=['health_check', 'get_status', 'get_debug_status', 'health', 'health_detailed',
        'get_info', 'connect', 'disconnect', 'reset', 'erase'])
def test_every_json_method_attaches_the_bearer(monkeypatch, call):
    token = gated_session()
    rec = install(monkeypatch, Recorder(ok()))

    call(client())

    assert rec.auth_on(0) == f'Bearer {token}'


# ---------------------------------------------------------------------------
# /debug/status must carry the net (#162)
# ---------------------------------------------------------------------------

def test_get_debug_status_sends_the_net(monkeypatch):
    """REGRESSION: this posted `{}`.

    The box resolves the probe serial from the net and checks that probe's
    pidfile. With no net it fell back to the legacy un-suffixed pidfile, which
    a serial-aware box never writes -- so a running gdbserver reported
    `connected: False`. `flash` believed it, erased and reconnected, killed the
    live session, and wedged the probe for every later command on that net.
    """
    rec = install(monkeypatch, Recorder(ok()))
    net = {'name': 'debug1', 'instrument': 'jlink', 'address': 'USB::123456'}

    client().get_debug_status(net)

    body = json.loads(rec.sent[0].body)
    assert body['net'] == net, f'/debug/status must carry the net, got {body!r}'


def test_get_debug_status_without_a_net_still_sends_a_dict(monkeypatch):
    """The box does `data.get('net') or {}`, so None must not reach the wire
    as a null that later indexing would trip over."""
    rec = install(monkeypatch, Recorder(ok()))

    client().get_debug_status()

    body = json.loads(rec.sent[0].body)
    assert body['net'] == {}


def test_flash_sends_the_jlink_script(monkeypatch, tmp_path):
    """REGRESSION: `flash` sent no script.

    The box's last-resort resolution is a single shared temp file written by
    whichever net connected most recently, so flashing net A could silently
    run net B's script.
    """
    rec = install(monkeypatch, Recorder(ok()))
    fw = tmp_path / 'fw.hex'
    fw.write_bytes(b':00000001FF\n')

    client().flash(fw, file_type='hex', net={'name': 'debug1'},
                   jlink_script='YmFzZTY0')

    body = json.loads(rec.sent[0].body)
    assert body['jlink_script'] == 'YmFzZTY0'
    assert body['net'] == {'name': 'debug1'}


def test_read_memory_attaches_bearer_and_decodes(monkeypatch):
    token = gated_session()
    rec = install(monkeypatch, Recorder(ok(b'{"data": "deadbeef"}')))

    assert client().read_memory({'name': 'debug1'}, 0x20000000) == b'\xde\xad\xbe\xef'
    assert rec.auth_on(0) == f'Bearer {token}'


def test_flash_attaches_bearer(monkeypatch, tmp_path):
    token = gated_session()
    rec = install(monkeypatch, Recorder(ok()))
    firmware = tmp_path / 'firmware.hex'
    firmware.write_bytes(b':00000001FF\n')

    client().flash(firmware)

    assert rec.auth_on(0) == f'Bearer {token}'

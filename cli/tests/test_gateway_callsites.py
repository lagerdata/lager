# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the gateway-auth discovery gap across CLI call sites: every code
path that talks to a box must record the box→auth-server mapping on a
discovery 401, retry once with a held token, and surface (not swallow, not
raw-print) genuine denials. Fan-out commands must keep rendering the other
boxes' rows.
"""
import json
import time

import pytest
import requests

from cli import gateway_auth
from cli.errors import LagerError
from cli.tests.test_gateway_auth import make_jwt


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(tmp_path / 'gateway_auth.json'))
    monkeypatch.setenv('LAGER_CONFIG_FILE_DIR', str(tmp_path))


def make_json_response(status, payload=None, headers=None, request_headers=None,
                       url='http://10.0.0.5:9000/x'):
    """requests.Response with a JSON body and an attached prepared request,
    matching what a live call would hand to the gateway check."""
    resp = requests.Response()
    resp.status_code = status
    resp.headers.update(headers or {})
    resp._content = json.dumps(payload if payload is not None else {}).encode()
    prepared = requests.PreparedRequest()
    prepared.method, prepared.url, prepared.body = 'GET', url, None
    prepared.headers = dict(request_headers or {})
    resp.request = prepared
    return resp


GATED_IP = '10.0.0.5'
PLAIN_IP = '10.0.0.6'
CP = 'http://cp:3001'


def _gated_401(request_headers=None):
    return make_json_response(
        401, {'error': 'authorization required'},
        headers={gateway_auth.DISCOVERY_HEADER: CP},
        request_headers=request_headers,
    )


# ---------------------------------------------------------------------------
# lager boxes — fan-out must render every row
# ---------------------------------------------------------------------------

def _run_list_boxes(monkeypatch, capsys, saved_boxes, fake_get, fake_send=None):
    import importlib
    # `from cli.commands.box import boxes` resolves to the click group the
    # package re-exports, not the module — import the module explicitly.
    boxes_mod = importlib.import_module('cli.commands.box.boxes')
    monkeypatch.setattr(boxes_mod, 'list_boxes', lambda: saved_boxes)
    monkeypatch.setattr(requests, 'get', fake_get)
    if fake_send is not None:
        monkeypatch.setattr(requests.Session, 'send', fake_send)
    boxes_mod._list_boxes_live(timeout=1)
    return capsys.readouterr().out


def test_boxes_fanout_gated_box_gets_status_others_still_render(monkeypatch, capsys):
    # No stored session: the gated box's row must read "sign-in required"
    # (not "HTTP 401"), the plain box must render normally, nothing raises.
    from cli import __version__ as cli_version

    def fake_get(url, timeout=None, headers=None):
        if GATED_IP in url:
            return _gated_401(request_headers=headers)
        if url.endswith('/lock'):
            return make_json_response(200, {'locked': False}, request_headers=headers)
        return make_json_response(200, {'version': cli_version}, request_headers=headers)

    out = _run_list_boxes(
        monkeypatch, capsys,
        {'GATED': GATED_IP, 'PLAIN': PLAIN_IP},
        fake_get,
    )

    assert 'sign-in required' in out
    assert 'HTTP 401' not in out
    assert 'current' in out                     # the plain box still rendered
    assert f'lager login {CP}' in out           # actionable follow-up printed
    # Discovery recorded, so the next run authenticates.
    assert gateway_auth.auth_server_for_box(GATED_IP) == CP


def test_boxes_fanout_with_session_authenticates_with_one_round_trip(monkeypatch, capsys):
    # Holding a valid session: first contact discovers on /lock, retries it
    # with the token, and /status then authenticates up front — exactly one
    # extra round trip for the whole box, none for the plain box.
    from cli import __version__ as cli_version
    gateway_auth.save_login(CP, make_jwt(time.time() + 900), {'refresh': 'r1'})

    sends = []

    def fake_send(self, prepared, **kwargs):
        sends.append(prepared.url)
        assert prepared.headers.get('Authorization', '').startswith('Bearer ')
        return make_json_response(200, {'locked': False})

    def fake_get(url, timeout=None, headers=None):
        headers = headers or {}
        if GATED_IP in url:
            if 'Authorization' not in headers:
                return _gated_401(request_headers=headers)
            return make_json_response(200, {'version': cli_version},
                                      request_headers=headers)
        if url.endswith('/lock'):
            return make_json_response(200, {'locked': False}, request_headers=headers)
        return make_json_response(200, {'version': cli_version}, request_headers=headers)

    out = _run_list_boxes(
        monkeypatch, capsys,
        {'GATED': GATED_IP, 'PLAIN': PLAIN_IP},
        fake_get, fake_send,
    )

    assert len(sends) == 1                      # one discovery retry, total
    assert 'sign-in required' not in out
    assert 'HTTP 401' not in out
    assert out.count('current') >= 2            # both boxes report a version


# ---------------------------------------------------------------------------
# lager uart — an auth problem must not read as "no instruments"
# ---------------------------------------------------------------------------

def test_uart_instruments_query_raises_on_gateway_denial(monkeypatch):
    from cli.commands.communication.uart import _run_query_instruments

    monkeypatch.setattr(
        requests, 'get',
        lambda url, timeout=None, headers=None: _gated_401(request_headers=headers))

    with pytest.raises(LagerError) as excinfo:
        _run_query_instruments(None, GATED_IP)
    assert 'lager login' in ' '.join(excinfo.value.fixes)


def test_uart_instruments_query_still_swallows_transport_errors(monkeypatch):
    from cli.commands.communication.uart import _run_query_instruments

    def fake_get(url, timeout=None, headers=None):
        raise requests.exceptions.ConnectionError('refused')
    monkeypatch.setattr(requests, 'get', fake_get)

    assert _run_query_instruments(None, PLAIN_IP) == []


# ---------------------------------------------------------------------------
# version-skew probe — fail-open, but discovery still happens
# ---------------------------------------------------------------------------

def test_version_skew_check_is_silent_on_gateway_denial(monkeypatch, capsys):
    from cli.core import version_skew
    version_skew.reset_cache_for_tests()

    monkeypatch.setattr(
        requests, 'get',
        lambda url, timeout=None, headers=None: _gated_401(request_headers=headers))

    version_skew.check_and_warn(GATED_IP, 'GATED')   # must not raise

    assert capsys.readouterr().err == ''
    assert gateway_auth.auth_server_for_box(GATED_IP) == CP

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the request-identification headers (gateway auth contract §6.5):
which command a request belongs to, sent only where a bearer token goes.
"""
import base64
import json
import time

import click
import pytest
from click.testing import CliRunner

from cli import __version__, gateway_auth
from cli.context.session import DirectHTTPSession

PINNED = 'opaque-machine-token'


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv('LAGER_GATEWAY_AUTH_FILE', str(tmp_path / 'gateway_auth.json'))
    monkeypatch.delenv(gateway_auth.PINNED_TOKEN_ENV, raising=False)


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setenv(gateway_auth.PINNED_TOKEN_ENV, PINNED)


def headers_inside(argv, box_ip='10.0.0.5', extra_command=None):
    """Run a stand-in `lager` CLI and return the headers its command built."""
    seen = {}

    @click.group(name='lager', invoke_without_command=True)
    @click.pass_context
    def lager(ctx):
        if ctx.invoked_subcommand is None:
            seen['headers'] = gateway_auth.auth_headers_for_box(box_ip)

    @lager.group()
    def supply():
        pass

    @supply.command()
    @click.argument('net')
    @click.argument('volts')
    def voltage(net, volts):
        seen['headers'] = gateway_auth.auth_headers_for_box(box_ip)

    if extra_command is not None:
        @lager.command(name=extra_command)
        def odd():
            seen['headers'] = gateway_auth.auth_headers_for_box(box_ip)

    result = CliRunner().invoke(lager, argv, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return seen['headers']


def test_nothing_is_added_outside_a_command(pinned):
    # A library caller or a test has no command to name: the headers are
    # exactly the bearer, as before.
    assert gateway_auth.invocation_headers() == {}
    assert gateway_auth.auth_headers_for_box('10.0.0.5') == {
        'Authorization': f'Bearer {PINNED}'}


def test_names_the_command_and_never_its_arguments(pinned):
    headers = headers_inside(['supply', 'voltage', 'VCC_MAIN', '3.3'])

    assert headers == {
        'Authorization': f'Bearer {PINNED}',
        'Lager-Version': __version__,
        'Lager-Invocation-Id': gateway_auth.INVOCATION_ID,
        'Lager-Command': 'supply voltage',
    }
    sent = ' '.join(headers.values())
    assert 'VCC_MAIN' not in sent
    assert '3.3' not in sent


def test_a_gated_box_from_the_store_carries_them_too():
    gateway_auth.record_box_auth_server('10.0.0.5', 'http://cp:3001')
    gateway_auth.save_login('http://cp:3001', _fresh_jwt(), {'refresh': 'r1'})

    headers = headers_inside(['supply', 'voltage', 'VCC', '5'])

    assert headers['Lager-Command'] == 'supply voltage'
    assert headers['Authorization'].startswith('Bearer ')


def test_a_plain_box_gets_nothing():
    # No token, so no gateway to tell: a box that is not gated sees exactly
    # what it saw before this header existed.
    assert headers_inside(['supply', 'voltage', 'VCC', '5'], box_ip='10.0.0.99') == {}


def test_the_bare_program_names_no_command(pinned):
    headers = headers_inside([])

    assert 'Lager-Command' not in headers
    assert headers['Lager-Invocation-Id'] == gateway_auth.INVOCATION_ID


def test_characters_that_could_break_a_header_are_dropped(pinned):
    headers = headers_inside(['we\r\nird: x'], extra_command='we\r\nird: x')

    assert headers['Lager-Command'] == 'weird x'


def test_one_invocation_id_for_every_request_in_the_process(pinned):
    first = headers_inside(['supply', 'voltage', 'VCC', '5'])
    second = headers_inside(['supply', 'voltage', 'VCC', '3.3'], box_ip='10.0.0.6')
    session = DirectHTTPSession('10.0.0.77')

    assert first['Lager-Invocation-Id'] == second['Lager-Invocation-Id']
    assert session.session.headers['Lager-Invocation-Id'] == gateway_auth.INVOCATION_ID


def _fresh_jwt():
    def b64(data):
        return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

    payload = json.dumps({'exp': time.time() + 900}).encode()
    return f'{b64(b"{}")}.{b64(payload)}.sig'

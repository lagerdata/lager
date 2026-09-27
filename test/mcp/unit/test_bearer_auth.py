# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.auth -- the optional bearer token on the MCP port.

The middleware is driven as a raw ASGI callable around a trivial inner app.
That keeps the MCP server singleton out of it, and it needs no HTTP client:
Starlette's ``TestClient`` imports ``httpx2`` or ``httpx`` depending on the
Starlette release the resolver picked, and neither is a declared dependency
here.

The properties that matter, in order: no token file means nothing changes; a
token file the server cannot use means CLOSED, never open; the inner app is
never reached by a request that was refused; and the secret reaches no log.
"""

import asyncio
import logging
import os

import pytest

from lager.mcp.auth import BearerTokenMiddleware

TOKEN = "s3cr3t-Value_for-the.test-0123456789abcdefghi"
OTHER = "an0th3r-Value_for-the.test-0123456789abcdefgh"
assert len(TOKEN) == len(OTHER)  # a rotate that keeps the size is the hard case


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Inner:
    """Stands in for the MCP app. Records every scope type that reaches it."""

    def __init__(self):
        self.reached = []

    async def __call__(self, scope, receive, send):
        self.reached.append(scope["type"])
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"inner"})
        elif scope["type"] == "websocket":
            await send({"type": "websocket.accept"})


def _call(mw, *, headers=(), scope_type="http"):
    """Send one request through ``mw``; return the ASGI messages it sent."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": scope_type, "method": "POST", "path": "/mcp", "headers": list(headers)}
    _run(mw(scope, receive, send))
    return sent


def _status(sent):
    return sent[0]["status"]


def _bearer(value, scheme=b"Bearer "):
    raw = value if isinstance(value, bytes) else value.encode()
    return [(b"authorization", scheme + raw)]


@pytest.fixture
def token_file(tmp_path):
    return tmp_path / "mcp_token"


@pytest.fixture
def inner():
    return _Inner()


@pytest.fixture
def mw(inner, token_file):
    return BearerTokenMiddleware(inner, token_file)


@pytest.mark.unit
class TestDisabledIsUnchanged:
    def test_no_token_file_passes_every_request_through(self, mw, inner):
        sent = _call(mw)
        assert _status(sent) == 200
        assert inner.reached == ["http"]

    def test_no_token_file_ignores_a_header_that_is_sent_anyway(self, mw, inner):
        assert _status(_call(mw, headers=_bearer(TOKEN))) == 200
        assert inner.reached == ["http"]


@pytest.mark.unit
class TestEnabled:
    def test_a_request_with_no_header_is_refused_with_a_challenge(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        sent = _call(mw)
        assert _status(sent) == 401
        assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]
        assert inner.reached == []

    def test_a_wrong_token_is_refused(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(OTHER))) == 401
        assert inner.reached == []

    def test_the_right_token_reaches_the_app(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(TOKEN))) == 200
        assert inner.reached == ["http"]

    @pytest.mark.parametrize("scheme", [b"bearer ", b"BEARER ", b"BeArEr "])
    def test_the_scheme_is_case_insensitive(self, mw, token_file, scheme):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(TOKEN, scheme))) == 200

    def test_the_header_name_is_case_insensitive(self, mw, token_file):
        token_file.write_text(TOKEN + "\n")
        headers = [(b"Authorization", b"Bearer " + TOKEN.encode())]
        assert _status(_call(mw, headers=headers)) == 200

    def test_whitespace_round_the_file_and_the_header_is_ignored(self, mw, token_file):
        token_file.write_text("\n  " + TOKEN + "  \n\n")
        headers = [(b"authorization", b"Bearer   " + TOKEN.encode() + b"  ")]
        assert _status(_call(mw, headers=headers)) == 200

    @pytest.mark.parametrize("presented", [TOKEN[:-1], TOKEN + "x", "", " "])
    def test_a_prefix_an_extension_and_an_empty_value_are_all_wrong(self, mw, inner, token_file, presented):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(presented))) == 401
        assert inner.reached == []

    def test_another_scheme_is_refused(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(TOKEN, b"Basic "))) == 401
        assert inner.reached == []

    def test_bytes_that_are_not_ascii_are_a_wrong_token_not_a_server_error(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(b"\xff\xfe\x00caf\xc3\xa9"))) == 401
        assert inner.reached == []


@pytest.mark.unit
class TestFailClosed:
    """A token file the server cannot use must never mean "open"."""

    def _assert_closed(self, mw, inner):
        for headers in ((), _bearer(TOKEN)):
            sent = _call(mw, headers=headers)
            assert _status(sent) == 503
            # No challenge: the fault is the file on the box, not the client.
            assert all(name != b"www-authenticate" for name, _ in sent[0]["headers"])
        assert inner.reached == []

    def test_an_empty_file(self, mw, inner, token_file):
        token_file.write_text("")
        self._assert_closed(mw, inner)

    def test_a_file_of_whitespace(self, mw, inner, token_file):
        token_file.write_text(" \n\n")
        self._assert_closed(mw, inner)

    def test_a_directory_where_the_file_belongs(self, mw, inner, token_file):
        token_file.mkdir()
        self._assert_closed(mw, inner)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_a_file_this_user_cannot_read(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        token_file.chmod(0o000)
        try:
            self._assert_closed(mw, inner)
        finally:
            token_file.chmod(0o600)

    def test_a_stat_that_fails_for_any_reason_but_absence(self, mw, inner, token_file, monkeypatch):
        import lager.mcp.auth as auth

        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if os.fspath(path) == os.fspath(token_file):
                raise PermissionError(13, "Permission denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(auth.os, "stat", stat)
        self._assert_closed(mw, inner)


@pytest.mark.unit
class TestChangesTakeEffectWithoutARestart:
    def test_enabling_after_the_server_started(self, mw, token_file):
        assert _status(_call(mw)) == 200
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw)) == 401

    def test_a_rotate_of_the_same_size_is_honoured_by_the_same_instance(self, mw, token_file, tmp_path):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(TOKEN))) == 200
        # What `rotate` does: a new file moved over the old one.
        replacement = tmp_path / "mcp_token.new"
        replacement.write_text(OTHER + "\n")
        os.replace(replacement, token_file)
        assert _status(_call(mw, headers=_bearer(TOKEN))) == 401
        assert _status(_call(mw, headers=_bearer(OTHER))) == 200

    def test_disabling_reopens_the_server(self, mw, token_file):
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw)) == 401
        token_file.unlink()
        assert _status(_call(mw)) == 200

    def test_a_repaired_file_is_picked_up(self, mw, token_file):
        token_file.write_text("")
        assert _status(_call(mw, headers=_bearer(TOKEN))) == 503
        token_file.write_text(TOKEN + "\n")
        assert _status(_call(mw, headers=_bearer(TOKEN))) == 200


@pytest.mark.unit
class TestOtherScopes:
    def test_lifespan_is_never_gated_even_when_the_file_is_unusable(self, mw, inner, token_file):
        token_file.write_text("")
        assert _call(mw, scope_type="lifespan") == []
        assert inner.reached == ["lifespan"]

    def test_a_websocket_without_the_token_is_closed_before_accept(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        sent = _call(mw, scope_type="websocket")
        assert sent == [{"type": "websocket.close", "code": 1008}]
        assert inner.reached == []

    def test_a_websocket_with_the_token_reaches_the_app(self, mw, inner, token_file):
        token_file.write_text(TOKEN + "\n")
        sent = _call(mw, scope_type="websocket", headers=_bearer(TOKEN))
        assert sent == [{"type": "websocket.accept"}]
        assert inner.reached == ["websocket"]


@pytest.mark.unit
class TestIsolationAndLogging:
    def test_two_instances_do_not_share_a_token(self, tmp_path):
        # The cache is per instance. A module-level one would let the first
        # app's file answer for the second.
        file_a, file_b = tmp_path / "a", tmp_path / "b"
        file_a.write_text(TOKEN + "\n")
        file_b.write_text(OTHER + "\n")
        mw_a = BearerTokenMiddleware(_Inner(), file_a)
        mw_b = BearerTokenMiddleware(_Inner(), file_b)
        assert _status(_call(mw_a, headers=_bearer(TOKEN))) == 200
        assert _status(_call(mw_b, headers=_bearer(TOKEN))) == 401
        assert _status(_call(mw_b, headers=_bearer(OTHER))) == 200
        assert _status(_call(mw_a, headers=_bearer(OTHER))) == 401

    def test_one_log_line_per_state_change_not_per_request(self, mw, token_file, caplog):
        caplog.set_level(logging.INFO, logger="lager.mcp.auth")
        token_file.write_text(TOKEN + "\n")
        for _ in range(3):
            _call(mw, headers=_bearer(TOKEN))
        assert len(caplog.records) == 1
        token_file.write_text("")
        for _ in range(3):
            _call(mw)
        assert len(caplog.records) == 2
        assert caplog.records[1].levelno == logging.ERROR

    def test_neither_the_token_nor_a_presented_value_reaches_the_log(self, mw, token_file, caplog):
        caplog.set_level(logging.DEBUG)
        token_file.write_text(TOKEN + "\n")
        _call(mw)
        _call(mw, headers=_bearer(OTHER))
        _call(mw, headers=_bearer(TOKEN))
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert TOKEN not in logged
        assert OTHER not in logged

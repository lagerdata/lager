# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.server.build_app and the startup posture lines.

``build_app`` is what puts the bearer check in front of the SDK's app, so the
first thing pinned here is that it really is in the request path: a request
with no token is refused by the BUILT app, not only by the middleware class in
isolation (``test_bearer_auth.py`` covers the class).

Every build uses a throwaway ``MCPServer``. The real one is an import-time
singleton, and building an app on it replaces its session manager.
"""

import asyncio
import logging

import pytest
from mcp.server.mcpserver import MCPServer

from lager.constants import MCP_TOKEN_PATH
from lager.mcp import server as server_mod
from lager.mcp.auth import BearerTokenMiddleware
from lager.mcp.server import _log_security_posture, build_app

TOKEN = "s3cr3t-Value_for-the.test-0123456789abcdefghi"


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _post_mcp(app, headers=()):
    """One POST /mcp through the whole app. Returns the ASGI messages sent."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/mcp", "raw_path": b"/mcp",
        "query_string": b"", "root_path": "", "headers": list(headers),
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8100),
    }
    _run(app(scope, receive, send))
    return sent


@pytest.fixture
def token_file(tmp_path):
    return tmp_path / "mcp_token"


@pytest.mark.unit
class TestBuildApp:
    def test_the_bearer_check_is_the_apps_middleware(self, token_file):
        app = build_app(server=MCPServer("t"), token_path=token_file)
        assert [m.cls for m in app.user_middleware] == [BearerTokenMiddleware]
        assert app.user_middleware[0].kwargs == {"token_path": token_file}

    def test_the_default_token_path_is_the_box_constant(self):
        app = build_app(server=MCPServer("t"))
        assert app.user_middleware[0].kwargs == {"token_path": MCP_TOKEN_PATH}
        assert MCP_TOKEN_PATH == "/etc/lager/mcp_token"

    def test_the_built_app_refuses_a_request_with_no_token(self, token_file):
        token_file.write_text(TOKEN + "\n")
        sent = _post_mcp(build_app(server=MCPServer("t"), token_path=token_file))
        assert sent[0]["status"] == 401
        assert (b"www-authenticate", b"Bearer") in sent[0]["headers"]

    def test_the_built_app_fails_closed_on_an_empty_token_file(self, token_file):
        token_file.write_text("")
        header = [(b"authorization", b"Bearer " + TOKEN.encode())]
        sent = _post_mcp(build_app(server=MCPServer("t"), token_path=token_file), header)
        assert sent[0]["status"] == 503

    @pytest.mark.parametrize("enabled", [False, True])
    def test_a_request_that_is_let_through_reaches_the_sdk_app(self, token_file, enabled):
        # The lifespan was never entered, so the SDK app cannot serve this: it
        # raises, or answers 5xx. Either is proof the request got PAST the
        # check -- which answers 401 or 503 itself and never raises.
        headers = []
        if enabled:
            token_file.write_text(TOKEN + "\n")
            headers = [(b"authorization", b"Bearer " + TOKEN.encode())]
        app = build_app(server=MCPServer("t"), token_path=token_file)
        try:
            sent = _post_mcp(app, headers)
        except Exception:  # pylint: disable=broad-except
            return
        assert sent[0]["status"] not in (401, 503)

    def test_each_app_runs_the_session_manager_its_own_route_uses(self, token_file):
        # The SDK makes a new session manager per build and re-points
        # server.session_manager at it, and a manager's run() works once. An
        # app whose lifespan looked the manager up late would start the NEWER
        # app's manager and leave its own route's manager unstarted.
        srv = MCPServer("t")
        first_app = build_app(server=srv, token_path=token_file)
        first_manager = srv.session_manager
        build_app(server=srv, token_path=token_file)
        second_manager = srv.session_manager
        assert first_manager is not second_manager

        async def scenario():
            async with first_app.router.lifespan_context(first_app):
                # Started by first_app's lifespan, so a second run() refuses.
                with pytest.raises(RuntimeError):
                    async with first_manager.run():
                        pass
                # Untouched by it, so this one still starts.
                async with second_manager.run():
                    pass

        _run(scenario())


@pytest.mark.unit
class TestSecurityPosture:
    @pytest.fixture(autouse=True)
    def _tiers_off(self, monkeypatch):
        # The tier switches are read from the environment when the function
        # runs, so each test states the ones it means.
        monkeypatch.delenv("LAGER_MCP_ALLOW_CONTROL", raising=False)
        monkeypatch.delenv("LAGER_MCP_ALLOW_EXEC", raising=False)

    def _warnings(self, caplog):
        return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    @pytest.mark.parametrize("var, tier", [
        ("LAGER_MCP_ALLOW_CONTROL", "control"),
        ("LAGER_MCP_ALLOW_EXEC", "exec"),
    ])
    def test_a_tier_with_no_token_is_warned_about_by_name(self, token_file, caplog, monkeypatch, var, tier):
        caplog.set_level(logging.INFO, logger="lager.mcp.server")
        monkeypatch.setenv(var, "1")
        _log_security_posture(token_file)
        unguarded = [m for m in self._warnings(caplog) if "NO bearer token" in m]
        assert len(unguarded) == 1
        assert f"MCP {tier} tools" in unguarded[0]
        assert "lager box-config mcp-token enable" in unguarded[0]

    def test_a_tier_with_a_token_is_not_warned_about(self, token_file, caplog, monkeypatch):
        caplog.set_level(logging.INFO, logger="lager.mcp.server")
        monkeypatch.setenv("LAGER_MCP_ALLOW_CONTROL", "1")
        token_file.write_text(TOKEN + "\n")
        _log_security_posture(token_file)
        assert self._warnings(caplog) == []
        assert any("bearer token required" in r.getMessage() for r in caplog.records)

    def test_the_read_only_default_with_no_token_is_not_a_warning(self, token_file, caplog):
        caplog.set_level(logging.INFO, logger="lager.mcp.server")
        _log_security_posture(token_file)
        assert self._warnings(caplog) == []
        assert [r.levelno for r in caplog.records] == [logging.INFO]

    def test_an_unusable_token_file_is_an_error(self, token_file, caplog):
        caplog.set_level(logging.INFO, logger="lager.mcp.server")
        token_file.write_text("")
        _log_security_posture(token_file)
        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "empty" in errors[0]

    def test_the_exec_tier_is_still_announced_now_that_it_is_said_here(self, token_file, caplog, monkeypatch):
        # These two lines used to be logged at import, before logging was
        # configured: the INFO one was dropped and the WARNING came out bare.
        caplog.set_level(logging.INFO, logger="lager.mcp.server")
        monkeypatch.setenv("LAGER_MCP_ALLOW_CONTROL", "1")
        monkeypatch.setenv("LAGER_MCP_ALLOW_EXEC", "1")
        token_file.write_text(TOKEN + "\n")
        _log_security_posture(token_file)
        messages = [r.getMessage() for r in caplog.records]
        assert any("control tools enabled" in m for m in messages)
        assert any("EXEC tools enabled" in m for m in messages)

    def test_the_token_is_never_logged(self, token_file, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG)
        monkeypatch.setenv("LAGER_MCP_ALLOW_EXEC", "1")
        token_file.write_text(TOKEN + "\n")
        _log_security_posture(token_file)
        assert TOKEN not in " ".join(r.getMessage() for r in caplog.records)


@pytest.mark.unit
class TestMain:
    def test_main_logs_the_posture_and_serves_the_built_app(self, monkeypatch):
        import uvicorn

        served, order = {}, []
        built = object()
        monkeypatch.setattr(server_mod.logging, "basicConfig", lambda **_kw: None)
        monkeypatch.setattr("lager.mcp.server_state.init_state", lambda: None)
        monkeypatch.setattr(server_mod, "_log_security_posture", lambda: order.append("posture"))
        monkeypatch.setattr(server_mod, "build_app", lambda: order.append("build") or built)
        monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))

        server_mod.main()

        assert order == ["posture", "build"]
        assert served["app"] is built
        assert served["port"] == 8100

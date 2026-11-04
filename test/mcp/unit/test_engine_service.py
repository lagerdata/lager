# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.engine_service -- the bench engine over HTTP.

Driven as a raw ASGI callable, like the MCP auth tests: no HTTP client is a
declared dependency. What matters: every view answers what ``engine.fleet``
answers for the same input, a body that is not a manifest is refused with a
reason, a newer manifest is still answered, the token is enforced on every
view but the health check, and an oversized body is refused before it is
parsed.
"""

import asyncio
import json
from pathlib import Path

import pytest

from lager.mcp.engine import fleet
from lager.mcp.engine_service import build_app
from lager.mcp.engine_service.app import MAX_BODY_BYTES, VIEWS

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "fleet-manifest-v1.json").read_text())


def _call(app, method, path, body=None, headers=(), raw=None, chunks=None):
    """One request through ``app``; return (status, decoded JSON body)."""
    payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
    parts = list(chunks) if chunks is not None else [payload]
    sent = []

    async def receive():
        if parts:
            part = parts.pop(0)
            return {"type": "http.request", "body": part, "more_body": bool(parts)}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    hdrs = [(b"content-type", b"application/json")] + [(k.encode(), v.encode()) for k, v in headers]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": hdrs, "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8200),
    }
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app(scope, receive, send))
    finally:
        loop.close()
    status = sent[0]["status"]
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, (json.loads(data) if data else None)


@pytest.fixture
def app():
    return build_app()


def test_healthz_reports_version_and_schema(app, monkeypatch):
    monkeypatch.setenv("LAGER_ENGINE_VERSION", "9.9.9")
    monkeypatch.setenv("LAGER_ENGINE_REVISION", "abc123")
    status, body = _call(app, "GET", "/healthz")
    assert status == 200
    assert body["engine_version"] == "9.9.9" and body["revision"] == "abc123"
    assert body["max_schema_version"] == 1
    assert body["views"] == sorted(VIEWS)


@pytest.mark.parametrize(
    "view, args, expected",
    [
        ("summary", {}, lambda m: fleet.summary(m)),
        ("net", {"net": "vin"}, lambda m: {"net": fleet.net_detail(m, "vin")}),
        ("net", {"net": "nope"}, lambda m: {"net": None}),
        ("dut", {}, lambda m: fleet.dut_context(m)),
        ("dut-overview", {}, lambda m: {"markdown": fleet.dut_overview(m)}),
        ("cite", {"net": "vin"}, lambda m: fleet.cite(m, "vin")),
        ("reference", {"nets": ["vin"], "include_gotchas": True}, lambda m: fleet.reference(m, ["vin"], True)),
        ("suitability", {"description": "uart_loopback"}, lambda m: fleet.suitability(m, "uart_loopback")),
    ],
)
def test_each_view_answers_what_the_engine_answers(app, view, args, expected):
    status, body = _call(app, "POST", f"/v1/{view}", {"manifest": MANIFEST, **args})
    assert status == 200
    assert body == json.loads(json.dumps(expected(MANIFEST)))


def test_messages_are_passed_through(app):
    no_nets = {**MANIFEST, "bench": {**MANIFEST["bench"], "nets": []}}
    _, body = _call(app, "POST", "/v1/summary", {"manifest": no_nets, "messages": {"no_nets": "custom"}})
    assert body["warning"] == "custom"
    status, body = _call(app, "POST", "/v1/summary", {"manifest": MANIFEST, "messages": {"bogus": "x"}})
    assert status == 422 and "unknown message key" in body["error"]


def test_search(app):
    benches = [
        {"key": "a", "box_id": "a", "capability_bindings": MANIFEST["bench"]["capability_bindings"]},
        {"key": "b", "box_id": "b", "capability_bindings": "nope"},
    ]
    status, body = _call(app, "POST", "/v1/search", {"description": "uart_loopback", "benches": benches})
    assert status == 200
    assert body["reports"]["a"]["report"]["box_id"] == "a"
    assert "error" in body["reports"]["b"]


@pytest.mark.parametrize(
    "body, fragment",
    [
        ({}, "manifest must be an object"),
        ({"manifest": {"bench": {}}}, "schema_version"),
        ({"manifest": {"schema_version": 0, "bench": {}}}, "schema_version"),
        ({"manifest": {"schema_version": True, "bench": {}}}, "schema_version"),
        ({"manifest": {"schema_version": 1, "bench": []}}, "bench must be an object"),
    ],
)
def test_a_body_that_is_not_a_manifest_is_refused(app, body, fragment):
    status, out = _call(app, "POST", "/v1/summary", body)
    assert status == 422 and fragment in out["error"]


def test_bad_arguments_are_refused(app):
    assert _call(app, "POST", "/v1/net", {"manifest": MANIFEST})[0] == 422
    assert _call(app, "POST", "/v1/suitability", {"manifest": MANIFEST, "description": ""})[0] == 422
    assert _call(app, "POST", "/v1/reference", {"manifest": MANIFEST, "nets": "vin"})[0] == 422
    assert _call(app, "POST", "/v1/reference", {"manifest": MANIFEST, "include_gotchas": "yes"})[0] == 422
    assert _call(app, "POST", "/v1/search", {"description": "x", "benches": {}})[0] == 422


def test_a_newer_manifest_is_still_answered(app):
    newer = {**MANIFEST, "schema_version": 2, "field_from_the_future": {"x": 1}}
    status, body = _call(app, "POST", "/v1/summary", {"manifest": newer})
    assert status == 200 and body["box_id"] == "box-fixture"


def test_non_json_and_non_object_bodies(app):
    assert _call(app, "POST", "/v1/summary", raw=b"{not json")[0] == 400
    assert _call(app, "POST", "/v1/summary", raw=b"[1, 2]")[0] == 422


def test_oversized_body_is_refused_by_declared_length(app):
    status, body = _call(app, "POST", "/v1/summary", headers=[("content-length", str(MAX_BODY_BYTES + 1))], raw=b"{}")
    assert status == 413


def test_oversized_body_is_refused_while_streaming(app):
    chunk = b" " * (MAX_BODY_BYTES // 2 + 1)
    status, _ = _call(app, "POST", "/v1/summary", chunks=[chunk, chunk, b"{}"])
    assert status == 413


def test_wrong_method_and_unknown_path_answer_json(app):
    status, body = _call(app, "GET", "/v1/summary")
    assert status == 405 and body["error"]
    status, body = _call(app, "POST", "/v1/nope", {"manifest": MANIFEST})
    assert status == 404 and body["error"]


def test_token_is_required_on_views_but_not_health():
    app = build_app("s3cret")
    assert _call(app, "GET", "/healthz")[0] == 200
    assert _call(app, "POST", "/v1/summary", {"manifest": MANIFEST})[0] == 401
    assert _call(app, "POST", "/v1/summary", {"manifest": MANIFEST}, headers=[("x-lager-engine-token", "wrong")])[0] == 401
    ok = _call(app, "POST", "/v1/summary", {"manifest": MANIFEST}, headers=[("x-lager-engine-token", "s3cret")])
    assert ok[0] == 200


def test_main_refuses_a_bad_port(monkeypatch, capsys):
    from lager.mcp.engine_service.__main__ import main

    monkeypatch.setenv("LAGER_ENGINE_PORT", "eighty")
    assert main() == 2
    assert "LAGER_ENGINE_PORT" in capsys.readouterr().err


def test_main_binds_loopback_by_default(monkeypatch):
    import uvicorn

    from lager.mcp.engine_service.__main__ import main

    seen = {}
    monkeypatch.delenv("LAGER_ENGINE_HOST", raising=False)
    monkeypatch.delenv("LAGER_ENGINE_PORT", raising=False)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert main() == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8200

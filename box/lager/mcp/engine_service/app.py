# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Routes of the bench engine service.

``GET /healthz`` reports the engine's version and the newest manifest
``schema_version`` it was written against. Each ``POST /v1/<view>`` takes a
JSON body with the manifest and the view's arguments and answers with that
view from ``engine.fleet``:

=================  ==========================================================
``summary``        ``{manifest, messages?}``
``net``            ``{manifest, net}`` -> ``{net: <detail> | null}``
``dut``            ``{manifest, messages?}``
``dut-overview``   ``{manifest, messages?}`` -> ``{markdown}``
``cite``           ``{manifest, net, messages?}``
``reference``      ``{manifest, nets?, include_gotchas?, include_examples?, messages?}``
``suitability``    ``{manifest, description}`` -> ``{requirement, report}``
``search``         ``{description, benches: [{key, box_id, capability_bindings}]}``
=================  ==========================================================

A manifest newer than ``max_schema_version`` is still answered: fields are
only ever added within a schema, and the views read the stable ones. A body
that is not a manifest at all is a 422 that says why.

When a token is configured every route but ``/healthz`` requires it in
``X-Lager-Engine-Token``. The service is meant to listen on loopback next to
its one caller; the token is for hosts where that is not the case.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Awaitable, Callable

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..engine import fleet
from ..schemas.manifest import MANIFEST_SCHEMA_VERSION

#: Largest request body accepted, in bytes. One manifest with its reference
#: entries is tens of kilobytes; a search sends bindings only.
MAX_BODY_BYTES = 2 * 1024 * 1024
#: Most benches one search may carry.
MAX_SEARCH_BENCHES = 2000
#: Longest test description accepted.
MAX_DESCRIPTION_LEN = 4000

TOKEN_HEADER = "x-lager-engine-token"


class _Rejected(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _read_json(request: Request) -> dict:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _Rejected(413, f"request body over {MAX_BODY_BYTES} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _Rejected(413, f"request body over {MAX_BODY_BYTES} bytes")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks) or b"null")
    except (ValueError, UnicodeDecodeError) as exc:
        raise _Rejected(400, f"body is not JSON: {exc}") from None
    if not isinstance(body, dict):
        raise _Rejected(422, "body must be a JSON object")
    return body


def _manifest(body: dict) -> dict:
    manifest = body.get("manifest")
    if not isinstance(manifest, dict):
        raise _Rejected(422, "manifest must be an object")
    version = manifest.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise _Rejected(422, "manifest.schema_version must be an integer of at least 1")
    if not isinstance(manifest.get("bench"), dict):
        raise _Rejected(422, "manifest.bench must be an object")
    return manifest


def _string(body: dict, key: str, max_len: int = MAX_DESCRIPTION_LEN) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise _Rejected(422, f"{key} must be a non-empty string of at most {max_len} characters")
    return value


def _flag(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise _Rejected(422, f"{key} must be a boolean")
    return value


def _messages(body: dict) -> dict | None:
    value = body.get("messages")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _Rejected(422, "messages must be an object")
    try:
        return fleet.resolve_messages(value)
    except ValueError as exc:
        raise _Rejected(422, str(exc)) from None


def _nets(body: dict) -> list[str] | None:
    value = body.get("nets")
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise _Rejected(422, "nets must be a list of strings")
    return value


# --- views ------------------------------------------------------------------------


def _summary(body: dict) -> Any:
    return fleet.summary(_manifest(body), _messages(body))


def _net(body: dict) -> Any:
    return {"net": fleet.net_detail(_manifest(body), _string(body, "net", 512))}


def _dut(body: dict) -> Any:
    return fleet.dut_context(_manifest(body), _messages(body))


def _dut_overview(body: dict) -> Any:
    return {"markdown": fleet.dut_overview(_manifest(body), _messages(body))}


def _cite(body: dict) -> Any:
    return fleet.cite(_manifest(body), _string(body, "net", 512), _messages(body))


def _reference(body: dict) -> Any:
    return fleet.reference(
        _manifest(body),
        _nets(body),
        include_gotchas=_flag(body, "include_gotchas"),
        include_examples=_flag(body, "include_examples"),
        messages=_messages(body),
    )


def _suitability(body: dict) -> Any:
    return fleet.suitability(_manifest(body), _string(body, "description"))


def _search(body: dict) -> Any:
    description = _string(body, "description")
    benches = body.get("benches")
    if not isinstance(benches, list):
        raise _Rejected(422, "benches must be a list")
    if len(benches) > MAX_SEARCH_BENCHES:
        raise _Rejected(422, f"at most {MAX_SEARCH_BENCHES} benches per search")
    return fleet.search(description, benches)


VIEWS: dict[str, Callable[[dict], Any]] = {
    "summary": _summary,
    "net": _net,
    "dut": _dut,
    "dut-overview": _dut_overview,
    "cite": _cite,
    "reference": _reference,
    "suitability": _suitability,
    "search": _search,
}


def engine_version() -> str:
    return os.environ.get("LAGER_ENGINE_VERSION", "") or "dev"


def build_app(token: str | None = None) -> Starlette:
    """The service app. ``token``, when non-empty, is required on every view."""
    expected = token.encode("utf-8") if token else None

    def authorized(request: Request) -> bool:
        if expected is None:
            return True
        presented = request.headers.get(TOKEN_HEADER, "").encode("utf-8")
        return hmac.compare_digest(presented, expected)

    def view_endpoint(view: Callable[[dict], Any]) -> Callable[[Request], Awaitable[JSONResponse]]:
        async def endpoint(request: Request) -> JSONResponse:
            if not authorized(request):
                return _error(401, "missing or wrong engine token")
            try:
                return JSONResponse(view(await _read_json(request)))
            except _Rejected as exc:
                return _error(exc.status, exc.message)

        return endpoint

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "engine_version": engine_version(),
                "revision": os.environ.get("LAGER_ENGINE_REVISION", ""),
                "max_schema_version": MANIFEST_SCHEMA_VERSION,
                "views": sorted(VIEWS),
            }
        )

    routes = [Route("/healthz", healthz, methods=["GET"])]
    routes += [Route(f"/v1/{name}", view_endpoint(view), methods=["POST"]) for name, view in VIEWS.items()]
    async def http_error(_request: Request, exc: Exception) -> JSONResponse:
        # Unknown path or wrong method: JSON like every other refusal, so a
        # client always finds the reason in ``error``.
        status = getattr(exc, "status_code", 500)
        return _error(status, str(getattr(exc, "detail", "")) or "error")

    return Starlette(routes=routes, exception_handlers={HTTPException: http_error})


__all__ = ["MAX_BODY_BYTES", "TOKEN_HEADER", "VIEWS", "build_app", "engine_version"]

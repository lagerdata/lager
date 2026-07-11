# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""In-memory fake of the box's :9000 nets/instruments/custom-devices HTTP API.

`lager nets` talks to the box exclusively over HTTP (requests.request); tests
patch ``requests.request`` with :meth:`FakeBoxHTTP.request` to replace the box
with this in-memory implementation. Route semantics mirror
``box/lager/http_handlers/{nets,instruments,custom_devices}_handler.py``.

Custom-device routes delegate to :meth:`custom_list` / :meth:`custom_assign`
/ :meth:`custom_remove`, which raise by default — subclass and override them
in tests that exercise ``lager nets assign``.
"""

from __future__ import annotations

from urllib.parse import unquote, urlparse


class FakeResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeBoxHTTP:
    def __init__(self, instruments=None):
        self.instruments: list[dict] = list(instruments or [])
        self.saved_nets: list[dict] = []
        # (method, path, body, params) tuples for request-shape assertions.
        self.calls: list[tuple] = []

    # Signature-compatible with requests.request as nets.py calls it.
    def request(self, method, url, json=None, params=None, timeout=None, **kwargs):
        path = unquote(urlparse(url).path)
        body = json or {}
        params = params or {}
        self.calls.append((method.upper(), path, body, params))
        return self._route(method.upper(), path, body, params)

    def _route(self, method, path, body, params) -> FakeResponse:
        if method == "GET" and path == "/nets/list":
            return FakeResponse(200, [dict(n) for n in self.saved_nets])

        if method == "GET" and path == "/instruments/list":
            return FakeResponse(200, [dict(i) for i in self.instruments])

        if method == "PUT" and path.startswith("/nets/"):
            name = path[len("/nets/"):]
            if not body.get("name") or not body.get("role") or not body.get("instrument"):
                return FakeResponse(
                    400, {"error": "name, role, and instrument are required"})
            if body["name"] != name:  # rename semantics: drop the old entry
                self.saved_nets = [n for n in self.saved_nets
                                   if n.get("name") != name]
            # save_local_net upserts by name+role.
            self.saved_nets = [
                n for n in self.saved_nets
                if not (n.get("name") == body["name"]
                        and n.get("role") == body.get("role"))
            ]
            self.saved_nets.append(dict(body))
            return FakeResponse(200, {"ok": True})

        if method == "DELETE" and path == "/nets":
            self.saved_nets = []
            return FakeResponse(200, {"ok": True})

        if method == "DELETE" and path.startswith("/nets/"):
            name = path[len("/nets/"):]
            role = params.get("role")
            before = len(self.saved_nets)
            self.saved_nets = [
                n for n in self.saved_nets
                if not (n.get("name") == name
                        and (role is None or n.get("role") == role))
            ]
            if len(self.saved_nets) == before:
                return FakeResponse(404, {"error": "Net not found"})
            return FakeResponse(200, {"ok": True})

        if path == "/custom-devices/list" and method == "GET":
            return self.custom_list()
        if path == "/custom-devices/assign" and method == "POST":
            return self.custom_assign(body)
        if path == "/custom-devices/remove" and method == "POST":
            return self.custom_remove(body)

        raise AssertionError(f"unexpected request {method} {path}")

    # ------------------------------------------------------------------ #
    # custom-devices hooks (override in assign tests)                     #
    # ------------------------------------------------------------------ #

    def custom_list(self) -> FakeResponse:
        raise AssertionError("unexpected /custom-devices/list")

    def custom_assign(self, payload: dict) -> FakeResponse:
        raise AssertionError("unexpected /custom-devices/assign")

    def custom_remove(self, payload: dict) -> FakeResponse:
        raise AssertionError("unexpected /custom-devices/remove")

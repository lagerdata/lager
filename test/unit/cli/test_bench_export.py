# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``lager bench export``.

The command fetches ``GET /bench`` from the box's :9000 API and hands the
manifest on unchanged. What it must get right is the failure text: a 404
from a box that predates the route is an update prompt, not "no bench", and
a reply that is not a manifest is refused rather than written to a file.
"""

import json
from unittest.mock import patch

from click.testing import CliRunner

from cli.commands.box.bench import box_bench

_MANIFEST = {
    "schema_version": 1,
    "generated_at": "2026-09-23T10:00:00Z",
    "content_hash": "0123456789abcdef" * 4,
    "box_id": "BX-3",
    "bench": {"box_id": "BX-3", "nets": []},
    "reference_keys": {},
}


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text or payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _invoke(args, response=None, side_effect=None):
    runner = CliRunner()
    with patch("cli.commands.box.bench._resolve_box", return_value="10.0.0.5"), \
         patch("cli.commands.box.bench.auth_headers_for_box", return_value={}), \
         patch("cli.commands.box.bench._check_gateway", side_effect=lambda resp, ip: resp), \
         patch("cli.commands.box.bench.requests.get",
               return_value=response, side_effect=side_effect) as get:
        result = runner.invoke(box_bench, args)
    return result, get


class TestExport:
    def test_prints_the_manifest_as_sorted_json(self):
        result, get = _invoke(["export", "--box", "bx3"], _Response(200, _MANIFEST))
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == _MANIFEST
        assert result.output.index('"bench"') < result.output.index('"schema_version"')
        url, kwargs = get.call_args[0][0], get.call_args[1]
        assert url == "http://10.0.0.5:9000/bench"
        assert kwargs["timeout"] == 30

    def test_compact_is_one_line(self):
        result, _ = _invoke(["export", "--compact"], _Response(200, _MANIFEST))
        assert result.exit_code == 0
        assert result.output.count("\n") == 1

    def test_out_writes_the_file_and_reports_on_stderr(self, tmp_path):
        out = tmp_path / "bx3.json"
        result, _ = _invoke(["export", "--out", str(out)], _Response(200, _MANIFEST))
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text()) == _MANIFEST
        assert "BX-3" in result.output
        assert "schema 1" in result.output
        assert _MANIFEST["content_hash"][:12] in result.output

    def test_404_is_an_update_prompt(self):
        result, _ = _invoke(["export"], _Response(404, {"error": "not found"}))
        assert result.exit_code == 1
        assert "0.50.0 or later" in result.output
        assert "lager update --box 10.0.0.5" in result.output

    def test_other_http_errors_show_the_status_and_body(self):
        result, _ = _invoke(["export"], _Response(500, {"error": "bench manifest unavailable: x"}))
        assert result.exit_code == 1
        assert "HTTP 500" in result.output
        assert "bench manifest unavailable" in result.output

    def test_connection_failure_points_at_hello(self):
        import requests

        result, _ = _invoke(["export"], side_effect=requests.exceptions.ConnectionError("refused"))
        assert result.exit_code == 1
        assert "refused" in result.output
        assert "lager hello" in result.output

    def test_a_reply_that_is_not_a_manifest_is_refused(self, tmp_path):
        out = tmp_path / "never.json"
        for bad in (_Response(200, {"nets": []}), _Response(200, None, text="<html>")):
            result, _ = _invoke(["export", "--out", str(out)], bad)
            assert result.exit_code == 1
            assert "did not parse as a manifest" in result.output
        assert not out.exists()

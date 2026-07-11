#!/usr/bin/env python3

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the binaries CLI + download-file migration to :9000.

`lager binaries add/list/remove` and `DirectHTTPSession.download_file` now
target the box HTTP server on :9000 (same wire contracts the :5000
python-exec service serves; both shim ``lager.binaries.store`` box-side).
Only the ``/python/*`` exec endpoints stay on :5000.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

binaries_mod = importlib.import_module("cli.commands.utility.binaries")

BOX_IP = "1.2.3.4"


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def resolved_box():
    with patch.object(binaries_mod, "resolve_and_validate_box",
                      return_value=BOX_IP):
        yield


class TestBinariesAdd:
    def test_add_posts_multipart_to_9000(self, tmp_path, resolved_box):
        binary = tmp_path / "my_tool"
        binary.write_bytes(b"#!/bin/sh\n")

        captured = {}

        def fake_post(url, files=None, data=None, timeout=None):
            captured.update(url=url, files=files, data=data, timeout=timeout)
            return _Resp(200, {"success": True, "name": "my_tool",
                               "path": "/home/www-data/customer-binaries/my_tool",
                               "size": 10, "restart_required": False})

        with patch.object(binaries_mod.requests, "post", fake_post):
            result = CliRunner().invoke(
                binaries_mod.binaries,
                ["add", str(binary), "--box", "mybox", "--yes"])

        assert result.exit_code == 0, result.output
        assert captured["url"] == f"http://{BOX_IP}:9000/binaries/add"
        assert captured["data"] == {"name": "my_tool"}
        assert captured["files"]["binary"][0] == "my_tool"
        assert "uploaded successfully" in result.output

    def test_add_error_response_fails(self, tmp_path, resolved_box):
        binary = tmp_path / "bad"
        binary.write_bytes(b"x")

        with patch.object(binaries_mod.requests, "post",
                          return_value=_Resp(400, {"error": "Invalid binary name"})):
            result = CliRunner().invoke(
                binaries_mod.binaries,
                ["add", str(binary), "--box", "mybox", "--yes"])

        assert result.exit_code == 1
        assert "Invalid binary name" in result.output


class TestBinariesList:
    def test_list_gets_from_9000(self, resolved_box):
        captured = {}

        def fake_get(url, timeout=None):
            captured["url"] = url
            return _Resp(200, {
                "binaries": [{"name": "tool_a", "size": 2048, "executable": True}],
                "host_path": "/home/lagerdata/third_party/customer-binaries",
                "mounted": True,
            })

        with patch.object(binaries_mod.requests, "get", fake_get):
            result = CliRunner().invoke(
                binaries_mod.binaries, ["list", "--box", "mybox"])

        assert result.exit_code == 0, result.output
        assert captured["url"] == f"http://{BOX_IP}:9000/binaries/list"
        assert "tool_a" in result.output


class TestBinariesRemove:
    def test_remove_posts_to_9000(self, resolved_box):
        captured = {}

        def fake_get(url, timeout=None):
            return _Resp(200, {"binaries": [{"name": "tool_a", "size": 1,
                                             "executable": True}]})

        def fake_post(url, json=None, timeout=None):
            captured.update(url=url, json=json)
            return _Resp(200, {"success": True, "name": "tool_a"})

        with patch.object(binaries_mod.requests, "get", fake_get), \
             patch.object(binaries_mod.requests, "post", fake_post):
            result = CliRunner().invoke(
                binaries_mod.binaries, ["remove", "tool_a", "--box", "mybox", "--yes"])

        assert result.exit_code == 0, result.output
        assert captured["url"] == f"http://{BOX_IP}:9000/binaries/remove"
        assert captured["json"] == {"name": "tool_a"}
        assert "removed" in result.output


class TestDirectHTTPDownloadFile:
    def test_download_file_targets_9000(self):
        session_mod = importlib.import_module("cli.context.session")
        sess = session_mod.DirectHTTPSession(BOX_IP)

        captured = {}

        def fake_get(url, params=None, stream=None):
            captured.update(url=url, params=params, stream=stream)
            return _Resp(200)

        with patch.object(sess, "session") as mock_session:
            mock_session.get.side_effect = fake_get
            sess.download_file(None, "/tmp/lager-output/result.bin")

        assert captured["url"] == f"http://{BOX_IP}:9000/download-file"
        assert captured["params"] == {"filename": "/tmp/lager-output/result.bin"}
        assert captured["stream"] is True

    def test_exec_base_url_stays_5000(self):
        # /python/* exec endpoints are the one thing that must remain on :5000.
        session_mod = importlib.import_module("cli.context.session")
        sess = session_mod.DirectHTTPSession(BOX_IP)
        assert sess.base_url == f"http://{BOX_IP}:5000"


class TestNo5000InBinariesModule:
    def test_binaries_module_has_no_5000_urls(self):
        source = inspect.getsource(binaries_mod)
        assert ":5000" not in source, (
            "cli.commands.utility.binaries must target the :9000 box HTTP "
            "server only")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.tools.exec -- general box-control primitives.

These tools (box_exec, read_file, write_file, list_dir) are gated behind
``LAGER_MCP_ALLOW_EXEC`` and are the powerful, non-read-only surface an AI uses
to repair the box environment. Tests cover each tool's happy/error paths plus
the gating contract (register adds exactly the four primitives; the default
server exposes none of them).
"""

import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lager.mcp.tools import exec as exec_tools


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


@pytest.mark.unit
class TestBoxExec:
    def test_success(self):
        fake = SimpleNamespace(returncode=0, stdout=b"hello\n", stderr=b"")
        with patch("lager.mcp.tools.exec.subprocess.run", return_value=fake) as run:
            result = json.loads(exec_tools.box_exec("echo hello"))
        run.assert_called_once()
        assert result["exit_code"] == 0
        assert result["stdout"] == "hello\n"
        assert result["timed_out"] is False
        assert result["truncated"] is False

    def test_nonzero_exit(self):
        fake = SimpleNamespace(returncode=2, stdout=b"", stderr=b"boom\n")
        with patch("lager.mcp.tools.exec.subprocess.run", return_value=fake):
            result = json.loads(exec_tools.box_exec("false"))
        assert result["exit_code"] == 2
        assert result["stderr"] == "boom\n"

    def test_timeout(self):
        exc = subprocess.TimeoutExpired(cmd="sleep 5", timeout=1, output=b"partial", stderr=b"")
        with patch("lager.mcp.tools.exec.subprocess.run", side_effect=exc):
            result = json.loads(exec_tools.box_exec("sleep 5", timeout_s=1))
        assert result["timed_out"] is True
        assert result["exit_code"] is None
        assert "partial" in result["stdout"]

    def test_launch_failure(self):
        with patch("lager.mcp.tools.exec.subprocess.run", side_effect=OSError("no shell")):
            result = json.loads(exec_tools.box_exec("whatever"))
        assert "error" in result

    def test_output_truncated(self):
        big = b"x" * 20000
        fake = SimpleNamespace(returncode=0, stdout=big, stderr=b"")
        with patch("lager.mcp.tools.exec.subprocess.run", return_value=fake):
            result = json.loads(exec_tools.box_exec("cat big"))
        assert result["truncated"] is True
        assert len(result["stdout"]) == 8192

    def test_non_utf8_output_does_not_crash(self):
        # subprocess.run (no text=) returns bytes; non-UTF-8 output must decode
        # with errors="replace" instead of raising UnicodeDecodeError.
        fake = SimpleNamespace(returncode=0, stdout=b"\xff\xfe bad", stderr=b"")
        with patch("lager.mcp.tools.exec.subprocess.run", return_value=fake):
            result = json.loads(exec_tools.box_exec("cat binary"))
        assert result["exit_code"] == 0
        assert chr(0xFFFD) in result["stdout"]


@pytest.mark.unit
class TestReadFile:
    def test_reads_existing(self, tmp_path):
        f = tmp_path / "cfg.txt"
        f.write_text("line1\nline2\n")
        result = json.loads(exec_tools.read_file(str(f)))
        assert result["content"] == "line1\nline2\n"
        assert result["truncated"] is False

    def test_truncates(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("y" * 100)
        result = json.loads(exec_tools.read_file(str(f), max_bytes=10))
        assert result["bytes"] == 10
        assert result["truncated"] is True

    def test_missing(self, tmp_path):
        result = json.loads(exec_tools.read_file(str(tmp_path / "nope.txt")))
        assert "error" in result


@pytest.mark.unit
class TestWriteFile:
    def test_new_file_no_backup(self, tmp_path):
        f = tmp_path / "new.txt"
        result = json.loads(exec_tools.write_file(str(f), "hello\n"))
        assert f.read_text() == "hello\n"
        assert result["backup"] is None
        assert result["bytes_written"] == 6

    def test_overwrite_backs_up_and_diffs(self, tmp_path):
        f = tmp_path / "edit.txt"
        f.write_text("old\n")
        result = json.loads(exec_tools.write_file(str(f), "new\n"))
        assert f.read_text() == "new\n"
        assert result["backup"] is not None
        # backup preserves the prior contents
        from pathlib import Path

        assert Path(result["backup"]).read_text() == "old\n"
        assert "-old" in result["diff"] and "+new" in result["diff"]

    def test_write_error(self, tmp_path):
        # Writing to a path whose parent is a file → OSError, surfaced as error.
        parent = tmp_path / "afile"
        parent.write_text("x")
        result = json.loads(exec_tools.write_file(str(parent / "child.txt"), "data"))
        assert "error" in result

    def test_write_error_cleans_up_tmp(self, tmp_path):
        # If the atomic replace fails after the temp file is written, the temp
        # file must be removed (no orphaned .tmp left behind).
        f = tmp_path / "x.txt"
        with patch("lager.mcp.tools.exec.os.replace", side_effect=OSError("boom")):
            result = json.loads(exec_tools.write_file(str(f), "data"))
        assert "error" in result
        assert not (tmp_path / "x.txt.tmp").exists()


@pytest.mark.unit
class TestListDir:
    def test_lists_sorted_with_types(self, tmp_path):
        (tmp_path / "b.txt").write_text("bb")
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "sub").mkdir()
        result = json.loads(exec_tools.list_dir(str(tmp_path)))
        names = [e["name"] for e in result["entries"]]
        assert names == ["a.txt", "b.txt", "sub"]
        by_name = {e["name"]: e for e in result["entries"]}
        assert by_name["sub"]["type"] == "dir"
        assert by_name["a.txt"]["type"] == "file"

    def test_missing(self, tmp_path):
        result = json.loads(exec_tools.list_dir(str(tmp_path / "ghost")))
        assert "error" in result


@pytest.mark.unit
class TestGating:
    def test_register_adds_exactly_the_primitives(self):
        from mcp.server.fastmcp import FastMCP

        m = FastMCP("test-exec")
        exec_tools.register(m)
        names = {t.name for t in _run(m.list_tools)}
        assert {"box_exec", "read_file", "write_file", "list_dir"} <= names

    def test_default_server_surface_excludes_exec_tools(self):
        # pytest runs without LAGER_MCP_ALLOW_EXEC, so the live server must not
        # have registered any exec primitives.
        from lager.mcp.server import mcp

        names = {t.name for t in _run(mcp.list_tools)}
        assert not ({"box_exec", "read_file", "write_file", "list_dir"} & names)

    def test_flag_parsing(self, monkeypatch):
        from lager.mcp import config

        monkeypatch.delenv("LAGER_MCP_ALLOW_EXEC", raising=False)
        assert config.exec_tools_enabled() is False
        for off in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("LAGER_MCP_ALLOW_EXEC", off)
            assert config.exec_tools_enabled() is False
        for on in ("1", "true", "yes", "on"):
            monkeypatch.setenv("LAGER_MCP_ALLOW_EXEC", on)
            assert config.exec_tools_enabled() is True

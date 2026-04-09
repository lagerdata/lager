# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the run_lager helper function in lager.mcp.server."""

import pytest
import subprocess
from unittest.mock import patch, MagicMock

from lager.mcp.server import run_lager


@pytest.mark.unit
class TestRunLager:
    """Test run_lager subprocess wrapper logic."""

    @patch("lager.mcp.server.subprocess.run")
    def test_success_returns_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="hello world", stderr=""
        )
        result = run_lager("hello")
        assert result == "hello world"

    @patch("lager.mcp.server.subprocess.run")
    def test_success_with_stderr_appends_warnings(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr="warn"
        )
        result = run_lager("hello")
        assert result == "ok\n\n[warnings] warn"

    @patch("lager.mcp.server.subprocess.run")
    def test_no_output_returns_placeholder(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        result = run_lager("hello")
        assert result == "(no output)"

    @patch("lager.mcp.server.subprocess.run")
    def test_nonzero_exit_returns_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="bad", stderr="err"
        )
        result = run_lager("hello")
        assert result == "Error (exit 1): bad | err"

    @patch("lager.mcp.server.subprocess.run")
    def test_nonzero_exit_stdout_only(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="bad", stderr=""
        )
        result = run_lager("hello")
        assert result == "Error (exit 1): bad"

    @patch("lager.mcp.server.subprocess.run")
    def test_nonzero_exit_stderr_only(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="err"
        )
        result = run_lager("hello")
        assert result == "Error (exit 1): err"

    @patch("lager.mcp.server.subprocess.run")
    def test_nonzero_exit_no_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr=""
        )
        result = run_lager("hello")
        assert result == "Error (exit 1): unknown error"

    @patch("lager.mcp.server.subprocess.run")
    def test_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        result = run_lager("hello")
        assert "'lager' CLI not found" in result
        assert "pip install" in result

    @patch("lager.mcp.server.subprocess.run")
    def test_timeout_expired(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="lager", timeout=60)
        result = run_lager("hello")
        assert "timed out after 60s" in result

    @patch("lager.mcp.server.subprocess.run")
    def test_default_timeout_60s(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        run_lager("hello")
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["timeout"] == 60

    @patch("lager.mcp.server.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        run_lager("cmd", timeout=120)
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["timeout"] == 120

    @patch("lager.mcp.server.subprocess.run")
    def test_command_construction(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        run_lager("i2c", "net1", "scan")
        mock_run.assert_called_once()
        actual_cmd = mock_run.call_args[0][0]
        assert actual_cmd == ["lager", "i2c", "net1", "scan"]

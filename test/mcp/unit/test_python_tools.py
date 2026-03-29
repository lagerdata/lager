# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP Python run tools (lager.mcp.tools.python_run)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestPythonTools:
    """Verify each Python tool builds the correct lager CLI command."""

    # -- python run ----------------------------------------------------------

    def test_run_default_timeout(self, mock_subprocess):
        from lager.mcp.tools.python_run import lager_python_run
        lager_python_run(box="X", script_path="test.py")
        # Default timeout=60: no --timeout flag in CLI args
        assert_lager_called_with(
            mock_subprocess, "python", "test.py", "--box", "X",
        )
        # subprocess timeout = max(60+10, 120) = 120
        assert mock_subprocess.call_args.kwargs["timeout"] == 120

    def test_run_custom_timeout(self, mock_subprocess):
        from lager.mcp.tools.python_run import lager_python_run
        lager_python_run(box="X", script_path="test.py", timeout=200)
        # Custom timeout=200: --timeout 200 added to CLI args
        assert_lager_called_with(
            mock_subprocess, "python", "test.py", "--box", "X",
            "--timeout", "200",
        )
        # subprocess timeout = max(200+10, 120) = 210
        assert mock_subprocess.call_args.kwargs["timeout"] == 210

    def test_run_with_detach(self, mock_subprocess):
        from lager.mcp.tools.python_run import lager_python_run
        lager_python_run(box="X", script_path="test.py", detach=True)
        assert_lager_called_with(
            mock_subprocess, "python", "test.py", "--box", "X", "--detach",
        )

    def test_run_custom_timeout_and_detach(self, mock_subprocess):
        from lager.mcp.tools.python_run import lager_python_run
        lager_python_run(box="X", script_path="test.py", timeout=300, detach=True)
        assert_lager_called_with(
            mock_subprocess, "python", "test.py", "--box", "X",
            "--timeout", "300", "--detach",
        )
        assert mock_subprocess.call_args.kwargs["timeout"] == 310

    # -- python kill ---------------------------------------------------------

    def test_kill_default_signal(self, mock_subprocess):
        from lager.mcp.tools.python_run import lager_python_kill
        lager_python_kill(box="X")
        # Default signal=SIGTERM: no --signal flag
        assert_lager_called_with(
            mock_subprocess, "python", "--kill", "--box", "X",
        )

    def test_kill_custom_signal(self, mock_subprocess):
        from lager.mcp.tools.python_run import lager_python_kill
        lager_python_kill(box="X", signal="SIGKILL")
        assert_lager_called_with(
            mock_subprocess, "python", "--kill", "--box", "X",
            "--signal", "SIGKILL",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_run_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.python_run import lager_python_run
        result = lager_python_run(box="B", script_path="test.py")
        assert "Error" in result

    def test_kill_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.python_run import lager_python_kill
        result = lager_python_kill(box="B")
        assert "Error" in result

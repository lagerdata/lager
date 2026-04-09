# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP logs tools (lager.mcp.tools.logs)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestLogsTools:
    """Verify each logs tool builds the correct lager CLI command."""

    # -- clean ---------------------------------------------------------------

    def test_clean_default_older_than(self, mock_subprocess):
        from lager.mcp.tools.logs import lager_logs_clean
        lager_logs_clean(box="X")
        assert_lager_called_with(
            mock_subprocess,
            "logs", "clean", "--older-than", "1d", "--yes", "--box", "X",
        )

    def test_clean_custom_older_than(self, mock_subprocess):
        from lager.mcp.tools.logs import lager_logs_clean
        lager_logs_clean(box="X", older_than="7d")
        assert_lager_called_with(
            mock_subprocess,
            "logs", "clean", "--older-than", "7d", "--yes", "--box", "X",
        )

    # -- size ----------------------------------------------------------------

    def test_size_with_box(self, mock_subprocess):
        from lager.mcp.tools.logs import lager_logs_size
        lager_logs_size(box="X")
        assert_lager_called_with(
            mock_subprocess, "logs", "size", "--box", "X",
        )

    def test_size_without_box(self, mock_subprocess):
        from lager.mcp.tools.logs import lager_logs_size
        lager_logs_size()
        assert_lager_called_with(mock_subprocess, "logs", "size")

    # -- docker --------------------------------------------------------------

    def test_docker_no_container(self, mock_subprocess):
        from lager.mcp.tools.logs import lager_logs_docker
        lager_logs_docker(box="X")
        assert_lager_called_with(
            mock_subprocess, "logs", "docker", "--box", "X",
        )

    def test_docker_with_container(self, mock_subprocess):
        from lager.mcp.tools.logs import lager_logs_docker
        lager_logs_docker(box="X", container="lager-worker")
        assert_lager_called_with(
            mock_subprocess,
            "logs", "docker", "--box", "X", "--container", "lager-worker",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_clean_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.logs import lager_logs_clean
        result = lager_logs_clean(box="B")
        assert "Error" in result

    def test_size_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.logs import lager_logs_size
        result = lager_logs_size(box="B")
        assert "Error" in result

    def test_docker_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.logs import lager_logs_docker
        result = lager_logs_docker(box="B")
        assert "Error" in result

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP binaries tools (lager.mcp.tools.binaries)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestBinariesTools:
    """Verify each binaries tool builds the correct lager CLI command."""

    # -- list ----------------------------------------------------------------

    def test_list(self, mock_subprocess):
        from lager.mcp.tools.binaries import lager_binaries_list
        lager_binaries_list(box="X")
        assert_lager_called_with(
            mock_subprocess, "binaries", "list", "--box", "X",
        )

    # -- add -----------------------------------------------------------------

    def test_add_without_name(self, mock_subprocess):
        from lager.mcp.tools.binaries import lager_binaries_add
        lager_binaries_add(box="X", file_path="/tmp/firmware.bin")
        assert_lager_called_with(
            mock_subprocess,
            "binaries", "add", "/tmp/firmware.bin", "--yes", "--box", "X",
        )

    def test_add_with_name(self, mock_subprocess):
        from lager.mcp.tools.binaries import lager_binaries_add
        lager_binaries_add(box="X", file_path="/tmp/firmware.bin", name="v2.0")
        assert_lager_called_with(
            mock_subprocess,
            "binaries", "add", "/tmp/firmware.bin", "--yes", "--box", "X",
            "--name", "v2.0",
        )

    # -- remove --------------------------------------------------------------

    def test_remove(self, mock_subprocess):
        from lager.mcp.tools.binaries import lager_binaries_remove
        lager_binaries_remove(box="X", name="old-firmware")
        assert_lager_called_with(
            mock_subprocess,
            "binaries", "remove", "old-firmware", "--yes", "--box", "X",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_list_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.binaries import lager_binaries_list
        result = lager_binaries_list(box="B")
        assert "Error" in result

    def test_add_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.binaries import lager_binaries_add
        result = lager_binaries_add(box="B", file_path="/tmp/f.bin")
        assert "Error" in result

    def test_remove_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.binaries import lager_binaries_remove
        result = lager_binaries_remove(box="B", name="old")
        assert "Error" in result

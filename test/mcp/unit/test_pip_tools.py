# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP pip tools (lager.mcp.tools.pip_tools).

After consolidation, the pip tools delegate to `lager box config pip ...`
plus an `apply` for install/uninstall. These tests verify the exact argv
each tool produces.
"""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestPipTools:
    """Verify each pip tool builds the correct lager CLI command."""

    # -- list ----------------------------------------------------------------

    def test_list(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_list
        lager_pip_list(box="X")
        assert_lager_called_with(
            mock_subprocess, "box", "config", "pip", "list", "--box", "X",
        )

    # -- install (add + apply) -----------------------------------------------

    def test_install_single_package(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_install
        lager_pip_install(box="X", packages="numpy")
        # Two CLI calls: pip add, then apply.
        assert mock_subprocess.call_count == 2
        first_argv = mock_subprocess.call_args_list[0][0][0]
        second_argv = mock_subprocess.call_args_list[1][0][0]
        assert first_argv[1:] == ["box", "config", "pip", "add", "numpy", "--box", "X"]
        assert second_argv[1:] == ["box", "config", "apply", "--yes", "--box", "X"]

    def test_install_multiple_packages(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_install
        lager_pip_install(box="X", packages="numpy pandas scipy")
        first_argv = mock_subprocess.call_args_list[0][0][0]
        assert first_argv[1:] == [
            "box", "config", "pip", "add", "numpy", "pandas", "scipy", "--box", "X",
        ]

    # -- uninstall (remove + apply) ------------------------------------------

    def test_uninstall(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_uninstall
        lager_pip_uninstall(box="X", packages="numpy pandas")
        assert mock_subprocess.call_count == 2
        first_argv = mock_subprocess.call_args_list[0][0][0]
        second_argv = mock_subprocess.call_args_list[1][0][0]
        assert first_argv[1:] == [
            "box", "config", "pip", "remove", "numpy", "pandas", "--box", "X",
        ]
        assert second_argv[1:] == ["box", "config", "apply", "--yes", "--box", "X"]

    # -- apply ---------------------------------------------------------------

    def test_apply(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_apply
        lager_pip_apply(box="X")
        assert_lager_called_with(
            mock_subprocess, "box", "config", "apply", "--yes", "--box", "X",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_list_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.pip_tools import lager_pip_list
        result = lager_pip_list(box="B")
        assert "Error" in result

    def test_install_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.pip_tools import lager_pip_install
        result = lager_pip_install(box="B", packages="numpy")
        assert "Error" in result

    def test_uninstall_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.pip_tools import lager_pip_uninstall
        result = lager_pip_uninstall(box="B", packages="numpy")
        assert "Error" in result

    def test_apply_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
        from lager.mcp.tools.pip_tools import lager_pip_apply
        result = lager_pip_apply(box="B")
        assert "Error" in result

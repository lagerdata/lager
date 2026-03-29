# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP pip tools (lager.mcp.tools.pip_tools)."""

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
            mock_subprocess, "pip", "list", "--box", "X",
        )

    # -- install -------------------------------------------------------------

    def test_install_single_package(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_install
        lager_pip_install(box="X", packages="numpy")
        assert_lager_called_with(
            mock_subprocess, "pip", "install", "numpy", "--yes", "--box", "X",
        )

    def test_install_multiple_packages(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_install
        lager_pip_install(box="X", packages="numpy pandas scipy")
        assert_lager_called_with(
            mock_subprocess,
            "pip", "install", "numpy", "pandas", "scipy", "--yes", "--box", "X",
        )

    # -- uninstall -----------------------------------------------------------

    def test_uninstall(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_uninstall
        lager_pip_uninstall(box="X", packages="numpy pandas")
        assert_lager_called_with(
            mock_subprocess,
            "pip", "uninstall", "numpy", "pandas", "--yes", "--box", "X",
        )

    # -- apply ---------------------------------------------------------------

    def test_apply(self, mock_subprocess):
        from lager.mcp.tools.pip_tools import lager_pip_apply
        lager_pip_apply(box="X")
        assert_lager_called_with(
            mock_subprocess, "pip", "apply", "--yes", "--box", "X",
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

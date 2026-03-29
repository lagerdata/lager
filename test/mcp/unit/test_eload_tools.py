# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP electronic load tools (lager.mcp.tools.eload)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
@pytest.mark.eload
class TestEloadTools:
    """Verify each electronic load tool builds the correct lager CLI command."""

    # -- cc (constant current) -------------------------------------------

    def test_cc_read(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cc
        lager_eload_cc(box="E", net="eload1")
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cc", "--box", "E",
        )

    def test_cc_set(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cc
        lager_eload_cc(box="E", net="eload1", value=1.5)
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cc", "1.5", "--box", "E",
        )

    # -- cv (constant voltage) -------------------------------------------

    def test_cv_read(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cv
        lager_eload_cv(box="E", net="eload1")
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cv", "--box", "E",
        )

    def test_cv_set(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cv
        lager_eload_cv(box="E", net="eload1", value=12.0)
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cv", "12.0", "--box", "E",
        )

    # -- cr (constant resistance) ----------------------------------------

    def test_cr_read(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cr
        lager_eload_cr(box="E", net="eload1")
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cr", "--box", "E",
        )

    def test_cr_set(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cr
        lager_eload_cr(box="E", net="eload1", value=100.0)
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cr", "100.0", "--box", "E",
        )

    # -- cp (constant power) ---------------------------------------------

    def test_cp_read(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cp
        lager_eload_cp(box="E", net="eload1")
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cp", "--box", "E",
        )

    def test_cp_set(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_cp
        lager_eload_cp(box="E", net="eload1", value=25.0)
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "cp", "25.0", "--box", "E",
        )

    # -- state -----------------------------------------------------------

    def test_state(self, mock_subprocess):
        from lager.mcp.tools.eload import lager_eload_state
        lager_eload_state(box="E", net="eload1")
        assert_lager_called_with(
            mock_subprocess, "eload", "eload1", "state", "--box", "E",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_cc_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.eload import lager_eload_cc
        result = lager_eload_cc(box="B", net="eload1")
        assert "Error" in result

    def test_cv_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.eload import lager_eload_cv
        result = lager_eload_cv(box="B", net="eload1")
        assert "Error" in result

    def test_cr_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.eload import lager_eload_cr
        result = lager_eload_cr(box="B", net="eload1")
        assert "Error" in result

    def test_cp_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.eload import lager_eload_cp
        result = lager_eload_cp(box="B", net="eload1")
        assert "Error" in result

    def test_state_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.eload import lager_eload_state
        result = lager_eload_state(box="B", net="eload1")
        assert "Error" in result

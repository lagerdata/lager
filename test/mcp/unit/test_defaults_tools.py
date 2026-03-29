# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP defaults tools (lager.mcp.tools.defaults)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
@pytest.mark.defaults
class TestDefaultsTools:
    """Verify each defaults tool builds the correct lager CLI command."""

    # -- show ----------------------------------------------------------------

    def test_show(self, mock_subprocess):
        from lager.mcp.tools.defaults import lager_defaults_show
        lager_defaults_show()
        assert_lager_called_with(mock_subprocess, "defaults")

    # -- set -----------------------------------------------------------------

    def test_set_box_only(self, mock_subprocess):
        from lager.mcp.tools.defaults import lager_defaults_set
        lager_defaults_set(box="DEMO")
        assert_lager_called_with(
            mock_subprocess, "defaults", "add", "--box", "DEMO",
        )

    def test_set_multiple_params(self, mock_subprocess):
        from lager.mcp.tools.defaults import lager_defaults_set
        lager_defaults_set(box="DEMO", supply_net="psu1", uart_net="uart0")
        # Verify the command was called once with defaults add + all three flags
        mock_subprocess.assert_called_once()
        actual_cmd = mock_subprocess.call_args[0][0]
        assert actual_cmd[0:3] == ["lager", "defaults", "add"]
        # All three flag pairs must be present (order depends on dict iteration)
        assert "--box" in actual_cmd
        assert actual_cmd[actual_cmd.index("--box") + 1] == "DEMO"
        assert "--supply-net" in actual_cmd
        assert actual_cmd[actual_cmd.index("--supply-net") + 1] == "psu1"
        assert "--uart-net" in actual_cmd
        assert actual_cmd[actual_cmd.index("--uart-net") + 1] == "uart0"

    def test_set_no_args_returns_error(self, mock_subprocess):
        from lager.mcp.tools.defaults import lager_defaults_set
        result = lager_defaults_set()
        assert "Error" in result
        mock_subprocess.assert_not_called()

    # -- delete --------------------------------------------------------------

    def test_delete(self, mock_subprocess):
        from lager.mcp.tools.defaults import lager_defaults_delete
        lager_defaults_delete(setting="box")
        assert_lager_called_with(
            mock_subprocess, "defaults", "delete", "box", "--yes",
        )

    # -- delete-all ----------------------------------------------------------

    def test_delete_all(self, mock_subprocess):
        from lager.mcp.tools.defaults import lager_defaults_delete_all
        lager_defaults_delete_all()
        assert_lager_called_with(
            mock_subprocess, "defaults", "delete-all", "--yes",
        )

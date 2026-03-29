# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP debug tools (lager.mcp.tools.debug)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestDebugTools:
    """Verify each debug tool builds the correct lager CLI command."""

    # -- list nets -------------------------------------------------------

    def test_list_nets(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_list_nets
        lager_debug_list_nets(box="X")
        assert_lager_called_with(mock_subprocess, "debug", "--box", "X")

    # -- flash -----------------------------------------------------------

    def test_flash_hex(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_flash
        lager_debug_flash(box="X", net="debug1", hex_file="fw.hex")
        assert_lager_called_with(
            mock_subprocess,
            "debug", "debug1", "flash", "--box", "X", "--hex", "fw.hex",
        )

    def test_flash_elf_with_erase(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_flash
        lager_debug_flash(box="X", net="debug1", elf_file="fw.elf", erase=True)
        assert_lager_called_with(
            mock_subprocess,
            "debug", "debug1", "flash", "--box", "X",
            "--elf", "fw.elf", "--erase",
        )

    def test_flash_no_file(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_flash
        lager_debug_flash(box="X", net="debug1")
        assert_lager_called_with(
            mock_subprocess, "debug", "debug1", "flash", "--box", "X",
        )

    # -- reset -----------------------------------------------------------

    def test_reset(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_reset
        lager_debug_reset(box="X", net="debug1")
        assert_lager_called_with(
            mock_subprocess, "debug", "debug1", "reset", "--box", "X",
        )

    # -- erase -----------------------------------------------------------

    def test_erase(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_erase
        lager_debug_erase(box="X", net="debug1")
        assert_lager_called_with(
            mock_subprocess,
            "debug", "debug1", "erase", "--yes", "--box", "X",
        )

    # -- status ----------------------------------------------------------

    def test_status(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_status
        lager_debug_status(box="X", net="debug1")
        assert_lager_called_with(
            mock_subprocess, "debug", "debug1", "status", "--box", "X",
        )

    # -- memrd -----------------------------------------------------------

    def test_memrd(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_memrd
        lager_debug_memrd(
            box="X", net="debug1",
            start_addr="0x08000000", length="256",
        )
        assert_lager_called_with(
            mock_subprocess,
            "debug", "debug1", "memrd", "0x08000000", "256", "--box", "X",
        )

    # -- health ----------------------------------------------------------

    def test_health(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_health
        lager_debug_health(box="X", net="debug1")
        assert_lager_called_with(
            mock_subprocess, "debug", "debug1", "health", "--box", "X",
        )

    # -- disconnect ------------------------------------------------------

    def test_disconnect(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_disconnect
        lager_debug_disconnect(box="X", net="debug1")
        assert_lager_called_with(
            mock_subprocess, "debug", "debug1", "disconnect", "--box", "X",
        )

    # -- gdbserver -------------------------------------------------------

    def test_gdbserver_defaults(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_gdbserver
        lager_debug_gdbserver(box="X", net="debug1")
        assert_lager_called_with(
            mock_subprocess,
            "debug", "debug1", "gdbserver", "--box", "X",
            "--gdb-port", "2331",
        )

    def test_gdbserver_all_options(self, mock_subprocess):
        from lager.mcp.tools.debug import lager_debug_gdbserver
        lager_debug_gdbserver(
            box="X", net="debug1",
            speed="4000", force=True, halt=True, reset=True, gdb_port=3333,
        )
        assert_lager_called_with(
            mock_subprocess,
            "debug", "debug1", "gdbserver", "--box", "X",
            "--gdb-port", "3333",
            "--speed", "4000", "--force", "--halt", "--reset",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_debug_flash_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.debug import lager_debug_flash
        result = lager_debug_flash(box="B", net="dbg1", hex_file="/tmp/test.hex")
        assert "Error" in result

    def test_debug_reset_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.debug import lager_debug_reset
        result = lager_debug_reset(box="B", net="dbg1")
        assert "Error" in result

    def test_debug_erase_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.debug import lager_debug_erase
        result = lager_debug_erase(box="B", net="dbg1")
        assert "Error" in result

    def test_debug_gdbserver_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.debug import lager_debug_gdbserver
        result = lager_debug_gdbserver(box="B", net="dbg1")
        assert "Error" in result

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP debug tools (lager.mcp.tools.debug).

All hardware interaction is mocked — no real debug probes are used.
"""

import json
from unittest.mock import patch, MagicMock

import pytest


@pytest.mark.unit
class TestDebugTools:
    """Verify each on-box debug tool calls the correct Net API."""

    @patch("lager.Net.get")
    def test_flash(self, mock_get):
        dbg = MagicMock()
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_flash
        result = json.loads(debug_flash(net="debug1", firmware_path="/tmp/fw.hex"))
        dbg.flash.assert_called_once_with("/tmp/fw.hex")
        assert result["status"] == "ok"

    @patch("lager.Net.get")
    def test_reset(self, mock_get):
        dbg = MagicMock()
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_reset
        result = json.loads(debug_reset(net="debug1", halt=True))
        dbg.reset.assert_called_once_with(halt=True)
        assert result["halt"] is True

    @patch("lager.Net.get")
    def test_erase(self, mock_get):
        dbg = MagicMock()
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_erase
        result = json.loads(debug_erase(net="debug1"))
        dbg.erase.assert_called_once()
        assert result["erased"] is True

    @patch("lager.Net.get")
    def test_connect(self, mock_get):
        dbg = MagicMock()
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_connect
        result = json.loads(debug_connect(net="debug1", speed=4000))
        dbg.connect.assert_called_once_with(speed=4000)
        assert result["connected"] is True

    @patch("lager.Net.get")
    def test_connect_auto_speed(self, mock_get):
        dbg = MagicMock()
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_connect
        result = json.loads(debug_connect(net="debug1"))
        dbg.connect.assert_called_once_with()

    @patch("lager.Net.get")
    def test_disconnect(self, mock_get):
        dbg = MagicMock()
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_disconnect
        result = json.loads(debug_disconnect(net="debug1"))
        dbg.disconnect.assert_called_once()
        assert result["connected"] is False

    @patch("lager.Net.get")
    def test_read_memory(self, mock_get):
        dbg = MagicMock()
        dbg.read_memory.return_value = [0xDE, 0xAD]
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_read_memory
        result = json.loads(debug_read_memory(net="debug1", address=0x08000000, length=2))
        dbg.read_memory.assert_called_once_with(0x08000000, 2)
        assert result["data"] == [0xDE, 0xAD]

    @patch("lager.Net.get")
    def test_gdbserver(self, mock_get):
        dbg = MagicMock()
        mock_get.return_value = dbg
        from lager.mcp.tools.debug import debug_gdbserver
        result = json.loads(debug_gdbserver(net="debug1", port=3333))
        dbg.gdbserver.assert_called_once_with(port=3333)
        assert result["gdbserver_port"] == 3333


@pytest.mark.unit
class TestRTTTools:
    """Verify RTT tools create and use cached sessions correctly."""

    @patch("lager.Net.get")
    def test_rtt_write(self, mock_get):
        rtt_session = MagicMock()
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__ = MagicMock(return_value=rtt_session)
        ctx_mgr.__exit__ = MagicMock(return_value=False)
        dbg = MagicMock()
        dbg.rtt.return_value = ctx_mgr
        mock_get.return_value = dbg

        from lager.mcp.tools.debug import rtt_write, _rtt_sessions
        _rtt_sessions.clear()

        result = json.loads(rtt_write(net="debug1", data="hello\n", channel=0))
        rtt_session.write.assert_called_once_with(b"hello\n")
        assert result["bytes_written"] == 6

    @patch("lager.Net.get")
    def test_rtt_read(self, mock_get):
        rtt_session = MagicMock()
        rtt_session.read_some.side_effect = [b"data", None]
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__ = MagicMock(return_value=rtt_session)
        ctx_mgr.__exit__ = MagicMock(return_value=False)
        dbg = MagicMock()
        dbg.rtt.return_value = ctx_mgr
        mock_get.return_value = dbg

        from lager.mcp.tools.debug import rtt_read, _rtt_sessions
        _rtt_sessions.clear()

        result = json.loads(rtt_read(net="debug1", channel=0, timeout_ms=500))
        assert result["output"] == "data"

    @patch("lager.Net.get")
    def test_rtt_expect_match(self, mock_get):
        rtt_session = MagicMock()
        rtt_session.read_some.return_value = b"sensor: 42\n"
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__ = MagicMock(return_value=rtt_session)
        ctx_mgr.__exit__ = MagicMock(return_value=False)
        dbg = MagicMock()
        dbg.rtt.return_value = ctx_mgr
        mock_get.return_value = dbg

        from lager.mcp.tools.debug import rtt_expect, _rtt_sessions
        _rtt_sessions.clear()

        result = json.loads(rtt_expect(net="debug1", pattern="sensor: 42", timeout_ms=1000))
        assert result["matched"] is True

    @patch("lager.Net.get")
    def test_rtt_expect_no_match(self, mock_get):
        rtt_session = MagicMock()
        rtt_session.read_some.return_value = None
        ctx_mgr = MagicMock()
        ctx_mgr.__enter__ = MagicMock(return_value=rtt_session)
        ctx_mgr.__exit__ = MagicMock(return_value=False)
        dbg = MagicMock()
        dbg.rtt.return_value = ctx_mgr
        mock_get.return_value = dbg

        from lager.mcp.tools.debug import rtt_expect, _rtt_sessions
        _rtt_sessions.clear()

        result = json.loads(rtt_expect(net="debug1", pattern="never", timeout_ms=200))
        assert result["matched"] is False

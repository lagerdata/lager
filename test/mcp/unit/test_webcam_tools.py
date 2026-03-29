# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP webcam tools (lager.mcp.tools.webcam)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestWebcamTools:
    """Verify each webcam tool builds the correct lager CLI command."""

    # -- start ---------------------------------------------------------------

    def test_start_with_net(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_start
        lager_webcam_start(box="X", net="cam1")
        assert_lager_called_with(
            mock_subprocess, "webcam", "cam1", "start", "--box", "X",
        )

    def test_start_without_net(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_start
        lager_webcam_start(box="X")
        assert_lager_called_with(
            mock_subprocess, "webcam", "start", "--box", "X",
        )

    # -- stop ----------------------------------------------------------------

    def test_stop_with_net(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_stop
        lager_webcam_stop(box="X", net="cam1")
        assert_lager_called_with(
            mock_subprocess, "webcam", "cam1", "stop", "--box", "X",
        )

    def test_stop_without_net(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_stop
        lager_webcam_stop(box="X")
        assert_lager_called_with(
            mock_subprocess, "webcam", "stop", "--box", "X",
        )

    # -- url -----------------------------------------------------------------

    def test_url_with_net(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_url
        lager_webcam_url(box="X", net="cam1")
        assert_lager_called_with(
            mock_subprocess, "webcam", "cam1", "url", "--box", "X",
        )

    def test_url_without_net(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_url
        lager_webcam_url(box="X")
        assert_lager_called_with(
            mock_subprocess, "webcam", "url", "--box", "X",
        )

    # -- start-all / stop-all ------------------------------------------------

    def test_start_all(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_start_all
        lager_webcam_start_all(box="X")
        assert_lager_called_with(
            mock_subprocess, "webcam", "start-all", "--box", "X",
        )

    def test_stop_all(self, mock_subprocess):
        from lager.mcp.tools.webcam import lager_webcam_stop_all
        lager_webcam_stop_all(box="X")
        assert_lager_called_with(
            mock_subprocess, "webcam", "stop-all", "--box", "X",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_webcam_start_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.webcam import lager_webcam_start
        result = lager_webcam_start(box="B", net="webcam1")
        assert "Error" in result

    def test_webcam_stop_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.webcam import lager_webcam_stop
        result = lager_webcam_stop(box="B", net="webcam1")
        assert "Error" in result

    def test_webcam_url_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.webcam import lager_webcam_url
        result = lager_webcam_url(box="B", net="webcam1")
        assert "Error" in result

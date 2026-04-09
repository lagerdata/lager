# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP webcam tools (lager.mcp.tools.webcam)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from lager import NetType


@pytest.mark.unit
class TestWebcamTools:
    """Verify each webcam tool calls the correct Net API."""

    # -- start ---------------------------------------------------------------

    @patch("lager.mcp.tools.webcam._box_ip", return_value="10.0.0.5")
    @patch("lager.Net.get")
    def test_start(self, mock_get, _mock_box_ip):
        cam = MagicMock()
        cam.start.return_value = {"url": "http://10.0.0.5:8080/stream", "port": 8080}
        mock_get.return_value = cam
        from lager.mcp.tools.webcam import webcam_start

        result = json.loads(webcam_start(net="cam1"))
        mock_get.assert_called_once_with("cam1", type=NetType.Webcam)
        cam.start.assert_called_once_with(box_ip="10.0.0.5")
        assert result["status"] == "ok"
        assert result["net"] == "cam1"
        assert result["url"] == "http://10.0.0.5:8080/stream"
        assert result["port"] == 8080

    # -- stop ----------------------------------------------------------------

    @patch("lager.Net.get")
    def test_stop(self, mock_get):
        cam = MagicMock()
        cam.stop.return_value = True
        mock_get.return_value = cam
        from lager.mcp.tools.webcam import webcam_stop

        result = json.loads(webcam_stop(net="cam1"))
        mock_get.assert_called_once_with("cam1", type=NetType.Webcam)
        cam.stop.assert_called_once_with()
        assert result["stopped"] is True

    # -- url -----------------------------------------------------------------

    @patch("lager.mcp.tools.webcam._box_ip", return_value="10.0.0.5")
    @patch("lager.Net.get")
    def test_url(self, mock_get, _mock_box_ip):
        cam = MagicMock()
        cam.get_url.return_value = "http://10.0.0.5:8080/stream"
        mock_get.return_value = cam
        from lager.mcp.tools.webcam import webcam_url

        result = json.loads(webcam_url(net="cam1"))
        mock_get.assert_called_once_with("cam1", type=NetType.Webcam)
        cam.get_url.assert_called_once_with(box_ip="10.0.0.5")
        assert result["url"] == "http://10.0.0.5:8080/stream"

    # -- info ----------------------------------------------------------------

    @patch("lager.mcp.tools.webcam._box_ip", return_value="10.0.0.5")
    @patch("lager.Net.get")
    def test_info_with_payload(self, mock_get, _mock_box_ip):
        cam = MagicMock()
        cam.is_active.return_value = True
        cam.get_info.return_value = {"codec": "mjpeg"}
        mock_get.return_value = cam
        from lager.mcp.tools.webcam import webcam_info

        result = json.loads(webcam_info(net="cam1"))
        mock_get.assert_called_once_with("cam1", type=NetType.Webcam)
        cam.get_info.assert_called_once_with(box_ip="10.0.0.5")
        assert result["active"] is True
        assert result["info"] == {"codec": "mjpeg"}

    @patch("lager.mcp.tools.webcam._box_ip", return_value="10.0.0.5")
    @patch("lager.Net.get")
    def test_info_empty(self, mock_get, _mock_box_ip):
        cam = MagicMock()
        cam.is_active.return_value = False
        cam.get_info.return_value = None
        mock_get.return_value = cam
        from lager.mcp.tools.webcam import webcam_info

        result = json.loads(webcam_info(net="cam1"))
        assert result["active"] is False
        assert "info" not in result

    # -- Net.get / device errors ---------------------------------------------

    @patch("lager.Net.get")
    def test_webcam_start_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.webcam import webcam_start

        with pytest.raises(RuntimeError, match="device not found"):
            webcam_start(net="webcam1")

    @patch("lager.Net.get")
    def test_webcam_stop_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.webcam import webcam_stop

        with pytest.raises(RuntimeError, match="device not found"):
            webcam_stop(net="webcam1")

    @patch("lager.Net.get")
    def test_webcam_url_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.webcam import webcam_url

        with pytest.raises(RuntimeError, match="device not found"):
            webcam_url(net="webcam1")

    @patch("lager.Net.get")
    def test_webcam_info_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.webcam import webcam_info

        with pytest.raises(RuntimeError, match="device not found"):
            webcam_info(net="webcam1")

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP robotic arm tools (lager.mcp.tools.arm)."""

import json
from unittest.mock import MagicMock, patch

import pytest
from lager import NetType


@pytest.mark.unit
class TestArmTools:
    """Verify each arm tool calls the correct Net API."""

    # -- position ------------------------------------------------------------

    @patch("lager.Net.get")
    def test_position(self, mock_get):
        arm = MagicMock()
        arm.position.return_value = (1.0, 2.0, 3.0)
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_position

        result = json.loads(arm_position(net="arm1"))
        mock_get.assert_called_once_with("arm1", type=NetType.Arm)
        arm.position.assert_called_once_with()
        assert result["status"] == "ok"
        assert result["net"] == "arm1"
        assert result["x"] == 1.0
        assert result["y"] == 2.0
        assert result["z"] == 3.0

    # -- move ----------------------------------------------------------------

    @patch("lager.Net.get")
    def test_move_default_timeout(self, mock_get):
        arm = MagicMock()
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_move

        result = json.loads(arm_move(net="arm1", x=10.0, y=20.0, z=30.0))
        mock_get.assert_called_once_with("arm1", type=NetType.Arm)
        arm.move_to.assert_called_once_with(10.0, 20.0, 30.0, timeout=15.0)
        assert result["x"] == 10.0
        assert result["y"] == 20.0
        assert result["z"] == 30.0

    @patch("lager.Net.get")
    def test_move_custom_timeout(self, mock_get):
        arm = MagicMock()
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_move

        json.loads(arm_move(net="arm1", x=1.0, y=2.0, z=3.0, timeout=30.0))
        arm.move_to.assert_called_once_with(1.0, 2.0, 3.0, timeout=30.0)

    # -- move_relative -------------------------------------------------------

    @patch("lager.Net.get")
    def test_move_relative_all_defaults(self, mock_get):
        arm = MagicMock()
        arm.move_relative.return_value = (0.0, 0.0, 0.0)
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_move_relative

        result = json.loads(arm_move_relative(net="arm1"))
        arm.move_relative.assert_called_once_with(0.0, 0.0, 0.0, timeout=15.0)
        assert result["x"] == 0.0
        assert result["y"] == 0.0
        assert result["z"] == 0.0

    @patch("lager.Net.get")
    def test_move_relative_dx_only(self, mock_get):
        arm = MagicMock()
        arm.move_relative.return_value = (5.0, 0.0, 0.0)
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_move_relative

        json.loads(arm_move_relative(net="arm1", dx=5.0))
        arm.move_relative.assert_called_once_with(5.0, 0.0, 0.0, timeout=15.0)

    @patch("lager.Net.get")
    def test_move_relative_all_nonzero(self, mock_get):
        arm = MagicMock()
        arm.move_relative.return_value = (11.0, 12.0, 13.0)
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_move_relative

        result = json.loads(
            arm_move_relative(net="arm1", dx=1.0, dy=2.0, dz=3.0, timeout=20.0)
        )
        arm.move_relative.assert_called_once_with(1.0, 2.0, 3.0, timeout=20.0)
        assert result["x"] == 11.0
        assert result["y"] == 12.0
        assert result["z"] == 13.0

    # -- go-home -------------------------------------------------------------

    @patch("lager.Net.get")
    def test_go_home(self, mock_get):
        arm = MagicMock()
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_go_home

        result = json.loads(arm_go_home(net="arm1"))
        arm.go_home.assert_called_once_with()
        assert result["action"] == "go_home"

    # -- enable / disable motor ----------------------------------------------

    @patch("lager.Net.get")
    def test_enable_motor(self, mock_get):
        arm = MagicMock()
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_enable_motor

        result = json.loads(arm_enable_motor(net="arm1"))
        arm.enable_motor.assert_called_once_with()
        assert result["motor"] == "enabled"

    @patch("lager.Net.get")
    def test_disable_motor(self, mock_get):
        arm = MagicMock()
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_disable_motor

        result = json.loads(arm_disable_motor(net="arm1"))
        arm.disable_motor.assert_called_once_with()
        assert result["motor"] == "disabled"

    # -- save_position -------------------------------------------------------

    @patch("lager.Net.get")
    def test_save_position(self, mock_get):
        arm = MagicMock()
        mock_get.return_value = arm
        from lager.mcp.tools.arm import arm_save_position

        result = json.loads(arm_save_position(net="arm1"))
        arm.save_position.assert_called_once_with()
        assert result["action"] == "save_position"

    # -- Net.get / device errors ---------------------------------------------

    @patch("lager.Net.get")
    def test_arm_position_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.arm import arm_position

        with pytest.raises(RuntimeError, match="device not found"):
            arm_position(net="arm1")

    @patch("lager.Net.get")
    def test_arm_move_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.arm import arm_move

        with pytest.raises(RuntimeError, match="device not found"):
            arm_move(net="arm1", x=0.0, y=0.0, z=0.0)

    @patch("lager.Net.get")
    def test_arm_go_home_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.arm import arm_go_home

        with pytest.raises(RuntimeError, match="device not found"):
            arm_go_home(net="arm1")

    @patch("lager.Net.get")
    def test_arm_enable_motor_net_get_failure(self, mock_get):
        mock_get.side_effect = RuntimeError("device not found")
        from lager.mcp.tools.arm import arm_enable_motor

        with pytest.raises(RuntimeError, match="device not found"):
            arm_enable_motor(net="arm1")

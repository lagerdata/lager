# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for MCP robotic arm tools (lager.mcp.tools.arm)."""

import pytest
from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestArmTools:
    """Verify each arm tool builds the correct lager CLI command."""

    # -- position ------------------------------------------------------------

    def test_position(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_position
        lager_arm_position(box="X", net="arm1")
        assert_lager_called_with(
            mock_subprocess, "arm", "arm1", "position", "--box", "X",
        )

    # -- move ----------------------------------------------------------------

    def test_move_default_timeout(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_move
        lager_arm_move(box="X", net="arm1", x=10.0, y=20.0, z=30.0)
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "move",
            "--x", "10.0", "--y", "20.0", "--z", "30.0",
            "--timeout", "15",
            "--yes", "--box", "X",
        )
        # Default timeout=15 -> subprocess timeout should be 15+10=25
        assert mock_subprocess.call_args.kwargs["timeout"] == 25

    def test_move_custom_timeout(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_move
        lager_arm_move(box="X", net="arm1", x=1.0, y=2.0, z=3.0, timeout=30)
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "move",
            "--x", "1.0", "--y", "2.0", "--z", "3.0",
            "--timeout", "30",
            "--yes", "--box", "X",
        )
        # Custom timeout=30 -> subprocess timeout should be 30+10=40
        assert mock_subprocess.call_args.kwargs["timeout"] == 40

    # -- move-by -------------------------------------------------------------

    def test_move_by_all_zeros(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_move_by
        lager_arm_move_by(box="X", net="arm1", dx=0, dy=0, dz=0)
        # With all zeros, no --dx/--dy/--dz flags should appear
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "move-by", "--yes", "--box", "X",
            "--timeout", "15",
        )
        assert mock_subprocess.call_args.kwargs["timeout"] == 25

    def test_move_by_dx_only(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_move_by
        lager_arm_move_by(box="X", net="arm1", dx=5.0)
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "move-by", "--yes", "--box", "X",
            "--dx", "5.0",
            "--timeout", "15",
        )
        assert mock_subprocess.call_args.kwargs["timeout"] == 25

    def test_move_by_all_nonzero(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_move_by
        lager_arm_move_by(box="X", net="arm1", dx=1.0, dy=2.0, dz=3.0, timeout=20)
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "move-by", "--yes", "--box", "X",
            "--dx", "1.0", "--dy", "2.0", "--dz", "3.0",
            "--timeout", "20",
        )
        assert mock_subprocess.call_args.kwargs["timeout"] == 30

    # -- go-home -------------------------------------------------------------

    def test_go_home(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_go_home
        lager_arm_go_home(box="X", net="arm1")
        assert_lager_called_with(
            mock_subprocess, "arm", "arm1", "go-home", "--yes", "--box", "X",
        )

    # -- enable / disable motor ----------------------------------------------

    def test_enable_motor(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_enable_motor
        lager_arm_enable_motor(box="X", net="arm1")
        assert_lager_called_with(
            mock_subprocess, "arm", "arm1", "enable-motor", "--box", "X",
        )

    def test_disable_motor(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_disable_motor
        lager_arm_disable_motor(box="X", net="arm1")
        assert_lager_called_with(
            mock_subprocess, "arm", "arm1", "disable-motor", "--box", "X",
        )

    # -- set-acceleration ----------------------------------------------------

    def test_set_acceleration_default_retract(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_set_acceleration
        lager_arm_set_acceleration(box="X", net="arm1", acceleration=100.0, travel=50.0)
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "set-acceleration",
            "--acceleration", "100.0",
            "--travel", "50.0",
            "--retract", "60",
            "--box", "X",
        )

    def test_set_acceleration_custom_retract(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_set_acceleration
        lager_arm_set_acceleration(
            box="X", net="arm1", acceleration=80.0, travel=40.0, retract=90.0,
        )
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "set-acceleration",
            "--acceleration", "80.0",
            "--travel", "40.0",
            "--retract", "90.0",
            "--box", "X",
        )

    # -- read-and-save-position ----------------------------------------------

    def test_read_and_save_position(self, mock_subprocess):
        from lager.mcp.tools.arm import lager_arm_read_and_save_position
        lager_arm_read_and_save_position(box="X", net="arm1")
        assert_lager_called_with(
            mock_subprocess,
            "arm", "arm1", "read-and-save-position", "--box", "X",
        )

    # -- subprocess failure error handling -----------------------------------

    def test_arm_position_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.arm import lager_arm_position
        result = lager_arm_position(box="B", net="arm1")
        assert "Error" in result

    def test_arm_move_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.arm import lager_arm_move
        result = lager_arm_move(box="B", net="arm1", x=0.0, y=0.0, z=0.0)
        assert "Error" in result

    def test_arm_go_home_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.arm import lager_arm_go_home
        result = lager_arm_go_home(box="B", net="arm1")
        assert "Error" in result

    def test_arm_enable_motor_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.arm import lager_arm_enable_motor
        result = lager_arm_enable_motor(box="B", net="arm1")
        assert "Error" in result

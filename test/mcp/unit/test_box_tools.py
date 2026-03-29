# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for lager.mcp.tools.box -- box and nets management MCP tools."""

import pytest

from test.mcp.conftest import assert_lager_called_with


@pytest.mark.unit
class TestBoxTools:
    """Tests for box connectivity tools."""

    def test_hello(self, mock_subprocess):
        from lager.mcp.tools.box import lager_hello
        lager_hello(box="DEMO")
        assert_lager_called_with(mock_subprocess, "hello", "--box", "DEMO")

    def test_instruments(self, mock_subprocess):
        from lager.mcp.tools.box import lager_instruments
        lager_instruments(box="DEMO")
        assert_lager_called_with(mock_subprocess, "instruments", "--box", "DEMO")

    def test_list_nets(self, mock_subprocess):
        from lager.mcp.tools.box import lager_list_nets
        lager_list_nets(box="MY-BOX")
        assert_lager_called_with(mock_subprocess, "nets", "--box", "MY-BOX")

    # -- subprocess failure error handling -----------------------------------

    def test_hello_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.box import lager_hello
        result = lager_hello(box="B")
        assert "Error" in result

    def test_list_nets_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.box import lager_list_nets
        result = lager_list_nets(box="B")
        assert "Error" in result

    def test_boxes_list_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.box import lager_boxes_list
        result = lager_boxes_list()
        assert "Error" in result

    def test_nets_add_subprocess_failure(self, mock_subprocess):
        from unittest.mock import MagicMock
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="device not found")
        from lager.mcp.tools.box import lager_nets_add
        result = lager_nets_add(box="B", name="test-net", role="gpio", channel="0", address="1")
        assert "Error" in result


@pytest.mark.unit
class TestBoxesManagement:
    """Tests for boxes list/add/delete/edit/export/import."""

    def test_boxes_list(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_list
        lager_boxes_list()
        assert_lager_called_with(mock_subprocess, "boxes")

    def test_boxes_add(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_add
        lager_boxes_add(name="NEW-BOX", ip="100.64.0.1")
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "add", "--name", "NEW-BOX", "--ip", "100.64.0.1", "--yes",
        )

    def test_boxes_delete(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_delete
        lager_boxes_delete(name="OLD-BOX")
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "delete", "--name", "OLD-BOX", "--yes",
        )

    def test_boxes_add_all(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_add_all
        lager_boxes_add_all()
        assert_lager_called_with(mock_subprocess, "boxes", "add-all", "--yes")

    def test_boxes_delete_all(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_delete_all
        lager_boxes_delete_all()
        assert_lager_called_with(mock_subprocess, "boxes", "delete-all", "--yes")

    def test_boxes_edit_ip_only(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_edit
        lager_boxes_edit(name="DEMO", ip="100.64.0.2")
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "edit", "--name", "DEMO", "--yes", "--ip", "100.64.0.2",
        )

    def test_boxes_edit_new_name_only(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_edit
        lager_boxes_edit(name="DEMO", new_name="DEMO-2")
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "edit", "--name", "DEMO", "--yes", "--new-name", "DEMO-2",
        )

    def test_boxes_edit_all_fields(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_edit
        lager_boxes_edit(
            name="DEMO", ip="192.0.2.1", new_name="PROD",
            user="admin", version="staging",
        )
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "edit", "--name", "DEMO", "--yes",
            "--ip", "192.0.2.1", "--new-name", "PROD",
            "--user", "admin", "--version", "staging",
        )

    def test_boxes_edit_no_optional_fields(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_edit
        lager_boxes_edit(name="DEMO")
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "edit", "--name", "DEMO", "--yes",
        )

    def test_boxes_export_to_file(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_export
        lager_boxes_export(output="/tmp/boxes.json")
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "export", "-o", "/tmp/boxes.json",
        )

    def test_boxes_export_to_stdout(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_export
        lager_boxes_export()
        assert_lager_called_with(mock_subprocess, "boxes", "export")

    def test_boxes_export_empty_string(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_export
        lager_boxes_export(output="")
        assert_lager_called_with(mock_subprocess, "boxes", "export")

    def test_boxes_import_basic(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_import
        lager_boxes_import(file="/tmp/boxes.json")
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "import", "/tmp/boxes.json", "--yes",
        )

    def test_boxes_import_with_merge(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_import
        lager_boxes_import(file="/tmp/boxes.json", merge=True)
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "import", "/tmp/boxes.json", "--yes", "--merge",
        )

    def test_boxes_import_merge_false(self, mock_subprocess):
        from lager.mcp.tools.box import lager_boxes_import
        lager_boxes_import(file="/tmp/boxes.json", merge=False)
        assert_lager_called_with(
            mock_subprocess,
            "boxes", "import", "/tmp/boxes.json", "--yes",
        )


@pytest.mark.unit
class TestNetsManagement:
    """Tests for nets add/delete/rename/batch/script tools."""

    def test_nets_add(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_add
        lager_nets_add(
            box="DEMO", name="i2c1", role="i2c",
            channel="0", address="0x76",
        )
        assert_lager_called_with(
            mock_subprocess,
            "nets", "add", "i2c1", "i2c", "0", "0x76", "--box", "DEMO",
        )

    def test_nets_delete(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_delete
        lager_nets_delete(box="DEMO", name="i2c1", net_type="i2c")
        assert_lager_called_with(
            mock_subprocess,
            "nets", "delete", "i2c1", "i2c", "--yes", "--box", "DEMO",
        )

    def test_nets_rename(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_rename
        lager_nets_rename(box="DEMO", name="i2c1", new_name="sensor_bus")
        assert_lager_called_with(
            mock_subprocess,
            "nets", "rename", "i2c1", "sensor_bus", "--box", "DEMO",
        )

    def test_nets_add_all(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_add_all
        lager_nets_add_all(box="DEMO")
        assert_lager_called_with(
            mock_subprocess,
            "nets", "add-all", "--yes", "--box", "DEMO",
        )

    def test_nets_add_batch(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_add_batch
        lager_nets_add_batch(box="DEMO", json_file="/tmp/nets.json")
        assert_lager_called_with(
            mock_subprocess,
            "nets", "add-batch", "/tmp/nets.json", "--box", "DEMO",
        )

    def test_nets_delete_all(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_delete_all
        lager_nets_delete_all(box="DEMO")
        assert_lager_called_with(
            mock_subprocess,
            "nets", "delete-all", "--yes", "--box", "DEMO",
        )

    def test_nets_set_script(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_set_script
        lager_nets_set_script(
            box="DEMO", name="debug1", script_path="/tmp/connect.jlink",
        )
        assert_lager_called_with(
            mock_subprocess,
            "nets", "set-script", "debug1", "/tmp/connect.jlink", "--box", "DEMO",
        )

    def test_nets_remove_script(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_remove_script
        lager_nets_remove_script(box="DEMO", name="debug1")
        assert_lager_called_with(
            mock_subprocess,
            "nets", "remove-script", "debug1", "--box", "DEMO",
        )

    def test_nets_show_script(self, mock_subprocess):
        from lager.mcp.tools.box import lager_nets_show_script
        lager_nets_show_script(box="DEMO", name="debug1")
        assert_lager_called_with(
            mock_subprocess,
            "nets", "show-script", "debug1", "--box", "DEMO",
        )

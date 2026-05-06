# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""
Tests for box_storage.py -- project-level .lager merging behavior.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from cli.box_storage import (
    _load_boxes_from_file,
    _load_global_boxes,
    load_boxes,
    add_box,
    delete_box,
    delete_all_boxes,
    update_box_version,
    save_boxes,
)


@pytest.fixture
def global_lager(tmp_path):
    """Create a temporary global .lager file and patch get_lager_file_path."""
    lager_file = tmp_path / "home" / ".lager"
    lager_file.parent.mkdir(parents=True)
    with mock.patch("cli.box_storage.get_lager_file_path", return_value=lager_file):
        yield lager_file


@pytest.fixture
def project_dir(tmp_path):
    """Create a temporary project directory."""
    proj = tmp_path / "project"
    proj.mkdir()
    return proj


def _write_lager(path, boxes):
    """Helper to write a .lager JSON file with a BOXES section."""
    data = {"BOXES": boxes}
    Path(path).write_text(json.dumps(data))


# ---------- _load_boxes_from_file ----------

class TestLoadBoxesFromFile:
    def test_reads_boxes_section(self, tmp_path):
        f = tmp_path / ".lager"
        _write_lager(f, {"box1": "1.1.1.1"})
        assert _load_boxes_from_file(f) == {"box1": "1.1.1.1"}

    def test_reads_legacy_duts(self, tmp_path):
        f = tmp_path / ".lager"
        f.write_text(json.dumps({"DUTS": {"old": "2.2.2.2"}}))
        assert _load_boxes_from_file(f) == {"old": "2.2.2.2"}

    def test_returns_empty_for_missing_file(self, tmp_path):
        assert _load_boxes_from_file(tmp_path / "nope") == {}

    def test_returns_empty_for_bad_json(self, tmp_path):
        f = tmp_path / ".lager"
        f.write_text("not json")
        assert _load_boxes_from_file(f) == {}

    def test_accepts_string_path(self, tmp_path):
        f = tmp_path / ".lager"
        _write_lager(f, {"s": "3.3.3.3"})
        assert _load_boxes_from_file(str(f)) == {"s": "3.3.3.3"}


# ---------- load_boxes (merge behavior) ----------

class TestLoadBoxesMerge:
    def test_global_only(self, global_lager):
        _write_lager(global_lager, {"g1": "10.0.0.1"})
        with mock.patch("cli.config._find_config_files", return_value=[]):
            assert load_boxes() == {"g1": "10.0.0.1"}

    def test_project_overrides_global(self, global_lager, project_dir):
        _write_lager(global_lager, {"shared": "10.0.0.1", "global_only": "10.0.0.2"})
        proj_lager = project_dir / ".lager"
        _write_lager(proj_lager, {"shared": "192.168.1.1", "proj_only": "192.168.1.2"})

        with mock.patch("cli.config._find_config_files", return_value=[str(proj_lager)]):
            boxes = load_boxes()

        assert boxes["shared"] == "192.168.1.1"       # project wins
        assert boxes["global_only"] == "10.0.0.2"     # kept from global
        assert boxes["proj_only"] == "192.168.1.2"    # added from project

    def test_closest_project_file_wins(self, global_lager, tmp_path):
        """When multiple project .lager files exist, the closest one wins."""
        _write_lager(global_lager, {"box": "10.0.0.1"})

        parent_lager = tmp_path / "parent" / ".lager"
        parent_lager.parent.mkdir(parents=True)
        _write_lager(parent_lager, {"box": "10.0.0.2"})

        child_lager = tmp_path / "parent" / "child" / ".lager"
        child_lager.parent.mkdir(parents=True)
        _write_lager(child_lager, {"box": "10.0.0.3"})

        # _find_config_files returns closest first
        with mock.patch(
            "cli.config._find_config_files",
            return_value=[str(child_lager), str(parent_lager)],
        ):
            boxes = load_boxes()

        assert boxes["box"] == "10.0.0.3"  # closest project file wins

    def test_no_global_file(self, global_lager, project_dir):
        """Works even when no global .lager exists."""
        proj_lager = project_dir / ".lager"
        _write_lager(proj_lager, {"proj": "1.2.3.4"})

        with mock.patch("cli.config._find_config_files", return_value=[str(proj_lager)]):
            assert load_boxes() == {"proj": "1.2.3.4"}

    def test_deleted_cwd_falls_back_to_global(self, global_lager):
        """If cwd has been deleted, falls back to global boxes only."""
        _write_lager(global_lager, {"g1": "10.0.0.1"})

        with mock.patch("cli.config._find_config_files", side_effect=FileNotFoundError):
            assert load_boxes() == {"g1": "10.0.0.1"}


# ---------- Write operations use global only ----------

class TestWriteOperationsUseGlobal:
    def test_add_box_writes_to_global(self, global_lager, project_dir):
        _write_lager(global_lager, {})
        proj_lager = project_dir / ".lager"
        _write_lager(proj_lager, {"proj_box": "192.168.1.1"})

        with mock.patch("cli.config._find_config_files", return_value=[str(proj_lager)]):
            add_box("new_box", "10.0.0.5")

        # Global file should have the new box
        global_data = json.loads(global_lager.read_text())
        assert "new_box" in global_data["BOXES"]

        # Project file should be unchanged
        proj_data = json.loads(proj_lager.read_text())
        assert "new_box" not in proj_data["BOXES"]
        assert "proj_box" in proj_data["BOXES"]

    def test_delete_box_only_deletes_from_global(self, global_lager, project_dir):
        _write_lager(global_lager, {"shared": "10.0.0.1"})
        proj_lager = project_dir / ".lager"
        _write_lager(proj_lager, {"proj_box": "192.168.1.1"})

        with mock.patch("cli.config._find_config_files", return_value=[str(proj_lager)]):
            result = delete_box("shared")

        assert result is True
        global_data = json.loads(global_lager.read_text())
        assert "shared" not in global_data["BOXES"]

        # Project file unchanged
        proj_data = json.loads(proj_lager.read_text())
        assert "proj_box" in proj_data["BOXES"]

    def test_delete_box_returns_false_for_project_only_box(self, global_lager, project_dir):
        """Deleting a box that only exists in project config returns False."""
        _write_lager(global_lager, {})
        proj_lager = project_dir / ".lager"
        _write_lager(proj_lager, {"proj_box": "192.168.1.1"})

        with mock.patch("cli.config._find_config_files", return_value=[str(proj_lager)]):
            result = delete_box("proj_box")

        assert result is False

    def test_delete_all_boxes_only_clears_global(self, global_lager, project_dir):
        _write_lager(global_lager, {"g1": "10.0.0.1", "g2": "10.0.0.2"})
        proj_lager = project_dir / ".lager"
        _write_lager(proj_lager, {"proj_box": "192.168.1.1"})

        with mock.patch("cli.config._find_config_files", return_value=[str(proj_lager)]):
            count = delete_all_boxes()

        assert count == 2
        global_data = json.loads(global_lager.read_text())
        assert global_data["BOXES"] == {}

        # Project file unchanged
        proj_data = json.loads(proj_lager.read_text())
        assert "proj_box" in proj_data["BOXES"]

    def test_update_box_version_global_only(self, global_lager):
        _write_lager(global_lager, {"mybox": {"ip": "10.0.0.1"}})

        update_box_version("mybox", "staging")

        global_data = json.loads(global_lager.read_text())
        assert global_data["BOXES"]["mybox"]["version"] == "staging"

# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""Live integration tests for Lager CLI defaults MCP tools."""

import pytest

from lager.mcp.tools.defaults import (
    lager_defaults_show,
    lager_defaults_set,
    lager_defaults_delete,
    lager_defaults_delete_all,
)


@pytest.mark.integration
@pytest.mark.defaults
class TestDefaultsLive:

    @pytest.fixture(autouse=True)
    def save_and_restore_defaults(self):
        """Save existing defaults before the test and restore them afterward.

        We capture the current defaults output so we can detect what was set,
        then delete everything we changed after the test.  Because defaults
        are stored as simple key-value pairs and there is no atomic
        save/restore API, we use delete-all + re-set as the safest approach.
        """
        original = lager_defaults_show()
        yield
        # Best-effort restore: wipe whatever the test left and silently
        # re-apply the originals.  If the original had no defaults the
        # delete-all is harmless.
        lager_defaults_delete_all()
        # Re-parse and restore any box default that was present.
        # The output format is human-readable; we look for the box line.
        for line in original.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("box:"):
                box_name = stripped.split(":", 1)[1].strip()
                if box_name:
                    lager_defaults_set(box=box_name)

    def test_show_defaults(self):
        """Showing defaults should return output without errors."""
        result = lager_defaults_show()
        assert "Error" not in result

    def test_set_and_show(self, box1):
        """Setting a box default then showing should reflect the change."""
        set_result = lager_defaults_set(box=box1)
        assert "Error" not in set_result

        show_result = lager_defaults_show()
        assert "Error" not in show_result
        assert box1 in show_result

    def test_delete(self, box1):
        """Deleting a default should actually remove the value."""
        set_result = lager_defaults_set(box=box1)
        assert "Error" not in set_result

        delete_result = lager_defaults_delete(setting="box")
        assert "Error" not in delete_result

        # Verify the value is actually gone
        show_result = lager_defaults_show()
        assert box1 not in show_result, \
            f"{box1} still present after delete: {show_result!r}"

    def test_delete_all(self, box1):
        """Deleting all defaults should clear everything."""
        lager_defaults_set(box=box1)

        result = lager_defaults_delete_all()
        assert "Error" not in result

        # Verify defaults are actually cleared
        show_result = lager_defaults_show()
        assert box1 not in show_result, \
            f"{box1} still present after delete_all: {show_result!r}"

    def test_set_no_args_error(self):
        """Calling set with no arguments should return an error string."""
        result = lager_defaults_set()
        assert "Error" in result

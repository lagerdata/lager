# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for filesystem-safe path building from client-supplied values.

The probe serial that names every per-probe pid/log file is read from a field
of a net's VISA address that is permissive about what it accepts. These tests
pin the invariant: whatever that field carries, the resulting path stays in
the directory it is supposed to be in.

The upgrade-safety assertion matters as much. An ordinary alphanumeric serial
must still produce the byte-identical filename it produced before, or a box
that upgrades mid-session loses track of a running gdbserver and orphans the
process.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BOX_DIR = os.path.join(REPO_ROOT, "box")

if BOX_DIR not in sys.path:
    sys.path.insert(0, BOX_DIR)

from lager.util.paths import contained_path, fs_slug  # noqa: E402

# contained_path normalises but does not resolve, so /tmp stays /tmp on
# macOS too -- production and a developer laptop spell these the same way.
TMP = "/tmp"

# An ordinary J-Link serial, and values whose separators must not survive
# into the built path.
REAL_SERIAL = "000651000000"
SEPARATOR_BEARING = (
    "../../a/b",
    "../a",
    "/a/b",
    "..",
    "a/b/c",
)


class TestFsSlug(unittest.TestCase):
    def test_leaves_ordinary_serials_untouched(self):
        for serial in (REAL_SERIAL, "A50285BI", "1-1.4", "abc_de-f.g"):
            self.assertEqual(fs_slug(serial), serial)

    def test_removes_every_separator(self):
        for value in SEPARATOR_BEARING:
            slug = fs_slug(value)
            self.assertNotIn("/", slug)
            self.assertNotIn("\\", slug)


class TestContainedPath(unittest.TestCase):
    def test_returns_a_path_under_the_root(self):
        self.assertEqual(
            os.path.dirname(contained_path("/tmp", "jlink_x.pid")), TMP)

    def test_rejects_an_unslugged_multi_component_name(self):
        with self.assertRaises(ValueError):
            contained_path("/tmp", "../a/b")


class TestProbeRuntimePaths(unittest.TestCase):
    """The six pid/log helpers in lager.debug.probes."""

    def setUp(self):
        from lager.debug import probes
        self.probes = probes
        self.helpers = (
            probes.jlink_gdbserver_pidfile,
            probes.jlink_gdbserver_logfile,
            probes.jlink_pidfile,
            probes.jlink_logfile,
            probes.openocd_pidfile,
            probes.openocd_logfile,
        )

    def test_ordinary_serial_keeps_its_historical_filename(self):
        """An upgrade must not rename a live probe's pid/log file."""
        expected = {
            "jlink_gdbserver_pidfile": "jlink_gdbserver_%s.pid" % REAL_SERIAL,
            "jlink_gdbserver_logfile": "jlink_gdbserver_%s.log" % REAL_SERIAL,
            "jlink_pidfile": "jlink_%s.pid" % REAL_SERIAL,
            "jlink_logfile": "jlink_%s.log" % REAL_SERIAL,
            "openocd_pidfile": "openocd_%s.pid" % REAL_SERIAL,
            "openocd_logfile": "openocd_%s.log" % REAL_SERIAL,
        }
        for helper in self.helpers:
            got = helper(REAL_SERIAL)
            self.assertEqual(os.path.basename(got), expected[helper.__name__])
            self.assertEqual(os.path.dirname(got), TMP)

    def test_a_serial_with_separators_stays_in_tmp(self):
        for helper in self.helpers:
            for value in SEPARATOR_BEARING:
                got = helper(value)
                self.assertEqual(
                    os.path.dirname(got), TMP,
                    "%s(%r) landed at %s" % (helper.__name__, value, got))

    def test_no_serial_still_returns_the_legacy_path(self):
        p = self.probes
        self.assertEqual(p.jlink_gdbserver_pidfile(None), "/tmp/jlink_gdbserver.pid")
        self.assertEqual(p.jlink_gdbserver_logfile(None), "/tmp/jlink_gdbserver.log")
        self.assertEqual(p.jlink_pidfile(None), "/tmp/jlink.pid")
        self.assertEqual(p.jlink_logfile(None), "/tmp/jlink.log")
        self.assertEqual(p.openocd_pidfile(None), "/tmp/openocd.pid")
        self.assertEqual(p.openocd_logfile(None), "/tmp/openocd.log")

    def test_slot_assignment_still_uses_the_raw_serial(self):
        """Slugging is a path concern; probe identity must not change."""
        self.assertEqual(
            self.probes.compute_slot(REAL_SERIAL, [REAL_SERIAL, "zzz"]), 0)


class TestPipEndpointRemoved(unittest.TestCase):
    """The unused /pip endpoint is gone, and stays gone.

    Its only caller addressed a port and path the box has never served, so
    both halves were dead code. This pins the removal.
    """

    def test_handler_is_gone(self):
        from lager.python import service
        self.assertFalse(hasattr(service.PythonServiceHandler, "_handle_pip"))

    def test_route_is_not_dispatched(self):
        source = open(
            os.path.join(BOX_DIR, "lager", "python", "service.py"),
            encoding="utf-8").read()
        self.assertNotIn("'/pip'", source)


if __name__ == "__main__":
    unittest.main()

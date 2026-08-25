# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""``probes.sniff_script_backend`` — routing a debug-script override by format.

A ``.JLinkScript`` is executed by the J-Link DLL and an OpenOCD ``.cfg`` is TCL
read by the daemon; neither backend can run the other's file. So
``DebugNet.connect(script=...)`` has to classify an override before it can
route it, and a wrong guess is worse than no guess — it would run the target
under an attach sequence nobody asked for. Hence: extension first, content
only as a fallback, and abstain rather than pick a side.

``cli/commands/box/nets.py`` carries a deliberate duplicate of this logic (it
must classify a file against ANY box version, including ones older than this
function, so it cannot import it). This module pins the box copy's rules; the
parity assertion that stops the two drifting lives in
``test/unit/cli/test_nets_debug_scripts.py``, where the CLI module has the
package context it needs.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", "..", ".."))
PROBES_PATH = os.path.join(REPO_ROOT, "box", "lager", "debug", "probes.py")


def _load_probes():
    """probes.py imports nothing but ``re``, so it loads standalone."""
    key = "_probes_for_sniff_tests"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, PROBES_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        spec.loader.exec_module(mod)
    return mod


class SniffScriptBackendTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.probes = _load_probes()

    def sniff(self, filename, content):
        return self.probes.sniff_script_backend(filename, content)

    # ---- extension is the dominant signal --------------------------------

    def test_jlink_extension_wins_over_openocd_content(self):
        """A user who named it .JLinkScript meant it."""
        self.assertEqual(
            self.sniff("a.JLinkScript", b"transport select swd\n"), "jlink")

    def test_openocd_extension_wins_over_jlink_content(self):
        self.assertEqual(
            self.sniff("a.cfg", b"void InitTarget() {}\n"), "openocd")

    def test_extensions_are_case_insensitive(self):
        self.assertEqual(self.sniff("A.JLINKSCRIPT", b""), "jlink")
        self.assertEqual(self.sniff("A.TCL", b""), "openocd")

    def test_every_declared_extension_classifies(self):
        for ext in self.probes._JLINK_EXTS:
            with self.subTest(ext=ext):
                self.assertEqual(self.sniff("a" + ext, b""), "jlink")
        for ext in self.probes._OPENOCD_EXTS:
            with self.subTest(ext=ext):
                self.assertEqual(self.sniff("a" + ext, b""), "openocd")

    # ---- content fallback -------------------------------------------------

    def test_every_declared_marker_classifies(self):
        """The content fallback is only as good as this list.

        Asserted marker-by-marker because the in-process path
        (``connect(script=<base64 blob>)``) has no filename to fall back on —
        markers are the ONLY signal there.
        """
        for marker in self.probes._OPENOCD_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(self.sniff("", marker.encode()), "openocd")
        for marker in self.probes._JLINK_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(self.sniff("", marker.encode()), "jlink")

    def test_markers_are_case_insensitive(self):
        self.assertEqual(self.sniff("", b"TRANSPORT SELECT swd\n"), "openocd")
        self.assertEqual(self.sniff("", b"void InitTarget() {}\n"), "jlink")

    # ---- abstention -------------------------------------------------------

    def test_abstains_when_both_families_present(self):
        mixed = b"transport select swd\nvoid InitTarget() {}\n"
        self.assertIsNone(self.sniff("", mixed))

    def test_abstains_when_neither_family_present(self):
        self.assertIsNone(self.sniff("", b"# just a comment\n"))

    def test_abstains_on_empty_input(self):
        self.assertIsNone(self.sniff("", b""))
        self.assertIsNone(self.sniff(None, b""))

    def test_only_the_first_4k_is_sniffed(self):
        """Bounded read: a marker past the window must not be found."""
        self.assertIsNone(self.sniff("", b"#" * 5000 + b"target create x"))

    def test_undecodable_bytes_do_not_raise(self):
        self.assertIsNone(self.sniff("", b"\xff\xfe\x00\x01"))

    # ---- known blind spots -------------------------------------------------

    def test_jlink_signature_with_void_arg_is_not_matched(self):
        """Pins a PRE-EXISTING gap, ported verbatim from the CLI copy.

        The marker is ``inittarget()`` — empty parens — but real JLinkScript
        files declare ``void InitTarget(void)``. Likewise the marker is
        ``jlink_executecommand`` while the Segger API is ``JLINK_ExecCommand``.
        So the content fallback misses the two most common J-Link forms and
        abstains.

        Abstaining is SAFE — ``connect`` raises and asks the caller to be
        explicit rather than routing on a guess — but it means a base64
        J-Link blob usually needs ``jlink_script=`` rather than ``script=``.
        Widening these markers changes ``lager nets set-script`` too, so it is
        deliberately not done here. Change this test when that is fixed.
        """
        self.assertIsNone(self.sniff("", b"void InitTarget(void) {}\n"))
        self.assertIsNone(self.sniff("", b'JLINK_ExecCommand("x");\n'))


if __name__ == "__main__":
    unittest.main()

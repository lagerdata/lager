# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
box/lager/docker/collect_pip_licenses.py -- the script the box image build runs
to put every Python distribution's license notices in one place.

It runs once, inside `docker build`, where nothing can watch it. So it is
exercised here against distributions made up in a temp directory: one that
follows PEP 639 (a `License-Expression` and a `licenses/` directory), one from
before it (a classifier and a LICENSE beside METADATA), one with the whole
license text in its `License` field, and one that declares nothing.

Two properties matter beyond "it finds the files". It must never fail the
build, because a package with no license file is a finding and not a reason to
ship no notices at all. And its output must be deterministic, because the
layer it produces sits above the box source in the image: output that changed
from build to build would give that layer a new digest every release, and
every box would download it again.
"""
import importlib.util
import os
import pathlib
import unittest
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO / "box" / "lager" / "docker" / "collect_pip_licenses.py"


def _load():
    spec = importlib.util.spec_from_file_location("collect_pip_licenses_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cpl = _load()


def _dist(site, dirname, metadata, files=None, record=True):
    """Create one fake ``*.dist-info`` directory under ``site``."""
    info = pathlib.Path(site) / dirname
    info.mkdir(parents=True)
    (info / "METADATA").write_text(metadata, encoding="utf-8")
    for relative, content in (files or {}).items():
        target = info / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if record:
        (info / "RECORD").write_text("", encoding="utf-8")
    return info


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = pathlib.Path(self.tmp.name)
        self.site = root / "site-packages"
        self.site.mkdir()
        self.dest = root / "out" / "pip"

    def _collect(self, extra_path=()):
        return cpl.collect(str(self.dest), path=[str(self.site), *extra_path])

    def _index(self):
        text = (self.dest / "INDEX.tsv").read_text(encoding="utf-8")
        return [line.split("\t") for line in text.splitlines()]

    def _four(self):
        # Created out of alphabetical order, on purpose.
        _dist(self.site, "gamma-3.dist-info",
              "Metadata-Version: 2.1\nName: gamma\nVersion: 3\n"
              "License: Permission is hereby granted, free of charge\n"
              "        to any person obtaining a copy\n", record=False)
        _dist(self.site, "Beta_pkg-2.0.dist-info",
              "Metadata-Version: 2.1\nName: Beta_pkg\nVersion: 2.0\n"
              "Classifier: Programming Language :: Python :: 3\n"
              "Classifier: License :: OSI Approved :: BSD License\n",
              files={"LICENSE.txt": "BSD text", "AUTHORS": "someone", "WHEEL": "x"})
        _dist(self.site, "alpha-1.0.dist-info",
              "Metadata-Version: 2.4\nName: alpha\nVersion: 1.0\n"
              "License-Expression: MIT\nLicense-File: LICENSE\n",
              files={"licenses/LICENSE": "MIT text",
                     "licenses/vendored/NOTICE.txt": "vendored notice"})
        _dist(self.site, "delta-0.1.dist-info",
              "Metadata-Version: 2.1\nName: delta\nVersion: 0.1\n")


class WhatItWrites(_Case):
    def test_one_row_per_distribution_sorted_by_name(self):
        self._four()
        self._collect()
        self.assertEqual(self._index(), [
            ["name", "version", "license", "notice_files"],
            ["alpha", "1.0", "MIT", "licenses/LICENSE;licenses/vendored/NOTICE.txt"],
            ["Beta_pkg", "2.0", "BSD License", "AUTHORS;LICENSE.txt"],
            ["delta", "0.1", "UNKNOWN", "-"],
            ["gamma", "3", "Permission is hereby granted, free of charge", "-"],
        ])

    def test_each_notice_file_is_copied_under_the_normalized_name(self):
        self._four()
        self._collect()
        self.assertEqual((self.dest / "alpha-1.0" / "licenses" / "LICENSE").read_text(), "MIT text")
        self.assertEqual(
            (self.dest / "alpha-1.0" / "licenses" / "vendored" / "NOTICE.txt").read_text(),
            "vendored notice")
        self.assertEqual((self.dest / "beta-pkg-2.0" / "LICENSE.txt").read_text(), "BSD text")
        self.assertEqual((self.dest / "beta-pkg-2.0" / "AUTHORS").read_text(), "someone")

    def test_a_file_that_is_not_a_notice_is_left_behind(self):
        self._four()
        self._collect()
        for name in ("WHEEL", "METADATA", "RECORD"):
            self.assertFalse((self.dest / "beta-pkg-2.0" / name).exists(), name)

    def test_a_distribution_with_no_record_is_still_read_from_disk(self):
        _dist(self.site, "norecord-1.dist-info",
              "Metadata-Version: 2.1\nName: norecord\nVersion: 1\n",
              files={"COPYING": "gpl"}, record=False)
        self._collect()
        self.assertEqual(self._index()[1], ["norecord", "1", "UNKNOWN", "COPYING"])

    def test_the_declared_license_prefers_the_most_precise_source(self):
        _dist(self.site, "both-1.dist-info",
              "Metadata-Version: 2.4\nName: both\nVersion: 1\n"
              "License-Expression: Apache-2.0 OR MIT\nLicense: something vaguer\n"
              "Classifier: License :: OSI Approved :: MIT License\n")
        _dist(self.site, "legacy-1.dist-info",
              "Metadata-Version: 2.1\nName: legacy\nVersion: 1\nLicense: UNKNOWN\n"
              "Classifier: License :: OSI Approved :: MIT License\n"
              "Classifier: License :: OSI Approved :: Apache Software License\n")
        self._collect()
        rows = {row[0]: row[2] for row in self._index()[1:]}
        self.assertEqual(rows["both"], "Apache-2.0 OR MIT")
        self.assertEqual(rows["legacy"], "Apache Software License; MIT License")

    def test_a_cell_never_holds_a_tab_or_a_newline_and_a_long_field_is_cut(self):
        _dist(self.site, "messy-1.dist-info",
              "Metadata-Version: 2.1\nName: messy\nVersion: 1\n"
              "License: tab\there " + "x" * 400 + "\n")
        self._collect()
        row = self._index()[1]
        self.assertEqual(len(row), 4)
        self.assertTrue(row[2].startswith("tab here x"))
        self.assertTrue(row[2].endswith("..."))
        self.assertLessEqual(len(row[2]), 203)


class ItIsDeterministic(_Case):
    def _snapshot(self):
        files = {}
        for path in sorted(self.dest.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(self.dest))] = path.read_bytes()
        return files

    def test_two_runs_give_byte_identical_output(self):
        self._four()
        self._collect()
        first = self._snapshot()
        self._collect()
        self.assertEqual(self._snapshot(), first)
        self.assertNotIn(b"\r", first["INDEX.tsv"])

    def test_a_package_that_went_away_leaves_nothing_behind(self):
        self._four()
        self._collect()
        self.assertTrue((self.dest / "alpha-1.0").is_dir())
        import shutil
        shutil.rmtree(self.site / "alpha-1.0.dist-info")
        self._collect()
        self.assertFalse((self.dest / "alpha-1.0").exists())
        self.assertNotIn("alpha", [row[0] for row in self._index()])

    def test_the_same_distribution_on_the_path_twice_is_one_row(self):
        self._four()
        other = pathlib.Path(self.tmp.name) / "other-site"
        _dist(other, "alpha-1.0.dist-info",
              "Metadata-Version: 2.4\nName: alpha\nVersion: 1.0\nLicense-Expression: MIT\n")
        self._collect(extra_path=[str(other)])
        self.assertEqual([row[0] for row in self._index()].count("alpha"), 1)


class ItNeverFailsTheBuild(_Case):
    @unittest.skipIf(os.geteuid() == 0, "root reads a mode-000 file")
    def test_a_notice_file_that_cannot_be_read_is_skipped_and_the_rest_is_written(self):
        info = _dist(self.site, "locked-1.dist-info",
                     "Metadata-Version: 2.1\nName: locked\nVersion: 1\nLicense-Expression: MIT\n",
                     files={"LICENSE": "cannot read me", "NOTICE": "can read me"})
        (info / "LICENSE").chmod(0o000)
        try:
            self._collect()
        finally:
            (info / "LICENSE").chmod(0o644)
        self.assertEqual(self._index()[1], ["locked", "1", "MIT", "NOTICE"])

    def test_an_empty_search_path_writes_a_header_and_nothing_else(self):
        self._collect()
        self.assertEqual(self._index(), [["name", "version", "license", "notice_files"]])

    def test_a_metadata_directory_with_no_name_is_ignored(self):
        _dist(self.site, "broken-1.dist-info", "Metadata-Version: 2.1\n")
        self._collect()
        self.assertEqual(len(self._index()), 1)


class TheCommandLine(_Case):
    def test_it_wants_exactly_one_argument(self):
        self.assertEqual(cpl.main(["collect_pip_licenses.py"]), 2)
        self.assertEqual(cpl.main(["collect_pip_licenses.py", "a", "b"]), 2)
        self.assertFalse(self.dest.exists())

    def test_against_this_interpreter_it_finds_real_distributions(self):
        # The image build calls it with no search path: whatever is installed.
        self.assertEqual(cpl.main(["collect_pip_licenses.py", str(self.dest)]), 0)
        names = {row[0].lower() for row in self._index()[1:]}
        self.assertIn("pytest", names)
        self.assertGreater(len(names), 5)

    def test_it_imports_nothing_outside_the_standard_library(self):
        import ast
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add((node.module or "").split(".")[0])
        self.assertEqual(imported - {"__future__", "os", "re", "shutil", "sys", "importlib"}, set())


if __name__ == "__main__":
    unittest.main()

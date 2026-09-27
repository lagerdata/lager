#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Collect the license notices of every Python distribution in the box image.

Run once while the image is built, after the last ``pip install``:

    python3 collect_pip_licenses.py /usr/share/licenses/lager/pip

It writes, under that directory:

    INDEX.tsv                   one row per distribution: name, version, the
                                license it declares, and the notice files found
    <name>-<version>/...        a copy of each of those notice files

The image is published, so the notices that the packages ship with have to
travel with it. They already sit in each ``*.dist-info`` directory, but spread
across site-packages under names nobody would think to look for; this puts
them in one documented place.

What this records is what each distribution DECLARES about itself. It is not a
review of whether those terms permit redistribution, and nothing here should
be read as one.

Standard library only, on purpose. A license tool from PyPI would be one more
package in the image, installed to describe the others.

Nothing here fails the image build. A distribution with no license file, or a
file that cannot be read, is recorded as such and the run goes on: a missing
notice is a finding to act on, not a reason to ship no notices at all.

The output is deterministic -- sorted, no timestamps -- so an unchanged set of
packages gives byte-identical files, and the image layer keeps its digest.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from importlib import metadata

INDEX_NAME = "INDEX.tsv"
INDEX_HEADER = ("name", "version", "license", "notice_files")

# File names that carry a notice, wherever they sit in the dist-info directory.
_NOTICE_NAME = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS|COPYRIGHT)([._-].*)?$", re.I)

# A legacy `License:` field sometimes holds the whole license text.
_MAX_LICENSE_FIELD = 200

NO_NOTICE_FILE = "-"
UNDECLARED = "UNKNOWN"


def _normalize(name: str) -> str:
    """PEP 503: the form in which two spellings of one name compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _one_line(text: str) -> str:
    """One TSV cell: no tabs, no newlines, no leading or trailing space."""
    return re.sub(r"\s+", " ", text or "").strip()


def declared_license(dist) -> str:
    """What the distribution says its license is, most precise source first."""
    meta = dist.metadata
    expression = _one_line(meta.get("License-Expression", ""))
    if expression:
        return expression
    field = meta.get("License", "") or ""
    first_line = _one_line(field.strip().splitlines()[0]) if field.strip() else ""
    if first_line and first_line.upper() != UNDECLARED:
        if len(first_line) > _MAX_LICENSE_FIELD:
            first_line = first_line[:_MAX_LICENSE_FIELD].rstrip() + "..."
        return first_line
    classifiers = sorted(
        _one_line(c.split("::")[-1])
        for c in (meta.get_all("Classifier") or [])
        if c.startswith("License ::")
    )
    return "; ".join(classifiers) if classifiers else UNDECLARED


def _dist_info_dir(dist):
    """The distribution's metadata directory, or None."""
    path = getattr(dist, "_path", None)
    return os.fspath(path) if path is not None and os.path.isdir(path) else None


def notice_files(dist) -> list:
    """``(relative name, absolute path)`` for each notice file, sorted.

    The directory is listed rather than read from RECORD: a distribution
    installed without a RECORD still has its files on disk, and RECORD says
    nothing a directory listing does not.
    """
    info_dir = _dist_info_dir(dist)
    if info_dir is None:
        return []
    found = {}
    for entry in sorted(os.listdir(info_dir)):
        full = os.path.join(info_dir, entry)
        if os.path.isfile(full) and _NOTICE_NAME.match(entry):
            found[entry] = full
    # PEP 639 puts them under licenses/, possibly in subdirectories.
    licenses_dir = os.path.join(info_dir, "licenses")
    if os.path.isdir(licenses_dir):
        for root, dirs, names in os.walk(licenses_dir):
            dirs.sort()
            for name in sorted(names):
                full = os.path.join(root, name)
                found[os.path.relpath(full, info_dir)] = full
    return sorted(found.items())


def collect(dest: str, path=None) -> list:
    """Write the index and the notice files under ``dest``. Returns the rows.

    ``path`` is the search path to read distributions from; ``None`` means
    this interpreter's own, which is what the image build wants. A test passes
    a directory of its own.
    """
    kwargs = {} if path is None else {"path": list(path)}
    seen, dists = set(), []
    for dist in metadata.distributions(**kwargs):
        name = dist.metadata.get("Name") if dist.metadata else None
        if not name:
            continue
        key = (_normalize(name), dist.version)
        if key in seen:  # the same distribution reachable twice on the path
            continue
        seen.add(key)
        dists.append((key, name, dist))
    dists.sort(key=lambda item: item[0])

    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)

    rows = []
    for (normalized, version), name, dist in dists:
        copied = []
        target_dir = os.path.join(dest, f"{normalized}-{version}")
        for relative, source in notice_files(dist):
            target = os.path.join(target_dir, relative)
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(source, target)
            except OSError as exc:
                print(f"collect_pip_licenses: could not copy {source}: {exc}", file=sys.stderr)
                continue
            copied.append(relative)
        rows.append((
            _one_line(name), _one_line(version), declared_license(dist),
            ";".join(copied) if copied else NO_NOTICE_FILE,
        ))

    with open(os.path.join(dest, INDEX_NAME), "w", encoding="utf-8", newline="\n") as index:
        index.write("\t".join(INDEX_HEADER) + "\n")
        for row in rows:
            index.write("\t".join(row) + "\n")
    return rows


def main(argv) -> int:
    if len(argv) != 2:
        print("usage: collect_pip_licenses.py DEST_DIR", file=sys.stderr)
        return 2
    rows = collect(argv[1])
    without = sum(1 for row in rows if row[3] == NO_NOTICE_FILE)
    undeclared = sum(1 for row in rows if row[2] == UNDECLARED)
    print(f"collect_pip_licenses: {len(rows)} distributions, {without} with no notice "
          f"file, {undeclared} with no declared license -> {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

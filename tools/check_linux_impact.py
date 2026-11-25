#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""Fail when an existing Box or CLI file is changed without a ledger entry.

The macOS port must not break the working Linux Box. The safeguard is that
every change to a file a Linux Box or the CLI already has is written down --
what it does to Linux, how risky it is, how it was checked -- in
tools/linux_impact_ledger.txt. This script compares the files changed since
the base branch against that ledger and fails on any that are missing.

Only files that already exist on the base branch count. A new file cannot
affect Linux by itself.

Usage: tools/check_linux_impact.py [--base main]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LEDGER = REPO / "tools" / "linux_impact_ledger.txt"
GUARDED_PREFIXES = ("box/", "cli/")
RISKS = {"low", "medium", "high"}


def parse_ledger(text: str) -> tuple[dict[str, dict], list[str]]:
    """Return ({path: entry}, [problems])."""
    entries: dict[str, dict] = {}
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(" | ")]
        if len(fields) != 4 or not all(fields):
            problems.append(f"ledger line {number}: expected 4 non-empty fields separated by ' | '")
            continue
        path, effect, risk, verified = fields
        if risk.lower() not in RISKS:
            problems.append(f"ledger line {number}: risk must be low, medium or high, not {risk!r}")
            continue
        if path in entries:
            problems.append(f"ledger line {number}: {path} is listed twice")
            continue
        entries[path] = {"effect": effect, "risk": risk.lower(), "verified": verified}
    return entries, problems


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True,
                          capture_output=True, text=True).stdout


def changed_existing_files(base: str) -> set[str]:
    """Guarded files that exist on ``base`` and differ from it now.

    Compares against the merge base, and includes uncommitted work, so the
    check means the same thing before and after a commit.
    """
    merge_base = _git("merge-base", base, "HEAD").strip()
    changed: set[str] = set()
    # --no-renames: a rename is a deletion of a file Linux has, and must be
    # listed under the old path.
    for line in _git("diff", "--name-status", "--no-renames", merge_base).splitlines():
        status, _, path = line.partition("\t")
        if status.startswith("A"):
            continue
        if path.startswith(GUARDED_PREFIXES):
            changed.add(path)
    return changed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default="main", help="branch to compare against")
    args = parser.parse_args(argv)

    entries, problems = parse_ledger(LEDGER.read_text(encoding="utf-8"))
    changed = changed_existing_files(args.base)

    for path in sorted(changed - entries.keys()):
        problems.append(
            f"{path} already exists on {args.base} and is changed here, but has no entry in "
            f"{LEDGER.relative_to(REPO)}. State its effect on a Linux Box there first."
        )
    stale = sorted(entries.keys() - changed)

    if problems:
        print(f"FAIL: {len(problems)} problem(s).\n")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(f"OK: {len(changed)} existing box/ or cli/ file(s) changed against {args.base}; all listed.")
    for path in sorted(changed):
        entry = entries[path]
        print(f"  [{entry['risk']}] {path}: {entry['effect']} ({entry['verified']})")
    for path in stale:
        print(f"  note: {path} is listed but not changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

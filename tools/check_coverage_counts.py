#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Assert that test/COVERAGE.md's gated-test counts match what pytest reports.

COVERAGE.md opens with "Counts here are checked against disk", but nothing
enforced it and it drifted repeatedly: two merges in a single afternoon added
37 gated tests between them without touching the file, leaving it understating
the gate by 290. A number nobody verifies is worse than no number, because it
still reads as authoritative.

Each suite is RUN, not merely collected. Those differ: test/unit/test_pdf_pages.py
does a module-level `pytest.importorskip("fitz")`, so without pymupdf installed
it contributes 0 to `--collect-only` but 1 skipped to a real run. Collection
counts would therefore disagree with the table by one, forever, for a reason
that has nothing to do with coverage.

Table format, and what this checks:

    | `unit (blufi)` | test/unit/blufi/ | 79 (+4 skipped) |
                                          ^^        ^
                                          passed    skipped

  * leading integer  -> `N passed`
  * `(+N skipped)`   -> `N skipped`
  * `(+N xfailed)`   -> `N xfailed`
  * **Total gated**  -> sum of the PASSED counts (not collected, not outcomes)

Each suite runs in its own pytest process because test/unit/box/ and
test/unit/measurement/ want incompatible sys.modules states for the name
`lager` -- the same reason unit-tests.yml uses one job per suite.

Usage:  python tools/check_coverage_counts.py [--fix]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COVERAGE_MD = REPO_ROOT / 'test' / 'COVERAGE.md'

# Must stay in step with the matrix in .github/workflows/unit-tests.yml.
SUITES = {
    'unit (cli)': ['test/unit/cli/', 'cli/tests/'],
    'unit (box)': ['test/unit/box/'],
    'unit (measurement)': ['test/unit/measurement/'],
    'unit (blufi)': ['test/unit/blufi/'],
    'unit (mcp)': ['test/mcp/unit/'],
    'unit (root)': ['test/unit/test_*.py', 'test/test_*.py'],
}

ROW = re.compile(r'^\|\s*`?(unit \([a-z]+\))`?\s*\|(.*?)\|\s*([^|]+?)\s*\|\s*$')
TOTAL_ROW = re.compile(r'^\|\s*\|\s*\*\*Total gated\*\*\s*\|\s*\*\*(\d+)\*\*\s*\|\s*$')


def run_suite(paths: list[str]) -> dict[str, int]:
    """Run one suite; return {'passed': n, 'skipped': n, 'xfailed': n}."""
    expanded: list[str] = []
    for p in paths:
        if '*' in p:
            expanded.extend(sorted(str(x.relative_to(REPO_ROOT))
                                   for x in REPO_ROOT.glob(p)))
        else:
            expanded.append(p)

    env = dict(os.environ)
    env['PYTHONPATH'] = f"{REPO_ROOT}:{REPO_ROOT / 'box'}"
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', *expanded, '-q',
         '--import-mode=importlib', '-c', '/dev/null', '--timeout=60'],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    if proc.returncode != 0:
        print(f'  suite FAILED: {" ".join(paths)}', file=sys.stderr)
        print(proc.stdout[-3000:], file=sys.stderr)
        raise SystemExit(2)

    tail = [ln for ln in proc.stdout.splitlines()
            if re.match(r'^\d+ (passed|failed)', ln)]
    if not tail:
        print(f'  could not parse summary for {" ".join(paths)}', file=sys.stderr)
        print(proc.stdout[-3000:], file=sys.stderr)
        raise SystemExit(2)
    line = tail[-1]
    out = {'passed': 0, 'skipped': 0, 'xfailed': 0}
    for key in out:
        m = re.search(rf'(\d+) {key}', line)
        if m:
            out[key] = int(m.group(1))
    return out


def parse_cell(cell: str) -> dict[str, int]:
    lead = re.match(r'\s*(\d+)', cell)
    out = {'passed': int(lead.group(1)) if lead else -1, 'skipped': 0, 'xfailed': 0}
    for key in ('skipped', 'xfailed'):
        m = re.search(rf'\+(\d+)\s+{key}', cell)
        if m:
            out[key] = int(m.group(1))
    return out


def render_cell(counts: dict[str, int]) -> str:
    parts = [str(counts['passed'])]
    extras = [f"+{counts[k]} {k}" for k in ('skipped', 'xfailed') if counts[k]]
    if extras:
        parts.append(f"({', '.join(extras)})")
    return ' '.join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--fix', action='store_true',
                    help='rewrite the table instead of failing')
    args = ap.parse_args()

    text = COVERAGE_MD.read_text()
    claimed, cells, claimed_total = {}, {}, None
    for line in text.splitlines():
        m = ROW.match(line)
        if m and m.group(1) in SUITES:
            claimed[m.group(1)] = parse_cell(m.group(3))
            cells[m.group(1)] = m.group(3)
        t = TOTAL_ROW.match(line)
        if t:
            claimed_total = int(t.group(1))

    missing = set(SUITES) - set(claimed)
    if missing:
        print(f'FAIL: no row in COVERAGE.md for: {sorted(missing)}', file=sys.stderr)
        return 1

    print('Running each suite in its own process, as CI does...')
    actual = {name: run_suite(paths) for name, paths in SUITES.items()}

    bad = []
    for name in SUITES:
        same = all(claimed[name][k] == actual[name][k]
                   for k in ('passed', 'skipped', 'xfailed'))
        if not same:
            bad.append(name)
        print(f'  {"OK " if same else "BAD"} {name:20s} '
              f'table {render_cell(claimed[name]):22s} '
              f'actual {render_cell(actual[name])}')

    actual_total = sum(a['passed'] for a in actual.values())
    total_ok = claimed_total == actual_total
    print(f'  {"OK " if total_ok else "BAD"} {"Total gated":20s} '
          f'table {claimed_total:<22} actual {actual_total}')

    if not bad and total_ok:
        print('\nCOVERAGE.md counts match.')
        return 0

    if args.fix:
        new = text
        for name in bad:
            new = new.replace(f'| {cells[name]} |',
                              f'| {render_cell(actual[name])} |', 1)
        if not total_ok:
            new = re.sub(r'\*\*Total gated\*\* \| \*\*\d+\*\*',
                         f'**Total gated** | **{actual_total}**', new)
        COVERAGE_MD.write_text(new)
        print('\nRewrote the table. Review the diff before committing.')
        return 0

    print('\nFAIL: test/COVERAGE.md disagrees with what pytest reports.\n'
          '      That file states its counts are checked against disk, so a\n'
          '      stale number there is a defect, not a formatting nit.\n\n'
          '      Fix:  python tools/check_coverage_counts.py --fix\n'
          '      then review the diff and commit it with your test change.',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

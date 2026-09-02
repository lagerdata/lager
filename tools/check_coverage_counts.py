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

Section headers carry file counts too, and those were unchecked until 2026-08-05
-- by which point the CLI header claimed 43 files against 48 on disk and nine
test files from three merged PRs had no row in any table. Those counts are now
checked the same way, by globbing the path the header itself names. That half is
pure filesystem work, so `--files-only` verifies it without running any suite.

The counts record what CI sees, and CI is Linux. Some tests are gated on the
platform -- `test/unit/box/` skips six on `/proc` and one on `flock(1)` -- so
the same suite legitimately reports 1547 passed / 7 skipped on macOS and 1554
passed / 0 skipped on a runner. Off Linux this tool therefore treats a row
whose SKIP count moved as unverifiable rather than wrong: it reports it, keeps
the table's number when totalling, and `--fix` will not touch it. Without that
rule `--fix` rewrote the box row to the local number and handed back a table
that fails the CI gate it feeds -- which happened, twice, on the branch that
added this paragraph.

A row whose skip count is unchanged is still checked off Linux, because nothing
platform-gated can be hiding in it. On Linux every row is checked, as before.

Per-file table rows are enforced too: every test file on disk must have a row
in its section's inventory table, and every row must name a file that exists.
Before this, a count could be corrected without describing what was added --
the header said 74 and the table listed 74, but nothing tied either to WHICH
files. `--fix` removes rows for deleted files; it never invents a row, because
a row's description has to come from the author and no reasonable check can
tell a real description from a placeholder. Like the header counts, this is
pure filesystem work and runs under `--files-only`, and it is platform-blind:
files on disk do not depend on where the tool runs.

What is still NOT enforced: that a row's description matches what its file
actually tests.

Usage:  python tools/check_coverage_counts.py [--fix] [--files-only]
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

#: The table records CI's numbers, and CI runs Linux. Elsewhere, platform-gated
#: tests skip instead of passing, so a moved skip count is a property of the
#: machine rather than of the tree. See the module docstring.
ON_LINUX = sys.platform.startswith('linux')

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

# Section headers carry their own file counts, e.g.
#
#     #### Box Unit Tests (`test/unit/box/` -- 67 files)
#                          ^^^^^^^^^^^^^^^    ^^
#                          path               claimed count
#
# Those drifted for months while the run-counts above were gated: on 2026-08-05
# the CLI header said 43 against 48 on disk, and 9 test files from three merged
# PRs had no row at all. The path is in the header, so this needs no table --
# a directory is counted recursively, and a header whose "path" is already a
# glob is matched as written.
HEADER = re.compile(r'^(#+ .*\(`)([^`]+)(`\s+--\s+)(\d+)( files?\))\s*$')

# pytest's own words when it is handed an argument no installed plugin
# declares. It exits 4 before collecting, and writes this to stderr.
MISSING_TIMEOUT_PLUGIN = 'unrecognized arguments: --timeout'

# The pin CI installs (static-checks.yml, unit-tests.yml). Named here so the
# message can tell a contributor exactly what to install.
TIMEOUT_PLUGIN_PIN = 'pytest-timeout>=2.3,<3'

# Suites that are not Python `test_*.py` files.
GLOB_OVERRIDES = {'test/integration/': '**/*.sh'}

# Sections whose tables carry ONE ROW PER TEST FILE. Keys are the header paths
# exactly as they appear in COVERAGE.md's `(-- N files)` headers. Every path
# here must parse to a non-empty table -- an empty parse means the table was
# removed or its format changed, and that must fail loudly rather than pass as
# "nothing to compare".
INVENTORY_TABLES = (
    'test/unit/box/',
    'test/unit/cli/',
    'test/unit/measurement/',
    'test/unit/blufi/',
    'test/unit/test_*.py',
    'test/test_*.py',
    'cli/tests/',
    'test/mcp/unit/',
    'test/mcp/integration/',
)

# A table row whose first cell is a backticked bare filename, e.g.
#     | `test_acroname_driver.py` | Acroname USB hub driver: ... |
# The character class excludes '/' on purpose: rows elsewhere in the document
# (the coverage-gap tables, the undertested list) put full *module* paths like
# `cli/core/param_types.py` in their first cell, and those are claims about
# source files, not test inventory.
ROW_FILE = re.compile(r'^\|\s*`([^`/]+\.py)`')


def count_files(path: str) -> int:
    """Files backing one section header, counted the way the header means it."""
    if path.endswith('/'):
        return len(list((REPO_ROOT / path).glob(GLOB_OVERRIDES.get(path, '**/test_*.py'))))
    return len(list(REPO_ROOT.glob(path)))


def check_file_counts(text: str, fix: bool) -> tuple[list[str], str]:
    """Compare every section header's file count with disk.

    Cheap on purpose -- globbing only, no pytest -- so a docs-only change can
    verify itself without a test environment.
    """
    bad, out = [], []
    for line in text.splitlines(keepends=True):
        m = HEADER.match(line.rstrip('\n'))
        if not m:
            out.append(line)
            continue
        head, path, mid, claimed, tail = m.groups()
        actual = count_files(path)
        same = int(claimed) == actual
        print(f'  {"OK " if same else "BAD"} {path:28s} '
              f'header {claimed:<6} actual {actual}')
        if not same:
            bad.append(path)
            if fix:
                tail = ' file)' if actual == 1 else ' files)'
                line = f'{head}{path}{mid}{actual}{tail}\n'
        out.append(line)
    return bad, ''.join(out)


def files_on_disk(path: str) -> set[str]:
    """Basenames of the test files behind one section header."""
    if path.endswith('/'):
        return {p.name for p in
                (REPO_ROOT / path).glob(GLOB_OVERRIDES.get(path, '**/test_*.py'))}
    return {p.name for p in REPO_ROOT.glob(path)}


def check_table_inventory(text: str, fix: bool) -> tuple[list[str], int, str]:
    """Compare every inventory table's rows with the files on disk.

    Rows are associated with the nearest preceding `(-- N files)` header, and
    the association resets on EVERY heading line -- a row can never bleed into
    the wrong section when a prose subsection sits between two tables. Fenced
    code is skipped: the tree diagram contains both '#' and '|' lines.

    Returns (bad section paths, count of files missing a row, updated text).
    With fix=True the updated text has rows for deleted files removed; rows
    for NEW files are never generated -- the author writes those.
    """
    rows: dict[str, list[str]] = {p: [] for p in INVENTORY_TABLES}
    out, current, in_fence = [], None, False
    stale_all: set[tuple[str, str]] = set()

    for line in text.splitlines(keepends=True):
        stripped = line.rstrip('\n')
        if stripped.startswith('```'):
            in_fence = not in_fence
        elif not in_fence and stripped.startswith('#'):
            m = HEADER.match(stripped)
            current = m.group(2) if m and m.group(2) in rows else None
        elif not in_fence and current:
            m = ROW_FILE.match(stripped)
            if m:
                rows[current].append((m.group(1)))
        out.append(line)

    bad, missing_total = [], 0
    for path in INVENTORY_TABLES:
        disk = files_on_disk(path)
        table = set(rows[path])
        missing = sorted(disk - table)
        stale = sorted(table - disk)
        ok = bool(rows[path]) and not missing and not stale
        print(f'  {"OK " if ok else "BAD"} {path:28s} '
              f'disk {len(disk):<4} rows {len(rows[path])}')
        if not rows[path]:
            print('        no rows parsed -- table removed, or its format changed')
        for f in missing:
            print(f'        on disk but no table row: {f}')
        for f in stale:
            print(f'        table row but not on disk: {f}')
        if not ok:
            bad.append(path)
        missing_total += len(missing)
        stale_all.update((path, f) for f in stale)

    if fix and stale_all:
        stale_names = {f for _, f in stale_all}
        kept, current, in_fence = [], None, False
        for line in out:
            stripped = line.rstrip('\n')
            if stripped.startswith('```'):
                in_fence = not in_fence
            elif not in_fence and stripped.startswith('#'):
                m = HEADER.match(stripped)
                current = m.group(2) if m and m.group(2) in rows else None
            elif not in_fence and current:
                m = ROW_FILE.match(stripped)
                if m and m.group(1) in stale_names:
                    continue
            kept.append(line)
        out = kept

    return bad, missing_total, ''.join(out)


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
    # --color=no: the summary is parsed with an anchored `^\d+ passed`, and
    # pytest colourises whenever FORCE_COLOR is set in the environment even
    # though this pipe is not a terminal. Some agent and CI shells export it,
    # and the escape prefix made every run die on "could not parse summary".
    proc = subprocess.run(
        [sys.executable, '-m', 'pytest', *expanded, '-q', '--color=no',
         '--import-mode=importlib', '-c', '/dev/null', '--timeout=60'],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    if proc.returncode != 0:
        # A missing plugin is not a failing suite, and saying so cost four
        # fresh environments a few minutes each in one day. pytest rejects an
        # unrecognized argument before it collects anything, exits 4, and
        # writes the reason to STDERR -- so the old message named a healthy
        # suite and then printed an empty stdout tail underneath it.
        #
        # Nothing a contributor can install declares the plugin:
        # test/requirements-unit.txt does not mention pytest at all, and CI
        # installs the pin inline (static-checks.yml). Rather than add a second
        # place for the pin to live and drift, the tool names what to install.
        if MISSING_TIMEOUT_PLUGIN in proc.stderr:
            print(f'  {TIMEOUT_PLUGIN_PIN} is not installed, so no suite could '
                  f'run.\n'
                  f'  The tests are fine. Install it and re-run:\n'
                  f"      pip install '{TIMEOUT_PLUGIN_PIN}'",
                  file=sys.stderr)
            raise SystemExit(2)
        print(f'  suite FAILED: {" ".join(paths)}', file=sys.stderr)
        print(proc.stdout[-3000:], file=sys.stderr)
        # stderr too: a usage error writes there and nowhere else, so
        # discarding it is what left the message above with nothing under it.
        if proc.stderr.strip():
            print(proc.stderr[-3000:], file=sys.stderr)
        raise SystemExit(2)

    tail = [ln for ln in proc.stdout.splitlines()
            if re.match(r'^\d+ (passed|failed)', ln)]
    if not tail:
        print(f'  could not parse summary for {" ".join(paths)}', file=sys.stderr)
        print(proc.stdout[-3000:], file=sys.stderr)
        if proc.stderr.strip():
            print(proc.stderr[-3000:], file=sys.stderr)
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


def classify(claimed: dict[str, int], actual: dict[str, int],
             on_linux: bool = ON_LINUX) -> str:
    """One row's verdict: 'ok', 'drift', or 'platform'.

    'platform' means the run skipped a different number of tests than the table
    records, on a machine that is not the one the table describes. The passed
    count from such a run says nothing about the tree -- the missing tests went
    to skips, not to failures -- so it is neither a pass nor a failure here.
    On Linux that leniency would hide real drift, so it is not applied.
    """
    if all(claimed[k] == actual[k] for k in ('passed', 'skipped', 'xfailed')):
        return 'ok'
    if not on_linux and claimed['skipped'] != actual['skipped']:
        return 'platform'
    return 'drift'


def gated_total(claimed: dict[str, dict[str, int]],
                actual: dict[str, dict[str, int]],
                verdicts: dict[str, str]) -> int:
    """Sum the passed counts, trusting the table where this machine cannot look.

    A platform-gated row contributes the number the table claims. That keeps the
    total a real check of every other row instead of an assertion that fails for
    a reason the developer cannot act on.
    """
    return sum((claimed if verdicts[name] == 'platform' else actual)[name]['passed']
               for name in claimed)


def render_cell(counts: dict[str, int]) -> str:
    parts = [str(counts['passed'])]
    extras = [f"+{counts[k]} {k}" for k in ('skipped', 'xfailed') if counts[k]]
    if extras:
        parts.append(f"({', '.join(extras)})")
    return ' '.join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--fix', action='store_true',
                    help='rewrite the counts instead of failing')
    ap.add_argument('--files-only', action='store_true',
                    help='check only the per-section file counts; no pytest run, '
                         'so a docs-only change can verify itself without a test env')
    args = ap.parse_args()

    text = COVERAGE_MD.read_text()

    print('Counting the files behind each section header...')
    bad_files, text = check_file_counts(text, args.fix)

    print('Checking each inventory table lists exactly the files on disk...')
    bad_rows, missing_rows, text = check_table_inventory(text, args.fix)

    if args.files_only:
        if args.fix:
            COVERAGE_MD.write_text(text)
            if bad_files or bad_rows:
                print('\nRewrote what can be rewritten. Review the diff before committing.')
            if missing_rows:
                print(f'\n{missing_rows} file(s) still have no table row. --fix never\n'
                      'writes one: add the row yourself, with a description of what\n'
                      'the file actually tests.', file=sys.stderr)
                return 1
            if bad_files or bad_rows:
                return 0
        if bad_files or bad_rows:
            print('\nFAIL: test/COVERAGE.md disagrees with disk (see BAD lines above).\n'
                  '      Fix:  python tools/check_coverage_counts.py --files-only --fix\n'
                  '      rewrites header counts and drops rows for deleted files.\n'
                  '      A NEW file needs a hand-written row in its section\'s table.',
                  file=sys.stderr)
            return 1
        print('\nCOVERAGE.md file counts and inventory tables match.')
        return 0

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

    verdicts = {name: classify(claimed[name], actual[name]) for name in SUITES}
    bad = [name for name, verdict in verdicts.items() if verdict == 'drift']
    label = {'ok': 'OK ', 'drift': 'BAD', 'platform': 'n/a'}
    for name in SUITES:
        print(f'  {label[verdicts[name]]} {name:20s} '
              f'table {render_cell(claimed[name]):22s} '
              f'actual {render_cell(actual[name])}'
              f'{"   (platform-gated here; table kept)" if verdicts[name] == "platform" else ""}')

    actual_total = gated_total(claimed, actual, verdicts)
    total_ok = claimed_total == actual_total
    print(f'  {"OK " if total_ok else "BAD"} {"Total gated":20s} '
          f'table {claimed_total:<22} actual {actual_total}')

    gated = [name for name, verdict in verdicts.items() if verdict == 'platform']
    if gated:
        print(f'\nNot checked on {sys.platform}: {", ".join(gated)}.\n'
              '      Those suites skipped tests this table counts as passing, so\n'
              '      the numbers here describe the machine, not the tree. CI (Linux)\n'
              '      checks them; --fix leaves them alone.')

    if not bad and total_ok and not bad_files and not bad_rows:
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
        print('\nRewrote the counts. Review the diff before committing.')
        if missing_rows:
            print(f'\n{missing_rows} file(s) still have no table row. --fix never\n'
                  'writes one: add the row yourself, with a description of what\n'
                  'the file actually tests.', file=sys.stderr)
            return 1
        return 0

    print('\nFAIL: test/COVERAGE.md disagrees with what is on disk.\n'
          '      That file states its counts are checked against disk, so a\n'
          '      stale number there is a defect, not a formatting nit.\n\n'
          '      Fix:  python tools/check_coverage_counts.py --fix\n'
          '      then review the diff and commit it with your test change.\n'
          '      A new file needs a row in its section table as well as a count.',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

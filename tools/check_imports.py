#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Import every module of an INSTALLED package and reconcile the failures
against a committed baseline.

Why this exists: every CI job installs the CLI editable from a full checkout,
so "the wheel a user pip-installs actually works" was proven by nothing. The
first run of this check against a built wheel found five defect classes that
twelve green checks never saw: a console script pointing at a module the
wheel does not ship, a vendored tree missing a whole subpackage, two modules
importing from the box/ tree, and an undeclared runtime dependency.

The baseline is TWO-SIDED, like test/xplat and the shellcheck exclusions:
  - a failure not covered by the baseline fails the check (new breakage);
  - a baseline entry whose module now imports cleanly ALSO fails, with
    "remove it" in the message -- the file can only shrink honestly.

Baseline format: one module per line, `#` comments. A trailing `.*` makes an
entry a prefix covering a subtree (it must still match at least one real
failure, or it is stale and fails).

The walk must resolve the package from the INSTALLED environment, not the
checkout sitting in the working directory. Running as a script keeps the cwd
off sys.path, and the resolved origin is asserted to live under sys.prefix.
`--allow-source` skips that assertion for editable/dev-tree runs (the
cross-platform smoke uses it).

Usage:
    <venv>/bin/python tools/check_imports.py \
        --package cli --baseline tools/packaging_import_baseline.txt
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path


def load_baseline(path: Path) -> tuple[set[str], set[str]]:
    """Return (exact module names, prefixes) from the baseline file."""
    exact, prefixes = set(), set()
    for raw in path.read_text().splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if line.endswith('.*'):
            prefixes.add(line[:-1])  # keep the trailing dot
        else:
            exact.add(line)
    return exact, prefixes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--package', required=True)
    ap.add_argument('--baseline', required=True, type=Path, action='append',
                    help='baseline file; repeatable -- entries are unioned. '
                         'The cross-platform smoke passes the shared packaging '
                         'baseline plus a per-OS delta file.')
    ap.add_argument('--allow-source', action='store_true',
                    help='permit the package to resolve from outside sys.prefix '
                         '(editable installs / dev trees)')
    args = ap.parse_args()

    pkg = importlib.import_module(args.package)
    origin = Path(pkg.__file__ or '').resolve()
    print(f'{args.package} resolved from: {origin}')
    if not args.allow_source and sys.prefix == sys.base_prefix:
        print('not running inside a venv -- refusing: the point is to test '
              'an installed artifact, not whatever the system python sees',
              file=sys.stderr)
        return 2
    if not args.allow_source and Path(sys.prefix).resolve() not in origin.parents:
        print(f'FAIL: {args.package} resolved from outside this environment '
              f'({origin}); the installed artifact is not what is being '
              'tested. Pass --allow-source only for editable/dev runs.',
              file=sys.stderr)
        return 2

    exact, prefixes = set(), set()
    for baseline in args.baseline:
        file_exact, file_prefixes = load_baseline(baseline)
        exact |= file_exact
        prefixes |= file_prefixes

    modules = [m.name for m in
               pkgutil.walk_packages(pkg.__path__, prefix=f'{args.package}.')]
    failures: dict[str, str] = {}
    for name in modules:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001 - anything import-time is a failure
            failures[name] = f'{type(exc).__name__}: {exc}'

    def covered(name: str) -> bool:
        return name in exact or any(name.startswith(p) for p in prefixes)

    unexpected = {n: e for n, e in failures.items() if not covered(n)}
    stale_exact = {e for e in exact if e not in failures}
    stale_prefixes = {p for p in prefixes
                      if not any(n.startswith(p) for n in failures)}

    known = len(failures) - len(unexpected)
    print(f'{len(modules)} modules walked: {len(failures)} import failures '
          f'({known} covered by baseline, {len(unexpected)} unexpected)')

    for name, err in sorted(unexpected.items()):
        print(f'  BAD new import failure: {name}\n      {err[:160]}')
    for name in sorted(stale_exact):
        print(f'  BAD stale baseline entry (imports cleanly now): {name}')
    for p in sorted(stale_prefixes):
        print(f'  BAD stale baseline prefix (matches no failure): {p}*')

    if unexpected or stale_exact or stale_prefixes:
        print('\nFAIL: the installed package and the baseline disagree.\n'
              '      A NEW failure means the wheel lost a module, a data\n'
              '      file, or a declared dependency -- fix the packaging.\n'
              '      A STALE entry means something got fixed -- delete its\n'
              '      line so the ratchet locks the improvement in.',
              file=sys.stderr)
        return 1

    print('All import failures reconciled against the baseline.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

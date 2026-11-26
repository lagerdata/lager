#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Changelog entries live in changelog.d/ as one file per change, until a release.

Every branch used to add its bullet to the same `## [Unreleased]` block in
CHANGELOG.md, so any two open branches conflicted there, and each merge sent
every other branch back to rebase. A release also renamed that heading, so a
branch cut before it and merged after it filed its entry inside the released
version, with no conflict to say so. A new file per change has neither
problem: two new files never conflict, and a fragment cannot enter a version
section until the release puts it there.

Fragment file:  changelog.d/<name>.<type>.md

  <name>  lowercase letters, digits, '.', '_' and '-'; the branch name without
          its prefix is a good choice (`ftdi-usb-cache-flush`)
  <type>  added | changed | deprecated | removed | fixed | security

The body is one or more Markdown bullets ("- ..."), written as they will
appear in CHANGELOG.md. No headings.

Usage:
  python tools/changelog_fragments.py --check
      Validate every fragment, and fail if CHANGELOG.md's Unreleased section
      holds an entry (it must stay empty; entries go in changelog.d/).
  python tools/changelog_fragments.py --preview
      Print the section a release would write now. Writes nothing.
  python tools/changelog_fragments.py --assemble X.Y.Z [--date YYYY-MM-DD]
      Release only. Write `## [X.Y.Z] - date` under the empty Unreleased
      section and delete the fragments. Prints the deleted paths; stage them
      with the CHANGELOG.md change.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / 'CHANGELOG.md'
FRAGMENT_DIR = REPO_ROOT / 'changelog.d'

# Keep a Changelog order. The release writes the sections in this order.
TYPES = ('added', 'changed', 'deprecated', 'removed', 'fixed', 'security')

NAME = re.compile(r'^[a-z0-9][a-z0-9._-]*\.(' + '|'.join(TYPES) + r')\.md$')
VERSION = re.compile(r'^\d+\.\d+\.\d+$')
UNRELEASED = '## [Unreleased]'

# Files in changelog.d/ that are not fragments.
NOT_FRAGMENTS = {'README.md', '.gitkeep'}


def fragment_files() -> list[Path]:
    if not FRAGMENT_DIR.is_dir():
        return []
    return sorted(p for p in FRAGMENT_DIR.iterdir()
                  if p.is_file() and p.name not in NOT_FRAGMENTS)


def check_fragment(path: Path) -> list[str]:
    """Problems with one fragment, as messages. Empty means it is fine."""
    m = NAME.match(path.name)
    if not m:
        return [f'{path.name}: name must be <name>.<type>.md, with <type> one of '
                f'{", ".join(TYPES)} and <name> in lowercase letters, digits, '
                f'".", "_" or "-"']
    text = path.read_text().strip()
    if not text:
        return [f'{path.name}: empty']
    problems = []
    if not text.startswith('- '):
        problems.append(f'{path.name}: must start with a Markdown bullet ("- ")')
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith('#'):
            problems.append(f'{path.name}:{n}: no headings; the release adds them')
        if line.strip() in ('<<<<<<<', '=======', '>>>>>>>') or \
                re.match(r'^(<{7}|>{7}) ', line):
            problems.append(f'{path.name}:{n}: merge-conflict marker')
    return problems


def unreleased_span(lines: list[str]) -> tuple[int, int]:
    """(index of the Unreleased heading, index of the next `## ` heading)."""
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == UNRELEASED)
    except StopIteration:
        raise SystemExit(f'CHANGELOG.md has no "{UNRELEASED}" heading') from None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith('## ')), len(lines))
    return start, end


def unreleased_entries(lines: list[str]) -> list[str]:
    """Lines in the Unreleased section that are entries or section headings.

    HTML comments and blank lines are allowed; anything else is an entry
    written in the wrong place.
    """
    start, end = unreleased_span(lines)
    found, in_comment = [], False
    for ln in lines[start + 1:end]:
        s = ln.strip()
        if in_comment:
            in_comment = '-->' not in s
            continue
        if s.startswith('<!--'):
            in_comment = '-->' not in s
            continue
        if s:
            found.append(ln)
    return found


def render_section(fragments: list[Path]) -> str:
    """The body of a release section, without its `## ` heading."""
    by_type: dict[str, list[str]] = {t: [] for t in TYPES}
    for p in fragments:
        m = NAME.match(p.name)
        assert m, f'{p.name} was not validated before rendering'
        by_type[m.group(1)].append(p.read_text().strip())
    parts = []
    for t in TYPES:
        if by_type[t]:
            parts.append(f'### {t.capitalize()}\n\n' + '\n\n'.join(by_type[t]))
    return '\n\n'.join(parts)


def cmd_check() -> int:
    problems = []
    for p in fragment_files():
        problems.extend(check_fragment(p))
    stray = unreleased_entries(CHANGELOG.read_text().splitlines())
    if stray:
        problems.append(
            'CHANGELOG.md: the Unreleased section must stay empty. Move these '
            'lines into a file under changelog.d/ (see changelog.d/README.md):\n'
            + '\n'.join(f'    {ln}' for ln in stray[:10])
            + ('\n    ...' if len(stray) > 10 else ''))
    if problems:
        print('FAIL: changelog fragments', file=sys.stderr)
        for msg in problems:
            print(f'  {msg}', file=sys.stderr)
        return 1
    print(f'changelog.d: {len(fragment_files())} fragment(s) OK; '
          f'Unreleased section empty.')
    return 0


def cmd_preview() -> int:
    frags = fragment_files()
    if not frags:
        print('No fragments in changelog.d/.')
        return 0
    rc = cmd_check()
    print()
    print(render_section(frags))
    return rc


def cmd_assemble(version: str, date: str) -> int:
    if not VERSION.match(version):
        print(f'FAIL: version must be X.Y.Z, got {version!r}', file=sys.stderr)
        return 1
    if cmd_check() != 0:
        return 1
    frags = fragment_files()
    if not frags:
        print('FAIL: no fragments in changelog.d/, so nothing to release.',
              file=sys.stderr)
        return 1
    text = CHANGELOG.read_text()
    if re.search(rf'^## \[{re.escape(version)}\]', text, re.M):
        print(f'FAIL: CHANGELOG.md already has a {version} section.', file=sys.stderr)
        return 1

    lines = text.splitlines()
    _, end = unreleased_span(lines)
    # Keep the Unreleased heading and its comment; drop trailing blanks so the
    # new section sits one blank line below.
    head = lines[:end]
    while head and not head[-1].strip():
        head.pop()
    section = f'## [{version}] - {date}\n\n{render_section(frags)}'
    new = '\n'.join(head) + '\n\n' + section + '\n\n' + '\n'.join(lines[end:])
    CHANGELOG.write_text(new.rstrip('\n') + '\n')

    for p in frags:
        p.unlink()
    print(f'Wrote ## [{version}] - {date} from {len(frags)} fragment(s).')
    print('Deleted (stage these with CHANGELOG.md):')
    for p in frags:
        print(f'  {p.relative_to(REPO_ROOT)}')
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='Check, preview or assemble the changelog.d/ fragments.')
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true',
                      help='validate fragments and the empty Unreleased section')
    mode.add_argument('--preview', action='store_true',
                      help='print the section a release would write now')
    mode.add_argument('--assemble', metavar='X.Y.Z',
                      help='release only: write the version section, delete fragments')
    ap.add_argument('--date', default=datetime.date.today().isoformat(),
                    help='release date for --assemble (default: today)')
    args = ap.parse_args(argv)
    if args.check:
        return cmd_check()
    if args.preview:
        return cmd_preview()
    return cmd_assemble(args.assemble, args.date)


if __name__ == '__main__':
    raise SystemExit(main())

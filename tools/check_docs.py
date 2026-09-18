#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Assert that docs/ still describes the CLI that actually ships.

The published docs drifted for a long time with nothing to catch it, because
every check here is one a human only performs when they happen to look:

  * `lager update` documented --all, --skip-restart and --check-jlink for
    eighteen releases after --all and --skip-restart were removed in v0.18.2,
    including a walkthrough with invented sample output.
  * `lager arm` documented --x/--y/--z as positional arguments, so every
    motion example on the page was un-runnable.
  * `--json` shipped on seven commands and appeared on no page.
  * NetType.Router -- 39 methods, the bench's only network fault-injection
    tooling -- appeared nowhere in the docs at all.

A docs bug is invisible in a way a code bug is not: nothing fails, a reader
just runs a command that does not work and concludes the tool is broken. So
these are gates, not advice.

WHAT IS CHECKED

  nav       every docs.json page exists on disk, and every .mdx under
            docs/source is reachable from docs.json (an unlisted page keeps
            building, so it is reachable by URL and by search while being
            invisible in the navigation)
  unlisted  every .md/.mdx under docs/ is either in docs.json or excluded by
            .mintignore -- Mintlify builds the whole directory, so a file
            nobody listed is a published page nobody reviewed
  notes     every CHANGELOG version has a release-notes page and every page
            has a CHANGELOG entry, counting both CHANGELOG.md and its
            docs/changelog/ archive; no version appears twice; CHANGELOG.md
            stays under CHANGELOG_MAX_BYTES
  commands  every non-hidden top-level click command has a docs page, or an
            explicit entry in DEPRECATED_ALIASES below
  flags     no page names a --flag that no click param anywhere declares

WHAT IS NOT CHECKED, AND WHY

Whether prose is *correct* -- that a described behavior matches the code. No
checker can do that. `--check-jlink` was catchable because it named a flag
that does not exist; "the tool surface is read-only" was not, and needed a
human reading the MCP server's gate logic.

THE FOUR TRAPS

Each of these produced a false finding while this file was being written, and
each is why the naive version of this check would cry wolf and get switched
off within a week:

  1. LAZY GROUPS. `lager debug` and 21 other groups resolve subcommands on
     demand, so `Group.commands` reads empty and every subcommand looks
     undocumented. Walk with list_commands()/get_command() instead.

  2. SECONDARY OPTS. click stores the `--no-x` half of a boolean flag pair in
     `param.secondary_opts`, not `param.opts`. Reading only `.opts` reports
     every documented `--no-warn`, `--no-cache`, `--no-halt` as nonexistent.

  3. HIDDEN OPTIONS. `lager python --org` is `hidden=True`: deliberately
     undocumented. Flagging it as a docs gap argues for publishing something
     the author chose to conceal. Hidden params and hidden commands are
     skipped on both sides of the comparison.

  4. FLAGS BELONGING TO OTHER TOOLS. Docs quote pytest, pip, docker, cargo and
     nrfjprog. `pip install --upgrade lager-cli` does not mean lager has an
     --upgrade flag. Fenced blocks are scanned line by line and a line is only
     read for lager flags when it invokes `lager`. Markdown anchors
     (`#channel-role-constraints`) are stripped too -- they are not flags.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / 'docs'
MINTIGNORE = DOCS / '.mintignore'
DOCS_JSON = DOCS / 'docs.json'
SOURCE = DOCS / 'source'
CHANGELOG = REPO / 'CHANGELOG.md'
CHANGELOG_ARCHIVE = DOCS / 'changelog'

# Commands that intentionally have no page. A deprecated alias should not be
# advertised: documenting it teaches the spelling we are trying to retire.
# Keep the reason -- an unexplained entry here becomes a place to silence the
# checker rather than fix the docs.
DEPRECATED_ALIASES = {
    'authorize': 'deprecated alias for `lager ssh-setup`',
    'box': 'deprecated aliases for `lager box-config` and `lager dut`',
}

# Pages under reference/cli/ that describe a concept rather than one command.
CONCEPT_PAGES = {'overview', 'lager-file', 'locking'}

# CHANGELOG versions deliberately without a release-notes page. 0.13.1 predates
# the current process and nobody now remembers whether it shipped; writing notes
# for a release that may never have existed would be worse than the gap. Every
# version after this point is expected to have a page -- that is the whole point
# of the check, so add to this set only with a reason, never to quiet a miss.
NOTES_EXEMPT = {'0.13.1'}

# GitHub stops rendering a Markdown file at about 512 KB, and README links
# CHANGELOG.md as the release history. At 0.40-0.47's pace that ceiling is weeks
# away, not years. When CHANGELOG.md crosses this line, move the oldest ten-minor
# block of releases (for example 0.40-0.49) into docs/changelog/ unchanged. The
# notes check reads the archive too, so no version drops out of it.
CHANGELOG_MAX_BYTES = 400_000

# Page stem -> command name, where they differ. Empty since the thermocouple page
# was renamed off its old `tc` stem; kept because a page whose filename does not
# match its command is a thing that recurs, and this is where it is declared.
PAGE_TO_COMMAND: dict[str, str] = {}

# Commands documented inside another command's page rather than their own.
DOCUMENTED_WITHIN = {
    'logout': 'login',
    'whoami': 'login',
}

# Flags that appear in docs and are real, but belong to the global CLI group
# rather than to any subcommand.
ROOT_FLAGS = {'--help', '--version', '--debug', '--colorize', '--interpreter'}


def load_cli_tree():
    """Walk the live click tree. Returns (all_flags, visible, hidden).

    Imports the CLI rather than parsing it: decorators, dynamic groups and
    conditional registration make the source text an unreliable description of
    the command surface (trap 1).

    Hidden *commands* are walked for their flags even though they are reported
    separately, because a hidden command with a published page (see the
    `hidden` check) would otherwise make every flag on that page look invented
    -- four confusing errors in place of the one true one. Hidden *options* are
    still skipped: that is trap 3, and it is a different thing.
    """
    sys.path.insert(0, str(REPO))
    import click  # noqa: E402
    from cli.main import cli as root  # noqa: E402

    flags: set[str] = set()
    ctx = click.Context(root)

    def walk(cmd, ctx, depth=0):
        for param in cmd.params:
            if not isinstance(param, click.Option):
                continue
            if getattr(param, 'hidden', False):
                continue                                  # trap 3
            flags.update(param.opts)
            flags.update(param.secondary_opts)            # trap 2
        # Duck-typed, not isinstance(click.Group): a custom class may inherit
        # click.MultiCommand directly, and MultiCommand is Group's base, not
        # its subclass -- so an isinstance check silently skips such a group.
        if hasattr(cmd, 'list_commands') and depth < 4:
            for name in cmd.list_commands(ctx):           # trap 1
                sub = cmd.get_command(ctx, name)
                if sub is not None:
                    walk(sub, click.Context(sub, parent=ctx), depth + 1)

    visible, hidden = {}, {}
    for name in root.list_commands(ctx):
        cmd = root.get_command(ctx, name)
        if cmd is None:
            continue
        (hidden if getattr(cmd, 'hidden', False) else visible)[name] = cmd
        walk(cmd, click.Context(cmd, parent=ctx))

    flags.update(ROOT_FLAGS)
    return flags, visible, hidden


def mintignore_patterns():
    """The non-comment rules in docs/.mintignore, as written.

    Nothing else in the repository reads this file, and no tool validated it,
    which is how it came to list one directory while four pages it was assumed
    to cover were live on the site.
    """
    if not MINTIGNORE.exists():
        return []
    rules = []
    for line in MINTIGNORE.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            rules.append(line)
    return rules


def _covered_by(rel, pattern):
    """Whether `rel` (a path under docs/) is excluded by one .mintignore rule.

    Deliberately simple: a leading slash anchors the rule to docs/, a trailing
    slash means a directory, and anything else is an exact path or a glob. A
    rule this does not understand is reported as NOT covering the file, so the
    result is a page named for review rather than one silently exempted.

    The leading slash is the load-bearing part. Unanchored, `reference/` also
    matches docs/source/reference/ -- the published CLI, Python, Rust and MCP
    reference -- and unpublishes a few hundred pages while this check stays
    green, because they are excluded rather than unlisted. Every rule in the
    file is anchored for that reason.
    """
    text = str(rel)
    if pattern.startswith('/'):
        anchored, pattern = True, pattern[1:]
    else:
        anchored = False
    if pattern.endswith('/'):
        if anchored:
            return text.startswith(pattern)
        return text.startswith(pattern) or f'/{pattern}' in text
    if anchored:
        # fnmatch, not Path.match: Path.match anchors at the RIGHT, so
        # `/STYLE.md` would also match source/getting-started/STYLE.md, which
        # is the opposite of what a leading slash asks for.
        return fnmatch.fnmatchcase(text, pattern)
    return text == pattern or rel.match(pattern)


def nav_pages():
    nav = json.loads(DOCS_JSON.read_text())
    pages: list[str] = []

    def collect(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'pages':
                    for page in value:
                        if isinstance(page, str):
                            pages.append(page)
                        else:
                            collect(page)
                else:
                    collect(value)
        elif isinstance(node, list):
            for item in node:
                collect(item)

    collect(nav['navigation'])
    return pages


# A page *declares* an option in one of two forms, and nowhere else:
#
#     | `--json` | Emit a machine-readable JSON object |      <- table row
#     - `--json` - Emit a machine-readable JSON object        <- options bullet
#
# Everything else -- prose, examples, REPL transcripts -- is quotation, and is
# where other tools' flags legitimately appear. `pip install --upgrade
# lager-cli` and `lager devenv add test "pytest tests/ --tb=short"` both name a
# foreign flag on a line that also says "lager", so no amount of line-level
# heuristics separates them (trap 4). Restricting the check to declaration
# sites removes that whole class: an option table is an assertion that lager
# accepts the flag, and that assertion is checkable.
#
# The cost is real and worth stating: a broken flag that appears *only* in an
# example is not caught here. `--check-jlink` was in a table and would be; a
# stray `--hexfile` in a REPL transcript would not. Examples need a human, or
# a future check that executes them.
_DECLARATION = re.compile(r'^\s*(?:\|\s*|[-*]\s+)`(--[a-zA-Z0-9][a-zA-Z0-9-]*)')


def lager_flags_in(path: Path) -> set[str]:
    """Flags the page declares -- in an option table or an options bullet list.

    A declaration row may pair two spellings (`--warn / --no-warn`,
    `--verbose` / `-v`), so every flag token on a matched line is taken.
    """
    found: set[str] = set()
    in_fence = False
    for line in path.read_text().split('\n'):
        if line.lstrip().startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence or not _DECLARATION.match(line):
            continue
        found.update(re.findall(r'--[a-zA-Z0-9][a-zA-Z0-9-]*', line))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Assert that docs/ still describes the CLI that actually ships.')
    parser.add_argument('--only',
                        choices=['nav', 'unlisted', 'notes', 'commands', 'flags'],
                        help='run a single check')
    args = parser.parse_args()

    failures: list[str] = []

    def run(name: str) -> bool:
        return args.only in (None, name)

    if run('nav'):
        pages = nav_pages()
        # A leading underscore marks a partial: a page that exists to be copied
        # or included, never to be published. Mintlify's own convention, and the
        # only case where "not in docs.json" is the intent rather than the bug.
        on_disk = {str(p.relative_to(DOCS)).removesuffix('.mdx')
                   for p in SOURCE.rglob('*.mdx')
                   if not p.name.startswith('_')}
        missing = sorted(set(pages) - on_disk)
        orphans = sorted(on_disk - set(pages))
        for page in missing:
            failures.append(f'nav: docs.json lists "{page}", which is not on disk')
        for page in orphans:
            failures.append(
                f'nav: {page}.mdx is not in docs.json, so it is published but '
                f'unreachable from the navigation')
        print(f'  nav       {len(pages)} nav entries, {len(on_disk)} files on disk')

    if run('unlisted'):
        # The gap the nav check above cannot see. It globs *.mdx under
        # docs/source only, so it is blind to .md and to everything beside
        # source/ -- and Mintlify builds every .md and .mdx under docs/.
        # docs/STYLE.md, docs/TRANSLATION.md and two files under
        # docs/reference/ were all live on the site, each returning 200, while
        # a CI comment described them as never published.
        #
        # A page is fine if it is in the navigation, or if .mintignore excludes
        # it. Anything else is published prose nobody decided to publish.
        ignored = mintignore_patterns()
        listed = set(nav_pages())
        unlisted = []
        for path in sorted(DOCS.rglob('*.md')) + sorted(DOCS.rglob('*.mdx')):
            rel = path.relative_to(DOCS)
            # Mintlify skips README.md, and a leading underscore marks a
            # partial that exists to be included rather than published.
            if path.name == 'README.md' or path.name.startswith('_'):
                continue
            if any(_covered_by(rel, pattern) for pattern in ignored):
                continue
            if str(rel).removesuffix('.mdx').removesuffix('.md') in listed:
                continue
            unlisted.append(str(rel))
        for rel in unlisted:
            failures.append(
                f'unlisted: docs/{rel} is neither in docs.json nor excluded by '
                f'.mintignore, so it publishes as a page nobody listed')
        print(f'  unlisted  {len(ignored)} .mintignore rule(s), '
              f'{len(unlisted)} unlisted file(s)')

    if run('notes'):
        # A rollover that copies instead of moves leaves a version in both files;
        # a set would hide that, so track where each version was first seen.
        changelogs = [CHANGELOG, *sorted(CHANGELOG_ARCHIVE.glob('*.md'))]
        first_seen: dict[str, Path] = {}
        for changelog in changelogs:
            for version in re.findall(r'^## \[(\d+\.\d+\.\d+)\]', changelog.read_text(), re.M):
                if version in first_seen:
                    failures.append(f'notes: {version} is in {first_seen[version].relative_to(REPO)} '
                                    f'and again in {changelog.relative_to(REPO)}')
                first_seen.setdefault(version, changelog)
        versions = set(first_seen)
        noted = {p.stem.lstrip('v') for p in (SOURCE / 'release-notes').glob('*.mdx')
                 if not p.name.startswith('_')}
        gaps = sorted(versions - noted - NOTES_EXEMPT,
                      key=lambda v: tuple(int(x) for x in v.split('.')))
        for version in gaps:
            failures.append(f'notes: CHANGELOG has {version} with no release-notes page')
        # The reverse direction is what catches a rollover that deletes a block
        # from CHANGELOG.md without writing it to docs/changelog/.
        unlogged = sorted(noted - versions,
                          key=lambda v: tuple(int(x) for x in v.split('.')))
        for version in unlogged:
            failures.append(f'notes: release-notes page v{version} has no CHANGELOG entry')
        size = CHANGELOG.stat().st_size
        if size > CHANGELOG_MAX_BYTES:
            failures.append(f'notes: CHANGELOG.md is {size:,} bytes, over {CHANGELOG_MAX_BYTES:,}; '
                            'move the oldest ten-minor block of releases into docs/changelog/')
        print(f'  notes     {len(versions)} CHANGELOG versions across {len(changelogs)} file(s), '
              f'{len(noted)} release-notes pages, {len(NOTES_EXEMPT)} exempt; '
              f'CHANGELOG.md is {size // 1024} KB')

    cli_flags: set[str] = set()
    visible: dict = {}
    hidden: dict = {}
    if run('commands') or run('flags'):
        cli_flags, visible, hidden = load_cli_tree()

    if run('commands'):
        documented = {p.stem for p in (SOURCE / 'reference' / 'cli').glob('*.mdx')}
        # A page whose name differs from the command it documents (tc -> thermocouple).
        for page_stem, command in PAGE_TO_COMMAND.items():
            if page_stem in documented:
                documented.add(command)
        # A command documented inside another command's page (logout, whoami in login).
        for command, host_page in DOCUMENTED_WITHIN.items():
            if host_page in documented:
                documented.add(command)
        for name in sorted(visible):
            if name in documented or name in DEPRECATED_ALIASES:
                continue
            failures.append(f'commands: `lager {name}` ships but has no docs page '
                            f'(add one, or list it in DEPRECATED_ALIASES with a reason)')
        # The mirror image, and the easier one to miss: a command marked
        # hidden=True does not appear in `lager --help`, so publishing a page
        # for it advertises something the CLI is deliberately concealing. One
        # of the two is wrong; which one is a judgement call, so say so rather
        # than guess.
        for name in sorted(set(hidden) & documented):
            failures.append(
                f'commands: `lager {name}` is hidden=True but reference/cli/{name}.mdx '
                f'is published -- either drop the hidden flag or unpublish the page')
        print(f'  commands  {len(visible)} visible, {len(hidden)} hidden, '
              f'{len(DEPRECATED_ALIASES)} documented aliases')

    if run('flags'):
        checked = 0
        for path in sorted(SOURCE.rglob('*.mdx')):
            if 'release-notes' in path.parts:
                continue        # historical record: describes past releases on purpose
            checked += 1
            for flag in sorted(lager_flags_in(path) - cli_flags):
                rel = path.relative_to(SOURCE)
                failures.append(f'flags: {rel} names {flag}, which no lager command declares')
        print(f'  flags     {checked} pages checked against the live click tree')

    if not failures:
        print('\ndocs match the shipping CLI.')
        return 0

    print(f'\nFAIL: {len(failures)} problem(s).\n', file=sys.stderr)
    for failure in failures:
        print(f'  {failure}', file=sys.stderr)
    print('\n      A stale flag or a missing page is a defect: the reader runs the\n'
          '      command and it does not work. Fix the docs, or -- if a command is\n'
          '      deliberately undocumented -- record why in tools/check_docs.py.',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

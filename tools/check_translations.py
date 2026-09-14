#!/usr/bin/env python3
"""Hold each translated page to the English page it was translated from.

A translation is a copy, and a copy goes stale in silence. Nothing about
`docs/source/zh/nets.mdx` changes when someone edits `docs/source/nets.mdx`, so
the Chinese page keeps describing the old flag, the old default, the old wiring
-- and it keeps describing it confidently. There is no broken link to find and
no build to fail. The reader follows an instruction that stopped being true.

Mintlify sells an automation that re-translates on push. This repository is on
the free plan and translates in-repo instead, so the resync has to be a gate.

The gate is a hash. `docs/translations.json` records, for every translated page,
the SHA-256 of the English file as it read when the translation was made. If the
English file no longer hashes to that value, the English changed and the
translation is behind. That is the entire mechanism, and it is deliberately
blunt: it cannot tell a reworded warning from a fixed typo, so it reports both
and a human decides. Blunt and honest beats clever and quiet -- an exemption
list is how a checker learns to pass while the docs rot.

A hash catches a translation that went stale. It cannot see one that arrived
incomplete -- the English page it was made from never changed, so the hash still
matches while a paragraph, a table row or a whole subcommand is simply absent.
`--completeness` covers that by comparing structure rather than prose; see the
comment above the counters for why structure is the only thing that compares.

Two rules beyond staleness, both there to stop a page escaping the gate:

  * A translated page with no manifest entry is UNTRACKED. It renders, it
    publishes, and nothing will ever tell you it drifted. A translation the
    gate cannot see is worse than no translation.
  * A manifest entry whose English page is gone is ORPHANED -- the English page
    was renamed or deleted, and the translation now documents a page that does
    not exist.

Paths mirror: `source/<path>.mdx` translates to `source/<lang>/<path>.mdx`. The
mapping is derived, never configured, so a translation cannot be filed under a
name its source does not have.

Usage:
    python tools/check_translations.py                 # the gate: all three checks
    python tools/check_translations.py --completeness  # structure only
    python tools/check_translations.py --progress      # coverage, per section
    python tools/check_translations.py --record PATH   # stamp one page
    python tools/check_translations.py --record-all    # stamp every page on disk
    python tools/check_translations.py --relink        # fix cross-references
    python tools/check_translations.py --reflow        # unwrap Chinese paragraphs
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / 'docs'
SOURCE = DOCS / 'source'
MANIFEST = DOCS / 'translations.json'

# Every language directory under docs/source/. A directory here is a
# translation root; a directory absent from here is ordinary English content.
LANGUAGES = ['zh']

# Release notes are a dated historical record. A note says what shipped on a
# day, and the archive's whole value is that it still says it. STYLE.md exempts
# the directory from rewriting for that reason, and the same reason exempts it
# from translation: nobody reads a 2024 release note to learn how to use the
# tool, and translating 166 of them would cost more than every page a new user
# actually opens.
NOT_TRANSLATED = {'release-notes'}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def english_pages() -> list[Path]:
    """Every English page a translation could be made from.

    A leading underscore marks a partial -- copied into other pages, never
    published on its own. Mintlify's convention, and check_docs.py honors it
    in the same way.
    """
    pages = []
    for path in sorted(SOURCE.rglob('*.mdx')):
        rel = path.relative_to(SOURCE)
        if rel.parts[0] in LANGUAGES or rel.parts[0] in NOT_TRANSLATED:
            continue
        if path.name.startswith('_'):
            continue
        pages.append(path)
    return pages


def translated_pages(lang: str) -> list[Path]:
    root = SOURCE / lang
    if not root.exists():
        return []
    return sorted(p for p in root.rglob('*.mdx') if not p.name.startswith('_'))


def to_english(translated: Path, lang: str) -> Path:
    """source/<lang>/a/b.mdx -> source/a/b.mdx"""
    return SOURCE / translated.relative_to(SOURCE / lang)


def to_translated(english: Path, lang: str) -> Path:
    """source/a/b.mdx -> source/<lang>/a/b.mdx"""
    return SOURCE / lang / english.relative_to(SOURCE)


def load_manifest() -> dict:
    if not MANIFEST.exists():
        return {}
    return json.loads(MANIFEST.read_text())


def save_manifest(manifest: dict) -> None:
    ordered = {lang: dict(sorted(pages.items())) for lang, pages in sorted(manifest.items())}
    MANIFEST.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + '\n')


def record(manifest: dict, english: Path, lang: str) -> None:
    key = str(english.relative_to(SOURCE))
    manifest.setdefault(lang, {})[key] = sha256(english)


def relink(lang: str) -> int:
    """Point every cross-reference in a translated page at the right language.

    A translated page links to its siblings. While a tab is half done, some of
    those siblings exist in this language and some do not, and the correct
    target changes as pages land. Doing it by hand produces two failures that
    look nothing alike: a link to a page that does not exist yet (the
    broken-links gate catches it, loudly) and a link left pointing at English
    after the translation landed (nothing catches it -- the reader silently
    falls out of their language and has no way to know they were supposed to
    stay in it).

    The rule has no judgement in it, so it belongs in a tool: if the
    translation exists, link to the translation; otherwise link to the English
    page. Idempotent, and correct at any point in a partial translation.
    """
    changed = 0
    anchors: list[str] = []
    for path in translated_pages(lang):
        text = original = path.read_text()

        def absolute(match):
            page = match.group(1)
            return (f'(/source/{lang}/{page}' if (SOURCE / lang / f'{page}.mdx').exists()
                    else f'(/source/{page}')

        text = re.sub(rf'\(/source/(?:{lang}/)?([A-Za-z0-9\-_/]+)', absolute, text)

        # Relative links (./sibling, ../group/page) resolve against the page's
        # own directory, so inside a translation they already point at the
        # translation -- correct once that sibling exists, a broken link until
        # then. Leave them relative when the sibling is translated; rewrite to
        # the absolute English path when it is not.
        def relative(match):
            rel = (path.parent / match.group(1)).resolve()
            if rel.with_suffix('.mdx').exists():
                return match.group(0)
            english = SOURCE / rel.relative_to(SOURCE / lang)
            return f'](/source/{english.relative_to(SOURCE)}'

        text = re.sub(r'\]\((\.{1,2}/[A-Za-z0-9\-_./]+)', relative, text)

        # An anchor does not survive the path rewrite. A link to
        # `/source/reference/cli/arm#detection` becomes a link to the
        # translation, but `#detection` is the slug of the *English* heading and
        # the translated page has `#检测` instead. The tool cannot translate the
        # fragment -- only the person who wrote the heading knows what it became
        # -- so it reports the link and leaves it for a human. The broken-links
        # gate catches these too, but only after the target is translated; the
        # report says which ones to look at now.
        for m in re.finditer(rf'\]\(/source/{lang}/([A-Za-z0-9\-_/]+)#([^)]+)\)', text):
            target, frag = m.group(1), m.group(2)
            if not re.fullmatch(r'[a-z0-9\-]+', frag):
                continue
            # An all-ASCII fragment is not wrong by itself. A command name stays
            # English in a translated heading -- `### tui`, `### assign` -- and
            # its slug is ASCII and correct. Only report a fragment that no
            # heading in the target actually produces. A report that fires on
            # correct links is a report people learn to skip.
            target_file = SOURCE / lang / f'{target}.mdx'
            if not target_file.exists():
                continue
            slugs = {re.sub(r'[^a-z0-9]+', '-', h.lower()).strip('-')
                     for h in re.findall(r'^#{2,6}\s+(.+?)\s*$',
                                         target_file.read_text(), re.M)}
            if frag not in slugs:
                anchors.append(f'{path.relative_to(DOCS)} -> {target}#{frag}')
        if text != original:
            path.write_text(text)
            changed += 1
    for a in anchors:
        print(f'  check anchor (looks like an English slug): {a}')
    return changed


# Structure a translation has to keep, whatever language it is written in.
#
# The hash gate catches a translation that went *stale*. Nothing caught one
# that arrived *incomplete*, and the two look identical from outside: a page
# that renders, publishes and reads perfectly well. Incomplete is the worse of
# the two, because a stale page is wrong in a way the reader can eventually
# notice, while a page that is missing a paragraph has no gap on it. Seven
# pages lost content this way and every gate passed.
#
# Prose cannot be compared across languages. Chinese says the same thing in
# markedly fewer characters, so any length or word comparison fires on every
# page and is worth nothing. Scaffolding can be compared: a bullet is a bullet
# in both languages, a table row carries one row of facts in both, a fenced
# block holds the same command, and `## heading` opens the same section. Count
# those and leave the words alone. If English has 22 bullets and the
# translation has 19, three bullets of content are gone.
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
HEADING = re.compile(r'^#{2,6}\s+\S')
BULLET = re.compile(r'^\s*[-*+]\s+\S')
NUMBERED = re.compile(r'^\s*\d{1,9}[.)]\s+\S')
TABLE_ROW = re.compile(r'^\s*\|')

# Counted separately rather than as one "list items" total, because a total
# hides a swap: drop a numbered step, add a bullet, and the sum still matches.
STRUCTURE = ('bullets', 'numbered items', 'headings', 'code fences', 'table rows')

# The characters a line break renders a stray space between: CJK punctuation,
# the ideographs themselves, and the full-width forms.
CJK = re.compile(r'[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]')

# A line that opens a block of its own never joins to the line above it.
BLOCK_START = re.compile(r'^\s*(\||#{1,6}\s|[-*+]\s|\d{1,9}[.)]\s|<|>|`{3,}|~{3,})')


def body(lines: list[str]) -> int:
    """Index of the first line of content. Frontmatter is metadata, not prose."""
    if not lines or lines[0].strip() != '---':
        return 0
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            return i + 1
    return 0


def fenced(lines: list[str]) -> tuple[list[bool], int, bool]:
    """Mark every line inside a fenced code block.

    Returns the mask, how many blocks were opened, and whether one was left
    open at the end of the file. Everything else here depends on this: a
    bullet in a shell sample is not a list item and a pipe in an ASCII table
    is not a table row, and counting them is exactly how a structural check
    learns to fire on pages that are perfectly fine.
    """
    inside = [False] * len(lines)
    fence = None
    opened = 0
    for i, line in enumerate(lines):
        match = FENCE.match(line)
        if fence is None:
            if match:
                fence, inside[i] = match.group(1), True
                opened += 1
            continue
        inside[i] = True
        if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
            fence = None
    return inside, opened, fence is not None


def structure(path: Path) -> tuple[dict[str, int], bool]:
    """Count the scaffolding of one page."""
    lines = path.read_text().split('\n')
    inside, fences, unbalanced = fenced(lines)
    counts = dict.fromkeys(STRUCTURE, 0)
    counts['code fences'] = fences
    for i in range(body(lines), len(lines)):
        if inside[i]:
            continue
        line = lines[i]
        if HEADING.match(line):
            counts['headings'] += 1
        elif TABLE_ROW.match(line):
            counts['table rows'] += 1
        elif BULLET.match(line):
            counts['bullets'] += 1
        elif NUMBERED.match(line):
            counts['numbered items'] += 1
    return counts, unbalanced


def completeness() -> list[str]:
    """Report every translation that carries less structure than its English page.

    One-sided on purpose. Fewer bullets than English means content was
    dropped; more means the translator added a clarifying line, which is
    theirs to judge and not a defect a checker can rule on.
    """
    failures = []
    for lang in LANGUAGES:
        for path in translated_pages(lang):
            english = to_english(path, lang)
            if not english.exists():
                continue            # ORPHANED -- check() reports it already
            rel = path.relative_to(DOCS)
            want, want_open = structure(english)
            got, got_open = structure(path)
            # An unclosed fence makes every count after it meaningless, so say
            # which file has it rather than reporting the difference it causes
            # somewhere else. This is how the stray ``` at the end of the
            # English watt.mdx was found -- as a fence count off by one on a
            # translation that was complete.
            for page, is_open in ((f'source/{english.relative_to(SOURCE)}', want_open),
                                  (str(rel), got_open)):
                if is_open:
                    failures.append(f'{lang}: {page} leaves a code fence open at the end '
                                    f'of the file, so its structure cannot be compared')
            if want_open or got_open:
                continue
            short = [f'{name}: en {want[name]} vs {lang} {got[name]}'
                     for name in STRUCTURE if got[name] < want[name]]
            if short:
                failures.append(f'{lang}: {rel} has less content than the English page -- '
                                + '; '.join(short) + ' -- restore what was dropped')
    return failures


def soft_breaks(path: Path) -> list[int]:
    """Line numbers where a paragraph wraps between two CJK characters.

    A newline inside a Markdown paragraph renders as a space. English does not
    care -- its words are space-separated anyway -- so wrapping at 80 columns
    is free there and everyone does it by habit. Chinese has no space between
    characters, so every wrapped line shows up as a gap in the middle of a
    sentence. A Chinese paragraph is therefore written as one long line, and
    this is the easiest mistake in the whole corpus to make by reflex.

    Only a break with CJK on *both* sides is one of these. A break between a
    Chinese character and a Latin word renders exactly the space the style
    guide asks for in that position, so it is correct and stays.
    """
    lines = path.read_text().split('\n')
    inside, _, _ = fenced(lines)
    found = []
    for i in range(body(lines), len(lines) - 1):
        line, nxt = lines[i], lines[i + 1]
        if inside[i] or inside[i + 1] or not line.strip() or not nxt.strip():
            continue
        if line.endswith('  '):
            continue                # two trailing spaces is a deliberate break
        if TABLE_ROW.match(line) or HEADING.match(line) or BLOCK_START.match(nxt):
            continue
        if CJK.match(line.rstrip()[-1]) and CJK.match(nxt.lstrip()[0]):
            found.append(i + 1)
    return found


def reflow(lang: str) -> int:
    """Join every CJK soft break, so each Chinese paragraph is one line."""
    changed = 0
    for path in translated_pages(lang):
        breaks = set(soft_breaks(path))
        if not breaks:
            continue
        lines = path.read_text().split('\n')
        out: list[str] = []
        for number, line in enumerate(lines, 1):
            if out and number - 1 in breaks:
                out[-1] += line.lstrip()
            else:
                out.append(line)
        path.write_text('\n'.join(out))
        changed += 1
    return changed


def wrapped() -> list[str]:
    """Report pages that wrap a Chinese paragraph mid-sentence."""
    failures = []
    for lang in LANGUAGES:
        for path in translated_pages(lang):
            breaks = soft_breaks(path)
            if breaks:
                where = ', '.join(str(b) for b in breaks[:6])
                more = f' and {len(breaks) - 6} more' if len(breaks) > 6 else ''
                failures.append(
                    f'{lang}: {path.relative_to(DOCS)} wraps a Chinese paragraph at line '
                    f'{where}{more}, and each wrap renders as a stray space -- run '
                    f'`python tools/check_translations.py --reflow`')
    return failures


def check(manifest: dict) -> list[str]:
    failures = []
    for lang in LANGUAGES:
        entries = manifest.get(lang, {})
        on_disk = translated_pages(lang)

        for path in on_disk:
            english = to_english(path, lang)
            key = str(english.relative_to(SOURCE))
            rel = path.relative_to(DOCS)
            if key not in entries:
                failures.append(
                    f'{lang}: {rel} has no entry in docs/translations.json, so nothing '
                    f'will report it when the English page changes -- run '
                    f'`python tools/check_translations.py --record docs/{rel}`')
            elif not english.exists():
                failures.append(
                    f'{lang}: {rel} translates source/{key}, which is not on disk -- the '
                    f'English page was renamed or deleted, so move or delete the translation')

        for key, recorded in sorted(entries.items()):
            english = SOURCE / key
            translation = SOURCE / lang / key
            if not translation.exists():
                failures.append(
                    f'{lang}: docs/translations.json records source/{lang}/{key}, '
                    f'which is not on disk')
                continue
            if not english.exists():
                continue        # already reported above, against the translation
            current = sha256(english)
            if current != recorded:
                failures.append(
                    f'{lang}: source/{key} changed since source/{lang}/{key} was '
                    f'translated -- update the translation, then run '
                    f'`python tools/check_translations.py --record docs/source/{key}`')
    return failures


def progress() -> None:
    pages = english_pages()
    sections: dict[str, list[Path]] = {}
    for path in pages:
        sections.setdefault(path.relative_to(SOURCE).parts[0], []).append(path)

    for lang in LANGUAGES:
        done = {to_english(p, lang) for p in translated_pages(lang)}
        print(f'\n{lang}:')
        for section, members in sorted(sections.items()):
            have = sum(1 for m in members if m in done)
            words = sum(len(m.read_text().split()) for m in members if m not in done)
            bar = '#' * round(20 * have / len(members))
            print(f'  {section:<24} {have:>3}/{len(members):<3} {bar:<20} '
                  f'{words:>7,} English words left')
        have = len(done & set(pages))
        left = sum(len(p.read_text().split()) for p in pages if p not in done)
        print(f'  {"TOTAL":<24} {have:>3}/{len(pages):<3} '
              f'{"#" * round(20 * have / len(pages)):<20} {left:>7,} English words left')


def report(failures: list[str], clean: str) -> int:
    if not failures:
        print(f'\n{clean}')
        return 0
    print(f'\nFAIL: {len(failures)} problem(s).\n', file=sys.stderr)
    for failure in failures:
        print(f'  {failure}', file=sys.stderr)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--record', metavar='PATH',
                        help='stamp one page: pass either the English page or its translation')
    parser.add_argument('--record-all', action='store_true',
                        help='stamp every translation currently on disk')
    parser.add_argument('--progress', action='store_true',
                        help='print translation coverage per section')
    parser.add_argument('--relink', action='store_true',
                        help='point cross-references at the translation where one exists, '
                             'and at the English page where one does not')
    parser.add_argument('--completeness', action='store_true',
                        help='compare the structure of each translation with its English '
                             'page, without the hash check')
    parser.add_argument('--reflow', action='store_true',
                        help='join every line break that falls inside a Chinese paragraph')
    args = parser.parse_args()

    manifest = load_manifest()

    if args.record:
        path = Path(args.record).resolve()
        rel = path.relative_to(SOURCE)
        lang = rel.parts[0] if rel.parts[0] in LANGUAGES else None
        for target in ([lang] if lang else LANGUAGES):
            english = to_english(path, target) if lang else path
            if not english.exists():
                print(f'no English page at {english}', file=sys.stderr)
                return 1
            if not to_translated(english, target).exists():
                if lang:
                    print(f'no translation at {to_translated(english, target)}', file=sys.stderr)
                    return 1
                continue
            record(manifest, english, target)
        save_manifest(manifest)
        print(f'recorded {args.record}')
        return 0

    if args.record_all:
        # Rebuild from what is on disk rather than merging into what was there.
        # A merge leaves an entry behind when a translation is deleted, and the
        # only way to clear it is to hand-edit the JSON -- which is the job this
        # tool exists to remove. Report the drops; a pruned entry means a
        # translation went away, and that is worth seeing rather than inferring.
        for lang in LANGUAGES:
            kept = {}
            for path in translated_pages(lang):
                english = to_english(path, lang)
                if english.exists():
                    kept[str(english.relative_to(SOURCE))] = sha256(english)
            for gone in sorted(set(manifest.get(lang, {})) - set(kept)):
                print(f'  pruned {lang}: {gone} (no translation on disk)')
            manifest[lang] = kept
        save_manifest(manifest)
        print(f'recorded {sum(len(v) for v in manifest.values())} translation(s)')
        return 0

    if args.relink:
        for lang in LANGUAGES:
            print(f'{lang}: rewrote links in {relink(lang)} page(s)')
        return 0

    if args.reflow:
        for lang in LANGUAGES:
            print(f'{lang}: unwrapped paragraphs in {reflow(lang)} page(s)')
        return 0

    if args.completeness:
        return report(completeness() + wrapped(),
                      'every translation carries the same structure as its English page.')

    if args.progress:
        progress()
        return 0

    counts = {lang: len(manifest.get(lang, {})) for lang in LANGUAGES}
    total = len(english_pages())
    for lang, n in counts.items():
        print(f'  {lang:<8}  {n}/{total} pages translated and tracked')

    return report(check(manifest) + completeness() + wrapped(),
                  'every translation matches the English page it was made from, '
                  'and carries the same structure.')


if __name__ == '__main__':
    sys.exit(main())

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
    python tools/check_translations.py                 # the gate
    python tools/check_translations.py --progress      # coverage, per section
    python tools/check_translations.py --record PATH   # stamp one page
    python tools/check_translations.py --record-all    # stamp every page on disk
"""

import argparse
import hashlib
import json
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--record', metavar='PATH',
                        help='stamp one page: pass either the English page or its translation')
    parser.add_argument('--record-all', action='store_true',
                        help='stamp every translation currently on disk')
    parser.add_argument('--progress', action='store_true',
                        help='print translation coverage per section')
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
        for lang in LANGUAGES:
            for path in translated_pages(lang):
                english = to_english(path, lang)
                if english.exists():
                    record(manifest, english, lang)
        save_manifest(manifest)
        print(f'recorded {sum(len(v) for v in manifest.values())} translation(s)')
        return 0

    if args.progress:
        progress()
        return 0

    failures = check(manifest)
    counts = {lang: len(manifest.get(lang, {})) for lang in LANGUAGES}
    total = len(english_pages())
    for lang, n in counts.items():
        print(f'  {lang:<8}  {n}/{total} pages translated and tracked')

    if not failures:
        print('\nevery translation matches the English page it was made from.')
        return 0

    print(f'\nFAIL: {len(failures)} problem(s).\n', file=sys.stderr)
    for failure in failures:
        print(f'  {failure}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())

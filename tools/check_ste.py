#!/usr/bin/env python3
# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
"""
Assert that user-facing prose still follows docs/STYLE.md.

`tools/check_docs.py` asks whether the docs describe the CLI that ships. This
file asks a different question: whether they say it the one way we have agreed
to say it. Both failures are invisible -- nothing goes red, a reader just works
harder than they needed to, and a reader whose first language is not English
works much harder than that.

Two defects that were already in the published corpus when this was written are
the argument for the whole file:

  * The product's own name had two spellings. `Lager Box` appeared 329 times and
    `Lagerbox` 119 times, across the same pages. A reader cannot tell whether
    they are the same thing, and neither can a translator or an agent.
  * British and American spelling were mixed inside single pages: `behaviour` 8
    / `behavior` 35, `serialise` 2 / `serialize` 14, `recognise` 4 /
    `recognize` 6.

Neither is catchable by review at the scale of 276 pages, and neither would
have been caught by any check that existed.

WHAT IS CHECKED

  terms       a Lager noun spelled a way STYLE.md does not sanction
  spelling    British spelling where the house style is American
  modals      should / would / may / might / could in normative text
  length      a sentence over the word cap for its register
  tense       a perfect or progressive form where STE allows only simple tenses
  conjunction `and/or`, which STE forbids
  paragraph   a paragraph over the sentence cap for its register
  passive     REPORT ONLY, never gated -- see below

WHAT IS NOT CHECKED, AND WHY

Whether the result is *good* controlled English. STE is controlled, not terse:
it keeps articles and it keeps `that`. A rewrite that hits every number here and
reads like a telegram has failed, and no checker can see that. That is the one
failure mode of this program and it needs a human reading whole pages.

`passive` is heuristic and stays report-only for the same reason. It cannot
separate "the lock is released through a finally block" -- a state description,
correct as written -- from "the script is read by the backend", which is a
passive to fix. Gating it would train people to write around the regex.

THE TRAPS

Each of these produced a wrong number while this file was being written.

  1. PARAGRAPH ASSEMBLY. A hard-wrapped sentence spans several source lines.
     Splitting per line reports it as three short sentences and the length rule
     never fires. Joining across a blank line instead glues a heading onto the
     next paragraph and invents 70-word sentences that nobody wrote -- the first
     survey of this corpus reported a 77-word sentence in
     setting-up-a-lager-box.mdx that does not exist. Join within a paragraph,
     break at the blank line, and drop headings before either.

  2. ABBREVIATIONS AND VERSIONS. `e.g.` and `v0.43.0` both contain a period
     followed by something. Split naively and the corpus fills with two-word
     sentences, which drags the average down and hides real length violations.

  3. LINK TARGETS ARE NOT WORDS. `[Setting Up a Lager Box](/source/getting-
     started/setting-up-a-lager-box)` is five words of prose and one URL. Count
     the URL and every page of cross-references looks like it violates the cap.

  4. CODE IS NOT PROSE. Fenced blocks, option tables and MDX component tags are
     exempt. `Lagerbox` inside a hostname or a JSON key is correct and must not
     be reported -- the rule is about prose. An inline backtick span becomes one
     placeholder word rather than nothing: deleting it removes a noun from the
     sentence and quietly undercounts every length cap.

  5. LIST ITEMS ARE SEPARATE UNITS. Markdown puts no blank line between the
     items of a list, so paragraph assembly joins them into one. That reported a
     58-word sentence in test/CONVENTIONS.md that is really six short bullets.

  6. COMMAND NAMES LOOK LIKE PROSE. `lager box-config` contains the string
     "lager box", which is the wrong spelling of the product name -- in 52
     places, none of them a defect. Terminology patterns need to exclude the
     command surface, and `lager-cli` is a PyPI package name rather than a
     misspelling at all.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / 'docs'
SOURCE = DOCS / 'source'
BASELINE = Path(__file__).resolve().parent / 'ste_baseline.json'

# Prose files outside docs/source that a user reads. docs/reference/*.md are
# deliberately absent: they are unpublished working notes, outside the mint
# broken-links gate for the same reason, and holding them to a published-prose
# standard would be inventing work.
#
# LICENSE, NOTICE and CODE_OF_CONDUCT.md are absent on purpose and must stay
# absent. The Code of Conduct is verbatim Contributor Covenant; editing its
# sentences to fit a word cap stops it being that document, which is the only
# property it has.
ROOT_PROSE = [
    'docs/STYLE.md',
    'README.md',
    'CONTRIBUTING.md',
    'RELEASE_PROCESS.md',
    'SECURITY.md',
    'docs/README.md',
    'test/README.md',
    'test/CONVENTIONS.md',
    'test/COVERAGE.md',
]

# Historical record. A release note describes what shipped on a date; rewriting
# it makes the archive disagree with itself about what was said at the time.
# Future notes are written against docs/source/release-notes/_template.mdx.
EXEMPT_DIRS = {'release-notes'}

# One term, one meaning (STYLE.md rule 5). Canonical spelling -> the patterns
# that are wrong for it. Zero tolerance: these have no defensible exception in
# prose, which is why they carry no budget.
# `lager-cli` is deliberately absent: it is the name of the PyPI distribution,
# not a misspelling of the product. `pip install lager-cli` is correct and the
# rule must not argue with it. So is `Device Under Test (DUT)` -- expanding an
# acronym on first use is good technical writing, not a terminology defect.
#
# The exclusions on `lager box` are load-bearing. Without `(?!-)` the pattern
# matches `lager box-config`, a command name, in 52 places. Without the
# subcommand lookahead it matches `lager box config` and `lager box dut` -- the
# deprecated aliases, which still ship -- where the words are a command line
# rather than the product's name.
#
# The distinguishing signal is what follows: prose never puts a command verb
# after the product name. Keep this list short and add to it only when a real
# message trips the rule.
_BOX_SUBCOMMANDS = ('update', 'list', 'add', 'delete', 'remove', 'show',
                    'set', 'get', 'init', 'apply', 'config', 'dut')

TECHNICAL_NAMES = {
    'Lager Box': [r'\bLagerbox\b', r'\bLagerBox\b', r'\bLager-Box\b',
                  r'\b[Ll]ager box\b(?!-)(?!\s+(?:' + '|'.join(_BOX_SUBCOMMANDS) + r')\b)'],
}

# American spelling (STYLE.md rule 6). The pairs actually present in the corpus,
# not every pair in English -- an entry nobody can trip is noise in the diff.
#
# `cancelled` is deliberately absent. The rule exists to end a split, and there
# is no split: the corpus says `cancelled` 16 times and `canceled` zero times.
# Both are acceptable American spellings, so flagging it would mean 26 edits to
# change one consistent spelling into another consistent spelling.
BRITISH = {
    'behaviour': 'behavior', 'behaviours': 'behaviors',
    'recognise': 'recognize', 'recognised': 'recognized', 'recognises': 'recognizes',
    'serialise': 'serialize', 'serialised': 'serialized', 'serialises': 'serializes',
    'initialise': 'initialize', 'initialised': 'initialized',
    'colour': 'color', 'colours': 'colors',
    'analyse': 'analyze', 'analysed': 'analyzed',
    'licence': 'license', 'organisation': 'organization',
}

# STE approves can / will / must, and bans the modals a reader is entitled to
# read as optional (STYLE.md rule 4). `could` is included: in this corpus it is
# almost always narrating a past defect ("nets could not be opened"), which the
# simple past says better.
BANNED_MODALS = ['should', 'would', 'may', 'might', 'could']

# Word caps (STYLE.md rule 1). Procedural text is executed a step at a time;
# reference text is scanned. The registers differ, so the caps differ.
CAP_PROCEDURAL = 20
CAP_DESCRIPTIVE = 25
PROCEDURAL_DIRS = {'getting-started'}

# STE allows only the simple tenses: simple past, simple present, simple future.
# A perfect or progressive form hides when the thing happens, which is the whole
# objection -- "the box is not starting" and "the box does not start" describe
# different situations to a reader who has to act on one of them.
#
# The adverb slot used to admit only `not`, so `is currently outputting` and
# `is actually presenting` sat in pages that reported clean. Any adverb can
# stand there; `\w+ly` plus the handful of common non-`-ly` ones covers it.
_ADVERB = r'(?:\w+ly\s+|not\s+|never\s+|already\s+|just\s+|still\s+|also\s+)?'

# `\w+ing` is not the same as a participle. `nothing`, `anything`, `something`
# and `string` all end in -ing and all follow `is` in ordinary correct prose.
# None appears in checked prose today, but the rule would fire on the first one
# written, and a false positive in a zero-tolerance corpus costs more than the
# rule earns.
_NOT_PARTICIPLE = (r'nothing|something|anything|everything|string|thing|'
                   r'during|morning|evening|ceiling|spring|timing|warning|'
                   r'setting|meaning|listing|heading|building|wiring')

_TENSE = re.compile(
    r'\b(?:has|have|had)\s+' + _ADVERB + r'(?:been|\w+ed)\b'
    r'|\b(?:is|are|was|were)\s+' + _ADVERB +
    r'(?!(?:' + _NOT_PARTICIPLE + r')\b)\w+ing\b'
    r'|\bwill\s+have\b', re.I)

# STE forbids the solidus as a conjunction. Only `and/or` is checked. A general
# rule is unenforceable here and would be 197 findings of pure noise: `I/O`,
# `HIGH/LOW`, `Input/Output`, `Receiver/Transmitter` and `ADC/DAC/GPIO` are
# Technical Names, which STE permits, and no regex separates them from a real
# conjunction.
_CONJUNCTION = re.compile(r'\band/or\b', re.I)

# Paragraph limits (STE). Procedural writing is executed a step at a time, so it
# gets the tighter bound.
PARA_PROCEDURAL = 6
PARA_DESCRIPTIVE = 10

_PASSIVE = re.compile(
    r'\b(?:is|are|was|were|be|been|being)\s+(?:not\s+|never\s+|already\s+)?'
    r'(\w+ed|built|written|sent|set|run|read|kept|left|held|made|shown|given|'
    r'taken|found|done|known|thrown|drawn|torn|lost|dealt|meant)\b', re.I)

# Trap 2: a period that does not end a sentence.
_ABBREV = ['e.g.', 'i.e.', 'etc.', 'vs.', 'cf.', 'approx.', 'Inc.', 'Ltd.', 'Dr.', 'St.']


def _mask(text: str) -> str:
    """Hide periods that do not end sentences, so the splitter cannot see them."""
    for abbrev in _ABBREV:
        text = text.replace(abbrev, abbrev.replace('.', '\x00'))
    # Versions and decimals: v0.43.0, 3.3, 192.168.1.100
    return re.sub(r'(?<=\d)\.(?=\d)', '\x00', text)


def _unmask(text: str) -> str:
    return text.replace('\x00', '.')


def split_sentences(paragraph: str):
    """Yield (offset, sentence) pairs. Offsets index into `paragraph`."""
    masked = _mask(paragraph)
    start = 0
    for match in re.finditer(r'(?<=[.!?])\s+|:\s*$', masked):
        chunk = masked[start:match.start()].strip()
        if chunk:
            yield start, _unmask(chunk)
        start = match.end()
    tail = masked[start:].strip()
    if tail:
        yield start, _unmask(tail)


def prose_paragraphs(text: str, is_mdx: bool):
    """Yield (lineno, paragraph) for every prose paragraph in a markdown file.

    Traps 1, 3 and 4 all live here. A paragraph is the unit: source lines are
    joined within one and never across the blank line that ends it.
    """
    lines = text.split('\n')

    # Frontmatter. Bounded by the first two `---` lines, not by every `---`,
    # because a horizontal rule is spelled the same way and there are many.
    first = 0
    if lines and lines[0].strip() == '---':
        for i in range(1, len(lines)):
            if lines[i].strip() == '---':
                first = i + 1
                break

    para: list[tuple[int, str]] = []
    in_fence = False

    def flush():
        """Join the paragraph's lines, THEN strip markdown across the whole of it.

        Order matters: an inline code span may span a newline, and only the
        joined text can see both of its backticks. The per-line map is rebuilt
        from the cleaned text so a violation still reports the line its sentence
        starts on.
        """
        nonlocal para
        if not para:
            return None
        joined = clean_inline(' '.join(t for _, t in para))
        # Re-derive the line map by cleaning each line on its own. That is the
        # old, wrong split for a wrapped span, but it is only used to attribute
        # a line number, never to count words.
        line_map = [(n, clean_inline(t)) for n, t in para]
        line_map = [(n, t) for n, t in line_map if t]
        para = []
        if not joined:
            return None
        return (line_map[0][0] if line_map else 0, joined, line_map)

    for index in range(first, len(lines)):
        raw = lines[index]
        stripped = raw.strip()

        if stripped.startswith('```') or stripped.startswith('~~~'):
            in_fence = not in_fence
            done = flush()
            if done:
                yield done
            continue
        if in_fence:
            continue

        if not stripped:
            done = flush()
            if done:
                yield done
            continue

        # Headings, table rows, blockquotes, MDX tags, imports, horizontal
        # rules, and MDX comments are all structure rather than prose (trap 4).
        if (stripped.startswith('#') or stripped.startswith('|')
                or stripped.startswith('>') or stripped.startswith('{/*')
                or re.match(r'^[-*=_]{3,}$', stripped)
                or re.match(r'^(import|export)\s', stripped)):
            done = flush()
            if done:
                yield done
            continue
        if is_mdx and stripped.startswith('<'):
            # A tag line. Its children on following lines are still prose.
            done = flush()
            if done:
                yield done
            continue

        # A new list item ends the previous one. Markdown puts no blank line
        # between items, so without this every list becomes a single
        # "sentence" -- one 58-word violation in test/CONVENTIONS.md that is
        # really six short bullets (trap 5).
        if re.match(r'^([-*+]|\d+[.)])\s', stripped) and para:
            done = flush()
            if done:
                yield done

        # Deliberately NOT cleaned here. clean_inline() used to run per source
        # line, so a `code span` opened on one line and closed on the next was
        # never collapsed to CODE and its literal words counted as prose -- the
        # whole of a 33-word violation in usb.mdx that was not one. Cleaning
        # happens once, on the joined paragraph, in flush().
        para.append((index + 1, stripped))

    done = flush()
    if done:
        yield done


def clean_inline(line: str) -> str:
    """Strip markdown that is not words: code spans, link targets, emphasis."""
    line = re.sub(r'`[^`]*`', 'CODE', line)                  # inline code (trap 4)
    line = re.sub(r'!\[[^\]]*\]\([^)]*\)', ' ', line)        # images
    line = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', line)     # links (trap 3)
    line = re.sub(r'<[^<>]{0,120}>', ' ', line)              # inline MDX tags
    line = re.sub(r'^([-*+]|\d+\.)\s+', '', line)            # list marker
    line = re.sub(r'\*\*|__|(?<!\w)[*_](?!\w)', '', line)    # emphasis
    return line.strip()


def user_facing_literals(path: Path):
    """Yield (lineno, text) for strings the CLI prints at a user.

    Parsed with `ast`, not regex: these are f-strings, implicit concatenations
    and `.format()` calls as often as they are plain literals, and a regex reads
    only as far as the first quote. The f-string case matters most -- almost
    every message that names a net or a box is one.
    """
    try:
        tree = ast.parse(path.read_text(errors='ignore'))
    except SyntaxError:
        return

    # LagerError and BoxError were absent until batch G, and carried 11 banned
    # modals across six files. They print at a user exactly as click.echo does;
    # the only reason they were missed is that they are raised rather than
    # called for their side effect.
    emitters = {'echo', 'secho', 'UsageError', 'BadParameter', 'ClickException',
                'LagerError', 'BoxError'}

    def literal_text(node) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return ' '.join(literal_text(v) for v in node.values)
        return ''

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else (
                node.func.id if isinstance(node.func, ast.Name) else '')
            if name in emitters:
                for arg in node.args[:1]:
                    text = clean_inline(literal_text(arg))
                    if text:
                        yield node.lineno, text
            for kw in node.keywords:
                if kw.arg in ('help', 'cause', 'suggestion'):
                    text = clean_inline(literal_text(kw.value))
                    if text:
                        yield node.lineno, text


def offset_to_line(line_map, offset: int) -> int:
    """Map a character offset in a joined paragraph back to its source line."""
    cursor = 0
    for lineno, text in line_map:
        cursor += len(text) + 1
        if offset < cursor:
            return lineno
    return line_map[-1][0] if line_map else 0


class Violation:
    __slots__ = ('path', 'line', 'rule', 'text')

    def __init__(self, path, line, rule, text):
        self.path, self.line, self.rule, self.text = path, line, rule, text

    def __str__(self):
        return f'{self.path}:{self.line}: {self.rule}: {self.text}'


def check_word_rules(rel, lineno, text, cap=None):
    """terms / spelling / modals / length / passive on one sentence."""
    found = []

    for canonical, patterns in TECHNICAL_NAMES.items():
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                found.append(Violation(rel, lineno, 'terms',
                                       f'"{match.group(0)}" -- write "{canonical}"'))

    for match in re.finditer(r'\b([a-zA-Z]+)\b', text):
        word = match.group(1)
        american = BRITISH.get(word.lower())
        if american:
            found.append(Violation(rel, lineno, 'spelling',
                                   f'"{word}" -- write "{american}"'))

    for match in re.finditer(r'\b(' + '|'.join(BANNED_MODALS) + r')\b', text, re.I):
        found.append(Violation(rel, lineno, 'modals',
                               f'"{match.group(1)}" -- use can, will, or must'))

    if cap is not None:
        words = len(text.split())
        if words > cap:
            excerpt = text if len(text) <= 90 else text[:87] + '...'
            found.append(Violation(rel, lineno, 'length',
                                   f'{words} words (cap {cap}): {excerpt}'))

    for match in _TENSE.finditer(text):
        found.append(Violation(rel, lineno, 'tense',
                               f'"{match.group(0)}" -- use a simple tense'))

    for match in _CONJUNCTION.finditer(text):
        found.append(Violation(rel, lineno, 'conjunction',
                               f'"{match.group(0)}" -- write the two cases out'))

    for match in _PASSIVE.finditer(text):
        found.append(Violation(rel, lineno, 'passive', f'"{match.group(0)}"'))

    return found


def markdown_targets():
    """Every prose file in scope, as (relative path, absolute path)."""
    targets = []
    for path in sorted(SOURCE.rglob('*.mdx')):
        if set(path.relative_to(SOURCE).parts) & EXEMPT_DIRS:
            continue
        targets.append((str(path.relative_to(REPO)), path))
    for name in ROOT_PROSE:
        path = REPO / name
        if path.exists():
            targets.append((name, path))
    return targets


def code_targets():
    """Python modules whose string literals reach a user."""
    targets = []
    for root in ('cli', 'box'):
        base = REPO / root
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            targets.append((str(path.relative_to(REPO)), path))
    return targets


def scan():
    """Every violation in the corpus, grouped by file."""
    by_file: dict[str, list[Violation]] = {}

    for rel, path in markdown_targets():
        cap = (CAP_PROCEDURAL
               if set(path.parts) & PROCEDURAL_DIRS else CAP_DESCRIPTIVE)
        procedural = bool(set(path.parts) & PROCEDURAL_DIRS)
        para_cap = PARA_PROCEDURAL if procedural else PARA_DESCRIPTIVE
        found = []
        for first, paragraph, line_map in prose_paragraphs(path.read_text(),
                                                           path.suffix == '.mdx'):
            sentences = list(split_sentences(paragraph))
            for offset, sentence in sentences:
                lineno = offset_to_line(line_map, offset)
                found.extend(check_word_rules(rel, lineno, sentence, cap))
            if len(sentences) > para_cap:
                found.append(Violation(rel, first, 'paragraph',
                                       f'{len(sentences)} sentences (cap {para_cap})'))
        if found:
            by_file[rel] = found

    # Help text and messages are fragments, not sentences: a 24-word help string
    # is a different defect from a 24-word paragraph sentence and the cap does
    # not transfer. Terminology, spelling and modals do.
    for rel, path in code_targets():
        found = []
        for lineno, text in user_facing_literals(path):
            found.extend(check_word_rules(rel, lineno, text, cap=None))
        if found:
            by_file[rel] = found

    return by_file


def counts(by_file):
    out: dict[str, dict[str, int]] = {}
    for rel, violations in by_file.items():
        tally: dict[str, int] = {}
        for violation in violations:
            tally[violation.rule] = tally.get(violation.rule, 0) + 1
        out[rel] = tally
    return out


# Rules with no budget: a violation is a defect on day one, in every file. They
# are cheap, they have no false positives, and allowing a budget would only
# record how many times we chose not to fix a two-character mistake.
ZERO_TOLERANCE = {'terms', 'spelling', 'conjunction'}

# Report-only. See the module docstring: gating a heuristic teaches people to
# write around the regex rather than to write actively.
UNGATED = {'passive'}


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Assert that user-facing prose still follows docs/STYLE.md.')
    parser.add_argument('--only', choices=['terms', 'spelling', 'modals',
                                           'length', 'tense', 'conjunction',
                                           'paragraph', 'passive'],
                        help='run a single rule')
    parser.add_argument('--report', action='store_true',
                        help='print every violation, ignoring the baseline')
    parser.add_argument('--update-baseline', action='store_true',
                        help='rewrite tools/ste_baseline.json from the current tree')
    parser.add_argument('--path', help='restrict to files under this path prefix')
    parser.add_argument('--list-files', action='store_true',
                        help='print the files in scope, one per line, and exit')
    args = parser.parse_args()

    # Batch conversions run mechanical sweeps (`sed -i` over a term). Deriving
    # the file list from `grep -rl` instead of from here is how the first sweep
    # edited two shipped release notes and test/COVERAGE.md: the sweep's scope
    # was wider than the gate's, so it changed files the gate cannot see. Pipe
    # this into xargs and the two cannot disagree.
    if args.list_files:
        for rel, _ in markdown_targets() + code_targets():
            print(rel)
        return 0

    by_file = scan()
    if args.only:
        by_file = {rel: [v for v in vs if v.rule == args.only]
                   for rel, vs in by_file.items()}
        by_file = {rel: vs for rel, vs in by_file.items() if vs}
    if args.path:
        by_file = {rel: vs for rel, vs in by_file.items() if rel.startswith(args.path)}

    current = counts(by_file)

    if args.update_baseline:
        gated = {rel: {rule: n for rule, n in tally.items()
                       if rule not in UNGATED and rule not in ZERO_TOLERANCE}
                 for rel, tally in current.items()}
        gated = {rel: tally for rel, tally in gated.items() if tally}
        BASELINE.write_text(json.dumps(dict(sorted(gated.items())),
                                       indent=2, sort_keys=True) + '\n')
        total = sum(sum(t.values()) for t in gated.values())
        print(f'baseline written: {len(gated)} files, {total} budgeted violations')
        return 0

    baseline = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}

    totals: dict[str, int] = {}
    for tally in current.values():
        for rule, n in tally.items():
            totals[rule] = totals.get(rule, 0) + n
    for rule in ['terms', 'spelling', 'modals', 'length', 'tense',
                 'conjunction', 'paragraph', 'passive']:
        if args.only and rule != args.only:
            continue
        note = ''
        if rule in ZERO_TOLERANCE:
            note = '  (zero tolerance)'
        elif rule in UNGATED:
            note = '  (report only)'
        print(f'  {rule:9} {totals.get(rule, 0):5}{note}')

    if args.report:
        for rel in sorted(by_file):
            for violation in by_file[rel]:
                print(f'  {violation}')
        return 0

    failures: list[str] = []
    for rel in sorted(set(current) | set(baseline)):
        tally = current.get(rel, {})
        allowed = baseline.get(rel, {})
        for rule, n in sorted(tally.items()):
            if rule in UNGATED:
                continue
            budget = 0 if rule in ZERO_TOLERANCE else allowed.get(rule, 0)
            if n > budget:
                failures.append(
                    f'{rel}: {rule} {n} > budget {budget}')
                for violation in by_file[rel]:
                    if violation.rule == rule:
                        failures.append(f'    {violation}')

    if not failures:
        print('\nprose follows docs/STYLE.md.')
        return 0

    print(f'\nFAIL: {len(set(f.split(":")[0] for f in failures if not f.startswith("    ")))}'
          f' file(s) over budget.\n', file=sys.stderr)
    for failure in failures:
        print(f'  {failure}', file=sys.stderr)
    print('\n      The budget in tools/ste_baseline.json only ratchets down. If a\n'
          '      rewrite genuinely improved a file, run --update-baseline and commit\n'
          '      the smaller number with it. Never raise a budget to land a change.',
          file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

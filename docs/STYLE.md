# Writing style for Lager user-facing text

This is the house style for every sentence a user reads. It covers the pages under
`docs/source/`, the root prose files, `lager --help` text, and the messages the CLI
prints.

The style is **ASD-STE100, Simplified Technical English** — the controlled language
written for aircraft maintenance manuals, and since adopted well beyond aerospace.

It applies because the audience matches the one STE was built for. Lager's docs tell
people to energize supplies, flash parts, and erase flash, often in a language that is
not their first. A sentence that a reader can take two ways gets taken the wrong way,
and the cost lands on hardware.

`tools/check_ste.py` enforces the measurable rules. The rest need a human.
**Rule 12 is the one a checker cannot see, and the one a rewrite most often breaks.**

## What is adopted, and what is not

STE has two parts. This project adopts them differently, and the difference is a
licensing constraint rather than a preference.

- **Part 1, the Writing Rules — adopted in full.** Every rule below comes from it.
- **Part 2, the Dictionary — not reproduced.** The approved-word list is a licensed ASD
  specification, and a public repository cannot carry it. In its place this project
  keeps its own Technical Names table (rule 7). STE itself expects that: a project
  defines its own Technical Names and Technical Verbs, and those are always approved.

So `MPSSE`, `bitbang`, `heartbeat`, `bringup` and `enumerate` are approved words here.
Nothing in this document argues with a domain noun.

No tool, this one included, certifies a document as STE. ASD certifies no tool.

## Scope

Applies to prose. Does **not** apply to: fenced code blocks, inline code spans, option
and column tables, `docs.json`, or headings.

Three bodies of text are out of scope entirely:

- `docs/source/release-notes/` — a historical record. A release note says what shipped on
  a date; editing it makes the archive disagree with itself. New notes are written against
  `docs/source/release-notes/_template.mdx`.
- `docs/reference/*.md` — unpublished working notes, outside the mint broken-links gate for
  the same reason.
- `LICENSE`, `NOTICE`, `CODE_OF_CONDUCT.md` — the Code of Conduct is verbatim Contributor
  Covenant. Rewriting its sentences stops it being that document, which is the only
  property it has.

`python tools/check_ste.py --list-files` prints the files in scope. A mechanical sweep
must take its file list from there, never from `grep -rl`. The first sweep on this
project used `grep -rl` and edited two shipped release notes. The sweep reached further
than the gate can see.

---

# Sentences

## 1. Keep sentences short

Twenty words maximum in procedural text (`docs/source/getting-started/`). Twenty-five in
reference and descriptive text. These are STE's own two limits.

> **Before** (39 words)
> Note that this list is unique to your personal computer which means you can give your
> Lager Boxes any name you want (though it is normally in your best interest to agree on
> a naming convention with your team).

> **After**
> This list is local to your computer. You can give each Lager Box any name you want.
> Agree on a naming convention with your team first.

The cap is a cap, not a target. A 9-word sentence is not better than a 16-word one.

## 2. One instruction per sentence, and the condition comes first

A reader executes a sentence as they reach its end. A condition that arrives after the
command arrives too late.

> **Before** — Install Docker yourself first, if the machine does not already have it.
>
> **After** — If the machine does not have Docker, install it first.

Do not join two instructions with "and" or a semicolon. Split them, or make them a
numbered list.

## 3. Keep paragraphs short

Six sentences maximum in procedural text. Ten in descriptive text. One topic per
paragraph, and the first sentence names the topic.

A list item counts as a paragraph. One bullet in `architecture.mdx` carried ten
sentences and read as a wall; it is now three short paragraphs under one bullet.

# Verbs

## 4. Write in the active voice

Name the thing that acts.

> **Before** — The script is written to disk and then never read by the backend.
>
> **After** — The backend writes the script to disk. It never reads the script back.

A description of state is not a passive to fix. "The lock is released through a `finally`
block" is correct as written: the sentence is about the lock, not about who released it.
`check_ste.py` reports passive constructions but never fails on them, because it cannot
tell these two apart. **This rule is enforced by review, not by the checker.**

## 5. Use only the simple tenses

Simple past, simple present, simple future. No perfect forms, no progressive forms.

> **Before** — Docker itself is not starting and installing it again will not help.
>
> **After** — Docker itself does not start. A second install does not help.

> **Before** — The command below lists every Lager Box you have added.
>
> **After** — The command below lists every Lager Box you added.

## 6. Use only `can`, `will`, and `must`

`should`, `would`, `may`, `might`, and `could` all read as optional. If the step is
required, say `must`. If it is a capability, say `can`. If it is what happens, say `will`.

> **Before** — Each net corresponds to an interface that you may want to interact with.
>
> **After** — Each net names one interface that you can control.

In a failure message, replace `Could not X` with what actually happened.

> **Before** — Could not connect to the box.
>
> **After** — The box did not answer within 10 seconds.

# Words

## 7. One term, one meaning

A thing has one name, and a name means one thing. `tools/check_ste.py` enforces this
table with no budget: a violation is a defect on the day it is written.

| Write | Never |
|---|---|
| `Lager Box` | `Lagerbox`, `LagerBox`, `Lager-Box`, `lager box` |

Two spellings of the product's own name were in the published corpus when this document
was written: `Lager Box` and `Lagerbox`, on the same pages. A reader cannot tell whether
they are one thing, and neither can a translator or an agent.

`lager-cli` is not on this table. It is the name of the PyPI distribution, and
`pip install lager-cli` is correct.

Add a row when a second spelling appears, not before.

## 8. Use American spelling

`behavior`, not `behaviour`. `serialize`, not `serialise`. `recognize`, not `recognise`.

The rule exists to end a split, not to police English. Where the corpus is already
consistent on a word that both dialects accept, leave it alone.

## 9. Replace gerund clauses with finite ones

An `-ing` clause hides which thing acts and when.

> **Before** — It provides a unified interface for interacting with embedded hardware,
> allowing firmware engineers to build repeatable workflows.
>
> **After** — It gives one interface to your hardware. Firmware engineers use it to build
> repeatable workflows.

## 10. Keep noun clusters to three words

Four or more nouns in a row stop telling the reader which noun is the subject. Break the
cluster with a preposition.

> **Before** — box image pull fallback behavior
>
> **After** — the fallback behavior when a box pulls an image

Not checked automatically. A reliable noun-cluster detector needs a parser, and the
false-positive rate on hardware names (`USB DAQ library build`) is too high to gate on.

# Punctuation

## 11. Do not write `and/or`

Write the cases out: "A, or B, or both."

The wider STE rule against the solidus is **not** enforced here, and must not be. This
corpus contains 197 slashes, and nearly all are Technical Names that STE permits: `I/O`,
`HIGH/LOW`, `Input/Output`, `Receiver/Transmitter`, `ADC/DAC/GPIO`. No regex separates
those from a real conjunction, so only `and/or` is checked.

# Practices

## 12. Keep the articles, and keep `that`

This is the rule that separates controlled English from telegraphese, and it is the one a
rewrite breaks first. STE shortens sentences. It does not delete words that carry grammar.

> **Wrong** — Set voltage net DUT_POWER. Check output enabled.
>
> **Right** — Set the voltage on the `DUT_POWER` net. Check that the output is enabled.

Nothing in this document authorizes dropping `the`, `a`, or `that`. If a rewritten page
reads like a telegram, it failed rule 12 even when it passes every check.

## 13. Write a warning as a command, with the condition first

A safety note states the condition, then the action, then the consequence. It does not
open with prose.

> **Before** — It is worth noting that the supply can be left enabled after a failed run.
>
> **After** — Disable the supply before you leave the bench. A failed run can leave it
> enabled, which powers the DUT with nobody watching.

## 14. Use a vertical list for anything with more than two steps

A sentence that carries three actions joined by commas is a list that lost its
formatting. Number the steps when order matters, and bullet them when it does not.

---

## Running the check

```bash
python tools/check_ste.py                 # gate: fails when a file is over budget
python tools/check_ste.py --report        # every violation, ignoring the budget
python tools/check_ste.py --only tense --path docs/source/getting-started
python tools/check_ste.py --list-files    # the files in scope, for a sweep
python tools/check_ste.py --update-baseline
```

| Rule | Enforcement |
|---|---|
| `terms`, `spelling`, `conjunction` | no budget: any violation fails |
| `modals`, `length`, `tense`, `paragraph` | per-file budget, ratchets down |
| `passive` | reported, never gates (rule 4) |
| noun clusters, topic sentences, warning shape | review only |

`tools/ste_baseline.json` holds the per-file budget. **It only ratchets down.** If a
rewrite genuinely improved a file, run `--update-baseline` and commit the smaller number
in the same change. Never raise a budget to land something.

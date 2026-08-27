# Writing style for Lager user-facing text

This is the house style for every sentence a user reads. It covers the pages under
`docs/source/`, the root prose files, `lager --help` text, and the messages the CLI
prints. It is derived from ASD-STE100 (Simplified Technical English), the controlled
language written for aircraft maintenance manuals.

It applies because the audience matches the one STE was built for. Lager's docs tell
people to energise supplies, flash parts, and erase flash, often in a language that is
not their first. A sentence that can be read two ways gets read the wrong way, and the
cost lands on hardware.

`tools/check_ste.py` enforces the measurable half of this document. The rest needs a
human. **Rule 8 is the one a checker cannot see and the one a rewrite most often breaks.**

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

## What this is not

This is not full ASD-STE100. The standard's approved-word dictionary is a licensed ASD
specification, and we cannot embed it in a public repository. Strict application also
mangles the vocabulary this project needs: `MPSSE`, `bitbang`, `heartbeat`, `bringup`,
`enumerate`. Domain nouns are always approved here.

What is taken from STE is the writing rules below, plus one project-specific technical
names table.

---

## 1. Keep sentences short

Twenty words maximum in procedural text (`docs/source/getting-started/`). Twenty-five in
reference and descriptive text.

> **Before** (39 words)
> Note that this list is unique to your personal computer which means you can give your
> Lager Boxes any name you want (though it is normally in your best interest to agree on
> a naming convention with your team).

> **After**
> This list is local to your computer. You can give each Lager Box any name you want.
> Agree on a naming convention with your team first.

The cap is a cap, not a target. A 9-word sentence is not better than a 16-word one.

## 2. Write in the active voice

Name the thing that acts.

> **Before** — The script is written to disk and then never read by the backend.
>
> **After** — The backend writes the script to disk. It never reads the script back.

A description of state is not a passive to fix. "The lock is released through a `finally`
block" is correct as written: the sentence is about the lock, not about who released it.
`check_ste.py` reports passive constructions but never fails on them, because it cannot
tell these two apart.

## 3. One instruction per sentence, and the condition comes first

A reader executes a sentence as they reach its end. A condition that arrives after the
command arrives too late.

> **Before** — Install Docker yourself first, if the machine does not already have it.
>
> **After** — If the machine does not have Docker, install it first.

Do not join two instructions with "and" or a semicolon. Split them, or make them a
numbered list.

## 4. Use only `can`, `will`, and `must`

`should`, `would`, `may`, `might`, and `could` all read as optional. If the step is
required, say `must`. If it is a capability, say `can`. If it is what happens, say `will`.

> **Before** — Each net corresponds to an interface that you may want to interact with.
>
> **After** — Each net names one interface that you can control.

In a failure message, replace `Could not X` with what actually happened.

> **Before** — Could not connect to the box.
>
> **After** — The box did not answer within 10 seconds.

## 5. One term, one meaning

A thing has one name. `tools/check_ste.py` enforces this table with no budget: a violation
is a defect on the day it is written.

| Write | Never |
|---|---|
| `Lager Box` | `Lager Box`, `LagerBox`, `Lager-Box`, `lager box` |

Two spellings of the product's own name were in the published corpus when this document
was written: `Lager Box` and `Lager Box`, on the same pages. A reader cannot tell whether
they are one thing, and neither can a translator or an agent.

`lager-cli` is not on this table. It is the name of the PyPI distribution, and
`pip install lager-cli` is correct.

Add a row when a second spelling appears, not before.

## 6. Use American spelling

`behavior`, not `behavior`. `serialize`, not `serialise`. `recognize`, not `recognise`.

The rule exists to end a split, not to police English. Where the corpus is already
consistent on a word that both dialects accept, leave it alone.

## 7. Replace gerund clauses with finite ones

An `-ing` clause hides which thing acts and when.

> **Before** — It provides a unified interface for interacting with embedded hardware,
> allowing firmware engineers to build repeatable workflows.
>
> **After** — It gives one interface to your hardware. Firmware engineers use it to build
> repeatable workflows.

## 8. Keep the articles, and keep `that`

This is the rule that separates controlled English from telegraphese, and it is the one a
rewrite breaks first. STE shortens sentences. It does not delete words that carry grammar.

> **Wrong** — Set voltage net DUT_POWER. Check output enabled.
>
> **Right** — Set the voltage on the `DUT_POWER` net. Check that the output is enabled.

Nothing in this document authorises dropping `the`, `a`, or `that`. If a rewritten page
reads like a telegram, it has failed rule 8 even if it passes every check.

## 9. Present tense for behavior, simple past for what changed

Describe what the software does now in the present tense. Narrate a defect or an incident
in the simple past, with times and numbers rather than hedges.

> **Before** — We have identified an issue that may have impacted some users.
>
> **After** — Between 14:02 and 14:31 UTC, 12% of requests failed. A deploy at 14:00
> removed the cache warmup step.

---

## Running the check

```bash
python tools/check_ste.py                 # gate: fails when a file is over budget
python tools/check_ste.py --report        # every violation, ignoring the budget
python tools/check_ste.py --only length --path docs/source/getting-started
python tools/check_ste.py --update-baseline
```

`tools/ste_baseline.json` holds a per-file violation budget. **It only ratchets down.** If
a rewrite genuinely improved a file, run `--update-baseline` and commit the smaller number
in the same change. Never raise a budget to land something.

`terms` and `spelling` carry no budget at all. `passive` is reported and never gated.

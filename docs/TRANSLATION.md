# Translating the Lager docs

The published docs ship in English and Simplified Chinese. This file is the
terminology contract between them. It exists because the failure it prevents is
invisible: two translators who each make a reasonable choice produce a corpus
where the same thing has two names, and no build ever complains.

`docs/STYLE.md` governs the English. It does not apply to a translation — its
rules are American spelling, approved modals and a word count per sentence, and
none of those has a meaning in Mandarin. `tools/check_ste.py` exempts
`docs/source/zh/` for that reason.

## Who owns what

Release notes are never translated. A note says what shipped on a day, and the
archive's whole value is that it still says it. `check_translations.py` excludes
the directory, so it never appears in `--progress`.

| Tab | Pages | English words | Owner | State |
|---|---|---|---|---|
| Overview (Getting Started) | 10 | 22,411 | -- | done |
| CLI Reference | 47 | 65,031 | -- | done |
| AI Agents (MCP) | 2 | 2,555 | -- | done |
| Supported Instruments | 1 | 2,514 | -- | done |
| Python API | 27 | 31,157 | -- | done |
| Rust API | 31 | 19,753 | -- | done |

Every page is translated, so nothing is owned right now and the column is
empty. It stays because the next English page added needs an owner before
anyone starts on it: a corpus with no owner column is how two people translate
the same tab. The column is for coordination while work is in flight, not for
recording who did what afterwards -- put whatever identifier your team uses in
it when you pick a tab up, and clear it when the tab is done.

Claim a tab in the owner column before you start, and change it here when it
changes. Two people who each assume the other has a tab produce the same corpus
as two people who both translate it, and neither is visible until a reviewer
reads the diff. This table is the only place that assumption is written down.

Translate a whole tab rather than scattered pages. A tab is the unit a reader
navigates, and a half-translated tab sends them between languages on every
click. `--progress` reports by directory, so a tab that is finished reads as
finished.

## Layout

A translation mirrors the English filename under a language directory:

```
docs/source/getting-started/overview.mdx        <- English
docs/source/zh/getting-started/overview.mdx     <- Simplified Chinese
```

The mapping is derived, never configured. Keeping every published page under
`docs/source/` is what lets `check_docs.py`, `check_translations.py` and the
`broken-links` gate see translations without a second glob.

Each language has its own tree in `docs.json` under `navigation.languages`. A
language lists **only the pages that exist in it**. An untranslated page is
absent from that language's navigation, not a 404, so a partial translation
ships safely.

## The staleness gate

`docs/translations.json` records the SHA-256 of each English page as it read
when its translation was made. `tools/check_translations.py` recomputes it and
fails when the two disagree.

```bash
python tools/check_translations.py                 # every gate at once
python tools/check_translations.py --progress      # coverage per section
python tools/check_translations.py --record PATH   # stamp after translating
python tools/check_translations.py --relink        # fix cross-references
```

Staleness is one of three things that run with no argument; the other two are
below.

After you translate a page, or update a translation to match an English edit,
stamp it. The gate is in `static-checks.yml`, so an unstamped change fails CI.

**Do not stamp a page you did not actually retranslate.** Re-stamping is how the
manifest says "these two agree". A stamp on an untranslated edit converts
"nobody checked" into "the check passed", which is the one outcome worse than no
gate at all.

## The completeness gate

The hash catches a translation that went *stale*. It cannot see one that
arrived *incomplete* -- the English page never changed, so the hash still
matches while a paragraph, a table row or a whole `## See Also` section is
simply absent. Seven pages had lost content this way and every gate passed.

`--completeness` compares structure instead of prose, because prose does not
compare: Chinese says the same thing in markedly fewer characters, so a length
check fires on every page and is worth nothing. A bullet is a bullet in both
languages, though, and so is a table row, a fenced block and a `##` heading:

```bash
python tools/check_translations.py --completeness
```

A translation with **fewer** of any of those has dropped content. More is not
reported -- a translator who adds a clarifying line is making a call that is
theirs to make.

It also reports a page that leaves a code fence open, naming the file that has
it. That is how the stray ``` at the end of the English `cli/watt.mdx` was
found: as a fence count off by one on a translation that was complete.

## Cross-references while a tab is half done

Write links to sibling pages however you like, then run `--relink` before you
push. It points each link at the translation when one exists and at the English
page when one does not, and it is idempotent.

**It rewrites the path, never the fragment.** A link to
`/source/reference/cli/arm#detection` becomes a link to the translation, but
`#detection` is the slug of the *English* heading — the translated page has
`#检测`. Only the person who wrote the heading knows what it became, so
`--relink` reports a link whose all-ASCII fragment matches no heading in the
target translation, and leaves the fix to you. It checks rather than guesses,
because an ASCII fragment is often correct: a command name stays English in a
translated heading (`### tui`, `### assign`), so its slug is ASCII too. Do that pass whenever a page you link into gets translated.

It handles both link forms, which fail differently. An absolute
`/source/reference/python/net` is rewritten to carry the language prefix or not.
A relative `./net` already resolves inside the translation directory, so it is
silently correct once that sibling is translated and broken until then; the tool
leaves it relative when the sibling exists and rewrites it to the absolute
English path when it does not.

Do this rather than fixing links by hand, because the two failure modes look
nothing alike. A link to a page that is not translated yet fails the
broken-links gate, loudly. A link left pointing at English after that page was
translated fails nothing at all — the reader silently drops out of their
language, with no way to tell they were meant to stay in it. `--relink` fixes
both, and it is the only one of the two a checker can catch.

## What stays in English

**Everything executable.** Commands, subcommands, flags, environment variables,
net names, file paths, JSON keys, code inside fences, and the output a user
matches against their terminal. A reader who sees `lager nets add-batch` must be
able to type it.

Error strings keep their English wording even inside a translated
`<Accordion title="...">`, because the reader is matching them against real
output. Translate the framing around the quoted string, not the string.

**Product terms of art**, because they are the nouns the CLI uses:

| Term | Why it stays |
|---|---|
| `Lager Box` | The CLI noun is `box` — `--box`, `lager boxes`, `lager box-config`. Translating it to 计算机 or 主机 breaks the link between what the reader reads and what they type. Glossed once in the glossary. |
| `Net` | See below. |
| `DUT` | Only as an identifier: `DUT_POWER`, `lager dut`. In prose it is 被测设备. |

### Why `Net` is not 网

`Net` is the product's central abstraction and it is **not** translated.

网 standing alone reads as "network" or "web" to a Chinese reader. These same
pages use 网络 (network) and 网关 (gateway) heavily — the Getting Started tree
alone has 37 of them, often in the same paragraph as a net. A reader would have
to separate the product's core concept from ordinary networking vocabulary by
context on every line.

The EDA term for a schematic net is 网络, which is exactly the colliding word.
The Verilog/IC-design term 线网 is unambiguous but means an electrical wire net,
and Lager nets also cover webcams, robot arms and USB hubs, so it would read as
wrong for a large share of the net types.

`Net` in English has none of those problems and matches `lager nets`. Write
`Net`, `电源 Net`, `调试 Net`, `默认 Net`. Put a space between a Chinese
character and a Latin word.

## What gets a Chinese term

**Protocol and standard names stay as acronyms in running text.** SWD, VISA,
I2C, SPI, UART, JTAG, SCPI, GPIO, USB, BLE, RTT, GDB, REPL, TUI, CLI, MCP. This
is how Chinese embedded and test-and-measurement engineers actually write. The
Chinese full form belongs in the glossary entry, once, not in every sentence:

| Acronym | Glossary gives | Running text |
|---|---|---|
| SWD | 串行线调试 | `SWD` |
| VISA | 虚拟仪器软件架构 | `VISA` |
| SCPI | 可编程仪器标准命令 | `SCPI` |
| UART | 通用异步收发器 | `UART` |

**Device and quantity names take their ordinary Chinese term** wherever they are
prose rather than a role name or a command:

| English | Chinese | Note |
|---|---|---|
| Device Under Test / DUT | 被测设备 | `被测设备（DUT）` on first use per page |
| ADC | 模数转换器 | but `lager adc` and the `adc` role stay English |
| DAC | 数模转换器 | same |
| electronic load | 电子负载 | |
| state of charge / SOC | 荷电状态 | |
| over-voltage protection / OVP | 过压保护 | |
| over-current protection / OCP | 过流保护 | |
| oscilloscope | 示波器 | |
| logic analyzer | 逻辑分析仪 | |
| debug probe | 调试探针 | |
| bench | 实验台 | |
| firmware | 固件 | |
| to flash | 烧录 | |

## Write a Chinese paragraph on one line

**Do not wrap Chinese prose at 80 columns.** A line break inside a Markdown
paragraph renders as a space. English does not care -- its words are separated
by spaces anyway -- so wrapping is free there and every one of these files does
it. Chinese has no space between characters, so each wrapped line shows up as a
gap in the middle of a sentence:

```markdown
Lager 命令必须能通过网络连接到一台 Box。有两种方式可以提供这个连接。
您的选择决定了后面所有的步骤。
```

renders as `...提供这个连接。 您的选择...`, with a space that belongs to nothing.
Write the paragraph as one long line instead, however long it gets. The same
applies to a wrapped bullet or a wrapped numbered step, which are paragraphs
too.

This is the easiest mistake in the corpus to make, because it is a habit rather
than a decision -- 882 of them accumulated across 72 pages before anyone
noticed. So it is checked rather than trusted:

```bash
python tools/check_translations.py --reflow    # join them all; idempotent
```

The gate reports any that are left. A break between a Chinese character and a
Latin word is *not* one of these: there the rendered space is the one the
spacing rule below asks for, so it is correct and the tool leaves it alone.

## Headings and anchors

Translate headings. Mintlify slugifies Chinese headings and
`broken-links --check-anchors` validates the result, so a cross-reference to a
translated heading must use the translated anchor:

```markdown
请参阅 [第 7 节](#7-如何确保-box-运行的是被测代码)
```

This is checked. A wrong anchor fails the build — it is not silently skipped for
CJK. Verified by breaking one on purpose.

**Full-width brackets survive into the slug; ASCII ones do not.** The English
heading `Debug nets (J-Link)` gives `#debug-nets-j-link`: the `()` become
separators and vanish. The Chinese heading `调试 Net（J-Link）` gives
`#调试-net（j-link）` — the `（）` are kept verbatim. Guessing by analogy with the
English page produces a dead link, which is how this was found.

So do not hand-write an anchor to a heading that contains `（）`. Either link to a
heading without them, or read the slug off the rendered page. Better still,
prefer a heading with no brackets at all when something else will link to it —
`## 调试 Net` beats `## 调试 Net（J-Link）` for a heading that is a link target.

Leave glossary term headings in English (`## ADC`, `## SWD`). They are the
lookup key, and the reader arrives holding the English word.

## Before you push

```bash
python tools/check_translations.py --relink     # fix cross-references first
python tools/check_translations.py --reflow     # then unwrap Chinese paragraphs
python tools/check_translations.py --record-all # stamp what you translated
python tools/check_translations.py              # staleness + completeness + wrapping
python tools/check_docs.py
python tools/check_ste.py
cd docs && npx --yes mint@4.2.827 broken-links --files 'source/**/*.mdx' --check-anchors
```

`mint` needs Node 20; `package.json` pins the version CI uses.

## Adding a language

1. Add the directory name to `LANGUAGES` in `tools/check_translations.py`.
2. Add it to `EXEMPT_DIRS` in `tools/check_ste.py`.
3. Add a `navigation.languages` entry in `docs.json` listing only the pages that
   exist in it.

Mintlify localizes its own chrome — search box, "copy page", the on-this-page
rail — from the language code alone. Nothing to translate for those.

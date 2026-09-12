# Translating the Lager docs

The published docs ship in English and Simplified Chinese. This file is the
terminology contract between them. It exists because the failure it prevents is
invisible: two translators who each make a reasonable choice produce a corpus
where the same thing has two names, and no build ever complains.

`docs/STYLE.md` governs the English. It does not apply to a translation — its
rules are American spelling, approved modals and a word count per sentence, and
none of those has a meaning in Mandarin. `tools/check_ste.py` exempts
`docs/source/zh/` for that reason.

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
python tools/check_translations.py                 # the gate
python tools/check_translations.py --progress      # coverage per section
python tools/check_translations.py --record PATH   # stamp after translating
```

After you translate a page, or update a translation to match an English edit,
stamp it. The gate is in `static-checks.yml`, so an unstamped change fails CI.

**Do not stamp a page you did not actually retranslate.** Re-stamping is how the
manifest says "these two agree". A stamp on an untranslated edit converts
"nobody checked" into "the check passed", which is the one outcome worse than no
gate at all.

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

## Headings and anchors

Translate headings. Mintlify slugifies Chinese headings and
`broken-links --check-anchors` validates the result, so a cross-reference to a
translated heading must use the translated anchor:

```markdown
请参阅 [第 7 节](#7-如何确保-box-运行的是被测代码)
```

This is checked. A wrong anchor fails the build — it is not silently skipped for
CJK. Verified by breaking one on purpose.

Leave glossary term headings in English (`## ADC`, `## SWD`). They are the
lookup key, and the reader arrives holding the English word.

## Before you push

```bash
python tools/check_translations.py
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

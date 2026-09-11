# Contributing to Lager

Thanks for your interest in contributing. This document covers how to get set up,
what CI checks before a pull request can merge, and how the repository is laid out.

## Getting Started

**Clone the repository and install the CLI in development mode.** Run the
commands from the repository root:

```bash
git clone https://github.com/lagerdata/lager.git
cd lager
pip install -e cli/
lager --version
```

Python 3.10 or newer is required. To run the unit suites, also install the test
dependencies, with the same pytest pins that CI uses:

```bash
pip install -r test/requirements-unit.txt 'pytest>=9,<10' 'pytest-timeout>=2.3,<3'
```

## What CI Checks

Every pull request runs five workflows, and a pull request cannot merge until every
required check passes. Run the local equivalents before you push, to save a round trip.

| Workflow | What it checks |
|----------|----------------|
| **PR Gate: Unit Tests** | The six unit suites on Python 3.11, all six again on 3.10, 3.12, 3.13 and 3.14, and the MCP suite with the `cli[mcp]` extra installed |
| **PR Gate: Static Checks** | ShellCheck, `bash -n`, `actionlint`, `zizmor`, `ruff` (errors only), the `test/COVERAGE.md` counts, the docs checks, the prose style check, and broken links. A coverage report and `pip-audit` also run, but do not gate. |
| **PR Gate: Rust Checks** | `cargo check`, `clippy` correctness lints, `cargo test` and `cargo audit` for `box/oscilloscope-daemon`. `cargo fmt` is reported, not gated. |
| **PR Gate: Packaging** | Builds the sdist and the wheel, installs each one in a clean virtual environment, and imports every module |
| **PR Gate: Cross-Platform Smoke** | Installs the wheel on macOS and Windows, and imports every module |

[`.github/workflows/README.md`](.github/workflows/README.md) lists every workflow and
the exact required checks. The bench workflows drive real hardware, and ordinary pull
requests do not trigger them.

### Unit tests (no hardware)

**Each suite needs its own pytest process.** The suites set up `sys.modules`
differently before they import `lager`, so one combined run gives wrong results.
Run the suites from the repository root:

```bash
export PYTHONPATH="$PWD:$PWD/box"
PYTEST="pytest -v --import-mode=importlib -c /dev/null --timeout=60"

$PYTEST test/unit/cli/ cli/tests/
$PYTEST test/unit/box/
$PYTEST test/unit/measurement/
$PYTEST test/unit/blufi/
$PYTEST test/mcp/unit/
$PYTEST test/unit/test_*.py test/test_*.py
```

**A change that adds or removes a test also changes `test/COVERAGE.md`.** The Static
Checks job runs every suite, and it fails when the counts or the per-file tables do not
match the tree. Refresh the counts, then write a table row for each new test file:

```bash
python tools/check_coverage_counts.py --fix
python tools/check_coverage_counts.py
```

### Docs and prose checks

User-facing text follows [docs/STYLE.md](docs/STYLE.md), a house style that is based on
ASD-STE100. The rules cover the main root Markdown files, the pages under
`docs/source/`, and every message that the CLI prints. Run both checks before you push:

```bash
python tools/check_ste.py      # prose style
python tools/check_docs.py     # docs match the CLI, and every release has notes
```

A new top-level command needs a page under `docs/source/reference/cli/`, or
`check_docs.py` fails.

### Shell lint

CI runs ShellCheck at warning severity, with a fixed list of exclusions:

```bash
shellcheck -S warning -e SC2034,SC2320,SC2155,SC2164,SC2046 \
  $(find test tools box cli/deployment -name '*.sh')
```

### Hardware tests (require a connected box)

```bash
# Bash integration suites
test/integration/power/supply.sh <box-name> <net-name>

# Python API tests, executed on the box
lager python test/api/power/test_supply_comprehensive.py --box <box-name>
```

## Pull Requests

1. **Fork** and branch from `main`. Use a short prefixed branch name, e.g.
   `fix/supply-trip-message` or `feat/rtt-streaming`.
2. **Keep the change focused.** Unrelated fixes belong in their own PR.
3. **Add or update tests,** and refresh `test/COVERAGE.md` as described above.
4. **Update `CHANGELOG.md`** for any user-facing change. File the entry under
   `## [Unreleased]`. Write one bullet per change, in one to three sentences: what
   changed for a user, and the command or API it affects.
5. **Update the docs** in `docs/source/` if you changed a command, an API, or
   supported hardware.
6. **Fill in the PR template**, including how you tested.

Note that this is a **public repository**. Pull request titles, bodies, commit
messages, and code comments are world-readable and permanent. The repository merges
with **Rebase and merge**, so every commit message on your branch lands on `main` as
you wrote it. An edit to a PR body leaves the original text visible in its revision
history. Do not include customer names, private deployment details, or internal
discussion in anything you push.

### Code Style

- **Python** — follow PEP 8, and match the conventions of the file you edit. CI runs
  `ruff` for errors only, such as undefined names and syntax errors, not for style.
- **Bash** — pass ShellCheck with the settings above.
- **Rust** — keep `clippy` correctness lints at zero. Run `cargo fmt` on the files
  you change; CI reports formatting but does not gate it.
- **Commits** — write a clear imperative subject line explaining the change.
- **No emoji** in code, docs, commit messages, or PR text. Use `PASS`/`FAIL`,
  `[x]`/`[ ]`, "Supported"/"Not supported".
- **Copyright header** on every new source file:

  ```
  # Copyright 2024-2026 Lager Data
  # SPDX-License-Identifier: Apache-2.0
  ```

## Reporting Issues

Use [GitHub Issues](https://github.com/lagerdata/lager/issues) and the bug report
template. Search first — the issue can already exist. A good report includes
the exact command, its full output, `lager --version`, and the box version from
`lager hello --box <name>`.

For security vulnerabilities, **do not open a public issue.** Follow
[SECURITY.md](SECURITY.md).

## Repository Layout

```
lager/
├── cli/                      # The lager CLI, published to PyPI as lager-cli
│   ├── main.py               #   entry point; registers every command
│   ├── commands/             #   command modules, grouped by domain
│   ├── core/                 #   shared helpers, including the client for the box's :9000 API
│   └── deployment/           #   box install scripts, packaged with the CLI
├── box/                      # Software that runs on a Lager Box
│   ├── lager/                #   the box services and the on-box Python API
│   ├── oscilloscope-daemon/  #   Rust scope-streaming daemon
│   ├── udev_rules/           #   device permission rules
│   └── start_box.sh          #   builds and starts the box containers
├── test/                     # unit/ (no hardware), api/ and integration/ (need a box), mcp/
├── tools/                    # CI checkers: coverage counts, docs, prose style, imports
└── docs/                     # Mintlify docs source (docs/source/) and the style guide
```

For how the pieces talk to each other, see the
[architecture guide](https://docs.lagerdata.com/source/getting-started/architecture).
For the test suites, see `test/COVERAGE.md` and `test/CONVENTIONS.md`.

The Rust daemon uses edition 2024, so it builds with Rust 1.85 or newer. CI builds it
with Rust 1.95.0:

```bash
cd box/oscilloscope-daemon
cargo build --release
```

## Development Guidelines

### Adding a CLI command

1. Add the command module under `cli/commands/<category>/`, next to a similar command.
2. Most net commands send their action to the box's HTTP API on port 9000. Follow
   `post_net_command` in `cli/core/net_helpers.py`. If the box needs a new endpoint,
   add a handler under `box/lager/http_handlers/`.
3. Register the command in `cli/main.py`.
4. Add unit tests in `test/unit/cli/`.
5. Add a reference page under `docs/source/reference/cli/`. `tools/check_docs.py`
   fails without one.

### Adding a box feature

1. Add the backend code in `box/lager/<category>/`.
2. Add HTTP handlers in `box/lager/http_handlers/` if it needs an endpoint.
3. Update `box/lager/docker/box.Dockerfile` if dependencies change, pinning new
   ones.
4. Add unit tests in `test/unit/box/`, and an API test in `test/api/` if it
   touches hardware.

### Adding instrument support

1. Add the driver beside its peers, e.g. `box/lager/power/supply/`.
2. Register it in that domain's `dispatcher.py`.
3. Update `docs/source/supported-instruments/supported-instruments.mdx` — that
   page is the authoritative hardware list.

## Code of Conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Report concerns to
hello@lagerdata.com.

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0.

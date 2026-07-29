# Contributing to Lager

Thanks for your interest in contributing. This document covers how to get set up,
how the repository is laid out, and what CI checks before a pull request can merge.

## Getting Started

**Clone and install the CLI in development mode:**

```bash
git clone https://github.com/lagerdata/lager.git
cd lager/cli
pip install -e .
lager --version
```

Python 3.10 or newer is required. If you plan to run the box unit suites, also
install the test dependencies:

```bash
pip install -r test/requirements-unit.txt
```

## What CI Checks

Every pull request runs these workflows. Run the equivalents locally before
pushing to avoid a round trip.

| Workflow | What it does |
|----------|--------------|
| **Unit Tests** | The unit suites below, across the Python versions in `cli/setup.py` |
| **Static Checks** | Lint over the untested tree, including ShellCheck on `*.sh` |
| **Rust Checks** | `cargo fmt`, `clippy`, and a build of `box/oscilloscope-daemon` |

Integration, hardware, and nightly bench workflows run against real hardware and
are not triggered by ordinary pull requests.

### Unit tests (no hardware)

**Each suite needs its own pytest process** — they install conflicting import
stubs, so a single combined run gives wrong results. See `test/COVERAGE.md` for
the details.

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

### Shell lint

```bash
shellcheck $(git ls-files '*.sh')
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
3. **Add or update tests.** New behaviour without a test will be asked for one.
4. **Update `CHANGELOG.md`** for any user-facing change, under an `Unreleased`
   heading if no release is pending.
5. **Update the docs** in `docs/source/` if you changed a command, an API, or
   supported hardware.
6. **Fill in the PR template**, including how you tested.

Note that this is a **public repository**. Pull request titles, bodies, commit
messages, and code comments are world-readable and permanent — a squash merge
copies the PR body into git history, and editing a PR body leaves the original
visible in its revision history. Do not include customer names, private
deployment details, or internal discussion in anything you push.

### Code Style

- **Python** — follow PEP 8. Match the conventions of the file you are editing.
- **Bash** — must pass ShellCheck.
- **Rust** — `cargo fmt` and no new `clippy` warnings.
- **Commits** — write a clear imperative subject line explaining the change.
- **No emoji** in code, docs, commit messages, or PR text. Use `PASS`/`FAIL`,
  `[x]`/`[ ]`, "Supported"/"Not supported".
- **Copyright headers** on new files.

## Reporting Issues

Use [GitHub Issues](https://github.com/lagerdata/lager/issues) and the bug report
template. Search first — the issue may already be filed. A good report includes
the exact command, its full output, `lager --version`, and the box version from
`lager hello --box <name>`.

For security vulnerabilities, **do not open a public issue.** Follow
[SECURITY.md](SECURITY.md).

## Repository Layout

```
lager/
├── cli/                    # Command-line interface (includes deployment scripts)
├── box/                    # Box hardware control software and services
├── test/                   # Unit, API, and integration tests
├── tools/                  # Repository tooling (coverage checks, doc helpers)
└── docs/                   # Mintlify documentation source
```

### CLI (`cli/`)

A Python [Click](https://click.palletsprojects.com/) application, published to
PyPI as `lager-cli`.

```
cli/
├── main.py                 # Entry point - registers all commands
├── config.py               # Configuration management (~/.lager)
├── box_storage.py          # Box/instrument storage utilities
│
├── core/                   # Shared utilities
│   ├── net_helpers.py      # Net command helpers (resolve_box, run_net_py, etc.)
│   ├── param_types.py      # Custom Click parameter types
│   ├── utils.py            # General utilities
│   ├── ssh_utils.py        # SSH connection utilities
│   ├── matchers.py         # Pattern matching utilities
│   └── net_storage.py      # Net storage operations
│
├── context/                # Session and authentication management
│   ├── core.py             # LagerContext class
│   ├── session.py          # DirectIPSession, LagerSession, DirectHTTPSession
│   ├── error_handlers.py   # Docker, CANbus error handling
│   └── ci_detection.py     # CI environment detection
│
├── commands/               # Command modules, grouped by domain
│   ├── power/              # supply, battery, solar, eload
│   ├── measurement/        # adc, dac, gpi, gpo, scope, logic, thermocouple, watt
│   ├── communication/      # uart, i2c, spi, ble, blufi, wifi, usb
│   ├── development/        # debug/, arm, python, devenv
│   ├── box/                # hello, status/, boxes, instruments, nets, ssh, diagnose
│   └── utility/            # defaults, update, pip, webcam
│
├── impl/                   # Implementation scripts executed on the box
│   ├── power/              # supply.py, battery.py, solar.py, eload.py
│   ├── measurement/        # adc.py, dac.py, scope.py, etc.
│   ├── communication/      # uart.py, ble.py, wifi.py
│   └── device/             # usb.py, arm.py, hello.py, webcam.py
│
├── deployment/             # Box deployment, packaged with the CLI
│   ├── scripts/            # setup_and_deploy_box.sh, setup_ssh_key.sh, ...
│   └── security/           # secure_box_firewall.sh (UFW configuration)
│
└── vendor/                 # Vendored third-party libraries (PyCRC, elftools)
```

Additional deployment references (cloud-init, process guides) live in
`docs/reference/deployment/`.

### Box (`box/`)

Services and libraries that run on the bench hardware.

```
box/
├── lager/                  # Python package - the box services
│   ├── core.py             # Core utilities (Interface, Transport)
│   ├── cache.py            # Thread-safe NetsCache singleton
│   ├── constants.py        # Centralized configuration constants
│   ├── exceptions.py       # Unified exception hierarchy
│   ├── box_http_server.py  # Main Flask + WebSocket server
│   ├── hardware_service.py # Device session pool behind the :9000 API
│   │
│   ├── nets/               # Net framework - net.py, device.py, mux.py, mappers/
│   ├── dispatchers/        # Shared dispatcher infrastructure
│   ├── http_handlers/      # HTTP/WebSocket handlers (app, uart, supply, state)
│   │
│   ├── power/              # supply/, battery/, solar/, eload/
│   ├── io/                 # LabJack T7 - adc/, dac/, gpio/
│   ├── measurement/        # thermocouple/, watt/, scope/
│   ├── protocols/          # uart/, i2c/, spi/, ble/, wifi/
│   ├── automation/         # arm/, usb_hub/, webcam/
│   ├── debug/              # api.py, service.py - GDB/J-Link integration
│   ├── python/             # Remote Python execution service
│   ├── exec/               # Process spawning and output streaming
│   ├── util/               # device_lock.py and other shared helpers
│   ├── box_config/         # Declarative box configuration
│   ├── mcp/                # MCP server for AI agent integration (port 8100)
│   ├── docker/             # box.Dockerfile and container assets
│   └── instrument_wrappers/
│
├── oscilloscope-daemon/    # Rust WebSocket/WebTransport scope streaming
├── udev_rules/             # Device permission rules
└── start_box.sh            # Container entry point
```

Building the Rust daemon requires **Rust 1.85+** (edition 2024):

```bash
cd box/oscilloscope-daemon
cargo build --release
```

### Tests (`test/`)

```
test/
├── framework/              # harness.sh, colors.sh, test_utils.py, fixtures.py
├── assets/                 # Test data, and a placeholder for firmware
│                           # (binaries are excluded; see assets/firmware/README.md)
│
├── unit/                   # Unit tests - no hardware
│   ├── cli/                # CLI unit tests
│   ├── box/                # Box service unit tests
│   ├── measurement/        # Measurement unit tests
│   └── blufi/              # BluFi unit tests
├── mcp/unit/               # MCP server unit tests
│
├── api/                    # Python API tests - run on a box
│   ├── power/  io/  usb/  communication/  sensors/  peripherals/
│
├── integration/            # Bash integration suites - run against a box
│   ├── power/  io/  usb/  communication/  sensors/  infrastructure/
│
├── COVERAGE.md             # Suite inventory and per-suite test counts
└── CONVENTIONS.md          # Test authoring conventions
```

## Development Guidelines

### Adding a CLI command

1. Create the command module in `cli/commands/<category>/`.
2. Add an implementation script in `cli/impl/<category>/` if it runs on the box.
3. Register the command in `cli/main.py`.
4. Add unit tests in `test/unit/cli/`.
5. Document it in `docs/source/reference/cli/`.

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

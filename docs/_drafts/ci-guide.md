# Using Lager in CI

This guide sets up hardware-in-the-loop (HIL) testing in GitHub Actions, with the
GitHub Actions runner installed **on the Lager Box itself**.

It is written to be read in order. The first four sections get you to a working
workflow. Everything after that is what you add as the setup grows — sharing a
bench between jobs, flashing firmware built elsewhere, telling a flaky bench
apart from a real bug, and scaling from one bench to a fleet.

Throughout, `BENCH-1` is a box name, `my-firmware` is your repository, and `SWD`,
`UART`, `BATT`, `USB_CHARGE` are net names. Substitute your own.

---

## Contents

1. [Where the runner should live](#1-where-the-runner-should-live)
2. [Installing the Actions runner on the box](#2-installing-the-actions-runner-on-the-box)
3. [Preparing the runner account](#3-preparing-the-runner-account)
4. [Your first workflow](#4-your-first-workflow)
5. [Sharing one bench between jobs](#5-sharing-one-bench-between-jobs)
6. [Getting firmware onto the DUT](#6-getting-firmware-onto-the-dut)
7. [Making sure the box runs the code under test](#7-making-sure-the-box-runs-the-code-under-test)
8. [Leaving the bench safe](#8-leaving-the-bench-safe)
9. [Telling a flaky bench from a real bug](#9-telling-a-flaky-bench-from-a-real-bug)
10. [Nets: naming them so tests need no per-bench config](#10-nets-naming-them-so-tests-need-no-per-bench-config)
11. [Scaling to a fleet](#11-scaling-to-a-fleet)
12. [Results across retries](#12-results-across-retries)
13. [Gated fleets: signing in from CI](#13-gated-fleets-signing-in-from-ci)
14. [Reference](#14-reference)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Where the runner should live

A Lager command needs network access to a box. There are two ways to arrange
that in CI, and the choice shapes everything else.

### Runner on a separate host

A build machine (or a cloud runner over a VPN) runs the job and reaches the box
across the network. The job must install the CLI, sign in, and be told the box's
address:

```yaml
runs-on: [self-hosted, lager-bench]
steps:
  - run: pip install lager-cli==0.39.1
  - run: lager login "$AUTH_URL" --email "$EMAIL" --password "$PASSWORD"
  - run: lager boxes add --name BENCH-1 --ip "$BOX_IP" --user lagerdata --yes
  - run: lager python tests/hil --box BENCH-1
```

This works, and it is the right answer if one runner drives several boxes, or if
your boxes are deliberately kept off the CI network. But every job re-does setup,
the box's address and credentials become repository secrets, and nothing stops
two jobs on that runner from reaching for the same bench at once.

### Runner on the box (recommended)

Install the GitHub Actions runner directly on the Lager Box, and give it **one
label: the box's name**.

```yaml
runs-on: BENCH-1
env:
  LAGER_BOX: BENCH-1
steps:
  - uses: actions/checkout@v4
  - run: lager python tests/hil
```

That is the entire setup. What you gain:

- **The runner label *is* the bench.** `runs-on: BENCH-1` and `--box BENCH-1` are
  the same string, so there is no mapping to keep in sync and no way to schedule
  a job onto a runner that cannot reach the bench it wants.
- **Serialization for free.** A self-hosted runner takes one job at a time. Two
  unrelated branches targeting `BENCH-1` queue at the runner without any
  `concurrency:` configuration.
- **No secrets.** The CLI is already installed, `~/.lager` already lists the box,
  and any gateway session already lives in the runner account's home directory.
  A production fleet running this topology can reference zero Lager secrets —
  the only repository variables it needs are the runner labels themselves.
- **No network hop.** Commands travel over loopback.

What it costs:

- The box does checkout and artifact download, so it is busy for the whole job,
  not just the hardware part. Build elsewhere (see [§6](#6-getting-firmware-onto-the-dut)).
- Anything a workflow puts on the box's `PATH` is inside your bench's trust
  boundary. Treat the box as a production machine.
- One box, one concurrent job. If you need parallel test lanes, you need parallel
  benches.

The rest of this guide assumes the runner is on the box. Where a step exists only
to support the separate-host topology, it is called out.

---

## 2. Installing the Actions runner on the box

> **Verify this section against your own bench.** The steps below are GitHub's
> documented runner installation, constrained by the Lager Box's requirements.
> Confirm the service account and `PATH` on your first box before rolling it out.

Prerequisites, from the Lager Box requirements: **x86-64**, **Ubuntu 22.04 or
newer**, a reachable IP, and a login account with sudo. That account is
root-equivalent by design; Lager's installer grants it passwordless sudo for a
scoped set of commands only.

Run everything below **as the box's ordinary login account** — the same account
you passed to `lager install --user`. Do not install the runner as root.

### 2.1 Register the runner

Get a registration token from your repository at
`Settings → Actions → Runners → New self-hosted runner`, then on the box:

```bash
mkdir -p ~/actions-runner && cd ~/actions-runner
curl -o actions-runner-linux-x64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.322.0/actions-runner-linux-x64-2.322.0.tar.gz
tar xzf actions-runner-linux-x64.tar.gz

./config.sh \
  --url https://github.com/my-org/my-firmware \
  --token <REGISTRATION_TOKEN> \
  --name BENCH-1 \
  --labels BENCH-1 \
  --work _work \
  --unattended \
  --replace
```

**The label convention is the whole trick.** Give the runner exactly one custom
label equal to the box name. `--labels BENCH-1` *replaces* the default label set
in older runner versions and *adds* to it in newer ones; either way, referring to
the runner as `runs-on: BENCH-1` selects it, and `runs-on: [self-hosted, BENCH-1]`
works too if you prefer to be explicit.

Do not add a shared label like `lager-bench` unless you genuinely want any bench
to be able to pick up the job. Once two boxes share a label, a job can land on a
bench that lacks the nets it needs, and you have to re-introduce a mapping from
runner to box in YAML.

### 2.2 Install it as a service

```bash
sudo ./svc.sh install "$USER"
sudo ./svc.sh start
sudo ./svc.sh status
```

`svc.sh install "$USER"` writes a systemd unit that runs the service **as the
account you name**, not as root. Pass `$USER` explicitly — the runner account and
the Lager box account must be the same one, or the job will not see the box
account's `~/.lager`.

### 2.3 Confirm the job environment

The runner inherits its environment from the systemd unit, not from your
interactive login shell. If `lager` lives in `~/.local/bin` (which is where the
Lager installer symlinks it on the box), a login shell will find it and the
service may not.

Check it from an actual workflow run rather than from ssh:

```yaml
- name: Runner sanity
  run: |
    echo "runner:  $RUNNER_NAME"
    echo "user:    $(id -un)"
    echo "arch:    $(uname -m)"
    command -v lager || { echo "::error::lager not on PATH for the runner account"; exit 1; }
    lager --version
```

If `lager` is missing, add its directory to the runner's environment — create
`~/actions-runner/.env` with `PATH=/home/<user>/.local/bin:/usr/local/bin:/usr/bin:/bin`
and restart the service. The runner reads that file at startup.

### 2.4 Repeat per bench

Every box gets its own runner, its own name, and its own label. Nothing is shared
between them.

---

## 3. Preparing the runner account

Because the runner *is* the box account, this is one-time setup on the machine,
not per-job workflow steps.

### 3.1 The CLI

```bash
pip install --user 'lager-cli==0.39.1'
lager --version
```

Requirements and rules:

- **Python 3.10 or newer.** The CLI declares `python_requires>=3.10`.
- **Pin the version.** An unpinned CLI drifts with whenever the machine was last
  touched. Pin it in one place and bump it deliberately.
- **Keep the CLI at or above the box's version.** The CLI compares its own
  version to the box's on every command and warns on skew. `lager boxes` shows
  each box as `current`, `needs update`, or `newer`.
- **Never invoke bare `lager`.** With no subcommand it launches an interactive
  REPL, which in CI means a job that hangs until its timeout.

### 3.2 The box registry

The CLI resolves `--box` against the `BOXES` section of `~/.lager`, a JSON file:

```json
{
  "BOXES": {
    "BENCH-1": { "ip": "10.0.1.42", "user": "lagerdata", "version": "0.39.1" }
  },
  "DEFAULTS": { "gateway_id": "BENCH-1" }
}
```

Seed it on a new box by exporting from a machine that already has it:

```bash
# on your laptop
lager boxes export -o boxes.json

# on the box, as the runner account
lager boxes import boxes.json --merge --yes
```

Or add the single entry directly:

```bash
lager boxes add --name BENCH-1 --ip 10.0.1.42 --user lagerdata --yes
```

`--user` is the box's **SSH** account, used by `lager update`, `lager logs`,
`lager box-config`, and `lager ssh`. It has no default.

### 3.3 Which box a command targets

Resolution order, highest first:

1. the `--box` flag
2. the `LAGER_BOX` environment variable
3. `DEFAULTS.gateway_id` in `~/.lager`
4. error

Set `LAGER_BOX` once in the job's `env:` block and omit `--box` everywhere else.
Then the workflow reads the same on every bench, and a box rename is a one-line
change.

```yaml
jobs:
  hil:
    runs-on: BENCH-1
    env:
      LAGER_BOX: BENCH-1
```

If you would rather not repeat the name, put it in a repository variable and use
it for both:

```yaml
    runs-on: ${{ vars.HIL_BENCH }}
    env:
      LAGER_BOX: ${{ vars.HIL_BENCH }}
```

That indirection is worth it exactly once: when you move the suite to a different
bench, you change one variable instead of editing every workflow.

### 3.4 Two config-file footguns

- **`~/.lager` must be a JSON file, not a directory.** If something on the box
  creates `~/.lager/` as a directory (a virtualenv placed there, for instance),
  every CLI command on that machine fails while your laptop keeps working — the
  symptom is invisible from anywhere except the box.
- **Do not let a virtualenv shadow the CLI.** The CLI warns when the `lager` on
  `PATH` is not the one the active environment installed. Resolve it rather than
  ignoring it; the two copies can be different versions.

`LAGER_CONFIG_FILE_DIR` overrides the directory holding `~/.lager`, and
`LAGER_CONFIG_FILE_NAME` overrides the filename. Both are useful if the runner
account's home is not where you want config to live.

---

## 4. Your first workflow

```yaml
name: HIL

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  hil:
    name: Hardware tests
    runs-on: BENCH-1
    timeout-minutes: 30
    env:
      LAGER_BOX: BENCH-1
    steps:
      - uses: actions/checkout@v4

      - name: Bench reachable
        run: |
          for attempt in 1 2 3; do
            if lager hello; then exit 0; fi
            echo "::warning::lager hello failed (attempt ${attempt}/3); retrying"
            sleep 5
          done
          echo "::error::bench unreachable after 3 attempts"
          exit 1

      - name: Run the suite
        run: lager python tests/hil
```

Three things about this that are not obvious:

**`lager hello` is a reachability probe, not a health check.** It exits 0 even
when the box answers with an HTTP error; only a connection failure or timeout
makes it non-zero. Retry it — on a gateway-fronted box the first authenticated
command after a fresh sign-in can legitimately fail once (see
[§13](#13-gated-fleets-signing-in-from-ci)) — but do not read too much into a
zero exit.

**`lager python` ships your script to the box and runs it there.** The argument
is a file or a directory; a directory is uploaded as a module. The script runs
inside the box's Python container, not on the runner, which has consequences
covered in [§7](#7-making-sure-the-box-runs-the-code-under-test) and
[§15](#15-troubleshooting).

**`timeout-minutes` is a bench-occupancy budget.** A hung job holds the bench —
and its lock — until GitHub kills it. Set it to something you are willing to
wait, not to the default 360 minutes.

### Passing arguments to your test

Everything after `--` goes to your script; everything before it is for the CLI.

```yaml
- run: lager python tests/hil/charge -- --target-soc 80 --timeout-min 45
```

### Sending files along

`lager python` uploads the script (and its directory), not arbitrary files.
Anything else — a firmware image, a debugger script, a limits table — needs
`--add-file`, and arrives next to the script by basename:

```yaml
- run: |
    lager python tests/hil/flash \
      --add-file ./artifacts/firmware.hex \
      --add-file tools/debug/target.script \
      -- --image firmware.hex
```

### Getting files back

```yaml
- run: lager python tests/hil --download results.json --allow-overwrite
```

Downloads happen after the script finishes, not while it runs.

---

## 5. Sharing one bench between jobs

A bench is a single physical resource. Three independent mechanisms keep jobs off
each other's toes, and they solve different problems. Understand all three before
reaching for any of them.

### Layer 1: the runner

A self-hosted runner accepts one job at a time. With one runner per box, this
alone serializes every job that targets that bench — across branches, across
workflows, across repositories. **You get this for free and it is usually
enough.** Unrelated work queues instead of colliding.

What it does not do: it queues rather than supersedes. Five pushes to a branch
produce five queued jobs, and the bench works through all of them.

### Layer 2: workflow concurrency

Use `concurrency:` when you want a newer run to *replace* a queued or in-flight
older one.

```yaml
concurrency:
  group: hil-BENCH-1-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: true
```

Two details that matter:

**Declare it on the job that holds the bench**, not at workflow level. A
workflow-level group does not propagate into called workflows, so the job that
actually occupies the hardware ends up outside the group you meant to put it in.

**Key it on the PR number, with `run_id` as the fallback.** A tempting key like
`github.head_ref || github.ref_name` collides: a `workflow_dispatch` on a
branch that also has an open PR produces the same string as that PR's runs, and
the manual run and the PR run cancel each other. `run_id` is unique per run, so
non-PR runs each get a group of their own — a dispatch can neither be
interrupted by a PR run nor stomp one.

**`cancel-in-progress: true` is only safe if you clean up on cancel.** Killing a
job mid-test can leave a supply energized, a battery simulator at a critical
voltage, or a debug probe isolated. Pair it with the cleanup step in
[§8](#8-leaving-the-bench-safe). For nightly and post-merge runs, prefer
`cancel-in-progress: false` — a run mid-measurement should finish.

### Layer 3: Lager's own lock

Lager locks the box automatically. **There is no `--lock`, `--lock-wait`, or
`--no-lock` flag** — the behaviour is automatic and tuned by environment
variables.

**The lock holder is CI-aware.** Under GitHub Actions the holder string is:

```
ci:github:<repo>#<run_id>-<attempt>/<job>@<runner>:<pid>
```

It always ends in `:<pid>`, so two matrix jobs can never accidentally share an
identity. `lager boxes` renders it readably — `github my-org/my-firmware run 9182
job hil on BENCH-1`.

**Collision behaviour depends on whether Lager thinks it is in CI**, and that
detection hinges on `CI=true` plus a provider marker like `GITHUB_RUN_ID`:

| Environment | On collision |
|---|---|
| CI | waits, polling every 2 s, up to `LAGER_LOCK_WAIT` (default **1800 s**) |
| Developer machine | prints an error and exits **immediately** |

GitHub Actions sets `CI` and `GITHUB_RUN_ID` for every `run:` step, so jobs get
queueing semantics automatically. **A `lager` command invoked on the same box
outside an Actions step — from cron, a systemd unit, or an ssh session — is not
in CI and gets fail-fast semantics.** This surprises people who put a maintenance
script on the box and find it dying instantly whenever CI is running.

**A lock collision exits 1, which is indistinguishable from any other error.**
If you need to branch on it, match the stderr text `is locked by`.

Lock lifetime is bounded by a server-side TTL (default 1800 s) refreshed by a
60 s heartbeat. The TTL does not cap how long your test may run — the heartbeat
keeps renewing it — it bounds how long a stale lock survives after the CLI
crashes.

Add a release as a safety net, and **never force it**:

```yaml
      - name: Release the bench lock
        if: always()
        run: lager boxes unlock --box "$LAGER_BOX" 2>/dev/null || true
```

`lager boxes unlock --force` overrides a lock held by someone else. A lock still
held after your job ends belongs to another caller — a colleague at the bench, a
queued CI run — and forcing it takes the bench out from under them. Leave a stale
lock for its TTL to reap and for your next run to report.

### Which commands take the lock

Auto-locking: `lager python`, and the instrument commands `adc`, `dac`, `gpi`,
`gpo`, `thermocouple`, `watt`, `energy`, `scope`, `logic`, `spi`, `i2c`, `uart`,
`usb`, `wifi`, `ble`, `blufi`, `router`, `supply`, `battery`, `eload`, `solar`,
`debug`, `arm`, `webcam` — plus the admin commands `install`, `uninstall`,
`update`, `install-wheel`.

Not locked, and therefore safe to run against a busy bench: `lager hello`,
`lager boxes`, `lager nets` (all subcommands), `lager instruments`,
`lager defaults`, `lager logs`, `lager binaries`, `lager dut`, `lager ssh`,
`lager login` / `logout` / `whoami`, `lager exec`, `lager devenv`.

That split is what makes a `lager hello` probe job and a `lager nets state`
inventory step safe even while another run holds the bench.

### Escape hatches

| Variable | Effect |
|---|---|
| `LAGER_LOCK_WAIT` | seconds to wait on collision (CI default 1800, dev default 0) |
| `LAGER_LOCK_TTL` | server-side TTL; `none` for a lock that never expires |
| `LAGER_LOCK_HEARTBEAT` | refresh interval, default 60 |
| `LAGER_LOCK_HOLDER` | override the holder identity string |
| `LAGER_AUTO_LOCK_DISABLE=1` | skip auto-locking entirely |

`LAGER_AUTO_LOCK_DISABLE` is for a single-user bench where locking is pure
overhead. Do not use it to work around a collision.

One more: `lager python --detach` transfers lock ownership to the box, which
keeps the bench locked for the lifetime of the detached job — after your workflow
step has already returned.

---

## 6. Getting firmware onto the DUT

### Build somewhere else

The bench runner should not build your firmware. A build occupies the bench for
its whole duration while touching no hardware, and a bench is the scarcest
resource you have.

Split it: build on a hosted (or general-purpose self-hosted) runner, publish the
image as an artifact, and have the bench job download it.

```yaml
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      artifact: ${{ steps.meta.outputs.name }}
    steps:
      - uses: actions/checkout@v4
      - run: ./build.sh
      - id: meta
        run: echo "name=firmware-${{ github.sha }}" >> "$GITHUB_OUTPUT"
      - uses: actions/upload-artifact@v4
        with:
          name: firmware-${{ github.sha }}
          path: build/firmware.hex
          if-no-files-found: error

  hil:
    needs: build
    runs-on: BENCH-1
    env:
      LAGER_BOX: BENCH-1
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.sha }}

      - uses: actions/download-artifact@v4
        with:
          name: ${{ needs.build.outputs.artifact }}
          path: ./firmware

      - run: lager debug SWD flash --hex ./firmware/firmware.hex
      - run: lager debug SWD reset
      - run: lager python tests/hil
```

### Pin the checkout to `github.sha`

On a `pull_request` event, the default checkout resolves the floating
`refs/pull/<n>/merge` ref. If a push lands while the bench job is starting, the
job runs **newer test code against older firmware** and reports the result under
the original commit. Passing `ref: ${{ github.sha }}` makes both halves come from
the same commit.

### Verify that the flash actually happened

A programming toolchain can report a fatal connection error and still let the
flash command exit 0 — leaving the device erased but not reprogrammed, which then
fails much later as a confusing test failure. Do not trust the exit code alone;
capture the output and fail on the markers your toolchain emits.

```yaml
- name: Flash
  run: |
    set -o pipefail
    log=$(mktemp)
    lager debug SWD flash --hex ./firmware/firmware.hex 2>&1 | tee "$log"
    if grep -qE 'Cannot power up debug port|Could not connect to the target device'; then
      echo "::error title=flash::the programmer reported a fatal error but the command returned success. The DUT is likely erased but not reprogrammed."
      exit 1
    fi
    lager debug SWD reset
```

Adjust the pattern to your programmer's actual failure strings. The principle is
the point: a flash step should assert its own success.

### Flashing from inside the test instead

Some suites program the device as their first test case, using the Python API on
the box, rather than as a separate CLI step. That is fine and sometimes better —
the test that programs the device is also the test that verifies programming
works. If you do this, note that the Python `DebugNet` methods return the
programmer's output as a **string** and do not raise on a programmer-reported
failure, so the same "assert your own success" rule applies inside the test.

### The debug subcommands that exist

```
lager debug [NET] gdbserver     # attach; --rtt to stream RTT
lager debug [NET] flash         # --hex / --elf / --bin ADDR
lager debug [NET] erase
lager debug [NET] reset
lager debug [NET] memrd
lager debug [NET] status
lager debug [NET] health
lager debug [NET] disconnect
```

There is no separate `connect` step — connection happens implicitly inside
`flash`, `reset`, and the rest. There is no `lager debug <net> rtt`; RTT is
`gdbserver --rtt`.

---

## 7. Making sure the box runs the code under test

This is the single most common source of misleading CI results, and it follows
directly from how `lager python` works.

**`lager python` ships your script to the box and executes it there.** The runner
contributes the script; the box contributes the Python environment, the
instrument drivers, the net definitions, and the Lager box code itself. So a
change to anything that lives on the box is *not* exercised by checking out your
branch — the box is still running whatever version was last deployed to it.

If your repository contains only test scripts, this does not affect you: the
scripts come from the checkout, and you are done.

If your CI also exercises box-side code, gate the run on the box being on the
right version:

```yaml
- name: Verify the box is running the ref under test
  run: |
    rc=0
    out=$(lager update --check --box "$LAGER_BOX" --version "$GITHUB_SHA" 2>&1) || rc=$?
    echo "$out"
    if [ "$rc" -gt 1 ]; then
      echo "::error title=box state::could not determine box state (exit ${rc})"
      exit "$rc"
    fi
    if [ "$rc" -eq 1 ]; then
      echo "::error title=box version::the box is not running ${GITHUB_SHA}"
      exit 1
    fi
```

`lager update --check` is a dry run that reports what *would* change without
touching the box. Its exit code is a tri-state:

| Exit | Meaning |
|---|---|
| 0 | already in sync — nothing to do |
| 1 | would update (code, dependencies, or a stopped container) |
| 2 | could not determine state (network error, SSH not configured) |

Distinguishing 1 from 2 matters: exit 1 is a real "your box is stale" verdict,
while exit 2 means the check itself did not run and you have learned nothing.

To actually move a box to a ref before dispatching a run:

```bash
lager update --box BENCH-1 --version main --yes
lager update --box BENCH-1 --version v0.39.1 --yes
```

`--version` accepts a release tag, a semver (with or without a leading `v`,
including pre-release suffixes), a branch name, or a full 40-character commit
SHA; the default is `main`. Other flags: `--force` (rebuild even when up to
date), `--pull` / `--no-pull` (fetch a prebuilt image versus building on the box),
`--verbose`, `--yes`.

`lager update` handles one box per invocation. Loop in your shell for several.

### Provisioning the box's Python dependencies

Your test scripts run inside the box's container, so their imports must be
installed there — not on the runner. Use the declarative box config, which
persists across container restarts and box updates:

```bash
lager box-config pip add pyserial rich --box BENCH-1
lager box-config pip list --box BENCH-1
```

Export and import it to keep a fleet identical:

```bash
lager box-config export --box BENCH-1 -o bench-config.json
lager box-config import bench-config.json --box BENCH-2
```

Check that file into your repository. It is the only record of what your benches
are expected to have.

---

## 8. Leaving the bench safe

A HIL job that dies partway through does not leave a clean workspace behind — it
leaves *hardware* in whatever state the test had it in. A supply still enabled at
an out-of-range voltage, a load still sinking current, a heater still on, an
enable line still asserted. The next job inherits that, and so does the next
person who walks up to the bench.

Two steps bracket every hardware job.

### Bring-up, before anything else

```yaml
- name: Bench bring-up
  run: ./tools/bench.sh bring-up
```

Put it in its own step rather than folding it into the flash step. A cold bench —
supply off, enable lines low, an instrument holding its output path open until a
session configures it — leaves the DUT unpowered, and an unpowered DUT presents
at the *flash* step as "cannot connect to the target", which reads as a dead
debugger. Separating them means the step that failed is the step that names the
cause.

### Cleanup, on cancel and on failure

```yaml
- name: Bench cleanup
  if: cancelled() || failure()
  run: |
    exec < /dev/null          # any interactive prompt sees EOF instead of hanging
    rc=0
    ./tools/bench.sh bring-up --recover || rc=$?
    lager debug SWD reset || rc=$?
    if [ "$rc" -ne 0 ]; then
      echo "::error title=cleanup::exited ${rc}; ${LAGER_BOX} may be unsafe for the next job"
      exit 1
    fi
```

Rules for that step:

- **`if: cancelled() || failure()`**, not `if: always()` — a passing run should
  end through its own teardown, and a cleanup that also runs on success hides
  bugs in that teardown.
- **Close stdin.** Any subcommand that falls through to an interactive `[y/N]`
  prompt will otherwise hang until the job timeout.
- **Keep going past a failure.** A dead instrument should not skip the rest of
  the cleanup.
- **Restore, do not unlock.** The lock is released by the run that took it. A
  lock still held afterwards belongs to somebody else, and `unlock --force` steals
  their bench. Leave it.
- **Assume no privileges.** The runner account has scoped sudo at most. Anything
  cleanup needs must work without a password prompt.

### Make cancellation actually reach your test

If your test runs under a shell wrapper, a `SIGTERM` from the runner on cancel
goes to the shell, which does not forward it. The test keeps running until
GitHub's hard kill, doing more hardware work after you asked it to stop.

Replace the shell with the test process:

```yaml
- run: exec ./tools/run-suite.sh --box "$LAGER_BOX"
```

`exec` means the signal lands on the thing you wanted to interrupt.

---

## 9. Telling a flaky bench from a real bug

The most valuable thing a HIL suite can do, after finding bugs, is to be honest
about when it did not actually test anything. A probe that failed to enumerate, a
debug session that never came up, a box that was briefly unreachable — none of
those are firmware defects, and retrying them is correct. A wrong chip ID or a
device that will not boot *is* a defect, and retrying it hides the bug you built
the bench to find.

Encode that difference in the exit code.

### The contract

Adopt this convention in your tests:

| Exit | Meaning | Retry? |
|---|---|---|
| **0** | Pass | — |
| **1** | Real device failure — wrong ID, image mismatch, no boot, out-of-spec measurement | **Never** |
| **2** | Infrastructure — probe, net, connection, or bench setup could not run the test | **Yes** |

Nothing in Lager enforces this; it is a convention your test scripts implement
and your CI acts on. `lager python` passes your script's exit code through
unchanged, which is what makes it work.

`lager python` contributes a few codes of its own on top:

| Exit | Meaning |
|---|---|
| 124 | `--timeout` expired; SIGTERM sent |
| 137 | `--timeout` expired; SIGKILL sent |
| 255 | could not retrieve the exit code from the box (reported as `-1`) |
| 130 | interrupted |

Treat 124, 137, and 255 as infrastructure.

### A retry wrapper

Save this as `tools/retry-hil.sh`. It retries only infrastructure failures,
power-cycles the probe and the DUT between attempts, and converts a hang into a
retryable failure.

```bash
#!/bin/bash
#
# Retry a HIL test, recovering the bench between attempts.
#
#   retry-hil.sh -- lager python tests/hil/flash --add-file firmware.hex
#
# Exit-code contract: 0 pass / 1 device failure / 2 infrastructure.
# Only infrastructure is retried. Exit status is the last attempt's code.
#
# Environment:
#   LAGER_BOX             target box (required)
#   HIL_RETRY_ATTEMPTS    total attempts, default 3
#   HIL_ATTEMPT_TIMEOUT   per-attempt seconds, default 300 (0 disables)
#   HIL_PROBE_NET         debug-probe USB net to cycle, default USB_DEBUG
#   HIL_PROBE_SETTLE      seconds to wait after re-enabling it, default 8
#   HIL_POWER_CYCLE_DUT   also power-cycle the DUT, 1/0, default 1
#   HIL_DUT_VBUS_NET      DUT USB/VBUS net, default USB_CHARGE
#   HIL_DUT_POWER_NET     DUT supply/battery net, default BATT
#   HIL_DUT_SETTLE        seconds to wait after the board reboots, default 3

set -u

[ "${1:-}" = "--" ] && shift
if [ "$#" -eq 0 ]; then
  echo "retry-hil.sh: no command given" >&2
  exit 2
fi

BOX="${LAGER_BOX:?retry-hil.sh: LAGER_BOX must be set}"
ATTEMPTS="${HIL_RETRY_ATTEMPTS:-3}"
ATTEMPT_TIMEOUT="${HIL_ATTEMPT_TIMEOUT:-300}"
PROBE_NET="${HIL_PROBE_NET:-USB_DEBUG}"
PROBE_SETTLE="${HIL_PROBE_SETTLE:-8}"
POWER_CYCLE_DUT="${HIL_POWER_CYCLE_DUT:-1}"
DUT_VBUS_NET="${HIL_DUT_VBUS_NET:-USB_CHARGE}"
DUT_POWER_NET="${HIL_DUT_POWER_NET:-BATT}"
DUT_SETTLE="${HIL_DUT_SETTLE:-3}"

# Wrap each attempt in `timeout` so a HANG becomes a retryable 124 rather than
# stalling until the job's own timeout. Degrade gracefully where it is absent.
if [ "$ATTEMPT_TIMEOUT" -gt 0 ] 2>/dev/null && command -v timeout >/dev/null 2>&1; then
  run_attempt() { timeout "$ATTEMPT_TIMEOUT" "$@"; }
else
  run_attempt() { "$@"; }
fi

# Explicit disable-then-enable, never `toggle`: the probe must end powered ON
# regardless of the state the failed attempt left it in.
power_cycle_probe() {
  echo "  - power-cycling ${PROBE_NET}" >&2
  lager usb "$PROBE_NET" disable --box "$BOX" || true
  sleep 2
  lager usb "$PROBE_NET" enable --box "$BOX" || true
  sleep "$PROBE_SETTLE"
}

# Re-enumerating the probe cannot wake a sleeping target; only cutting board
# power does. Restore VBUS LAST so the board cold-boots with VBUS present.
# Every command is best-effort: a net this bench does not have simply no-ops.
power_cycle_dut() {
  [ "$POWER_CYCLE_DUT" = "1" ] || return 0
  echo "  - power-cycling the DUT" >&2
  lager usb "$DUT_VBUS_NET" disable --box "$BOX" >/dev/null 2>&1 || true
  lager supply  "$DUT_POWER_NET" disable --yes --box "$BOX" >/dev/null 2>&1 \
    || lager battery "$DUT_POWER_NET" disable --yes --box "$BOX" >/dev/null 2>&1 || true
  sleep 2
  lager supply  "$DUT_POWER_NET" enable --yes --box "$BOX" >/dev/null 2>&1 \
    || lager battery "$DUT_POWER_NET" enable --yes --box "$BOX" >/dev/null 2>&1 || true
  lager usb "$DUT_VBUS_NET" enable --box "$BOX" >/dev/null 2>&1 || true
  sleep "$DUT_SETTLE"
}

# A box-connection failure exits 1 -- the same code a real device verdict uses --
# but it is infrastructure. Detect it by message. Keep the pattern tight so a
# genuine device failure is never masked.
CONN_FAIL_RE='Timed out connecting to the box|did not respond in time|Failed to connect|Connection refused|Could not connect'

# A lock collision also exits 1 and is likewise not a device verdict.
LOCK_RE='is locked by'

out="$(mktemp "${TMPDIR:-/tmp}/retry-hil.XXXXXX")"
trap 'rm -f "$out"' EXIT

rc=2
for attempt in $(seq 1 "$ATTEMPTS"); do
  echo "=== attempt ${attempt}/${ATTEMPTS}: $* ===" >&2
  run_attempt "$@" 2>&1 | tee "$out"
  rc=${PIPESTATUS[0]}

  [ "$rc" -eq 0 ] && exit 0

  if [ "$rc" -eq 1 ]; then
    if grep -qiE "$CONN_FAIL_RE|$LOCK_RE" "$out"; then
      echo "::warning::exit 1 but the output shows a box connection or lock failure; treating as infrastructure" >&2
    else
      echo "::error::device failure (exit 1); not retrying" >&2
      exit 1
    fi
  fi

  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    echo "::warning::exit ${rc} (infrastructure) on attempt ${attempt}/${ATTEMPTS}; recovering and retrying" >&2
    power_cycle_probe
    power_cycle_dut
  fi
done

echo "::error::still failing (exit ${rc}) after ${ATTEMPTS} attempts" >&2
exit "$rc"
```

Use it around each hardware step:

```yaml
- name: Flash and verify
  run: |
    bash tools/retry-hil.sh -- \
      lager python tests/hil/flash --add-file ./firmware/firmware.hex
```

The two non-obvious pieces are the `timeout` wrapper — without it a hang is not
retryable, it just consumes the whole job budget — and the message-matching
reclassification of exit 1, since a box that briefly went away reports the same
code as a device that failed.

---

## 10. Nets: naming them so tests need no per-bench config

### Nets live on the box, not in your repository

Net definitions are stored on the Lager Box and survive reboots. Your repository
cannot define them; it can only declare what it *requires* and fail clearly when
a bench does not have it.

That is the right split, but it means bench configuration is invisible to code
review unless you deliberately make it visible. Two things help.

**Provision from a checked-in file.** `lager nets add-batch` takes a JSON file of
net definitions:

```bash
lager nets add-batch bench/nets.json --box BENCH-1
```

Keep `bench/nets.json` in the repository. It is the difference between a bench
you can rebuild and a bench somebody configured once by hand.

**Inventory the bench in the job.** `lager nets state --json` is machine-readable
and does not take the lock, so a preflight step can assert the bench has what the
suite needs — and say which net is missing, rather than failing later inside a
test with a stack trace.

### Name nets by function, because nothing else records it

A net is `{name, role, instrument, channel, address}`. The **role** is its
*type* — `usb`, `gpio`, `uart`, `debug`, `power-supply`, `battery`, `adc`, and so
on — and the box stores one default net per role. There is no "function" field.

So the moment a bench has two nets of the same role, **the name is the only thing
recording what each one is for**. Two USB ports are both role `usb`; only the
name says which one charges the device and which one powers the debug probe.

A convention that has held up:

1. **Prefix ambiguous roles with the role, then the function:** `USB_CHARGE`,
   `USB_DEBUG`, `UART_CONSOLE`, `ADC_VBUS`, `GPIO_NRST`. Name and role must
   agree, so a miswired bench is detectable.
2. **Name power nets by instrument, not function:** `BATT` for a battery-emulator
   net, `SUPPLY` for a programmable-supply net. The role and the name then carry
   the same information on purpose.
3. **Single-instance roles keep the bare name:** `SWD` for the one debug probe,
   `UART` for the one console.

Roles that routinely need the discipline: `usb` (a hub port's purpose is entirely
name-encoded — the hub only enables and disables), `gpio` (every pin is role
`gpio`), `uart`, and the measurement roles `adc` / `dac` / `scope` / `logic` /
`thermocouple` / `watt-meter`.

The payoff is that a test can find the charge port on any conforming bench
without per-bench configuration, and adding a second bench costs nothing.

### Do not rebind a shared net from CI

`lager nets set-script` changes the net's stored configuration **for everyone**.
A CI job that sets a debug script to suit itself leaves the bench that way for
the next user, who may need a different one.

Hand the script to the run instead, and apply it in-process:

```yaml
- run: |
    lager python tests/hil/flash \
      --add-file tools/debug/halt-first.script \
      -- --script halt-first.script
```

Same for any other per-run override. The rule: **a CI job should leave the bench's
net configuration exactly as it found it.**

### Useful net commands

```bash
lager nets --box BENCH-1                    # list (also: lager nets list)
lager nets show USB_CHARGE --box BENCH-1 --json
lager nets state --box BENCH-1 --json       # machine-readable inventory
lager nets add NAME ROLE CHANNEL ADDRESS --box BENCH-1
lager nets add-batch nets.json --box BENCH-1
lager nets add-all --box BENCH-1 --yes      # auto-generate from connected instruments
lager nets delete NAME ROLE --box BENCH-1 --yes
lager nets describe NAME -p "what it is for" --box BENCH-1
```

Note it is `lager nets` (plural) and `delete` (not `remove`). None of these take
the box lock.

---

## 11. Scaling to a fleet

One bench needs none of this. Several benches — especially benches that are not
identical — need a way to answer three questions in code: which benches exist,
what each one can do, and which tests can run where.

### Declare bench capability, not bench identity

Group benches into **roles**. A role names the nets a bench must have, and any
capability it provides that does not correspond to a single named net.

`bench/roles.toml`:

```toml
# Each role declares the nets a bench of that role must have. `capabilities`
# covers bench abilities that are not a single net -- different benches can
# realize the same capability through different topologies, and a test that
# needs it cares only that the effect is available, not how it is wired.

[standard]
nets = ["UART", "SWD", "USB_CHARGE", "USB_DEBUG", "BATT"]

[power]
nets = ["UART", "SWD", "USB_CHARGE", "USB_DEBUG", "BATT"]
capabilities = ["current_measurement"]

[supply-fed]
# Battery rail driven by a bench supply rather than a charger, so no charge
# port. Tests that would charge must reach the same state another way.
nets = ["UART", "SWD", "USB_DEBUG", "SUPPLY"]
```

A test declares what it needs — required nets by the net options it takes, and
`REQUIRED_CAPABILITIES = ["current_measurement"]` at module level for the rest —
and the runner routes it only to roles that satisfy it.

The resulting decision, per test per bench:

| Condition | Verdict |
|---|---|
| Required capability not in this role | **N/A** — another role runs it |
| Required net not in this role | **N/A** |
| Matched by this bench's quarantine list | **QUARANTINED** |
| Required net in the role but missing on the bench | **FAIL** — the role is misconfigured |
| Everything satisfied | **RUN** |

The distinction between the third-to-last and second-to-last rows is worth the
effort. "This test does not apply here" and "this bench is broken" look identical
if you only have pass and fail.

### One file per bench

`bench/boxes/BENCH-1.yml`:

```yaml
role: standard
enabled: true
quarantine:
  - reason: "BENCH-1's supply collapses under load; the DUT browns out mid-test"
    nets: [SUPPLY]
  - reason: "actuator strikes too softly to register on this bench"
    tests: [gesture_stress, double_tap]
```

`enabled: false` takes a bench out of rotation in a reviewable one-line diff
instead of an edit to a workflow file. The quarantine list is how a known-bad
bench stops producing noise without anybody disabling the test globally.

### Generate the matrix

```yaml
jobs:
  matrix:
    runs-on: ubuntu-latest
    outputs:
      matrix: ${{ steps.gen.outputs.matrix }}
      empty: ${{ steps.gen.outputs.empty }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.sha }}   # pin: the fleet is the one this commit declares
      - id: gen
        run: |
          set -euo pipefail
          rows=()
          for f in bench/boxes/*.yml; do
            name=$(basename "$f" .yml)
            enabled=$(yq -r '.enabled' "$f")
            [ "$enabled" = "true" ] || continue
            role=$(yq -r '.role' "$f")
            rows+=("$(jq -nc --arg n "$name" --arg r "$role" '{name:$n, role:$r}')")
          done
          if [ "${#rows[@]}" -eq 0 ]; then
            echo "empty=true" >> "$GITHUB_OUTPUT"
            echo "matrix={\"include\":[]}" >> "$GITHUB_OUTPUT"
            exit 0
          fi
          echo "empty=false" >> "$GITHUB_OUTPUT"
          printf '%s\n' "${rows[@]}" | jq -sc '{include: .}' \
            | sed 's/^/matrix=/' >> "$GITHUB_OUTPUT"

  validate:
    runs-on: ubuntu-latest
    needs: matrix
    steps:
      - run: |
          if [ "${{ needs.matrix.outputs.empty }}" = "true" ]; then
            echo "::error title=no benches::no enabled entries in bench/boxes/."
            exit 1
          fi

  hil:
    needs: [build, matrix]
    if: needs.matrix.outputs.empty != 'true'
    name: ${{ matrix.name }} (${{ matrix.role }})
    runs-on: ${{ matrix.name }}
    timeout-minutes: 90
    strategy:
      fail-fast: false
      matrix: ${{ fromJson(needs.matrix.outputs.matrix) }}
    env:
      LAGER_BOX: ${{ matrix.name }}
    concurrency:
      group: hil-${{ matrix.name }}-${{ github.event.pull_request.number || github.run_id }}
      cancel-in-progress: true
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.sha }}
      # ... download firmware, flash, run suite, clean up
```

Four details:

- **`fail-fast: false`.** One bad bench should not cancel the others; you want
  the whole fleet's verdict.
- **The separate `validate` job.** An empty matrix silently expands to zero jobs
  and the workflow *succeeds*. Failing loudly in its own job means "nobody ran
  anything" cannot be mistaken for "everything passed".
- **Job name.** GitHub treats `/` as job-path hierarchy and several views show
  only the leaf segment, so `BENCH-1 / standard` degrades to `standard` and you
  lose the bench. Keep both in one parenthesised segment.
- **Per-bench concurrency groups** run different benches in parallel while
  superseding older runs on the same one.

### Gate on the merged result

Each bench reports only what it ran. A test that is `N/A` on *every* bench — a
new test nobody routes to, or one whose required net is in no role — produces a
green fleet and zero coverage.

Add a gate job on a hosted runner that merges every bench's results:

```yaml
  gate:
    name: HIL Gate
    runs-on: ubuntu-latest
    needs: [matrix, validate, hil]
    if: always()
    steps:
      - name: No bench failed
        run: |
          r='${{ needs.hil.result }}'
          if [ "$r" = "failure" ] || [ "$r" = "cancelled" ]; then
            echo "::error::one or more bench jobs failed or were cancelled"
            exit 1
          fi

      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        continue-on-error: true
        with:
          pattern: hil-results-*
          path: ./results

      - name: Every test passed somewhere
        run: python3 tools/hil_coverage.py --results ./results --tests tests/hil
```

Make that gate job the required status check for branch protection, not the
individual bench jobs — the set of bench jobs changes whenever you enable or
disable a bench, and a required check that keeps disappearing is worse than none.

Workflow annotations are single-line and cannot render tables, so write the
fleet-wide view — tests as rows, benches as columns — to `$GITHUB_STEP_SUMMARY`
from the gate job, and suppress the per-bench summaries so there is one place to
look.

### Say what you skipped

If a run bounds its own coverage — a quarantine, a disabled bench, a skip cache,
a top-N cap — log it. Silent truncation reads as "covered everything" when it did
not, and that is the failure mode a HIL suite can least afford.

---

## 12. Results across retries

"Re-run failed jobs" wipes the workspace. If your suite skips tests that already
passed, the state recording those passes has to survive that.

**`actions/cache` cannot do this.** A save from attempt N is stored under the
current `run_id`, and a restore in attempt N+1 of the *same* run never sees it.
Cache keys are immutable, so `restore-keys` cannot usefully fall back either.

**The previous attempt's uploaded artifact can.** Upload results on every attempt
with `overwrite: true`, and restore them when `github.run_attempt > 1`:

```yaml
      - name: Restore results from the previous attempt
        if: github.run_attempt > 1
        continue-on-error: true
        uses: actions/download-artifact@v4
        with:
          name: hil-results-${{ matrix.name }}
          path: .test_state

      # ... run the suite ...

      - name: Stage results
        if: always()
        id: stage
        run: |
          stage="${RUNNER_TEMP}/hil-state"
          rm -rf "$stage" && mkdir -p "$stage"
          for f in results.json meta.json; do
            [ -f ".test_state/$f" ] && cp ".test_state/$f" "$stage/$f"
          done
          echo "dir=$stage" >> "$GITHUB_OUTPUT"

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: hil-results-${{ matrix.name }}
          path: ${{ steps.stage.outputs.dir }}
          overwrite: true
          retention-days: 7
```

Two things that bite here:

**Use `$RUNNER_TEMP`, not `/tmp`.** On a self-hosted runner `/tmp` persists
between jobs. A leftover `results.json` from an earlier run will be picked up and
uploaded even when this run never produced one — and the next attempt then skips
tests on the strength of stale passes. `$RUNNER_TEMP` is emptied at job start and
end.

**Carry the invalidation key with the results.** Store alongside them whatever
identifies the firmware they describe — a version string, a content hash — and
discard the whole set when it disagrees with what this attempt actually flashed.
Without that, a retry with a different image happily reports the old image's
passes.

Upload on failure too (`if: always()`). Partial results from a run that died
halfway are exactly what the next attempt and the gate job need.

---

## 13. Gated fleets: signing in from CI

Boxes behind an access gateway need a session. Boxes without one need nothing —
skip this section.

With the runner on the box, sign in **once on the machine**, not in every job:
the session is stored in the runner account's home directory and refreshes
itself.

```bash
# once, as the runner account
lager login https://gateway.example.com
lager whoami
```

If you would rather have CI establish it — for a separate-host runner, or a box
that is periodically re-imaged — do it non-interactively:

```yaml
      - name: Sign in
        env:
          AUTH_URL: ${{ vars.LAGER_AUTH_URL }}
          CI_EMAIL: ${{ secrets.LAGER_CI_EMAIL }}
          CI_PASSWORD: ${{ secrets.LAGER_CI_PASSWORD }}
        run: lager login "$AUTH_URL" --email "$CI_EMAIL" --password "$CI_PASSWORD"
```

Those three names are yours, not variables the CLI reads. Notes:

- **Read the password from a secret, never a literal.** A command-line password
  is visible in process listings on the box.
- **Use a dedicated CI account with MFA disabled.** `--email` and `--password`
  alone cannot satisfy an MFA prompt, and the command will sit waiting for input.
- The session lives in `~/.lager_gateway_auth`, mode 0600.
  `LAGER_GATEWAY_AUTH_FILE` relocates it.
- **The Python CLI has no pinned-token environment variable.** A bearer token
  such as `LAGER_GATEWAY_TOKEN` applies to the Rust SDK, not to `lager`. Sign in
  with `lager login`.

### The first-contact retry

On a gated box, the *first* authenticated command after a fresh sign-in can fail
once while the box-to-auth-server association is recorded, then succeed on the
next call. This is why the reachability probe retries:

```yaml
      - run: lager hello || lager hello
```

Keep that retry even if you have never seen the failure. It costs one redundant
call and removes a class of first-run-of-the-day flake.

---

## 14. Reference

### Environment variables the CLI reads

| Variable | Effect |
|---|---|
| `LAGER_BOX` | default box when `--box` is omitted; also injected into the box-side script |
| `LAGER_CONFIG_FILE_DIR` | directory holding the global `.lager` (default `~`) |
| `LAGER_CONFIG_FILE_NAME` | config filename (default `.lager`) |
| `LAGER_GATEWAY_AUTH_FILE` | override `~/.lager_gateway_auth` |
| `LAGER_USER` | effective user identity (lock holder on a dev machine) |
| `LAGER_LOCK_HOLDER` | override the lock-holder string |
| `LAGER_LOCK_WAIT` | seconds to wait on a lock collision |
| `LAGER_LOCK_TTL` | server-side lock TTL; `none` for eternal |
| `LAGER_LOCK_HEARTBEAT` | heartbeat interval, default 60 |
| `LAGER_AUTO_LOCK_DISABLE` | `1` skips auto-locking entirely |
| `LAGER_DEBUG` | full tracebacks, same as `--debug` |
| `LAGER_NO_UPDATE_CHECK` | suppress the background version check |
| `CI` | `true` selects CI locking semantics (Actions sets this) |

Injected **into** your box-side script by `lager python`: `LAGER_BOX`,
`LAGER_RUNNABLE`, `LAGER_PROCESS_ID`, `LAGER_OUTPUT_CHANNEL`.

### Exit codes

| Command | Code | Meaning |
|---|---|---|
| `lager python` | script's own | passed through unchanged |
| | 124 | `--timeout` expired, SIGTERM |
| | 137 | `--timeout` expired, SIGKILL |
| | 255 | could not retrieve the exit code from the box |
| | 130 | interrupted |
| `lager update --check` | 0 / 1 / 2 | in sync / would update / undeterminable |
| `lager exec` | container's | passed through |
| `lager ssh -- cmd` | remote's | passed through; 255 on SSH transport failure |
| any command | 1 | general error, **including a lock collision** |
| any command | 2 | usage error (unknown option, missing argument) |

### The `.lager` file

Global `~/.lager` — JSON only, no INI:

| Section | Contents |
|---|---|
| `BOXES` | name to `{ip, user, version}` |
| `NETS` | net definitions keyed by box |
| `DEFAULTS` | `gateway_id` (default box), `user`, per-role default nets |

Project-local `./.lager`, found by walking up from the working directory:

| Section | Contents |
|---|---|
| `DEVENV` | container image, mount point, shell, volumes, saved commands |
| `DEBUG` | debug-net name to a local debug-script path |
| `includes` | extra directories to upload with `lager python` |

### Commands you will not find

These do not exist; if you see them in an older example, they are stale:

| Not a command | Use instead |
|---|---|
| `lager test` | `lager python <script-or-dir>` |
| `lager net add` | `lager nets add` |
| `lager nets remove` | `lager nets delete` |
| `lager connect`, `lager debug NET connect` | connection is implicit in `flash` / `reset` / `erase` |
| `lager debug NET rtt` | `lager debug NET gdbserver --rtt` |
| `lager gdbserver` (top level) | `lager debug [NET] gdbserver` |
| `lager box update` | `lager update` |
| `--lock`, `--lock-wait`, `--no-lock` | locking is automatic; tune with `LAGER_LOCK_*` |
| `lager update --all` | loop over boxes in your shell |

---

## 15. Troubleshooting

**`Error: Box 'BENCH-1' is locked by ...`**
Another run holds the bench. Under CI the command waits up to `LAGER_LOCK_WAIT`
(default 30 minutes) before printing this; on a developer machine it is immediate.
Check who: `lager boxes`. A `ci:github:...` holder renders as the repository, run,
job and runner. Wait, or ask the holder. Do not `unlock --force` from a job.

**A `lager` command on the box fails instantly with a lock error, but CI is fine.**
Lock semantics switch on `CI=true`. A command run from cron, systemd, or an ssh
session is not in CI and fails fast by design. Set `LAGER_LOCK_WAIT` for that
invocation if you want it to queue.

**The job hangs with no output.**
Most likely a bare `lager` with no subcommand — that opens an interactive REPL.
Otherwise an interactive `[y/N]` prompt: add `--yes` where the command supports
it, and `exec < /dev/null` in cleanup steps.

**`lager exec` hangs or errors about a TTY.**
It defaults to `--interactive --tty`. Pass `--no-tty` in CI.

**Your test cannot see an environment variable set in the workflow step.**
A step's `env:` block applies to the runner, and your script runs on the box.
Only `--env FOO=bar` and `--passenv FOO` cross that boundary:

```yaml
- run: lager python tests/hil --env LOG_LEVEL=debug --passenv GITHUB_SHA
```

**A backgrounded test dies with `SIGTTIN`.**
`lager python` starts an interactive "press Enter to resume" watcher whenever
stdin is a TTY, and a read from the controlling terminal by a background process
group raises `SIGTTIN`. Redirect stdin from `/dev/null`.

**`[warning] Box BENCH-1 is on lager X; CLI is on Y.`**
Version skew. Update the box (`lager update --box BENCH-1`) or pin the runner's
CLI to match. A box that reports no version at all is running an image too old
for this CLI.

**Tests pass but you changed box-side code and nothing was exercised.**
`lager python` runs your script inside the box's environment. Box-side code comes
from the box's deployment, not your checkout. See
[§7](#7-making-sure-the-box-runs-the-code-under-test).

**A retry reports passes for tests it did not run.**
Stale results restored from `/tmp` on a self-hosted runner, or restored results
whose firmware key was not checked. See [§12](#12-results-across-retries).

**Flash "succeeds" and every later test fails.**
The programmer reported a fatal connection error and the command still exited 0,
leaving the device erased. Grep the flash output. See
[§6](#6-getting-firmware-onto-the-dut).

**The bench is left in a bad state after a cancelled run.**
`cancel-in-progress: true` without a cleanup step. See
[§8](#8-leaving-the-bench-safe).

**The workflow is green but no hardware ran.**
An empty `strategy.matrix` expands to zero jobs and the workflow succeeds. Add
the `validate` job from [§11](#11-scaling-to-a-fleet).

---

## Appendix: a complete single-bench workflow

```yaml
name: HIL

on:
  push:
    branches: [main]
  schedule:
    - cron: '0 15 * * *'
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: hil-${{ vars.HIL_BENCH }}
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      artifact: firmware-${{ github.sha }}
    steps:
      - uses: actions/checkout@v4
      - run: ./build.sh
      - uses: actions/upload-artifact@v4
        with:
          name: firmware-${{ github.sha }}
          path: build/firmware.hex
          if-no-files-found: error

  reachable:
    runs-on: ${{ vars.HIL_BENCH }}
    timeout-minutes: 5
    env:
      LAGER_BOX: ${{ vars.HIL_BENCH }}
    steps:
      - name: Bench reachable
        run: |
          for attempt in 1 2 3; do
            if lager hello; then exit 0; fi
            echo "::warning::lager hello failed (attempt ${attempt}/3); retrying"
            sleep 5
          done
          echo "::error::bench unreachable after 3 attempts"
          exit 1

  hil:
    needs: [build, reachable]
    runs-on: ${{ vars.HIL_BENCH }}
    timeout-minutes: 60
    env:
      LAGER_BOX: ${{ vars.HIL_BENCH }}
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.sha }}

      - uses: actions/download-artifact@v4
        with:
          name: ${{ needs.build.outputs.artifact }}
          path: ./firmware

      - name: Bench bring-up
        run: ./tools/bench.sh bring-up

      - name: Flash
        run: |
          set -o pipefail
          log=$(mktemp)
          lager debug SWD flash --hex ./firmware/firmware.hex 2>&1 | tee "$log"
          if grep -qE 'Cannot power up debug port|Could not connect to the target device' "$log"; then
            echo "::error title=flash::programmer reported a fatal error but the command returned success"
            exit 1
          fi
          lager debug SWD reset

      - name: Boot and identity
        run: bash tools/retry-hil.sh -- lager python tests/hil/boot

      - name: Console commands
        run: bash tools/retry-hil.sh -- lager python tests/hil/console

      - name: Charge behaviour
        if: github.event_name == 'schedule'
        run: |
          bash tools/retry-hil.sh -- \
            lager python tests/hil/charge -- --target-soc 80 --timeout-min 45

      - name: Bench cleanup
        if: cancelled() || failure()
        run: |
          exec < /dev/null
          rc=0
          ./tools/bench.sh bring-up --recover || rc=$?
          lager debug SWD reset || rc=$?
          if [ "$rc" -ne 0 ]; then
            echo "::error title=cleanup::exited ${rc}; ${LAGER_BOX} may be unsafe for the next job"
            exit 1
          fi

      - name: Release the bench lock
        if: always()
        run: lager boxes unlock --box "$LAGER_BOX" 2>/dev/null || true
```

That is the whole shape: build off the bench, probe before committing to a run,
pin the checkout, assert the flash, wrap each hardware step so infrastructure
retries and device failures do not, and always leave the bench safe.

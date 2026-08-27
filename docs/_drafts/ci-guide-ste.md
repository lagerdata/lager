# How to Use Lager in CI

**This document uses ASD-STE100 Simplified Technical English (Issue 8).**

This document tells you how to do hardware-in-the-loop (HIL) tests in GitHub
Actions. You install the GitHub Actions runner on the Lager Box.

Read this document in sequence. Sections 1 to 4 give you a workflow that
operates. The sections after them tell you what to add when your test system
becomes larger.

In the examples, `BENCH-1` is the name of a box. `my-firmware` is the name of
your repository. `SWD`, `UART`, `BATT` and `USB_CHARGE` are the names of nets.
Use your own names in their place.

---

## Technical Names and Technical Verbs

This document uses these Technical Names:

artifact, bench, box, branch, cache, CI, commit, container, DUT, exit code,
firmware, flag, gateway, GitHub Actions, HIL, image, job, label, Lager, Lager
Box, lock, matrix, net, quarantine, repository, role, runner, script, secret,
session, step, test, variable, workflow.

This document uses these Technical Verbs:

to cancel, to check out, to download, to flash, to install, to log in, to
queue, to retry, to run (a script), to upload.

---

## Contents

1. [The location of the runner](#1-the-location-of-the-runner)
2. [How to install the Actions runner on the box](#2-how-to-install-the-actions-runner-on-the-box)
3. [How to prepare the runner account](#3-how-to-prepare-the-runner-account)
4. [Your first workflow](#4-your-first-workflow)
5. [How to share one bench between jobs](#5-how-to-share-one-bench-between-jobs)
6. [How to put the firmware on the DUT](#6-how-to-put-the-firmware-on-the-dut)
7. [How to make sure that the box has the code under test](#7-how-to-make-sure-that-the-box-has-the-code-under-test)
8. [How to make the bench safe after a job](#8-how-to-make-the-bench-safe-after-a-job)
9. [How to find the difference between a bench failure and a firmware failure](#9-how-to-find-the-difference-between-a-bench-failure-and-a-firmware-failure)
10. [Net names](#10-net-names)
11. [How to use more than one bench](#11-how-to-use-more-than-one-bench)
12. [Test results after a retry](#12-test-results-after-a-retry)
13. [How to log in to a gateway from CI](#13-how-to-log-in-to-a-gateway-from-ci)
14. [Reference data](#14-reference-data)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. The location of the runner

A Lager command must have a network connection to a box. There are two
arrangements that give this connection. Your selection controls all the steps
that follow.

### Arrangement A: the runner is on a different machine

A different machine runs the job. The job connects to the box across the
network. The job must install the CLI, log in, and get the address of the box.

```yaml
runs-on: [self-hosted, lager-bench]
steps:
  - run: pip install lager-cli==0.39.1
  - run: lager login "$AUTH_URL" --email "$EMAIL" --password "$PASSWORD"
  - run: lager boxes add --name BENCH-1 --ip "$BOX_IP" --user lagerdata --yes
  - run: lager python tests/hil --box BENCH-1
```

This arrangement is correct if one runner controls more than one box. It is
also correct if you keep your boxes off the CI network on purpose.

This arrangement has three disadvantages:

- Each job does the same preparation again.
- The address of the box and the login data become repository secrets.
- Two jobs on that runner can try to use the same bench at the same time.

### Arrangement B: the runner is on the box

Install the GitHub Actions runner on the Lager Box. Give the runner one label.
The label is the name of the box.

```yaml
runs-on: BENCH-1
env:
  LAGER_BOX: BENCH-1
steps:
  - uses: actions/checkout@v4
  - run: lager python tests/hil
```

This is all the configuration that the job needs. This arrangement gives you
four advantages:

- **The label of the runner is the bench.** `runs-on: BENCH-1` and
  `--box BENCH-1` are the same text. There is no list to keep correct. A job
  cannot go to a runner that has no connection to the bench that the job needs.
- **The runner makes the jobs sequential.** A self-hosted runner accepts one job
  at a time. Two different branches that use `BENCH-1` go into a queue. You do
  not add a `concurrency:` block to get this.
- **You do not need secrets.** The CLI is on the box. The file `~/.lager` has
  the box in it. The gateway session is in the home directory of the runner
  account. A test system with this arrangement can operate with no Lager
  secrets. The only repository variables that it needs are the labels of the
  runners.
- **There is no network connection between the runner and the box.** The
  commands go through the local interface.

This arrangement has three disadvantages:

- The box does the check-out and the artifact download. Thus the box is busy
  for all of the job. Build the firmware on a different machine. Refer to
  [Section 6](#6-how-to-put-the-firmware-on-the-dut).
- All software that a workflow puts in the `PATH` of the box can control your
  bench. Give the box the same protection as a production machine.
- One box does one job at a time. For more test jobs at the same time, you need
  more benches.

The remainder of this document uses Arrangement B.

---

## 2. How to install the Actions runner on the box

> **NOTE:** The steps that follow are the standard GitHub runner installation.
> The rules for the Lager Box control them. Do a check of the service account
> and the `PATH` on your first box before you use these steps on all the boxes.

The Lager Box has these requirements: an x86-64 processor, Ubuntu 22.04 or a
subsequent version, an IP address that other machines can find, and a login
account with sudo permission.

Do all the steps that follow **as the login account of the box**. This is the
account that you gave to `lager install --user`. Do not install the runner as
the root account.

### 2.1 Registration of the runner

Get a registration token. In your repository, go to
`Settings > Actions > Runners > New self-hosted runner`. Then do these steps on
the box:

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

Give the runner only one label. The label must be the name of the box. Then
`runs-on: BENCH-1` selects that runner. `runs-on: [self-hosted, BENCH-1]` also
selects it.

**CAUTION:** Do not give two boxes the same label. If two boxes have the same
label, a job can go to a bench that does not have the nets that the job needs.
Then you must add a list in the YAML that connects each runner to its box.

### 2.2 Installation as a service

```bash
sudo ./svc.sh install "$USER"
sudo ./svc.sh start
sudo ./svc.sh status
```

`svc.sh install "$USER"` makes a systemd unit. The service operates as the
account that you give to it. It does not operate as root.

Give `$USER` in the command. The runner account and the Lager box account must
be the same account. If they are not the same, the job cannot read the
`~/.lager` file of the box account.

### 2.3 A check of the job environment

The runner gets its environment from the systemd unit. It does not get the
environment of your login shell. The Lager installer puts a symbolic link to
`lager` in `~/.local/bin`. A login shell finds that directory, but the service
can fail to find it.

Do this check in a workflow. Do not do it in an SSH session.

```yaml
- name: Runner sanity
  run: |
    echo "runner:  $RUNNER_NAME"
    echo "user:    $(id -un)"
    echo "arch:    $(uname -m)"
    command -v lager || { echo "::error::lager not on PATH for the runner account"; exit 1; }
    lager --version
```

If the runner cannot find `lager`, do these steps:

1. Make the file `~/actions-runner/.env`.
2. Put this line in the file:
   `PATH=/home/<user>/.local/bin:/usr/local/bin:/usr/bin:/bin`
3. Start the service again. The runner reads this file when it starts.

### 2.4 Do this for each bench

Each box has its own runner, its own name, and its own label. The boxes do not
share these items.

---

## 3. How to prepare the runner account

The runner account is the box account. Thus you do this preparation one time on
the machine. You do not do it in each job.

### 3.1 The CLI

```bash
pip install --user 'lager-cli==0.39.1'
lager --version
```

Obey these rules:

- **Use Python 3.10 or a subsequent version.** The CLI needs it.
- **Set the version of the CLI.** If you do not set the version, the version
  changes when a person makes an unrelated change to the machine. Set the
  version in one location. Change it only when you decide to change it.
- **Keep the version of the CLI the same as the version of the box, or more
  recent.** The CLI compares the two versions with each command. It gives a
  warning if they are different. The command `lager boxes` shows each box as
  `current`, `needs update`, or `newer`.
- **Always give a subcommand to `lager`.** If you give no subcommand, the CLI
  starts an interactive session. In CI, the job then continues until its
  time limit.

### 3.2 The list of boxes

The CLI finds the value of `--box` in the `BOXES` section of `~/.lager`. This
file is a JSON file.

```json
{
  "BOXES": {
    "BENCH-1": { "ip": "10.0.1.42", "user": "lagerdata", "version": "0.39.1" }
  },
  "DEFAULTS": { "gateway_id": "BENCH-1" }
}
```

To put this file on a new box, export it from a machine that has it:

```bash
# on your computer
lager boxes export -o boxes.json

# on the box, as the runner account
lager boxes import boxes.json --merge --yes
```

You can also add one box directly:

```bash
lager boxes add --name BENCH-1 --ip 10.0.1.42 --user lagerdata --yes
```

`--user` is the SSH account of the box. The commands `lager update`,
`lager logs`, `lager box-config` and `lager ssh` use it. It has no default
value.

### 3.3 How the CLI selects the box

The CLI uses this sequence. The first item has the highest priority.

1. The `--box` flag.
2. The `LAGER_BOX` environment variable.
3. `DEFAULTS.gateway_id` in `~/.lager`.
4. If the CLI finds no value, it gives an error.

Set `LAGER_BOX` one time in the `env:` block of the job. Then do not use
`--box` in the steps. Each workflow is then the same on each bench. To change
the name of a box, you change one line.

```yaml
jobs:
  hil:
    runs-on: BENCH-1
    env:
      LAGER_BOX: BENCH-1
```

You can also put the name in a repository variable and use it two times:

```yaml
    runs-on: ${{ vars.HIL_BENCH }}
    env:
      LAGER_BOX: ${{ vars.HIL_BENCH }}
```

Do this if you move the test suite to a different bench. Then you change one
variable. You do not change each workflow.

### 3.4 Two problems with the configuration file

- **`~/.lager` must be a file. It must not be a directory.** If other software
  makes `~/.lager` a directory, each CLI command on that machine fails. The
  commands on your computer continue to operate. Thus you can find this fault
  only on the box.
- **Do not let a virtual environment hide the CLI.** The CLI gives a warning if
  the `lager` in the `PATH` is not the `lager` of the active environment. Do
  not ignore this warning. The two files can have different versions.

`LAGER_CONFIG_FILE_DIR` sets a different directory for `~/.lager`.
`LAGER_CONFIG_FILE_NAME` sets a different name for the file.

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

Three parts of this workflow need more data.

**`lager hello` shows only that the box has a connection.** It does not show
that the box is serviceable. The command gives exit code 0 also when the box
sends an HTTP error. Only a connection failure or a timeout gives a different
exit code. Do the command again after a failure. On a box behind a gateway, the
first command after a new login can fail one time. Refer to
[Section 13](#13-how-to-log-in-to-a-gateway-from-ci). But do not use exit code
0 as proof that the box is fully serviceable.

**`lager python` sends your script to the box. The box runs the script.** The
argument is a file or a directory. The CLI uploads a directory as a module. The
script operates in the Python container of the box. It does not operate on the
runner. Refer to
[Section 7](#7-how-to-make-sure-that-the-box-has-the-code-under-test) and
[Section 15](#15-troubleshooting).

**`timeout-minutes` is the maximum time that the job can hold the bench.** A
job that stops in an unusual condition holds the bench and its lock until
GitHub stops the job. Set a time that is acceptable to you. Do not use the
default value of 360 minutes.

### How to give arguments to your test

The CLI reads all the text before `--`. Your script reads all the text after
`--`.

```yaml
- run: lager python tests/hil/charge -- --target-soc 80 --timeout-min 45
```

### How to send other files

`lager python` uploads the script and its directory. It does not upload other
files. To send a firmware image, a debug script, or a table of limits, use
`--add-file`. The file goes adjacent to the script. Use its base name.

```yaml
- run: |
    lager python tests/hil/flash \
      --add-file ./artifacts/firmware.hex \
      --add-file tools/debug/target.script \
      -- --image firmware.hex
```

### How to get files back

```yaml
- run: lager python tests/hil --download results.json --allow-overwrite
```

The CLI downloads the files after the script stops. It does not download them
during the test.

---

## 5. How to share one bench between jobs

A bench is one item of physical equipment. Three independent mechanisms keep the
jobs separate. Each mechanism has a different purpose. Read about all three
before you use one of them.

### Mechanism 1: the runner

A self-hosted runner accepts one job at a time. Use one runner for each box.
Then the runner makes all the jobs for that bench sequential. This applies
across branches, across workflows, and across repositories. You get this
mechanism with no configuration. In most conditions it is enough.

This mechanism puts the jobs in a queue. It does not remove old jobs. Five
pushes to a branch make five jobs. The bench does all five.

### Mechanism 2: workflow concurrency

Use `concurrency:` when a new job must replace an older job.

```yaml
concurrency:
  group: hil-BENCH-1-${{ github.event.pull_request.number || github.run_id }}
  cancel-in-progress: true
```

Two details are important.

**Put `concurrency:` on the job that holds the bench.** Do not put it at
workflow level. A workflow-level group does not go into a workflow that this
workflow calls. Thus the job that uses the hardware is not in the group.

**Use the pull request number as the key. Use `run_id` as the alternative.** A
key such as `github.head_ref || github.ref_name` gives the same text for two
different conditions. A `workflow_dispatch` on a branch that also has a pull
request gives the same text as the runs of that pull request. Then the manual
job and the pull request job cancel each other. `run_id` is different for each
run. Thus each non-pull-request run gets its own group.

**CAUTION:** Use `cancel-in-progress: true` only if you also clean the bench.
If GitHub cancels a job during a test, the bench can stay in an unsafe
condition. A power supply can stay on. A battery simulator can stay at a
dangerous voltage. A debug buffer can stay isolated. Use the cleanup step in
[Section 8](#8-how-to-make-the-bench-safe-after-a-job).

For nightly jobs and post-merge jobs, use `cancel-in-progress: false`. Let a job
that does a measurement continue to the end.

### Mechanism 3: the Lager lock

Lager locks the box automatically. There is no `--lock` flag, no `--lock-wait`
flag, and no `--no-lock` flag. Environment variables control this function.

**The identity of the lock holder is different in CI.** In GitHub Actions, the
identity has this format:

```
ci:github:<repo>#<run_id>-<attempt>/<job>@<runner>:<pid>
```

The text always ends with `:<pid>`. Thus two matrix jobs cannot get the same
identity. `lager boxes` shows the identity in a format that is easy to read.

**The behavior after a collision is different in CI.** Lager finds CI from the
`CI=true` variable and a variable such as `GITHUB_RUN_ID`.

| Environment | Behavior after a collision |
|---|---|
| CI | The command waits. It examines the lock each 2 seconds. The maximum time is `LAGER_LOCK_WAIT`. The default value is 1800 seconds. |
| A user computer | The command gives an error and stops immediately. |

GitHub Actions sets `CI` and `GITHUB_RUN_ID` for each `run:` step. Thus jobs
wait automatically.

**CAUTION:** A `lager` command on the same box that does not operate in an
Actions step is not in CI. This applies to cron, to systemd, and to an SSH
session. Such a command stops immediately after a collision. A maintenance
script on the box thus fails each time that CI operates.

**A lock collision gives exit code 1.** All other errors also give exit code 1.
To find a lock collision, look for the text `is locked by` in the error output.

The lock has a maximum life. The default value is 1800 seconds. The CLI sends a
heartbeat each 60 seconds. The heartbeat makes the maximum life start again.
Thus the maximum life does not limit the length of your test. It limits how
long a lock stays after the CLI stops in an unusual condition.

Add a step that releases the lock:

```yaml
      - name: Release the bench lock
        if: always()
        run: lager boxes unlock --box "$LAGER_BOX" 2>/dev/null || true
```

**WARNING:** Do not use `lager boxes unlock --force` in a job. This command
releases a lock that a different person holds. If a lock stays after your job
ends, a different person or a different job holds it. If you release that lock,
you take the bench from that person. Let the lock end at its maximum life.

### Commands that use the lock

These commands get the lock automatically:

- `lager python`
- the instrument commands `adc`, `dac`, `gpi`, `gpo`, `thermocouple`, `watt`,
  `energy`, `scope`, `logic`
- the communication commands `spi`, `i2c`, `uart`, `usb`, `wifi`, `ble`,
  `blufi`, `router`
- the power commands `supply`, `battery`, `eload`, `solar`
- the equipment commands `debug`, `arm`, `webcam`
- the administration commands `install`, `uninstall`, `update`, `install-wheel`

These commands do not use the lock:

- `lager hello`, `lager boxes`, `lager instruments`
- `lager nets` and all its subcommands
- `lager defaults`, `lager logs`, `lager binaries`, `lager dut`
- `lager ssh`, `lager exec`, `lager devenv`
- `lager login`, `lager logout`, `lager whoami`

Because of this difference, a `lager hello` job and a `lager nets state` step
are safe when a different job holds the bench.

### Environment variables for the lock

| Variable | Function |
|---|---|
| `LAGER_LOCK_WAIT` | Seconds to wait after a collision. CI default 1800. User default 0. |
| `LAGER_LOCK_TTL` | Maximum life of the lock. Use `none` for a lock with no limit. |
| `LAGER_LOCK_HEARTBEAT` | Seconds between two heartbeats. Default 60. |
| `LAGER_LOCK_HOLDER` | A different identity for the lock holder. |
| `LAGER_AUTO_LOCK_DISABLE` | Set to `1` to stop the automatic lock. |

Use `LAGER_AUTO_LOCK_DISABLE` only on a bench that one person uses. Do not use
it to prevent a collision.

**NOTE:** `lager python --detach` gives the lock to the box. The box holds the
lock until the detached job ends. This is after your workflow step ends.

---

## 6. How to put the firmware on the DUT

### Build the firmware on a different machine

Do not build your firmware on the bench runner. A build uses the bench for all
of its length, but it does not use the hardware. The bench is your most limited
resource.

Divide the work. Build on a GitHub-hosted runner or a general-purpose
self-hosted runner. Upload the image as an artifact. Then the bench job
downloads it.

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

### Set the check-out to `github.sha`

On a `pull_request` event, the default check-out uses the variable reference
`refs/pull/<n>/merge`. If a push happens when the bench job starts, the job uses
new test code with old firmware. The job then reports this result for the
first commit. To prevent this, give `ref: ${{ github.sha }}`. Then the test
code and the firmware come from the same commit.

### Do a check that the flash operation was successful

A programmer tool can report a connection failure and still give exit code 0.
The device is then erased but not programmed. The tests fail subsequently, and
the cause is not clear. Do not use the exit code only. Record the output and
look for the failure text of your tool.

```yaml
- name: Flash
  run: |
    set -o pipefail
    log=$(mktemp)
    lager debug SWD flash --hex ./firmware/firmware.hex 2>&1 | tee "$log"
    if grep -qE 'Cannot power up debug port|Could not connect to the target device' "$log"; then
      echo "::error title=flash::the programmer reported a fatal error but the command returned success. The DUT is likely erased but not reprogrammed."
      exit 1
    fi
    lager debug SWD reset
```

Change the text pattern to the failure text of your programmer tool. The rule
is more important than the example: a flash step must do a check of its own
result.

### How to flash from a test

Some test suites program the device in their first test. They use the Python
API on the box. They do not use a separate CLI step. This is also correct.
Sometimes it is better, because the test that programs the device is also the
test that shows that the program operation is correct.

**NOTE:** The Python `DebugNet` methods give the output of the programmer as
text. They do not give an error when the programmer reports a failure. Thus the
test must do a check of its own result.

### The debug subcommands

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

There is no separate `connect` command. The commands `flash`, `reset` and
`erase` make the connection. There is no `lager debug <net> rtt` command. For
RTT, use `gdbserver --rtt`.

---

## 7. How to make sure that the box has the code under test

This is the most frequent cause of incorrect CI results. It is a result of the
operation of `lager python`.

**`lager python` sends your script to the box. The box runs the script.** The
runner gives the script. The box gives the Python environment, the instrument
drivers, the net definitions, and the Lager box software. Thus a check-out of
your branch does not test the software on the box. The box continues to use the
version of its last installation.

If your repository has only test scripts, this is not a problem. The scripts
come from the check-out.

If your CI also tests software that operates on the box, do a check of the box
version:

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

`lager update --check` is a dry run. It reports the changes that it would make.
It does not change the box. Its exit code has three values:

| Exit code | Meaning |
|---|---|
| 0 | The box is correct. No change is necessary. |
| 1 | An update is necessary. The code, the dependencies, or the container. |
| 2 | The command cannot find the condition of the box. |

The difference between 1 and 2 is important. Exit code 1 tells you that the box
is old. Exit code 2 tells you that the check did not operate. Exit code 2 gives
you no data about the box.

To change a box to a specified version:

```bash
lager update --box BENCH-1 --version main --yes
lager update --box BENCH-1 --version v0.39.1 --yes
```

`--version` accepts a release tag, a version number with or without an initial
`v`, a branch name, or a full 40-character commit SHA. The default value is
`main`. The other flags are `--force`, `--pull`, `--no-pull`, `--verbose` and
`--yes`.

`lager update` changes one box for each command. Use a loop in your shell for
more boxes.

### How to install the Python dependencies of the box

Your test scripts operate in the container of the box. Thus you install their
dependencies on the box. Do not install them on the runner. Use the box
configuration. It stays after a container restart and after a box update.

```bash
lager box-config pip add pyserial rich --box BENCH-1
lager box-config pip list --box BENCH-1
```

Export the configuration and import it to make each bench the same:

```bash
lager box-config export --box BENCH-1 -o bench-config.json
lager box-config import bench-config.json --box BENCH-2
```

Put that file in your repository. It is the only record of the necessary
contents of your benches.

---

## 8. How to make the bench safe after a job

A HIL job that stops during a test does not only leave files. It leaves the
**hardware** in the condition of the test. A power supply can stay on at an
incorrect voltage. A load can continue to take current. A heater can stay on.
An enable signal can stay high. The next job gets this condition. The next
person at the bench also gets it.

Put two steps at the two ends of each hardware job.

### Preparation, before all other steps

```yaml
- name: Bench bring-up
  run: ./tools/bench.sh bring-up
```

Make this a separate step. Do not put it in the flash step. A cold bench has
the supply off and the enable signals low. Some instruments keep their output
path open until a session sets them. Then the DUT has no power. A DUT with no
power gives this message at the **flash** step: "cannot connect to the target".
This message looks like a debugger failure. With a separate step, the step that
failed gives the correct cause.

### Cleanup, after a cancel or a failure

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

Obey these rules for the cleanup step:

- **Use `if: cancelled() || failure()`. Do not use `if: always()`.** A job that
  is successful must end with its own procedure. A cleanup step that also
  operates after a successful job hides the faults in that procedure.
- **Close the standard input.** If a subcommand asks a `[y/N]` question, the
  job continues until its time limit.
- **Continue after a failure.** An unserviceable instrument must not prevent the
  remainder of the cleanup.
- **Set the equipment to a safe condition. Do not release the lock.** The job
  that got the lock releases it. If a lock stays after that, a different person
  holds it.
- **Do not use privileges.** The runner account has limited sudo permission or
  none. All cleanup commands must operate with no password.

### Make sure that a cancel signal goes to your test

If a shell script starts your test, a `SIGTERM` from the runner goes to the
shell. The shell does not send it to the test. The test continues until GitHub
stops the job. Thus the test operates the hardware after you asked it to stop.

Use `exec` to replace the shell with the test:

```yaml
- run: exec ./tools/run-suite.sh --box "$LAGER_BOX"
```

Then the signal goes to the correct process.

---

## 9. How to find the difference between a bench failure and a firmware failure

A HIL test suite must find faults. It must also be correct when it did not test
anything. A debug probe that did not connect to the USB bus, a debug session
that did not start, or a box that was not available: these are not firmware
faults. It is correct to do these tests again.

An incorrect device identifier, or a device that does not start, **is** a
firmware fault. If you do that test again, you hide the fault that you made the
bench to find.

Put this difference in the exit code.

### The rule

Use this rule in your tests:

| Exit code | Meaning | Do the test again? |
|---|---|---|
| **0** | The test is successful. | Not applicable |
| **1** | Device failure. Incorrect identifier, incorrect image, no start, or a measurement out of limits. | **No** |
| **2** | Equipment failure. The probe, the net, the connection or the bench preparation prevented the test. | **Yes** |

Lager does not make this rule. Your test scripts make it. Your CI uses it.
`lager python` gives the exit code of your script with no change. This is why
the rule operates.

`lager python` can also give these exit codes:

| Exit code | Meaning |
|---|---|
| 124 | The `--timeout` time ended. The CLI sent SIGTERM. |
| 137 | The `--timeout` time ended. The CLI sent SIGKILL. |
| 255 | The CLI could not get the exit code from the box. |
| 130 | A person or a signal stopped the command. |

Put 124, 137 and 255 in the equipment-failure group.

### A script that does the test again

Put this script in `tools/retry-hil.sh`. It does the test again only after an
equipment failure. It removes the power from the probe and the DUT between two
attempts. It also changes a test that does not stop into a failure that it can
do again.

```bash
#!/bin/bash
#
# Do a HIL test again. Set the bench to a known condition between attempts.
#
#   retry-hil.sh -- lager python tests/hil/flash --add-file firmware.hex
#
# Exit codes: 0 success / 1 device failure / 2 equipment failure.
# The script does the test again only after an equipment failure.
# The exit code of the script is the exit code of the last attempt.
#
# Environment variables:
#   LAGER_BOX             the box (necessary)
#   HIL_RETRY_ATTEMPTS    number of attempts, default 3
#   HIL_ATTEMPT_TIMEOUT   seconds for each attempt, default 300 (0 = no limit)
#   HIL_PROBE_NET         USB net of the debug probe, default USB_DEBUG
#   HIL_PROBE_SETTLE      seconds to wait after you set the probe on, default 8
#   HIL_POWER_CYCLE_DUT   also remove the power from the DUT, 1/0, default 1
#   HIL_DUT_VBUS_NET      USB net of the DUT, default USB_CHARGE
#   HIL_DUT_POWER_NET     supply or battery net of the DUT, default BATT
#   HIL_DUT_SETTLE        seconds to wait after the DUT starts, default 3

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

# Put each attempt in `timeout`. Then a test that does not stop gives exit code
# 124. The script can do that test again. If you do not do this, the test
# continues until the time limit of the job.
if [ "$ATTEMPT_TIMEOUT" -gt 0 ] 2>/dev/null && command -v timeout >/dev/null 2>&1; then
  run_attempt() { timeout "$ATTEMPT_TIMEOUT" "$@"; }
else
  run_attempt() { "$@"; }
fi

# Use `disable` and then `enable`. Do not use `toggle`. The probe must be on at
# the end. This is correct for all conditions that the failed attempt made.
power_cycle_probe() {
  echo "  - power-cycling ${PROBE_NET}" >&2
  lager usb "$PROBE_NET" disable --box "$BOX" || true
  sleep 2
  lager usb "$PROBE_NET" enable --box "$BOX" || true
  sleep "$PROBE_SETTLE"
}

# A new connection of the probe cannot start a device that is asleep. Only a
# removal of the board power can start it. Set VBUS on last. Then the board
# starts with VBUS present. Each command can fail: a net that this bench does
# not have does nothing.
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

# A box connection failure gives exit code 1. A device failure gives the same
# exit code. But a connection failure is an equipment failure. Find it in the
# message. Keep the pattern small. Then the script cannot hide a device failure.
CONN_FAIL_RE='Timed out connecting to the box|did not respond in time|Failed to connect|Connection refused|Could not connect'

# A lock collision also gives exit code 1. It is also not a device failure.
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

Use the script for each hardware step:

```yaml
- name: Flash and verify
  run: |
    bash tools/retry-hil.sh -- \
      lager python tests/hil/flash --add-file ./firmware/firmware.hex
```

Two parts of the script are important. The `timeout` command changes a test
that does not stop into a failure that the script can do again. The text
patterns change an exit code 1 into an equipment failure. This is necessary
because a box that was not available gives the exit code of a device failure.

---

## 10. Net names

### The box holds the nets. Your repository does not.

The Lager Box holds the net definitions. They stay after a restart. Your
repository cannot make them. Your repository can only tell which nets it needs.
It can also give a clear failure when a bench does not have them.

This division is correct. But it makes the bench configuration difficult to
examine. Two methods help.

**Make the nets from a file in the repository.** `lager nets add-batch` reads a
JSON file of net definitions:

```bash
lager nets add-batch bench/nets.json --box BENCH-1
```

Keep `bench/nets.json` in the repository. Then you can build the bench again.
If you do not do this, one person configures the bench one time by hand.

**Make a list of the nets in the job.** `lager nets state --json` gives data
that a program can read. It does not use the lock. Thus a preparation step can
make sure that the bench has the necessary nets. The step can give the name of
the net that is not present. Without this step, a test fails subsequently with
a Python error.

### Give each net a name that tells its function

A net has these fields: `name`, `role`, `instrument`, `channel` and `address`.
The **role** is the type of the net. Examples are `usb`, `gpio`, `uart`,
`debug`, `power-supply`, `battery` and `adc`. The box keeps one default net for
each role. There is no field for the function.

Thus when a bench has two nets with the same role, **only the name tells the
function of each net**. Two USB ports both have the role `usb`. Only the name
tells which port charges the device and which port supplies the debug probe.

Use these rules:

1. **For a role with more than one net, put the role first and the function
   second:** `USB_CHARGE`, `USB_DEBUG`, `UART_CONSOLE`, `ADC_VBUS`,
   `GPIO_NRST`. The name and the role must agree. Then you can find an
   incorrect connection.
2. **Give the power nets the name of the instrument, not the function:** `BATT`
   for a battery-simulator net, `SUPPLY` for a programmable-supply net. Then
   the role and the name give the same data on purpose.
3. **For a role with only one net, use the bare name:** `SWD` for the one debug
   probe, `UART` for the one console.

These roles usually need the rules:

- `usb`. The hub can only set a port on or off. Thus only the name gives the
  function of the port.
- `gpio`. Each pin has the role `gpio`.
- `uart`.
- The measurement roles `adc`, `dac`, `scope`, `logic`, `thermocouple` and
  `watt-meter`.

The advantage is large. A test can find the charge port on each bench that obeys
the rules. It does not need a configuration for each bench. Thus a second bench
is easy to add.

### Do not change a shared net from CI

`lager nets set-script` changes the configuration of the net **for all users**.
A CI job that sets a debug script leaves the bench in that condition. The next
person can need a different script.

Send the script with the job. Use it only for that job:

```yaml
- run: |
    lager python tests/hil/flash \
      --add-file tools/debug/halt-first.script \
      -- --script halt-first.script
```

Do the same for all other changes. **A CI job must leave the net configuration
of the bench in its initial condition.**

### Net commands

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

**NOTE:** The command is `lager nets` with an `s`. The subcommand is `delete`,
not `remove`. These commands do not use the lock.

---

## 11. How to use more than one bench

One bench does not need this section. More than one bench needs a method to
give three answers in code: which benches are present, what each bench can do,
and which tests each bench can do.

### Declare the function of a bench, not its identity

Put the benches into **roles**. A role gives the nets that a bench must have. It
also gives each function that is not one net.

`bench/roles.toml`:

```toml
# Each role declares the nets that a bench of that role must have. The
# `capabilities` field gives the bench functions that are not one net. Two
# benches can give the same function with different equipment. A test that
# needs the function does not need to know the equipment.

[standard]
nets = ["UART", "SWD", "USB_CHARGE", "USB_DEBUG", "BATT"]

[power]
nets = ["UART", "SWD", "USB_CHARGE", "USB_DEBUG", "BATT"]
capabilities = ["current_measurement"]

[supply-fed]
# A bench supply gives the battery rail. A charger does not. Thus there is no
# charge port. A test that charges the DUT must get the same condition with a
# different method.
nets = ["UART", "SWD", "USB_DEBUG", "SUPPLY"]
```

A test declares its requirements. The net options that it accepts give the
necessary nets. A module-level statement such as
`REQUIRED_CAPABILITIES = ["current_measurement"]` gives the other requirements.
Then the test runner sends the test only to a role that has them.

The result for each test on each bench:

| Condition | Result |
|---|---|
| This role does not have the necessary function. | **N/A** — a different role does the test. |
| This role does not have the necessary net. | **N/A** |
| The quarantine list of this bench includes the test. | **QUARANTINED** |
| The role has the net but the bench does not. | **FAIL** — the role is not correct. |
| All requirements are satisfied. | **RUN** |

The difference between the last two conditions is important. "This test is not
applicable here" and "this bench is unserviceable" look the same if you have
only PASS and FAIL.

### One file for each bench

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

`enabled: false` removes a bench from the test system. This is a change of one
line that a person can examine. It is not a change to a workflow file. The
quarantine list stops the incorrect results of an unserviceable bench. No person
disables the test for all benches.

### Make the matrix

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

Four details are important:

- **Use `fail-fast: false`.** One unserviceable bench must not cancel the other
  benches. You need the result of each bench.
- **Add the `validate` job.** An empty matrix makes zero jobs, and the workflow
  is then successful. A separate job that fails makes the difference clear.
  Then "no bench did a test" is not the same as "each test was successful".
- **Give the job a correct name.** GitHub uses `/` to divide the parts of a job
  name. Some displays show only the last part. Thus `BENCH-1 / standard` becomes
  `standard`, and you lose the name of the bench. Put both names in one part
  with parentheses.
- **Use one concurrency group for each bench.** Then different benches operate
  at the same time. A new job replaces an older job on the same bench.

### Use a gate job for the result of all benches

Each bench reports only the tests that it did. A test that is `N/A` on **each**
bench gives a green result and no test coverage. This happens with a new test
that no role accepts. It also happens when no role has the necessary net.

Add a gate job on a GitHub-hosted runner. The job collects the results of all
the benches.

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

Make this gate job the necessary status check for branch protection. Do not use
the bench jobs. The set of bench jobs changes when you enable or disable a
bench. A necessary check that is not always present is worse than no check.

Workflow annotations have one line. They cannot show a table. Thus write the
results of all the benches to `$GITHUB_STEP_SUMMARY` from the gate job. Use one
row for each test and one column for each bench. Stop the summary of each bench.
Then there is one location to look at.

### Report the tests that you did not do

Your test system can limit its own coverage. Examples are a quarantine, a
disabled bench, a cache of previous results, and a limit on the number of
tests. Write a message for each one. If you do not, the report looks the same as
a report of full coverage. A HIL system must not do this.

---

## 12. Test results after a retry

The GitHub function "Re-run failed jobs" erases the work directory. If your test
suite does not do the tests that were successful before, the data about those
tests must stay.

**`actions/cache` cannot do this.** The cache from attempt N has the current
`run_id` as part of its key. Attempt N+1 of the **same** run cannot find it.
Cache keys do not change, thus `restore-keys` also cannot find it.

**The artifact from the last attempt can do this.** Upload the results after
each attempt. Use `overwrite: true`. Download them when `github.run_attempt` is
more than 1.

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

Two conditions can give you incorrect results.

**Use `$RUNNER_TEMP`. Do not use `/tmp`.** On a self-hosted runner, the contents
of `/tmp` stay between two jobs. Thus an old `results.json` from a previous run
can go into the artifact. This also happens when this run made no results. The
next attempt then does not do the tests, because the old results show that they
were successful. GitHub erases `$RUNNER_TEMP` at the start and at the end of
each job.

**Put the identity of the firmware with the results.** Use a version text or a
content hash. Erase all the results when this identity is not the same as the
firmware of this attempt. If you do not do this, an attempt with a different
image reports the results of the previous image.

Upload the results also after a failure. Use `if: always()`. The next attempt
and the gate job need the results of a job that stopped during a test.

---

## 13. How to log in to a gateway from CI

Boxes behind an access gateway need a session. Boxes with no gateway need no
session. If your boxes have no gateway, do not read this section.

The runner is on the box. Thus you log in one time on the machine. You do not
log in in each job. The CLI keeps the session in the home directory of the
runner account. The CLI also makes the session current again automatically.

```bash
# one time, as the runner account
lager login https://gateway.example.com
lager whoami
```

Some conditions need a login in the job. Examples are a runner on a different
machine, or a box that you install again at intervals. For these conditions, use
the flags:

```yaml
      - name: Sign in
        env:
          AUTH_URL: ${{ vars.LAGER_AUTH_URL }}
          CI_EMAIL: ${{ secrets.LAGER_CI_EMAIL }}
          CI_PASSWORD: ${{ secrets.LAGER_CI_PASSWORD }}
        run: lager login "$AUTH_URL" --email "$CI_EMAIL" --password "$CI_PASSWORD"
```

These three names are your names. The CLI does not read them.

**WARNING:** Read the password from a secret. Do not write the password in the
workflow. A password on a command line is visible in the list of processes on
the box.

Obey these rules:

- **Use a CI account with no multi-factor authentication.** The `--email` and
  `--password` flags cannot answer a multi-factor question. The command then
  waits for an input that never comes.
- The CLI keeps the session in `~/.lager_gateway_auth` with permission 0600.
  `LAGER_GATEWAY_AUTH_FILE` sets a different location.
- **The Python CLI does not read a token from an environment variable.** A
  variable such as `LAGER_GATEWAY_TOKEN` applies to the Rust SDK. It does not
  apply to `lager`. Use `lager login`.

### Do the first command two times

On a box with a gateway, the **first** command after a new login can fail one
time. The system records the connection between the box and the authentication
server at that moment. The next command is successful. Thus the connection test
does the command two times:

```yaml
      - run: lager hello || lager hello
```

Keep this even if you have not seen the failure. It is the cost of one more
command. It removes a failure that happens on the first job of the day.

---

## 14. Reference data

### Environment variables that the CLI reads

| Variable | Function |
|---|---|
| `LAGER_BOX` | The default box when there is no `--box` flag. The CLI also sends it to the script on the box. |
| `LAGER_CONFIG_FILE_DIR` | The directory of the global `.lager` file. Default `~`. |
| `LAGER_CONFIG_FILE_NAME` | The name of the configuration file. Default `.lager`. |
| `LAGER_GATEWAY_AUTH_FILE` | A different location for `~/.lager_gateway_auth`. |
| `LAGER_USER` | The user identity. It is the lock holder on a user computer. |
| `LAGER_LOCK_HOLDER` | A different identity for the lock holder. |
| `LAGER_LOCK_WAIT` | Seconds to wait after a lock collision. |
| `LAGER_LOCK_TTL` | Maximum life of the lock. Use `none` for no limit. |
| `LAGER_LOCK_HEARTBEAT` | Seconds between two heartbeats. Default 60. |
| `LAGER_AUTO_LOCK_DISABLE` | Set to `1` to stop the automatic lock. |
| `LAGER_DEBUG` | Show the full error data. The same as `--debug`. |
| `LAGER_NO_UPDATE_CHECK` | Stop the background version check. |
| `CI` | The value `true` selects the CI lock behavior. Actions sets this. |

`lager python` sends these variables **to** your script on the box:
`LAGER_BOX`, `LAGER_RUNNABLE`, `LAGER_PROCESS_ID` and `LAGER_OUTPUT_CHANNEL`.

### Exit codes

| Command | Exit code | Meaning |
|---|---|---|
| `lager python` | the code of the script | The CLI does not change it. |
| | 124 | The `--timeout` time ended. SIGTERM. |
| | 137 | The `--timeout` time ended. SIGKILL. |
| | 255 | The CLI could not get the exit code from the box. |
| | 130 | A person or a signal stopped the command. |
| `lager update --check` | 0 / 1 / 2 | Correct / an update is necessary / the condition is not known. |
| `lager exec` | the code of the container | The CLI does not change it. |
| `lager ssh -- cmd` | the code of the remote command | 255 shows an SSH failure. |
| all commands | 1 | A general error. **This includes a lock collision.** |
| all commands | 2 | An error in the command line. An unknown flag or a missing argument. |

### The `.lager` file

The global file `~/.lager` is a JSON file. It is not an INI file.

| Section | Contents |
|---|---|
| `BOXES` | The name of each box, with `{ip, user, version}`. |
| `NETS` | The net definitions for each box. |
| `DEFAULTS` | `gateway_id` (the default box), `user`, and a default net for each role. |

The CLI also reads a `.lager` file in the project. It looks in the work
directory and then in each directory above it.

| Section | Contents |
|---|---|
| `DEVENV` | The container image, the mount point, the shell, the volumes, and the commands. |
| `DEBUG` | The name of a debug net, with the path of a local debug script. |
| `includes` | More directories to upload with `lager python`. |

### Commands that do not exist

These commands do not exist. If you find them in an old example, that example is
not current.

| Not a command | Use this instead |
|---|---|
| `lager test` | `lager python <script-or-dir>` |
| `lager net add` | `lager nets add` |
| `lager nets remove` | `lager nets delete` |
| `lager connect`, `lager debug NET connect` | `flash`, `reset` and `erase` make the connection. |
| `lager debug NET rtt` | `lager debug NET gdbserver --rtt` |
| `lager gdbserver` | `lager debug [NET] gdbserver` |
| `lager box update` | `lager update` |
| `--lock`, `--lock-wait`, `--no-lock` | The lock is automatic. Use the `LAGER_LOCK_*` variables. |
| `lager update --all` | Use a loop in your shell. |

---

## 15. Troubleshooting

**Message: `Error: Box 'BENCH-1' is locked by ...`**
A different job holds the bench. In CI, the command waits for `LAGER_LOCK_WAIT`
seconds before it gives this message. The default is 30 minutes. On a user
computer, the message comes immediately. To find the holder, use `lager boxes`.
A holder that starts with `ci:github:` shows the repository, the run, the job
and the runner. Wait, or speak to the holder. Do not use `unlock --force` in a
job.

**A `lager` command on the box fails immediately, but CI has no failure.**
The lock behavior changes with the `CI=true` variable. A command from cron, from
systemd, or from an SSH session is not in CI. It fails immediately. This is
correct. Set `LAGER_LOCK_WAIT` for that command if it must wait.

**The job does not stop and gives no output.**
Usually the cause is a `lager` command with no subcommand. That command starts
an interactive session. The other cause is an interactive `[y/N]` question. Add
`--yes` if the command accepts it. Add `exec < /dev/null` in a cleanup step.

**`lager exec` does not stop, or it gives an error about a TTY.**
The defaults are `--interactive` and `--tty`. Give `--no-tty` in CI.

**Your test cannot read an environment variable from the workflow.**
The `env:` block of a step applies to the runner. Your script operates on the
box. Only `--env FOO=bar` and `--passenv FOO` send a variable to the box.

```yaml
- run: lager python tests/hil --env LOG_LEVEL=debug --passenv GITHUB_SHA
```

**A background test stops with `SIGTTIN`.**
`lager python` starts an interactive function when the standard input is a TTY.
The function waits for the Enter key. A background process group that reads the
terminal gets a `SIGTTIN` signal. Send the standard input from `/dev/null`.

**Message: `[warning] Box BENCH-1 is on lager X; CLI is on Y.`**
The two versions are different. Update the box with
`lager update --box BENCH-1`. You can also set the CLI of the runner to the
version of the box. A box that reports no version has an image that is too old
for this CLI.

**The tests are successful, but you changed the box software and nothing tested
it.**
`lager python` runs your script in the environment of the box. The box software
comes from the installation on the box. It does not come from your check-out.
Refer to
[Section 7](#7-how-to-make-sure-that-the-box-has-the-code-under-test).

**An attempt reports that tests were successful, but it did not do those
tests.**
The cause is old results in `/tmp` on a self-hosted runner. The other cause is
results with no check of the firmware identity. Refer to
[Section 12](#12-test-results-after-a-retry).

**The flash operation is successful, but each subsequent test fails.**
The programmer reported a connection failure but gave exit code 0. The device is
erased. Look for the failure text in the output of the flash command. Refer to
[Section 6](#6-how-to-put-the-firmware-on-the-dut).

**The bench is in an unsafe condition after a cancelled job.**
The workflow has `cancel-in-progress: true` and no cleanup step. Refer to
[Section 8](#8-how-to-make-the-bench-safe-after-a-job).

**The workflow is successful, but no hardware did a test.**
An empty `strategy.matrix` makes zero jobs, and the workflow is then successful.
Add the `validate` job from
[Section 11](#11-how-to-use-more-than-one-bench).

---

## Appendix: a full workflow for one bench

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

This workflow shows the full method:

1. Build the firmware on a different machine.
2. Do a connection test before you use the bench.
3. Set the check-out to the commit.
4. Do a check of the flash operation.
5. Put each hardware step in the retry script.
6. Make the bench safe at the end.

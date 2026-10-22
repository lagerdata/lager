#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
#
# Box-level commands against a real box: ssh, a command in the box's lager
# container, the box lock, logs, custom binaries and install-wheel.
#
# Each check reads the command's exit status AND what it printed, so a command
# that exits 0 without doing its job fails.
#
# The lock section takes real locks under two made-up holders, A and B. Both
# have the CI holder shape, but a job name no CI job uses, so neither counts
# as this run's own lock and a leak is still one tools/bench_release_lock.sh
# can clear. A third holder, U, is a plain user name, the shape a person's
# lock has. Every lock the suite takes is released by its own holder in a
# cleanup registered BEFORE the first lock, and never with --force. The suite
# refuses to start while anyone else holds a lock on the box. A lock this
# very CI job left behind (an earlier step that did not release it) is
# released by the job itself, as its own holder, with a ::warning naming it.
#
# Usage: box_ops.sh <BOX>
#        box_ops.sh <BOX> --release-locks
#
#   --release-locks   release only the suite's own test locks (A and U), as
#                     their own holders, then exit. The bench workflow runs
#                     this in an always() step, for a suite that was killed
#                     before its cleanup could run.
#
# Environment:
#   GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_RUN_ATTEMPT, RUNNER_NAME
#                     name the test holders; outside CI they default to
#                     lagerdata/lager, 0, 1 and "local".
#
# Run by hand: bash test/integration/infrastructure/box_ops.sh MASTER

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX> [--release-locks]"
  exit 1
fi

BOX="$1"
MODE="${2:-}"

# No "Update available" notice on stdout: the ssh checks compare it exactly.
export LAGER_NO_UPDATE_CHECK=1

REPO="${GITHUB_REPOSITORY:-lagerdata/lager}"
RUN="${GITHUB_RUN_ID:-0}"
ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
RUNNER="${RUNNER_NAME:-local}"
HOLDER_A="ci:github:${REPO}#${RUN}-${ATTEMPT}/box-ops-a@${RUNNER}:1"
HOLDER_B="ci:github:${REPO}#${RUN}-${ATTEMPT}/box-ops-b@${RUNNER}:1"
HOLDER_U="box-ops-user"
# How `lager hello` names A's lock (format_lock_user).
SHOWN_A="github ${REPO} run ${RUN} job box-ops-a on ${RUNNER}"

BIN_NAME="box_ops_probe"
BIN_HOST_DIR="/home/lagerdata/third_party/customer-binaries"
BIN_CONTAINER_DIR="/home/www-data/customer-binaries"

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

# Run lager as holder $1: both the lock holder and the user, so the stored
# lock matches it exactly and nothing else does.
as_holder() {
  local h="$1"
  shift
  LAGER_USER="$h" LAGER_LOCK_HOLDER="$h" lager "$@"
}

# Print who holds the box lock, as `lager hello` names it, or nothing. The
# probe identity is nobody's, so every lock is named (the probe that
# tools/bench_release_lock.sh uses).
lock_holder_shown() {
  as_holder bench-release-lock-probe hello --box "$BOX" 2>&1 \
    | grep -o "is locked by [^;]*" | head -1 | sed 's/^is locked by //'
}

release_test_locks() {
  local h
  for h in "$HOLDER_A" "$HOLDER_U"; do
    as_holder "$h" boxes unlock --box "$BOX" --user "$h" >/dev/null 2>&1
  done
}

if [ "$MODE" = "--release-locks" ]; then
  release_test_locks
  shown=$(lock_holder_shown)
  case "$shown" in
    *"job box-ops-a on"*|"$HOLDER_U")
      echo "::warning title=box_ops.sh test lock still held::Box ${BOX} is still locked by ${shown}."
      exit 1 ;;
  esac
  echo "No box_ops.sh test lock held on ${BOX}."
  exit 0
elif [ -n "$MODE" ]; then
  echo "Unknown option: $MODE" >&2
  exit 1
fi

# Run one check. $1 = description, $2 = detail printed on failure, the rest
# is the condition, run as a command: exit 0 passes.
check() {
  local desc="$1" detail="$2"
  shift 2
  echo -n "Test: $desc... "
  if "$@"; then
    track_test "pass"
  else
    track_test "fail"
    [ -n "$detail" ] && echo "    detail: $(printf '%s' "$detail" | head -3 | tr '\n' ' ')"
  fi
}

# $1 = exit status, $2 = output, $3 = pattern the output must contain.
succeeded() { [ "$1" -eq 0 ] && printf '%s' "$2" | grep -qE -- "$3"; }
refused() { [ "$1" -ne 0 ] && printf '%s' "$2" | grep -qE -- "$3"; }
rc_is() { [ "$1" -eq "$2" ]; }
equals() { [ "$1" = "$2" ]; }

# Run lager into OUT (stdout and stderr) and RC.
run() {
  OUT=$(lager "$@" 2>&1); RC=$?
  echo "    $ lager $* -> rc=$RC: $(printf '%s' "$OUT" | head -1)"
}
# The same as holder $1.
run_as() {
  local h="$1"
  shift
  OUT=$(as_holder "$h" "$@" 2>&1); RC=$?
  echo "    $ lager $* (as ${h##*/}) -> rc=$RC: $(printf '%s' "$OUT" | head -1)"
}
# Run `lager ssh -- <cmd>` into STDOUT (stdout only) and RC.
ssh_run() {
  STDOUT=$(lager ssh --box "$BOX" -- "$@" 2>/dev/null); RC=$?
  echo "    $ lager ssh -- $* -> rc=$RC: $(printf '%s' "$STDOUT" | head -1)"
}

# Kill on a cancel still runs the EXIT cleanups (the test locks).
trap 'exit 143' TERM INT HUP

init_harness

print_script_header "Box-level commands" "$BOX"

HELD=$(lock_holder_shown)
# This job's own lock: the probe names it, but a plain hello under the job's
# identity does not, because the CLI counts it as the caller's.
if [ -n "$HELD" ] && [ -n "${GITHUB_RUN_ID:-}" ] \
    && [[ "$HELD" == "github ${REPO} run ${RUN} "* ]] \
    && ! lager hello --box "$BOX" 2>&1 | grep -q "is locked by"; then
  echo "::warning title=Box lock left by an earlier step::Box ${BOX} is locked by ${HELD}, this job's own holder, with no step running. Releasing it as that holder before the lock tests."
  lager boxes unlock --box "$BOX" >/dev/null 2>&1
  HELD=$(lock_holder_shown)
fi
if [ -n "$HELD" ]; then
  echo "Box ${BOX} is locked by ${HELD}. This suite takes and releases locks, so it does not run on a box someone else has locked."
  echo "Nothing was changed. Rerun once the lock is released."
  exit 1
fi

# ------------------------------------------------------------
start_section "ssh pass-through"
# ------------------------------------------------------------

ssh_run true
check "ssh -- true exits 0" "rc=$RC" rc_is "$RC" 0

ssh_run sh -c 'exit 7'
check "ssh passes the remote exit status through (7)" "rc=$RC" rc_is "$RC" 7

ssh_run echo box-ops-marker
check "ssh stdout is the remote stdout, unchanged" "got '$STDOUT'" equals "$STDOUT" "box-ops-marker"

ssh_run printf '%s\n' 'a b'
check "ssh keeps an argument's spaces" "got '$STDOUT'" equals "$STDOUT" "a b"

ssh_run sh -c 'echo box-ops-err >&2'
check "remote stderr stays off stdout" "got '$STDOUT'" equals "$STDOUT" ""

# ------------------------------------------------------------
start_section "Command in the box's lager container"
# ------------------------------------------------------------

ssh_run docker exec lager true
check "docker exec lager true exits 0" "rc=$RC" rc_is "$RC" 0

ssh_run docker exec lager sh -c 'exit 3'
check "the container command's exit status comes back (3)" "rc=$RC" rc_is "$RC" 3

ssh_run docker exec lager python3 -c 'import lager; print("box-ops-import-ok")'
check "the container imports the lager package" "rc=$RC got '$STDOUT'" \
  equals "$STDOUT" "box-ops-import-ok"

# ------------------------------------------------------------
start_section "Box lock"
# ------------------------------------------------------------

# Registered before the first lock: a failed or killed run still releases.
harness_add_cleanup release_test_locks

run_as "$HOLDER_A" boxes lock --box "$BOX" --user "$HOLDER_A"
check "holder A locks the box" "$OUT" succeeded "$RC" "$OUT" "is locked by"
check "the lock is shown as A's" "shown: $(lock_holder_shown)" equals "$(lock_holder_shown)" "$SHOWN_A"

run_as "$HOLDER_A" boxes lock --box "$BOX" --user "$HOLDER_A"
check "A locking again is accepted" "$OUT" succeeded "$RC" "$OUT" "is locked by"

run_as "$HOLDER_B" boxes lock --box "$BOX" --user "$HOLDER_B"
check "a second holder cannot take the lock" "$OUT" refused "$RC" "$OUT" "already locked by"

run_as "$HOLDER_B" ssh --box "$BOX" -- true
check "a second holder's ssh is refused" "$OUT" refused "$RC" "$OUT" "is locked by"

run_as "$HOLDER_B" hello --box "$BOX"
check "hello still runs for a second holder, read-only" "$OUT" \
  succeeded "$RC" "$OUT" "running read-only"

run_as "$HOLDER_B" nets --box "$BOX"
check "the net list still runs for a second holder, read-only" "$OUT" \
  succeeded "$RC" "$OUT" "running read-only"

run_as "$HOLDER_B" binaries list --box "$BOX"
check "binaries list still runs for a second holder, read-only" "$OUT" \
  succeeded "$RC" "$OUT" "running read-only"

run_as "$HOLDER_B" boxes unlock --box "$BOX" --user "$HOLDER_B"
check "a second holder cannot unlock without --force" "$OUT" refused "$RC" "$OUT" "is locked by"
check "the lock is still A's" "shown: $(lock_holder_shown)" equals "$(lock_holder_shown)" "$SHOWN_A"

run boxes list
check "boxes list shows the lock" "$OUT" succeeded "$RC" "$OUT" "locked by"

run_as "$HOLDER_A" ssh --box "$BOX" -- true
check "the holder's own ssh still runs" "$OUT" rc_is "$RC" 0

run_as "$HOLDER_A" boxes unlock --box "$BOX" --user "$HOLDER_A"
check "A unlocks the box" "$OUT" succeeded "$RC" "$OUT" "is now unlocked"
check "no lock is held afterwards" "shown: $(lock_holder_shown)" equals "$(lock_holder_shown)" ""

# ------------------------------------------------------------
start_section "Stale lock release (tools/bench_release_lock.sh)"
# ------------------------------------------------------------

# A lock left by a run of this repository is released...
run_as "$HOLDER_A" boxes lock --box "$BOX" --user "$HOLDER_A"
SHOWN=$(lock_holder_shown)
if [ "$RC" -eq 0 ] && [ "$SHOWN" = "$SHOWN_A" ]; then
  REL=$(GITHUB_REPOSITORY="$REPO" bash "${REPO_ROOT}/tools/bench_release_lock.sh" "$BOX" 3 2 2>&1)
  echo "    $ bench_release_lock.sh -> $(printf '%s' "$REL" | head -1)"
  check "a run-held lock is released" "$REL" \
    succeeded 0 "$REL" "Released the box lock on ${BOX} held by ${SHOWN_A}"
  check "the release is real" "shown: $(lock_holder_shown)" equals "$(lock_holder_shown)" ""
else
  # Never hand the release tool a lock that is not the suite's own.
  check "lock as A before the release" "rc=$RC shown: $SHOWN" false
  release_test_locks
fi

# ...and a person's lock is left alone.
run_as "$HOLDER_U" boxes lock --box "$BOX" --user "$HOLDER_U"
check "a user lock is taken" "$OUT" succeeded "$RC" "$OUT" "is locked by"
if [ "$(lock_holder_shown)" = "$HOLDER_U" ]; then
  REL=$(GITHUB_REPOSITORY="$REPO" bash "${REPO_ROOT}/tools/bench_release_lock.sh" "$BOX" 3 2 2>&1)
  echo "    $ bench_release_lock.sh -> $(printf '%s' "$REL" | head -1)"
  check "a person's lock is reported, not released" "$REL" \
    succeeded 0 "$REL" "held by someone else"
  check "the person's lock is still held" "shown: $(lock_holder_shown)" \
    equals "$(lock_holder_shown)" "$HOLDER_U"
else
  check "the user lock is shown as U's" "shown: $(lock_holder_shown)" false
fi
run_as "$HOLDER_U" boxes unlock --box "$BOX" --user "$HOLDER_U"
check "the user unlocks it" "$OUT" succeeded "$RC" "$OUT" "is now unlocked"
check "no lock is held afterwards" "shown: $(lock_holder_shown)" equals "$(lock_holder_shown)" ""

# ------------------------------------------------------------
start_section "Logs"
# ------------------------------------------------------------

# `logs size` exits 0 even when its ssh fails, so the text is the check.
run logs size --box "$BOX"
check "logs size names the box" "$OUT" succeeded "$RC" "$OUT" "^${BOX} \("
check "logs size reports a total" "$OUT" succeeded "$RC" "$OUT" "Total logs: [0-9.]+[KMGT]?"
check "the logs are not over the clean-now size" "$OUT" \
  bash -c '! printf "%s" "$1" | grep -q "Logs are very large"' _ "$OUT"

# --verbose lists the *.log files, and prints no list when there are none.
# Either way the file list must have come back, and each line names a size.
verbose_ok() {
  succeeded "$RC" "$OUT" "Total logs: " || return 1
  printf '%s' "$OUT" | grep -q "did not return the file list" && return 1
  if printf '%s' "$OUT" | grep -q "Individual files:"; then
    printf '%s\n' "$OUT" | sed -n '/Individual files:/,$p' | sed 1d \
      | grep -vqE '^    .+: [0-9.]+[KMGT]?$' && return 1
  fi
  return 0
}
run logs size --box "$BOX" --verbose
check "logs size --verbose answers, with any file list well formed" "$OUT" verbose_ok

# ------------------------------------------------------------
start_section "Custom binaries"
# ------------------------------------------------------------

BIN_DIR=$(mktemp -d)
BIN_FILE="${BIN_DIR}/${BIN_NAME}"
printf '#!/bin/sh\necho box-ops-probe "$@"\n' > "$BIN_FILE"
chmod +x "$BIN_FILE"
remove_probe_binary() {
  lager binaries remove "$BIN_NAME" --box "$BOX" --yes >/dev/null 2>&1
  rm -rf "$BIN_DIR"
}
harness_add_cleanup remove_probe_binary
# A leftover from a killed run.
lager binaries remove "$BIN_NAME" --box "$BOX" --yes >/dev/null 2>&1

run binaries add "$BIN_FILE" --box "$BOX" --name "$BIN_NAME" --yes
check "binaries add uploads the file" "$OUT" succeeded "$RC" "$OUT" "uploaded successfully"

run binaries list --box "$BOX"
check "binaries list shows it as executable" "$OUT" \
  succeeded "$RC" "$OUT" "- ${BIN_NAME} \(.*\(executable\)"

ssh_run "${BIN_HOST_DIR}/${BIN_NAME}" x
check "it runs on the box host" "rc=$RC got '$STDOUT'" equals "$STDOUT" "box-ops-probe x"

ssh_run docker exec lager "${BIN_CONTAINER_DIR}/${BIN_NAME}" y
check "it runs in the container" "rc=$RC got '$STDOUT'" equals "$STDOUT" "box-ops-probe y"

run binaries remove "$BIN_NAME" --box "$BOX" --yes
check "binaries remove removes it" "$OUT" succeeded "$RC" "$OUT" "removed from"

run binaries list --box "$BOX"
check "binaries list no longer shows it" "$OUT" \
  bash -c '[ "$1" -eq 0 ] && ! printf "%s" "$2" | grep -q -- "- $3 ("' _ "$RC" "$OUT" "$BIN_NAME"

run binaries remove "$BIN_NAME" --box "$BOX" --yes
check "removing it again says it is not there" "$OUT" refused "$RC" "$OUT" "not found"

# ------------------------------------------------------------
start_section "install-wheel (no upload)"
# ------------------------------------------------------------

run install-wheel --help
check "install-wheel --help" "$OUT" succeeded "$RC" "$OUT" "Install a local Python wheel file"

run install-wheel --box "$BOX" "${BIN_DIR}/missing-0.0.0-py3-none-any.whl"
check "a missing wheel is refused" "$OUT" refused "$RC" "$OUT" "Wheel file not found"

printf 'not a wheel\n' > "${BIN_DIR}/notes.txt"
run install-wheel --box "$BOX" "${BIN_DIR}/notes.txt"
check "a file that is not a wheel is refused" "$OUT" refused "$RC" "$OUT" "does not appear to be a wheel"

print_summary
exit_with_status

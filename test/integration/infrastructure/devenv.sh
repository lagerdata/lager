#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Integration test suite for lager devenv commands.
#
# Tests the CURRENT devenv model: one devenv per project, configured in the
# project's .lager file (create/show/set/unset, commands add/delete, mount
# and env config). The previous version of this suite tested the removed
# named-environments model (create --name / list / remove) and failed on
# every one of those commands the first time it ran in CI.
#
# Everything here is config-level: `create` writes .lager and no container
# is ever started, so Docker is NOT required and no image is pulled. The
# container-side behavior (terminal, exec) stays out of scope -- it needs a
# TTY and an image pull, neither of which belongs in this suite.
#
# All work happens in a throwaway directory: devenv config is project-local,
# and .lager discovery walks parent directories, so running from a real
# checkout would read (or write!) that checkout's config.
#
# Usage: ./test/integration/infrastructure/devenv.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

init_harness

WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/lager_devenv_test.XXXXXX")
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT
cd "$WORKDIR" || exit 1

TEST_IMAGE="debian:bookworm-slim"

echo "========================================================================"
echo "LAGER DEVENV COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Working directory: $WORKDIR"
echo ""

# ============================================================
# SECTION 1: HELP COMMANDS
# ============================================================
start_section "Help Commands"

echo "Test 1.1: Devenv command help"
lager devenv --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: Devenv create help"
lager devenv create --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Devenv show help"
lager devenv show --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.4: Devenv mount help"
lager devenv mount --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: NO-CONFIG ERROR PATHS
# ============================================================
start_section "No-Config Errors"

echo "Test 2.1: show without a .lager file fails informatively"
OUTPUT=$(lager devenv show 2>&1)
if [ ! -e .lager ] && echo "$OUTPUT" | grep -qi "No .lager config"; then
  track_test "pass"
else
  echo "$OUTPUT" | head -3
  track_test "fail"
fi
echo ""

echo "Test 2.2: set without a .lager file fails informatively"
OUTPUT=$(lager devenv set shell /bin/sh 2>&1)
if echo "$OUTPUT" | grep -qi "No .lager config"; then
  track_test "pass"
else
  echo "$OUTPUT" | head -3
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 3: CREATE AND CONFIG LIFECYCLE
# ============================================================
start_section "Create and Configure"

echo "Test 3.1: create with all options is non-interactive and writes .lager"
# All three options given: create prompts for anything it was not told, and
# a prompt in a non-TTY context aborts -- see Test 5.1, which pins exactly
# that.
lager devenv create --image "$TEST_IMAGE" --mount-dir /app --shell /bin/sh \
  && [ -f .lager ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: show reflects what create wrote"
OUTPUT=$(lager devenv show 2>&1)
if echo "$OUTPUT" | grep -q "$TEST_IMAGE" \
    && echo "$OUTPUT" | grep -q "/app" \
    && echo "$OUTPUT" | grep -q "/bin/sh"; then
  track_test "pass"
else
  echo "$OUTPUT" | head -6
  track_test "fail"
fi
echo ""

echo "Test 3.3: set/show roundtrip"
lager devenv set shell /bin/bash >/dev/null 2>&1
if lager devenv show 2>&1 | grep -q "/bin/bash"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 3.4: unset removes the key"
lager devenv unset shell >/dev/null 2>&1
if lager devenv show 2>&1 | grep -q "/bin/bash"; then
  track_test "fail"
else
  track_test "pass"
fi
echo ""

echo "Test 3.5: commands starts empty and add/delete roundtrips"
lager devenv add build "make all" >/dev/null 2>&1
ADDED=$(lager devenv commands 2>&1)
lager devenv delete build >/dev/null 2>&1
GONE=$(lager devenv commands 2>&1)
if echo "$ADDED" | grep -q "build" && ! echo "$GONE" | grep -q "build"; then
  track_test "pass"
else
  echo "after add: $(echo "$ADDED" | head -2)"
  echo "after delete: $(echo "$GONE" | head -2)"
  track_test "fail"
fi
echo ""

echo "Test 3.6: env config help is reachable"
lager devenv env --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: HEADLESS BEHAVIOR
# ============================================================
start_section "Headless Behavior"

echo "Test 5.1: create with missing options ABORTS headless, never hangs"
# Without --mount-dir, create prompts. In a non-TTY context the prompt must
# abort promptly; hanging forever is the failure mode this pins (same
# headless-prompt class as the box_config `show` hang and the historical
# "Invalid sudoers syntax" install failure).
rm -f .lager
timeout 15 lager devenv create --image "$TEST_IMAGE" < /dev/null >/dev/null 2>&1
RC=$?
if [ $RC -eq 124 ]; then
  echo "create HUNG on a prompt in a non-TTY context (timeout hit)"
  track_test "fail"
elif [ $RC -ne 0 ] && [ ! -f .lager ]; then
  track_test "pass"
else
  echo "expected a prompt abort; rc=$RC, .lager present: $([ -f .lager ] && echo yes || echo no)"
  track_test "fail"
fi
echo ""

# ============================================================
# SUMMARY
# ============================================================

print_summary
exit_with_status

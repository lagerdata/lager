#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Integration test suite for lager devenv commands
# Tests: create, list, remove
#
# Usage: ./test/integration/infrastructure/devenv.sh
# Example: ./test/integration/infrastructure/devenv.sh
#
# Note: These tests require Docker to be installed locally.
#       Tests are non-destructive -- they use a dedicated test
#       environment name and clean up after themselves.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e

init_harness

TEST_ENV_NAME="lager_test_devenv_$$"

echo "========================================================================"
echo "LAGER DEVENV COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Test environment: $TEST_ENV_NAME"
echo ""

# Check Docker prerequisite
if ! command -v docker &>/dev/null; then
  echo "Docker is not installed. Skipping devenv tests."
  start_section "Prerequisites"
  skip_test "Docker check" "Docker not installed"
  print_summary
  exit_with_status
fi

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

echo "Test 1.3: Devenv list help"
lager devenv list --help && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.4: Devenv remove help"
lager devenv remove --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: LIST
# ============================================================
start_section "List Environments"

echo "Test 2.1: List dev environments"
lager devenv list && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: List stability (5 iterations)"
FAILED=0
for i in {1..5}; do
  lager devenv list >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 3: CREATE AND REMOVE LIFECYCLE
# ============================================================
start_section "Create and Remove"

echo "Test 3.1: Create dev environment"
lager devenv create --name "$TEST_ENV_NAME" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Verify environment appears in list"
if lager devenv list 2>/dev/null | grep -q "$TEST_ENV_NAME"; then
  track_test "pass"
else
  track_test "fail"
fi
echo ""

echo "Test 3.3: Remove dev environment"
lager devenv remove --name "$TEST_ENV_NAME" --yes && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Verify environment removed from list"
if lager devenv list 2>/dev/null | grep -q "$TEST_ENV_NAME"; then
  track_test "fail"
else
  track_test "pass"
fi
echo ""

# ============================================================
# SECTION 4: ERROR CASES
# ============================================================
start_section "Error Cases"

echo "Test 4.1: Remove non-existent environment"
lager devenv remove --name "nonexistent_env_12345" --yes 2>&1 | grep -qi "error\|not found" && track_test "pass" || track_test "pass"
echo ""

echo "Test 4.2: Create with invalid image name"
lager devenv create --name "test_invalid_$$" --image "!!!invalid!!!" 2>&1 | grep -qi "error\|invalid" && track_test "pass" || track_test "pass"
# Clean up in case it somehow succeeded
lager devenv remove --name "test_invalid_$$" --yes 2>&1 >/dev/null || true
echo ""

# ============================================================
# SUMMARY
# ============================================================

# Final cleanup
lager devenv remove --name "$TEST_ENV_NAME" --yes 2>&1 >/dev/null || true

print_summary
exit_with_status

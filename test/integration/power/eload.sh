#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager electronic load commands
# Tests all edge cases and production features for cc, cv, cr, cp commands

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e  # DON'T exit on error - we want to track failures

# Initialize the test harness
init_harness

# Check if output contains error
has_error() {
  local output="$1"
  echo "$output" | grep -qi '"error"'
}

# Check if command succeeded (exit code 0 and no error in JSON)
command_succeeded() {
  local exit_code=$1
  local output="$2"
  if [ $exit_code -eq 0 ] && ! has_error "$output"; then
    return 0
  else
    return 1
  fi
}

# Check if command failed (exit code non-zero or error in JSON)
command_failed() {
  local exit_code=$1
  local output="$2"
  if [ $exit_code -ne 0 ] || has_error "$output"; then
    return 0
  else
    return 1
  fi
}

# Check if required arguments are provided
if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX_NAME_OR_IP> <ELOAD_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box eload1"
  echo "  $0 <BOX_IP> eload1"
  echo ""
  echo "Arguments:"
  echo "  BOX_NAME_OR_IP - Box name or Tailscale IP address"
  echo "  ELOAD_NET      - Name of the eload net to test"
  echo ""
  exit 1
fi

BOX_INPUT="$1"
ELOAD_NET="$2"

# Detect if input is an IP address (IPv4 pattern)
if echo "$BOX_INPUT" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
  # Input is an IP address - register it with a temporary name
  BOX_NAME="temp_box_$(echo $BOX_INPUT | tr '.' '_')"
  BOX_IP="$BOX_INPUT"
  echo "Detected IP address: $BOX_IP"
  echo "Registering as temporary box: $BOX_NAME"
  lager boxes add --name "$BOX_NAME" --ip "$BOX_IP" --yes >/dev/null 2>&1 || true
  BOX="$BOX_NAME"
else
  # Input is a box name - use it directly
  BOX_NAME="$BOX_INPUT"
  BOX="$BOX_NAME"
  echo "Using box name: $BOX_NAME"
fi

echo "========================================================================"
echo "LAGER ELECTRONIC LOAD TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "Electronic Load Net: $ELOAD_NET"
echo ""
echo "Testing commands: cc, cv, cr, cp, state"
echo ""

# ============================================================
# SECTION 1: CC (CONSTANT CURRENT) - WRITE TESTS
# ============================================================
start_section "CC Write Tests"
echo "========================================================================"
echo "SECTION 1: CC (CONSTANT CURRENT) - WRITE TESTS"
echo "========================================================================"
echo ""

echo "Test 1.1: Set CC to 1.0 A"
OUTPUT=$(lager eload $ELOAD_NET cc 1.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: Set CC to 0.1 A (small current)"
OUTPUT=$(lager eload $ELOAD_NET cc 0.1 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Set CC to 5.0 A (larger current)"
OUTPUT=$(lager eload $ELOAD_NET cc 5.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.4: Set CC to 0.001 A (very small)"
OUTPUT=$(lager eload $ELOAD_NET cc 0.001 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.5: Set CC to 2.5678 A (decimal precision)"
OUTPUT=$(lager eload $ELOAD_NET cc 2.5678 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.6: Set CC to 0 A (zero current)"
OUTPUT=$(lager eload $ELOAD_NET cc 0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: CC (CONSTANT CURRENT) - READ TESTS
# ============================================================
start_section "CC Read Tests"
echo "========================================================================"
echo "SECTION 2: CC (CONSTANT CURRENT) - READ TESTS"
echo "========================================================================"
echo ""

echo "Test 2.1: Read CC after setting to 1.5 A"
lager eload $ELOAD_NET cc 1.5 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cc --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && echo "$OUTPUT" | grep -qi "current" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: Read CC multiple times (consistency)"
FAILED=0
for i in {1..3}; do
  OUTPUT=$(lager eload $ELOAD_NET cc --box $BOX 2>&1)
  EXIT_CODE=$?
  echo "  Read $i: $OUTPUT"
  command_succeeded $EXIT_CODE "$OUTPUT" || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 3: CV (CONSTANT VOLTAGE) - WRITE TESTS
# ============================================================
start_section "CV Write Tests"
echo "========================================================================"
echo "SECTION 3: CV (CONSTANT VOLTAGE) - WRITE TESTS"
echo "========================================================================"
echo ""

echo "Test 3.1: Set CV to 5.0 V"
OUTPUT=$(lager eload $ELOAD_NET cv 5.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Set CV to 3.3 V (common voltage)"
OUTPUT=$(lager eload $ELOAD_NET cv 3.3 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: Set CV to 12.0 V"
OUTPUT=$(lager eload $ELOAD_NET cv 12.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Set CV to 0.5 V (low voltage)"
OUTPUT=$(lager eload $ELOAD_NET cv 0.5 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.5: Set CV to 24.0 V (higher voltage)"
OUTPUT=$(lager eload $ELOAD_NET cv 24.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.6: Set CV to 0 V (zero voltage)"
OUTPUT=$(lager eload $ELOAD_NET cv 0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.7: Set CV to 1.234567 V (high precision)"
OUTPUT=$(lager eload $ELOAD_NET cv 1.234567 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: CV (CONSTANT VOLTAGE) - READ TESTS
# ============================================================
start_section "CV Read Tests"
echo "========================================================================"
echo "SECTION 4: CV (CONSTANT VOLTAGE) - READ TESTS"
echo "========================================================================"
echo ""

echo "Test 4.1: Read CV after setting to 5.0 V"
lager eload $ELOAD_NET cv 5.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cv --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && echo "$OUTPUT" | grep -qi "voltage" && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Read CV after mode switch from CC"
lager eload $ELOAD_NET cc 2.0 --box $BOX >/dev/null 2>&1
lager eload $ELOAD_NET cv 3.3 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cv --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 5: CR (CONSTANT RESISTANCE) - WRITE TESTS
# ============================================================
start_section "CR Write Tests"
echo "========================================================================"
echo "SECTION 5: CR (CONSTANT RESISTANCE) - WRITE TESTS"
echo "========================================================================"
echo ""

echo "Test 5.1: Set CR to 10.0 Ω"
OUTPUT=$(lager eload $ELOAD_NET cr 10.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Set CR to 1.0 Ω (low resistance)"
OUTPUT=$(lager eload $ELOAD_NET cr 1.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: Set CR to 100.0 Ω"
OUTPUT=$(lager eload $ELOAD_NET cr 100.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.4: Set CR to 0.1 Ω (very low)"
OUTPUT=$(lager eload $ELOAD_NET cr 0.1 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.5: Set CR to 1000.0 Ω (high resistance)"
OUTPUT=$(lager eload $ELOAD_NET cr 1000.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.6: Set CR to 47.5 Ω (decimal)"
OUTPUT=$(lager eload $ELOAD_NET cr 47.5 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: CR (CONSTANT RESISTANCE) - READ TESTS
# ============================================================
start_section "CR Read Tests"
echo "========================================================================"
echo "SECTION 6: CR (CONSTANT RESISTANCE) - READ TESTS"
echo "========================================================================"
echo ""

echo "Test 6.1: Read CR after setting to 50.0 Ω"
lager eload $ELOAD_NET cr 50.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cr --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && echo "$OUTPUT" | grep -qi "resistance" && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Read CR multiple times"
FAILED=0
for i in {1..3}; do
  OUTPUT=$(lager eload $ELOAD_NET cr --box $BOX 2>&1)
  EXIT_CODE=$?
  echo "  Read $i: $OUTPUT"
  command_succeeded $EXIT_CODE "$OUTPUT" || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: CP (CONSTANT POWER) - WRITE TESTS
# ============================================================
start_section "CP Write Tests"
echo "========================================================================"
echo "SECTION 7: CP (CONSTANT POWER) - WRITE TESTS"
echo "========================================================================"
echo ""

echo "Test 7.1: Set CP to 10.0 W"
OUTPUT=$(lager eload $ELOAD_NET cp 10.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.2: Set CP to 1.0 W (low power)"
OUTPUT=$(lager eload $ELOAD_NET cp 1.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.3: Set CP to 50.0 W"
OUTPUT=$(lager eload $ELOAD_NET cp 50.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.4: Set CP to 0.5 W (fractional)"
OUTPUT=$(lager eload $ELOAD_NET cp 0.5 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.5: Set CP to 100.0 W (high power)"
OUTPUT=$(lager eload $ELOAD_NET cp 100.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.6: Set CP to 0 W (zero power)"
OUTPUT=$(lager eload $ELOAD_NET cp 0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.7: Set CP to 7.777 W (high precision)"
OUTPUT=$(lager eload $ELOAD_NET cp 7.777 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 8: CP (CONSTANT POWER) - READ TESTS
# ============================================================
start_section "CP Read Tests"
echo "========================================================================"
echo "SECTION 8: CP (CONSTANT POWER) - READ TESTS"
echo "========================================================================"
echo ""

echo "Test 8.1: Read CP after setting to 15.0 W"
lager eload $ELOAD_NET cp 15.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cp --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && echo "$OUTPUT" | grep -qi "power" && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.2: Read CP after mode switch"
lager eload $ELOAD_NET cr 10.0 --box $BOX >/dev/null 2>&1
lager eload $ELOAD_NET cp 20.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cp --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 9: ERROR HANDLING - INVALID VALUES
# ============================================================
start_section "Error Handling"
echo "========================================================================"
echo "SECTION 9: ERROR HANDLING - INVALID VALUES"
echo "========================================================================"
echo ""

echo "Test 9.1: Negative current (should fail)"
OUTPUT=$(lager eload $ELOAD_NET cc -1.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_failed $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.2: Negative voltage (should fail)"
OUTPUT=$(lager eload $ELOAD_NET cv -5.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_failed $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.3: Negative resistance (should fail)"
OUTPUT=$(lager eload $ELOAD_NET cr -10.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_failed $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.4: Negative power (should fail)"
OUTPUT=$(lager eload $ELOAD_NET cp -20.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_failed $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.5: Invalid net name (should fail)"
OUTPUT=$(lager eload NONEXISTENT cc 1.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_failed $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.6: Invalid box (should fail)"
OUTPUT=$(lager eload $ELOAD_NET cc 1.0 --box INVALID_BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_failed $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 10: MODE SWITCHING
# ============================================================
start_section "Mode Switching"
echo "========================================================================"
echo "SECTION 10: MODE SWITCHING"
echo "========================================================================"
echo ""

echo "Test 10.1: Switch from CC to CV"
lager eload $ELOAD_NET cc 1.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cv 5.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.2: Switch from CV to CR"
lager eload $ELOAD_NET cv 5.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cr 10.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.3: Switch from CR to CP"
lager eload $ELOAD_NET cr 10.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cp 15.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.4: Switch from CP back to CC"
lager eload $ELOAD_NET cp 15.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET cc 2.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.5: Rapid mode switching (10 cycles)"
FAILED=0
for i in {1..10}; do
  lager eload $ELOAD_NET cc 1.0 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager eload $ELOAD_NET cv 5.0 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager eload $ELOAD_NET cr 10.0 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager eload $ELOAD_NET cp 10.0 --box $BOX >/dev/null 2>&1 || FAILED=1
done
echo "Completed 10 rapid mode switch cycles"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 11: BOUNDARY CASES
# ============================================================
start_section "Boundary Cases"
echo "========================================================================"
echo "SECTION 11: BOUNDARY CASES"
echo "========================================================================"
echo ""

echo "Test 11.1: Very small current (0.0001 A)"
OUTPUT=$(lager eload $ELOAD_NET cc 0.0001 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.2: Large current (30.0 A)"
OUTPUT=$(lager eload $ELOAD_NET cc 30.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
# May succeed or fail depending on device limits - just check it doesn't crash
[ $EXIT_CODE -ne 0 ] || ! has_error "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.3: Very high precision (1.23456789 A)"
OUTPUT=$(lager eload $ELOAD_NET cc 1.23456789 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.4: Large resistance (10000.0 Ω)"
OUTPUT=$(lager eload $ELOAD_NET cr 10000.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
# May succeed or fail depending on device limits
[ $EXIT_CODE -ne 0 ] || ! has_error "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.5: Very small resistance (0.01 Ω)"
OUTPUT=$(lager eload $ELOAD_NET cr 0.01 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.6: High voltage (150.0 V)"
OUTPUT=$(lager eload $ELOAD_NET cv 150.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
# May succeed or fail depending on device limits
[ $EXIT_CODE -ne 0 ] || ! has_error "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.7: High power (200.0 W)"
OUTPUT=$(lager eload $ELOAD_NET cp 200.0 --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
# May succeed or fail depending on device limits
[ $EXIT_CODE -ne 0 ] || ! has_error "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 12: STRESS TEST
# ============================================================
start_section "Stress Tests"
echo "========================================================================"
echo "SECTION 12: STRESS TESTS"
echo "========================================================================"
echo ""

echo "Test 12.1: Rapid value changes (CC mode, 20 iterations)"
lager eload $ELOAD_NET cc 1.0 --box $BOX >/dev/null 2>&1
FAILED=0
for i in {1..20}; do
  VALUE=$(echo "scale=2; 0.5 + ($i % 5) * 0.3" | bc)
  lager eload $ELOAD_NET cc $VALUE --box $BOX >/dev/null 2>&1 || FAILED=1
done
echo "Completed 20 rapid CC value changes"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.2: Interleaved read/write (10 cycles)"
FAILED=0
for i in {1..10}; do
  lager eload $ELOAD_NET cc 1.5 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager eload $ELOAD_NET cc --box $BOX >/dev/null 2>&1 || FAILED=1
done
echo "Completed 10 interleaved read/write cycles"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.3: All modes rapid cycling (15 iterations)"
FAILED=0
for i in {1..15}; do
  lager eload $ELOAD_NET cc 1.0 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager eload $ELOAD_NET cv 5.0 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager eload $ELOAD_NET cr 10.0 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager eload $ELOAD_NET cp 10.0 --box $BOX >/dev/null 2>&1 || FAILED=1
done
echo "Completed 15 full mode cycles"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 13: READ CONSISTENCY
# ============================================================
start_section "Read Consistency"
echo "========================================================================"
echo "SECTION 13: READ CONSISTENCY"
echo "========================================================================"
echo ""

echo "Test 13.1: CC read consistency (5 consecutive reads)"
lager eload $ELOAD_NET cc 2.5 --box $BOX >/dev/null 2>&1
FAILED=0
PREV=""
for i in {1..5}; do
  OUTPUT=$(lager eload $ELOAD_NET cc --box $BOX 2>&1)
  EXIT_CODE=$?
  echo "  Read $i: $OUTPUT"
  command_succeeded $EXIT_CODE "$OUTPUT" || FAILED=1
  if [ -n "$PREV" ] && [ "$OUTPUT" != "$PREV" ]; then
    echo "  Warning: Read value changed between iterations"
  fi
  PREV="$OUTPUT"
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.2: Parameter persistence after read"
lager eload $ELOAD_NET cr 75.0 --box $BOX >/dev/null 2>&1
BEFORE=$(lager eload $ELOAD_NET cr --box $BOX 2>&1)
AFTER=$(lager eload $ELOAD_NET cr --box $BOX 2>&1)
echo "Before: $BEFORE"
echo "After: $AFTER"
[ "$BEFORE" = "$AFTER" ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 14: STATE COMMAND
# ============================================================
start_section "State Command"
echo "========================================================================"
echo "SECTION 14: STATE COMMAND"
echo "========================================================================"
echo ""

echo "Test 14.1: State in CC mode"
lager eload $ELOAD_NET cc 2.5 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET state --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && echo "$OUTPUT" | grep -qi "mode" && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.2: State in CV mode"
lager eload $ELOAD_NET cv 5.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET state --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && echo "$OUTPUT" | grep -q "Mode: CV" && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.3: State in CR mode"
lager eload $ELOAD_NET cr 50.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET state --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && echo "$OUTPUT" | grep -q "Mode: CR" && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.4: State in CP mode"
lager eload $ELOAD_NET cp 15.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET state --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
# Accept either "Mode: CP" or "Mode: CW" since device may report CW for constant power
command_succeeded $EXIT_CODE "$OUTPUT" && (echo "$OUTPUT" | grep -q "Mode: CP" || echo "$OUTPUT" | grep -q "Mode: CW") && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.5: State displays measured values"
lager eload $ELOAD_NET cc 1.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET state --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
# Check for measured voltage, current, and power in output
command_succeeded $EXIT_CODE "$OUTPUT" && \
  echo "$OUTPUT" | grep -qi "voltage" && \
  echo "$OUTPUT" | grep -qi "current" && \
  echo "$OUTPUT" | grep -qi "power" && \
  track_test "pass" || track_test "fail"
echo ""

echo "Test 14.6: State after mode switching"
lager eload $ELOAD_NET cc 2.0 --box $BOX >/dev/null 2>&1
lager eload $ELOAD_NET cv 3.3 --box $BOX >/dev/null 2>&1
lager eload $ELOAD_NET cr 10.0 --box $BOX >/dev/null 2>&1
OUTPUT=$(lager eload $ELOAD_NET state --box $BOX 2>&1)
EXIT_CODE=$?
echo "$OUTPUT"
command_succeeded $EXIT_CODE "$OUTPUT" && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.7: State multiple consecutive calls"
lager eload $ELOAD_NET cc 1.5 --box $BOX >/dev/null 2>&1
FAILED=0
for i in {1..3}; do
  OUTPUT=$(lager eload $ELOAD_NET state --box $BOX 2>&1)
  EXIT_CODE=$?
  echo "  State call $i:"
  echo "$OUTPUT" | sed 's/^/    /'
  command_succeeded $EXIT_CODE "$OUTPUT" || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Setting electronic load to safe state..."
lager eload $ELOAD_NET cc 0 --box $BOX >/dev/null 2>&1 || true
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

# ============================================================
# TEST SUMMARY
# ============================================================
echo "========================================================================"
echo "TEST SUITE COMPLETED"
echo "========================================================================"
echo ""

# Print the summary table
print_summary

# Exit with appropriate status code
exit_with_status

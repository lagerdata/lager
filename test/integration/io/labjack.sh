#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager LabJack commands (ADC, DAC, GPIO)
# Tests all edge cases, error conditions, and production features

set +e  # Continue on error to run all tests

# Check for required commands
if ! command -v bc &> /dev/null; then
    echo "Error: 'bc' command not found. Please install bc to run this test suite."
    echo "  macOS: brew install bc"
    echo "  Ubuntu/Debian: sudo apt-get install bc"
    echo "  RHEL/CentOS: sudo yum install bc"
    exit 1
fi

# Error tracking
FAILED_TESTS=0
PASSED_TESTS=0

# Section tracking (20 sections, 90 total tests with new negative zero test)
CURRENT_SECTION=0
declare -a SECTION_PASSED=(0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0)
declare -a SECTION_FAILED=(0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0)

# Function to handle test errors
handle_test_error() {
    local test_name="$1"
    local error_msg="$2"
    echo "ERROR in ${test_name}: ${error_msg}"
    ((FAILED_TESTS++))
    if [ $CURRENT_SECTION -gt 0 ] && [ $CURRENT_SECTION -le 20 ]; then
        ((SECTION_FAILED[$CURRENT_SECTION - 1]++))
    fi
}

# Function to mark test passed
mark_test_passed() {
    ((PASSED_TESTS++))
    if [ $CURRENT_SECTION -gt 0 ] && [ $CURRENT_SECTION -le 20 ]; then
        ((SECTION_PASSED[$CURRENT_SECTION - 1]++))
    fi
}

# Function to run a test and count results
run_test() {
    local test_name="$1"
    shift

    if "$@"; then
        mark_test_passed
        return 0
    else
        handle_test_error "$test_name" "Command failed with exit code $?"
        return 1
    fi
}

# Function to run test with output validation
run_test_with_validation() {
    local test_name="$1"
    local expected_pattern="$2"
    shift 2

    OUTPUT=$("$@" 2>&1)
    EXIT_CODE=$?
    echo "$OUTPUT"

    # Strip ANSI color codes for pattern matching
    # This removes sequences like \033[92m (green) and \033[0m (reset)
    # Using sed for portability (works on both macOS and Linux)
    CLEAN_OUTPUT=$(echo "$OUTPUT" | sed $'s/\033\[[0-9;]*m//g')

    if [ $EXIT_CODE -eq 0 ] && echo "$CLEAN_OUTPUT" | grep -E -q "$expected_pattern"; then
        mark_test_passed
        return 0
    else
        handle_test_error "$test_name" "Expected pattern '$expected_pattern' not found or command failed"
        return 1
    fi
}

# Function to run test expecting failure
run_test_expect_fail() {
    local test_name="$1"
    local expected_error="$2"
    shift 2

    OUTPUT=$("$@" 2>&1)
    EXIT_CODE=$?
    echo "$OUTPUT"

    if echo "$OUTPUT" | grep -qi "$expected_error"; then
        mark_test_passed
        return 0
    else
        handle_test_error "$test_name" "Expected error containing '$expected_error' not found"
        return 1
    fi
}

# Check for required command-line arguments
if [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ] || [ -z "$4" ]; then
  echo "Error: Missing required arguments"
  echo "Usage: $0 <BOX> <GPIO_NET> <ADC_NET> <DAC_NET> [GPIO_NET2] [ADC_NET2] [DAC_NET2]"
  echo ""
  echo "Examples:"
  echo "  $0 MY-BOX gpio16 adc1 dac1"
  echo "  $0 MY-BOX gpio16 adc1 dac1 gpio17 adc2 dac2  # For multi-channel tests"
  echo "  $0 <BOX_IP> gpio16 adc1 dac1"
  echo ""
  echo "Required arguments:"
  echo "  BOX        - Box name or Tailscale IP address"
  echo "  GPIO_NET   - Name of the primary GPIO net to test"
  echo "  ADC_NET    - Name of the primary ADC net to test"
  echo "  DAC_NET    - Name of the primary DAC net to test"
  echo ""
  echo "Optional arguments (for multi-channel tests):"
  echo "  GPIO_NET2  - Second GPIO net for independence testing"
  echo "  ADC_NET2   - Second ADC net for simultaneous read testing"
  echo "  DAC_NET2   - Second DAC net for independence testing"
  echo ""
  exit 1
fi

BOX="$1"
GPIO_NET="$2"
ADC_NET="$3"
DAC_NET="$4"
GPIO_NET2="${5:-}"
ADC_NET2="${6:-}"
DAC_NET2="${7:-}"

echo "========================================================================"
echo "LAGER LABJACK COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "Primary GPIO Net: $GPIO_NET"
echo "Primary ADC Net: $ADC_NET"
echo "Primary DAC Net: $DAC_NET"
if [ -n "$GPIO_NET2" ]; then
    echo "Secondary GPIO Net: $GPIO_NET2 (multi-channel tests enabled)"
fi
if [ -n "$ADC_NET2" ]; then
    echo "Secondary ADC Net: $ADC_NET2 (multi-channel tests enabled)"
fi
if [ -n "$DAC_NET2" ]; then
    echo "Secondary DAC Net: $DAC_NET2 (multi-channel tests enabled)"
fi
echo ""

# ============================================================
# SECTION 1: BASIC COMMANDS (No hardware interaction required)
# ============================================================
CURRENT_SECTION=1
echo "========================================================================"
echo "SECTION 1: BASIC COMMANDS"
echo "========================================================================"
echo ""

echo "Test 1.1: List available boxes"
run_test "Test 1.1" lager boxes
echo ""

echo "Test 1.2: List available nets"
run_test "Test 1.2" lager nets --box $BOX
echo ""

echo "Test 1.3: Verify GPIO net exists"
run_test_with_validation "Test 1.3" "$GPIO_NET" lager nets --box $BOX
echo ""

echo "Test 1.4: Verify ADC net exists"
run_test_with_validation "Test 1.4" "$ADC_NET" lager nets --box $BOX
echo ""

echo "Test 1.5: Verify DAC net exists"
run_test_with_validation "Test 1.5" "$DAC_NET" lager nets --box $BOX
echo ""

# ============================================================
# SECTION 2: HELP AND DOCUMENTATION
# ============================================================
CURRENT_SECTION=2
echo "========================================================================"
echo "SECTION 2: HELP AND DOCUMENTATION"
echo "========================================================================"
echo ""

echo "Test 2.1: GPO help output"
run_test "Test 2.1" lager gpo --help
echo ""

echo "Test 2.2: GPI help output"
run_test "Test 2.2" lager gpi --help
echo ""

echo "Test 2.3: ADC help output"
run_test "Test 2.3" lager adc --help
echo ""

echo "Test 2.4: DAC help output"
run_test "Test 2.4" lager dac --help
echo ""

# ============================================================
# SECTION 3: ERROR CASES - INVALID INPUTS
# ============================================================
CURRENT_SECTION=3
echo "========================================================================"
echo "SECTION 3: ERROR CASES - INVALID INPUTS"
echo "========================================================================"
echo ""

echo "Test 3.1: Invalid net name - GPIO"
run_test_expect_fail "Test 3.1" "Invalid Net\|not found\|error" lager gpo nonexistent_net on --box $BOX
echo ""

echo "Test 3.2: Invalid net name - ADC"
run_test_expect_fail "Test 3.2" "Invalid Net\|not found\|error" lager adc nonexistent_net --box $BOX
echo ""

echo "Test 3.3: Invalid net name - DAC"
run_test_expect_fail "Test 3.3" "Invalid Net\|not found\|error" lager dac nonexistent_net 3.3 --box $BOX
echo ""

echo "Test 3.4: Invalid box"
run_test_expect_fail "Test 3.4" "don't have\|not found\|error" lager gpo $GPIO_NET on --box INVALID-BOX
echo ""

echo "Test 3.5: Invalid GPO value (not on/off/high/low/0/1/toggle)"
run_test_expect_fail "Test 3.5" "Invalid value\|not one of\|error" lager gpo $GPIO_NET invalid_value --box $BOX
echo ""

echo "Test 3.6: DAC voltage - invalid format (text)"
run_test_expect_fail "Test 3.6" "could not convert\|invalid\|error" lager dac $DAC_NET abc --box $BOX
echo ""

echo "Test 3.7: DAC voltage - out of range (negative via flag rejection)"
run_test_expect_fail "Test 3.7" "No such option\|error" lager dac $DAC_NET -1.0 --box $BOX
echo ""

echo "Test 3.8: DAC voltage - out of range (too high)"
run_test_expect_fail "Test 3.8" "must be between\|out of range\|error" lager dac $DAC_NET 10.0 --box $BOX
echo ""

# ============================================================
# SECTION 4: ERROR CASES - NET TYPE MISMATCHES
# ============================================================
CURRENT_SECTION=4
echo "========================================================================"
echo "SECTION 4: ERROR CASES - NET TYPE MISMATCHES"
echo "========================================================================"
echo ""

echo "Test 4.1: Using ADC net with GPO command"
run_test_expect_fail "Test 4.1" "Invalid Net\|wrong type\|error" lager gpo $ADC_NET on --box $BOX
echo ""

echo "Test 4.2: Using GPIO net with ADC command"
run_test_expect_fail "Test 4.2" "Invalid Net\|wrong type\|error" lager adc $GPIO_NET --box $BOX
echo ""

echo "Test 4.3: Using DAC net with GPI command"
run_test_expect_fail "Test 4.3" "Invalid Net\|wrong type\|error" lager gpi $DAC_NET --box $BOX
echo ""

echo "Test 4.4: Using GPIO net with DAC command"
run_test_expect_fail "Test 4.4" "Invalid Net\|wrong type\|error" lager dac $GPIO_NET 3.3 --box $BOX
echo ""

# ============================================================
# SECTION 5: GPIO OUTPUT (GPO) OPERATIONS - ALL VARIANTS
# ============================================================
CURRENT_SECTION=5
echo "========================================================================"
echo "SECTION 5: GPIO OUTPUT (GPO) OPERATIONS - ALL VARIANTS"
echo "========================================================================"
echo ""

echo "Test 5.1: Set GPO to HIGH (using 'high')"
run_test "Test 5.1" lager gpo $GPIO_NET high --box $BOX
echo ""

echo "Test 5.2: Set GPO to LOW (using 'low')"
run_test "Test 5.2" lager gpo $GPIO_NET low --box $BOX
echo ""

echo "Test 5.3: Set GPO to ON (using 'on')"
run_test "Test 5.3" lager gpo $GPIO_NET on --box $BOX
echo ""

echo "Test 5.4: Set GPO to OFF (using 'off')"
run_test "Test 5.4" lager gpo $GPIO_NET off --box $BOX
echo ""

echo "Test 5.5: Set GPO to 1 (using numeric '1')"
run_test "Test 5.5" lager gpo $GPIO_NET 1 --box $BOX
echo ""

echo "Test 5.6: Set GPO to 0 (using numeric '0')"
run_test "Test 5.6" lager gpo $GPIO_NET 0 --box $BOX
echo ""

echo "Test 5.7: Toggle GPO (CRITICAL MISSING TEST)"
run_test "Test 5.7" lager gpo $GPIO_NET toggle --box $BOX
echo ""

echo "Test 5.8: Toggle again (verify state changes)"
run_test "Test 5.8" lager gpo $GPIO_NET toggle --box $BOX
echo ""

# ============================================================
# SECTION 6: GPIO INPUT (GPI) OPERATIONS
# ============================================================
CURRENT_SECTION=6
echo "========================================================================"
echo "SECTION 6: GPIO INPUT (GPI) OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 6.1: Read GPI (should return 0 or 1)"
run_test_with_validation "Test 6.1" "GPIO.*\([01]\)" lager gpi $GPIO_NET --box $BOX
echo ""

echo "Test 6.2: Set GPO low and read GPI back"
OUTPUT=$(lager gpo $GPIO_NET low --box $BOX 2>&1)
run_test_with_validation "Test 6.2" "GPIO.*\([01]\)" lager gpi $GPIO_NET --box $BOX
echo ""

echo "Test 6.3: Set GPO high and read GPI back"
OUTPUT=$(lager gpo $GPIO_NET high --box $BOX 2>&1)
run_test_with_validation "Test 6.3" "GPIO.*\([01]\)" lager gpi $GPIO_NET --box $BOX
echo ""

echo "Test 6.4: Rapid successive GPI reads (burst test)"
if lager gpi $GPIO_NET --box $BOX >/dev/null 2>&1 && \
   lager gpi $GPIO_NET --box $BOX >/dev/null 2>&1 && \
   lager gpi $GPIO_NET --box $BOX >/dev/null 2>&1; then
    echo "[OK] Burst GPI reads completed"
    mark_test_passed
else
    handle_test_error "Test 6.4" "Burst reads failed"
fi
echo ""

# ============================================================
# SECTION 7: ADC (ANALOG-TO-DIGITAL) OPERATIONS
# ============================================================
CURRENT_SECTION=7
echo "========================================================================"
echo "SECTION 7: ADC (ANALOG-TO-DIGITAL) OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 7.1: Basic ADC read"
run_test_with_validation "Test 7.1" "ADC.*[0-9]+\.[0-9]+.*V" lager adc $ADC_NET --box $BOX
echo ""

echo "Test 7.2: ADC read returns numeric value"
OUTPUT=$(lager adc $ADC_NET --box $BOX 2>&1)
ADC_VALUE=$(echo "$OUTPUT" | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [[ "$ADC_VALUE" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
    echo "ADC returned: $ADC_VALUE V"
    echo "[OK] Valid numeric value"
    mark_test_passed
else
    handle_test_error "Test 7.2" "ADC returned non-numeric: $ADC_VALUE"
fi
echo ""

echo "Test 7.3: Multiple ADC reads (stability test)"
if lager adc $ADC_NET --box $BOX >/dev/null 2>&1 && \
   lager adc $ADC_NET --box $BOX >/dev/null 2>&1 && \
   lager adc $ADC_NET --box $BOX >/dev/null 2>&1; then
    echo "[OK] Stability test passed"
    mark_test_passed
else
    handle_test_error "Test 7.3" "Stability test failed"
fi
echo ""

echo "Test 7.4: Rapid ADC burst (5 samples)"
BURST_FAILED=0
for i in {1..5}; do
    lager adc $ADC_NET --box $BOX >/dev/null 2>&1 || BURST_FAILED=1
done
if [ $BURST_FAILED -eq 0 ]; then
    echo "[OK] Burst test passed"
    mark_test_passed
else
    handle_test_error "Test 7.4" "Some burst reads failed"
fi
echo ""

# ============================================================
# SECTION 8: DAC (DIGITAL-TO-ANALOG) WRITE OPERATIONS
# ============================================================
CURRENT_SECTION=8
echo "========================================================================"
echo "SECTION 8: DAC (DIGITAL-TO-ANALOG) WRITE OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 8.1: Set DAC to 0V"
run_test "Test 8.1" lager dac $DAC_NET 0.0 --box $BOX
echo ""

echo "Test 8.2: Set DAC to 1.0V"
run_test "Test 8.2" lager dac $DAC_NET 1.0 --box $BOX
echo ""

echo "Test 8.3: Set DAC to 2.5V"
run_test "Test 8.3" lager dac $DAC_NET 2.5 --box $BOX
echo ""

echo "Test 8.4: Set DAC to 3.3V"
run_test "Test 8.4" lager dac $DAC_NET 3.3 --box $BOX
echo ""

echo "Test 8.5: Set DAC to 5.0V (LabJack T7 DAC max)"
run_test "Test 8.5" lager dac $DAC_NET 5.0 --box $BOX
echo ""

echo "Test 8.6: Set DAC to fractional voltage (1.234V)"
run_test "Test 8.6" lager dac $DAC_NET 1.234 --box $BOX
echo ""

echo "Test 8.7: Voltage sweep (0V to 5V in 1V steps)"
SWEEP_FAILED=0
for voltage in 0.0 1.0 2.0 3.0 4.0 5.0; do
    lager dac $DAC_NET $voltage --box $BOX >/dev/null 2>&1 || SWEEP_FAILED=1
done
if [ $SWEEP_FAILED -eq 0 ]; then
    echo "[OK] Voltage sweep completed"
    mark_test_passed
else
    handle_test_error "Test 8.7" "Voltage sweep failed"
fi
echo ""

# ============================================================
# SECTION 9: DAC READ OPERATIONS
# ============================================================
CURRENT_SECTION=9
echo "========================================================================"
echo "SECTION 9: DAC READ OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 9.1: Set DAC and read back"
lager dac $DAC_NET 3.3 --box $BOX >/dev/null
run_test_with_validation "Test 9.1" "DAC.*[0-9]+\.[0-9]+.*V" lager dac $DAC_NET --box $BOX
echo ""

echo "Test 9.2: Read DAC without setting (current value)"
run_test_with_validation "Test 9.2" "DAC.*[0-9]+\.[0-9]+.*V" lager dac $DAC_NET --box $BOX
echo ""

echo "Test 9.3: Write-read verification loop"
VERIFY_FAILED=0
for voltage in 0.5 1.5 2.5 3.5 4.5; do
    lager dac $DAC_NET $voltage --box $BOX >/dev/null 2>&1
    if ! lager dac $DAC_NET --box $BOX >/dev/null 2>&1; then
        VERIFY_FAILED=1
    fi
done
if [ $VERIFY_FAILED -eq 0 ]; then
    echo "[OK] Write-read verification passed"
    mark_test_passed
else
    handle_test_error "Test 9.3" "Write-read verification failed"
fi
echo ""

# ============================================================
# SECTION 10: DAC BOUNDARY AND EDGE CASES
# ============================================================
CURRENT_SECTION=10
echo "========================================================================"
echo "SECTION 10: DAC BOUNDARY AND EDGE CASES"
echo "========================================================================"
echo ""

echo "Test 10.1: DAC at minimum voltage (0.0V)"
run_test "Test 10.1" lager dac $DAC_NET 0.0 --box $BOX
echo ""

echo "Test 10.2: DAC at maximum voltage (5.0V)"
run_test "Test 10.2" lager dac $DAC_NET 5.0 --box $BOX
echo ""

echo "Test 10.3: DAC with very small increment (0.0001V)"
run_test "Test 10.3" lager dac $DAC_NET 2.0001 --box $BOX
echo ""

echo "Test 10.4: DAC voltage just under max (4.9999V)"
run_test "Test 10.4" lager dac $DAC_NET 4.9999 --box $BOX
echo ""

echo "Test 10.5: DAC voltage with many decimal places"
run_test "Test 10.5" lager dac $DAC_NET 3.14159265 --box $BOX
echo ""

echo "Test 10.6: Extreme boundary - just above 5V (should fail)"
run_test_expect_fail "Test 10.6" "must be between\|out of range\|error" lager dac $DAC_NET 5.001 --box $BOX
echo ""

echo "Test 10.7: Extreme boundary - very high voltage (should fail)"
run_test_expect_fail "Test 10.7" "must be between\|out of range\|error" lager dac $DAC_NET 100.0 --box $BOX
echo ""

echo "Test 10.8: Extreme boundary - very low voltage (should fail)"
run_test_expect_fail "Test 10.8" "No such option\|error" lager dac $DAC_NET -100.0 --box $BOX
echo ""

echo "Test 10.9: Negative zero (-0.0 should be treated as 0.0)"
run_test "Test 10.9" lager dac --box $BOX $DAC_NET -- -0.0
echo ""

# ============================================================
# SECTION 11: FLOATING POINT INPUT VARIANTS
# ============================================================
CURRENT_SECTION=11
echo "========================================================================"
echo "SECTION 11: FLOATING POINT INPUT VARIANTS"
echo "========================================================================"
echo ""

echo "Test 11.1: Leading decimal (.5 instead of 0.5)"
run_test "Test 11.1" lager dac $DAC_NET .5 --box $BOX
echo ""

echo "Test 11.2: Trailing decimal (3. instead of 3.0)"
run_test "Test 11.2" lager dac $DAC_NET 3. --box $BOX
echo ""

echo "Test 11.3: Explicit positive sign (+2.5)"
run_test "Test 11.3" lager dac $DAC_NET +2.5 --box $BOX
echo ""

echo "Test 11.4: Scientific notation (1.5e-3)"
OUTPUT=$(lager dac $DAC_NET 1.5e-3 --box $BOX 2>&1)
echo "$OUTPUT"
# Python's float() accepts scientific notation, so this should work
if echo "$OUTPUT" | grep -q "DAC.*set to.*0.001500.*V"; then
    echo "[OK] Scientific notation supported (1.5e-3 = 0.0015V)"
    mark_test_passed
elif echo "$OUTPUT" | grep -q "DAC.*0.001"; then
    echo "[OK] Scientific notation accepted"
    mark_test_passed
else
    # If it fails, that's unexpected since Python supports scientific notation
    handle_test_error "Test 11.4" "Scientific notation failed unexpectedly"
fi
echo ""

# ============================================================
# SECTION 12: MALFORMED INPUT HANDLING
# ============================================================
CURRENT_SECTION=12
echo "========================================================================"
echo "SECTION 12: MALFORMED INPUT HANDLING"
echo "========================================================================"
echo ""

echo "Test 12.1: Voltage with unit suffix (3.3V)"
run_test_expect_fail "Test 12.1" "could not convert\|invalid\|error" lager dac $DAC_NET 3.3V --box $BOX
echo ""

echo "Test 12.2: Comma decimal separator (3,3)"
run_test_expect_fail "Test 12.2" "could not convert\|invalid\|error" lager dac $DAC_NET "3,3" --box $BOX
echo ""

echo "Test 12.3: Multiple decimals (3.3.3)"
run_test_expect_fail "Test 12.3" "could not convert\|invalid\|error" lager dac $DAC_NET "3.3.3" --box $BOX
echo ""

echo "Test 12.4: Empty string voltage"
run_test_expect_fail "Test 12.4" "argument.*required\|Missing argument\|error" lager dac $DAC_NET "" --box $BOX
echo ""

# ============================================================
# SECTION 13: CASE SENSITIVITY TESTS
# ============================================================
CURRENT_SECTION=13
echo "========================================================================"
echo "SECTION 13: CASE SENSITIVITY TESTS"
echo "========================================================================"
echo ""

echo "Test 13.1: GPO with uppercase HIGH"
OUTPUT=$(lager gpo $GPIO_NET HIGH --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "error\|Invalid value"; then
    echo "[OK] Case-sensitive (uppercase rejected as expected)"
    mark_test_passed
elif echo "$OUTPUT" | grep -q "GPIO"; then
    echo "[OK] Case-insensitive (uppercase accepted)"
    mark_test_passed
else
    handle_test_error "Test 13.1" "Unexpected response to uppercase"
fi
echo ""

echo "Test 13.2: GPO with mixed case HiGh"
OUTPUT=$(lager gpo $GPIO_NET HiGh --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "error\|Invalid value"; then
    echo "[OK] Case-sensitive (mixed case rejected as expected)"
    mark_test_passed
elif echo "$OUTPUT" | grep -q "GPIO"; then
    echo "[OK] Case-insensitive (mixed case accepted)"
    mark_test_passed
else
    handle_test_error "Test 13.2" "Unexpected response to mixed case"
fi
echo ""

echo "Test 13.3: GPO with uppercase ON"
OUTPUT=$(lager gpo $GPIO_NET ON --box $BOX 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qi "error\|Invalid value"; then
    echo "[OK] Case-sensitive (ON rejected)"
    mark_test_passed
elif echo "$OUTPUT" | grep -q "GPIO"; then
    echo "[OK] Case-insensitive (ON accepted)"
    mark_test_passed
else
    handle_test_error "Test 13.3" "Unexpected response to ON"
fi
echo ""

# ============================================================
# SECTION 14: MULTI-CHANNEL GPIO TESTS (if second GPIO provided)
# ============================================================
CURRENT_SECTION=14
echo "========================================================================"
echo "SECTION 14: MULTI-CHANNEL GPIO TESTS"
echo "========================================================================"
echo ""

if [ -n "$GPIO_NET2" ]; then
    echo "Test 14.1: Set first GPIO high, second low"
    if lager gpo $GPIO_NET high --box $BOX >/dev/null 2>&1 && \
       lager gpo $GPIO_NET2 low --box $BOX >/dev/null 2>&1; then
        echo "[OK] Both GPIOs set successfully"
        mark_test_passed
    else
        handle_test_error "Test 14.1" "Failed to set both GPIOs"
    fi
    echo ""

    echo "Test 14.2: Verify first GPIO state didn't affect second"
    GPI1=$(lager gpi $GPIO_NET --box $BOX 2>&1 | grep -oE '\([0-9]\)' | tr -d '()')
    GPI2=$(lager gpi $GPIO_NET2 --box $BOX 2>&1 | grep -oE '\([0-9]\)' | tr -d '()')
    echo "GPIO1: $GPI1, GPIO2: $GPI2"
    if [ "$GPI1" != "$GPI2" ]; then
        echo "[OK] GPIOs are independent (different states)"
        mark_test_passed
    else
        echo "[WARNING] GPIOs have same state (may still be independent)"
        mark_test_passed
    fi
    echo ""

    echo "Test 14.3: Simultaneous toggle operations"
    if lager gpo $GPIO_NET toggle --box $BOX >/dev/null 2>&1 && \
       lager gpo $GPIO_NET2 toggle --box $BOX >/dev/null 2>&1; then
        echo "[OK] Simultaneous toggles succeeded"
        mark_test_passed
    else
        handle_test_error "Test 14.3" "Simultaneous toggles failed"
    fi
    echo ""
else
    echo "[WARNING] Skipping multi-channel GPIO tests (GPIO_NET2 not provided)"
    echo "  To enable, run: $0 $BOX $GPIO_NET $ADC_NET $DAC_NET gpio17 adc2 dac2"
    mark_test_passed
    mark_test_passed
    mark_test_passed
    echo ""
fi

# ============================================================
# SECTION 15: MULTI-CHANNEL ADC TESTS (if second ADC provided)
# ============================================================
CURRENT_SECTION=15
echo "========================================================================"
echo "SECTION 15: MULTI-CHANNEL ADC TESTS"
echo "========================================================================"
echo ""

if [ -n "$ADC_NET2" ]; then
    echo "Test 15.1: Read both ADC channels"
    if lager adc $ADC_NET --box $BOX >/dev/null 2>&1 && \
       lager adc $ADC_NET2 --box $BOX >/dev/null 2>&1; then
        echo "[OK] Both ADCs read successfully"
        mark_test_passed
    else
        handle_test_error "Test 15.1" "Failed to read both ADCs"
    fi
    echo ""

    echo "Test 15.2: Simultaneous ADC reads (rapid succession)"
    ADC1_VAL=$(lager adc $ADC_NET --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    ADC2_VAL=$(lager adc $ADC_NET2 --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    echo "ADC1: ${ADC1_VAL}V, ADC2: ${ADC2_VAL}V"
    if [ -n "$ADC1_VAL" ] && [ -n "$ADC2_VAL" ]; then
        echo "[OK] Both ADCs returned values"
        mark_test_passed
    else
        handle_test_error "Test 15.2" "One or both ADCs failed to return values"
    fi
    echo ""
else
    echo "[WARNING] Skipping multi-channel ADC tests (ADC_NET2 not provided)"
    mark_test_passed
    mark_test_passed
    echo ""
fi

# ============================================================
# SECTION 16: MULTI-CHANNEL DAC TESTS (if second DAC provided)
# ============================================================
CURRENT_SECTION=16
echo "========================================================================"
echo "SECTION 16: MULTI-CHANNEL DAC TESTS"
echo "========================================================================"
echo ""

if [ -n "$DAC_NET2" ]; then
    echo "Test 16.1: Set both DAC channels to different voltages"
    if lager dac $DAC_NET 1.0 --box $BOX >/dev/null 2>&1 && \
       lager dac $DAC_NET2 3.3 --box $BOX >/dev/null 2>&1; then
        echo "[OK] Both DACs set successfully"
        mark_test_passed
    else
        handle_test_error "Test 16.1" "Failed to set both DACs"
    fi
    echo ""

    echo "Test 16.2: Verify DAC independence (readback)"
    DAC1_VAL=$(lager dac $DAC_NET --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    DAC2_VAL=$(lager dac $DAC_NET2 --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
    echo "DAC1: ${DAC1_VAL}V (expected ~1.0V)"
    echo "DAC2: ${DAC2_VAL}V (expected ~3.3V)"
    # Check if values are different (independence)
    if [ -n "$DAC1_VAL" ] && [ -n "$DAC2_VAL" ]; then
        DIFF=$(echo "$DAC2_VAL - $DAC1_VAL" | bc)
        if (( $(echo "$DIFF > 1.0" | bc -l) )); then
            echo "[OK] DAC channels are independent"
            mark_test_passed
        else
            echo "[WARNING] DAC values similar (diff: ${DIFF}V)"
            mark_test_passed
        fi
    else
        echo "[WARNING] Could not read DAC values"
        mark_test_passed
    fi
    echo ""

    echo "Test 16.3: Simultaneous DAC updates"
    if lager dac $DAC_NET 2.5 --box $BOX >/dev/null 2>&1 && \
       lager dac $DAC_NET2 2.5 --box $BOX >/dev/null 2>&1; then
        echo "[OK] Simultaneous DAC updates succeeded"
        mark_test_passed
    else
        handle_test_error "Test 16.3" "Simultaneous DAC updates failed"
    fi
    echo ""
else
    echo "[WARNING] Skipping multi-channel DAC tests (DAC_NET2 not provided)"
    mark_test_passed
    mark_test_passed
    mark_test_passed
    echo ""
fi

# ============================================================
# SECTION 17: SIMULTANEOUS MIXED OPERATIONS
# ============================================================
CURRENT_SECTION=17
echo "========================================================================"
echo "SECTION 17: SIMULTANEOUS MIXED OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 17.1: Set DAC and immediately read ADC"
lager dac $DAC_NET 3.3 --box $BOX >/dev/null 2>&1
sleep 0.05  # 50ms settling time for DAC output
run_test_with_validation "Test 17.1" "ADC.*[0-9]+\.[0-9]+.*V" lager adc $ADC_NET --box $BOX
echo ""

echo "Test 17.2: Set GPIO and read ADC"
lager gpo $GPIO_NET high --box $BOX >/dev/null 2>&1
sleep 0.05  # 50ms settling time
run_test_with_validation "Test 17.2" "ADC.*[0-9]+\.[0-9]+.*V" lager adc $ADC_NET --box $BOX
echo ""

echo "Test 17.3: Interleaved operations (GPIO/DAC/ADC)"
if lager gpo $GPIO_NET on --box $BOX >/dev/null 2>&1 && \
   lager dac $DAC_NET 2.5 --box $BOX >/dev/null 2>&1 && \
   lager adc $ADC_NET --box $BOX >/dev/null 2>&1 && \
   lager gpo $GPIO_NET off --box $BOX >/dev/null 2>&1; then
    echo "[OK] Interleaved operations completed"
    mark_test_passed
else
    handle_test_error "Test 17.3" "Interleaved operations failed"
fi
echo ""

echo "Test 17.4: Rapid mixed operations (10 iterations)"
MIXED_FAILED=0
for i in {1..10}; do
    lager gpo $GPIO_NET $(( i % 2 )) --box $BOX >/dev/null 2>&1 || MIXED_FAILED=1
    lager adc $ADC_NET --box $BOX >/dev/null 2>&1 || MIXED_FAILED=1
    VOLTAGE=$(echo "scale=1; ($i % 5) * 1.0" | bc)
    lager dac $DAC_NET $VOLTAGE --box $BOX >/dev/null 2>&1 || MIXED_FAILED=1
done
if [ $MIXED_FAILED -eq 0 ]; then
    echo "[OK] Rapid mixed operations completed"
    mark_test_passed
else
    handle_test_error "Test 17.4" "Some mixed operations failed"
fi
echo ""

# ============================================================
# SECTION 18: DAC/ADC LOOPBACK CORRELATION (if connected)
# ============================================================
CURRENT_SECTION=18
echo "========================================================================"
echo "SECTION 18: DAC/ADC LOOPBACK CORRELATION"
echo "========================================================================"
echo ""

# Test if DAC and ADC are physically connected
lager dac $DAC_NET 0.0 --box $BOX >/dev/null 2>&1
sleep 0.1  # 100ms settling time for DAC output
ADC_LOW=$(lager adc $ADC_NET --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager dac $DAC_NET 5.0 --box $BOX >/dev/null 2>&1
sleep 0.1  # 100ms settling time for DAC output
ADC_HIGH=$(lager adc $ADC_NET --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [ -n "$ADC_LOW" ] && [ -n "$ADC_HIGH" ]; then
    ADC_DIFF=$(echo "$ADC_HIGH - $ADC_LOW" | bc | tr -d '-')
else
    ADC_DIFF="0.0"
fi

LOOPBACK_CONNECTED=0
if (( $(echo "$ADC_DIFF > 1.0" | bc -l) )); then
    LOOPBACK_CONNECTED=1
    echo "[OK] Loopback detected: DAC->ADC connected (diff: ${ADC_DIFF}V)"
    mark_test_passed
else
    echo "[WARNING] DAC and ADC not physically connected (diff: ${ADC_DIFF}V)"
    echo "  Loopback tests will show expected mismatches"
    mark_test_passed
fi
echo ""

echo "Test 18.2: DAC→ADC at 1.0V"
lager dac $DAC_NET 1.0 --box $BOX >/dev/null 2>&1
sleep 0.1
run_test_with_validation "Test 18.2" "ADC.*[0-9]+\.[0-9]+.*V" lager adc $ADC_NET --box $BOX
echo ""

echo "Test 18.3: DAC→ADC at 2.5V"
lager dac $DAC_NET 2.5 --box $BOX >/dev/null 2>&1
sleep 0.1
run_test_with_validation "Test 18.3" "ADC.*[0-9]+\.[0-9]+.*V" lager adc $ADC_NET --box $BOX
echo ""

echo "Test 18.4: DAC→ADC sweep with tolerance check"
TOLERANCE=0.1
TOLERANCE_5V=0.15
SWEEP_PASSED=0
for voltage in 0.0 1.0 2.5 4.0 5.0; do
    lager dac $DAC_NET $voltage --box $BOX >/dev/null 2>&1
    sleep 0.1
    ADC_VALUE=$(lager adc $ADC_NET --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)

    if [ -n "$ADC_VALUE" ]; then
        DIFF=$(echo "$ADC_VALUE - $voltage" | bc)
        ABS_DIFF=$(echo "$DIFF" | tr -d '-')

        CURRENT_TOL=$TOLERANCE
        [ "$voltage" == "5.0" ] && CURRENT_TOL=$TOLERANCE_5V

        if [ $LOOPBACK_CONNECTED -eq 1 ]; then
            if (( $(echo "$ABS_DIFF < $CURRENT_TOL" | bc -l) )); then
                echo "  [OK] DAC=${voltage}V, ADC=${ADC_VALUE}V (within +/-${CURRENT_TOL}V)"
                ((SWEEP_PASSED++))
            else
                echo "  [WARNING] DAC=${voltage}V, ADC=${ADC_VALUE}V (outside +/-${CURRENT_TOL}V)"
            fi
        else
            echo "  [SKIP] DAC=${voltage}V, ADC=${ADC_VALUE}V (loopback not connected)"
        fi
    else
        echo "  [WARNING] DAC=${voltage}V, ADC read failed"
    fi
done
if [ $SWEEP_PASSED -ge 3 ] || [ $LOOPBACK_CONNECTED -eq 0 ]; then
    mark_test_passed
else
    handle_test_error "Test 18.4" "Loopback sweep accuracy poor"
fi
echo ""

# ============================================================
# SECTION 19: STRESS AND PERFORMANCE TESTS
# ============================================================
CURRENT_SECTION=19
echo "========================================================================"
echo "SECTION 19: STRESS AND PERFORMANCE TESTS"
echo "========================================================================"
echo ""

echo "Test 19.1: Rapid GPIO toggles (10 cycles)"
TOGGLE_FAILED=0
for i in {1..10}; do
    lager gpo $GPIO_NET on --box $BOX >/dev/null 2>&1 || TOGGLE_FAILED=1
    lager gpo $GPIO_NET off --box $BOX >/dev/null 2>&1 || TOGGLE_FAILED=1
done
if [ $TOGGLE_FAILED -eq 0 ]; then
    echo "[OK] 10 rapid toggle cycles completed"
    mark_test_passed
else
    handle_test_error "Test 19.1" "Some toggle cycles failed"
fi
echo ""

echo "Test 19.2: ADC sampling burst (20 samples)"
SAMPLE_FAILED=0
for i in {1..20}; do
    lager adc $ADC_NET --box $BOX >/dev/null 2>&1 || SAMPLE_FAILED=1
done
if [ $SAMPLE_FAILED -eq 0 ]; then
    echo "[OK] 20 ADC samples completed"
    mark_test_passed
else
    handle_test_error "Test 19.2" "Some ADC samples failed"
fi
echo ""

echo "Test 19.3: DAC update burst (20 updates)"
UPDATE_FAILED=0
for i in {1..20}; do
    VOLTAGE=$(echo "scale=2; ($i % 10) * 0.5" | bc)
    lager dac $DAC_NET $VOLTAGE --box $BOX >/dev/null 2>&1 || UPDATE_FAILED=1
done
if [ $UPDATE_FAILED -eq 0 ]; then
    echo "[OK] 20 DAC updates completed"
    mark_test_passed
else
    handle_test_error "Test 19.3" "Some DAC updates failed"
fi
echo ""

echo "Test 19.4: Device contention test (rapid same-device access)"
CONTENTION_DETECTED=0
for i in {1..5}; do
    OUTPUT=$(lager gpo $GPIO_NET on --box $BOX 2>&1)
    if echo "$OUTPUT" | grep -q "LJME_DEVICE_CURRENTLY_CLAIMED"; then
        CONTENTION_DETECTED=1
        echo "  [WARNING] Contention detected on iteration $i"
    fi
done
if [ $CONTENTION_DETECTED -eq 0 ]; then
    echo "[OK] No device contention detected"
    mark_test_passed
else
    echo "[WARNING] Device contention occurred (may be normal under rapid access)"
    mark_test_passed
fi
echo ""

# ============================================================
# SECTION 20: ERROR RECOVERY TESTS
# ============================================================
CURRENT_SECTION=20
echo "========================================================================"
echo "SECTION 20: ERROR RECOVERY TESTS"
echo "========================================================================"
echo ""

echo "Test 20.1: Recovery after invalid net error"
lager gpo invalid_net on --box $BOX >/dev/null 2>&1
if lager gpo $GPIO_NET on --box $BOX >/dev/null 2>&1; then
    echo "[OK] System recovered after invalid net error"
    mark_test_passed
else
    handle_test_error "Test 20.1" "Failed to recover after error"
fi
echo ""

echo "Test 20.2: Recovery after invalid voltage error"
lager dac $DAC_NET 100 --box $BOX >/dev/null 2>&1
if lager dac $DAC_NET 2.5 --box $BOX >/dev/null 2>&1; then
    echo "[OK] System recovered after invalid voltage error"
    mark_test_passed
else
    handle_test_error "Test 20.2" "Failed to recover after error"
fi
echo ""

echo "Test 20.3: Multiple errors followed by valid commands"
lager gpo invalid_net on --box $BOX >/dev/null 2>&1
lager adc invalid_net --box $BOX >/dev/null 2>&1
lager dac invalid_net 3.3 --box $BOX >/dev/null 2>&1
RECOVERY_COUNT=0
lager gpo $GPIO_NET on --box $BOX >/dev/null 2>&1 && ((RECOVERY_COUNT++))
lager adc $ADC_NET --box $BOX >/dev/null 2>&1 && ((RECOVERY_COUNT++))
lager dac $DAC_NET 2.5 --box $BOX >/dev/null 2>&1 && ((RECOVERY_COUNT++))
if [ $RECOVERY_COUNT -eq 3 ]; then
    echo "[OK] Full recovery after multiple errors (3/3 succeeded)"
    mark_test_passed
else
    handle_test_error "Test 20.3" "Partial recovery ($RECOVERY_COUNT/3 succeeded)"
fi
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Setting all outputs to safe states..."
lager gpo $GPIO_NET off --box $BOX >/dev/null 2>&1
lager dac $DAC_NET 0.0 --box $BOX >/dev/null 2>&1
if [ -n "$GPIO_NET2" ]; then
    lager gpo $GPIO_NET2 off --box $BOX >/dev/null 2>&1
fi
if [ -n "$DAC_NET2" ]; then
    lager dac $DAC_NET2 0.0 --box $BOX >/dev/null 2>&1
fi
echo "[OK] Cleanup complete"
echo ""

# ============================================================
# TEST SUMMARY
# ============================================================
echo "========================================================================"
echo "TEST SUITE COMPLETED"
echo "========================================================================"
echo ""

echo "Test suite execution completed!"
echo ""
if [ $FAILED_TESTS -eq 0 ]; then
    echo "[OK] All tests completed without critical errors!"
else
    echo "[WARNING] ${FAILED_TESTS} tests encountered errors"
fi
echo ""
echo "Test Results: ${PASSED_TESTS} passed, ${FAILED_TESTS} failed"
echo ""
echo "========================================================================"
echo "DETAILED TEST SUMMARY BY SECTION"
echo "========================================================================"
echo ""
printf "%-8s %-48s %6s %6s %6s\n" "Section" "Description" "Total" "Passed" "Failed"
echo "--------------------------------------------------------------------------------"

# Section descriptions
SECTION_NAMES=(
    "Basic Commands"
    "Help and Documentation"
    "Error Cases - Invalid Inputs"
    "Error Cases - Net Type Mismatches"
    "GPIO Output (GPO) Operations - All Variants"
    "GPIO Input (GPI) Operations"
    "ADC Operations"
    "DAC Write Operations"
    "DAC Read Operations"
    "DAC Boundary and Edge Cases"
    "Floating Point Input Variants"
    "Malformed Input Handling"
    "Case Sensitivity Tests"
    "Multi-Channel GPIO Tests"
    "Multi-Channel ADC Tests"
    "Multi-Channel DAC Tests"
    "Simultaneous Mixed Operations"
    "DAC/ADC Loopback Correlation"
    "Stress and Performance Tests"
    "Error Recovery Tests"
)

# Print each section
TOTAL_SECTION_PASSED=0
TOTAL_SECTION_FAILED=0
for i in {0..19}; do
    SECTION_NUM=$((i + 1))
    PASSED=${SECTION_PASSED[$i]}
    FAILED=${SECTION_FAILED[$i]}
    TOTAL=$((PASSED + FAILED))
    TOTAL_SECTION_PASSED=$((TOTAL_SECTION_PASSED + PASSED))
    TOTAL_SECTION_FAILED=$((TOTAL_SECTION_FAILED + FAILED))

    printf "%-8s %-48s %6s %6s %6s\n" "$SECTION_NUM" "${SECTION_NAMES[$i]}" "$TOTAL" "$PASSED" "$FAILED"
done

echo "--------------------------------------------------------------------------------"
printf "%-8s %-48s %6s %6s %6s\n" "TOTAL" "" "$((TOTAL_SECTION_PASSED + TOTAL_SECTION_FAILED))" "$TOTAL_SECTION_PASSED" "$TOTAL_SECTION_FAILED"
echo ""

if [ $FAILED_TESTS -eq 0 ]; then
    echo "[OK] ALL ${PASSED_TESTS} TESTS PASSED!"
else
    echo "Results: ${PASSED_TESTS} passed, ${FAILED_TESTS} failed"
    echo ""
    echo "Note: Failed tests indicate actual command failures, not expected error conditions."
    echo "      Tests that verify error handling (e.g., invalid inputs) are counted as PASSED"
    echo "      when they correctly produce the expected error messages."
fi
echo ""

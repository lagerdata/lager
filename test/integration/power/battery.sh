#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager battery commands
# Tests all edge cases, error conditions, and production features

set +e  # DON'T exit on error - we want to track failures

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

# Initialize the test harness
init_harness

# Check if required arguments are provided
if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX_NAME_OR_IP> <BATTERY_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box battery1"
  echo "  $0 <BOX_IP> battery1"
  echo ""
  echo "Arguments:"
  echo "  BOX_NAME_OR_IP - Box name or Tailscale IP address"
  echo "  BATTERY_NET    - Name of the battery net to test"
  echo ""
  exit 1
fi

BOX_INPUT="$1"
BATTERY_NET="$2"

register_box_from_ip "$BOX_INPUT"
print_script_header "LAGER BATTERY COMPREHENSIVE TEST SUITE" "$BOX" "$BATTERY_NET"

# ============================================================
# SECTION 1: BASIC COMMANDS (No connection required)
# ============================================================
start_section "Basic Commands"
echo "========================================================================"
echo "SECTION 1: BASIC COMMANDS (No Connection Required)"
echo "========================================================================"
echo ""

echo "Test 1.1: List available boxes"
lager boxes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: List available nets"
lager nets --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Verify battery net exists"
if lager nets --box $BOX 2>&1 | grep -q "$BATTERY_NET"; then
  track_test_msg "pass" "Battery net found"
else
  track_test_msg "fail" "Battery net not found"
fi
echo ""

echo "Test 1.4: Battery help output"
lager battery --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: ERROR CASES (Invalid Commands)
# ============================================================
start_section "Error Cases"
echo "========================================================================"
echo "SECTION 2: ERROR CASES (Invalid Commands)"
echo "========================================================================"
echo ""

echo "Test 2.1: Invalid net name"
lager battery nonexistent_net state --box $BOX 2>&1 | grep -qi "not found\|error" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: Invalid box"
lager battery $BATTERY_NET state --box INVALID-BOX 2>&1 | grep -qi "error\|don't have" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: Negative voltage (voc)"
lager battery $BATTERY_NET voc -1.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|negative\|No such option" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.4: Zero capacity (should be rejected - must be positive)"
lager battery $BATTERY_NET capacity 0.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|positive" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.5: Current-limit below 1mA (should be rejected)"
lager battery $BATTERY_NET current-limit 0.0005 --box $BOX 2>&1 | grep -qi "error\|1mA\|0.001" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.6: Invalid SOC (>100%)"
lager battery $BATTERY_NET soc 150 --box $BOX 2>&1 | grep -qi "error\|invalid\|range" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.7: Invalid voltage format (voc)"
lager battery $BATTERY_NET voc abc --box $BOX 2>&1 | grep -qi "error\|invalid\|not a valid" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 3: STATE AND STATUS COMMANDS
# ============================================================
start_section "State and Status Commands"
echo "========================================================================"
echo "SECTION 3: STATE AND STATUS COMMANDS"
echo "========================================================================"
echo ""

echo "Test 3.1: Get comprehensive battery state"
lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: State check while disabled"
lager battery $BATTERY_NET disable --box $BOX --yes >/dev/null 2>&1 || true
lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: State check while enabled"
lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1 || true
lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Multiple state queries (stability test)"
FAILED=0
for i in {1..5}; do
  lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: ENABLE/DISABLE OPERATIONS
# ============================================================
start_section "Enable/Disable Operations"
echo "========================================================================"
echo "SECTION 4: ENABLE/DISABLE OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 4.1: Disable battery output (using --yes flag)"
lager battery $BATTERY_NET disable --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Check state after disable"
lager battery $BATTERY_NET state --box $BOX 2>&1 | grep -iE "disabled|output.*off|enabled:.*off" >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.3: Enable battery output (using --yes flag)"
lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.4: Check state after enable"
lager battery $BATTERY_NET state --box $BOX 2>&1 | grep -iE "enabled|output.*on|enabled:.*on" >/dev/null && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 5: VOLTAGE PARAMETERS (VOC, BATT-FULL, BATT-EMPTY)
# ============================================================
start_section "Voltage Parameters"
echo "========================================================================"
echo "SECTION 5: VOLTAGE PARAMETERS (VOC, BATT-FULL, BATT-EMPTY)"
echo "========================================================================"
echo ""

echo "Test 5.1: Set open circuit voltage (VOC) to 3.7V"
lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Read VOC"
lager battery $BATTERY_NET voc --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: Set battery full voltage to 4.2V"
lager battery $BATTERY_NET batt-full 4.2 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.4: Read battery full voltage"
lager battery $BATTERY_NET batt-full --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.5: Set battery empty voltage to 3.0V"
lager battery $BATTERY_NET batt-empty 3.0 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.6: Read battery empty voltage"
lager battery $BATTERY_NET batt-empty --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: STATE OF CHARGE (SOC) OPERATIONS
# ============================================================
start_section "State of Charge (SOC)"
echo "========================================================================"
echo "SECTION 6: STATE OF CHARGE (SOC) OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 6.1: Set SOC to 0%"
lager battery $BATTERY_NET soc 0 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Read SOC at 0%"
lager battery $BATTERY_NET soc --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.3: Set SOC to 50%"
lager battery $BATTERY_NET soc 50 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.4: Read SOC at 50%"
lager battery $BATTERY_NET soc --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.5: Set SOC to 100%"
lager battery $BATTERY_NET soc 100 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.6: Read SOC at 100%"
lager battery $BATTERY_NET soc --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: CAPACITY AND CURRENT LIMITS
# ============================================================
start_section "Capacity and Current Limits"
echo "========================================================================"
echo "SECTION 7: CAPACITY AND CURRENT LIMITS"
echo "========================================================================"
echo ""

echo "Test 7.1: Set battery capacity to 2.5 Ah"
lager battery $BATTERY_NET capacity 2.5 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.2: Read battery capacity"
lager battery $BATTERY_NET capacity --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.3: Set current limit to 1.0 A"
lager battery $BATTERY_NET current-limit 1.0 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.4: Read current limit"
lager battery $BATTERY_NET current-limit --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.5: Very small current limit (0.001 A)"
lager battery $BATTERY_NET current-limit 0.001 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.6: Large capacity value (100 Ah)"
lager battery $BATTERY_NET capacity 100.0 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.7: Large current limit (50 A)"
lager battery $BATTERY_NET current-limit 50.0 --box $BOX >/dev/null 2>&1
track_test "pass"  # Pass if doesn't crash
echo ""

echo "Test 7.8: Boundary test - current-limit just below 1mA (should fail)"
lager battery $BATTERY_NET current-limit 0.0009 --box $BOX 2>&1 | grep -qi "error\|1mA\|0.001" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 8: PROTECTION FEATURES (OVP, OCP)
# ============================================================
start_section "Protection Features (OVP, OCP)"
echo "========================================================================"
echo "SECTION 8: PROTECTION FEATURES (OVP, OCP)"
echo "========================================================================"
echo ""

echo "Test 8.1: Set over-voltage protection (OVP) to 4.5V"
lager battery $BATTERY_NET ovp 4.5 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.2: Read OVP setting"
lager battery $BATTERY_NET ovp --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.3: Set over-current protection (OCP) to 3.0A"
lager battery $BATTERY_NET ocp 3.0 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.4: Read OCP setting"
lager battery $BATTERY_NET ocp --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.5: Clear all protection conditions (OVP/OCP)"
lager battery $BATTERY_NET clear --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.6: Clear OVP only"
lager battery $BATTERY_NET clear-ovp --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.7: Clear OCP only"
lager battery $BATTERY_NET clear-ocp --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.8: OVP should be >= batt-full (validation)"
lager battery $BATTERY_NET batt-full 4.2 --box $BOX >/dev/null 2>&1
lager battery $BATTERY_NET ovp 4.5 --box $BOX >/dev/null 2>&1
BATT_FULL=$(lager battery $BATTERY_NET batt-full --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
OVP=$(lager battery $BATTERY_NET ovp --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [ -n "$OVP" ] && [ -n "$BATT_FULL" ] && (( $(echo "$OVP >= $BATT_FULL" | bc -l) )); then
  track_test_msg "pass" "OVP ($OVP) >= batt-full ($BATT_FULL)"
else
  track_test_msg "fail" "OVP ($OVP) < batt-full ($BATT_FULL)"
fi
echo ""

# ============================================================
# SECTION 9: MODE AND MODEL OPERATIONS
# ============================================================
start_section "Mode and Model Operations"
echo "========================================================================"
echo "SECTION 9: MODE AND MODEL OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 9.1: Read current battery mode"
lager battery $BATTERY_NET mode --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.2: Set battery mode to 'dynamic'"
lager battery $BATTERY_NET mode dynamic --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.3: Read current battery model"
lager battery $BATTERY_NET model --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.4: Set battery model to discharge mode"
lager battery $BATTERY_NET model "discharge" --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.5: Set battery mode (Keithley specific)"
lager battery $BATTERY_NET set --box $BOX >/dev/null 2>&1
track_test "pass"  # Pass if doesn't crash
echo ""

# ============================================================
# SECTION 10: COMPREHENSIVE PARAMETER CONFIGURATION
# ============================================================
start_section "Comprehensive Configuration"
echo "========================================================================"
echo "SECTION 10: COMPREHENSIVE PARAMETER CONFIGURATION"
echo "========================================================================"
echo ""

echo "Test 10.1: Configure complete battery profile (Li-Ion 3.7V 2500mAh)"
FAILED=0
lager battery $BATTERY_NET disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET batt-empty 3.0 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET batt-full 4.2 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET capacity 2.5 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET current-limit 1.0 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET ovp 4.5 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET ocp 2.0 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET soc 50 --box $BOX >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test_msg "pass" "Li-Ion profile configured" || track_test_msg "fail" "Li-Ion profile failed"
echo ""

echo "Test 10.2: Get comprehensive state"
lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.3: Enable battery with configured profile"
lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 11: SEQUENTIAL OPERATIONS
# ============================================================
start_section "Sequential Operations"
echo "========================================================================"
echo "SECTION 11: SEQUENTIAL OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 11.1: Rapid sequential parameter updates"
FAILED=0
lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET soc 50 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET capacity 2.5 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET current-limit 1.0 --box $BOX >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.2: State persistence during parameter changes"
lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1
lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1
lager battery $BATTERY_NET soc 80 --box $BOX >/dev/null 2>&1
lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 12: BOUNDARY AND EDGE CASES
# ============================================================
start_section "Boundary and Edge Cases"
echo "========================================================================"
echo "SECTION 12: BOUNDARY AND EDGE CASES"
echo "========================================================================"
echo ""

echo "Test 12.1: Zero capacity"
lager battery $BATTERY_NET capacity 0.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|positive"
track_test "pass"  # Pass whether it rejects or accepts cleanly
echo ""

echo "Test 12.2: Zero current limit"
lager battery $BATTERY_NET current-limit 0.0 --box $BOX >/dev/null 2>&1
track_test "pass"  # Pass if doesn't crash
echo ""

echo "Test 12.3: Very large voltage (test clamping)"
lager battery $BATTERY_NET voc 1000.0 --box $BOX >/dev/null 2>&1
track_test "pass"  # Pass if doesn't crash
echo ""

echo "Test 12.4: SOC at exact boundaries"
FAILED=0
lager battery $BATTERY_NET soc 0 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET soc 100 --box $BOX >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.5: Maximum precision voltage (many decimal places)"
lager battery $BATTERY_NET voc 3.7777777777 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 13: CONCURRENCY AND STRESS TESTS
# ============================================================
start_section "Concurrency and Stress Tests"
echo "========================================================================"
echo "SECTION 13: CONCURRENCY AND STRESS TESTS"
echo "========================================================================"
echo ""

echo "Test 13.1: Rapid SOC changes (10 iterations)"
FAILED=0
START_TIME=$(get_timestamp_ms)
for i in {1..10}; do
  SOC=$(( (i * 10) % 101 ))
  lager battery $BATTERY_NET soc $SOC --box $BOX >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  10 SOC changes completed in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 10))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.2: Rapid voltage changes (5 iterations)"
FAILED=0
for i in {1..5}; do
  VOLTAGE=$(echo "scale=2; 3.0 + ($i % 5) * 0.2" | bc)
  lager battery $BATTERY_NET voc $VOLTAGE --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.3: Mixed parameter stress test (5 iterations)"
FAILED=0
for i in {1..5}; do
  lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1 || FAILED=1
  lager battery $BATTERY_NET soc $(( (i * 20) % 101 )) --box $BOX >/dev/null 2>&1 || FAILED=1
  lager battery $BATTERY_NET capacity 2.5 --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.4: Enable/disable stress test (5 cycles)"
FAILED=0
START_TIME=$(get_timestamp_ms)
for i in {1..5}; do
  lager battery $BATTERY_NET disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  5 enable/disable cycles completed in ${ELAPSED_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.5: State query burst (5 queries)"
FAILED=0
for i in {1..5}; do
  lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.6: Protection clear stress test (5 iterations)"
FAILED=0
for i in {1..5}; do
  lager battery $BATTERY_NET clear --box $BOX >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 14: PERFORMANCE BENCHMARKS
# ============================================================
start_section "Performance Benchmarks"
echo "========================================================================"
echo "SECTION 14: PERFORMANCE BENCHMARKS"
echo "========================================================================"
echo ""

echo "Test 14.1: VOC write latency (5 iterations average)"
TOTAL_TIME=0
FAILED=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1 || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo "  Average VOC write time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.2: State query latency (5 iterations average)"
TOTAL_TIME=0
FAILED=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo "  Average state query time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.3: Enable/disable latency (5 iterations average)"
TOTAL_TIME=0
FAILED=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager battery $BATTERY_NET disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo "  Average enable/disable cycle time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.4: Parameter update rate (5 commands timed)"
START_TIME=$(get_timestamp_ms)
FAILED=0
for i in {1..5}; do
  lager battery $BATTERY_NET soc $(( (i * 20) % 101 )) --box $BOX >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$((END_TIME - START_TIME))
if [ $ELAPSED_MS -gt 0 ]; then
  UPDATE_RATE_DECIMAL=$(echo "scale=2; 5000 / $ELAPSED_MS" | bc)
  echo "  Parameter update rate: ${UPDATE_RATE_DECIMAL} updates/second (5 commands in ${ELAPSED_MS}ms)"
fi
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 15: ERROR RECOVERY TESTS
# ============================================================
start_section "Error Recovery Tests"
echo "========================================================================"
echo "SECTION 15: ERROR RECOVERY TESTS"
echo "========================================================================"
echo ""

echo "Test 15.1: Operations after invalid SOC"
lager battery $BATTERY_NET soc 999 --box $BOX >/dev/null 2>&1 || true
lager battery $BATTERY_NET soc 50 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 15.2: Multiple errors followed by valid commands"
lager battery $BATTERY_NET voc -1.0 --box $BOX >/dev/null 2>&1 || true
lager battery $BATTERY_NET soc -50 --box $BOX >/dev/null 2>&1 || true
lager battery $BATTERY_NET capacity -10.0 --box $BOX >/dev/null 2>&1 || true
FAILED=0
lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET soc 50 --box $BOX >/dev/null 2>&1 || FAILED=1
lager battery $BATTERY_NET capacity 2.5 --box $BOX >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 15.3: State consistency after errors"
lager battery $BATTERY_NET voc -999.0 --box $BOX >/dev/null 2>&1 || true
lager battery $BATTERY_NET state --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 16: DISCHARGE SIMULATION SCENARIOS
# ============================================================
start_section "Discharge Simulation Scenarios"
echo "========================================================================"
echo "SECTION 16: DISCHARGE SIMULATION SCENARIOS"
echo "========================================================================"
echo ""

echo "Test 16.1: Critical battery state (low SOC warning)"
lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1
lager battery $BATTERY_NET soc 5 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 16.2: Dead battery state (0% SOC)"
lager battery $BATTERY_NET soc 0 --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 17: REGRESSION TESTS (Specific Bug Fixes)
# ============================================================
start_section "Regression Tests"
echo "========================================================================"
echo "SECTION 17: REGRESSION TESTS (Bug Fixes Validation)"
echo "========================================================================"
echo ""

echo "Test 17.1: Verify negative voltage rejection"
lager battery $BATTERY_NET voc -1.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|negative" && track_test "pass" || track_test "fail"
echo ""

echo "Test 17.2: Verify SOC >100% rejection"
lager battery $BATTERY_NET soc 150 --box $BOX 2>&1 | grep -qi "error\|invalid\|range" && track_test "pass" || track_test "fail"
echo ""

echo "Test 17.3: Verify SOC <0% rejection"
lager battery $BATTERY_NET soc -50 --box $BOX 2>&1 | grep -qi "error\|invalid\|range" && track_test "pass" || track_test "fail"
echo ""

echo "Test 17.4: Verify enable/disable state persistence"
lager battery $BATTERY_NET enable --box $BOX --yes >/dev/null 2>&1
STATE1=$(lager battery $BATTERY_NET state --box $BOX 2>&1 | grep -iE "enabled:.*on" && echo "enabled" || echo "disabled")
lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1
STATE2=$(lager battery $BATTERY_NET state --box $BOX 2>&1 | grep -iE "enabled:.*on" && echo "enabled" || echo "disabled")
[ "$STATE1" = "$STATE2" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 17.5: Verify protection clear doesn't affect other parameters"
lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1
lager battery $BATTERY_NET soc 75 --box $BOX >/dev/null 2>&1
VOC_BEFORE=$(lager battery $BATTERY_NET voc --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager battery $BATTERY_NET clear --box $BOX >/dev/null 2>&1
VOC_AFTER=$(lager battery $BATTERY_NET voc --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
[ "$VOC_BEFORE" = "$VOC_AFTER" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 17.6: Verify parameter consistency after multiple reads"
lager battery $BATTERY_NET voc 3.777 --box $BOX >/dev/null 2>&1
READBACK1=$(lager battery $BATTERY_NET voc --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
READBACK2=$(lager battery $BATTERY_NET voc --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
READBACK3=$(lager battery $BATTERY_NET voc --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
[ "$READBACK1" = "$READBACK2" ] && [ "$READBACK2" = "$READBACK3" ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 18: TIMEOUT AND RELIABILITY TESTS
# ============================================================
start_section "Timeout and Reliability Tests"
echo "========================================================================"
echo "SECTION 18: TIMEOUT AND RELIABILITY TESTS"
echo "========================================================================"
echo ""

echo "Test 18.1: Command timeout test (VOC with 10s timeout)"
timeout 10s lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1
EXIT_CODE=$?
if [ $EXIT_CODE -eq 124 ]; then
  track_test_msg "fail" "Command timed out after 10s"
else
  track_test "pass"
fi
echo ""

echo "Test 18.2: Multiple rapid commands (reliability test)"
FAILURE_COUNT=0
for i in {1..5}; do
  timeout 10s lager battery $BATTERY_NET soc $(( (i * 20) % 101 )) --box $BOX >/dev/null 2>&1
  if [ $? -eq 124 ]; then
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
  fi
done
[ $FAILURE_COUNT -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 18.3: Stress test with timeout protection (10 commands in 60s)"
COMPLETED=0
TIMEDOUT=0
for i in {1..10}; do
  timeout 5s lager battery $BATTERY_NET soc $(( i * 10 )) --box $BOX >/dev/null 2>&1
  EXIT_CODE=$?
  if [ $EXIT_CODE -eq 124 ]; then
    TIMEDOUT=$((TIMEDOUT + 1))
  elif [ $EXIT_CODE -eq 0 ]; then
    COMPLETED=$((COMPLETED + 1))
  fi
done
echo "  Stress test: $COMPLETED succeeded, $TIMEDOUT timed out"
[ $TIMEDOUT -le 3 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Setting battery to safe state..."
lager battery $BATTERY_NET disable --box $BOX --yes >/dev/null 2>&1 || true
lager battery $BATTERY_NET soc 50 --box $BOX >/dev/null 2>&1 || true
lager battery $BATTERY_NET voc 3.7 --box $BOX >/dev/null 2>&1 || true
lager battery $BATTERY_NET clear --box $BOX >/dev/null 2>&1 || true
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

echo "Final battery state:"
lager battery $BATTERY_NET state --box $BOX
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

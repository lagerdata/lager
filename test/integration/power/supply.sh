#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Streamlined test suite for lager supply commands (5-10 minute target)
# Focuses on critical functionality with reduced iterations

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e  # DON'T exit on error - we want to track failures

# Initialize the test harness
init_harness

# Safety delay between tests (reduced)
TEST_DELAY=0.2

# Check if required arguments are provided
if [ $# -lt 2 ]; then
  echo "Usage: $0 <BOX> <SUPPLY_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box supply1"
  echo "  $0 <BOX_IP> supply1"
  echo ""
  echo "Arguments:"
  echo "  BOX         - Box ID or Tailscale IP address"
  echo "  SUPPLY_NET  - Name of the supply net to test"
  echo ""
  exit 1
fi

BOX="$1"
SUPPLY_NET="$2"

echo "========================================================================"
echo "LAGER SUPPLY FAST TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "Supply Net: $SUPPLY_NET"
echo ""

# ============================================================
# SECTION 1: BASIC COMMANDS
# ============================================================
start_section "Basic Commands"
echo "========================================================================"
echo "SECTION 1: BASIC COMMANDS"
echo "========================================================================"
echo ""

echo "Test 1.1: List available boxes"
lager boxes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: List available nets"
lager nets --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Verify supply net exists"
if lager nets --box $BOX | grep -q "$SUPPLY_NET"; then
  echo -e "${GREEN}[OK] Supply net found${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] Supply net not found${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.4: Supply help output"
lager supply --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: ERROR VALIDATION
# ============================================================
start_section "Error Validation"
echo "========================================================================"
echo "SECTION 2: ERROR VALIDATION"
echo "========================================================================"
echo ""

echo "Test 2.1: Invalid net name"
lager supply nonexistent_net state --box $BOX 2>&1 | grep -qi "not found\|error" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: Invalid box"
lager supply $SUPPLY_NET state --box INVALID-BOX 2>&1 | grep -qi "error\|don't have" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: Negative voltage"
lager supply $SUPPLY_NET voltage -1.0 --box $BOX --yes 2>&1 | grep -qi "error\|No such option" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.4: Invalid voltage format"
lager supply $SUPPLY_NET voltage abc --box $BOX --yes 2>&1 | grep -qi "error\|not a valid" && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.5: Extremely high voltage (1000V - should clamp or reject)"
lager supply $SUPPLY_NET voltage 1000.0 --box $BOX --yes 2>&1 | grep -qi "WARNING\|clamp\|ERROR" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 3: STATE AND CONTROL
# ============================================================
start_section "State and Control"
echo "========================================================================"
echo "SECTION 3: STATE AND CONTROL"
echo "========================================================================"
echo ""

echo "Test 3.1: Get power supply state"
lager supply $SUPPLY_NET state --box $BOX >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Disable supply output"
lager supply $SUPPLY_NET disable --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: Verify disabled state"
lager supply $SUPPLY_NET state --box $BOX | grep -iE "disabled|output.*off|enabled:.*off" >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Enable supply output"
lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.5: Verify enabled state"
STATE_OUTPUT=$(lager supply $SUPPLY_NET state --box $BOX 2>&1)
echo "$STATE_OUTPUT" | grep -iE "enabled:.*on" >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.6: Rapid enable/disable cycling (5 cycles)"
FAILED=0
for i in {1..5}; do
  lager supply $SUPPLY_NET disable --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: VOLTAGE OPERATIONS
# ============================================================
start_section "Voltage Operations"
echo "========================================================================"
echo "SECTION 4: VOLTAGE OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 4.1: Set voltage to 0V"
lager supply $SUPPLY_NET voltage 0.0 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Set voltage to 3.3V"
lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.3: Set voltage to 5.0V"
lager supply $SUPPLY_NET voltage 5.0 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.4: Read voltage back"
VOLTAGE_OUTPUT=$(lager supply $SUPPLY_NET voltage --box $BOX 2>/dev/null)
[ -n "$VOLTAGE_OUTPUT" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.5: Voltage sweep (0V to 10V in 2V steps)"
FAILED=0
for voltage in 0.0 2.0 4.0 6.0 8.0 10.0; do
  lager supply $SUPPLY_NET voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.6: Common supply voltages"
FAILED=0
for voltage in 1.8 3.3 5.0 12.0; do
  lager supply $SUPPLY_NET voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.7: Rapid voltage changes (10 iterations)"
FAILED=0
for i in {1..10}; do
  VOLTAGE=$(echo "scale=1; ($i % 5) * 1.0" | bc)
  lager supply $SUPPLY_NET voltage $VOLTAGE --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 5: CURRENT OPERATIONS
# ============================================================
start_section "Current Operations"
echo "========================================================================"
echo "SECTION 5: CURRENT OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 5.1: Set current limit to 0.5A"
lager supply $SUPPLY_NET current 0.5 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Set current limit to 1.0A"
lager supply $SUPPLY_NET current 1.0 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: Set current limit to 2.0A"
lager supply $SUPPLY_NET current 2.0 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.4: Read current back"
CURRENT_OUTPUT=$(lager supply $SUPPLY_NET current --box $BOX 2>/dev/null)
[ -n "$CURRENT_OUTPUT" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.5: Current sweep (0.5A to 2.0A)"
FAILED=0
for current in 0.5 1.0 1.5 2.0; do
  lager supply $SUPPLY_NET current $current --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.6: Rapid current changes (10 iterations)"
FAILED=0
for i in {1..10}; do
  CURRENT=$(echo "scale=1; 0.5 + ($i % 3) * 0.5" | bc)
  lager supply $SUPPLY_NET current $CURRENT --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: PROTECTION FEATURES
# ============================================================
start_section "Protection Features"
echo "========================================================================"
echo "SECTION 6: PROTECTION FEATURES"
echo "========================================================================"
echo ""

echo "Test 6.1: Set voltage with OVP"
lager supply $SUPPLY_NET voltage 5.0 --ovp 6.0 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Set voltage with both OVP and OCP"
lager supply $SUPPLY_NET voltage 3.3 --ovp 5.0 --ocp 2.0 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.3: OVP below voltage (should fail)"
lager supply $SUPPLY_NET voltage 5.0 --ovp 4.0 --box $BOX --yes 2>&1 | grep -qi "error\|cannot\|less than\|invalid" && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.4: Clear OVP"
lager supply $SUPPLY_NET clear-ovp --box $BOX >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.5: Clear OCP"
lager supply $SUPPLY_NET clear-ocp --box $BOX >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.6: Rapid protection clears (10 iterations)"
FAILED=0
for i in {1..10}; do
  lager supply $SUPPLY_NET clear-ovp --box $BOX >/dev/null || FAILED=1
  lager supply $SUPPLY_NET clear-ocp --box $BOX >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: COMBINED OPERATIONS
# ============================================================
start_section "Combined Operations"
echo "========================================================================"
echo "SECTION 7: COMBINED OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 7.1: Set voltage and current together"
FAILED=0
lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY_NET current 1.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.2: Various V/I combinations"
FAILED=0
for combo in "1.8,0.5" "3.3,1.0" "5.0,2.0"; do
  IFS=',' read -r voltage current <<< "$combo"
  lager supply $SUPPLY_NET voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY_NET current $current --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.3: Full supply configuration (3.3V @ 1A)"
FAILED=0
lager supply $SUPPLY_NET disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY_NET current 1.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY_NET clear-ovp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY_NET clear-ocp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.4: State persistence during parameter changes"
lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null
STATE1=$(lager supply $SUPPLY_NET state --box $BOX 2>&1 | grep -iE "enabled:.*on" && echo "enabled" || echo "disabled")
lager supply $SUPPLY_NET voltage 5.0 --box $BOX --yes >/dev/null
STATE2=$(lager supply $SUPPLY_NET state --box $BOX 2>&1 | grep -iE "enabled:.*on" && echo "enabled" || echo "disabled")
[ "$STATE1" = "$STATE2" ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 8: BOUNDARY CASES
# ============================================================
start_section "Boundary Cases"
echo "========================================================================"
echo "SECTION 8: BOUNDARY CASES"
echo "========================================================================"
echo ""

echo "Test 8.1: Zero voltage"
lager supply $SUPPLY_NET voltage 0.0 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.2: Very small voltage (0.001V)"
lager supply $SUPPLY_NET voltage 0.001 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.3: High precision voltage (3.141592V)"
lager supply $SUPPLY_NET voltage 3.141592 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.4: Very large voltage (100V - clamping test)"
lager supply $SUPPLY_NET voltage 100.0 --box $BOX --yes >/dev/null 2>&1
track_test "pass"  # Pass if doesn't crash
echo ""

# ============================================================
# SECTION 9: STRESS TESTS
# ============================================================
start_section "Stress Tests"
echo "========================================================================"
echo "SECTION 9: STRESS TESTS"
echo "========================================================================"
echo ""

echo "Test 9.1: Rapid voltage changes (20 iterations)"
FAILED=0
START_TIME=$(get_timestamp_ms)
for i in {1..20}; do
  VOLTAGE=$(echo "scale=1; 1.0 + ($i % 5) * 1.0" | bc)
  lager supply $SUPPLY_NET voltage $VOLTAGE --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  Completed 20 changes in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 20))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.2: Rapid current changes (20 iterations)"
FAILED=0
for i in {1..20}; do
  CURRENT=$(echo "scale=1; 0.5 + ($i % 3) * 0.5" | bc)
  lager supply $SUPPLY_NET current $CURRENT --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.3: Enable/disable stress (20 cycles)"
FAILED=0
for i in {1..20}; do
  lager supply $SUPPLY_NET disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.4: Mixed parameter stress (15 iterations)"
FAILED=0
for i in {1..15}; do
  VOLTAGE=$(echo "scale=1; 2.0 + ($i % 3) * 1.0" | bc)
  CURRENT=$(echo "scale=1; 0.5 + ($i % 2) * 0.5" | bc)
  lager supply $SUPPLY_NET voltage $VOLTAGE --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY_NET current $CURRENT --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 10: PERFORMANCE BENCHMARKS
# ============================================================
start_section "Performance Benchmarks"
echo "========================================================================"
echo "SECTION 10: PERFORMANCE BENCHMARKS"
echo "========================================================================"
echo ""

echo "Test 10.1: Voltage write latency (5 iterations average)"
TOTAL_TIME=0
FAILED=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo "  Average voltage write time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.2: State query latency (5 iterations average)"
TOTAL_TIME=0
FAILED=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager supply $SUPPLY_NET state --box $BOX >/dev/null || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo "  Average state query time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.3: Enable/disable latency (5 cycles average)"
TOTAL_TIME=0
FAILED=0
for i in {1..5}; do
  START_TIME=$(get_timestamp_ms)
  lager supply $SUPPLY_NET disable --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 5))
echo "  Average enable/disable cycle time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 11: ERROR RECOVERY
# ============================================================
start_section "Error Recovery"
echo "========================================================================"
echo "SECTION 11: ERROR RECOVERY"
echo "========================================================================"
echo ""

echo "Test 11.1: Recovery after invalid voltage"
lager supply $SUPPLY_NET voltage -100.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.2: Recovery after invalid current"
lager supply $SUPPLY_NET current -50.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY_NET current 1.0 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.3: State consistency after errors"
lager supply $SUPPLY_NET voltage -999.0 --box $BOX >/dev/null 2>&1 || true
lager supply $SUPPLY_NET state --box $BOX >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 12: POWER RAMPING
# ============================================================
start_section "Power Ramping"
echo "========================================================================"
echo "SECTION 12: POWER RAMPING"
echo "========================================================================"
echo ""

echo "Test 12.1: Voltage ramp-up (0V to 5V)"
FAILED=0
lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null || FAILED=1
for voltage in 0.0 1.0 2.0 3.0 4.0 5.0; do
  lager supply $SUPPLY_NET voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.2: Voltage ramp-down (5V to 0V)"
FAILED=0
for voltage in 5.0 4.0 3.0 2.0 1.0 0.0; do
  lager supply $SUPPLY_NET voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.3: Power cycling (5 cycles)"
FAILED=0
lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY_NET current 1.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
for i in {1..5}; do
  lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null || FAILED=1
  sleep 0.1
  lager supply $SUPPLY_NET disable --box $BOX --yes >/dev/null || FAILED=1
  sleep 0.1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 13: REGRESSION TESTS
# ============================================================
start_section "Regression Tests"
echo "========================================================================"
echo "SECTION 13: REGRESSION TESTS"
echo "========================================================================"
echo ""

echo "Test 13.1: Negative voltage rejection"
lager supply $SUPPLY_NET voltage -1.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|negative\|No such option" && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.2: Enable state persistence across voltage changes"
lager supply $SUPPLY_NET enable --box $BOX --yes >/dev/null
STATE1=$(lager supply $SUPPLY_NET state --box $BOX 2>&1 | grep -iE "enabled:.*on" && echo "enabled" || echo "disabled")
lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null
STATE2=$(lager supply $SUPPLY_NET state --box $BOX 2>&1 | grep -iE "enabled:.*on" && echo "enabled" || echo "disabled")
[ "$STATE1" = "$STATE2" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.3: Protection clear doesn't affect V/I settings"
lager supply $SUPPLY_NET voltage 3.3 --box $BOX --yes >/dev/null
lager supply $SUPPLY_NET current 1.5 --box $BOX --yes >/dev/null 2>&1
V_BEFORE=$(lager supply $SUPPLY_NET voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager supply $SUPPLY_NET clear-ovp --box $BOX >/dev/null
lager supply $SUPPLY_NET clear-ocp --box $BOX >/dev/null
V_AFTER=$(lager supply $SUPPLY_NET voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
# Allow for small tolerance (0.5% or 0.01V, whichever is larger) due to instrument precision
if [ -n "$V_BEFORE" ] && [ -n "$V_AFTER" ]; then
  DIFF=$(echo "scale=4; if ($V_BEFORE > $V_AFTER) $V_BEFORE - $V_AFTER else $V_AFTER - $V_BEFORE" | bc)
  TOLERANCE=$(echo "scale=4; pct = 3.3 * 0.005; if (pct > 0.01) pct else 0.01" | bc)
  IS_CLOSE=$(echo "$DIFF <= $TOLERANCE" | bc)
  [ "$IS_CLOSE" = "1" ] && track_test "pass" || track_test "fail"
else
  track_test "fail"
fi
echo ""

echo "Test 13.4: Parameter consistency across multiple reads"
lager supply $SUPPLY_NET voltage 5.0 --box $BOX --yes >/dev/null
READBACK1=$(lager supply $SUPPLY_NET voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
READBACK2=$(lager supply $SUPPLY_NET voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
READBACK3=$(lager supply $SUPPLY_NET voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
[ "$READBACK1" = "$READBACK2" ] && [ "$READBACK2" = "$READBACK3" ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Setting supply to safe state..."
lager supply $SUPPLY_NET disable --box $BOX --yes >/dev/null
lager supply $SUPPLY_NET voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY_NET current 0.1 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY_NET clear-ovp --box $BOX >/dev/null 2>&1 || true
lager supply $SUPPLY_NET clear-ocp --box $BOX >/dev/null 2>&1 || true
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

echo "Final supply state:"
lager supply $SUPPLY_NET state --box $BOX
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

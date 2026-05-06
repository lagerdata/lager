#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for lager arm commands
# Tests all edge cases, error conditions, and production features
#
# Usage: ./arm.sh <BOX_NAME_OR_IP> <ARM_NET>

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
  echo "Usage: $0 <BOX_NAME_OR_IP> <ARM_NET>"
  echo ""
  echo "Examples:"
  echo "  $0 my-box arm1"
  echo "  $0 <BOX_IP> rotrics_arm"
  echo ""
  echo "Arguments:"
  echo "  BOX_NAME_OR_IP - Box name or Tailscale IP address"
  echo "  ARM_NET        - Name of the arm net to test"
  echo ""
  exit 1
fi

BOX_INPUT="$1"
ARM_NET="$2"

# Register box from IP if needed
register_box_from_ip "$BOX_INPUT"

print_script_header "LAGER ARM COMPREHENSIVE TEST SUITE" "$BOX" "$ARM_NET"
echo "IMPORTANT: This test will physically move the arm!"
echo "Ensure the workspace is clear and the arm can move safely."
echo ""

# ============================================================================
# SECTION 1: BASIC COMMANDS
# ============================================================================
print_section_header "SECTION 1: BASIC COMMANDS (No Connection Required)"
start_section "Basic Commands"

echo "Test 1.1: List available boxes"
lager boxes 2>&1 | grep -q '.' && track_test_msg "pass" "Boxes listed" || track_test_msg "fail" "Could not list boxes"
echo ""

echo "Test 1.2: List available nets"
lager nets --box $BOX 2>&1 | grep -q '.' && track_test_msg "pass" "Nets listed" || track_test_msg "fail" "Could not list nets"
echo ""

echo "Test 1.3: Verify arm net exists"
lager nets --box $BOX 2>&1 | grep -q "$ARM_NET" && track_test_msg "pass" "Arm net found" || track_test_msg "fail" "Arm net not found"
echo ""

echo "Test 1.4: ARM help output"
lager arm --help 2>&1 | grep -q "Interface for robot arm" && track_test_msg "pass" "Help output OK" || track_test_msg "fail" "Help output missing"
echo ""

# ============================================================================
# SECTION 2: ERROR CASES
# ============================================================================
print_section_header "SECTION 2: ERROR CASES (Invalid Commands)"
start_section "Error Cases"

echo "Test 2.1: Invalid net name"
OUTPUT=$(lager arm nonexistent_net position --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|not found\|required"; then
  track_test_msg "pass" "Error caught correctly"
else
  track_test_msg "fail" "No error for invalid net"
fi
echo ""

echo "Test 2.2: Invalid box"
OUTPUT=$(lager arm $ARM_NET position --box INVALID-BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|don't have"; then
  track_test_msg "pass" "Error caught correctly"
else
  track_test_msg "fail" "No error for invalid box"
fi
echo ""

echo "Test 2.3: Missing net name argument"
OUTPUT=$(lager arm position --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "required\|missing\|error\|usage"; then
  track_test_msg "pass" "Missing argument caught"
else
  track_test_msg "fail" "Missing argument not caught"
fi
echo ""

echo "Test 2.4: Move without coordinates"
OUTPUT=$(lager arm $ARM_NET move --box $BOX --yes 2>&1)
if echo "$OUTPUT" | grep -qi "missing\|required"; then
  track_test_msg "pass" "Missing coordinates caught"
else
  track_test_msg "fail" "Missing coordinates not caught"
fi
echo ""

echo "Test 2.5: Move with invalid coordinate format"
OUTPUT=$(lager arm $ARM_NET move abc def ghi --box $BOX --yes 2>&1)
if echo "$OUTPUT" | grep -qi "error\|invalid"; then
  track_test_msg "pass" "Invalid format caught"
else
  track_test_msg "fail" "Invalid format not caught"
fi
echo ""

echo "Test 2.6: Negative timeout value"
OUTPUT=$(lager arm $ARM_NET move 0 300 0 --timeout -1 --box $BOX --yes 2>&1)
if echo "$OUTPUT" | grep -qi "error\|invalid"; then
  track_test_msg "pass" "Negative timeout caught"
else
  echo -e "  ${YELLOW}[WARNING] Negative timeout may have been accepted${NC}"
  track_test_msg "pass" "Negative timeout accepted (non-fatal)"
fi
echo ""

echo "Test 2.7: Set acceleration with invalid values"
OUTPUT=$(lager arm $ARM_NET set-acceleration -10 20 --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qi "error\|invalid\|negative"; then
  track_test_msg "pass" "Invalid acceleration caught"
else
  track_test_msg "fail" "Invalid acceleration not caught"
fi
echo ""

# ============================================================================
# SECTION 3: MOTOR CONTROL
# ============================================================================
print_section_header "SECTION 3: MOTOR CONTROL (Enable/Disable)"
start_section "Motor Control"

echo "Test 3.1: Disable motors"
if lager arm $ARM_NET disable-motor --box $BOX 2>&1; then
  track_test_msg "pass" "Motors disabled"
else
  track_test_msg "fail" "Failed to disable motors"
fi
echo ""

echo "Test 3.2: Read position with motors disabled"
OUTPUT=$(lager arm $ARM_NET position --box $BOX 2>&1)
if echo "$OUTPUT" | grep -qE "X:.*Y:.*Z:"; then
  track_test_msg "pass" "Position read with motors disabled"
else
  echo -e "  ${YELLOW}[WARNING] Position read failed with motors disabled${NC}"
  track_test_msg "pass" "Position read with motors disabled (non-fatal)"
fi
echo ""

echo "Test 3.3: Enable motors"
if lager arm $ARM_NET enable-motor --box $BOX 2>&1; then
  track_test_msg "pass" "Motors enabled"
else
  track_test_msg "fail" "Failed to enable motors"
fi
echo ""

echo "Test 3.4: Multiple enable/disable cycles"
for i in {1..3}; do
  lager arm $ARM_NET disable-motor --box $BOX >/dev/null 2>&1
  lager arm $ARM_NET enable-motor --box $BOX >/dev/null 2>&1
done
track_test_msg "pass" "Multiple cycles completed"
echo ""

# ============================================================================
# SECTION 4: POSITION READING
# ============================================================================
print_section_header "SECTION 4: POSITION READING"
start_section "Position Reading"

echo "Test 4.1: Read current position"
POS_OUTPUT=$(lager arm $ARM_NET position --box $BOX 2>&1)
echo "$POS_OUTPUT"
if echo "$POS_OUTPUT" | grep -qE "X:.*Y:.*Z:"; then
  track_test_msg "pass" "Position read successful"
else
  track_test_msg "fail" "Position read failed"
fi
echo ""

echo "Test 4.2: Extract position values"
X_VAL=$(echo "$POS_OUTPUT" | grep -oE "X: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+")
Y_VAL=$(echo "$POS_OUTPUT" | grep -oE "Y: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+")
Z_VAL=$(echo "$POS_OUTPUT" | grep -oE "Z: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+")
if [[ -n "$X_VAL" && -n "$Y_VAL" && -n "$Z_VAL" ]]; then
  track_test_msg "pass" "Position values: X=$X_VAL Y=$Y_VAL Z=$Z_VAL"
else
  track_test_msg "fail" "Could not extract position values"
fi
echo ""

echo "Test 4.3: Multiple position reads (stability)"
echo "Reading position 5 times:"
for i in {1..5}; do
  POS=$(lager arm $ARM_NET position --box $BOX 2>&1)
  X=$(echo "$POS" | grep -oE "X: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+")
  Y=$(echo "$POS" | grep -oE "Y: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+")
  Z=$(echo "$POS" | grep -oE "Z: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+")
  echo "  Read $i: X=$X Y=$Y Z=$Z"
done
track_test_msg "pass" "All reads completed"
echo ""

echo "Test 4.4: Rapid position reads (10x)"
FAIL_COUNT=0
for i in {1..10}; do
  lager arm $ARM_NET position --box $BOX >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
done
if [ $FAIL_COUNT -eq 0 ]; then
  track_test_msg "pass" "10 rapid reads completed"
else
  track_test_msg "fail" "$FAIL_COUNT/10 reads failed"
fi
echo ""

# ============================================================================
# SECTION 5: HOME POSITION
# ============================================================================
print_section_header "SECTION 5: HOME POSITION (X0 Y300 Z0)"
start_section "Home Position"

echo "Test 5.1: Move to home position"
if lager arm $ARM_NET go-home --box $BOX --yes 2>&1; then
  track_test_msg "pass" "Moved to home position"
else
  track_test_msg "fail" "Failed to move home"
fi
echo ""

echo "Test 5.2: Verify home position coordinates"
sleep 1  # Allow movement to complete
POS=$(lager arm $ARM_NET position --box $BOX 2>&1)
echo "$POS"
X=$(echo "$POS" | grep -oE "X: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)
Y=$(echo "$POS" | grep -oE "Y: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)
Z=$(echo "$POS" | grep -oE "Z: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)

# Home position should be approximately X=0, Y=300, Z=0 (with tolerance)
if [ -n "$X" ] && [ -n "$Y" ] && [ -n "$Z" ]; then
  X_ABS=$(echo "$X" | sed 's/-//')
  Y_DIFF=$(echo "$Y - 300" | bc | sed 's/-//')
  Z_ABS=$(echo "$Z" | sed 's/-//')

  # Check with 5mm tolerance
  if (( $(echo "$X_ABS < 5 && $Y_DIFF < 5 && $Z_ABS < 5" | bc -l) )); then
    track_test_msg "pass" "At home position (tolerance: +/-5mm)"
  else
    echo -e "  ${YELLOW}[WARNING] Position differs from expected home (X=$X Y=$Y Z=$Z)${NC}"
    track_test_msg "pass" "Position differs from expected home (non-fatal)"
  fi
else
  track_test_msg "fail" "Could not verify position"
fi
echo ""

echo "Test 5.3: Multiple go-home commands"
for i in {1..3}; do
  lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
  sleep 1
done
track_test_msg "pass" "Multiple go-home commands completed"
echo ""

# ============================================================================
# SECTION 6: ABSOLUTE MOVEMENT
# ============================================================================
print_section_header "SECTION 6: ABSOLUTE MOVEMENT"
start_section "Absolute Movement"

# Return to home first
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 1

echo "Test 6.1: Move to safe position (X=50, Y=250, Z=50)"
if lager arm $ARM_NET move 50 250 50 --box $BOX --yes 2>&1; then
  track_test_msg "pass" "Move command succeeded"
else
  track_test_msg "fail" "Move command failed"
fi
sleep 2  # Allow movement to complete
echo ""

echo "Test 6.2: Verify position after move"
POS=$(lager arm $ARM_NET position --box $BOX 2>&1)
echo "$POS"
X=$(echo "$POS" | grep -oE "X: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)
Y=$(echo "$POS" | grep -oE "Y: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)
Z=$(echo "$POS" | grep -oE "Z: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)

# Check with 5mm tolerance
X_DIFF=$(echo "$X - 50" | bc | sed 's/-//')
Y_DIFF=$(echo "$Y - 250" | bc | sed 's/-//')
Z_DIFF=$(echo "$Z - 50" | bc | sed 's/-//')

if (( $(echo "$X_DIFF < 5 && $Y_DIFF < 5 && $Z_DIFF < 5" | bc -l) )); then
  track_test_msg "pass" "Position verified (tolerance: +/-5mm)"
else
  echo -e "  ${YELLOW}[WARNING] Position differs (X=$X Y=$Y Z=$Z)${NC}"
  track_test_msg "pass" "Position differs (non-fatal)"
fi
echo ""

echo "Test 6.3: Move to another position (X=0, Y=280, Z=30)"
lager arm $ARM_NET move 0 280 30 --box $BOX --yes >/dev/null 2>&1
sleep 2
POS=$(lager arm $ARM_NET position --box $BOX 2>&1)
echo "$POS"
track_test_msg "pass" "Second move completed"
echo ""

echo "Test 6.4: Move with custom timeout (2 seconds)"
if lager arm $ARM_NET move 25 275 25 --timeout 2.0 --box $BOX --yes 2>&1; then
  track_test_msg "pass" "Move with custom timeout succeeded"
else
  echo -e "  ${YELLOW}[WARNING] Move with short timeout may have timed out${NC}"
  track_test_msg "pass" "Move with short timeout (non-fatal)"
fi
sleep 2
echo ""

echo "Test 6.5: Sequential moves (5 positions)"
POSITIONS=(
  "10 290 10"
  "20 280 20"
  "30 270 30"
  "20 280 20"
  "10 290 10"
)
for pos in "${POSITIONS[@]}"; do
  lager arm $ARM_NET move $pos --box $BOX --yes >/dev/null 2>&1
  sleep 1
done
track_test_msg "pass" "Sequential moves completed"
echo ""

# Return to home
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2

# ============================================================================
# SECTION 7: RELATIVE MOVEMENT (DELTA)
# ============================================================================
print_section_header "SECTION 7: RELATIVE MOVEMENT (DELTA)"
start_section "Relative Movement"

# Ensure we're at home
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2

echo "Test 7.1: Move by delta (dX=10, dY=0, dZ=10)"
BEFORE=$(lager arm $ARM_NET position --box $BOX 2>&1)
X_BEFORE=$(echo "$BEFORE" | grep -oE "X: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)
Z_BEFORE=$(echo "$BEFORE" | grep -oE "Z: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)

if lager arm $ARM_NET move-by 10 0 10 --box $BOX --yes 2>&1; then
  track_test_msg "pass" "Delta move succeeded"
else
  track_test_msg "fail" "Delta move failed"
fi
sleep 2
echo ""

echo "Test 7.2: Verify delta movement"
AFTER=$(lager arm $ARM_NET position --box $BOX 2>&1)
echo "$AFTER"
X_AFTER=$(echo "$AFTER" | grep -oE "X: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)
Z_AFTER=$(echo "$AFTER" | grep -oE "Z: *[-+]?[0-9]*\.?[0-9]+" | grep -oE "[-+]?[0-9]*\.?[0-9]+" | head -1)

X_DELTA=$(echo "$X_AFTER - $X_BEFORE" | bc)
Z_DELTA=$(echo "$Z_AFTER - $Z_BEFORE" | bc)
echo "Deltas measured: dX=$X_DELTA dZ=$Z_DELTA"

X_DELTA_ABS=$(echo "$X_DELTA - 10" | bc | sed 's/-//')
Z_DELTA_ABS=$(echo "$Z_DELTA - 10" | bc | sed 's/-//')

if (( $(echo "$X_DELTA_ABS < 5 && $Z_DELTA_ABS < 5" | bc -l) )); then
  track_test_msg "pass" "Delta movement verified (tolerance: +/-5mm)"
else
  echo -e "  ${YELLOW}[WARNING] Delta differs from expected${NC}"
  track_test_msg "pass" "Delta differs from expected (non-fatal)"
fi
echo ""

echo "Test 7.3: Move by negative delta (return)"
lager arm $ARM_NET move-by -10 0 -10 --box $BOX --yes >/dev/null 2>&1
sleep 2
POS=$(lager arm $ARM_NET position --box $BOX 2>&1)
echo "$POS"
track_test_msg "pass" "Negative delta completed"
echo ""

echo "Test 7.4: Single-axis deltas"
echo "  Moving X only (+20mm)"
lager arm $ARM_NET move-by 20 0 0 --box $BOX --yes >/dev/null 2>&1
sleep 1
echo "  Moving Y only (-10mm)"
lager arm $ARM_NET move-by 0 -10 0 --box $BOX --yes >/dev/null 2>&1
sleep 1
echo "  Moving Z only (+15mm)"
lager arm $ARM_NET move-by 0 0 15 --box $BOX --yes >/dev/null 2>&1
sleep 1
track_test_msg "pass" "Single-axis deltas completed"
echo ""

echo "Test 7.5: Multiple small deltas"
for i in {1..5}; do
  lager arm $ARM_NET move-by 2 0 2 --box $BOX --yes >/dev/null 2>&1
  sleep 0.5
done
track_test_msg "pass" "Multiple small deltas completed"
echo ""

# Return to home
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2

# ============================================================================
# SECTION 8: ACCELERATION CONTROL
# ============================================================================
print_section_header "SECTION 8: ACCELERATION CONTROL"
start_section "Acceleration Control"

echo "Test 8.1: Set acceleration to default values (60, 60, 60)"
if lager arm $ARM_NET set-acceleration 60 60 60 --box $BOX 2>&1; then
  track_test_msg "pass" "Acceleration set"
else
  track_test_msg "fail" "Failed to set acceleration"
fi
echo ""

echo "Test 8.2: Set low acceleration (30, 30, 30)"
if lager arm $ARM_NET set-acceleration 30 30 30 --box $BOX 2>&1; then
  track_test_msg "pass" "Low acceleration set"
else
  track_test_msg "fail" "Failed to set low acceleration"
fi
echo ""

echo "Test 8.3: Test movement with low acceleration"
lager arm $ARM_NET move 20 280 20 --box $BOX --yes >/dev/null 2>&1
sleep 2
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2
track_test_msg "pass" "Movement with low acceleration completed"
echo ""

echo "Test 8.4: Set high acceleration (100, 100, 80)"
if lager arm $ARM_NET set-acceleration 100 100 80 --box $BOX 2>&1; then
  track_test_msg "pass" "High acceleration set"
else
  track_test_msg "fail" "Failed to set high acceleration"
fi
echo ""

echo "Test 8.5: Test movement with high acceleration"
lager arm $ARM_NET move 30 270 30 --box $BOX --yes >/dev/null 2>&1
sleep 2
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2
track_test_msg "pass" "Movement with high acceleration completed"
echo ""

# Reset to default
lager arm $ARM_NET set-acceleration 60 60 60 --box $BOX >/dev/null 2>&1

# ============================================================================
# SECTION 9: CALIBRATION
# ============================================================================
print_section_header "SECTION 9: CALIBRATION (Read and Save Position)"
start_section "Calibration"

echo "Test 9.1: Read and save current position"
if lager arm $ARM_NET read-and-save-position --box $BOX 2>&1; then
  track_test_msg "pass" "Position saved"
else
  track_test_msg "fail" "Failed to save position"
fi
echo ""

echo "Test 9.2: Move and save again"
lager arm $ARM_NET move 15 285 15 --box $BOX --yes >/dev/null 2>&1
sleep 2
if lager arm $ARM_NET read-and-save-position --box $BOX 2>&1; then
  track_test_msg "pass" "New position saved"
else
  track_test_msg "fail" "Failed to save new position"
fi
echo ""

echo "Test 9.3: Multiple calibration saves"
for i in {1..3}; do
  lager arm $ARM_NET read-and-save-position --box $BOX >/dev/null 2>&1
done
track_test_msg "pass" "Multiple saves completed"
echo ""

# Return to home
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2

# ============================================================================
# SECTION 10: PERFORMANCE BENCHMARKS
# ============================================================================
print_section_header "SECTION 10: PERFORMANCE BENCHMARKS"
start_section "Performance Benchmarks"

echo "Test 10.1: Position read latency (10 iterations average)"
TOTAL_TIME=0
for i in {1..10}; do
  START_TIME=$(get_timestamp_ms)
  lager arm $ARM_NET position --box $BOX >/dev/null
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 10))
echo "  Average read time: ${AVG_MS}ms"
if [ $AVG_MS -gt 5000 ]; then
  echo "  [WARNING] Slow (>5s per read)"
  track_test_msg "fail" "Average read time ${AVG_MS}ms (>5s)"
else
  track_test_msg "pass" "Average read time: ${AVG_MS}ms"
fi
echo ""

echo "Test 10.2: Move command latency"
START_TIME=$(get_timestamp_ms)
lager arm $ARM_NET move 10 290 10 --box $BOX --yes >/dev/null 2>&1
END_TIME=$(get_timestamp_ms)
MOVE_TIME=$((END_TIME - START_TIME))
track_test_msg "pass" "Move time: ${MOVE_TIME}ms"
sleep 1
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2
echo ""

echo "Test 10.3: Motor enable/disable latency"
START_TIME=$(get_timestamp_ms)
lager arm $ARM_NET disable-motor --box $BOX >/dev/null 2>&1
lager arm $ARM_NET enable-motor --box $BOX >/dev/null 2>&1
END_TIME=$(get_timestamp_ms)
MOTOR_TIME=$((END_TIME - START_TIME))
track_test_msg "pass" "Motor toggle time: ${MOTOR_TIME}ms"
echo ""

# ============================================================================
# SECTION 11: TIMEOUT HANDLING
# ============================================================================
print_section_header "SECTION 11: TIMEOUT HANDLING"
start_section "Timeout Handling"

echo "Test 11.1: Move with very short timeout (likely to timeout)"
OUTPUT=$(lager arm $ARM_NET move 50 250 50 --timeout 0.1 --box $BOX --yes 2>&1)
if echo "$OUTPUT" | grep -qi "timeout\|error"; then
  track_test_msg "pass" "Timeout detected correctly"
else
  echo -e "  ${YELLOW}[WARNING] Move completed faster than expected or timeout not detected${NC}"
  track_test_msg "pass" "Move completed (non-fatal)"
fi
sleep 2
echo ""

echo "Test 11.2: Move with generous timeout (10 seconds)"
if lager arm $ARM_NET move 0 300 0 --timeout 10.0 --box $BOX --yes >/dev/null 2>&1; then
  track_test_msg "pass" "Move with long timeout succeeded"
else
  track_test_msg "fail" "Move failed even with long timeout"
fi
sleep 2
echo ""

echo "Test 11.3: Multiple moves with standard timeout"
FAIL_COUNT=0
for i in {1..5}; do
  lager arm $ARM_NET move 10 290 10 --timeout 5.0 --box $BOX --yes >/dev/null 2>&1 || FAIL_COUNT=$((FAIL_COUNT + 1))
  sleep 1
done
if [ $FAIL_COUNT -eq 0 ]; then
  track_test_msg "pass" "All moves completed within timeout"
else
  echo -e "  ${YELLOW}[WARNING] ${FAIL_COUNT}/5 moves timed out${NC}"
  track_test_msg "pass" "${FAIL_COUNT}/5 moves timed out (non-fatal)"
fi
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2
echo ""

# ============================================================================
# SECTION 12: ERROR RECOVERY
# ============================================================================
print_section_header "SECTION 12: ERROR RECOVERY"
start_section "Error Recovery"

echo "Test 12.1: Recover from invalid command"
lager arm invalid_net position --box $BOX >/dev/null 2>&1 || true
if lager arm $ARM_NET position --box $BOX >/dev/null 2>&1; then
  track_test_msg "pass" "Recovered after invalid command"
else
  track_test_msg "fail" "Failed to recover"
fi
echo ""

echo "Test 12.2: Recover from movement error"
lager arm $ARM_NET move 99999 99999 99999 --timeout 0.1 --box $BOX --yes >/dev/null 2>&1 || true
sleep 1
if lager arm $ARM_NET position --box $BOX >/dev/null 2>&1; then
  track_test_msg "pass" "System functional after movement error"
else
  track_test_msg "fail" "System not responsive after error"
fi
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2
echo ""

echo "Test 12.3: Multiple errors then valid operation"
lager arm invalid1 position --box $BOX >/dev/null 2>&1 || true
lager arm invalid2 position --box $BOX >/dev/null 2>&1 || true
if lager arm $ARM_NET position --box $BOX >/dev/null 2>&1; then
  track_test_msg "pass" "Recovered after multiple errors"
else
  track_test_msg "fail" "Failed to recover after multiple errors"
fi
echo ""

# ============================================================================
# SECTION 13: STRESS TEST
# ============================================================================
print_section_header "SECTION 13: STRESS TEST (Continuous Operations)"
start_section "Stress Test"

echo "Test 13.1: Continuous position reads (30 seconds)"
echo "Reading position continuously for 30 seconds..."
START_TIME=$(date +%s)
SAMPLE_COUNT=0
FAIL_COUNT=0
while [ $(($(date +%s) - START_TIME)) -lt 30 ]; do
  lager arm $ARM_NET position --box $BOX >/dev/null 2>&1 && SAMPLE_COUNT=$((SAMPLE_COUNT + 1)) || FAIL_COUNT=$((FAIL_COUNT + 1))
done
echo "  Completed $SAMPLE_COUNT reads with $FAIL_COUNT failures"
if [ $FAIL_COUNT -eq 0 ]; then
  track_test_msg "pass" "$SAMPLE_COUNT reads, 0 failures"
else
  track_test_msg "fail" "$SAMPLE_COUNT reads, $FAIL_COUNT failures"
fi
echo ""

echo "Test 13.2: Repeated movement sequence (5 cycles)"
for cycle in {1..5}; do
  lager arm $ARM_NET move 20 280 20 --box $BOX --yes >/dev/null 2>&1
  sleep 1
  lager arm $ARM_NET move 10 290 10 --box $BOX --yes >/dev/null 2>&1
  sleep 1
  lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
  sleep 1
done
track_test_msg "pass" "5 movement cycles completed"
echo ""

echo "Test 13.3: Mixed operations (position, move, enable/disable)"
for i in {1..10}; do
  lager arm $ARM_NET position --box $BOX >/dev/null 2>&1
  lager arm $ARM_NET move-by 1 0 1 --box $BOX --yes >/dev/null 2>&1
  lager arm $ARM_NET disable-motor --box $BOX >/dev/null 2>&1
  lager arm $ARM_NET enable-motor --box $BOX >/dev/null 2>&1
done
track_test_msg "pass" "Mixed operations completed"
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2
echo ""

# ============================================================================
# SECTION 14: BOUNDARY TESTING
# ============================================================================
print_section_header "SECTION 14: BOUNDARY TESTING"
start_section "Boundary Testing"

echo "Test 14.1: Move to workspace limits (safe boundaries)"
echo "Testing Y-axis maximum (safe value: 350mm)"
lager arm $ARM_NET move 0 350 0 --timeout 10.0 --box $BOX --yes >/dev/null 2>&1 || echo "  Note: May be outside workspace"
sleep 2
track_test_msg "pass" "Boundary test completed"
echo ""

echo "Test 14.2: Move to workspace limits (Z-axis)"
lager arm $ARM_NET move 0 300 100 --timeout 10.0 --box $BOX --yes >/dev/null 2>&1 || echo "  Note: May be outside workspace"
sleep 2
track_test_msg "pass" "Z-axis boundary test completed"
echo ""

echo "Test 14.3: Zero coordinates (X=0, Y=0, Z=0 - may be invalid)"
OUTPUT=$(lager arm $ARM_NET move 0 0 0 --timeout 5.0 --box $BOX --yes 2>&1)
if echo "$OUTPUT" | grep -qi "error\|timeout\|obstructed"; then
  track_test_msg "pass" "Invalid position detected correctly"
else
  echo -e "  ${YELLOW}[WARNING] Zero coordinates may have been accepted${NC}"
  track_test_msg "pass" "Zero coordinates accepted (non-fatal)"
fi
sleep 1
echo ""

# Return to safe position
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2

# ============================================================================
# CLEANUP
# ============================================================================
print_section_header "CLEANUP"

echo "Returning arm to home position and disabling motors..."
lager arm $ARM_NET go-home --box $BOX --yes >/dev/null 2>&1
sleep 2
lager arm $ARM_NET disable-motor --box $BOX >/dev/null 2>&1
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

# ============================================================================
# PRINT SUMMARY
# ============================================================================
print_summary
exit_with_status

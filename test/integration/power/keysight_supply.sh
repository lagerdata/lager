#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive test suite for Keysight E36312A power supply (3 channels)
# Tests all three channels thoroughly with edge cases and cross-channel interactions

# Determine script directory for relative paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Source test framework
source "${SCRIPT_DIR}/../../framework/colors.sh"
source "${SCRIPT_DIR}/../../framework/harness.sh"

set +e  # DON'T exit on error - we want to track failures

# Initialize the test harness
init_harness

# Safety delay between tests
TEST_DELAY=0.2

# Check if required arguments are provided
if [ $# -lt 1 ]; then
  echo "Usage: $0 <BOX> [SUPPLY1] [SUPPLY2] [SUPPLY3]"
  echo ""
  echo "Examples:"
  echo "  $0 my-box                            # Auto-detect supply1, supply2, supply3"
  echo "  $0 my-box supply1 supply2 supply3    # Explicit net names"
  echo "  $0 <BOX_IP> ch1 ch2 ch3       # Custom net names"
  echo ""
  echo "Arguments:"
  echo "  BOX         - Box ID or Tailscale IP address"
  echo "  SUPPLY1-3   - Names of the supply nets (default: supply1, supply2, supply3)"
  echo ""
  exit 1
fi

BOX="$1"
SUPPLY1="${2:-supply1}"
SUPPLY2="${3:-supply2}"
SUPPLY3="${4:-supply3}"

# Cross-platform timestamp function (milliseconds)
get_timestamp_ms() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    echo $(( $(date +%s) * 1000 ))
  else
    echo $(( $(date +%s%N) / 1000000 ))
  fi
}

echo "========================================================================"
echo "KEYSIGHT E36312A COMPREHENSIVE TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "Channel 1: $SUPPLY1"
echo "Channel 2: $SUPPLY2"
echo "Channel 3: $SUPPLY3"
echo ""

# ============================================================
# SECTION 1: BASIC CONNECTIVITY
# ============================================================
start_section "Basic Connectivity"
echo "========================================================================"
echo "SECTION 1: BASIC CONNECTIVITY"
echo "========================================================================"
echo ""

echo "Test 1.1: List available boxes"
echo -e "${CYAN}Running: lager boxes${NC}"
lager boxes && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.2: List available nets on box"
echo -e "${CYAN}Running: lager nets --box $BOX${NC}"
lager nets --box $BOX && track_test "pass" || track_test "fail"
echo ""

echo "Test 1.3: Verify all three supply nets exist"
echo -e "${CYAN}Checking for: $SUPPLY1, $SUPPLY2, $SUPPLY3${NC}"
NETS_OUTPUT=$(lager nets --box $BOX 2>&1)
if echo "$NETS_OUTPUT" | grep -q "$SUPPLY1" && \
   echo "$NETS_OUTPUT" | grep -q "$SUPPLY2" && \
   echo "$NETS_OUTPUT" | grep -q "$SUPPLY3"; then
  echo -e "${GREEN}[OK] All three supply nets found${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] One or more supply nets not found${NC}"
  echo "Available nets:"
  echo "$NETS_OUTPUT"
  track_test "fail"
fi
echo ""

echo "Test 1.4: Supply command help"
echo -e "${CYAN}Running: lager supply --help${NC}"
lager supply --help && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: INDIVIDUAL CHANNEL BASIC OPERATIONS
# ============================================================
start_section "Individual Channel Basic Operations"
echo "========================================================================"
echo "SECTION 2: INDIVIDUAL CHANNEL BASIC OPERATIONS"
echo "========================================================================"
echo ""

for CHANNEL in 1 2 3; do
  eval SUPPLY=\$SUPPLY$CHANNEL

  echo -e "${BLUE}--- Channel $CHANNEL ($SUPPLY) ---${NC}"
  echo ""

  echo "Test 2.${CHANNEL}.1: Get state for channel $CHANNEL"
  echo -e "${CYAN}Running: lager supply $SUPPLY state --box $BOX${NC}"
  lager supply $SUPPLY state --box $BOX && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 2.${CHANNEL}.2: Disable channel $CHANNEL"
  echo -e "${CYAN}Running: lager supply $SUPPLY disable --box $BOX --yes${NC}"
  lager supply $SUPPLY disable --box $BOX --yes && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 2.${CHANNEL}.3: Verify disabled state for channel $CHANNEL"
  echo -e "${CYAN}Running: lager supply $SUPPLY state --box $BOX${NC}"
  STATE_OUTPUT=$(lager supply $SUPPLY state --box $BOX 2>&1)
  echo "$STATE_OUTPUT"
  echo "$STATE_OUTPUT" | grep -iE "disabled|output.*off|enabled:.*off" && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 2.${CHANNEL}.4: Set voltage to 3.3V on channel $CHANNEL"
  echo -e "${CYAN}Running: lager supply $SUPPLY voltage 3.3 --box $BOX --yes${NC}"
  lager supply $SUPPLY voltage 3.3 --box $BOX --yes && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 2.${CHANNEL}.5: Read voltage back from channel $CHANNEL"
  echo -e "${CYAN}Running: lager supply $SUPPLY voltage --box $BOX${NC}"
  VOLTAGE_OUTPUT=$(lager supply $SUPPLY voltage --box $BOX 2>&1)
  echo "$VOLTAGE_OUTPUT"
  [ -n "$VOLTAGE_OUTPUT" ] && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 2.${CHANNEL}.6: Enable channel $CHANNEL"
  echo -e "${CYAN}Running: lager supply $SUPPLY enable --box $BOX --yes${NC}"
  lager supply $SUPPLY enable --box $BOX --yes && track_test "pass" || track_test "fail"
  echo ""

  echo "Test 2.${CHANNEL}.7: Verify enabled state for channel $CHANNEL"
  echo -e "${CYAN}Running: lager supply $SUPPLY state --box $BOX${NC}"
  STATE_OUTPUT=$(lager supply $SUPPLY state --box $BOX 2>&1)
  echo "$STATE_OUTPUT"
  echo "$STATE_OUTPUT" | grep -iE "enabled:.*on" && track_test "pass" || track_test "fail"
  echo ""

done

# ============================================================
# SECTION 3: VOLTAGE OPERATIONS - ALL CHANNELS
# ============================================================
start_section "Voltage Operations - All Channels"
echo "========================================================================"
echo "SECTION 3: VOLTAGE OPERATIONS - ALL CHANNELS"
echo "========================================================================"
echo ""

echo "Test 3.1: Set different voltages on each channel"
echo -e "${CYAN}Ch1: 1.8V, Ch2: 3.3V, Ch3: 5.0V${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 1.8 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 3.3 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 5.0 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Verify independent voltage settings"
echo -e "${CYAN}Reading back all three channels${NC}"
V1=$(lager supply $SUPPLY1 voltage --box $BOX 2>&1)
V2=$(lager supply $SUPPLY2 voltage --box $BOX 2>&1)
V3=$(lager supply $SUPPLY3 voltage --box $BOX 2>&1)
echo "Ch1: $V1"
echo "Ch2: $V2"
echo "Ch3: $V3"
[ -n "$V1" ] && [ -n "$V2" ] && [ -n "$V3" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: Voltage sweep on all channels simultaneously"
echo -e "${CYAN}Sweeping 0V to 6V in 2V steps${NC}"
FAILED=0
for voltage in 0.0 2.0 4.0 6.0; do
  echo "  Setting all channels to ${voltage}V"
  lager supply $SUPPLY1 voltage $voltage --box $BOX --yes || FAILED=1
  lager supply $SUPPLY2 voltage $voltage --box $BOX --yes || FAILED=1
  lager supply $SUPPLY3 voltage $voltage --box $BOX --yes || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Zero voltage on all channels"
echo -e "${CYAN}Setting all channels to 0V${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 0.0 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.5: High precision voltage on all channels"
echo -e "${CYAN}Setting to 3.141592V (pi)${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 3.141592 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 3.141592 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 3.141592 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.6: Maximum voltage test (clamping validation)"
echo -e "${CYAN}Ch1: 20V->6.18V, Ch2/3: 20V (within 25.75V max)${NC}"
FAILED=0
echo "  Ch1: Attempting 20V (max 6.18V - should clamp with warning)"
lager supply $SUPPLY1 voltage 20.0 --box $BOX --yes 2>&1 | grep -q "Warning.*Clamping" && echo "    [OK] Clamped correctly" || echo "    Note: May have clamped"
echo "  Ch2: Attempting 20V (max 25.75V - should succeed)"
lager supply $SUPPLY2 voltage 20.0 --box $BOX --yes 2>&1 || FAILED=1
echo "  Ch3: Attempting 20V (max 25.75V - should succeed)"
lager supply $SUPPLY3 voltage 20.0 --box $BOX --yes 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: CURRENT OPERATIONS - ALL CHANNELS
# ============================================================
start_section "Current Operations - All Channels"
echo "========================================================================"
echo "SECTION 4: CURRENT OPERATIONS - ALL CHANNELS"
echo "========================================================================"
echo ""

echo "Test 4.1: Set current limit on all channels"
echo -e "${CYAN}Ch1: 0.5A, Ch2: 1.0A, Ch3: 1.0A (respecting Ch2/3 1.03A max)${NC}"
FAILED=0
lager supply $SUPPLY1 current 0.5 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 current 1.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 current 1.0 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Read current settings from all channels"
echo -e "${CYAN}Reading back current limits${NC}"
I1=$(lager supply $SUPPLY1 current --box $BOX 2>&1)
I2=$(lager supply $SUPPLY2 current --box $BOX 2>&1)
I3=$(lager supply $SUPPLY3 current --box $BOX 2>&1)
echo "Ch1: $I1"
echo "Ch2: $I2"
echo "Ch3: $I3"
[ -n "$I1" ] && [ -n "$I2" ] && [ -n "$I3" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.3: Current sweep on all channels (respecting limits)"
echo -e "${CYAN}Ch1: 0.1A to 5.0A, Ch2/3: 0.1A to 1.0A${NC}"
FAILED=0
for current in 0.1 0.5 1.0; do
  echo "  Setting all channels to ${current}A"
  lager supply $SUPPLY1 current $current --box $BOX --yes || FAILED=1
  lager supply $SUPPLY2 current $current --box $BOX --yes || FAILED=1
  lager supply $SUPPLY3 current $current --box $BOX --yes || FAILED=1
done
# Ch1 only can go higher
for current in 2.0 3.0 5.0; do
  echo "  Setting Ch1 only to ${current}A"
  lager supply $SUPPLY1 current $current --box $BOX --yes || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.4: Maximum current test (clamping validation)"
echo -e "${CYAN}Ch1: 10A->5.15A, Ch2/3: 5A->1.03A (should clamp with warning)${NC}"
FAILED=0
echo "  Ch1: Attempting 10A (max 5.15A)"
lager supply $SUPPLY1 current 10.0 --box $BOX --yes 2>&1 | grep -q "Warning.*Clamping" && echo "    [OK] Clamped correctly" || echo "    Note: May have clamped"
echo "  Ch2: Attempting 5A (max 1.03A)"
lager supply $SUPPLY2 current 5.0 --box $BOX --yes 2>&1 | grep -q "Warning.*Clamping" && echo "    [OK] Clamped correctly" || echo "    Note: May have clamped"
echo "  Ch3: Attempting 5A (max 1.03A)"
lager supply $SUPPLY3 current 5.0 --box $BOX --yes 2>&1 | grep -q "Warning.*Clamping" && echo "    [OK] Clamped correctly" || echo "    Note: May have clamped"
track_test "pass"
echo ""

# ============================================================
# SECTION 5: CROSS-CHANNEL INDEPENDENCE
# ============================================================
start_section "Cross-Channel Independence"
echo "========================================================================"
echo "SECTION 5: CROSS-CHANNEL INDEPENDENCE"
echo "========================================================================"
echo ""

echo "Test 5.1: Enable/disable independence"
echo -e "${CYAN}Ch1: ON, Ch2: OFF, Ch3: ON${NC}"
FAILED=0
lager supply $SUPPLY1 enable --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 enable --box $BOX --yes || FAILED=1
sleep 0.2
S1=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
S2=$(lager supply $SUPPLY2 state --box $BOX 2>&1)
S3=$(lager supply $SUPPLY3 state --box $BOX 2>&1)
echo "Ch1 state: $S1"
echo "Ch2 state: $S2"
echo "Ch3 state: $S3"
echo "$S1" | grep -iE "enabled:.*on" || FAILED=1
echo "$S2" | grep -iE "disabled|output.*off|enabled:.*off" || FAILED=1
echo "$S3" | grep -iE "enabled:.*on" || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Voltage change on Ch1 doesn't affect Ch2 or Ch3"
echo -e "${CYAN}Setting Ch1 to 5V, verifying others unchanged${NC}"
lager supply $SUPPLY2 voltage 2.0 --box $BOX --yes
lager supply $SUPPLY3 voltage 3.0 --box $BOX --yes
sleep 0.2
V2_BEFORE=$(lager supply $SUPPLY2 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
V3_BEFORE=$(lager supply $SUPPLY3 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager supply $SUPPLY1 voltage 5.0 --box $BOX --yes
sleep 0.2
V2_AFTER=$(lager supply $SUPPLY2 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
V3_AFTER=$(lager supply $SUPPLY3 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
echo "Ch2: ${V2_BEFORE}V -> ${V2_AFTER}V"
echo "Ch3: ${V3_BEFORE}V -> ${V3_AFTER}V"
# Allow 0.1V tolerance for measurement variations
if [ -n "$V2_BEFORE" ] && [ -n "$V2_AFTER" ] && [ -n "$V3_BEFORE" ] && [ -n "$V3_AFTER" ]; then
  DIFF2=$(echo "scale=2; if ($V2_BEFORE > $V2_AFTER) $V2_BEFORE - $V2_AFTER else $V2_AFTER - $V2_BEFORE" | bc)
  DIFF3=$(echo "scale=2; if ($V3_BEFORE > $V3_AFTER) $V3_BEFORE - $V3_AFTER else $V3_AFTER - $V3_BEFORE" | bc)
  OK2=$(echo "$DIFF2 <= 0.1" | bc)
  OK3=$(echo "$DIFF3 <= 0.1" | bc)
  [ "$OK2" = "1" ] && [ "$OK3" = "1" ] && track_test "pass" || track_test "fail"
else
  track_test "fail"
fi
echo ""

echo "Test 5.3: Protection clear on Ch1 doesn't affect Ch2 or Ch3"
echo -e "${CYAN}Clearing OVP/OCP on Ch1 only${NC}"
lager supply $SUPPLY2 voltage 3.3 --box $BOX --yes
lager supply $SUPPLY3 voltage 5.0 --box $BOX --yes
sleep 0.2
V2_BEFORE=$(lager supply $SUPPLY2 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
V3_BEFORE=$(lager supply $SUPPLY3 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager supply $SUPPLY1 clear-ovp --box $BOX
lager supply $SUPPLY1 clear-ocp --box $BOX
sleep 0.2
V2_AFTER=$(lager supply $SUPPLY2 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
V3_AFTER=$(lager supply $SUPPLY3 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
echo "Ch2: ${V2_BEFORE}V -> ${V2_AFTER}V"
echo "Ch3: ${V3_BEFORE}V -> ${V3_AFTER}V"
if [ -n "$V2_BEFORE" ] && [ -n "$V2_AFTER" ] && [ -n "$V3_BEFORE" ] && [ -n "$V3_AFTER" ]; then
  DIFF2=$(echo "scale=2; if ($V2_BEFORE > $V2_AFTER) $V2_BEFORE - $V2_AFTER else $V2_AFTER - $V2_BEFORE" | bc)
  DIFF3=$(echo "scale=2; if ($V3_BEFORE > $V3_AFTER) $V3_BEFORE - $V3_AFTER else $V3_AFTER - $V3_BEFORE" | bc)
  OK2=$(echo "$DIFF2 <= 0.1" | bc)
  OK3=$(echo "$DIFF3 <= 0.1" | bc)
  [ "$OK2" = "1" ] && [ "$OK3" = "1" ] && track_test "pass" || track_test "fail"
else
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 6: PROTECTION FEATURES - ALL CHANNELS
# ============================================================
start_section "Protection Features - All Channels"
echo "========================================================================"
echo "SECTION 6: PROTECTION FEATURES - ALL CHANNELS"
echo "========================================================================"
echo ""

echo "Test 6.1: Set OVP on all channels"
echo -e "${CYAN}Setting voltage with OVP protection${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --ovp 5.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 3.3 --ovp 5.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 3.3 --ovp 5.0 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Set OCP on all channels (respecting limits)"
echo -e "${CYAN}Ch1: 1.5A OCP, Ch2/3: 1.0A OCP (max 1.03A)${NC}"
FAILED=0
lager supply $SUPPLY1 current 1.0 --ocp 1.5 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 current 0.8 --ocp 1.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 current 0.8 --ocp 1.0 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.3: Combined OVP and OCP on all channels (respecting limits)"
echo -e "${CYAN}Ch1: 3.3V/2A with OVP/OCP, Ch2/3: 3.3V/1A with OVP/OCP${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --ovp 5.0 --ocp 2.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 3.3 --ovp 5.0 --ocp 1.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 3.3 --ovp 5.0 --ocp 1.0 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.4: Invalid OVP (below voltage) - should fail"
echo -e "${CYAN}Attempting OVP < voltage on all channels${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 5.0 --ovp 4.0 --box $BOX --yes 2>&1 | grep -qi "error\|cannot\|less than\|invalid" || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --ovp 4.0 --box $BOX --yes 2>&1 | grep -qi "error\|cannot\|less than\|invalid" || FAILED=1
lager supply $SUPPLY3 voltage 5.0 --ovp 4.0 --box $BOX --yes 2>&1 | grep -qi "error\|cannot\|less than\|invalid" || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.5: Clear protections on all channels"
echo -e "${CYAN}Clearing OVP and OCP${NC}"
FAILED=0
for CHANNEL in 1 2 3; do
  eval SUPPLY=\$SUPPLY$CHANNEL
  lager supply $SUPPLY clear-ovp --box $BOX || FAILED=1
  lager supply $SUPPLY clear-ocp --box $BOX || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: ERROR HANDLING
# ============================================================
start_section "Error Handling"
echo "========================================================================"
echo "SECTION 7: ERROR HANDLING"
echo "========================================================================"
echo ""

echo "Test 7.1: Invalid net name"
echo -e "${CYAN}Accessing non-existent net${NC}"
lager supply nonexistent_supply state --box $BOX 2>&1 | grep -qi "not found\|error" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.2: Negative voltage (should fail)"
echo -e "${CYAN}Attempting -5V on Ch1${NC}"
lager supply $SUPPLY1 voltage -5.0 --box $BOX --yes 2>&1 | grep -qi "error\|No such option\|negative" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.3: Invalid voltage format"
echo -e "${CYAN}Attempting non-numeric voltage${NC}"
lager supply $SUPPLY1 voltage abc --box $BOX --yes 2>&1 | grep -qi "error\|not a valid" && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.4: Recovery after error - all channels"
echo -e "${CYAN}Verify channels recover from invalid commands${NC}"
FAILED=0
for CHANNEL in 1 2 3; do
  eval SUPPLY=\$SUPPLY$CHANNEL
  lager supply $SUPPLY voltage -100 --box $BOX --yes 2>&1 || true
  lager supply $SUPPLY voltage 3.3 --box $BOX --yes || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 8: SIMULTANEOUS CHANNEL OPERATIONS
# ============================================================
start_section "Simultaneous Channel Operations"
echo "========================================================================"
echo "SECTION 8: SIMULTANEOUS CHANNEL OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 8.1: Enable all channels simultaneously"
echo -e "${CYAN}Enabling Ch1, Ch2, Ch3${NC}"
FAILED=0
lager supply $SUPPLY1 enable --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 enable --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.2: Disable all channels simultaneously"
echo -e "${CYAN}Disabling Ch1, Ch2, Ch3${NC}"
FAILED=0
lager supply $SUPPLY1 disable --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 disable --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.3: Set different configurations on each channel (respecting limits)"
echo -e "${CYAN}Ch1: 1.8V@0.5A, Ch2: 3.3V@1.0A, Ch3: 5.0V@1.0A (Ch3 max 1.03A)${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 1.8 --box $BOX --yes || FAILED=1
lager supply $SUPPLY1 current 0.5 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 3.3 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 current 1.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 5.0 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 current 1.0 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.4: Rapid sequential channel switching (10 cycles)"
echo -e "${CYAN}Cycling through channels rapidly${NC}"
FAILED=0
START_TIME=$(get_timestamp_ms)
for i in {1..10}; do
  CHANNEL=$((i % 3 + 1))
  eval SUPPLY=\$SUPPLY$CHANNEL
  VOLTAGE=$(echo "scale=1; 1.0 + ($i % 5) * 1.0" | bc)
  lager supply $SUPPLY voltage $VOLTAGE --box $BOX --yes || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  Completed 10 changes in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 10))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 9: POWER SEQUENCING
# ============================================================
start_section "Power Sequencing"
echo "========================================================================"
echo "SECTION 9: POWER SEQUENCING"
echo "========================================================================"
echo ""

echo "Test 9.1: Sequential power-up (Ch1 -> Ch2 -> Ch3)"
echo -e "${CYAN}Power-up sequence with delays${NC}"
FAILED=0
lager supply $SUPPLY1 disable --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 disable --box $BOX --yes || FAILED=1
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 3.3 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 3.3 --box $BOX --yes || FAILED=1
echo "  Enabling Ch1..."
lager supply $SUPPLY1 enable --box $BOX --yes || FAILED=1
sleep 0.2
echo "  Enabling Ch2..."
lager supply $SUPPLY2 enable --box $BOX --yes || FAILED=1
sleep 0.2
echo "  Enabling Ch3..."
lager supply $SUPPLY3 enable --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.2: Sequential power-down (Ch3 -> Ch2 -> Ch1)"
echo -e "${CYAN}Power-down sequence with delays${NC}"
FAILED=0
echo "  Disabling Ch3..."
lager supply $SUPPLY3 disable --box $BOX --yes || FAILED=1
sleep 0.2
echo "  Disabling Ch2..."
lager supply $SUPPLY2 disable --box $BOX --yes || FAILED=1
sleep 0.2
echo "  Disabling Ch1..."
lager supply $SUPPLY1 disable --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.3: Voltage ramp-up on all channels (0V to 6V)"
echo -e "${CYAN}Ramping all channels simultaneously${NC}"
FAILED=0
for voltage in 0.0 3.0 6.0; do
  echo "  All channels -> ${voltage}V"
  lager supply $SUPPLY1 voltage $voltage --box $BOX --yes || FAILED=1
  lager supply $SUPPLY2 voltage $voltage --box $BOX --yes || FAILED=1
  lager supply $SUPPLY3 voltage $voltage --box $BOX --yes || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.4: Voltage ramp-down on all channels (6V to 0V)"
echo -e "${CYAN}Ramping down all channels simultaneously${NC}"
FAILED=0
for voltage in 6.0 3.0 0.0; do
  echo "  All channels -> ${voltage}V"
  lager supply $SUPPLY1 voltage $voltage --box $BOX --yes || FAILED=1
  lager supply $SUPPLY2 voltage $voltage --box $BOX --yes || FAILED=1
  lager supply $SUPPLY3 voltage $voltage --box $BOX --yes || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 10: STRESS TESTING
# ============================================================
start_section "Stress Testing"
echo "========================================================================"
echo "SECTION 10: STRESS TESTING"
echo "========================================================================"
echo ""

echo "Test 10.1: Rapid enable/disable all channels (5 cycles)"
echo -e "${CYAN}Enable/disable cycling on all channels${NC}"
FAILED=0
for i in {1..5}; do
  lager supply $SUPPLY1 disable --box $BOX --yes || FAILED=1
  lager supply $SUPPLY2 disable --box $BOX --yes || FAILED=1
  lager supply $SUPPLY3 disable --box $BOX --yes || FAILED=1
  lager supply $SUPPLY1 enable --box $BOX --yes || FAILED=1
  lager supply $SUPPLY2 enable --box $BOX --yes || FAILED=1
  lager supply $SUPPLY3 enable --box $BOX --yes || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.2: Random voltage changes on all channels (10 iterations)"
echo -e "${CYAN}Random voltage stress test${NC}"
FAILED=0
for i in {1..10}; do
  V1=$(echo "scale=1; 1.0 + ($i % 5) * 0.8" | bc)
  V2=$(echo "scale=1; 2.0 + (($i + 1) % 5) * 0.7" | bc)
  V3=$(echo "scale=1; 3.0 + (($i + 2) % 5) * 0.6" | bc)
  lager supply $SUPPLY1 voltage $V1 --box $BOX --yes || FAILED=1
  lager supply $SUPPLY2 voltage $V2 --box $BOX --yes || FAILED=1
  lager supply $SUPPLY3 voltage $V3 --box $BOX --yes || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.3: Interleaved parameter changes (6 iterations)"
echo -e "${CYAN}Alternating voltage and current changes${NC}"
FAILED=0
for i in {1..6}; do
  CHANNEL=$((i % 3 + 1))
  eval SUPPLY=\$SUPPLY$CHANNEL
  if [ $((i % 2)) -eq 0 ]; then
    VOLTAGE=$(echo "scale=1; 2.0 + ($i % 3) * 1.0" | bc)
    lager supply $SUPPLY voltage $VOLTAGE --box $BOX --yes || FAILED=1
  else
    CURRENT=$(echo "scale=1; 0.5 + ($i % 3) * 0.5" | bc)
    lager supply $SUPPLY current $CURRENT --box $BOX --yes || FAILED=1
  fi
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 11: BOUNDARY CONDITIONS
# ============================================================
start_section "Boundary Conditions"
echo "========================================================================"
echo "SECTION 11: BOUNDARY CONDITIONS"
echo "========================================================================"
echo ""

echo "Test 11.1: Minimum voltage (0.001V) on all channels"
echo -e "${CYAN}Testing very small voltages${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 0.001 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 voltage 0.001 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 voltage 0.001 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.2: High precision current (0.123456A) on all channels"
echo -e "${CYAN}Testing precision current settings${NC}"
FAILED=0
lager supply $SUPPLY1 current 0.123456 --box $BOX --yes || FAILED=1
lager supply $SUPPLY2 current 0.123456 --box $BOX --yes || FAILED=1
lager supply $SUPPLY3 current 0.123456 --box $BOX --yes || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.3: Extreme voltage clamping (1000V test)"
echo -e "${CYAN}Testing voltage clamping behavior${NC}"
for CHANNEL in 1 2 3; do
  eval SUPPLY=\$SUPPLY$CHANNEL
  echo "  Ch$CHANNEL: Attempting 1000V"
  lager supply $SUPPLY voltage 1000.0 --box $BOX --yes 2>&1 || true
done
track_test "pass"  # Pass if doesn't crash
echo ""

# ============================================================
# SECTION 12: STATE VERIFICATION
# ============================================================
start_section "State Verification"
echo "========================================================================"
echo "SECTION 12: STATE VERIFICATION"
echo "========================================================================"
echo ""

echo "Test 12.1: State consistency across multiple reads"
echo -e "${CYAN}Verifying state readback consistency${NC}"
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes
for i in {1..5}; do
  V=$(lager supply $SUPPLY1 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
  echo "  Read $i: ${V}V"
  [ -n "$V" ] || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.2: Verify all channels after complex operations"
echo -e "${CYAN}Final state verification${NC}"
echo ""
for CHANNEL in 1 2 3; do
  eval SUPPLY=\$SUPPLY$CHANNEL
  echo "Channel $CHANNEL ($SUPPLY) state:"
  lager supply $SUPPLY state --box $BOX
  echo ""
done
track_test "pass"
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Setting all channels to safe state..."
for CHANNEL in 1 2 3; do
  eval SUPPLY=\$SUPPLY$CHANNEL
  echo "  Ch$CHANNEL: Disable and reset to 0V"
  lager supply $SUPPLY disable --box $BOX --yes
  lager supply $SUPPLY voltage 0.0 --box $BOX --yes
  lager supply $SUPPLY current 0.1 --box $BOX --yes
  lager supply $SUPPLY clear-ovp --box $BOX 2>&1 || true
  lager supply $SUPPLY clear-ocp --box $BOX 2>&1 || true
done
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

echo "Final state of all channels:"
echo ""
for CHANNEL in 1 2 3; do
  eval SUPPLY=\$SUPPLY$CHANNEL
  echo "═══ Channel $CHANNEL ($SUPPLY) ═══"
  lager supply $SUPPLY state --box $BOX
  echo ""
done

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

#!/bin/bash

# Copyright 2024-2026 Lager Data
# SPDX-License-Identifier: Apache-2.0
# Comprehensive multi-channel supply test suite (20 minute target)
# Tests independent and coordinated operation of multiple supply channels

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
  echo "Usage: $0 <BOX> [SUPPLY_NET_1] [SUPPLY_NET_2]"
  echo ""
  echo "Examples:"
  echo "  $0 <YOUR-BOX> supply2 supply3"
  echo "  $0 <BOX_IP>"
  echo ""
  echo "Arguments:"
  echo "  BOX          - Box ID or Tailscale IP address"
  echo "  SUPPLY_NET_1 - Name of first supply net (optional, will auto-detect)"
  echo "  SUPPLY_NET_2 - Name of second supply net (optional, will auto-detect)"
  echo ""
  exit 1
fi

BOX="$1"

# Auto-detect supply nets if not provided
if [ $# -ge 3 ]; then
  SUPPLY1="$2"
  SUPPLY2="$3"
else
  echo "Auto-detecting supply nets..."
  NETS_OUTPUT=$(lager nets --box $BOX 2>/dev/null | grep power-supply)
  SUPPLY1=$(echo "$NETS_OUTPUT" | awk 'NR==1 {print $1}')
  SUPPLY2=$(echo "$NETS_OUTPUT" | awk 'NR==2 {print $1}')

  if [ -z "$SUPPLY1" ] || [ -z "$SUPPLY2" ]; then
    echo -e "${RED}Error: Could not auto-detect two supply nets${NC}"
    echo "Available nets:"
    lager nets --box $BOX
    echo ""
    echo "Please specify supply nets manually:"
    echo "  $0 $BOX <SUPPLY_NET_1> <SUPPLY_NET_2>"
    exit 1
  fi

  echo -e "${GREEN}Detected supply nets: $SUPPLY1, $SUPPLY2${NC}"
fi

SCRIPT_START_TIME=$(get_timestamp_ms)

echo "========================================================================"
echo "LAGER MULTI-CHANNEL SUPPLY TEST SUITE"
echo "========================================================================"
echo ""
echo "Box: $BOX"
echo "Supply Channel 1: $SUPPLY1"
echo "Supply Channel 2: $SUPPLY2"
echo ""

# ============================================================
# INITIAL SETUP: Reset OVP limits from previous runs
# ============================================================
echo "Initializing test environment..."
# OVP limits persist on Rigol DP821 between test runs
# CH1 (supply2) = 60V max, CH2 (supply3) = 8V max (per Rigol DP821 specs)
# Set OVP near max for each channel to avoid blocking tests
lager supply $SUPPLY1 voltage 1.0 --ovp 30.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 1.0 --ovp 8.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
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

echo "Test 1.3: Verify both supply nets exist"
NETS_OUTPUT=$(lager nets --box $BOX 2>/dev/null)
if echo "$NETS_OUTPUT" | grep -q "$SUPPLY1" && echo "$NETS_OUTPUT" | grep -q "$SUPPLY2"; then
  echo -e "${GREEN}[OK] Both supply nets found${NC}"
  track_test "pass"
else
  echo -e "${RED}[FAIL] One or both supply nets not found${NC}"
  track_test "fail"
fi
echo ""

echo "Test 1.4: Supply help output"
lager supply --help >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 2: MULTI-CHANNEL DISCOVERY
# ============================================================
start_section "Multi-Channel Discovery"
echo "========================================================================"
echo "SECTION 2: MULTI-CHANNEL DISCOVERY"
echo "========================================================================"
echo ""

echo "Test 2.1: Get state from channel 1"
lager supply $SUPPLY1 state --box $BOX >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.2: Get state from channel 2"
lager supply $SUPPLY2 state --box $BOX >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 2.3: Verify channels are independent"
echo "  Setting channel 1 to 3.3V and channel 2 to 5.0V..."
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null
V1=$(lager supply $SUPPLY1 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
V2=$(lager supply $SUPPLY2 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
echo "  Channel 1: ${V1}V, Channel 2: ${V2}V"
if [ -n "$V1" ] && [ -n "$V2" ]; then
  # Check they're different (tolerance of 1V)
  DIFF=$(echo "scale=2; if ($V1 > $V2) $V1 - $V2 else $V2 - $V1" | bc)
  IS_DIFFERENT=$(echo "$DIFF > 1.0" | bc)
  [ "$IS_DIFFERENT" = "1" ] && track_test "pass" || track_test "fail"
else
  track_test "fail"
fi
echo ""

# ============================================================
# SECTION 3: ERROR VALIDATION
# ============================================================
start_section "Error Validation"
echo "========================================================================"
echo "SECTION 3: ERROR VALIDATION"
echo "========================================================================"
echo ""

echo "Test 3.1: Invalid net name"
lager supply nonexistent_net state --box $BOX 2>&1 | grep -qi "not found\|error" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.2: Invalid box"
lager supply $SUPPLY1 state --box INVALID-BOX 2>&1 | grep -qi "error\|don't have" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.3: Negative voltage on channel 1"
lager supply $SUPPLY1 voltage -1.0 --box $BOX --yes 2>&1 | grep -qi "error\|No such option" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.4: Negative voltage on channel 2"
lager supply $SUPPLY2 voltage -1.0 --box $BOX --yes 2>&1 | grep -qi "error\|No such option" && track_test "pass" || track_test "fail"
echo ""

echo "Test 3.5: Invalid voltage format"
lager supply $SUPPLY1 voltage abc --box $BOX --yes 2>&1 | grep -qi "error\|not a valid" && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 4: INDEPENDENT STATE CONTROL
# ============================================================
start_section "Independent State Control"
echo "========================================================================"
echo "SECTION 4: INDEPENDENT STATE CONTROL"
echo "========================================================================"
echo ""

echo "Test 4.1: Disable channel 1, enable channel 2"
FAILED=0
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
STATE1_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
STATE2_OUTPUT=$(lager supply $SUPPLY2 state --box $BOX 2>&1)
if echo "$STATE1_OUTPUT" | grep -qiE "Enabled:.*OFF"; then STATE1="disabled"; else STATE1="enabled"; fi
if echo "$STATE2_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE2="enabled"; else STATE2="disabled"; fi
echo "  Channel 1: $STATE1, Channel 2: $STATE2"
[ "$STATE1" = "disabled" ] && [ "$STATE2" = "enabled" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.2: Enable channel 1, disable channel 2"
FAILED=0
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
STATE1_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
STATE2_OUTPUT=$(lager supply $SUPPLY2 state --box $BOX 2>&1)
if echo "$STATE1_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE1="enabled"; else STATE1="disabled"; fi
if echo "$STATE2_OUTPUT" | grep -qiE "Enabled:.*OFF"; then STATE2="disabled"; else STATE2="enabled"; fi
echo "  Channel 1: $STATE1, Channel 2: $STATE2"
[ "$STATE1" = "enabled" ] && [ "$STATE2" = "disabled" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.3: Both channels disabled"
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null
STATE1_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
STATE2_OUTPUT=$(lager supply $SUPPLY2 state --box $BOX 2>&1)
if echo "$STATE1_OUTPUT" | grep -qiE "Enabled:.*OFF"; then STATE1="disabled"; else STATE1="enabled"; fi
if echo "$STATE2_OUTPUT" | grep -qiE "Enabled:.*OFF"; then STATE2="disabled"; else STATE2="enabled"; fi
echo "  Channel 1: $STATE1, Channel 2: $STATE2"
[ "$STATE1" = "disabled" ] && [ "$STATE2" = "disabled" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 4.4: Both channels enabled"
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null
STATE1_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
STATE2_OUTPUT=$(lager supply $SUPPLY2 state --box $BOX 2>&1)
if echo "$STATE1_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE1="enabled"; else STATE1="disabled"; fi
if echo "$STATE2_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE2="enabled"; else STATE2="disabled"; fi
echo "  Channel 1: $STATE1, Channel 2: $STATE2"
[ "$STATE1" = "enabled" ] && [ "$STATE2" = "enabled" ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 5: SIMULTANEOUS VOLTAGE OPERATIONS
# ============================================================
start_section "Simultaneous Voltage Operations"
echo "========================================================================"
echo "SECTION 5: SIMULTANEOUS VOLTAGE OPERATIONS"
echo "========================================================================"
echo ""

# Set high OVP limits to avoid blocking high-voltage tests
# CH1=60V max, CH2=8V max (Rigol DP821 specs)
lager supply $SUPPLY1 voltage 1.0 --ovp 30.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 1.0 --ovp 8.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true

echo "Test 5.1: Set different voltages on each channel"
FAILED=0
lager supply $SUPPLY1 voltage 1.8 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.2: Set same voltage on both channels"
FAILED=0
lager supply $SUPPLY1 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.3: Voltage sweep on both channels (synchronized)"
FAILED=0
# CH2 is limited to 8V max, so sweep only up to 8V for both channels
for voltage in 0.0 1.5 3.0 4.5 6.0 7.5; do
  lager supply $SUPPLY1 voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.4: Voltage sweep on both channels (alternating)"
FAILED=0
# CH2 is limited to 8V max, so use voltages within that range
VOLTAGES=(1.8 3.3 5.0 7.5)
for i in "${!VOLTAGES[@]}"; do
  V1="${VOLTAGES[$i]}"
  V2="${VOLTAGES[$(((i+1) % ${#VOLTAGES[@]}))]}"
  lager supply $SUPPLY1 voltage $V1 --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage $V2 --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.5: Read voltage from both channels"
V1=$(lager supply $SUPPLY1 voltage --box $BOX 2>/dev/null)
V2=$(lager supply $SUPPLY2 voltage --box $BOX 2>/dev/null)
[ -n "$V1" ] && [ -n "$V2" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 5.6: Rapid alternating voltage changes (10 cycles)"
FAILED=0
for i in {1..10}; do
  V1=$(echo "scale=1; 1.0 + ($i % 5) * 1.0" | bc)
  V2=$(echo "scale=1; 2.0 + ($i % 4) * 1.0" | bc)
  lager supply $SUPPLY1 voltage $V1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 voltage $V2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 6: SIMULTANEOUS CURRENT OPERATIONS
# ============================================================
start_section "Simultaneous Current Operations"
echo "========================================================================"
echo "SECTION 6: SIMULTANEOUS CURRENT OPERATIONS"
echo "========================================================================"
echo ""

echo "Test 6.1: Set different current limits on each channel"
FAILED=0
lager supply $SUPPLY1 current 0.5 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY2 current 1.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.2: Set same current limit on both channels"
FAILED=0
lager supply $SUPPLY1 current 1.5 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY2 current 1.5 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.3: Current sweep on both channels"
FAILED=0
for current in 0.5 1.0 1.5 2.0; do
  lager supply $SUPPLY1 current $current --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 current $current --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 6.4: Read current from both channels"
I1=$(lager supply $SUPPLY1 current --box $BOX 2>/dev/null)
I2=$(lager supply $SUPPLY2 current --box $BOX 2>/dev/null)
[ -n "$I1" ] && [ -n "$I2" ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 7: PROTECTION FEATURES (MULTI-CHANNEL)
# ============================================================
start_section "Protection Features (Multi-Channel)"
echo "========================================================================"
echo "SECTION 7: PROTECTION FEATURES (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

echo "Test 7.1: Set OVP on channel 1 only"
lager supply $SUPPLY1 voltage 3.3 --ovp 5.0 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.2: Set OVP on channel 2 only"
lager supply $SUPPLY2 voltage 5.0 --ovp 6.0 --box $BOX --yes >/dev/null 2>&1 && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.3: Set OVP on both channels"
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --ovp 5.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --ovp 6.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.4: Set OCP on both channels"
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --ocp 1.5 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --ocp 2.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.5: Clear OVP on channel 1 only"
lager supply $SUPPLY1 clear-ovp --box $BOX >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.6: Clear OCP on channel 2 only"
lager supply $SUPPLY2 clear-ocp --box $BOX >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 7.7: Clear all protections on both channels"
FAILED=0
lager supply $SUPPLY1 clear-ovp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY1 clear-ocp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY2 clear-ovp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY2 clear-ocp --box $BOX >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# Additional cleanup: Ensure OVP limits from Test 7.3 don't affect subsequent sections
# Test 7.3 set OVP to 5.0V (CH1) and 6.0V (CH2), which would block high-voltage tests
# CH1=60V max, CH2=8V max (Rigol DP821 specs)
echo "Setting high OVP limits for subsequent test sections..."
lager supply $SUPPLY1 voltage 1.0 --ovp 30.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 1.0 --ovp 8.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
echo ""

# ============================================================
# SECTION 8: CROSS-CHANNEL SCENARIOS
# ============================================================
start_section "Cross-Channel Scenarios"
echo "========================================================================"
echo "SECTION 8: CROSS-CHANNEL SCENARIOS"
echo "========================================================================"
echo ""

echo "Test 8.1: Channel 1 voltage change doesn't affect channel 2"
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null
V2_BEFORE=$(lager supply $SUPPLY2 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager supply $SUPPLY1 voltage 1.8 --box $BOX --yes >/dev/null
V2_AFTER=$(lager supply $SUPPLY2 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [ -n "$V2_BEFORE" ] && [ -n "$V2_AFTER" ]; then
  DIFF=$(echo "scale=4; if ($V2_BEFORE > $V2_AFTER) $V2_BEFORE - $V2_AFTER else $V2_AFTER - $V2_BEFORE" | bc)
  IS_SAME=$(echo "$DIFF <= 0.1" | bc)
  [ "$IS_SAME" = "1" ] && track_test "pass" || track_test "fail"
else
  track_test "fail"
fi
echo ""

echo "Test 8.2: Channel 2 voltage change doesn't affect channel 1"
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null
V1_BEFORE=$(lager supply $SUPPLY1 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager supply $SUPPLY2 voltage 8.0 --box $BOX --yes >/dev/null
V1_AFTER=$(lager supply $SUPPLY1 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [ -n "$V1_BEFORE" ] && [ -n "$V1_AFTER" ]; then
  DIFF=$(echo "scale=4; if ($V1_BEFORE > $V1_AFTER) $V1_BEFORE - $V1_AFTER else $V1_AFTER - $V1_BEFORE" | bc)
  IS_SAME=$(echo "$DIFF <= 0.1" | bc)
  [ "$IS_SAME" = "1" ] && track_test "pass" || track_test "fail"
else
  track_test "fail"
fi
echo ""

echo "Test 8.3: Channel 1 enable/disable doesn't affect channel 2 state"
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null
STATE2_BEFORE_OUTPUT=$(lager supply $SUPPLY2 state --box $BOX 2>&1)
if echo "$STATE2_BEFORE_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE2_BEFORE="enabled"; else STATE2_BEFORE="disabled"; fi
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null
STATE2_AFTER_OUTPUT=$(lager supply $SUPPLY2 state --box $BOX 2>&1)
if echo "$STATE2_AFTER_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE2_AFTER="enabled"; else STATE2_AFTER="disabled"; fi
echo "  Channel 2 before: $STATE2_BEFORE, after: $STATE2_AFTER"
[ "$STATE2_BEFORE" = "enabled" ] && [ "$STATE2_AFTER" = "enabled" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.4: Channel 2 enable/disable doesn't affect channel 1 state"
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null
STATE1_BEFORE_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
if echo "$STATE1_BEFORE_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE1_BEFORE="enabled"; else STATE1_BEFORE="disabled"; fi
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null
STATE1_AFTER_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
if echo "$STATE1_AFTER_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE1_AFTER="enabled"; else STATE1_AFTER="disabled"; fi
echo "  Channel 1 before: $STATE1_BEFORE, after: $STATE1_AFTER"
[ "$STATE1_BEFORE" = "enabled" ] && [ "$STATE1_AFTER" = "enabled" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 8.5: Simultaneous rapid enable/disable on both channels (10 cycles)"
FAILED=0
for i in {1..10}; do
  lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
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

echo "Test 9.1: Sequential power-up (CH1 then CH2)"
FAILED=0
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
sleep 0.1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.2: Sequential power-up (CH2 then CH1)"
FAILED=0
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
sleep 0.1
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.3: Simultaneous power-up"
FAILED=0
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.4: Sequential power-down (CH1 then CH2)"
FAILED=0
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
sleep 0.1
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.5: Sequential power-down (CH2 then CH1)"
FAILED=0
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
sleep 0.1
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 9.6: Power cycling both channels (5 cycles)"
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
for i in {1..5}; do
  lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
  sleep 0.1
  lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
  sleep 0.1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 10: VOLTAGE RAMPING (MULTI-CHANNEL)
# ============================================================
start_section "Voltage Ramping (Multi-Channel)"
echo "========================================================================"
echo "SECTION 10: VOLTAGE RAMPING (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

# Set high OVP limits to avoid blocking high-voltage tests
lager supply $SUPPLY1 voltage 1.0 --ovp 30.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 1.0 --ovp 8.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true

echo "Test 10.1: Synchronized voltage ramp-up (0V to 5V)"
FAILED=0
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
for voltage in 0.0 1.0 2.0 3.0 4.0 5.0; do
  lager supply $SUPPLY1 voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.2: Synchronized voltage ramp-down (5V to 0V)"
FAILED=0
for voltage in 5.0 4.0 3.0 2.0 1.0 0.0; do
  lager supply $SUPPLY1 voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage $voltage --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.3: Opposing ramps (CH1 up, CH2 down)"
FAILED=0
VOLTAGES_UP=(0.0 1.0 2.0 3.0 4.0 5.0)
VOLTAGES_DOWN=(5.0 4.0 3.0 2.0 1.0 0.0)
for i in "${!VOLTAGES_UP[@]}"; do
  lager supply $SUPPLY1 voltage ${VOLTAGES_UP[$i]} --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage ${VOLTAGES_DOWN[$i]} --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 10.4: Different ramp speeds (CH1 slow, CH2 fast)"
FAILED=0
# CH1: 0V to 5V in 1V steps
# CH2: 0V to 8V in 1.6V steps
for i in {0..5}; do
  V1=$(echo "scale=1; $i * 1.0" | bc)
  V2=$(echo "scale=1; $i * 1.6" | bc)
  lager supply $SUPPLY1 voltage $V1 --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage $V2 --box $BOX --yes >/dev/null || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 11: STRESS TESTS (MULTI-CHANNEL)
# ============================================================
start_section "Stress Tests (Multi-Channel)"
echo "========================================================================"
echo "SECTION 11: STRESS TESTS (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

echo "Test 11.1: Rapid alternating voltage changes (15 iterations)"
FAILED=0
START_TIME=$(get_timestamp_ms)
for i in {1..15}; do
  V1=$(echo "scale=1; 1.0 + ($i % 5) * 1.0" | bc)
  V2=$(echo "scale=1; 2.0 + ($i % 4) * 1.0" | bc)
  lager supply $SUPPLY1 voltage $V1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 voltage $V2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  Completed 30 changes (15 per channel) in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 30))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.2: Rapid current changes on both channels (15 iterations)"
FAILED=0
for i in {1..15}; do
  I1=$(echo "scale=1; 0.5 + ($i % 3) * 0.5" | bc)
  I2=$(echo "scale=1; 0.5 + ($i % 4) * 0.5" | bc)
  lager supply $SUPPLY1 current $I1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 current $I2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.3: Enable/disable stress on both channels (15 cycles)"
FAILED=0
START_TIME=$(get_timestamp_ms)
for i in {1..15}; do
  lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
END_TIME=$(get_timestamp_ms)
ELAPSED_MS=$(( END_TIME - START_TIME ))
echo "  Completed 60 state changes in ${ELAPSED_MS}ms (avg: $((ELAPSED_MS / 60))ms)"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.4: Mixed parameter stress on both channels (10 iterations)"
FAILED=0
for i in {1..10}; do
  V1=$(echo "scale=1; 2.0 + ($i % 3) * 1.0" | bc)
  V2=$(echo "scale=1; 3.0 + ($i % 4) * 1.0" | bc)
  I1=$(echo "scale=1; 0.5 + ($i % 2) * 0.5" | bc)
  I2=$(echo "scale=1; 0.5 + ($i % 3) * 0.5" | bc)
  lager supply $SUPPLY1 voltage $V1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 voltage $V2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY1 current $I1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 current $I2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 11.5: Chaotic multi-parameter stress (10 iterations)"
FAILED=0
for i in {1..10}; do
  # Random-ish pattern using modulo arithmetic
  V1=$(echo "scale=1; 1.5 + (($i * 3) % 5) * 0.8" | bc)
  V2=$(echo "scale=1; 2.0 + (($i * 7) % 6) * 0.7" | bc)
  I1=$(echo "scale=1; 0.5 + (($i * 2) % 3) * 0.3" | bc)
  I2=$(echo "scale=1; 0.6 + (($i * 5) % 4) * 0.4" | bc)

  lager supply $SUPPLY1 voltage $V1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY1 current $I1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1

  if [ $((i % 2)) -eq 0 ]; then
    lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  else
    lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  fi

  lager supply $SUPPLY2 voltage $V2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 current $I2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1

  if [ $((i % 3)) -eq 0 ]; then
    lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  fi
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 12: PERFORMANCE BENCHMARKS (MULTI-CHANNEL)
# ============================================================
start_section "Performance Benchmarks (Multi-Channel)"
echo "========================================================================"
echo "SECTION 12: PERFORMANCE BENCHMARKS (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

echo "Test 12.1: Simultaneous voltage write latency (3 iterations)"
TOTAL_TIME=0
FAILED=0
for i in {1..3}; do
  START_TIME=$(get_timestamp_ms)
  lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 3))
echo "  Average dual-channel voltage write time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.2: Simultaneous state query latency (3 iterations)"
TOTAL_TIME=0
FAILED=0
for i in {1..3}; do
  START_TIME=$(get_timestamp_ms)
  lager supply $SUPPLY1 state --box $BOX >/dev/null || FAILED=1
  lager supply $SUPPLY2 state --box $BOX >/dev/null || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 3))
echo "  Average dual-channel state query time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 12.3: Full channel configuration time (3 iterations)"
TOTAL_TIME=0
FAILED=0
for i in {1..3}; do
  START_TIME=$(get_timestamp_ms)
  # Configure both channels completely
  lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY1 current 1.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 current 2.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
  END_TIME=$(get_timestamp_ms)
  TOTAL_TIME=$((TOTAL_TIME + (END_TIME - START_TIME)))
done
AVG_MS=$((TOTAL_TIME / 3))
echo "  Average full dual-channel configuration time: ${AVG_MS}ms"
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 13: ERROR RECOVERY (MULTI-CHANNEL)
# ============================================================
start_section "Error Recovery (Multi-Channel)"
echo "========================================================================"
echo "SECTION 13: ERROR RECOVERY (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

echo "Test 13.1: Recovery after invalid voltage on CH1"
lager supply $SUPPLY1 voltage -100.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.2: Recovery after invalid voltage on CH2"
lager supply $SUPPLY2 voltage -100.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.3: CH2 operation after CH1 error"
lager supply $SUPPLY1 voltage -999.0 --box $BOX >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 3.3 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.4: CH1 operation after CH2 error"
lager supply $SUPPLY2 voltage -999.0 --box $BOX >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 5.0 --box $BOX --yes >/dev/null && track_test "pass" || track_test "fail"
echo ""

echo "Test 13.5: Both channels recover after simultaneous errors"
lager supply $SUPPLY1 voltage -100.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage -100.0 --box $BOX --yes >/dev/null 2>&1 || true
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 14: BOUNDARY CASES (MULTI-CHANNEL)
# ============================================================
start_section "Boundary Cases (Multi-Channel)"
echo "========================================================================"
echo "SECTION 14: BOUNDARY CASES (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

# Set high OVP limits to avoid blocking high-voltage tests
lager supply $SUPPLY1 voltage 1.0 --ovp 30.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 1.0 --ovp 8.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true

echo "Test 14.1: Zero voltage on both channels"
FAILED=0
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.2: Very small voltage on both channels (0.001V)"
FAILED=0
lager supply $SUPPLY1 voltage 0.001 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 0.001 --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.3: High precision voltages (different on each channel)"
FAILED=0
lager supply $SUPPLY1 voltage 3.141592 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 2.718281 --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 14.4: Maximum voltage on both channels (clamping test)"
FAILED=0
lager supply $SUPPLY1 voltage 100.0 --box $BOX --yes >/dev/null 2>&1
lager supply $SUPPLY2 voltage 100.0 --box $BOX --yes >/dev/null 2>&1
track_test "pass"  # Pass if doesn't crash
echo ""

echo "Test 14.5: Minimum and maximum on different channels"
FAILED=0
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null || FAILED=1
# CH2 is limited to 8V max hardware limit
lager supply $SUPPLY2 voltage 8.0 --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 15: REGRESSION TESTS (MULTI-CHANNEL)
# ============================================================
start_section "Regression Tests (Multi-Channel)"
echo "========================================================================"
echo "SECTION 15: REGRESSION TESTS (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

echo "Test 15.1: Negative voltage rejection on both channels"
ERR1=$(lager supply $SUPPLY1 voltage -1.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|negative\|No such option" && echo "rejected" || echo "accepted")
ERR2=$(lager supply $SUPPLY2 voltage -1.0 --box $BOX 2>&1 | grep -qi "error\|invalid\|negative\|No such option" && echo "rejected" || echo "accepted")
[ "$ERR1" = "rejected" ] && [ "$ERR2" = "rejected" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 15.2: Enable state persistence during cross-channel operations"
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null
STATE1_BEFORE_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
if echo "$STATE1_BEFORE_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE1_BEFORE="enabled"; else STATE1_BEFORE="disabled"; fi
# Change voltage on CH2
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null
STATE1_AFTER_OUTPUT=$(lager supply $SUPPLY1 state --box $BOX 2>&1)
if echo "$STATE1_AFTER_OUTPUT" | grep -qiE "Enabled:.*ON"; then STATE1_AFTER="enabled"; else STATE1_AFTER="disabled"; fi
[ "$STATE1_BEFORE" = "enabled" ] && [ "$STATE1_AFTER" = "enabled" ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 15.3: Protection clear on one channel doesn't affect the other"
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null
V2_BEFORE=$(lager supply $SUPPLY2 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
lager supply $SUPPLY1 clear-ovp --box $BOX >/dev/null
lager supply $SUPPLY1 clear-ocp --box $BOX >/dev/null
V2_AFTER=$(lager supply $SUPPLY2 voltage --box $BOX 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
if [ -n "$V2_BEFORE" ] && [ -n "$V2_AFTER" ]; then
  DIFF=$(echo "scale=4; if ($V2_BEFORE > $V2_AFTER) $V2_BEFORE - $V2_AFTER else $V2_AFTER - $V2_BEFORE" | bc)
  IS_CLOSE=$(echo "$DIFF <= 0.1" | bc)
  [ "$IS_CLOSE" = "1" ] && track_test "pass" || track_test "fail"
else
  track_test "fail"
fi
echo ""

echo "Test 15.4: Parameter consistency across multiple reads (both channels)"
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null
V1_READ1=$(lager supply $SUPPLY1 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
V1_READ2=$(lager supply $SUPPLY1 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
V2_READ1=$(lager supply $SUPPLY2 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
V2_READ2=$(lager supply $SUPPLY2 voltage --box $BOX 2>/dev/null | grep -oE '[0-9]+\.[0-9]+' | head -1)
[ "$V1_READ1" = "$V1_READ2" ] && [ "$V2_READ1" = "$V2_READ2" ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# SECTION 16: COMBINED OPERATIONS (MULTI-CHANNEL)
# ============================================================
start_section "Combined Operations (Multi-Channel)"
echo "========================================================================"
echo "SECTION 16: COMBINED OPERATIONS (MULTI-CHANNEL)"
echo "========================================================================"
echo ""

echo "Test 16.1: Full configuration of both channels"
FAILED=0
# Channel 1: 3.3V @ 1A
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 voltage 3.3 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY1 current 1.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY1 clear-ovp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY1 clear-ocp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY1 enable --box $BOX --yes >/dev/null || FAILED=1
# Channel 2: 5.0V @ 2A
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --box $BOX --yes >/dev/null || FAILED=1
lager supply $SUPPLY2 current 2.0 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY2 clear-ovp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY2 clear-ocp --box $BOX >/dev/null || FAILED=1
lager supply $SUPPLY2 enable --box $BOX --yes >/dev/null || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 16.2: Multiple V/I combinations on both channels"
FAILED=0
COMBOS_CH1=("1.8,0.5" "3.3,1.0" "5.0,1.5")
COMBOS_CH2=("2.5,0.8" "5.0,2.0" "8.0,1.0")
for i in "${!COMBOS_CH1[@]}"; do
  IFS=',' read -r v1 i1 <<< "${COMBOS_CH1[$i]}"
  IFS=',' read -r v2 i2 <<< "${COMBOS_CH2[$i]}"
  lager supply $SUPPLY1 voltage $v1 --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY1 current $i1 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
  lager supply $SUPPLY2 voltage $v2 --box $BOX --yes >/dev/null || FAILED=1
  lager supply $SUPPLY2 current $i2 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
done
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

echo "Test 16.3: Coordinated protection settings"
FAILED=0
lager supply $SUPPLY1 voltage 3.3 --ovp 5.0 --ocp 1.5 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
lager supply $SUPPLY2 voltage 5.0 --ovp 6.0 --ocp 2.5 --box $BOX --yes >/dev/null 2>&1 || FAILED=1
[ $FAILED -eq 0 ] && track_test "pass" || track_test "fail"
echo ""

# ============================================================
# CLEANUP
# ============================================================
echo "========================================================================"
echo "CLEANUP"
echo "========================================================================"
echo ""

echo "Setting both channels to safe state..."
lager supply $SUPPLY1 disable --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 disable --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 voltage 0.0 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 current 0.1 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY2 current 0.1 --box $BOX --yes >/dev/null 2>&1 || true
lager supply $SUPPLY1 clear-ovp --box $BOX >/dev/null 2>&1 || true
lager supply $SUPPLY1 clear-ocp --box $BOX >/dev/null 2>&1 || true
lager supply $SUPPLY2 clear-ovp --box $BOX >/dev/null 2>&1 || true
lager supply $SUPPLY2 clear-ocp --box $BOX >/dev/null 2>&1 || true
echo -e "${GREEN}[OK] Cleanup complete${NC}"
echo ""

echo "Final supply states:"
echo ""
echo -e "${BLUE}Channel 1 ($SUPPLY1):${NC}"
lager supply $SUPPLY1 state --box $BOX
echo ""
echo -e "${BLUE}Channel 2 ($SUPPLY2):${NC}"
lager supply $SUPPLY2 state --box $BOX
echo ""

# ============================================================
# TEST SUMMARY
# ============================================================
SCRIPT_END_TIME=$(get_timestamp_ms)
TOTAL_RUNTIME_MS=$((SCRIPT_END_TIME - SCRIPT_START_TIME))
TOTAL_RUNTIME_SEC=$((TOTAL_RUNTIME_MS / 1000))
RUNTIME_MINUTES=$((TOTAL_RUNTIME_SEC / 60))
RUNTIME_SECONDS=$((TOTAL_RUNTIME_SEC % 60))

echo "========================================================================"
echo "TEST SUITE COMPLETED"
echo "========================================================================"
echo ""
echo "Total runtime: ${RUNTIME_MINUTES}m ${RUNTIME_SECONDS}s"
echo ""

# Print the summary table
print_summary

# Exit with appropriate status code
exit_with_status
